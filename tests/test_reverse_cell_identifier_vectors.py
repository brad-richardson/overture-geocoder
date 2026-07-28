"""Cross-implementation gate for the cell identifier and the reverse leaf key.

The reverse design (docs/plans/2026-07-25-reverse-v2-design.md, increment 1b)
names the gap this closes: three identifiers share one grid -- `route()` in
places_transform_v1.rs, `point_quadkey` in places_pages.rs, and
`cell_partition_key` in both construction planes -- and no cross-implementation
test tied them together. Every claim here runs against ONE committed vector
file, which the Rust `world_quadkey_matches_the_python_partition_contract` test
also loads, so the mirrors cannot drift from each other or from the binary.
"""

from __future__ import annotations

import importlib.util
import json
import math
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


pa = pytest.importorskip("pyarrow")
duckdb = pytest.importorskip("duckdb")

ROOT = Path(__file__).parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


GENERATOR = _load(
    "reverse_cell_vectors_generator", "tests/generate_reverse_cell_identifier_vectors.py"
)
PLACES_TEST = _load(
    "reverse_cell_places_helpers", "tests/test_places_construction_v1.py"
)
ADDRESS = _load("reverse_cell_address", "scripts/address_construction_v1.py")
PLACES = _load("reverse_cell_places", "scripts/places_construction_v1.py")
PARTITION = _load("reverse_cell_partition", "scripts/places_partition.py")
REVERSE = _load("reverse_cell_reverse", "scripts/reverse_shard_v1.py")

FIXTURE = ROOT / "tests/fixtures/reverse/cell-identifier-vectors-v1.json"
PAYLOAD = json.loads(FIXTURE.read_text())
VECTORS = PAYLOAD["vectors"]


@pytest.fixture(scope="module")
def places_transform_binary() -> Path:
    subprocess.run(
        ["cargo", "build", "-p", "geocoder-construction", "--bin", "places-transform-v1"],
        cwd=ROOT / "crates",
        check=True,
    )
    return ROOT / "crates/target/debug/places-transform-v1"


def test_committed_vectors_are_exactly_the_generator_output():
    """The fixture regenerates byte-identically; a hand edit cannot survive."""
    assert PAYLOAD == GENERATOR.build_payload()
    assert PAYLOAD["schema"] == GENERATOR.SCHEMA
    assert PAYLOAD["cell_level"] == REVERSE.CELL_LEVEL


def test_vectors_cover_the_documented_seams():
    """Corners, both polar rows, the antimeridian, and a broad cell spread.

    A vector file that silently lost its seam cases would leave every downstream
    assertion vacuous exactly where the grid is hardest, so coverage is pinned.
    """
    points = {(v["longitude_e7"], v["latitude_e7"]) for v in VECTORS}
    assert len(VECTORS) == len(points) >= 300
    for longitude in (-1_800_000_000, 1_800_000_000):
        for latitude in (-900_000_000, 900_000_000):
            assert (longitude, latitude) in points
    assert any(longitude == -1_799_999_999 for longitude, _ in points)
    rows = {int(v["partition_cell"][:2], 16) for v in VECTORS}
    assert {0, 255} <= rows
    assert len({v["partition_cell"] for v in VECTORS}) >= 200


def test_python_mirrors_agree_on_every_vector():
    for vector in VECTORS:
        longitude_e7 = vector["longitude_e7"]
        latitude_e7 = vector["latitude_e7"]
        cell = vector["partition_cell"]
        key = vector["partition_key"]
        assert ADDRESS.route_e7(longitude_e7, latitude_e7) == (key, cell)
        assert ADDRESS.cell_partition_key(cell) == key
        assert PLACES.cell_partition_key(cell) == key
        quadkey8 = PARTITION.point_quadkey(
            longitude_e7 / 1e7, latitude_e7 / 1e7, REVERSE.CELL_LEVEL
        )
        assert quadkey8 == vector["quadkey8"]
        # The 8-char base-4 quadkey re-encodes the 4-hex cell and the (y<<8)|x
        # key: same grid, three writings.
        x = y = 0
        for digit in quadkey8:
            value = ord(digit) - ord("0")
            x = (x << 1) | (value & 1)
            y = (y << 1) | ((value >> 1) & 1)
        assert f"{y:02x}{x:02x}" == cell
        assert (y << 8) | x == key


