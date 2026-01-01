#!/usr/bin/env python3
"""
Build SQLite FTS5 index from global divisions Parquet file.

Usage:
    python scripts/build_divisions_index.py

Input:
    exports/divisions-global.parquet

Output:
    indexes/divisions-global.db
"""

import sqlite3
from pathlib import Path

import duckdb


def build_divisions_index(
    parquet_path: Path = Path("exports/divisions-global.parquet"),
    output_db: Path = Path("indexes/divisions-global.db"),
    batch_size: int = 50000
):
    """Build SQLite FTS5 index from divisions Parquet file."""

    if not parquet_path.exists():
        print(f"Error: {parquet_path} not found")
        print("Run: duckdb < scripts/download_divisions_global.sql")
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

    # Create table for divisions
    db.execute("""
        CREATE TABLE divisions (
            rowid INTEGER PRIMARY KEY,
            gers_id TEXT NOT NULL,
            type TEXT NOT NULL,
            display_name TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            bbox_xmin REAL NOT NULL,
            bbox_ymin REAL NOT NULL,
            bbox_xmax REAL NOT NULL,
            bbox_ymax REAL NOT NULL,
            population INTEGER,
            country TEXT,
            region TEXT
        )
    """)

    # Create FTS5 virtual table
    db.execute("""
        CREATE VIRTUAL TABLE divisions_fts USING fts5(
            search_text,
            content=divisions,
            content_rowid=rowid,
            tokenize='porter unicode61 remove_diacritics 1'
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
            subtype as type,
            display_name,
            lat,
            lon,
            bbox_xmin,
            bbox_ymin,
            bbox_xmax,
            bbox_ymax,
            population,
            country,
            region,
            search_text
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

    # Create indexes
    print("Creating indexes...")
    db.execute("CREATE INDEX idx_gers ON divisions(gers_id)")
    db.execute("CREATE INDEX idx_type ON divisions(type)")
    db.execute("CREATE INDEX idx_country ON divisions(country)")

    # Optimize FTS index
    print("Optimizing FTS index...")
    db.execute("INSERT INTO divisions_fts(divisions_fts) VALUES('optimize')")

    db.commit()
    db.close()
    con.close()

    # Report file size
    size_mb = output_db.stat().st_size / (1024 * 1024)
    print(f"Done! Index size: {size_mb:.1f} MB")


def _insert_batch(db: sqlite3.Connection, batch: list):
    """Insert a batch of divisions and their FTS entries."""
    # Insert into main table
    db.executemany(
        """
        INSERT INTO divisions (
            gers_id, type, display_name, lat, lon,
            bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
            population, country, region
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11]) for r in batch],
    )

    # Get last rowid range
    last_rowid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    first_rowid = last_rowid - len(batch) + 1

    # Insert into FTS table
    db.executemany(
        "INSERT INTO divisions_fts(rowid, search_text) VALUES (?, ?)",
        [(first_rowid + i, batch[i][12]) for i in range(len(batch))],
    )


def test_search(db_path: Path, query: str, limit: int = 5):
    """Test search on the built index."""
    print(f"\nTest search: '{query}'")
    print("-" * 60)

    db = sqlite3.connect(db_path)

    # Search with boost for divisions based on population
    results = db.execute(
        """
        SELECT
            d.type,
            d.display_name,
            d.population,
            d.country,
            d.lat,
            d.lon,
            CASE
                WHEN d.population IS NOT NULL
                THEN bm25(divisions_fts) - (LOG(d.population + 1) * 2.0)
                ELSE bm25(divisions_fts) - 2.0
            END as boosted_score
        FROM divisions_fts
        JOIN divisions d ON divisions_fts.rowid = d.rowid
        WHERE divisions_fts MATCH ?
        ORDER BY boosted_score
        LIMIT ?
        """,
        (query, limit),
    ).fetchall()

    for r in results:
        pop = f", pop={r[2]:,}" if r[2] else ""
        print(f"  [{r[0]:12}] {r[1]} ({r[3]}){pop}")

    db.close()


if __name__ == "__main__":
    build_divisions_index()

    # Run test searches
    db_path = Path("indexes/divisions-global.db")
    test_search(db_path, "boston")
    test_search(db_path, "london")
    test_search(db_path, "paris")
    test_search(db_path, "tokyo")
    test_search(db_path, "sydney")
