#!/usr/bin/env python3
"""Encode and verify reverse `.plrx` shards from a completed slice work tree.

Reverse R1-c: the real-data rung between the synthetic R1-b roundtrip tests and
any scale probe. It consumes a work tree left by
`scripts/run_slice_construction_v1.py` -- no credentials, no re-map -- and
drives the per-record artifacts (Places positions packs / Address records
packs) through `reverse-encode-v1` and `reverse-verify-v1`, one shard per
populated level-8 cell, then cross-checks every shard against the Python
oracle (`reverse_shard_v1.ReverseShard`).

Everything a shard is sized by comes from the pack DIRECTORIES, never from the
data: the per-cell record count feeds `sub_cell_level`, and the encoded stream
must then reproduce that count exactly (the encoder, the verifier and the
oracle each re-assert it independently). The slice-completeness invariant is
the R1 form of the finalizer invariant:

    sum(.plrx records over cells) == per-record artifact records
                                  == transform admitted rows

Fails closed on: a missing marker or per-record artifact, a pack without its
directory, an object whose bytes differ from the marker identity, a cell whose
data disagrees with its directory count, an encoder/verifier failure, and any
oracle disagreement.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location(
    "reverse_r1_slice_reverse_shard", ROOT / "scripts/reverse_shard_v1.py"
)
assert _SPEC and _SPEC.loader
REVERSE = importlib.util.module_from_spec(_SPEC)
# Registered before exec: dataclass resolution looks the module up by name.
sys.modules[_SPEC.name] = REVERSE
_SPEC.loader.exec_module(REVERSE)

SUMMARY_SCHEMA = "overture-reverse-r1-slice-summary-v1"

# The marker key holding the per-record artifact, its pack directory schema,
# and the transform key holding the admitted-row count, per serving family.
ARTIFACT_KEY = {"places": "positions", "addresses": "address_records"}
ARTIFACT_SCHEMA = {
    "places": "overture-places-map-positions-v1",
    "addresses": "overture-address-map-address-records-v1",
}
DIRECTORY_SCHEMA = {
    "places": "overture-places-map-positions-directory-v1",
    "addresses": "overture-address-map-address-records-directory-v1",
}
ADMITTED_KEY = {"places": "admitted_features", "addresses": "admitted_rows"}

# MEASURED mean payload bytes per record from the current encoder
# (tests/test_reverse_shard_v1.py::test_mean_record_size_matches_the_size_model):
# Places retain the design model plus the 16-byte source locator. Address
# records average 84 bytes after repeated strings moved into shard dictionaries.
MODEL_RECORD_BYTES = {"places": 112, "addresses": 84}

# Column projections mirroring the encoder's expected Arrow IPC input
# (crates/geocoder-construction/src/bin/reverse_encode_v1.rs,
# encode_places_batch / encode_addresses_batch). The pack parquet carries
# exactly these columns for its family.
PLACES_COLUMNS = (
    "feature_id",
    "partition_cell",
    "longitude",
    "latitude",
    "primary_name",
    "brand_name",
    "category",
    "locality",
    "region",
    "country",
    "confidence_rank",
    "source_object_index",
    "source_row_group",
    "source_row_index",
)
ADDRESS_COLUMNS = (
    "feature_id",
    "partition_cell",
    "longitude_e7",
    "latitude_e7",
    "display_country",
    "postal_city",
    "postcode",
    "street",
    "number",
    "unit",
    "address_levels",
    "source_object_index",
    "source_row_group",
    "source_row_index",
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


def fail(reason: str) -> None:
    raise SystemExit(f"reverse-r1-slice: {reason}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def input_schema(family: str):
    import pyarrow as pa

    if family == "places":
        return pa.schema(
            [
                ("feature_id", pa.binary()),
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
    return pa.schema(
        [
            ("feature_id", pa.binary()),
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


def object_roots(work: Path, family: str) -> list[Path]:
    """Where a completed work tree can hold the immutable map objects.

    `store-map` is the map phase's local store (staged runs), `store` the
    legacy shared store (--no-staging), and the staging mirror keeps a copy
    under its run-scoped prefix either way.
    """
    roots = [work / "store-map", work / "store"]
    roots += sorted(
        work.glob(f"staging/staging/global-v2/*/construction-v1/{family}")
    )
    return [root for root in roots if root.is_dir()]


def resolve_object(roots: list[Path], identity: dict[str, Any]) -> Path:
    key = identity["key"]
    for root in roots:
        path = root / key
        if not path.is_file():
            continue
        if (
            path.stat().st_size != identity["bytes"]
            or sha256_file(path) != identity["sha256"]
        ):
            fail(f"immutable object differs from its marker identity: {key}")
        return path
    fail(f"immutable object is missing from the work tree: {key}")
    raise AssertionError("unreachable")


def load_packs(work: Path, family: str) -> tuple[list[dict[str, Any]], int]:
    """Every per-record pack in the work tree's markers, plus admitted rows.

    Returns pack entries augmented with a resolved local `path`, and the sum of
    the transform-admitted rows across markers. Fails closed on a marker
    without the artifact, a pack without its embedded directory, or a pack
    object that is missing or changed.
    """
    markers_dir = work / "markers"
    marker_paths = sorted(markers_dir.glob("*.json")) if markers_dir.is_dir() else []
    if not marker_paths:
        fail(f"no map markers under {markers_dir}")
    roots = object_roots(work, family)
    if not roots:
        fail(f"no object store directories under {work}")
    packs: list[dict[str, Any]] = []
    admitted = 0
    for marker_path in marker_paths:
        marker = json.loads(marker_path.read_text())
        artifact = marker.get(ARTIFACT_KEY[family])
        if (
            not isinstance(artifact, dict)
            or artifact.get("schema") != ARTIFACT_SCHEMA[family]
        ):
            fail(
                f"marker {marker_path.name} carries no "
                f"{ARTIFACT_SCHEMA[family]} artifact (pre-artifact marker?)"
            )
        transform = marker.get("transform", {})
        if ADMITTED_KEY[family] not in transform:
            fail(f"marker {marker_path.name} records no admitted-row count")
        admitted += int(transform[ADMITTED_KEY[family]])
        marker_records = 0
        for pack in artifact.get("packs") or ():
            directory = pack.get("directory")
            if (
                not isinstance(directory, dict)
                or directory.get("schema") != DIRECTORY_SCHEMA[family]
            ):
                fail(
                    f"pack {pack.get('object', {}).get('key')!r} in marker "
                    f"{marker_path.name} carries no per-cell directory"
                )
            cells = directory.get("cells")
            if not cells:
                fail(f"a pack directory in {marker_path.name} names no cells")
            if sum(cell["records"] for cell in cells) != directory["records"]:
                fail(
                    f"a pack directory in {marker_path.name} does not "
                    "reconstruct its own record count"
                )
            marker_records += directory["records"]
            packs.append({**pack, "path": resolve_object(roots, pack["object"])})
        if marker_records != artifact.get("records"):
            fail(
                f"marker {marker_path.name} pack directories sum to "
                f"{marker_records} records but the artifact declares "
                f"{artifact.get('records')}"
            )
    return packs, admitted


def cell_counts(packs: list[dict[str, Any]]) -> dict[str, int]:
    """Per-cell record counts, from the directories alone -- never the data."""
    counts: dict[str, int] = {}
    for pack in packs:
        for cell in pack["directory"]["cells"]:
            REVERSE.cell_yx(cell["partition_cell"])
            counts[cell["partition_cell"]] = (
                counts.get(cell["partition_cell"], 0) + cell["records"]
            )
    return counts


def cell_query(family: str, cell: str, level: int, paths: list[str]) -> str:
    """The one SELECT feeding the encoder: this cell's rows, row-major order.

    Ordering note: sorting by the `leaf_sql` digit string directly would be
    Z-order (the digits interleave y and x bits), but the encoder requires
    ROW-MAJOR (sub_y, sub_x) order -- so the y-bit and x-bit projections of the
    digits are sorted separately, keeping the arithmetic mirrored to
    `leaf_sql` rather than re-derived.

    Places E7 for the leaf digits is `CAST(longitude * 10000000 AS BIGINT)`:
    DuckDB's DOUBLE->BIGINT cast rounds ties to even, matching the encoder's
    `round_ties_even` on the identical f64 product.
    """
    if family == "places":
        lon = "CAST(longitude * 10000000 AS BIGINT)"
        lat = "CAST(latitude * 10000000 AS BIGINT)"
        columns = PLACES_COLUMNS
    else:
        lon, lat = "longitude_e7", "latitude_e7"
        columns = ADDRESS_COLUMNS
    leaf = REVERSE.leaf_sql(level, longitude_e7=lon, latitude_e7=lat)
    sources = ", ".join(f"'{path}'" for path in paths)
    return (
        f"SELECT {', '.join(columns)} FROM read_parquet([{sources}]) "
        f"WHERE partition_cell = '{cell}' ORDER BY "
        f"translate({leaf}, '0123', '0011'), translate({leaf}, '0123', '0101'), "
        "feature_id, source_object_index, source_row_group, source_row_index"
    )


def expected_records(table: Any, family: str) -> list[dict[str, Any]]:
    """The oracle's expectation: the input rows in the decoded record shape."""
    expected = []
    for row in table.to_pylist():
        record = {
            "feature_id": row["feature_id"],
            "source_object_index": row["source_object_index"],
            "source_row_group": row["source_row_group"],
            "source_row_index": row["source_row_index"],
        }
        if family == "places":
            # Python round() is ties-even on floats, matching round_ties_even.
            record["longitude_e7"] = round(row["longitude"] * 1e7)
            record["latitude_e7"] = round(row["latitude"] * 1e7)
            record["confidence_rank"] = row["confidence_rank"]
            fields = PLACES_TEXT_FIELDS
        else:
            record["longitude_e7"] = row["longitude_e7"]
            record["latitude_e7"] = row["latitude_e7"]
            fields = ADDRESS_TEXT_FIELDS
        for field in fields:
            record[field] = row[field]
        if family == "addresses":
            record["address_levels"] = row["address_levels"]
        expected.append(record)
    return expected


