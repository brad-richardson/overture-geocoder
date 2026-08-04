"""Tests for the `.plrx` reverse serving shard format (reverse R1-b).

Three independent implementations meet here: the Rust encoder
(`reverse-encode-v1`), the Rust verifier (`reverse-verify-v1`, sharing no code
with the encoder) and the Python oracle (`reverse_shard_v1.ReverseShard`). The
suite is organised around the design's increment-1 gate
(docs/plans/2026-07-25-reverse-v2-design.md section 6):

* encode -> verify green, and the oracle decodes exactly the input rows;
* byte determinism across runs;
* a dense L=5 cell where EVERY 3x3 leaf block resolves to exactly 3 payload
  runs (row-major payload order), exhaustively over all 900 blocks;
* the three-ceilings depth rule end to end where record counts stay testable
  (density-driven L, the L_lon clamp at a high-latitude row) plus the pinned
  L_lat ceilings at unit level;
* fail-closed encoder behaviour and verifier tamper detection;
* mean record size against the design section-5 model (plus the 16-byte source
  locator its own section-2 order requires, and the address level-count u16);
* the `L_lon` table pinned against the same committed fixture the two Rust
  mirrors are pinned against.
"""

from __future__ import annotations

import importlib.util
import json
import math
import random
import struct
import subprocess
import sys
from pathlib import Path

import pytest


pa = pytest.importorskip("pyarrow")
ipc = pytest.importorskip("pyarrow.ipc")

ROOT = Path(__file__).parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


REVERSE = _load("reverse_shard_module", "scripts/reverse_shard_v1.py")

L_LON_FIXTURE = ROOT / "tests/fixtures/reverse/l-lon-by-row-v1.json"

# Places retain the section-5 model plus the 16-byte source locator. Addresses
# use the measured mean after repeated strings moved into shard dictionaries.
#
# ARDX0002 sizes each field's codes from its own cardinality, so this fixture's
# low-cardinality fields encode as one byte each rather than a fixed two. That
# drops the measured address mean from 84 to 78 bytes here. Real planet cells
# widen their two highest-cardinality fields instead, so this fixture's gain is
# not the planet number -- see PR #218 for the per-field byte accounting.
PLACES_MODEL_BYTES = 96 + 16
ADDRESS_MODEL_BYTES = 78

PLACES_SCHEMA = pa.schema(
    [
        ("feature_id", pa.binary(16)),
        ("partition_cell", pa.string()),
        ("longitude", pa.float64()),
        ("latitude", pa.float64()),
        ("primary_name", pa.string()),
        ("brand_name", pa.string()),
        ("category", pa.string()),
        ("locality", pa.string()),
        ("region", pa.string()),
        ("country", pa.string()),
        ("confidence_rank", pa.uint8()),
        ("source_object_index", pa.uint32()),
        ("source_row_group", pa.uint32()),
        ("source_row_index", pa.uint64()),
    ]
)

ADDRESS_SCHEMA = pa.schema(
    [
        ("feature_id", pa.binary(16)),
        ("partition_cell", pa.string()),
        ("longitude_e7", pa.int32()),
        ("latitude_e7", pa.int32()),
        ("display_country", pa.string()),
        ("postal_city", pa.string()),
        ("postcode", pa.string()),
        ("street", pa.string()),
        ("number", pa.string()),
        ("unit", pa.string()),
        ("address_levels", pa.list_(pa.string())),
        ("source_object_index", pa.uint32()),
        ("source_row_group", pa.uint32()),
        ("source_row_index", pa.uint64()),
    ]
)

PLACES_TEXT_FIELDS = (
    "primary_name",
    "brand_name",
    "category",
    "locality",
    "region",
    "country",
)
ADDRESS_TEXT_FIELDS = (
    "display_country",
    "postal_city",
    "postcode",
    "street",
    "number",
    "unit",
)


@pytest.fixture(scope="module")
def binaries() -> dict[str, Path]:
    subprocess.run(
        [
            "cargo",
            "build",
            "-p",
            "geocoder-construction",
            "--bin",
            "reverse-encode-v1",
            "--bin",
            "reverse-verify-v1",
        ],
        cwd=ROOT / "crates",
        check=True,
    )
    target = ROOT / "crates/target/debug"
    return {"encode": target / "reverse-encode-v1", "verify": target / "reverse-verify-v1"}


