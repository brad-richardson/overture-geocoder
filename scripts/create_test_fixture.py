#!/usr/bin/env python3
"""
Create test fixture databases for E2E tests.

Usage:
    python scripts/create_test_fixture.py

Output:
    tests/fixtures/test.db      - Features database (addresses) for DB_MA
    tests/fixtures/divisions.db - Divisions database for DB_DIVISIONS
"""

import sqlite3
from pathlib import Path


# Test data for divisions (matches what tests expect)
# search_text includes hierarchy to match production data format
DIVISIONS_DATA = [
    # Boston - locality with high population (should rank first for "boston")
    {
        "gers_id": "5df2793f-5a0a-4fcf-bd3c-7edb8cc495d8",
        "type": "locality",
        "primary_name": "Boston, MA",
        "lat": 42.3601,
        "lon": -71.0589,
        "bbox_xmin": -71.1912,
        "bbox_ymin": 42.2279,
        "bbox_xmax": -70.9234,
        "bbox_ymax": 42.3974,
        "population": 675647,
        "country": "US",
        "region": "US-MA",
        "search_text": "boston massachusetts united states ma us suffolk county",
    },
    # Cambridge, MA - locality (for disambiguation with Cambridge UK)
    {
        "gers_id": "e66a9243-9cc5-40a8-a44e-363f9721113f",
        "type": "locality",
        "primary_name": "Cambridge, MA",
        "lat": 42.3736,
        "lon": -71.1097,
        "bbox_xmin": -71.1606,
        "bbox_ymin": 42.3524,
        "bbox_xmax": -71.0636,
        "bbox_ymax": 42.4040,
        "population": 105162,
        "country": "US",
        "region": "US-MA",
        "search_text": "cambridge massachusetts united states ma us middlesex county",
    },
    # Worcester County - higher population than city
    {
        "gers_id": "5fc7bdb6-37b6-4759-94df-e94fef6571fa",
        "type": "county",
        "primary_name": "Worcester County, MA",
        "lat": 42.3500,
        "lon": -71.9000,
        "bbox_xmin": -72.2000,
        "bbox_ymin": 42.0000,
        "bbox_xmax": -71.6000,
        "bbox_ymax": 42.7000,
        "population": 862111,
        "country": "US",
        "region": "US-MA",
        "search_text": "worcester county massachusetts united states ma us",
    },
    # Worcester City - lower population than county
    {
        "gers_id": "a1b2c3d4-5678-90ab-cdef-1234567890ab",
        "type": "locality",
        "primary_name": "Worcester, MA",
        "lat": 42.2626,
        "lon": -71.8023,
        "bbox_xmin": -71.8800,
        "bbox_ymin": 42.2200,
        "bbox_xmax": -71.7300,
        "bbox_ymax": 42.3200,
        "population": 206518,
        "country": "US",
        "region": "US-MA",
        "search_text": "worcester massachusetts united states ma us",
    },
    # Boston neighborhoods (macrohood)
    {
        "gers_id": "b1234567-89ab-cdef-0123-456789abcdef",
        "type": "macrohood",
        "primary_name": "South Boston, MA",
        "lat": 42.3381,
        "lon": -71.0476,
        "bbox_xmin": -71.0700,
        "bbox_ymin": 42.3200,
        "bbox_xmax": -71.0200,
        "bbox_ymax": 42.3550,
        "population": None,
        "country": "US",
        "region": "US-MA",
        "search_text": "south boston southie massachusetts united states ma us",
    },
    {
        "gers_id": "c2345678-90ab-cdef-1234-567890abcdef",
        "type": "macrohood",
        "primary_name": "East Boston, MA",
        "lat": 42.3750,
        "lon": -71.0350,
        "bbox_xmin": -71.0600,
        "bbox_ymin": 42.3550,
        "bbox_xmax": -71.0100,
        "bbox_ymax": 42.3950,
        "population": None,
        "country": "US",
        "region": "US-MA",
        "search_text": "east boston eastie massachusetts united states ma us",
    },
    # === International cities ===
    # Paris, France
    {
        "gers_id": "paris-france-001",
        "type": "locality",
        "primary_name": "Paris",
        "lat": 48.8566,
        "lon": 2.3522,
        "bbox_xmin": 2.2241,
        "bbox_ymin": 48.8156,
        "bbox_xmax": 2.4699,
        "bbox_ymax": 48.9022,
        "population": 2161000,
        "country": "FR",
        "region": None,
        "search_text": "paris france île-de-france fr europe",
    },
    # New York City (with alternate names)
    {
        "gers_id": "nyc-001",
        "type": "locality",
        "primary_name": "New York City, NY",
        "lat": 40.7128,
        "lon": -74.0060,
        "bbox_xmin": -74.2591,
        "bbox_ymin": 40.4774,
        "bbox_xmax": -73.7004,
        "bbox_ymax": 40.9176,
        "population": 8336817,
        "country": "US",
        "region": "US-NY",
        "search_text": "new york city nyc ny new york united states us big apple manhattan",
    },
    # London, UK
    {
        "gers_id": "london-uk-001",
        "type": "locality",
        "primary_name": "London",
        "lat": 51.5074,
        "lon": -0.1278,
        "bbox_xmin": -0.5103,
        "bbox_ymin": 51.2868,
        "bbox_xmax": 0.3340,
        "bbox_ymax": 51.6919,
        "population": 8982000,
        "country": "GB",
        "region": None,
        "search_text": "london england united kingdom uk gb great britain",
    },
    # Tokyo, Japan
    {
        "gers_id": "tokyo-001",
        "type": "locality",
        "primary_name": "Tokyo",
        "lat": 35.6762,
        "lon": 139.6503,
        "bbox_xmin": 138.9428,
        "bbox_ymin": 35.5006,
        "bbox_xmax": 139.9200,
        "bbox_ymax": 35.8984,
        "population": 13960000,
        "country": "JP",
        "region": None,
        "search_text": "tokyo japan jp 東京",
    },
    # Cambridge, UK (for disambiguation with Cambridge MA)
    {
        "gers_id": "cambridge-uk-001",
        "type": "locality",
        "primary_name": "Cambridge",
        "lat": 52.2053,
        "lon": 0.1218,
        "bbox_xmin": 0.0469,
        "bbox_ymin": 52.1551,
        "bbox_xmax": 0.1922,
        "bbox_ymax": 52.2371,
        "population": 145700,
        "country": "GB",
        "region": None,
        "search_text": "cambridge england united kingdom uk gb cambridgeshire",
    },
]

