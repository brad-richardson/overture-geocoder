#!/usr/bin/env python3
"""
Export SQLite database to chunked SQL files for D1 import.

Usage:
    python scripts/export_to_sql.py indexes/divisions-global.db exports/divisions --limit 1000
    python scripts/export_to_sql.py indexes/US-MA.db exports/us-ma --table features

Output:
    exports/divisions/schema.sql       - Table schema with FTS and triggers
    exports/divisions/data-001.sql     - First chunk of INSERT OR REPLACE statements
    exports/divisions/data-002.sql     - Second chunk, etc.
"""

import argparse
import sqlite3
from pathlib import Path


def get_divisions_schema() -> str:
    """Return the divisions table schema with FTS and triggers."""
    return """-- Divisions table schema for D1
DROP TABLE IF EXISTS divisions_fts;
DROP TABLE IF EXISTS divisions;
DROP TABLE IF EXISTS metadata;

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE divisions (
    rowid INTEGER PRIMARY KEY,
    gers_id TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL DEFAULT 0,
    type TEXT NOT NULL,
    primary_name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    bbox_xmin REAL NOT NULL,
    bbox_ymin REAL NOT NULL,
    bbox_xmax REAL NOT NULL,
    bbox_ymax REAL NOT NULL,
    population INTEGER,
    country TEXT,
    region TEXT,
    search_text TEXT NOT NULL
);

CREATE VIRTUAL TABLE divisions_fts USING fts5(
    search_text,
    content=divisions,
    content_rowid=rowid,
    tokenize='porter unicode61 remove_diacritics 1',
    prefix='2 3'
);

-- Triggers for FTS sync (auto-update on INSERT OR REPLACE)
CREATE TRIGGER divisions_ai AFTER INSERT ON divisions BEGIN
    INSERT INTO divisions_fts(rowid, search_text)
    VALUES (new.rowid, new.search_text);
END;

CREATE TRIGGER divisions_ad AFTER DELETE ON divisions BEGIN
    INSERT INTO divisions_fts(divisions_fts, rowid, search_text)
    VALUES ('delete', old.rowid, old.search_text);
END;

CREATE TRIGGER divisions_au AFTER UPDATE ON divisions BEGIN
    INSERT INTO divisions_fts(divisions_fts, rowid, search_text)
    VALUES ('delete', old.rowid, old.search_text);
    INSERT INTO divisions_fts(rowid, search_text)
    VALUES (new.rowid, new.search_text);
END;

-- Indexes (UNIQUE on gers_id already creates an index)
CREATE INDEX idx_type ON divisions(type);
CREATE INDEX idx_country ON divisions(country);
"""


def get_features_schema() -> str:
    """Return the features table schema with FTS and triggers."""
    return """-- Features table schema for D1
DROP TABLE IF EXISTS features_fts;
DROP TABLE IF EXISTS features;

CREATE TABLE features (
    rowid INTEGER PRIMARY KEY,
    gers_id TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    primary_name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    bbox_xmin REAL NOT NULL,
    bbox_ymin REAL NOT NULL,
    bbox_xmax REAL NOT NULL,
    bbox_ymax REAL NOT NULL,
    population INTEGER,
    city TEXT,
    state TEXT,
    postcode TEXT,
    search_text TEXT NOT NULL
);

CREATE VIRTUAL TABLE features_fts USING fts5(
    search_text,
    content=features,
    content_rowid=rowid,
    tokenize='porter unicode61 remove_diacritics 1'
);

-- Triggers for FTS sync (auto-update on INSERT OR REPLACE)
CREATE TRIGGER features_ai AFTER INSERT ON features BEGIN
    INSERT INTO features_fts(rowid, search_text)
    VALUES (new.rowid, new.search_text);
END;

CREATE TRIGGER features_ad AFTER DELETE ON features BEGIN
    INSERT INTO features_fts(features_fts, rowid, search_text)
    VALUES ('delete', old.rowid, old.search_text);
END;

CREATE TRIGGER features_au AFTER UPDATE ON features BEGIN
    INSERT INTO features_fts(features_fts, rowid, search_text)
    VALUES ('delete', old.rowid, old.search_text);
    INSERT INTO features_fts(rowid, search_text)
    VALUES (new.rowid, new.search_text);
END;

-- Indexes (UNIQUE on gers_id already creates an index)
CREATE INDEX idx_type ON features(type);
"""


def get_divisions_reverse_schema() -> str:
    """Return the divisions_reverse table schema for reverse geocoding."""
    return """-- Divisions reverse table schema for D1 (reverse geocoding)
-- Note: hierarchy_json removed - hierarchy built from query results
DROP TABLE IF EXISTS divisions_reverse;

CREATE TABLE divisions_reverse (
    rowid INTEGER PRIMARY KEY,
    gers_id TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL DEFAULT 0,
    subtype TEXT NOT NULL,
    primary_name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    population INTEGER,
    country TEXT,
    region TEXT,
    bbox_xmin REAL NOT NULL,
    bbox_ymin REAL NOT NULL,
    bbox_xmax REAL NOT NULL,
    bbox_ymax REAL NOT NULL,
    area REAL,
    h3_cells TEXT
);

-- Metadata table
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Spatial indexes for bbox queries
CREATE INDEX idx_bbox_xmin ON divisions_reverse(bbox_xmin);
CREATE INDEX idx_bbox_xmax ON divisions_reverse(bbox_xmax);
CREATE INDEX idx_bbox_ymin ON divisions_reverse(bbox_ymin);
CREATE INDEX idx_bbox_ymax ON divisions_reverse(bbox_ymax);

-- Additional indexes
CREATE INDEX idx_subtype ON divisions_reverse(subtype);
CREATE INDEX idx_country ON divisions_reverse(country);
CREATE INDEX idx_area ON divisions_reverse(area);
CREATE INDEX idx_h3_cells ON divisions_reverse(h3_cells);
"""