def test_leaf_keys_extend_the_cell_and_refine():
    for vector in VECTORS:
        cell = vector["partition_cell"]
        leaf_keys = vector["leaf_keys"]
        assert len(leaf_keys) == REVERSE.MAX_SUB_CELL_LEVEL + 1
        assert leaf_keys[0] == cell
        for level, leaf in enumerate(leaf_keys):
            digits = REVERSE.leaf_digits_e7(
                vector["longitude_e7"], vector["latitude_e7"], cell, level
            )
            assert leaf == REVERSE.leaf_key(cell, digits) == cell + digits
            assert len(leaf) == 4 + level
            # Deeper keys refine shallower ones: floor division nests, so the
            # level-L digits are a prefix of the level-(L+1) digits.
            if level:
                assert leaf.startswith(leaf_keys[level - 1])


def test_leaf_digits_clamp_into_the_authoritative_cell():
    """A point outside the cell keys to the nearest edge leaf, never escapes.

    This is the ~5 mm f64/E7 cell-assignment seam the design accepts: the leaf
    key must stay consistent with whichever cell owns the record.
    """
    cell = "8080"
    for level in (1, 3, 7):
        south_west = REVERSE.leaf_digits_e7(-40_000_000, -40_000_000, cell, level)
        north_east = REVERSE.leaf_digits_e7(1_800_000_000, 900_000_000, cell, level)
        assert south_west == "0" * level
        assert north_east == "3" * level


def test_leaf_sql_matches_the_python_mirror_on_every_vector():
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE points "
        "(longitude_e7 INTEGER, latitude_e7 INTEGER, partition_cell VARCHAR)"
    )
    connection.executemany(
        "INSERT INTO points VALUES (?, ?, ?)",
        [
            (v["longitude_e7"], v["latitude_e7"], v["partition_cell"])
            for v in VECTORS
        ],
    )
    for level in range(REVERSE.MAX_SUB_CELL_LEVEL + 1):
        observed = connection.execute(
            f"SELECT longitude_e7, latitude_e7, partition_cell, {REVERSE.leaf_sql(level)} "
            "FROM points"
        ).fetchall()
        assert len(observed) == len(VECTORS)
        for longitude_e7, latitude_e7, cell, digits in observed:
            assert digits == REVERSE.leaf_digits_e7(
                longitude_e7, latitude_e7, cell, level
            ), f"leaf_sql mismatch at E7 ({longitude_e7}, {latitude_e7}) L={level}"


def test_the_authoritative_rust_route_agrees_on_every_vector(
    tmp_path, places_transform_binary
):
    """Run through the REAL binary rather than a re-derived formula: a second
    copy of the formula in the test would only prove the test agrees with
    itself."""
    places_rows = [
        {
            "id": str(uuid.UUID(int=index + 1)),
            "primary_name": f"Feature {index}",
            "country": "US",
            "point": [vector["longitude_e7"] / 1e7, vector["latitude_e7"] / 1e7],
            "source_object_index": 0,
            "source_row_group": 0,
            "source_row_index": index,
        }
        for index, vector in enumerate(VECTORS)
    ]
    _, table = PLACES_TEST.run_transform(
        tmp_path, places_transform_binary, places_rows, use_limits=False
    )
    observed: dict[str, tuple[str, int]] = {}
    for feature, cell, key in zip(
        table.column("feature_id").to_pylist(),
        table.column("partition_cell").to_pylist(),
        table.column("partition_key").to_pylist(),
        strict=True,
    ):
        observed[bytes(feature).hex()] = (cell, key)
    assert len(observed) == len(VECTORS)
    for index, vector in enumerate(VECTORS):
        identity = uuid.UUID(int=index + 1).bytes.hex()
        assert observed[identity] == (
            vector["partition_cell"],
            vector["partition_key"],
        ), f"route mismatch at E7 ({vector['longitude_e7']}, {vector['latitude_e7']})"


# --------------------------------------------------------------------------- #
# the depth rule
# --------------------------------------------------------------------------- #
def l_lon_from_design_formula(row: int, max_radius_m: int) -> int:
    """The design's formula, re-derived here so the pinned literals cannot be
    edited without this test noticing. `phi_edge` is the poleward edge of the
    row; values below -1 are indistinguishable after the clamp to [0, l_lat]."""
    phi_edge = (row + 1) * 180.0 / 256.0
    scale = 1.40625 * 111195.0 * math.cos(math.radians(phi_edge))
    return max(-1, math.floor(math.log2(scale / max_radius_m)))


