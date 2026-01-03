#!/usr/bin/env python3
"""
Build country-partitioned SQLite shards for R2 storage.

This script orchestrates the full shard build pipeline:
1. Downloads divisions data from Overture (if needed)
2. Builds per-country SQLite shards with FTS5
3. Builds HEAD shard (countries, regions, large cities)
4. Generates STAC catalog for shard discovery

Usage:
    python scripts/build_shards.py [--version VERSION] [--head-threshold 100000]
    python scripts/build_shards.py --countries US,CA,GB  # Build specific countries only
    python scripts/build_shards.py --head-only           # Build HEAD shard only

Output:
    shards/{version}/
        shards/HEAD.db
        shards/US.db
        shards/CA.db
        ...
        items/HEAD.json
        items/US.json
        ...
        collection.json
    shards/catalog.json
"""

import argparse
import json
import hashlib
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

# Validation patterns
COUNTRY_CODE_PATTERN = re.compile(r'^[A-Z]{2}$')

def validate_country_code(code: str) -> str:
    """Validate and return a country code, or raise ValueError."""
    if not COUNTRY_CODE_PATTERN.match(code):
        raise ValueError(f"Invalid country code: {code!r} (must be 2 uppercase letters)")
    return code

def validate_population_threshold(threshold: int) -> int:
    """Validate population threshold is a reasonable positive integer."""
    if not isinstance(threshold, int) or threshold < 0 or threshold > 10_000_000_000:
        raise ValueError(f"Invalid population threshold: {threshold}")
    return threshold

# Default paths
EXPORTS_DIR = Path("exports")
SHARDS_DIR = Path("shards")
DIVISIONS_PARQUET = EXPORTS_DIR / "divisions-global.parquet"

# HEAD shard includes countries, regions, and localities with pop >= threshold
DEFAULT_HEAD_THRESHOLD = 100_000


def get_version() -> str:
    """Get version string (date-based)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".0"


def get_countries(parquet_path: Path) -> list[str]:
    """Get list of unique country codes from parquet file."""
    parquet_str = str(parquet_path.resolve())
    con = duckdb.connect()
    result = con.execute(f"""
        SELECT DISTINCT country
        FROM read_parquet('{parquet_str}')
        WHERE country IS NOT NULL
        ORDER BY country
    """).fetchall()
    con.close()
    return [r[0] for r in result]


def build_shard_schema(db: sqlite3.Connection):
    """Create the shard schema with FTS5."""
    db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        PRAGMA cache_size=-64000;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS divisions (
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

        CREATE VIRTUAL TABLE IF NOT EXISTS divisions_fts USING fts5(
            search_text,
            content=divisions,
            content_rowid=rowid,
            tokenize='porter unicode61 remove_diacritics 1',
            prefix='2 3'
        );

        CREATE TRIGGER IF NOT EXISTS divisions_ai AFTER INSERT ON divisions BEGIN
            INSERT INTO divisions_fts(rowid, search_text)
            VALUES (new.rowid, new.search_text);
        END;

        CREATE TRIGGER IF NOT EXISTS divisions_ad AFTER DELETE ON divisions BEGIN
            INSERT INTO divisions_fts(divisions_fts, rowid, search_text)
            VALUES ('delete', old.rowid, old.search_text);
        END;

        CREATE TRIGGER IF NOT EXISTS divisions_au AFTER UPDATE ON divisions BEGIN
            INSERT INTO divisions_fts(divisions_fts, rowid, search_text)
            VALUES ('delete', old.rowid, old.search_text);
            INSERT INTO divisions_fts(rowid, search_text)
            VALUES (new.rowid, new.search_text);
        END;
    """)


