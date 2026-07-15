#!/usr/bin/env python3
"""Extract bounded Overture division areas for offline address experiments."""

from __future__ import annotations

import argparse
from pathlib import Path

try:
    import duckdb
except ImportError as exc:  # pragma: no cover - CLI dependency
    raise SystemExit("extract_division_areas.py requires duckdb") from exc


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True)
    parser.add_argument("--country", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    source = (
        "s3://overturemaps-us-west-2/release/"
        f"{args.release}/theme=divisions/type=division_area/*"
    )
    connection = duckdb.connect()
    connection.execute("INSTALL spatial; LOAD spatial")
    connection.execute("INSTALL httpfs; LOAD httpfs")
    connection.execute("SET s3_region='us-west-2'")
    connection.execute(
        """
        CREATE TEMP TABLE selected_areas AS
            SELECT
                division_id,
                subtype,
                admin_level,
                names.primary AS name,
                country,
                region,
                geometry,
                bbox,
                ST_Area_Spheroid(geometry) AS area_m2
            FROM read_parquet(?, hive_partitioning = 1)
            WHERE country = ?
              AND region = ?
              AND is_land = true
        """,
        [source, args.country, args.region],
    )
    output_sql = "'" + str(args.output).replace("'", "''") + "'"
    connection.execute(
        f"COPY selected_areas TO {output_sql} (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    row_count = connection.execute(
        "SELECT count(*) FROM read_parquet(?)", [str(args.output)]
    ).fetchone()[0]
    print(f"wrote {row_count:,} division areas to {args.output}")


if __name__ == "__main__":
    main()