def escape_sql_string(s: str) -> str:
    """Escape a string for SQL by doubling single quotes."""
    if s is None:
        return "NULL"
    return "'" + s.replace("'", "''") + "'"


def format_value(val) -> str:
    """Format a Python value for SQL."""
    if val is None:
        return "NULL"
    if isinstance(val, str):
        return escape_sql_string(val)
    if isinstance(val, (int, float)):
        return str(val)
    return escape_sql_string(str(val))


def export_to_sql(
    db_path: Path,
    output_dir: Path,
    table: str = "divisions",
    chunk_size: int = 50000,
    limit: int | None = None,
):
    """Export SQLite table to chunked SQL files with INSERT OR REPLACE."""

    if not db_path.exists():
        print(f"Error: Database not found: {db_path}")
        return False

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write schema file
    schema_file = output_dir / "schema.sql"
    if table == "divisions":
        schema = get_divisions_schema()
        columns = [
            "gers_id", "version", "type", "primary_name", "lat", "lon",
            "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
            "population", "country", "region", "search_text"
        ]
    elif table == "divisions_reverse":
        schema = get_divisions_reverse_schema()
        columns = [
            "gers_id", "version", "subtype", "primary_name", "lat", "lon",
            "population", "country", "region",
            "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
            "area", "h3_cells"
        ]
    else:
        schema = get_features_schema()
        columns = [
            "gers_id", "type", "primary_name", "lat", "lon",
            "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
            "population", "city", "state", "postcode", "search_text"
        ]

    with open(schema_file, "w") as f:
        f.write(schema)
    print(f"Wrote schema to {schema_file}")

    # Connect to source database
    db = sqlite3.connect(db_path)

    # Get total count
    total_query = f"SELECT COUNT(*) FROM {table}"
    if limit:
        total_count = min(limit, db.execute(total_query).fetchone()[0])
    else:
        total_count = db.execute(total_query).fetchone()[0]

    print(f"Exporting {total_count:,} rows from {table}")

    # Build select query
    cols_str = ", ".join(columns)
    query = f"SELECT {cols_str} FROM {table}"
    if limit:
        query += f" LIMIT {limit}"

    cursor = db.execute(query)

    # Export in chunks
    chunk_num = 1
    rows_in_chunk = 0
    total_exported = 0
    current_file = None

    for row in cursor:
        # Start new chunk if needed
        if rows_in_chunk == 0:
            if current_file:
                current_file.close()

            chunk_file = output_dir / f"data-{chunk_num:03d}.sql"
            current_file = open(chunk_file, "w")
            current_file.write(f"-- Chunk {chunk_num}: rows {total_exported + 1} to {min(total_exported + chunk_size, total_count)}\n\n")

        # Format INSERT OR REPLACE statement
        values = ", ".join(format_value(v) for v in row)
        stmt = f"INSERT OR REPLACE INTO {table} ({cols_str}) VALUES ({values});\n"
        current_file.write(stmt)

        rows_in_chunk += 1
        total_exported += 1

        # Close chunk if full
        if rows_in_chunk >= chunk_size:
            current_file.close()
            print(f"  Wrote {chunk_file.name} ({rows_in_chunk:,} rows)")
            chunk_num += 1
            rows_in_chunk = 0
            current_file = None

    # Close final chunk
    if current_file:
        current_file.close()
        print(f"  Wrote data-{chunk_num:03d}.sql ({rows_in_chunk:,} rows)")

    db.close()

    print(f"\nExported {total_exported:,} rows to {output_dir}/")
    print(f"  - schema.sql")
    for i in range(1, chunk_num + 1):
        print(f"  - data-{i:03d}.sql")

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Export SQLite database to chunked SQL files for D1 import"
    )
    parser.add_argument(
        "db_path",
        type=Path,
        help="Path to source SQLite database"
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        help="Output directory for SQL files"
    )
    parser.add_argument(
        "--table",
        default="divisions",
        choices=["divisions", "divisions_reverse", "features"],
        help="Table to export (default: divisions)"
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=50000,
        help="Rows per SQL file (default: 50000)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of rows to export (for testing)"
    )

    args = parser.parse_args()

    success = export_to_sql(
        db_path=args.db_path,
        output_dir=args.output_dir,
        table=args.table,
        chunk_size=args.chunk_size,
        limit=args.limit,
    )

    return 0 if success else 1


if __name__ == "__main__":
    exit(main())