def leaf_yx(digits: str) -> tuple[int, int]:
    y = x = 0
    for char in digits:
        value = ord(char) - ord("0")
        y = (y << 1) | (value >> 1)
        x = (x << 1) | (value & 1)
    return y, x


def leaf_digits_from_yx(y: int, x: int, level: int) -> str:
    return "".join(
        str((((y >> bit) & 1) << 1) | ((x >> bit) & 1))
        for bit in range(level - 1, -1, -1)
    )


def places_e7(row: dict) -> tuple[int, int]:
    # Python round() is ties-even on floats, matching Rust round_ties_even.
    longitude, latitude = row["longitude"], row["latitude"]
    if not (math.isfinite(longitude) and math.isfinite(latitude)):
        return 0, 0  # ordering placeholder; the encoder fail-closes on the row
    return round(longitude * 1e7), round(latitude * 1e7)


def order_rows(rows: list[dict], cell: str, level: int, family: str) -> list[dict]:
    """Row-major leaf order, then (feature_id, source locator) within a leaf."""

    def key(row: dict):
        if family == "places":
            longitude_e7, latitude_e7 = places_e7(row)
        else:
            longitude_e7, latitude_e7 = row["longitude_e7"], row["latitude_e7"]
        digits = REVERSE.leaf_digits_e7(longitude_e7, latitude_e7, cell, level)
        return (
            *leaf_yx(digits),
            row["feature_id"],
            row["source_object_index"],
            row["source_row_group"],
            row["source_row_index"],
        )

    return sorted(rows, key=key)


def write_ipc(path: Path, rows: list[dict], schema: pa.Schema) -> None:
    table = pa.Table.from_pylist(rows, schema=schema)
    with ipc.new_stream(path, schema) as writer:
        for batch in table.to_batches(max_chunksize=65_536):
            writer.write_batch(batch)


def run_encode(
    binaries: dict[str, Path],
    directory: Path,
    name: str,
    rows: list[dict],
    *,
    family: str,
    cell: str,
    records: int | None = None,
    digest: bool = True,
    presorted: bool = False,
) -> tuple[subprocess.CompletedProcess, Path, Path]:
    records = len(rows) if records is None else records
    depth_family = REVERSE.DEPTH_FAMILY_BY_SERVING[family]
    level = REVERSE.sub_cell_level(records, cell, depth_family)
    if not presorted:
        rows = order_rows(rows, cell, level, family)
    source = directory / f"{name}.arrow"
    output = directory / f"{name}.plrx"
    sidecar = directory / f"{name}.digest.json"
    write_ipc(source, rows, PLACES_SCHEMA if family == "places" else ADDRESS_SCHEMA)
    command = [
        str(binaries["encode"]),
        "--input",
        str(source),
        "--output",
        str(output),
        "--family",
        family,
        "--cell",
        cell,
        "--records",
        str(records),
    ]
    if digest:
        command += ["--digest-out", str(sidecar)]
    process = subprocess.run(command, capture_output=True, text=True)
    return process, output, sidecar


def run_verify(
    binaries: dict[str, Path],
    artifact: Path,
    *,
    family: str,
    cell: str,
    records: int,
    sidecar: Path | None = None,
) -> subprocess.CompletedProcess:
    command = [
        str(binaries["verify"]),
        "--input",
        str(artifact),
        "--family",
        family,
        "--cell",
        cell,
        "--records",
        str(records),
    ]
    if sidecar is not None:
        command += ["--digest", str(sidecar)]
    return subprocess.run(command, capture_output=True, text=True)


