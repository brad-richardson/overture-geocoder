#!/usr/bin/env python3
"""Extract the bounded Overture Places projection used by factory spikes.

Rows come from a rectangular California-area bbox and a source-order LIMIT.
This is neither exact California containment nor a random/representative sample.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import duckdb


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+", args.release):
        raise SystemExit("invalid release")
    if args.limit <= 0:
        raise SystemExit("limit must be positive")

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output_sql = str(output).replace("'", "''")
    source = (
        f"s3://overturemaps-us-west-2/release/{args.release}/"
        "theme=places/type=place/*"
    )

    connection = duckdb.connect()
    connection.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial")
    connection.execute("SET s3_region='us-west-2'")
    connection.execute("SET memory_limit='8GB'")
    connection.execute("SET threads=4")
    connection.execute("SET preserve_insertion_order=false")
    connection.execute(
        f"""
        COPY (
          SELECT
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
          FROM read_parquet('{source}', hive_partitioning=true)
          WHERE bbox.xmin BETWEEN -124.5 AND -114.0
            AND bbox.ymin BETWEEN 32.5 AND 42.1
            AND names.primary IS NOT NULL
            AND COALESCE(operating_status, 'open') != 'permanently_closed'
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