def build_country_shard(
    parquet_path: Path,
    country_code: str,
    output_path: Path,
    version: str,
) -> dict:
    """Build SQLite shard for a single country."""
    # Validate inputs to prevent SQL injection
    country_code = validate_country_code(country_code)
    parquet_str = str(parquet_path.resolve())

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        output_path.unlink()

    con = duckdb.connect()
    db = sqlite3.connect(output_path)

    build_shard_schema(db)

    # Query divisions for this country
    # Note: DuckDB requires file paths in the query string, but country_code is validated above
    cursor = con.execute(f"""
        SELECT
            gers_id,
            version,
            subtype as type,
            primary_name,
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
        FROM read_parquet('{parquet_str}')
        WHERE country = '{country_code}'
    """)

    # Stream rows in chunks to avoid loading entire dataset into memory
    count = 0
    bbox = [180.0, 90.0, -180.0, -90.0]  # [min_lon, min_lat, max_lon, max_lat]
    FETCH_SIZE = 50000

    while True:
        rows = cursor.fetchmany(FETCH_SIZE)
        if not rows:
            break

        # Update bbox from this batch
        for row in rows:
            bbox[0] = min(bbox[0], row[6])  # bbox_xmin
            bbox[1] = min(bbox[1], row[7])  # bbox_ymin
            bbox[2] = max(bbox[2], row[8])  # bbox_xmax
            bbox[3] = max(bbox[3], row[9])  # bbox_ymax

        db.executemany("""
            INSERT INTO divisions (
                gers_id, version, type, primary_name, lat, lon,
                bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                population, country, region, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        count += len(rows)

    # Store metadata
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('version', ?)", (version,))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('country', ?)", (country_code,))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(count),))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('created_at', ?)",
               (datetime.now(timezone.utc).isoformat(),))

    # Optimize FTS
    db.execute("INSERT INTO divisions_fts(divisions_fts) VALUES('optimize')")
    db.commit()
    db.close()
    con.close()

    return {
        "country": country_code,
        "record_count": count,
        "size_bytes": output_path.stat().st_size,
        "bbox": bbox,
    }


def build_head_shard(
    parquet_path: Path,
    output_path: Path,
    version: str,
    population_threshold: int = DEFAULT_HEAD_THRESHOLD,
) -> dict:
    """Build HEAD shard with countries, regions, and large cities."""
    # Validate inputs
    population_threshold = validate_population_threshold(population_threshold)
    parquet_str = str(parquet_path.resolve())

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        output_path.unlink()

    con = duckdb.connect()
    db = sqlite3.connect(output_path)

    build_shard_schema(db)

    # HEAD shard query: countries, regions, and high-population localities
    # We need to query Overture directly for countries/regions since they're
    # not in the divisions-global.parquet (which filters to localities etc.)

    # For now, just include high-population localities from the existing parquet
    # TODO: Add countries and regions from a separate query
    # Note: population_threshold is validated as integer above
    cursor = con.execute(f"""
        SELECT
            gers_id,
            version,
            subtype as type,
            primary_name,
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
        FROM read_parquet('{parquet_str}')
        WHERE population >= {population_threshold}
           OR subtype IN ('county')
    """)

    # Stream rows in chunks to avoid loading entire dataset into memory
    count = 0
    FETCH_SIZE = 50000

    while True:
        rows = cursor.fetchmany(FETCH_SIZE)
        if not rows:
            break

        db.executemany("""
            INSERT INTO divisions (
                gers_id, version, type, primary_name, lat, lon,
                bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                population, country, region, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        count += len(rows)

    # Store metadata
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('version', ?)", (version,))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('type', ?)", ("head",))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('population_threshold', ?)",
               (str(population_threshold),))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('record_count', ?)", (str(count),))
    db.execute("INSERT OR REPLACE INTO metadata VALUES ('created_at', ?)",
               (datetime.now(timezone.utc).isoformat(),))

    # Optimize FTS
    db.execute("INSERT INTO divisions_fts(divisions_fts) VALUES('optimize')")
    db.commit()
    db.close()
    con.close()

    return {
        "country": "HEAD",
        "record_count": count,
        "size_bytes": output_path.stat().st_size,
        "bbox": [-180.0, -90.0, 180.0, 90.0],
    }