def places_rows(count: int, cell: str = "8080", seed: int = 7) -> list[dict]:
    """Rows inside `cell` with text sized to the section-5 places model
    (name 20, brand 2, category 15, locality 10, region 6, country 2)."""
    generator = random.Random(seed)
    y, x = REVERSE.cell_yx(cell)
    rows = []
    for index in range(count):
        longitude_e7 = (
            x * REVERSE.LONGITUDE_E7_PER_CELL
            - REVERSE.LONGITUDE_E7_ORIGIN
            + generator.randrange(REVERSE.LONGITUDE_E7_PER_CELL)
        )
        latitude_e7 = (
            y * REVERSE.LATITUDE_E7_PER_CELL
            - REVERSE.LATITUDE_E7_ORIGIN
            + generator.randrange(REVERSE.LATITUDE_E7_PER_CELL)
        )
        name_length = 18 + index % 5  # mean 20
        rows.append(
            {
                "feature_id": index.to_bytes(16, "big"),
                "partition_cell": cell,
                "longitude": longitude_e7 / 1e7,
                "latitude": latitude_e7 / 1e7,
                "primary_name": f"Place {index:06d}".ljust(name_length, "x")[
                    :name_length
                ],
                "brand_name": "Br",
                "category": "eat_and_drink.x",
                "locality": "Springfiel",
                "region": "Region",
                "country": "US",
                "confidence_rank": index % 256,
                "source_object_index": index % 3,
                "source_row_group": index % 5,
                "source_row_index": index,
            }
        )
    return rows


def address_rows(count: int, cell: str = "8080", seed: int = 11) -> list[dict]:
    """Rows sized to the section-5 address model (street 18, number 4, unit 1,
    postcode 7, postal_city 12, two 8-byte levels, display_country 2)."""
    generator = random.Random(seed)
    y, x = REVERSE.cell_yx(cell)
    rows = []
    for index in range(count):
        longitude_e7 = (
            x * REVERSE.LONGITUDE_E7_PER_CELL
            - REVERSE.LONGITUDE_E7_ORIGIN
            + generator.randrange(REVERSE.LONGITUDE_E7_PER_CELL)
        )
        latitude_e7 = (
            y * REVERSE.LATITUDE_E7_PER_CELL
            - REVERSE.LATITUDE_E7_ORIGIN
            + generator.randrange(REVERSE.LATITUDE_E7_PER_CELL)
        )
        rows.append(
            {
                "feature_id": index.to_bytes(16, "big"),
                "partition_cell": cell,
                "longitude_e7": longitude_e7,
                "latitude_e7": latitude_e7,
                "display_country": "US",
                "postal_city": "Springfield1",
                "postcode": "9812345",
                "street": f"Main Street {index % 10000:04d}xx"[:18],
                "number": f"{index % 10000:04d}",
                "unit": "A",
                "address_levels": ["Level Aa", "Level Bb"],
                "source_object_index": index % 3,
                "source_row_group": index % 5,
                "source_row_index": index,
            }
        )
    return rows


def expected_grouping(
    rows: list[dict], cell: str, level: int, family: str
) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in order_rows(rows, cell, level, family):
        if family == "places":
            longitude_e7, latitude_e7 = places_e7(row)
        else:
            longitude_e7, latitude_e7 = row["longitude_e7"], row["latitude_e7"]
        digits = REVERSE.leaf_digits_e7(longitude_e7, latitude_e7, cell, level)
        expected = {
            "feature_id": row["feature_id"],
            "longitude_e7": longitude_e7,
            "latitude_e7": latitude_e7,
            "source_object_index": row["source_object_index"],
            "source_row_group": row["source_row_group"],
            "source_row_index": row["source_row_index"],
        }
        fields = PLACES_TEXT_FIELDS if family == "places" else ADDRESS_TEXT_FIELDS
        for field in fields:
            expected[field] = row[field]
        if family == "places":
            expected["confidence_rank"] = row["confidence_rank"]
        else:
            expected["address_levels"] = row["address_levels"]
        grouped.setdefault(REVERSE.leaf_key(cell, digits), []).append(expected)
    return grouped


# --------------------------------------------------------------------------- #
# the pinned L_lon table
# --------------------------------------------------------------------------- #
def test_l_lon_table_is_pinned_by_the_shared_fixture():
    """The committed JSON fixture equals the Python module's table for every
    one of the 128 rows in both families. The two Rust mirrors (encoder and
    verifier) each assert the same equality against the same file in their
    `cargo test` unit tests, so Rust == Python transitively, byte-exact."""
    payload = json.loads(L_LON_FIXTURE.read_text())
    assert payload["schema"] == "overture-reverse-l-lon-by-row-v1"
    assert payload["rows"] == 128
    assert payload["families"] == {
        family: list(table) for family, table in REVERSE.L_LON_BY_ROW.items()
    }


