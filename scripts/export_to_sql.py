#!/usr/bin/env python3
"""
Export SQLite database to chunked SQL files for D1 import.

Supports two modes:
  - rebuild: Plain INSERT for fresh databases (fastest, no conflict handling)
  - incremental: INSERT with ON CONFLICT for live updates (proper UPSERT)

Usage:
    # Rebuild mode (for DB swap workflow)
    python scripts/export_to_sql.py indexes/divisions-global.db exports/divisions --mode rebuild

    # Incremental mode (for live updates)
    python scripts/export_to_sql.py indexes/divisions-global.db exports/divisions --mode incremental

Output (rebuild mode):
    exports/divisions/schema-base.sql    - Tables only (no indexes, no FTS)
    exports/divisions/schema-indexes.sql - UNIQUE constraints and indexes
    exports/divisions/schema-fts.sql     - FTS5 tables, rebuild, and triggers
    exports/divisions/data-001.sql       - First chunk of INSERT statements
    exports/divisions/data-002.sql       - Second chunk, etc.

Output (incremental mode):
    exports/divisions/data-001.sql       - INSERT ... ON CONFLICT statements
"""

import argparse
import sqlite3
from pathlib import Path


# =============================================================================
# DIVISIONS (Forward Geocoding) - Phased Schema
# =============================================================================

def get_divisions_base_schema() -> str:
    """Return base table schema (no indexes, no FTS) for bulk loading."""
    return """-- Divisions base schema for D1 (Phase 1: Tables only)
-- Apply this first, then bulk load data, then apply indexes and FTS

DROP TABLE IF EXISTS divisions_fts;
DROP TRIGGER IF EXISTS divisions_ai;
DROP TRIGGER IF EXISTS divisions_ad;
DROP TRIGGER IF EXISTS divisions_au;
DROP TABLE IF EXISTS divisions;
DROP TABLE IF EXISTS metadata;

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE divisions (
    rowid INTEGER PRIMARY KEY,
    gers_id TEXT NOT NULL,
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
"""


def get_divisions_indexes_schema() -> str:
    """Return index schema (apply after data load)."""
    return """-- Divisions indexes for D1 (Phase 2: After data load)
-- Creating UNIQUE index after bulk load is faster than maintaining during inserts

CREATE UNIQUE INDEX IF NOT EXISTS idx_divisions_gers_id ON divisions(gers_id);
"""


def get_divisions_fts_schema() -> str:
    """Return FTS schema with triggers (apply after indexes)."""
    return """-- Divisions FTS for D1 (Phase 3: After indexes)
-- Creates FTS table, rebuilds index from existing data, adds sync triggers

CREATE VIRTUAL TABLE IF NOT EXISTS divisions_fts USING fts5(
    search_text,
    content=divisions,
    content_rowid=rowid,
    tokenize='porter unicode61 remove_diacritics 1',
    prefix='2 3'
);

-- Rebuild FTS index from existing data (one-time bulk operation)
INSERT INTO divisions_fts(divisions_fts) VALUES('rebuild');

-- Triggers for FTS sync (for incremental updates after initial load)
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
"""


def get_divisions_schema() -> str:
    """Return the full divisions schema (for backwards compatibility)."""
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

-- Note: UNIQUE on gers_id already creates an index
-- idx_type and idx_country removed as unused in current queries
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

-- Note: UNIQUE on gers_id already creates an index
-- idx_type removed as unused in current queries
"""


# =============================================================================
# DIVISIONS_REVERSE (Reverse Geocoding) - Phased Schema
# =============================================================================

def get_divisions_reverse_base_schema() -> str:
    """Return base table schema for reverse geocoding (no indexes)."""
    return """-- Divisions reverse base schema for D1 (Phase 1: Tables only)
DROP TABLE IF EXISTS divisions_reverse;
DROP TABLE IF EXISTS metadata;

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE divisions_reverse (
    rowid INTEGER PRIMARY KEY,
    gers_id TEXT NOT NULL,
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
    area REAL
);
"""


def get_divisions_reverse_indexes_schema() -> str:
    """Return index schema for reverse geocoding (apply after data load)."""
    return """-- Divisions reverse indexes for D1 (Phase 2: After data load)

