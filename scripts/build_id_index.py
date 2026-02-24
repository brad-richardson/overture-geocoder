#!/usr/bin/env python3
"""
Build GERS ID -> bbox index as UUID-prefix-sharded SQLite databases.

Performance-optimized pipeline:
1. Download registry parquet files locally (aws s3 cp, ~10-50x faster than httpfs)
2. Use DuckDB to compute hex prefixes and export partitioned parquet
3. Build SQLite shards in parallel from partitioned parquet

Usage:
    python scripts/build_id_index.py                          # Build all registry types
    python scripts/build_id_index.py --types division          # Single type
    python scripts/build_id_index.py --dry-run                 # Count records only
    python scripts/build_id_index.py --prefix-len 3            # 4096 shards (default)
    python scripts/build_id_index.py --skip-download           # Reuse previously downloaded registry

Output:
    shards/{version}/
        id-index/{prefix}.db   (e.g., 000.db, 001.db, ..., fff.db)
        id-collection.json
"""

import argparse
import hashlib
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

# Add scripts directory to path for stac module
sys.path.insert(0, str(Path(__file__).parent))
from stac import get_latest_release

# Registry types: all in s3://overturemaps-us-west-2/registry/ (flat, not partitioned)
# The 'path' column encodes type: "theme=buildings/type=building/part-..."
REGISTRY_TYPES = [
    "building",
    "connector",
    "division",
    "division_area",
    "division_boundary",
    "place",
    "segment",
]

# Non-registry types: queried from release buckets (stubbed for now)
# Maps type_name -> (theme, type) for S3 path construction
RELEASE_TYPES = {
    "address": ("addresses", "address"),
    "infrastructure": ("base", "infrastructure"),
    "land": ("base", "land"),
    "water": ("base", "water"),
    "land_cover": ("base", "land_cover"),
    "land_use": ("base", "land_use"),
    "bathymetry": ("base", "bathymetry"),
}

REGISTRY_S3_URL = "s3://overturemaps-us-west-2/registry/"
EXPORTS_DIR = Path("exports")
REGISTRY_LOCAL_DIR = EXPORTS_DIR / "registry"
PARTITIONED_DIR = EXPORTS_DIR / "id-partitioned"
SHARDS_DIR = Path("shards")


def get_version(suffix: str = "0") -> str:
    """Get version string (date-based with suffix)."""
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{date}.{suffix}"


