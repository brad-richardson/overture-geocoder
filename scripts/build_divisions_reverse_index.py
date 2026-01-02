#!/usr/bin/env python3
"""
Build SQLite spatial index for reverse geocoding from divisions Parquet.

This index is optimized for spatial queries (point-in-bbox) rather than text search.
No FTS5 is needed - we use B-tree indexes on bbox columns instead.

Usage:
    python scripts/build_divisions_reverse_index.py

Input:
    exports/divisions-reverse.parquet

Output:
    indexes/divisions-reverse.db
"""

import sqlite3
from pathlib import Path

import duckdb


def build_divisions_reverse_index(
    parquet_path: Path = Path("exports/divisions-reverse.parquet"),
    output_db: Path = Path("indexes/divisions-reverse.db"),
    batch_size: int = 50000,
):
    """Build SQLite spatial index for reverse geocoding."""

    if not parquet_path.exists():
        print(f"Error: {parquet_path} not found")
        print("Run: ./scripts/download_divisions.sh --reverse")
        print("  or: duckdb < scripts/download_divisions_reverse.sql")
        return

    con = duckdb.connect()

    # Create SQLite database
    output_db.parent.mkdir(parents=True, exist_ok=True)
    if output_db.exists():
        output_db.unlink()

    db = sqlite3.connect(output_db)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA cache_size=-128000")  # 128MB cache

    # Create metadata table
    db.execute("""
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # Create table for divisions (optimized for spatial queries)
    # Note: hierarchy_json and parent_division_id removed - hierarchy built from query results
    db.execute("""
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
        )
    """)

    # Read and insert divisions
    print(f"Reading divisions from {parquet_path}...")
    total_count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{parquet_path}')"
    ).fetchone()[0]
    print(f"Found {total_count:,} divisions")

    cursor = con.execute(f"""
        SELECT
            gers_id,
            version,
            subtype,
            primary_name,
            lat,
            lon,
            population,
            country,
            region,
            bbox_xmin,
            bbox_ymin,
            bbox_xmax,
            bbox_ymax,
            area,
            h3_cells
        FROM read_parquet('{parquet_path}')
    """)

    batch = []
    inserted = 0

    for row in cursor.fetchall():
        batch.append(row)

        if len(batch) >= batch_size:
            _insert_batch(db, batch)
            inserted += len(batch)
            pct = 100 * inserted / total_count
            print(f"  Progress: {inserted:,} / {total_count:,} ({pct:.1f}%)")
            batch = []

    if batch:
        _insert_batch(db, batch)
        inserted += len(batch)
        print(f"  Progress: {inserted:,} / {total_count:,} (100%)")

    # Create spatial indexes for bbox queries
    # These indexes enable efficient queries like:
    #   WHERE bbox_xmin <= lon AND bbox_xmax >= lon
    #     AND bbox_ymin <= lat AND bbox_ymax >= lat
    print("Creating spatial indexes...")
    db.execute("CREATE INDEX idx_bbox_xmin ON divisions_reverse(bbox_xmin)")
    db.execute("CREATE INDEX idx_bbox_xmax ON divisions_reverse(bbox_xmax)")
    db.execute("CREATE INDEX idx_bbox_ymin ON divisions_reverse(bbox_ymin)")
    db.execute("CREATE INDEX idx_bbox_ymax ON divisions_reverse(bbox_ymax)")

    # Additional indexes for filtering and lookups
    print("Creating additional indexes...")
    db.execute("CREATE INDEX idx_gers ON divisions_reverse(gers_id)")
    db.execute("CREATE INDEX idx_subtype ON divisions_reverse(subtype)")
    db.execute("CREATE INDEX idx_country ON divisions_reverse(country)")
    db.execute("CREATE INDEX idx_area ON divisions_reverse(area)")
    db.execute("CREATE INDEX idx_h3_cells ON divisions_reverse(h3_cells)")

    # Store metadata
    db.execute("INSERT INTO metadata VALUES ('type', 'divisions-reverse')")
    db.execute("INSERT INTO metadata VALUES ('record_count', ?)", (str(inserted),))

    db.commit()
    db.close()
    con.close()

    # Report file size
    size_mb = output_db.stat().st_size / (1024 * 1024)
    print(f"Done! Index size: {size_mb:.1f} MB")


def _insert_batch(db: sqlite3.Connection, batch: list):
    """Insert a batch of divisions."""
    db.executemany(
        """
        INSERT INTO divisions_reverse (
            gers_id, version, subtype, primary_name, lat, lon,
            population, country, region,
            bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
            area, h3_cells
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        batch,
    )


def test_reverse_query(db_path: Path, lat: float, lon: float, limit: int = 10):
    """Test reverse geocoding query on the built index."""
    print(f"\nTest reverse query: lat={lat}, lon={lon}")
    print("-" * 60)

    db = sqlite3.connect(db_path)

    # Find divisions whose bbox contains the point, sorted by area (smallest first)
    # Hierarchy is built from these overlapping results by sorting by subtype
    results = db.execute(
        """
        SELECT
            subtype,
            primary_name,
            area,
            population,
            country,
            h3_cells
        FROM divisions_reverse
        WHERE bbox_xmin <= ?
          AND bbox_xmax >= ?
          AND bbox_ymin <= ?
          AND bbox_ymax >= ?
        ORDER BY area ASC
        LIMIT ?
        """,
        (lon, lon, lat, lat, limit),
    ).fetchall()

    if not results:
        print("  No results found")
    else:
        for r in results:
            pop = f", pop={r[3]:,}" if r[3] else ""
            area = f", area={r[2]:.4f}" if r[2] else ""
            h3 = f", h3_cells={len(r[5].split(',')) if r[5] else 0}" if r[5] else ""
            print(f"  [{r[0]:12}] {r[1]} ({r[4]}){pop}{area}{h3}")

    db.close()
    return results


if __name__ == "__main__":
    build_divisions_reverse_index()

    # Run test queries
    db_path = Path("indexes/divisions-reverse.db")
    if db_path.exists():
        # Boston, MA
        test_reverse_query(db_path, lat=42.3601, lon=-71.0589)
        # London, UK
        test_reverse_query(db_path, lat=51.5074, lon=-0.1278)
        # Tokyo, Japan
        test_reverse_query(db_path, lat=35.6762, lon=139.6503)