def test_l_lon_tables_reproduce_the_design_formula():
    assert set(REVERSE.L_LON_BY_ROW) == set(REVERSE.FAMILIES)
    for family, spec in REVERSE.FAMILIES.items():
        table = REVERSE.L_LON_BY_ROW[family]
        assert len(table) == 128
        assert list(table) == [
            l_lon_from_design_formula(row, spec.max_radius_m) for row in range(128)
        ]
        # Monotone: depth can only shrink toward the pole.
        assert all(a >= b for a, b in zip(table, table[1:]))


def test_l_lon_thresholds_match_the_design_table():
    """The design's poi ceiling table: L=5 up to 65.84 degrees, 4 to 78.19,
    3 to 84.13, 2 to 87.07, 1 to 88.53, 0 to 89.27. Identical degrees for
    address at l_lat=7 (same radius ratio)."""
    thresholds = [(5, 65.84), (4, 78.19), (3, 84.13), (2, 87.07), (1, 88.53), (0, 89.27)]
    for family, offset in (("poi", 0), ("address", 2)):
        table = REVERSE.L_LON_BY_ROW[family]
        for level, degrees in thresholds:
            for row in range(128):
                phi_edge = (row + 1) * 180.0 / 256.0
                assert (table[row] >= level + offset) == (phi_edge <= degrees), (
                    f"{family} L_lon threshold broken at row {row}"
                )


def test_cell_row_index_pairs_rows_by_poleward_edge():
    assert REVERSE.cell_row_index(128) == REVERSE.cell_row_index(127) == 0
    assert REVERSE.cell_row_index(255) == REVERSE.cell_row_index(0) == 127
    for y in range(256):
        assert REVERSE.cell_row_index(y) == REVERSE.cell_row_index(255 - y)
    with pytest.raises(ValueError, match="cell row"):
        REVERSE.cell_row_index(256)


def test_sub_cell_level_takes_the_minimum_of_the_three_ceilings():
    # Density ceiling: 2048 records fit one leaf; one more forces a split.
    assert REVERSE.sub_cell_level(0, "8080", "poi") == 0
    assert REVERSE.sub_cell_level(2048, "8080", "poi") == 0
    assert REVERSE.sub_cell_level(2049, "8080", "poi") == 1
    assert REVERSE.sub_cell_level(2048 * 4**4, "8080", "poi") == 4
    # Family latitude ceiling binds in the dense tail (Tokyo b2e3).
    assert REVERSE.sub_cell_level(1_384_000, "b2e3", "poi") == 5
    assert REVERSE.sub_cell_level(10**9, "8080", "poi") == 5
    assert REVERSE.sub_cell_level(10**9, "8080", "address") == 7
    # Longitude ceiling binds poleward: row 119 caps poi at 2, address at 4.
    assert REVERSE.sub_cell_level(10**9, "f700", "poi") == 2
    assert REVERSE.sub_cell_level(10**9, "f700", "address") == 4
    # Polar rows clamp the negative ceiling to a whole-cell shard.
    assert REVERSE.sub_cell_level(10**9, "ff00", "poi") == 0
    assert REVERSE.sub_cell_level(10**9, "0000", "poi") == 0
    # The two equator-adjacent rows are symmetric.
    assert REVERSE.sub_cell_level(10**9, "7f00", "poi") == REVERSE.sub_cell_level(
        10**9, "8000", "poi"
    )
    with pytest.raises(ValueError, match="family"):
        REVERSE.sub_cell_level(1, "8080", "roads")
    with pytest.raises(ValueError, match="negative"):
        REVERSE.sub_cell_level(-1, "8080", "poi")
    with pytest.raises(ValueError, match="malformed"):
        REVERSE.sub_cell_level(1, "80800", "poi")


def test_leaf_contract_rejects_out_of_range_inputs():
    with pytest.raises(ValueError, match="level"):
        REVERSE.leaf_digits_e7(0, 0, "8080", 8)
    with pytest.raises(ValueError, match="level"):
        REVERSE.leaf_sql(-1)
    with pytest.raises(ValueError, match="malformed"):
        REVERSE.leaf_digits_e7(0, 0, "80G0", 3)
    with pytest.raises(ValueError, match="malformed"):
        REVERSE.leaf_key("8080", "04")