def run_tool(argv: list[str], what: str) -> None:
    result = subprocess.run([str(a) for a in argv], capture_output=True, text=True)
    if result.returncode:
        fail(f"{what} failed:\n{result.stdout[-2000:]}\n{result.stderr[-4000:]}")


def encode_cell(
    *,
    family: str,
    cell: str,
    count: int,
    packs: list[dict[str, Any]],
    output: Path,
    encode_binary: Path,
    verify_binary: Path,
    connection: Any,
) -> dict[str, Any]:
    import pyarrow.ipc as ipc

    depth_family = REVERSE.DEPTH_FAMILY_BY_SERVING[family]
    level = REVERSE.sub_cell_level(count, cell, depth_family)
    paths = sorted(
        str(pack["path"])
        for pack in packs
        if any(c["partition_cell"] == cell for c in pack["directory"]["cells"])
    )
    table = connection.execute(
        cell_query(family, cell, level, paths)
    ).to_arrow_table()
    if table.num_rows != count:
        fail(
            f"cell {cell} carries {table.num_rows} rows but its directories "
            f"promise {count}"
        )
    table = table.cast(input_schema(family))
    source = output / f"{family}-{cell}.arrow"
    with ipc.new_stream(source, table.schema) as writer:
        for batch in table.to_batches(max_chunksize=65_536):
            writer.write_batch(batch)
    shard = output / f"{family}-{cell}.plrx"
    sidecar = output / f"{family}-{cell}.digest.json"
    run_tool(
        [encode_binary, "--input", source, "--output", shard, "--family", family,
         "--cell", cell, "--records", count, "--digest-out", sidecar],
        f"reverse-encode-v1 for cell {cell}",
    )
    run_tool(
        [verify_binary, "--input", shard, "--family", family, "--cell", cell,
         "--records", count, "--digest", sidecar],
        f"reverse-verify-v1 for cell {cell}",
    )
    # Oracle cross-check: the third implementation of the wire format decodes
    # the shard and must reproduce the input rows exactly -- count, E7
    # coordinates, cell, and the header depth against the Python-computed L.
    decoded_shard = REVERSE.ReverseShard(shard.read_bytes())
    if decoded_shard.sub_cell_level != level:
        fail(
            f"cell {cell} header sub_cell_level {decoded_shard.sub_cell_level} "
            f"differs from the Python-computed {level}"
        )
    if (
        decoded_shard.records != count
        or decoded_shard.family != family
        or decoded_shard.cell != cell
    ):
        fail(f"cell {cell} oracle header disagreement")
    decoded = [
        record
        for leaf in decoded_shard.leaf_ranges()
        for record in decoded_shard.decode_leaf(leaf.key)
    ]
    if decoded != expected_records(table, family):
        fail(f"cell {cell} oracle decode differs from the encoder input")
    return {
        "records": count,
        "sub_cell_level": level,
        "leaves": len(decoded_shard.leaf_ranges()),
        "shard_bytes": shard.stat().st_size,
        "payload_bytes": decoded_shard.index_offset - REVERSE.SERVING_HEADER_BYTES,
    }