# --------------------------------------------------------------------------- #
# roundtrip: encode -> verify -> oracle decode, both families
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def places_artifact(binaries, tmp_path_factory):
    rows = places_rows(4_100)
    directory = tmp_path_factory.mktemp("plrx-places")
    process, output, sidecar = run_encode(
        binaries, directory, "places", rows, family="places", cell="8080"
    )
    assert process.returncode == 0, process.stderr
    return rows, output, sidecar


@pytest.fixture(scope="module")
def address_artifact(binaries, tmp_path_factory):
    rows = address_rows(2_100)
    directory = tmp_path_factory.mktemp("plrx-addresses")
    process, output, sidecar = run_encode(
        binaries, directory, "addresses", rows, family="addresses", cell="8080"
    )
    assert process.returncode == 0, process.stderr
    return rows, output, sidecar


def roundtrip(binaries, rows, output, sidecar, family: str):
    process = run_verify(
        binaries,
        output,
        family=family,
        cell="8080",
        records=len(rows),
        sidecar=sidecar,
    )
    assert process.returncode == 0, process.stderr
    shard = REVERSE.ReverseShard(output.read_bytes())
    assert shard.records == len(rows)
    assert shard.family == family
    assert shard.cell == "8080"
    assert shard.sub_cell_level == 1  # density-driven: 2048 < rows <= 2048 * 4
    grouped = expected_grouping(rows, "8080", shard.sub_cell_level, family)
    ranges = shard.leaf_ranges()
    assert {leaf.key for leaf in ranges} == set(grouped)
    decoded_total = 0
    for leaf in ranges:
        decoded = shard.decode_leaf(leaf.key)
        assert decoded == grouped[leaf.key]
        decoded_total += len(decoded)
    assert decoded_total == len(rows)
    return shard


def test_places_roundtrip_decodes_exactly_the_input(binaries, places_artifact):
    rows, output, sidecar = places_artifact
    roundtrip(binaries, rows, output, sidecar, "places")


def test_address_roundtrip_decodes_exactly_the_input(binaries, address_artifact):
    rows, output, sidecar = address_artifact
    roundtrip(binaries, rows, output, sidecar, "addresses")


def test_encoding_is_byte_deterministic(binaries, tmp_path, places_artifact):
    rows, output, sidecar = places_artifact
    process, second_output, second_sidecar = run_encode(
        binaries, tmp_path, "again", rows, family="places", cell="8080"
    )
    assert process.returncode == 0, process.stderr
    assert second_output.read_bytes() == output.read_bytes()
    assert second_sidecar.read_bytes() == sidecar.read_bytes()


def test_mean_record_size_matches_the_size_model(places_artifact, address_artifact):
    for (rows, output, _), model in (
        (places_artifact, PLACES_MODEL_BYTES),
        (address_artifact, ADDRESS_MODEL_BYTES),
    ):
        shard = REVERSE.ReverseShard(output.read_bytes())
        mean = (shard.index_offset - 32) / len(rows)
        assert abs(mean - model) <= 0.05 * model, f"mean {mean} vs model {model}"


