#!/usr/bin/env python3
"""
Differential update script for Overture geocoder D1 database.

Compares new Overture data against production and generates minimal SQL updates.
Uses the version field (which increments when features change) to identify updates.

Uses proper UPSERT (INSERT ... ON CONFLICT) to avoid delete+reinsert overhead
that occurs with INSERT OR REPLACE. This is critical for minimizing D1 write costs.

Supports two table types:
- divisions: Forward geocoding with FTS search_text
- divisions_reverse: Reverse geocoding with bbox/H3 spatial index

Usage:
    # Forward geocoding (default)
    python scripts/diff_and_update.py indexes/divisions-global.db exports/diff \
        --prod-versions prod_versions.csv --release 2025-12-17.0

    # Reverse geocoding
    python scripts/diff_and_update.py indexes/divisions-reverse.db exports/diff-reverse \
        --prod-versions prod_versions_reverse.csv --release 2025-12-17.0 \
        --table divisions_reverse

Output:
    exports/diff/upserts.sql    - INSERT ... ON CONFLICT for new/changed records
    exports/diff/deletes.sql    - DELETE for removed records
    exports/diff/metadata.sql   - UPDATE release version
    exports/diff/stats.json     - Statistics about the diff
"""

import argparse
import csv
import json
import math
import sqlite3
from pathlib import Path

# Table schemas for different database types
TABLE_SCHEMAS = {
    "divisions": {
        "columns": [
            "gers_id", "version", "type", "primary_name", "lat", "lon",
            "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
            "population", "country", "region", "search_text"
        ],
        # Columns to update on conflict (excludes gers_id which is the key)
        "update_columns": [
            "version", "type", "primary_name", "lat", "lon",
            "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
            "population", "country", "region", "search_text"
        ],
    },
    "divisions_reverse": {
        "columns": [
            "gers_id", "version", "subtype", "primary_name", "lat", "lon",
            "population", "country", "region",
            "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
            "area"
        ],
        "update_columns": [
            "version", "subtype", "primary_name", "lat", "lon",
            "population", "country", "region",
            "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
            "area"
        ],
    },
}


def load_prod_versions(csv_path: Path) -> dict[str, int]:
    """Load production gers_id -> version mapping from CSV.

    Handles case-insensitive column names and various edge cases.
    """
    versions = {}
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)

        # Normalize fieldnames to lowercase for case-insensitive matching
        if reader.fieldnames:
            fieldname_map = {name.lower(): name for name in reader.fieldnames}
        else:
            return versions

        gers_id_col = fieldname_map.get("gers_id")
        version_col = fieldname_map.get("version")

        if not gers_id_col or not version_col:
            print(f"Warning: CSV missing required columns. Found: {reader.fieldnames}")
            return versions

        for row in reader:
            gers_id = row.get(gers_id_col, "").strip()
            version_str = row.get(version_col, "").strip()

            if gers_id and version_str:
                try:
                    versions[gers_id] = int(version_str)
                except ValueError:
                    print(f"Warning: Invalid version '{version_str}' for gers_id '{gers_id}'")
                    continue

    return versions


def escape_sql_string(s: str) -> str:
    """Escape a string for SQL by doubling single quotes."""
    if s is None:
        return "NULL"
    return "'" + s.replace("'", "''") + "'"


def format_value(val) -> str:
    """Format a Python value for SQL.

    Handles None, strings, integers, floats (including edge cases like NaN/Infinity).
    """
    if val is None:
        return "NULL"
    if isinstance(val, str):
        return escape_sql_string(val)
    if isinstance(val, float):
        # Handle special float values
        if math.isnan(val) or math.isinf(val):
            return "NULL"
        return str(val)
    if isinstance(val, int):
        return str(val)
    return escape_sql_string(str(val))


