#!/usr/bin/env python3
"""
Create a small test fixture database from the full MA index.

Usage:
    python scripts/create_test_fixture.py

Output:
    tests/fixtures/test.db - Small SQLite database for E2E tests
"""

import sqlite3
from pathlib import Path


def create_test_fixture():
    """Extract a subset of records for testing."""

    source_db = Path("indexes/US-MA.db")
    output_dir = Path("tests/fixtures")
    output_db = output_dir / "test.db"

    if not source_db.exists():
        print(f"Error: Source database not found: {source_db}")
        print("Run the indexing pipeline first to create indexes/US-MA.db")
        return False

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    # Remove existing fixture
    if output_db.exists():
        output_db.unlink()

    # Connect to source
    src = sqlite3.connect(source_db)

    # Create fixture database
    dst = sqlite3.connect(output_db)
    dst.execute("PRAGMA journal_mode=WAL")

    # Create schema (same as source)
    dst.execute("""
        CREATE TABLE features (
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
            city TEXT,
            state TEXT,
            postcode TEXT
        )
    """)

    dst.execute("""
        CREATE VIRTUAL TABLE features_fts USING fts5(
            search_text,
            content=features,
            content_rowid=rowid,
            tokenize='porter unicode61 remove_diacritics 1'
        )
    """)

    # Extract specific divisions for testing
    # These are the ones we'll use in ranking tests
    division_queries = [
        "boston",      # Should get Boston city (pop 675K) + neighborhoods
        "cambridge",   # Should get Cambridge city (pop 105K)
        "worcester",   # Should get Worcester County + city
    ]

    divisions = []
    for query in division_queries:
        rows = src.execute("""
            SELECT
                f.gers_id, f.type, f.display_name, f.lat, f.lon,
                f.bbox_xmin, f.bbox_ymin, f.bbox_xmax, f.bbox_ymax,
                f.population, f.city, f.state, f.postcode,
                LOWER(f.display_name) as search_text
            FROM features f
            JOIN features_fts ON features_fts.rowid = f.rowid
            WHERE features_fts MATCH ?
              AND f.type != 'address'
            LIMIT 10
        """, (query,)).fetchall()
        divisions.extend(rows)

    # Deduplicate by gers_id
    seen_ids = set()
    unique_divisions = []
    for row in divisions:
        if row[0] not in seen_ids:
            seen_ids.add(row[0])
            unique_divisions.append(row)

    print(f"Extracted {len(unique_divisions)} unique divisions")

    # Extract some addresses with "123 main" for address search tests
    addresses = src.execute("""
        SELECT
            f.gers_id, f.type, f.display_name, f.lat, f.lon,
            f.bbox_xmin, f.bbox_ymin, f.bbox_xmax, f.bbox_ymax,
            f.population, f.city, f.state, f.postcode,
            LOWER(f.display_name) as search_text
        FROM features f
        JOIN features_fts ON features_fts.rowid = f.rowid
        WHERE features_fts MATCH '"123" "main"'
          AND f.type = 'address'
        LIMIT 20
    """).fetchall()

    print(f"Extracted {len(addresses)} addresses")

    # Combine all records
    all_records = unique_divisions + list(addresses)

    # Insert into fixture
    for row in all_records:
        dst.execute("""
            INSERT INTO features (
                gers_id, type, display_name, lat, lon,
                bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                population, city, state, postcode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, row[:13])

        # Insert FTS entry
        last_id = dst.execute("SELECT last_insert_rowid()").fetchone()[0]
        dst.execute(
            "INSERT INTO features_fts(rowid, search_text) VALUES (?, ?)",
            (last_id, row[13])
        )

    # Create indexes
    dst.execute("CREATE INDEX idx_gers ON features(gers_id)")
    dst.execute("CREATE INDEX idx_type ON features(type)")

    # Optimize FTS
    dst.execute("INSERT INTO features_fts(features_fts) VALUES('optimize')")

    dst.commit()

    # Report
    count = dst.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    dst.close()
    src.close()

    size_kb = output_db.stat().st_size / 1024
    print(f"Created {output_db} with {count} records ({size_kb:.1f} KB)")

    return True


if __name__ == "__main__":
    create_test_fixture()