# --------------------------------------------------------------------------- #
# the dense-cell gate: 3x3 leaf blocks are exactly 3 payload runs
# --------------------------------------------------------------------------- #
@pytest.mark.slow  # ~8s: builds and encodes a dense synthetic cell end to end.
def test_dense_cell_3x3_blocks_resolve_to_exactly_3_runs(binaries, tmp_path):
    """Increment 1's gate. A fully populated L=5 cell (Tokyo b2e3): with
    row-major payload order, EVERY one of the 900 possible 3x3 leaf blocks is
    exactly 3 contiguous payload runs -- one per leaf row. Under quadkey
    (Morton) payload order this is 4-5 runs in every case."""
    cell = "b2e3"
    y, x = REVERSE.cell_yx(cell)
    per_leaf = 513  # 1024 leaves * 513 = 525,312 > 2048 * 4**4 forces L=5
    rows = []
    index = 0
    for leaf_y in range(32):
        for leaf_x in range(32):
            longitude_e7 = (
                x * REVERSE.LONGITUDE_E7_PER_CELL
                - REVERSE.LONGITUDE_E7_ORIGIN
                + (2 * leaf_x + 1) * REVERSE.LONGITUDE_E7_PER_CELL // 64
            )
            latitude_e7 = (
                y * REVERSE.LATITUDE_E7_PER_CELL
                - REVERSE.LATITUDE_E7_ORIGIN
                + (2 * leaf_y + 1) * REVERSE.LATITUDE_E7_PER_CELL // 64
            )
            for _ in range(per_leaf):
                rows.append(
                    {
                        "feature_id": index.to_bytes(16, "big"),
                        "partition_cell": cell,
                        "longitude": longitude_e7 / 1e7,
                        "latitude": latitude_e7 / 1e7,
                        "primary_name": "",
                        "brand_name": "",
                        "category": "",
                        "locality": "",
                        "region": "",
                        "country": "",
                        "confidence_rank": 0,
                        "source_object_index": 0,
                        "source_row_group": 0,
                        "source_row_index": index,
                    }
                )
                index += 1
    process, output, _ = run_encode(
        binaries,
        tmp_path,
        "dense",
        rows,
        family="places",
        cell=cell,
        digest=False,
        presorted=True,
    )
    assert process.returncode == 0, process.stderr
    process = run_verify(binaries, output, family="places", cell=cell, records=len(rows))
    assert process.returncode == 0, process.stderr
    shard = REVERSE.ReverseShard(output.read_bytes())
    assert shard.sub_cell_level == 5
    ranges = {leaf.key: leaf for leaf in shard.leaf_ranges()}
    assert len(ranges) == 1024
    for block_y in range(30):
        for block_x in range(30):
            extents = sorted(
                (
                    ranges[
                        cell + leaf_digits_from_yx(block_y + dy, block_x + dx, 5)
                    ].payload_offset,
                    ranges[
                        cell + leaf_digits_from_yx(block_y + dy, block_x + dx, 5)
                    ].payload_bytes,
                )
                for dy in range(3)
                for dx in range(3)
            )
            runs = 1
            for (offset, length), (next_offset, _) in zip(extents, extents[1:]):
                if offset + length != next_offset:
                    runs += 1
            assert runs == 3, f"block ({block_y}, {block_x}) spans {runs} runs"


# --------------------------------------------------------------------------- #
# the depth rule, end to end where record counts stay testable
# --------------------------------------------------------------------------- #
def test_records_drive_the_sub_cell_level(binaries, tmp_path):
    rows = places_rows(2_049)
    process, output, _ = run_encode(
        binaries, tmp_path, "depth", rows, family="places", cell="8080", digest=False
    )
    assert process.returncode == 0, process.stderr
    assert REVERSE.ReverseShard(output.read_bytes()).sub_cell_level == 1


def test_l_lon_clamps_the_level_at_a_high_latitude_row(binaries, tmp_path):
    """Cell f700 (row index 119, ~84 degrees north): density alone would give
    L=3 at 33,000 records, but the poi longitude ceiling caps the row at 2."""
    rows = places_rows(33_000, cell="f700")
    process, output, _ = run_encode(
        binaries, tmp_path, "polar", rows, family="places", cell="f700", digest=False
    )
    assert process.returncode == 0, process.stderr
    assert REVERSE.ReverseShard(output.read_bytes()).sub_cell_level == 2


def test_l_lat_ceiling_is_pinned_per_family():
    """The family latitude ceilings (5 poi / 7 address) bind only above ~2M and
    ~8.4M records, out of end-to-end test range; the Python contract is pinned
    here and both Rust mirrors pin the identical cases in their unit tests."""
    assert REVERSE.sub_cell_level(10**9, "8080", "poi") == 5
    assert REVERSE.sub_cell_level(10**9, "8080", "address") == 7


# --------------------------------------------------------------------------- #
# fail-closed encoding
# --------------------------------------------------------------------------- #
def test_encoder_rejects_unsorted_input(binaries, tmp_path):
    rows = order_rows(places_rows(8), "8080", 0, "places")
    rows[2], rows[5] = rows[5], rows[2]
    process, _, _ = run_encode(
        binaries,
        tmp_path,
        "unsorted",
        rows,
        family="places",
        cell="8080",
        presorted=True,
    )
    assert process.returncode != 0
    assert "row-major" in process.stderr


def test_encoder_rejects_a_wrong_cell_row(binaries, tmp_path):
    rows = places_rows(8)
    rows[3]["partition_cell"] = "8081"
    process, _, _ = run_encode(
        binaries, tmp_path, "wrong-cell", rows, family="places", cell="8080"
    )
    assert process.returncode != 0
    assert "differs from --cell" in process.stderr