# Test data for addresses
ADDRESSES_DATA = [
    {
        "gers_id": "addr-001",
        "type": "address",
        "primary_name": "123 Main St, Boston, MA 02101",
        "lat": 42.3580,
        "lon": -71.0600,
        "city": "Boston",
        "state": "MA",
        "postcode": "02101",
        "search_text": "123 main st boston 02101",
    },
    {
        "gers_id": "addr-002",
        "type": "address",
        "primary_name": "123 Main St, Cambridge, MA 02139",
        "lat": 42.3700,
        "lon": -71.1050,
        "city": "Cambridge",
        "state": "MA",
        "postcode": "02139",
        "search_text": "123 main st cambridge 02139",
    },
    {
        "gers_id": "addr-003",
        "type": "address",
        "primary_name": "456 Main St, Worcester, MA 01608",
        "lat": 42.2650,
        "lon": -71.8000,
        "city": "Worcester",
        "state": "MA",
        "postcode": "01608",
        "search_text": "456 main st worcester 01608",
    },
    {
        "gers_id": "addr-004",
        "type": "address",
        "primary_name": "123 Main St, Somerville, MA 02143",
        "lat": 42.3900,
        "lon": -71.1000,
        "city": "Somerville",
        "state": "MA",
        "postcode": "02143",
        "search_text": "123 main st somerville 02143",
    },
    {
        "gers_id": "addr-005",
        "type": "address",
        "primary_name": "123 Main St, Brookline, MA 02446",
        "lat": 42.3400,
        "lon": -71.1200,
        "city": "Brookline",
        "state": "MA",
        "postcode": "02446",
        "search_text": "123 main st brookline 02446",
    },
]


