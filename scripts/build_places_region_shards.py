#!/usr/bin/env python3
"""Split an extracted Places bbox region into routed compact shards + a head.

This is the region-scale companion to ``prepare_places_worker_smoke.py``. That
script builds a three-fixture relevance oracle; this one takes a single
full-scale bbox extraction (produced by
``experiment_places_partition_extract.py``) and splits it, by serving order,
into as many compact ``.pcsh`` shards as the row cap requires, then writes the
routing ``catalog.pcat`` and (best-effort) the context-free ``head.phrp``.

Format code is reused, not forked: shard bodies come from
``experiment_places_compact_shard.build_artifact``, catalog framing and route
records from ``prepare_places_worker_smoke.build_catalog`` / ``shard_route``,
and the packed head from ``experiment_places_head_repack``. The only new logic
here is the deterministic split.

Serving-order split
-------------------
``ordered_places`` sorts every place by spatial cell (0.25 deg), then by
descending quantized confidence, then by id -- the same order a single shard
stores internally. Splitting that global order into contiguous, near-equal
chunks yields shards that are internally already in serving order, so re-running
``build_artifact`` on a chunk is idempotent, and each shard's unique id doubles
as a routing-catalog ``context`` entry. Two runs are byte-identical because
every step is a total-order sort.

Point-routing recall is NOT single-shard-equivalent -- measure before promotion.
The cell key is row-major (``y`` then ``x``), so a contiguous run of cells is
not a spatially compact rectangle: it steps across ``x`` within a latitude band
and wraps to the next band, giving each shard a wide "staircase" bounding box
whose extent overlaps its neighbours' -- across the whole band, not merely at
the chunk seam. The Worker's ``route_point`` (crates/geocoder-worker/src/
places_pages.rs) picks the single smallest-area covering bbox, so a query point
can route to a shard that overlaps the point but does not contain the point's
cell, and a place can be unreachable via a point query at its own coordinates
(demonstrated: multi-shard point routing returns a strict subset of the
single-shard result for a fraction of points). This is the pre-existing
``route_point`` bounded-result-window contract, now at region scale; it is a
recall regression versus a single shard that MUST be quantified before any slice
built from these shards is promoted. Context routing (by shard id) is exact and
unaffected.

Packed head at region scale
---------------------------
The packed head is a single context-free object consulted before routing, so it
is built over the concatenation of all shards' serving orders. At region scale
the number of admitted head keys can exceed the Worker reader's hard caps
(``READER_MAX_HEAD_KEYS`` / ``READER_MAX_HEAD_INDEX_BYTES`` in
``experiment_places_head_repack``). That is a real, measured guardrail outcome,
not a build error: the head is then reported ``over_reader_caps`` and omitted
from the produced object set rather than failing the whole region build. The
shards and routing catalog -- the load-bearing deliverables -- are always
produced.

The head is not optional at serving time for the query class it owns. The
Worker's ``lookup_places_head_spike`` hard-requires ``head.phrp`` for a
head-eligible query (context-free, one-or-two exact unfielded tokens) and
returns early with no routing fallback, so an omitted head means that query
class cannot be served -- its absence is a hard read error for those queries,
not a graceful miss that falls through to shard routing. Context- and
point-routed queries are unaffected. A region reported ``over_reader_caps`` is
therefore only servable for routed queries; the head-key overflow must be
resolved (or the head query class accepted as unserved) before promotion.

For a full-region, non-promoting scale measurement the CLI supports
``--input-serving-ordered --no-head``. That mode validates the extractor's
total serving order and holds only one shard-sized chunk in Python at a time;
it explicitly records the head as skipped. This bounded-memory signal measures
the load-bearing routed shards and catalog, not context-free serving readiness.

This is an offline producer. It does not access Cloudflare, R2, or Overture S3
and cannot promote a catalog.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import sys
from collections.abc import Iterator
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
from experiment_places_locality_head import ordered_places, spatial_cell  # noqa: E402

# Reuse the smoke builder's catalog framing and route record verbatim so the
# region catalog is byte-compatible with the Worker's PCAT parser.
from prepare_places_worker_smoke import (  # noqa: E402
    TOKENIZER_VERSION,
    build_catalog,
    shard_route,
)


REPORT_SCHEMA = "overture-places-region-build-v1"
DEFAULT_SHARD_ROW_CAP = 1_500_000
DEFAULT_CELL_DEGREES = 0.25
DEFAULT_HEAD_MINIMUM_CANDIDATES = 64
DEFAULT_HEAD_FAMOUS_CAP = 1024


def _chunk_sizes(total: int, row_cap: int) -> list[int]:
    if row_cap < 1:
        raise ValueError("row_cap must be a positive integer")
    if total < 1:
        raise ValueError("input contains no named Places")
    chunk_count = math.ceil(total / row_cap)
    base, remainder = divmod(total, chunk_count)
    return [base + (1 if index < remainder else 0) for index in range(chunk_count)]


def _serving_key(place: Place, cell_degrees: float) -> tuple[str, int, str]:
    return (
        spatial_cell(place, cell_degrees),
        -round(place.confidence * 255),
        place.place_id,
    )


def _parquet_row_count(path: Path) -> int:
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Parquet input requires duckdb") from exc
    connection = duckdb.connect()
    try:
        row = connection.execute(
            "SELECT count(*) FROM read_parquet(?)", [str(path)]
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError("failed to count serving-ordered Parquet rows")
    return int(row[0])


def _iter_parquet_rows_in_file_order(path: Path) -> Iterator[dict[str, Any]]:
    """Read one Parquet file sequentially in its physical insertion order."""
    try:
        import duckdb  # type: ignore
    except ImportError as exc:
        raise RuntimeError("Parquet input requires duckdb") from exc
    connection = duckdb.connect()
    try:
        # A single scan thread avoids parallel row-group interleaving. The
        # monotonic serving-key check below remains the fail-closed proof that
        # the observed stream matches the extractor's declared total order.
        connection.execute("SET threads=1")
        connection.execute("SET preserve_insertion_order=true")
        cursor = connection.execute("SELECT * FROM read_parquet(?)", [str(path)])
        columns = [item[0] for item in cursor.description]
        while batch := cursor.fetchmany(10_000):
            for row in batch:
                yield dict(zip(columns, row))
    finally:
        connection.close()


def iter_serving_order_chunks(
    path: Path,
    *,
    row_cap: int,
    cell_degrees: float,
) -> Iterator[list[Place]]:
    """Stream validated, near-even chunks from a serving-ordered Parquet file.

    The extractor writes a total serving order into the Parquet row stream.
    Only one shard's Python objects are retained at a time, avoiding the full
    region list + global sort + combined-list memory multiplier that can make a
    hosted runner lose communication at CONUS scale.
    """
    if path.suffix.lower() != ".parquet":
        raise ValueError("serving-ordered streaming requires Parquet input")
    total = _parquet_row_count(path)
    sizes = _chunk_sizes(total, row_cap)
    rows = iter(_iter_parquet_rows_in_file_order(path))
    row_number = 0
    previous_key: tuple[str, int, str] | None = None
    for chunk_index, target_size in enumerate(sizes):
        chunk: list[Place] = []
        while len(chunk) < target_size:
            try:
                row = next(rows)
            except StopIteration as exc:
                raise RuntimeError(
                    f"serving-ordered input ended inside chunk {chunk_index}"
                ) from exc
            row_number += 1
            place = place_from_row(row, row_number)
            if not place.name:
                raise ValueError(
                    f"serving-ordered input row {row_number} has no primary name"
                )
            key = _serving_key(place, cell_degrees)
            if previous_key is not None and key < previous_key:
                raise ValueError(
                    "input declared as serving-ordered is not monotonic at "
                    f"row {row_number}: {key!r} < {previous_key!r}"
                )
            previous_key = key
            chunk.append(place)
        yield chunk
    try:
        next(rows)
    except StopIteration:
        return
    raise RuntimeError("serving-ordered input contains more rows than its Parquet count")


def split_serving_order(
    places: list[Place], cell_degrees: float, row_cap: int
) -> list[list[Place]]:
    """Order every place by serving key, then split into <= row_cap chunks.

    The chunk count is ``ceil(n / row_cap)`` and the split is as even as
    possible, so the largest chunk holds ``ceil(n / chunk_count)`` rows -- which
    is always <= ``row_cap``. Each chunk is a contiguous slice of the globally
    ordered list, so it is spatially clustered and already in serving order.
    """
    ordered = ordered_places(places, cell_degrees)
    total = len(ordered)
    sizes = _chunk_sizes(total, row_cap)
    chunks: list[list[Place]] = []
    start = 0
    for size in sizes:
        chunks.append(ordered[start : start + size])
        start += size
    if start != total:
        raise RuntimeError("serving-order split dropped rows")
    if any(len(chunk) > row_cap for chunk in chunks):
        raise RuntimeError("a split chunk exceeds the row cap")
    return chunks


def build_region_head(
    combined: list[Place],
    output_dir: Path,
    *,
    head_minimum_candidates: int,
    head_famous_cap: int,
) -> dict[str, Any]:
    """Best-effort packed head over the combined serving order.

    Returns a status record. ``over_reader_caps`` is a legitimate measured
    outcome at region scale, not a failure: the head object is removed and the
    region build continues with shards + catalog only.
    """
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
    row_cap: int = DEFAULT_SHARD_ROW_CAP,
    cell_degrees: float = DEFAULT_CELL_DEGREES,
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
    output_dir.mkdir(parents=True, exist_ok=True)
    if input_serving_ordered:
        if build_head:
            raise ValueError(
                "serving-ordered streaming requires build_head=False so the "
                "full region is not retained in memory"
            )
        loaded_places = _parquet_row_count(input_path)
        chunk_sizes = _chunk_sizes(loaded_places, row_cap)
        chunks: Iterator[list[Place]] | list[list[Place]] = iter_serving_order_chunks(
            input_path,
            row_cap=row_cap,
            cell_degrees=cell_degrees,
        )
    else:
        places = load_places(input_path)
        loaded_places = len(places)
        chunks = split_serving_order(places, cell_degrees, row_cap)
        chunk_sizes = [len(chunk) for chunk in chunks]

    shard_reports: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    combined: list[Place] | None = [] if build_head else None
    for index, chunk in enumerate(chunks):
        print(
            f"building shard {index + 1}/{len(chunk_sizes)} rows={len(chunk)}",
            file=sys.stderr,
            flush=True,
        )
        shard_id = f"{region_name}-{index:04d}"
        object_name = f"{shard_id}.pcsh"
        artifact_path = output_dir / object_name
        # Both split paths have already established and (for streamed input)
        # validated the exact serving order.
        ordered, report = build_artifact(
            chunk, artifact_path, preserve_input_order=True
        )
        route = shard_route(shard_id, object_name, ordered)
        routes.append(route)
        if combined is not None:
            combined.extend(ordered)
        shard_reports.append(
            {
                "id": shard_id,
                "object": object_name,
                "rows": report["places"],
                "artifact_bytes": report["artifact_bytes"],
                "bytes_per_place": report["bytes_per_place"],
                "tokens": report["tokens"],
                "bbox": route["bbox"],
                "center": route["center"],
            }
        )
        print(
            f"built shard {index + 1}/{len(chunk_sizes)} "
            f"bytes={report['artifact_bytes']}",
            file=sys.stderr,
            flush=True,
        )
        # ``build_artifact`` creates large temporary posting maps and byte
        # buffers. Force collection between shards so the next bounded chunk
        # can reuse the process heap rather than accumulating dead cycles.
        del ordered, chunk
        gc.collect()

    catalog_report = build_catalog(routes, output_dir / "catalog.pcat")
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
                "non-promoting region measurement skips the context-free global "
                "head so full-region rows are not retained in memory"
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
            "shard_row_cap": row_cap,
            "cell_degrees": cell_degrees,
            "head_minimum_candidates": head_minimum_candidates,
            "head_famous_cap": head_famous_cap,
        },
        "totals": {
            "loaded_places": loaded_places,
            "shard_rows": total_rows,
            "shards": len(shard_reports),
            "shard_bytes": total_shard_bytes,
            "bytes_per_place": (
                total_shard_bytes / total_rows if total_rows else 0.0
            ),
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
    parser.add_argument("--shard-row-cap", type=int, default=DEFAULT_SHARD_ROW_CAP)
    parser.add_argument("--cell-degrees", type=float, default=DEFAULT_CELL_DEGREES)
    parser.add_argument(
        "--head-minimum-candidates", type=int, default=DEFAULT_HEAD_MINIMUM_CANDIDATES
    )
    parser.add_argument("--head-famous-cap", type=int, default=DEFAULT_HEAD_FAMOUS_CAP)
    parser.add_argument("--no-head", action="store_true")
    parser.add_argument(
        "--input-serving-ordered",
        action="store_true",
        help=(
            "Stream a Parquet file already written in the exact serving order; "
            "requires --no-head and validates monotonic order while reading."
        ),
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = build_region(
        args.input,
        args.output_dir,
        region_name=args.region_name,
        row_cap=args.shard_row_cap,
        cell_degrees=args.cell_degrees,
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