-- UNIQUE constraint on gers_id
CREATE UNIQUE INDEX IF NOT EXISTS idx_divisions_reverse_gers_id ON divisions_reverse(gers_id);

-- Composite spatial index for bbox queries (more efficient than 4 separate indexes)
CREATE INDEX IF NOT EXISTS idx_bbox ON divisions_reverse(bbox_xmin, bbox_xmax, bbox_ymin, bbox_ymax);

-- Area index for ORDER BY optimization
CREATE INDEX IF NOT EXISTS idx_area ON divisions_reverse(area);
"""


def get_divisions_reverse_schema() -> str:
    """Return the full divisions_reverse schema (for backwards compatibility)."""
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
    area REAL
);

-- Metadata table
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Composite spatial index for bbox queries (more efficient than 4 separate indexes)
CREATE INDEX idx_bbox ON divisions_reverse(bbox_xmin, bbox_xmax, bbox_ymin, bbox_ymax);

-- Area index for ORDER BY optimization
CREATE INDEX idx_area ON divisions_reverse(area);

-- Note: idx_subtype and idx_country removed as unused in current queries
-- Note: UNIQUE on gers_id already creates an index
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


# =============================================================================
# Table column definitions
# =============================================================================

TABLE_COLUMNS = {
    "divisions": [
        "gers_id", "version", "type", "primary_name", "lat", "lon",
        "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
        "population", "country", "region", "search_text"
    ],
    "divisions_reverse": [
        "gers_id", "version", "subtype", "primary_name", "lat", "lon",
        "population", "country", "region",
        "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
        "area"
    ],
    "features": [
        "gers_id", "type", "primary_name", "lat", "lon",
        "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
        "population", "city", "state", "postcode", "search_text"
    ],
}

# Columns to update in UPSERT (excludes gers_id which is the conflict key)
UPSERT_COLUMNS = {
    "divisions": [
        "version", "type", "primary_name", "lat", "lon",
        "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
        "population", "country", "region", "search_text"
    ],
    "divisions_reverse": [
        "version", "subtype", "primary_name", "lat", "lon",
        "population", "country", "region",
        "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
        "area"
    ],
    "features": [
        "type", "primary_name", "lat", "lon",
        "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
        "population", "city", "state", "postcode", "search_text"
    ],
}


def generate_insert_statement(table: str, columns: list[str], values: str, mode: str) -> str:
    """Generate an INSERT statement based on mode.

    Args:
        table: Table name
        columns: List of column names
        values: Formatted SQL values string
        mode: 'rebuild' for plain INSERT, 'incremental' for UPSERT

    Returns:
        SQL INSERT statement
    """
    cols_str = ", ".join(columns)

    if mode == "rebuild":
        # Plain INSERT - no conflict handling needed for fresh DB
        return f"INSERT INTO {table} ({cols_str}) VALUES ({values});\n"
    else:
        # Proper UPSERT with ON CONFLICT
        update_cols = UPSERT_COLUMNS.get(table, columns[1:])  # Skip gers_id
        set_clauses = ", ".join(f"{col} = excluded.{col}" for col in update_cols)
        return (
            f"INSERT INTO {table} ({cols_str}) VALUES ({values})\n"
            f"ON CONFLICT(gers_id) DO UPDATE SET {set_clauses};\n"
        )


def write_phased_schema(output_dir: Path, table: str) -> list[str]:
    """Write phased schema files for rebuild mode.

    Returns list of schema files written.
    """
    files_written = []

    if table == "divisions":
        # Base schema
        base_file = output_dir / "schema-base.sql"
        with open(base_file, "w") as f:
            f.write(get_divisions_base_schema())
        files_written.append("schema-base.sql")

        # Indexes
        indexes_file = output_dir / "schema-indexes.sql"
        with open(indexes_file, "w") as f:
            f.write(get_divisions_indexes_schema())
        files_written.append("schema-indexes.sql")

        # FTS
        fts_file = output_dir / "schema-fts.sql"
        with open(fts_file, "w") as f:
            f.write(get_divisions_fts_schema())
        files_written.append("schema-fts.sql")

    elif table == "divisions_reverse":
        # Base schema
        base_file = output_dir / "schema-base.sql"
        with open(base_file, "w") as f:
            f.write(get_divisions_reverse_base_schema())
        files_written.append("schema-base.sql")

        # Indexes
        indexes_file = output_dir / "schema-indexes.sql"
        with open(indexes_file, "w") as f:
            f.write(get_divisions_reverse_indexes_schema())
        files_written.append("schema-indexes.sql")

    else:
        # Features - no phased schema yet, use full schema
        schema_file = output_dir / "schema.sql"
        with open(schema_file, "w") as f:
            f.write(get_features_schema())
        files_written.append("schema.sql")

    return files_written


def export_to_sql(
    db_path: Path,
    output_dir: Path,
    table: str = "divisions",
    chunk_size: int = 50000,
    limit: int | None = None,
    mode: str = "rebuild",
):
    """Export SQLite table to chunked SQL files.

    Args:
        db_path: Path to source SQLite database
        output_dir: Directory for output SQL files
        table: Table to export (divisions, divisions_reverse, features)
        chunk_size: Rows per SQL file
        limit: Limit rows exported (for testing)
        mode: 'rebuild' for fresh DB (plain INSERT), 'incremental' for UPSERT

    Modes:
        rebuild: Generates phased schema files and plain INSERT statements.
                 Use with DB swap workflow for zero-downtime rebuilds.
        incremental: Generates INSERT ... ON CONFLICT statements.
                     Use for live updates to existing database.
    """
    if not db_path.exists():
        print(f"Error: Database not found: {db_path}")
        return False

    if mode not in ("rebuild", "incremental"):
        print(f"Error: Invalid mode '{mode}'. Must be 'rebuild' or 'incremental'")
        return False

    columns = TABLE_COLUMNS.get(table)
    if not columns:
        print(f"Error: Unknown table '{table}'")
        return False

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write schema files
    if mode == "rebuild":
        schema_files = write_phased_schema(output_dir, table)
        for sf in schema_files:
            print(f"Wrote {sf}")
    else:
        # Incremental mode - write full schema for reference only
        schema_file = output_dir / "schema.sql"
        if table == "divisions":
            schema = get_divisions_schema()
        elif table == "divisions_reverse":
            schema = get_divisions_reverse_schema()
        else:
            schema = get_features_schema()
        with open(schema_file, "w") as f:
            f.write(schema)
        print(f"Wrote schema.sql (reference only)")

    # Connect to source database
    db = sqlite3.connect(db_path)

    # Get total count
    total_query = f"SELECT COUNT(*) FROM {table}"
    if limit:
        total_count = min(limit, db.execute(total_query).fetchone()[0])
    else:
        total_count = db.execute(total_query).fetchone()[0]

    print(f"Exporting {total_count:,} rows from {table} (mode: {mode})")

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
            current_file.write(f"-- Chunk {chunk_num}: rows {total_exported + 1} to {min(total_exported + chunk_size, total_count)}\n")
            current_file.write(f"-- Mode: {mode}\n\n")

        # Format INSERT statement based on mode
        values = ", ".join(format_value(v) for v in row)
        stmt = generate_insert_statement(table, columns, values, mode)
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
    if mode == "rebuild":
        print(f"\nRebuild mode output:")
        print(f"  1. Apply schema-base.sql (creates tables)")
        print(f"  2. Load data-*.sql files (bulk INSERT)")
        print(f"  3. Apply schema-indexes.sql (creates indexes)")
        if table == "divisions":
            print(f"  4. Apply schema-fts.sql (creates FTS + triggers)")
    else:
        print(f"\nIncremental mode: data files use INSERT ... ON CONFLICT")

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
        "--mode",
        default="rebuild",
        choices=["rebuild", "incremental"],
        help="Export mode: 'rebuild' for fresh DB (plain INSERT), 'incremental' for UPSERT (default: rebuild)"
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
        mode=args.mode,
    )

    return 0 if success else 1


if __name__ == "__main__":
    exit(main())
