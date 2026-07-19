#!/usr/bin/env python3
"""Build stable, spatially owned compact Places shards and a routing catalog.

Shard IDs are world quadkeys, not region-local ordinals. The input is planned
into fixed minimum-level cells and only cells above the row cap split further.
Supplying the prior catalog makes those splits sticky, so a later release never
merges a cell and silently changes its ownership again.

For a global build, the extractor writes maximum-level Morton order and this
builder performs two bounded-memory passes over that local Parquet file: a
coordinate/count planning pass followed by the compact-shard build pass. At no
point does it retain the complete global Places collection in Python.

This is an offline producer. It does not access Cloudflare, R2, or Overture S3
and cannot promote a catalog.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import struct
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from experiment_places_compact_index import (  # noqa: E402
    Place,
    load_places,
    place_from_row,
)
from experiment_places_compact_shard import build_artifact  # noqa: E402
from experiment_places_head_repack import (  # noqa: E402
    build_heads_and_baseline,
    build_repack_object,
)
from places_partition import (  # noqa: E402
    DEFAULT_MAXIMUM_LEVEL,
    DEFAULT_MINIMUM_LEVEL,
    PARTITION_SCHEME,
    PartitionCell,
    morton_quadkey,
    plan_partition_cells,
    point_morton,
    quadkey_bbox,
    validate_levels,
    validate_quadkey,
    validate_split_cells,
)
from prepare_places_worker_smoke import TOKENIZER_VERSION  # noqa: E402


CATALOG_MAGIC = b"PCAT0001"
CATALOG_PREAMBLE = struct.Struct("<8sI")
REPORT_SCHEMA = "overture-places-region-build-v2"
DEFAULT_SHARD_ROW_CAP = 1_500_000
DEFAULT_HEAD_MINIMUM_CANDIDATES = 64
DEFAULT_HEAD_FAMOUS_CAP = 1024
WORLD_COVERAGE = [-180.0, -90.0, 180.0, 90.0]


def validate_coverage(values: Iterable[float]) -> list[float]:
    coverage = [float(value) for value in values]
    if len(coverage) != 4 or not all(math.isfinite(value) for value in coverage):
        raise ValueError("coverage_bbox must contain four finite numbers")
    xmin, ymin, xmax, ymax = coverage
    if (
        not -180.0 <= xmin < xmax <= 180.0
        or not -90.0 <= ymin < ymax <= 90.0
    ):
        raise ValueError("coverage_bbox is outside the world bounds")
    return coverage


def _inside_coverage(place: Place, coverage: list[float]) -> bool:
    return (
        coverage[0] <= place.lon <= coverage[2]
        and coverage[1] <= place.lat <= coverage[3]
    )


def _iter_parquet_rows_in_file_order(path: Path) -> Iterator[dict[str, Any]]:
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Parquet input requires duckdb") from exc
    connection = duckdb.connect()
    try:
        connection.execute("SET threads=1")
        connection.execute("SET preserve_insertion_order=true")
        cursor = connection.execute("SELECT * FROM read_parquet(?)", [str(path)])
        columns = [item[0] for item in cursor.description]
        while batch := cursor.fetchmany(10_000):
            for row in batch:
                yield dict(zip(columns, row))
    finally:
        connection.close()


def _iter_parquet_partition_rows(path: Path) -> Iterator[tuple[int | None, float, float]]:
    """Read only the planning columns in physical file order."""
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Parquet input requires duckdb") from exc
    connection = duckdb.connect()
    try:
        connection.execute("SET threads=1")
        connection.execute("SET preserve_insertion_order=true")
        probe = connection.execute("SELECT * FROM read_parquet(?) LIMIT 0", [str(path)])
        columns = {item[0] for item in probe.description}
        if not {"lat", "lon"}.issubset(columns):
            raise ValueError("serving-ordered Parquet is missing lat/lon")
        select = "partition_key, lon, lat" if "partition_key" in columns else "NULL, lon, lat"
        cursor = connection.execute(f"SELECT {select} FROM read_parquet(?)", [str(path)])
        while batch := cursor.fetchmany(50_000):
            for raw_key, longitude, latitude in batch:
                yield (
                    None if raw_key is None else int(raw_key),
                    float(longitude),
                    float(latitude),
                )
    finally:
        connection.close()


def _validate_declared_morton(
    declared: int | None, longitude: float, latitude: float, maximum_level: int
) -> int:
    computed = point_morton(longitude, latitude, maximum_level)
    if declared is not None and declared != computed:
        raise ValueError(
            f"input partition_key {declared} does not match coordinate key {computed}"
        )
    return computed


def iter_parquet_full_counts(
    path: Path, *, maximum_level: int
) -> Iterator[tuple[str, int]]:
    """Yield ordered, run-length encoded maximum-level cell counts."""
    previous: int | None = None
    current: int | None = None
    count = 0
    for declared, longitude, latitude in _iter_parquet_partition_rows(path):
        key = _validate_declared_morton(declared, longitude, latitude, maximum_level)
        if previous is not None and key < previous:
            raise ValueError("input declared as serving-ordered is not Morton-monotonic")
        if current is not None and key != current:
            yield morton_quadkey(current, maximum_level), count
            count = 0
        current = key
        previous = key
        count += 1
    if current is not None:
        yield morton_quadkey(current, maximum_level), count


def _serving_key(place: Place, maximum_level: int) -> tuple[int, int, str]:
    return (
        point_morton(place.lon, place.lat, maximum_level),
        -round(place.confidence * 255),
        place.place_id,
    )


def _iter_streamed_places(
    path: Path,
    *,
    maximum_level: int,
    coverage: list[float],
) -> Iterator[tuple[str, Place]]:
    previous: tuple[int, int, str] | None = None
    for row_number, row in enumerate(_iter_parquet_rows_in_file_order(path), start=1):
        place = place_from_row(row, row_number)
        if not place.name:
            raise ValueError(f"serving-ordered input row {row_number} has no primary name")
        if not _inside_coverage(place, coverage):
            raise ValueError(f"input row {row_number} is outside the declared coverage")
        declared = row.get("partition_key")
        morton = _validate_declared_morton(
            None if declared is None else int(declared),
            place.lon,
            place.lat,
            maximum_level,
        )
        key = (morton, -round(place.confidence * 255), place.place_id)
        if previous is not None and key < previous:
            raise ValueError(
                "input declared as serving-ordered is not monotonic at "
                f"row {row_number}: {key!r} < {previous!r}"
            )
        previous = key
        yield morton_quadkey(morton, maximum_level), place


def _full_counts_from_ordered(
    ordered: Iterable[tuple[str, Place]],
) -> Iterator[tuple[str, int]]:
    current: str | None = None
    count = 0
    for full_cell, _ in ordered:
        if current is not None and full_cell != current:
            yield current, count
            count = 0
        current = full_cell
        count += 1
    if current is not None:
        yield current, count


def _leaf_for(full_cell: str, leaves: set[str], minimum_level: int) -> str:
    for length in range(minimum_level, len(full_cell) + 1):
        candidate = full_cell[:length]
        if candidate in leaves:
            return candidate
    raise RuntimeError(f"partition plan does not own maximum-level cell {full_cell}")


def iter_partition_chunks(
    ordered: Iterable[tuple[str, Place]],
    cells: list[PartitionCell],
    *,
    minimum_level: int,
) -> Iterator[tuple[PartitionCell, list[Place]]]:
    by_cell = {cell.cell: cell for cell in cells}
    expected = set(by_cell)
    completed: set[str] = set()
    current: str | None = None
    chunk: list[Place] = []
    for full_cell, place in ordered:
        leaf = _leaf_for(full_cell, expected, minimum_level)
        if current is not None and leaf != current:
            planned = by_cell[current]
            if len(chunk) != planned.rows:
                raise RuntimeError(f"partition {current} row count changed after planning")
            completed.add(current)
            yield planned, chunk
            chunk = []
            if leaf in completed:
                raise RuntimeError("partition input is not spatially contiguous")
        current = leaf
        chunk.append(place)
    if current is not None:
        planned = by_cell[current]
        if len(chunk) != planned.rows:
            raise RuntimeError(f"partition {current} row count changed after planning")
        completed.add(current)
        yield planned, chunk
    if completed != expected:
        raise RuntimeError("partition build did not produce every planned cell")


def _read_catalog_payload(path: Path) -> dict[str, Any]:
    encoded = path.read_bytes()
    if len(encoded) < CATALOG_PREAMBLE.size:
        raise ValueError("previous Places catalog is truncated")
    magic, length = CATALOG_PREAMBLE.unpack(encoded[: CATALOG_PREAMBLE.size])
    if magic != CATALOG_MAGIC or len(encoded) != CATALOG_PREAMBLE.size + length:
        raise ValueError("previous Places catalog has invalid framing")
    payload = json.loads(encoded[CATALOG_PREAMBLE.size :])
    if not isinstance(payload, dict):
        raise ValueError("previous Places catalog payload must be an object")
    return payload


def previous_split_cells(
    path: Path | None,
    *,
    minimum_level: int,
    maximum_level: int,
    coverage: list[float],
) -> list[str]:
    if path is None:
        return []
    payload = _read_catalog_payload(path)
    partition = payload.get("partition")
    previous_maximum_level = (
        partition.get("maximum_level") if isinstance(partition, dict) else None
    )
    if (
        payload.get("schema_version") != 2
        or payload.get("coverage") != coverage
        or not isinstance(partition, dict)
        or partition.get("scheme") != PARTITION_SCHEME
        or partition.get("minimum_level") != minimum_level
        or not isinstance(previous_maximum_level, int)
        or not minimum_level <= previous_maximum_level <= maximum_level
        or not isinstance(partition.get("split_row_cap"), int)
        or partition["split_row_cap"] < 1
        or not isinstance(partition.get("split_cells"), list)
    ):
        raise ValueError("previous Places catalog has an incompatible partition contract")
    splits = validate_split_cells(
        partition["split_cells"],
        minimum_level=minimum_level,
        maximum_level=previous_maximum_level,
    )
    _validate_previous_shards(
        payload.get("shards"),
        split_cells=splits,
        minimum_level=minimum_level,
        maximum_level=previous_maximum_level,
    )
    return sorted(splits)


def _validate_previous_shards(
    raw_shards: Any,
    *,
    split_cells: set[str],
    minimum_level: int,
    maximum_level: int,
) -> None:
    if not isinstance(raw_shards, list) or not raw_shards:
        raise ValueError("previous Places catalog must contain routed shards")
    cells: list[str] = []
    ids: set[str] = set()
    objects: set[str] = set()
    for shard in raw_shards:
        if not isinstance(shard, dict) or not isinstance(shard.get("cell"), str):
            raise ValueError("previous Places catalog has an invalid shard")
        cell = shard["cell"]
        validate_quadkey(cell, minimum=minimum_level, maximum=maximum_level)
        expected_id = f"q-{cell}"
        expected_object = f"{expected_id}.pcsh"
        bbox = quadkey_bbox(cell)
        center = [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2]
        if (
            shard.get("id") != expected_id
            or shard.get("object") != expected_object
            or shard.get("bbox") != bbox
            or shard.get("center") != center
            or cell in split_cells
            or (len(cell) > minimum_level and cell[:-1] not in split_cells)
            or expected_id in ids
            or expected_object in objects
        ):
            raise ValueError(
                "previous Places catalog shard is inconsistent with split history"
            )
        cells.append(cell)
        ids.add(expected_id)
        objects.add(expected_object)
    cells.sort()
    if any(right.startswith(left) for left, right in zip(cells, cells[1:])):
        raise ValueError("previous Places catalog has overlapping leaf ownership")


def _route(cell: str) -> dict[str, Any]:
    bbox = quadkey_bbox(cell)
    identifier = f"q-{cell}"
    return {
        "id": identifier,
        "object": f"{identifier}.pcsh",
        "cell": cell,
        "bbox": bbox,
        "center": [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2],
    }


def build_catalog(
    routes: list[dict[str, Any]],
    output: Path,
    *,
    coverage: list[float],
    minimum_level: int,
    maximum_level: int,
    row_cap: int,
    split_cells: list[str],
) -> dict[str, Any]:
    payload = {
        "schema_version": 2,
        "tokenizer_version": TOKENIZER_VERSION,
        "coverage": coverage,
        "partition": {
            "scheme": PARTITION_SCHEME,
            "minimum_level": minimum_level,
            "maximum_level": maximum_level,
            "split_row_cap": row_cap,
            "split_cells": split_cells,
        },
        "shards": routes,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    output.write_bytes(CATALOG_PREAMBLE.pack(CATALOG_MAGIC, len(encoded)) + encoded)
    return {
        "schema_version": 2,
        "object": output.name,
        "bytes": output.stat().st_size,
        "coverage": coverage,
        "partition": payload["partition"],
        "shards": routes,
    }


def build_region_head(
    combined: list[Place],
    output_dir: Path,
    *,
    head_minimum_candidates: int,
    head_famous_cap: int,
) -> dict[str, Any]:
    ordered, heads, _ = build_heads_and_baseline(
        combined,
        head_minimum_candidates=head_minimum_candidates,
        head_famous_cap=head_famous_cap,
        preserve_input_order=True,
    )
    head_path = output_dir / "head.phrp"
    try:
        report = build_repack_object(
            ordered, heads, head_path, head_famous_cap=head_famous_cap
        )
    except ValueError as exc:
        head_path.unlink(missing_ok=True)
        return {
            "status": "over_reader_caps",
            "object": None,
            "head_key_candidates": len(heads),
            "detail": str(exc),
        }
    return {
        "status": "built",
        "object": head_path.name,
        "object_bytes": report["object_bytes"],
        "key_count": report["key_count"],
        "key_index_bytes": report["key_index_bytes"],
        "entries_bytes": report["entries_bytes"],
        "build_seconds": report["build_seconds"],
    }


def build_region(
    input_path: Path,
    output_dir: Path,
    *,
    region_name: str,
    coverage_bbox: Iterable[float],
    row_cap: int = DEFAULT_SHARD_ROW_CAP,
    minimum_level: int = DEFAULT_MINIMUM_LEVEL,
    maximum_level: int = DEFAULT_MAXIMUM_LEVEL,
    previous_catalog: Path | None = None,
    head_minimum_candidates: int = DEFAULT_HEAD_MINIMUM_CANDIDATES,
    head_famous_cap: int = DEFAULT_HEAD_FAMOUS_CAP,
    build_head: bool = True,
    input_serving_ordered: bool = False,
) -> dict[str, Any]:
    if not region_name or len(region_name) > 48 or any(
        not (character.isascii() and (character.isalnum() or character == "-"))
        for character in region_name
    ):
        raise ValueError("region_name must be <=48 ASCII alphanumeric/hyphen chars")
    validate_levels(minimum_level, maximum_level)
    coverage = validate_coverage(coverage_bbox)
    sticky_splits = previous_split_cells(
        previous_catalog,
        minimum_level=minimum_level,
        maximum_level=maximum_level,
        coverage=coverage,
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    if input_serving_ordered:
        if build_head:
            raise ValueError(
                "serving-ordered streaming requires build_head=False so the "
                "full region is not retained in memory"
            )
        full_counts: Iterable[tuple[str, int]] = iter_parquet_full_counts(
            input_path, maximum_level=maximum_level
        )
        ordered_factory = lambda: _iter_streamed_places(  # noqa: E731
            input_path, maximum_level=maximum_level, coverage=coverage
        )
        combined: list[Place] | None = None
    else:
        places = load_places(input_path)
        for index, place in enumerate(places, start=1):
            if not _inside_coverage(place, coverage):
                raise ValueError(f"input row {index} is outside the declared coverage")
        ordered_items = sorted(
            (
                (morton_quadkey(point_morton(place.lon, place.lat, maximum_level), maximum_level), place)
                for place in places
            ),
            key=lambda item: _serving_key(item[1], maximum_level),
        )
        full_counts = _full_counts_from_ordered(ordered_items)
        ordered_factory = lambda: iter(ordered_items)  # noqa: E731
        combined = [] if build_head else None

    cells, split_cells = plan_partition_cells(
        full_counts,
        minimum_level=minimum_level,
        maximum_level=maximum_level,
        row_cap=row_cap,
        sticky_splits=sticky_splits,
    )
    loaded_places = sum(cell.rows for cell in cells)
    chunks = iter_partition_chunks(
        ordered_factory(), cells, minimum_level=minimum_level
    )

    shard_reports: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    for index, (cell, chunk) in enumerate(chunks):
        print(
            f"building shard {index + 1}/{len(cells)} cell={cell.cell} rows={len(chunk)}",
            file=sys.stderr,
            flush=True,
        )
        route = _route(cell.cell)
        artifact_path = output_dir / route["object"]
        ordered, report = build_artifact(chunk, artifact_path, preserve_input_order=True)
        routes.append(route)
        if combined is not None:
            combined.extend(ordered)
        shard_reports.append(
            {
                "id": route["id"],
                "object": route["object"],
                "cell": cell.cell,
                "rows": report["places"],
                "artifact_bytes": report["artifact_bytes"],
                "bytes_per_place": report["bytes_per_place"],
                "tokens": report["tokens"],
                "bbox": route["bbox"],
                "center": route["center"],
            }
        )
        del ordered, chunk
        gc.collect()

    catalog_report = build_catalog(
        routes,
        output_dir / "catalog.pcat",
        coverage=coverage,
        minimum_level=minimum_level,
        maximum_level=maximum_level,
        row_cap=row_cap,
        split_cells=split_cells,
    )
    if build_head:
        if combined is None:
            raise RuntimeError("head build requested without a combined place list")
        head_info = build_region_head(
            combined,
            output_dir,
            head_minimum_candidates=head_minimum_candidates,
            head_famous_cap=head_famous_cap,
        )
    else:
        head_info = {
            "status": "skipped",
            "object": None,
            "reason": (
                "bounded-memory build skips the context-free global head; "
                "general forward search will use its separately built global tier"
            ),
        }

    produced_objects = [shard["object"] for shard in shard_reports]
    produced_objects.append("catalog.pcat")
    if head_info.get("object"):
        produced_objects.append(head_info["object"])
    total_rows = sum(shard["rows"] for shard in shard_reports)
    total_shard_bytes = sum(shard["artifact_bytes"] for shard in shard_reports)
    return {
        "schema": REPORT_SCHEMA,
        "region_name": region_name,
        "tokenizer_version": TOKENIZER_VERSION,
        "input": str(input_path),
        "config": {
            "coverage_bbox": coverage,
            "partition_scheme": PARTITION_SCHEME,
            "minimum_level": minimum_level,
            "maximum_level": maximum_level,
            "shard_row_cap": row_cap,
            "previous_catalog": None if previous_catalog is None else str(previous_catalog),
            "head_minimum_candidates": head_minimum_candidates,
            "head_famous_cap": head_famous_cap,
        },
        "totals": {
            "loaded_places": loaded_places,
            "shard_rows": total_rows,
            "shards": len(shard_reports),
            "split_cells": len(split_cells),
            "shard_bytes": total_shard_bytes,
            "bytes_per_place": total_shard_bytes / total_rows if total_rows else 0.0,
        },
        "shards": shard_reports,
        "catalog": catalog_report,
        "head": head_info,
        "produced_objects": sorted(produced_objects),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--region-name", required=True)
    parser.add_argument(
        "--coverage-bbox",
        type=float,
        nargs=4,
        required=True,
        metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
        help="Exact coverage of the extracted input; use -180 -90 180 90 globally.",
    )
    parser.add_argument("--shard-row-cap", type=int, default=DEFAULT_SHARD_ROW_CAP)
    parser.add_argument("--minimum-level", type=int, default=DEFAULT_MINIMUM_LEVEL)
    parser.add_argument("--maximum-level", type=int, default=DEFAULT_MAXIMUM_LEVEL)
    parser.add_argument(
        "--previous-catalog",
        type=Path,
        help="Prior v2 catalog whose split cells must remain split.",
    )
    parser.add_argument(
        "--head-minimum-candidates", type=int, default=DEFAULT_HEAD_MINIMUM_CANDIDATES
    )
    parser.add_argument("--head-famous-cap", type=int, default=DEFAULT_HEAD_FAMOUS_CAP)
    parser.add_argument("--no-head", action="store_true")
    parser.add_argument(
        "--input-serving-ordered",
        action="store_true",
        help=(
            "Stream a Parquet file already written in exact Morton serving order; "
            "requires --no-head and validates both planning and record order."
        ),
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = build_region(
        args.input,
        args.output_dir,
        region_name=args.region_name,
        coverage_bbox=args.coverage_bbox,
        row_cap=args.shard_row_cap,
        minimum_level=args.minimum_level,
        maximum_level=args.maximum_level,
        previous_catalog=args.previous_catalog,
        head_minimum_candidates=args.head_minimum_candidates,
        head_famous_cap=args.head_famous_cap,
        build_head=not args.no_head,
        input_serving_ordered=args.input_serving_ordered,
    )
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(serialized)
    print(serialized, end="")


if __name__ == "__main__":
    main()