def hash_file(path: Path) -> str:
    """Calculate SHA256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def write_json(path: Path, data: dict):
    """Write JSON file with pretty formatting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def download_registry(skip_download: bool = False):
    """Download registry parquet files from S3 to local disk."""
    if skip_download and REGISTRY_LOCAL_DIR.exists():
        parquet_files = list(REGISTRY_LOCAL_DIR.glob("*.parquet"))
        if parquet_files:
            print(f"  Skipping download, using {len(parquet_files)} existing files in {REGISTRY_LOCAL_DIR}")
            return

    REGISTRY_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  Downloading registry from {REGISTRY_S3_URL} ...")
    t0 = time.time()

    result = subprocess.run(
        [
            "aws", "s3", "cp",
            "--no-sign-request",
            "--recursive",
            REGISTRY_S3_URL,
            str(REGISTRY_LOCAL_DIR),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"  ERROR downloading registry: {result.stderr}")
        sys.exit(1)

    elapsed = time.time() - t0
    parquet_files = list(REGISTRY_LOCAL_DIR.glob("*.parquet"))
    total_size = sum(f.stat().st_size for f in parquet_files)
    print(f"  Downloaded {len(parquet_files)} files ({total_size / 1024 / 1024 / 1024:.1f} GB) in {elapsed:.0f}s")


def dry_run(types: list[str], prefix_len: int, skip_download: bool):
    """Count records per type without building."""
    registry_types = [t for t in types if t in REGISTRY_TYPES]
    release_types = [t for t in types if t in RELEASE_TYPES]

    print(f"Dry run: counting records per type")
    print()

    total = 0

    if registry_types:
        # Download registry for fast local scan
        download_registry(skip_download)

        con = duckdb.connect()
        con.execute("SET memory_limit = '8GB';")
        local_glob = str(REGISTRY_LOCAL_DIR / "*.parquet")

        print("  Registry types (local scan)...", flush=True)
        t0 = time.time()

        type_likes = " OR ".join(
            f"path LIKE '%/type={t}/%'" for t in registry_types
        )
        result = con.execute(f"""
            SELECT
                regexp_extract(path, 'type=([^/]+)', 1) as feature_type,
                COUNT(*) as cnt
            FROM read_parquet('{local_glob}')
            WHERE ({type_likes})
            GROUP BY feature_type
            ORDER BY cnt DESC
        """).fetchall()

        elapsed = time.time() - t0
        for row in result:
            print(f"    {row[0]}: {row[1]:,} records")
            total += row[1]
        print(f"    (scanned in {elapsed:.1f}s)")

        # Estimate shard sizes
        shard_count = 16 ** prefix_len
        avg_per_shard = total // shard_count if total else 0
        # Rough estimate: ~30 bytes per row in SQLite (id=36 + type~10 + 4*4 floats + overhead)
        est_shard_mb = avg_per_shard * 70 / 1024 / 1024
        print(f"\n  Estimated sharding ({prefix_len} hex chars = {shard_count} shards):")
        print(f"    ~{avg_per_shard:,} records/shard")
        print(f"    ~{est_shard_mb:.1f} MB/shard (rough estimate)")
        con.close()

    if release_types:
        print(f"\n  Release types: {', '.join(release_types)} (not yet implemented)")

    print(f"\n  Total: {total:,} records")


def partition_registry(types: list[str], prefix_len: int):
    """Use DuckDB to scan local registry and export prefix-partitioned parquet."""
    print("  Partitioning registry by ID prefix...")
    t0 = time.time()

    # Clean previous partitioned output
    if PARTITIONED_DIR.exists():
        import shutil
        shutil.rmtree(PARTITIONED_DIR)
    PARTITIONED_DIR.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute("SET memory_limit = '8GB';")
    con.execute("SET threads TO 4;")
    local_glob = str(REGISTRY_LOCAL_DIR / "*.parquet")

    # Build type filter using LIKE (faster than split_part)
    type_likes = " OR ".join(f"path LIKE '%/type={t}/%'" for t in types)

    # Compute prefix and export partitioned by prefix
    # This lets DuckDB do the heavy lifting - grouping billions of rows by prefix
    con.execute(f"""
        COPY (
            SELECT
                id,
                regexp_extract(path, 'type=([^/]+)', 1) as type,
                bbox.xmin as bbox_xmin,
                bbox.ymin as bbox_ymin,
                bbox.xmax as bbox_xmax,
                bbox.ymax as bbox_ymax,
                lower(left(replace(id, '-', ''), {prefix_len})) as prefix
            FROM read_parquet('{local_glob}')
            WHERE ({type_likes})
              AND id IS NOT NULL
              AND bbox IS NOT NULL
              AND bbox.xmin IS NOT NULL
        ) TO '{PARTITIONED_DIR}' (
            FORMAT PARQUET,
            PARTITION_BY (prefix),
            OVERWRITE_OR_IGNORE
        );
    """)
    con.close()

    elapsed = time.time() - t0
    # Count partitions created
    prefix_dirs = [d for d in PARTITIONED_DIR.iterdir() if d.is_dir()]
    print(f"  Partitioned into {len(prefix_dirs)} prefix groups in {elapsed:.1f}s")
    return len(prefix_dirs)


def build_single_shard(args_tuple):
    """Build a single SQLite shard from its partitioned parquet. (multiprocessing worker)"""
    prefix, parquet_dir, output_path = args_tuple

    # Read the partitioned parquet for this prefix
    parquet_glob = str(parquet_dir / "*.parquet")

    try:
        con = duckdb.connect()
        rows = con.execute(f"""
            SELECT id, type, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax
            FROM read_parquet('{parquet_glob}')
        """).fetchall()
        con.close()
    except Exception as e:
        return (prefix, 0, 0, str(e))

    if not rows:
        return (prefix, 0, 0, None)

    # Create SQLite shard
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    db = sqlite3.connect(str(output_path))
    db.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA cache_size=-64000;
        PRAGMA locking_mode=EXCLUSIVE;

        CREATE TABLE ids (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            bbox_xmin REAL NOT NULL,
            bbox_ymin REAL NOT NULL,
            bbox_xmax REAL NOT NULL,
            bbox_ymax REAL NOT NULL
        ) WITHOUT ROWID;
    """)

    # Bulk insert all rows
    db.executemany(
        "INSERT OR IGNORE INTO ids VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    db.commit()
    db.execute("VACUUM")
    db.close()

    size = output_path.stat().st_size
    return (prefix, len(rows), size, None)


def build_id_index(args):
    """Main build pipeline."""
    # Discover release version
    if args.version:
        version = args.version
    else:
        version = get_version(args.version_suffix)

    print(f"Discovering latest Overture release...")
    release_version = get_latest_release()
    print(f"  Release: {release_version}")

    # Determine types to index
    if args.types:
        types = [t.strip() for t in args.types.split(",")]
        for t in types:
            if t not in REGISTRY_TYPES and t not in RELEASE_TYPES:
                print(f"Error: Unknown type '{t}'.")
                print(f"  Registry: {', '.join(REGISTRY_TYPES)}")
                print(f"  Release: {', '.join(RELEASE_TYPES.keys())}")
                sys.exit(1)
    else:
        types = REGISTRY_TYPES

    # Separate registry vs release types
    registry_types = [t for t in types if t in REGISTRY_TYPES]
    release_types = [t for t in types if t in RELEASE_TYPES]

    shard_count = 16 ** args.prefix_len
    print(f"  Types: {', '.join(types)}")
    print(f"  Prefix length: {args.prefix_len} ({shard_count} shards)")
    print(f"  Version: {version}")
    print(f"  Workers: {args.workers}")
    print()

    # Dry run mode
    if args.dry_run:
        dry_run(types, args.prefix_len, args.skip_download)
        return

    if release_types:
        print(f"  NOTE: Release types ({', '.join(release_types)}) not yet implemented, skipping.")
        print()
        if not registry_types:
            print("No registry types to build. Exiting.")
            return

    # === Phase 1: Download registry locally ===
    print("Phase 1: Download registry")
    download_registry(args.skip_download)

    # === Phase 2: Partition by ID prefix using DuckDB ===
    print("\nPhase 2: Partition by ID prefix")
    t_total = time.time()
    partition_registry(registry_types, args.prefix_len)

    # === Phase 3: Build SQLite shards in parallel ===
    print(f"\nPhase 3: Build SQLite shards ({args.workers} workers)")
    output_dir = SHARDS_DIR / version / "id-index"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Collect all prefix directories that have data
    work_items = []
    for prefix_dir in sorted(PARTITIONED_DIR.iterdir()):
        if not prefix_dir.is_dir():
            continue
        prefix = prefix_dir.name.split("=", 1)[-1]  # "prefix=0a" -> "0a"
        db_path = output_dir / f"{prefix}.db"
        work_items.append((prefix, prefix_dir, db_path))

    print(f"  Building {len(work_items)} shards...")
    t0 = time.time()

    # Build shards in parallel
    with multiprocessing.Pool(args.workers) as pool:
        results = []
        for i, result in enumerate(pool.imap_unordered(build_single_shard, work_items)):
            results.append(result)
            if (i + 1) % 100 == 0 or (i + 1) == len(work_items):
                print(f"    {i + 1}/{len(work_items)} shards built...", end="\r", flush=True)

    print()
    elapsed_build = time.time() - t0

    # Process results
    shard_infos = {}
    total_records = 0
    errors = []
    for prefix, count, size, error in results:
        if error:
            errors.append((prefix, error))
            continue
        if count == 0:
            # Remove empty shard if it exists
            db_path = output_dir / f"{prefix}.db"
            if db_path.exists():
                db_path.unlink()
            continue
        shard_infos[prefix] = {
            "record_count": count,
            "size_bytes": size,
            "href": f"./id-index/{prefix}.db",
        }
        total_records += count

    if errors:
        print(f"  WARNING: {len(errors)} shards had errors:")
        for prefix, err in errors[:5]:
            print(f"    {prefix}: {err}")

    print(f"  Built {len(shard_infos)} shards with {total_records:,} records in {elapsed_build:.1f}s")

    # === Phase 4: Calculate hashes ===
    print("\nPhase 4: Calculate hashes")
    t0 = time.time()
    for prefix in shard_infos:
        path = output_dir / f"{prefix}.db"
        shard_infos[prefix]["sha256"] = hash_file(path)
    print(f"  Hashed {len(shard_infos)} shards in {time.time() - t0:.1f}s")

    # === Phase 5: Generate id-collection.json ===
    print("\nPhase 5: Generate id-collection.json")
    version_dir = SHARDS_DIR / version
    now = datetime.now(timezone.utc).isoformat()

    # Count records per type across all shards
    type_counts = {}
    for prefix in shard_infos:
        path = output_dir / f"{prefix}.db"
        db = sqlite3.connect(str(path))
        for row in db.execute("SELECT type, COUNT(*) FROM ids GROUP BY type").fetchall():
            type_counts[row[0]] = type_counts.get(row[0], 0) + row[1]
        db.close()

    collection = {
        "type": "Collection",
        "stac_version": "1.1.0",
        "id": f"geocoder-id-index-{version}",
        "title": f"Overture GERS ID Index {version}",
        "description": "UUID-prefix-sharded SQLite index mapping GERS IDs to bounding boxes",
        "license": "CDLA-Permissive-2.0",
        "extent": {
            "spatial": {"bbox": [[-180.0, -90.0, 180.0, 90.0]]},
            "temporal": {"interval": [[now, None]]},
        },
        "summaries": {
            "shard_count": len(shard_infos),
            "total_records": total_records,
            "total_size_bytes": sum(s["size_bytes"] for s in shard_infos.values()),
            "prefix_len": args.prefix_len,
            "overture_release": release_version,
            "type_counts": type_counts,
        },
        "items": {
            prefix: {
                "record_count": info["record_count"],
                "size_bytes": info["size_bytes"],
                "sha256": info["sha256"],
                "href": info["href"],
            }
            for prefix, info in sorted(shard_infos.items())
        },
        "links": [
            {"rel": "root", "href": "../catalog.json", "type": "application/json"},
            {"rel": "parent", "href": "../catalog.json", "type": "application/json"},
            {"rel": "self", "href": "./id-collection.json", "type": "application/json"},
        ],
    }
    write_json(version_dir / "id-collection.json", collection)

    # === Summary ===
    elapsed_total = time.time() - t_total
    total_size = sum(s["size_bytes"] for s in shard_infos.values())
    max_shard = max(s["size_bytes"] for s in shard_infos.values()) if shard_infos else 0
    min_shard = min(s["size_bytes"] for s in shard_infos.values()) if shard_infos else 0

    print(f"\nDone! ({elapsed_total:.0f}s total)")
    print(f"  Shards: {len(shard_infos)} (of {shard_count} possible)")
    print(f"  Total records: {total_records:,}")
    print(f"  Total size: {total_size / 1024 / 1024:.1f} MB")
    print(f"  Shard sizes: {min_shard / 1024 / 1024:.1f} MB - {max_shard / 1024 / 1024:.1f} MB")
    print(f"  Type breakdown:")
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"    {t}: {c:,}")
    print(f"\nOutput:")
    print(f"  {version_dir}/id-collection.json")
    print(f"  {output_dir}/*.db")

    if max_shard > 128 * 1024 * 1024:
        print(f"\n  WARNING: Max shard size ({max_shard / 1024 / 1024:.1f} MB) exceeds 128MB worker limit!")
        print(f"  Consider rebuilding with --prefix-len {args.prefix_len + 1}")


def main():
    # Default workers: use most CPUs but leave some headroom
    default_workers = max(1, (os.cpu_count() or 4) - 1)

    parser = argparse.ArgumentParser(description="Build GERS ID -> bbox index")
    parser.add_argument("--version", help="Version string (default: date-based)")
    parser.add_argument("--version-suffix", default="0",
                        help="Version suffix (default: 0)")
    parser.add_argument("--types", help="Comma-separated types to index (default: all registry)")
    parser.add_argument("--prefix-len", type=int, default=3,
                        help="Hex prefix length for sharding (default: 3 = 4096 shards)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Count records per type without building")
    parser.add_argument("--skip-download", action="store_true",
                        help="Skip registry download (reuse existing local files)")
    parser.add_argument("--workers", type=int, default=default_workers,
                        help=f"Parallel workers for shard building (default: {default_workers})")
    args = parser.parse_args()

    build_id_index(args)


if __name__ == "__main__":
    main()