def test_encoder_rejects_non_finite_and_out_of_world_coordinates(binaries, tmp_path):
    rows = places_rows(4)
    rows[1]["longitude"] = float("nan")
    process, _, _ = run_encode(
        binaries, tmp_path, "nan", rows, family="places", cell="8080"
    )
    assert process.returncode != 0
    assert "finite world position" in process.stderr

    rows = places_rows(4)
    rows[2]["latitude"] = 90.5
    process, _, _ = run_encode(
        binaries, tmp_path, "out-of-world", rows, family="places", cell="8080"
    )
    assert process.returncode != 0
    assert "finite world position" in process.stderr

    rows = address_rows(4)
    rows[1]["longitude_e7"] = 1_800_000_001
    process, _, _ = run_encode(
        binaries, tmp_path, "e7-out", rows, family="addresses", cell="8080"
    )
    assert process.returncode != 0
    assert "finite world position" in process.stderr


def test_encoder_rejects_a_record_count_mismatch(binaries, tmp_path):
    rows = places_rows(8)
    process, _, _ = run_encode(
        binaries, tmp_path, "count", rows, family="places", cell="8080", records=9
    )
    assert process.returncode != 0
    assert "--records declared" in process.stderr


def test_encoder_rejects_a_u16_text_overflow(binaries, tmp_path):
    rows = places_rows(4)
    rows[0]["primary_name"] = "x" * 70_000
    process, _, _ = run_encode(
        binaries, tmp_path, "overflow", rows, family="places", cell="8080"
    )
    assert process.returncode != 0
    assert "exceeds u16" in process.stderr


# --------------------------------------------------------------------------- #
# the verifier fails closed
# --------------------------------------------------------------------------- #
def test_verifier_rejects_a_corrupted_payload_byte(
    binaries, tmp_path, places_artifact
):
    rows, output, sidecar = places_artifact
    corrupted = bytearray(output.read_bytes())
    # High byte of the first record's lon_e7 (header 32 + length 4 + id 16 + 3).
    corrupted[32 + 4 + 16 + 3] ^= 0x40
    tampered = tmp_path / "tampered.plrx"
    tampered.write_bytes(bytes(corrupted))
    process = run_verify(
        binaries,
        tampered,
        family="places",
        cell="8080",
        records=len(rows),
        sidecar=sidecar,
    )
    assert process.returncode != 0


def test_verifier_rejects_wrong_declarations(binaries, places_artifact):
    rows, output, _ = places_artifact
    process = run_verify(
        binaries, output, family="places", cell="8080", records=len(rows) - 1
    )
    assert process.returncode != 0
    process = run_verify(
        binaries, output, family="addresses", cell="8080", records=len(rows)
    )
    assert process.returncode != 0


def test_verifier_rejects_a_truncated_artifact(binaries, tmp_path, places_artifact):
    rows, output, _ = places_artifact
    truncated = tmp_path / "truncated.plrx"
    truncated.write_bytes(output.read_bytes()[:-5])
    process = run_verify(
        binaries, truncated, family="places", cell="8080", records=len(rows)
    )
    assert process.returncode != 0


# --------------------------------------------------------------------------- #
# oracle structure checks
# --------------------------------------------------------------------------- #
def test_oracle_index_hash_is_the_domain_separated_sha256():
    import hashlib

    key = b"8080123"
    digest = hashlib.sha256(b"overture-reverse-index-v1\x00" + key).digest()
    assert REVERSE.serving_index_hash(key) == int.from_bytes(digest[:8], "big")


def test_oracle_rejects_foreign_bytes():
    with pytest.raises(ValueError, match="not a reverse serving shard"):
        REVERSE.ReverseShard(b"PLRV0002" + b"\0" * 40)


def test_oracle_rejects_a_tampered_index_hash(places_artifact):
    _, output, _ = places_artifact
    data = bytearray(output.read_bytes())
    (index_offset,) = struct.unpack_from("<Q", data, 16)
    data[index_offset + 4] ^= 0x01  # first stored index hash byte
    with pytest.raises(ValueError, match="hash"):
        REVERSE.ReverseShard(bytes(data))