def create_divisions_fixture(output_dir: Path) -> bool:
    """Create divisions fixture database."""
    output_db = output_dir / "divisions.db"

    if output_db.exists():
        output_db.unlink()

    db = sqlite3.connect(output_db)
    db.execute("PRAGMA journal_mode=WAL")

    # Create divisions table (same schema as build_divisions_index.py)
    db.execute("""
        CREATE TABLE divisions (
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
            country TEXT,
            region TEXT,
            search_text TEXT NOT NULL
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

    # Create triggers for FTS sync
    db.execute("""
        CREATE TRIGGER divisions_ai AFTER INSERT ON divisions BEGIN
            INSERT INTO divisions_fts(rowid, search_text)
            VALUES (new.rowid, new.search_text);
        END
    """)
    db.execute("""
        CREATE TRIGGER divisions_ad AFTER DELETE ON divisions BEGIN
            INSERT INTO divisions_fts(divisions_fts, rowid, search_text)
            VALUES ('delete', old.rowid, old.search_text);
        END
    """)
    db.execute("""
        CREATE TRIGGER divisions_au AFTER UPDATE ON divisions BEGIN
            INSERT INTO divisions_fts(divisions_fts, rowid, search_text)
            VALUES ('delete', old.rowid, old.search_text);
            INSERT INTO divisions_fts(rowid, search_text)
            VALUES (new.rowid, new.search_text);
        END
    """)

    # Insert divisions
    for div in DIVISIONS_DATA:
        db.execute("""
            INSERT INTO divisions (
                gers_id, type, primary_name, lat, lon,
                bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                population, country, region, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            div["gers_id"], div["type"], div["primary_name"],
            div["lat"], div["lon"],
            div["bbox_xmin"], div["bbox_ymin"], div["bbox_xmax"], div["bbox_ymax"],
            div["population"], div["country"], div["region"], div["search_text"]
        ))

    # Create indexes
    db.execute("CREATE INDEX idx_gers ON divisions(gers_id)")
    db.execute("CREATE INDEX idx_type ON divisions(type)")
    db.execute("CREATE INDEX idx_country ON divisions(country)")

    # Optimize FTS
    db.execute("INSERT INTO divisions_fts(divisions_fts) VALUES('optimize')")

    db.commit()
    count = db.execute("SELECT COUNT(*) FROM divisions").fetchone()[0]
    db.close()

    size_kb = output_db.stat().st_size / 1024
    print(f"Created {output_db} with {count} divisions ({size_kb:.1f} KB)")
    return True


def create_features_fixture(output_dir: Path) -> bool:
    """Create features (addresses) fixture database."""
    output_db = output_dir / "test.db"

    if output_db.exists():
        output_db.unlink()

    db = sqlite3.connect(output_db)
    db.execute("PRAGMA journal_mode=WAL")

    # Create features table (same schema as build_index.py)
    db.execute("""
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
        )
    """)

    # Create FTS5 virtual table
    db.execute("""
        CREATE VIRTUAL TABLE features_fts USING fts5(
            search_text,
            content=features,
            content_rowid=rowid,
            tokenize='porter unicode61 remove_diacritics 1'
        )
    """)

    # Create triggers for FTS sync
    db.execute("""
        CREATE TRIGGER features_ai AFTER INSERT ON features BEGIN
            INSERT INTO features_fts(rowid, search_text)
            VALUES (new.rowid, new.search_text);
        END
    """)
    db.execute("""
        CREATE TRIGGER features_ad AFTER DELETE ON features BEGIN
            INSERT INTO features_fts(features_fts, rowid, search_text)
            VALUES ('delete', old.rowid, old.search_text);
        END
    """)
    db.execute("""
        CREATE TRIGGER features_au AFTER UPDATE ON features BEGIN
            INSERT INTO features_fts(features_fts, rowid, search_text)
            VALUES ('delete', old.rowid, old.search_text);
            INSERT INTO features_fts(rowid, search_text)
            VALUES (new.rowid, new.search_text);
        END
    """)

    # Insert addresses
    for addr in ADDRESSES_DATA:
        # Use address coordinates for bbox (point)
        lat, lon = addr["lat"], addr["lon"]
        db.execute("""
            INSERT INTO features (
                gers_id, type, primary_name, lat, lon,
                bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                population, city, state, postcode, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            addr["gers_id"], addr["type"], addr["primary_name"],
            lat, lon,
            lon, lat, lon, lat,  # Point bbox
            None, addr["city"], addr["state"], addr["postcode"], addr["search_text"]
        ))

    # Create indexes
    db.execute("CREATE INDEX idx_gers ON features(gers_id)")
    db.execute("CREATE INDEX idx_type ON features(type)")

    # Optimize FTS
    db.execute("INSERT INTO features_fts(features_fts) VALUES('optimize')")

    db.commit()
    count = db.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    db.close()

    size_kb = output_db.stat().st_size / 1024
    print(f"Created {output_db} with {count} addresses ({size_kb:.1f} KB)")
    return True


def create_test_fixtures():
    """Create all test fixture databases."""
    output_dir = Path("tests/fixtures")
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Creating test fixtures...")
    create_divisions_fixture(output_dir)
    create_features_fixture(output_dir)
    print("Done!")

    return True


if __name__ == "__main__":
    create_test_fixtures()