def main(argv: list[str] | None = None) -> int:
    import duckdb

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True,
                        help="Completed run_slice_construction_v1.py work tree")
    parser.add_argument("--family", choices=("places", "addresses"),
                        default="places")
    parser.add_argument("--output", type=Path, default=None,
                        help="Shard/summary output directory "
                             "(default: WORK/reverse-r1)")
    parser.add_argument("--encode-binary", type=Path,
                        default=ROOT / "crates/target/release/reverse-encode-v1")
    parser.add_argument("--verify-binary", type=Path,
                        default=ROOT / "crates/target/release/reverse-verify-v1")
    args = parser.parse_args(argv)
    family = args.family
    output = args.output or args.work / "reverse-r1"
    output.mkdir(parents=True, exist_ok=True)
    for binary in (args.encode_binary, args.verify_binary):
        if not binary.is_file():
            fail(f"missing binary {binary}; build geocoder-construction bins")

    packs, admitted = load_packs(args.work, family)
    counts = cell_counts(packs)
    artifact_records = sum(counts.values())
    connection = duckdb.connect()
    per_cell: dict[str, dict[str, Any]] = {}
    for cell in sorted(counts):
        per_cell[cell] = encode_cell(
            family=family,
            cell=cell,
            count=counts[cell],
            packs=packs,
            output=output,
            encode_binary=args.encode_binary,
            verify_binary=args.verify_binary,
            connection=connection,
        )
        print(
            f"  {cell}: {counts[cell]:,} records, L={per_cell[cell]['sub_cell_level']}, "
            f"{per_cell[cell]['leaves']} leaves, "
            f"{per_cell[cell]['shard_bytes']:,} bytes",
            flush=True,
        )

    # The R1 form of the finalizer invariant. `artifact_records` is the
    # directory-declared total, and every per-cell shard was record-count
    # checked three independent ways above, so equality here means no admitted
    # record can be missing from, or duplicated across, the reverse shards.
    records = sum(entry["records"] for entry in per_cell.values())
    if not (records == artifact_records == admitted):
        fail(
            f"slice completeness violated: shards carry {records} records, the "
            f"per-record artifact declares {artifact_records}, the transform "
            f"admitted {admitted}"
        )

    payload_bytes = sum(entry["payload_bytes"] for entry in per_cell.values())
    summary = {
        "schema": SUMMARY_SCHEMA,
        "family": family,
        "cells": sorted(per_cell),
        "shards": len(per_cell),
        "records": records,
        "artifact_records": artifact_records,
        "admitted_rows": admitted,
        "bytes": sum(entry["shard_bytes"] for entry in per_cell.values()),
        "payload_bytes": payload_bytes,
        "mean_record_bytes": round(payload_bytes / records, 3) if records else None,
        "model_record_bytes": MODEL_RECORD_BYTES[family],
        "per_cell": per_cell,
    }
    (output / "summary.json").write_text(json.dumps(summary, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
