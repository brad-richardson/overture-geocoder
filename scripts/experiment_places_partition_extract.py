#!/usr/bin/env python3
"""Deterministic bbox-parameterized Overture Places extractor for partition spikes.

This mirrors ``scripts/factory_extract_places.py`` exactly -- same projection,
same filters, same determinism (``ORDER BY id`` before ``LIMIT`` with
``preserve_insertion_order=true``) -- and only parameterizes the bounding box.
Running this with the California bbox (-124.5..-114.0 x, 32.5..42.1 y) produces
byte-identical parquet to ``factory_extract_places.py`` (verified once locally
by SHA-256 on a 5k-row sample; CI asserts only projection parity via
``test_partition_extractor_projection_matches_factory``). The result is a
source-order-free deterministic sample of a rectangular bbox, not exact
administrative containment or a representative random sample.

It exists so the compact-shard stability experiment can extract a second,
non-California partition without forking or editing the pinned California
extractor. No production pipeline script is modified.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import duckdb


# Kept identical (column list, COALESCE order, casts) to
# factory_extract_places.py so that the California bbox reproduces its output
# byte-for-byte. A test asserts the projection columns stay in sync.
PROJECTION = """
            id AS gers_id,
            names.primary AS primary_name,
            COALESCE(brand.names.primary, '') AS brand_name,
            COALESCE(categories.primary, basic_category, '') AS category_primary,
            COALESCE(addresses[1].locality, '') AS locality,
            COALESCE(addresses[1].region, '') AS region,
            COALESCE(addresses[1].country, '') AS country,
            ST_Y(geometry) AS lat,
            ST_X(geometry) AS lon,
            COALESCE(confidence, 0.5) AS confidence
"""


def _connect(release: str) -> tuple[duckdb.DuckDBPyConnection, str]:
    connection = duckdb.connect()
    connection.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial")
    connection.execute("SET s3_region='us-west-2'")
    connection.execute("SET memory_limit='8GB'")
    connection.execute("SET threads=4")
    # Deterministic output: preserve the ORDER BY id ordering through the COPY
    # so the same release/bbox always yields byte-identical parquet.
    connection.execute("SET preserve_insertion_order=true")
    source = (
        f"s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"
    )
    return connection, source


def _bbox_predicate(xmin: float, xmax: float, ymin: float, ymax: float) -> str:
    # Match factory_extract_places.py's literal formatting so the California
    # bbox produces identical SQL semantics and byte-identical output.
    return (
        f"bbox.xmin BETWEEN {xmin} AND {xmax}\n"
        f"            AND bbox.ymin BETWEEN {ymin} AND {ymax}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--xmin", type=float, required=True)
    parser.add_argument("--xmax", type=float, required=True)
    parser.add_argument("--ymin", type=float, required=True)
    parser.add_argument("--ymax", type=float, required=True)
    parser.add_argument(
        "--count-only",
        action="store_true",
        help="Count matching rows in the bbox (before LIMIT) and exit.",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+", args.release):
        raise SystemExit("invalid release")
    if args.limit <= 0:
        raise SystemExit("limit must be positive")
    if args.xmin >= args.xmax or args.ymin >= args.ymax:
        raise SystemExit("bbox must have xmin<xmax and ymin<ymax")
    if not args.count_only and args.output is None:
        raise SystemExit("--output is required unless --count-only")

    connection, source = _connect(args.release)
    predicate = _bbox_predicate(args.xmin, args.xmax, args.ymin, args.ymax)

    if args.count_only:
        total = connection.execute(
            f"""
            SELECT count(*)
            FROM read_parquet('{source}', hive_partitioning=true)
            WHERE {predicate}
              AND names.primary IS NOT NULL
              AND COALESCE(operating_status, 'open') != 'permanently_closed'
            """
        ).fetchone()[0]
        print(f"bbox_candidate_rows={total}")
        return

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output_sql = str(output).replace("'", "''")
    connection.execute(
        f"""
        COPY (
          SELECT
{PROJECTION.rstrip()}
          FROM read_parquet('{source}', hive_partitioning=true)
          WHERE {predicate}
            AND names.primary IS NOT NULL
            AND COALESCE(operating_status, 'open') != 'permanently_closed'
          ORDER BY id
          LIMIT {args.limit}
        ) TO '{output_sql}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    rows = connection.execute(
        "SELECT count(*) FROM read_parquet(?)", [str(output)]
    ).fetchone()[0]
    print(f"rows={rows} output={output}")


if __name__ == "__main__":
    main()