def generate_upsert_statement(table: str, columns: list[str], update_columns: list[str], values: str) -> str:
    """Generate a proper UPSERT statement using ON CONFLICT.

    Uses INSERT ... ON CONFLICT(gers_id) DO UPDATE SET instead of INSERT OR REPLACE
    to avoid delete+reinsert overhead and reduce D1 write costs.
    """
    cols_str = ", ".join(columns)
    set_clauses = ", ".join(f"{col} = excluded.{col}" for col in update_columns)
    return (
        f"INSERT INTO {table} ({cols_str}) VALUES ({values})\n"
        f"ON CONFLICT(gers_id) DO UPDATE SET {set_clauses};\n"
    )


def generate_diff(
    db_path: Path,
    output_dir: Path,
    prod_versions: dict[str, int],
    release: str,
    table: str = "divisions",
    chunk_size: int = 10000,
    force_all: bool = False,
) -> dict:
    """Generate differential SQL updates.

    Uses proper UPSERT (INSERT ... ON CONFLICT) instead of INSERT OR REPLACE
    to minimize D1 write costs. INSERT OR REPLACE causes a delete+reinsert
    which triggers extra index and FTS maintenance.

    Args:
        force_all: If True, treat all records as updates (for schema/logic changes)
    """

    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    if table not in TABLE_SCHEMAS:
        raise ValueError(f"Unknown table: {table}. Must be one of: {list(TABLE_SCHEMAS.keys())}")

    output_dir.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(db_path)

    schema = TABLE_SCHEMAS[table]
    columns = schema["columns"]
    update_columns = schema["update_columns"]
    cols_str = ", ".join(columns)

    # Track stats
    stats = {
        "release": release,
        "force_all": force_all,
        "total_new": 0,
        "inserts": 0,
        "updates": 0,
        "unchanged": 0,
        "deletes": 0,
    }

    # Track which gers_ids we've seen
    seen_gers_ids = set()

    # Open upserts file
    upserts_file = output_dir / "upserts.sql"
    with open(upserts_file, "w") as f:
        if force_all:
            f.write(f"-- Force all: updating all records for {table}\n")
        else:
            f.write(f"-- Upserts: new and changed records for {table}\n")
        f.write(f"-- Using INSERT ... ON CONFLICT (proper UPSERT) to minimize write costs\n\n")

        cursor = db.execute(f"SELECT {cols_str} FROM {table}")

        batch_count = 0
        for row in cursor:
            gers_id = row[0]
            new_version = row[1]
            seen_gers_ids.add(gers_id)
            stats["total_new"] += 1

            prod_version = prod_versions.get(gers_id)

            if prod_version is None:
                # New record - use UPSERT for safety (handles race conditions)
                stats["inserts"] += 1
                values = ", ".join(format_value(v) for v in row)
                f.write(generate_upsert_statement(table, columns, update_columns, values))
                batch_count += 1
            elif force_all or new_version > prod_version:
                # Updated record (or forced update)
                stats["updates"] += 1
                values = ", ".join(format_value(v) for v in row)
                f.write(generate_upsert_statement(table, columns, update_columns, values))
                batch_count += 1
            else:
                # Unchanged
                stats["unchanged"] += 1

            # Progress marker for large updates
            if batch_count > 0 and batch_count % chunk_size == 0:
                f.write(f"-- Progress: {batch_count} records\n")

    db.close()

    # Generate deletes for records no longer in source
    deletes_file = output_dir / "deletes.sql"
    with open(deletes_file, "w") as f:
        f.write(f"-- Deletes: records removed from Overture for {table}\n\n")

        for gers_id in prod_versions.keys():
            if gers_id not in seen_gers_ids:
                stats["deletes"] += 1
                f.write(f"DELETE FROM {table} WHERE gers_id = {escape_sql_string(gers_id)};\n")

    # Generate metadata update (using proper UPSERT)
    metadata_file = output_dir / "metadata.sql"
    with open(metadata_file, "w") as f:
        f.write("-- Update release metadata\n")
        f.write(f"INSERT INTO metadata (key, value) VALUES ('overture_release', {escape_sql_string(release)})\n")
        f.write(f"ON CONFLICT(key) DO UPDATE SET value = excluded.value;\n")
        f.write(f"INSERT INTO metadata (key, value) VALUES ('updated_at', datetime('now'))\n")
        f.write(f"ON CONFLICT(key) DO UPDATE SET value = excluded.value;\n")

    # Calculate destructive change percentage
    prod_count = len(prod_versions)
    if prod_count > 0:
        stats["prod_count"] = prod_count
        stats["destructive_changes"] = stats["updates"] + stats["deletes"]
        stats["destructive_pct"] = round(100 * stats["destructive_changes"] / prod_count, 2)

    # Write stats
    stats_file = output_dir / "stats.json"
    with open(stats_file, "w") as f:
        json.dump(stats, f, indent=2)

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Generate differential SQL updates for D1"
    )
    parser.add_argument(
        "db_path",
        type=Path,
        help="Path to new SQLite database"
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        help="Output directory for SQL files"
    )
    parser.add_argument(
        "--prod-versions",
        type=Path,
        required=True,
        help="CSV file with production gers_id,version data"
    )
    parser.add_argument(
        "--release",
        required=True,
        help="Overture release version (e.g., 2025-12-17.0)"
    )
    parser.add_argument(
        "--table",
        default="divisions",
        choices=list(TABLE_SCHEMAS.keys()),
        help="Table schema to use (default: divisions)"
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=10000,
        help="Commit every N records (default: 10000)"
    )
    parser.add_argument(
        "--max-change-pct",
        type=float,
        default=5.0,
        help="Maximum allowed change percentage (updates + deletes) before aborting (default: 5.0)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the safety check and allow any change percentage"
    )
    parser.add_argument(
        "--force-all",
        action="store_true",
        help="Treat all records as updates (for schema/logic changes)"
    )

    args = parser.parse_args()

    print(f"Loading production versions from {args.prod_versions}...")
    prod_versions = load_prod_versions(args.prod_versions)
    print(f"  Found {len(prod_versions):,} records in production")

    if args.force_all:
        print(f"\n⚠️  FORCE ALL: All records will be marked as updates")

    print(f"\nGenerating diff from {args.db_path} (table: {args.table})...")
    stats = generate_diff(
        db_path=args.db_path,
        output_dir=args.output_dir,
        prod_versions=prod_versions,
        release=args.release,
        table=args.table,
        chunk_size=args.chunk_size,
        force_all=args.force_all,
    )

    print(f"\nDiff statistics:")
    print(f"  Total in new release: {stats['total_new']:,}")
    print(f"  New records (inserts): {stats['inserts']:,}")
    print(f"  Changed records (updates): {stats['updates']:,}")
    print(f"  Unchanged records: {stats['unchanged']:,}")
    print(f"  Removed records (deletes): {stats['deletes']:,}")

    changes = stats['inserts'] + stats['updates'] + stats['deletes']
    if changes == 0:
        print(f"\n  No changes detected!")
    else:
        pct = 100 * changes / max(stats['total_new'], 1)
        print(f"\n  Total changes: {changes:,} ({pct:.2f}% of data)")

    # Safety check: ensure updates + deletes don't exceed threshold
    # (inserts are generally safe - they're new data)
    # Skip safety check if --force-all since we're intentionally updating everything
    if "destructive_pct" in stats:
        print(f"\n  Destructive changes (updates + deletes): {stats['destructive_changes']:,} ({stats['destructive_pct']:.2f}% of production)")

        if stats["destructive_pct"] > args.max_change_pct and not args.force and not args.force_all:
            print(f"\n❌ SAFETY CHECK FAILED: Destructive changes ({stats['destructive_pct']:.2f}%) exceed threshold ({args.max_change_pct}%)")
            print(f"   This could indicate a data issue or schema change.")
            print(f"   Use --force to override, or --max-change-pct to adjust the threshold.")
            return 1

    print(f"\nOutput files:")
    print(f"  {args.output_dir}/upserts.sql")
    print(f"  {args.output_dir}/deletes.sql")
    print(f"  {args.output_dir}/metadata.sql")
    print(f"  {args.output_dir}/stats.json")

    return 0


if __name__ == "__main__":
    exit(main())