def hash_file(path: Path) -> str:
    """Calculate SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha256.update(chunk)
    return sha256.hexdigest()


def generate_stac_item(
    shard_id: str,
    shard_info: dict,
    shard_path: Path,
    version: str,
) -> dict:
    """Generate STAC Item for a shard."""
    bbox = shard_info["bbox"]

    return {
        "type": "Feature",
        "stac_version": "1.1.0",
        "stac_extensions": [],
        "id": shard_id,
        "bbox": bbox,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [bbox[0], bbox[1]],
                [bbox[2], bbox[1]],
                [bbox[2], bbox[3]],
                [bbox[0], bbox[3]],
                [bbox[0], bbox[1]],
            ]]
        },
        "properties": {
            "datetime": datetime.now(timezone.utc).isoformat(),
            "record_count": shard_info["record_count"],
            "size_bytes": shard_info["size_bytes"],
            "sha256": hash_file(shard_path),
        },
        "assets": {
            "data": {
                "href": f"./shards/{shard_id}.db",
                "type": "application/x-sqlite3",
                "roles": ["data"],
                "title": f"{shard_id} geocoding shard",
            }
        },
        "links": [
            {"rel": "root", "href": "../../catalog.json", "type": "application/json"},
            {"rel": "parent", "href": "../collection.json", "type": "application/json"},
            {"rel": "collection", "href": "../collection.json", "type": "application/json"},
        ]
    }


def generate_stac_collection(
    version: str,
    shard_infos: dict[str, dict],
) -> dict:
    """Generate STAC Collection for all shards."""
    # Calculate overall bbox and temporal extent
    overall_bbox = [-180.0, -90.0, 180.0, 90.0]
    now = datetime.now(timezone.utc).isoformat()

    item_links = [
        {
            "rel": "item",
            "href": f"./items/{shard_id}.json",
            "type": "application/geo+json",
            "title": f"{shard_id} shard",
        }
        for shard_id in sorted(shard_infos.keys())
    ]

    return {
        "type": "Collection",
        "stac_version": "1.1.0",
        "stac_extensions": [],
        "id": f"geocoder-shards-{version}",
        "title": f"Overture Geocoder Shards {version}",
        "description": "Pre-built SQLite FTS5 shards for geocoding Overture Maps divisions data",
        "license": "CDLA-Permissive-2.0",
        "extent": {
            "spatial": {"bbox": [overall_bbox]},
            "temporal": {"interval": [[now, None]]},
        },
        "summaries": {
            "shard_count": len(shard_infos),
            "total_records": sum(s["record_count"] for s in shard_infos.values()),
            "total_size_bytes": sum(s["size_bytes"] for s in shard_infos.values()),
        },
        "links": [
            {"rel": "root", "href": "../catalog.json", "type": "application/json"},
            {"rel": "parent", "href": "../catalog.json", "type": "application/json"},
            {"rel": "self", "href": "./collection.json", "type": "application/json"},
            *item_links,
        ],
    }


def generate_stac_catalog(versions: list[str], latest: str) -> dict:
    """Generate root STAC Catalog."""
    child_links = [
        {
            "rel": "child",
            "href": f"./{v}/collection.json",
            "type": "application/json",
            "title": f"Geocoder shards {v}",
            **({"latest": True} if v == latest else {}),
        }
        for v in sorted(versions, reverse=True)
    ]

    return {
        "type": "Catalog",
        "stac_version": "1.1.0",
        "id": "geocoder-shards",
        "title": "Overture Geocoder Shards",
        "description": "STAC catalog for Overture geocoder SQLite shards",
        "links": [
            {"rel": "root", "href": "./catalog.json", "type": "application/json"},
            {"rel": "self", "href": "./catalog.json", "type": "application/json"},
            *child_links,
        ],
    }


def write_json(path: Path, data: dict):
    """Write JSON file with pretty formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Build geocoder shards")
    parser.add_argument("--version", help="Version string (default: date-based)")
    parser.add_argument("--head-threshold", type=int, default=DEFAULT_HEAD_THRESHOLD,
                        help=f"Population threshold for HEAD shard (default: {DEFAULT_HEAD_THRESHOLD})")
    parser.add_argument("--countries", help="Comma-separated list of countries to build")
    parser.add_argument("--head-only", action="store_true", help="Build HEAD shard only")
    parser.add_argument("--skip-head", action="store_true", help="Skip HEAD shard")
    parser.add_argument("--parquet", type=Path, default=DIVISIONS_PARQUET,
                        help=f"Input parquet file (default: {DIVISIONS_PARQUET})")
    args = parser.parse_args()

    if not args.parquet.exists():
        print(f"Error: {args.parquet} not found")
        print("Run: ./scripts/download_divisions.sh")
        sys.exit(1)

    version = args.version or get_version()
    version_dir = SHARDS_DIR / version
    shards_subdir = version_dir / "shards"
    items_subdir = version_dir / "items"

    print(f"Building shards for version {version}")
    print(f"Output directory: {version_dir}")
    print(f"HEAD threshold: {args.head_threshold:,}")
    print()

    shard_infos = {}

    # Build HEAD shard
    if not args.skip_head:
        print("Building HEAD shard...")
        head_path = shards_subdir / "HEAD.db"
        head_info = build_head_shard(
            args.parquet, head_path, version, args.head_threshold
        )
        shard_infos["HEAD"] = head_info
        print(f"  HEAD: {head_info['record_count']:,} records, "
              f"{head_info['size_bytes'] / 1024 / 1024:.1f} MB")

    if args.head_only:
        print("\nHead-only mode, skipping country shards")
    else:
        # Get list of countries
        if args.countries:
            countries = [c.strip().upper() for c in args.countries.split(",")]
        else:
            print("\nDiscovering countries...")
            countries = get_countries(args.parquet)
            print(f"Found {len(countries)} countries")

        # Build country shards (sequential for simplicity - parallel has pickle issues)
        print(f"\nBuilding {len(countries)} country shards...")

        for i, country in enumerate(countries, 1):
            output_path = shards_subdir / f"{country}.db"
            info = build_country_shard(args.parquet, country, output_path, version)
            shard_infos[country] = info
            pct = 100 * i / len(countries)
            print(f"  [{i}/{len(countries)} {pct:.0f}%] {country}: "
                  f"{info['record_count']:,} records, "
                  f"{info['size_bytes'] / 1024 / 1024:.1f} MB")

    # Generate STAC items
    print("\nGenerating STAC catalog...")
    for shard_id, info in shard_infos.items():
        shard_path = shards_subdir / f"{shard_id}.db"
        item = generate_stac_item(shard_id, info, shard_path, version)
        write_json(items_subdir / f"{shard_id}.json", item)

    # Generate collection
    collection = generate_stac_collection(version, shard_infos)
    write_json(version_dir / "collection.json", collection)

    # Update root catalog
    existing_versions = [version]
    catalog_path = SHARDS_DIR / "catalog.json"
    if catalog_path.exists():
        with open(catalog_path) as f:
            old_catalog = json.load(f)
            for link in old_catalog.get("links", []):
                if link.get("rel") == "child":
                    v = link["href"].split("/")[1]
                    if v not in existing_versions:
                        existing_versions.append(v)

    catalog = generate_stac_catalog(existing_versions, version)
    write_json(catalog_path, catalog)

    # Summary
    total_records = sum(s["record_count"] for s in shard_infos.values())
    total_size = sum(s["size_bytes"] for s in shard_infos.values())

    print(f"\nDone!")
    print(f"  Shards: {len(shard_infos)}")
    print(f"  Total records: {total_records:,}")
    print(f"  Total size: {total_size / 1024 / 1024:.1f} MB")
    print(f"\nOutput:")
    print(f"  {catalog_path}")
    print(f"  {version_dir}/collection.json")
    print(f"  {version_dir}/items/*.json")
    print(f"  {version_dir}/shards/*.db")


if __name__ == "__main__":
    main()
