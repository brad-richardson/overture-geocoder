#!/usr/bin/env python3
"""
Build GERS ID -> bbox index as UUID-prefix-sharded SQLite databases.

R2 Staging Pipeline (default):
  Phase 1: DuckDB streams Overture S3 -> R2 staging (partitioned parquet)
  Phase 2: Build SQLite shards from R2 staging, upload to R2 final
  Phase 3: Generate id-collection.json, upload to R2

Local Pipeline (--skip-upload):
  Phase 1: DuckDB partitions to local disk
  Phase 2: Build SQLite shards from local partitions
  Phase 3: Generate id-collection.json locally

Usage:
    python scripts/build_id_index.py                      # Full R2 pipeline
    python scripts/build_id_index.py --types division      # Single type
    python scripts/build_id_index.py --dry-run             # Count records only
    python scripts/build_id_index.py --skip-upload         # Local build only
    python scripts/build_id_index.py --phase partition     # Only Phase 1

Environment (R2 mode):
    R2_ACCESS_KEY_ID      S3 API key for DuckDB R2 access
    R2_SECRET_ACCESS_KEY  S3 API secret for DuckDB R2 access
    CLOUDFLARE_ACCOUNT_ID Account ID (R2 S3 endpoint)
    CLOUDFLARE_API_TOKEN  API token for wrangler uploads
"""

import argparse
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

sys.path.insert(0, str(Path(__file__).parent))
from stac import get_latest_release

# Registry types: flat in s3://overturemaps-us-west-2/registry/
# The 'path' column encodes type: "theme=buildings/type=building/part-..."
REGISTRY_TYPES = [
    "building", "connector", "division", "division_area",
    "division_boundary", "place", "segment",
]

RELEASE_TYPES = {
    "address": ("addresses", "address"),
    "infrastructure": ("base", "infrastructure"),
    "land": ("base", "land"),
    "water": ("base", "water"),
    "land_cover": ("base", "land_cover"),
    "land_use": ("base", "land_use"),
    "bathymetry": ("base", "bathymetry"),
}

REGISTRY_S3 = "s3://overturemaps-us-west-2/registry/"
EXPORTS_DIR = Path("exports")
PARTITIONED_DIR = EXPORTS_DIR / "id-partitioned"
SHARDS_DIR = Path("shards")


def get_version(suffix="0"):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{date}.{suffix}"


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def get_r2_config(args):
    """Get R2 configuration from args/env, or None if unavailable."""
    account_id = (
        args.r2_account_id
        or os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        or os.environ.get("R2_ACCOUNT_ID")
    )
    access_key = args.r2_access_key or os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = args.r2_secret_key or os.environ.get("R2_SECRET_ACCESS_KEY")

    if not all([account_id, access_key, secret_key]):
        return None

    return {
        "account_id": account_id,
        "endpoint": f"{account_id}.r2.cloudflarestorage.com",
        "key_id": access_key,
        "secret": secret_key,
        "bucket": args.r2_bucket,
    }


def make_type_filter(types):
    """Build SQL WHERE clause for registry type filtering."""
    return " OR ".join(f"path LIKE '%/type={t}/%'" for t in types)


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def dry_run(types, prefix_len, release_version):
    registry_types = [t for t in types if t in REGISTRY_TYPES]

    print(f"Dry run: counting records (release {release_version})\n")
    total = 0

    if registry_types:
        con = duckdb.connect()
        con.execute("SET memory_limit = '8GB';")
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute("SET s3_region = 'us-west-2';")

        t0 = time.time()
        result = con.execute(f"""
            SELECT regexp_extract(path, 'type=([^/]+)', 1) as feature_type,
                   COUNT(*) as cnt
            FROM read_parquet('{REGISTRY_S3}*')
            WHERE ({make_type_filter(registry_types)})
              AND last_seen = '{release_version}'
            GROUP BY feature_type ORDER BY cnt DESC
        """).fetchall()

        for row in result:
            print(f"  {row[0]}: {row[1]:,}")
            total += row[1]
        print(f"  (scanned in {time.time() - t0:.1f}s)")

        shard_count = 16 ** prefix_len
        avg = total // shard_count if total else 0
        est_mb = avg * 70 / 1024 / 1024
        print(f"\n  Sharding: {prefix_len} hex = {shard_count} shards")
        print(f"  ~{avg:,} records/shard, ~{est_mb:.1f} MB/shard")
        con.close()

    print(f"\n  Total: {total:,}")


# ---------------------------------------------------------------------------
# Phase 1: Partition
# ---------------------------------------------------------------------------

def _partition_query(types, prefix_len, release_version):
    """Return the SELECT query used for partitioning."""
    return f"""
        SELECT
            id,
            regexp_extract(path, 'type=([^/]+)', 1) as type,
            bbox.xmin as bbox_xmin, bbox.ymin as bbox_ymin,
            bbox.xmax as bbox_xmax, bbox.ymax as bbox_ymax,
            lower(left(replace(id, '-', ''), {prefix_len})) as prefix
        FROM read_parquet('{REGISTRY_S3}*')
        WHERE ({make_type_filter(types)})
          AND last_seen = '{release_version}'
          AND id IS NOT NULL AND bbox IS NOT NULL AND bbox.xmin IS NOT NULL
    """


def phase_partition_r2(types, prefix_len, release_version, r2_config, version):
    """Partition from Overture S3 directly to R2 staging (zero local disk)."""
    print("  Streaming Overture S3 -> R2 staging...")

    con = duckdb.connect()
    con.execute("SET memory_limit = '8GB'; SET threads TO 4;")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region = 'us-west-2';")
    con.execute(f"""
        CREATE SECRET r2 (
            TYPE S3,
            SCOPE 's3://{r2_config["bucket"]}/',
            KEY_ID '{r2_config["key_id"]}',
            SECRET '{r2_config["secret"]}',
            ENDPOINT '{r2_config["endpoint"]}',
            REGION 'auto',
            URL_STYLE 'path'
        );
    """)

    staging = f"s3://{r2_config['bucket']}/{version}/staging/id-partitioned"
    query = _partition_query(types, prefix_len, release_version)

    t0 = time.time()
    con.execute(f"""
        COPY ({query})
        TO '{staging}'
        (FORMAT PARQUET, PARTITION_BY (prefix), COMPRESSION ZSTD, OVERWRITE_OR_IGNORE);
    """)
    con.close()
    print(f"  Done in {time.time() - t0:.0f}s")


def phase_partition_local(types, prefix_len, release_version):
    """Partition from Overture S3 to local disk."""
    import shutil
    if PARTITIONED_DIR.exists():
        shutil.rmtree(PARTITIONED_DIR)
    PARTITIONED_DIR.mkdir(parents=True, exist_ok=True)

    print("  Streaming Overture S3 -> local disk...")

    con = duckdb.connect()
    con.execute("SET memory_limit = '8GB'; SET threads TO 4;")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region = 'us-west-2';")

    query = _partition_query(types, prefix_len, release_version)

    t0 = time.time()
    con.execute(f"""
        COPY ({query})
        TO '{PARTITIONED_DIR}'
        (FORMAT PARQUET, PARTITION_BY (prefix), COMPRESSION ZSTD, OVERWRITE_OR_IGNORE);
    """)
    con.close()

    prefix_dirs = [d for d in PARTITIONED_DIR.iterdir() if d.is_dir()]
    print(f"  {len(prefix_dirs)} prefixes in {time.time() - t0:.0f}s")


# ---------------------------------------------------------------------------
# Phase 2: Build SQLite shards
# ---------------------------------------------------------------------------

def _create_shard_db(db_path, rows):
    """Create a SQLite WITHOUT ROWID shard from rows."""
    db = sqlite3.connect(str(db_path))
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
    db.executemany("INSERT OR IGNORE INTO ids VALUES (?, ?, ?, ?, ?, ?)", rows)
    db.commit()
    db.execute("VACUUM")
    db.close()


def _upload_to_r2(local_path, r2_key, retries=3):
    """Upload a file to R2 via wrangler with retries."""
    for attempt in range(retries):
        result = subprocess.run(
            ["wrangler", "r2", "object", "put", r2_key,
             "--file", str(local_path), "--remote"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return None
    return result.stderr[:200]


def _worker_build_r2(args_tuple):
    """Multiprocessing worker: read from R2 staging, build shard, upload."""
    prefix, r2_config, version, tmp_dir = args_tuple

    staging = (
        f"s3://{r2_config['bucket']}/{version}"
        f"/staging/id-partitioned/prefix={prefix}/*.parquet"
    )

    try:
        con = duckdb.connect()
        con.execute("LOAD httpfs;")
        con.execute(f"""
            CREATE SECRET r2 (
                TYPE S3,
                SCOPE 's3://{r2_config["bucket"]}/',
                KEY_ID '{r2_config["key_id"]}',
                SECRET '{r2_config["secret"]}',
                ENDPOINT '{r2_config["endpoint"]}',
                REGION 'auto',
                URL_STYLE 'path'
            );
        """)
        rows = con.execute(f"""
            SELECT id, type, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax
            FROM read_parquet('{staging}')
        """).fetchall()
        con.close()
    except Exception:
        return (prefix, 0, 0, None)

    if not rows:
        return (prefix, 0, 0, None)

    db_path = Path(tmp_dir) / f"{prefix}.db"
    _create_shard_db(db_path, rows)
    size = db_path.stat().st_size

    r2_key = f"geocoder-shards/{version}/id-index/{prefix}.db"
    err = _upload_to_r2(db_path, r2_key)
    db_path.unlink(missing_ok=True)

    if err:
        return (prefix, len(rows), size, f"Upload failed: {err}")
    return (prefix, len(rows), size, None)


def _worker_build_local(args_tuple):
    """Multiprocessing worker: build shard from local partition."""
    prefix, parquet_dir, output_path = args_tuple

    try:
        con = duckdb.connect()
        rows = con.execute(f"""
            SELECT id, type, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax
            FROM read_parquet('{parquet_dir}/*.parquet')
        """).fetchall()
        con.close()
    except Exception as e:
        return (prefix, 0, 0, str(e))

    if not rows:
        return (prefix, 0, 0, None)

    _create_shard_db(Path(output_path), rows)
    return (prefix, len(rows), Path(output_path).stat().st_size, None)


def _run_pool(worker_fn, work_items, total_label, workers):
    """Run multiprocessing pool with progress reporting."""
    total = len(work_items)
    results = []

    with multiprocessing.Pool(workers) as pool:
        for i, result in enumerate(pool.imap_unordered(worker_fn, work_items)):
            results.append(result)
            if (i + 1) % 100 == 0 or (i + 1) == total:
                built = sum(1 for r in results if r[1] > 0)
                print(
                    f"    {i+1}/{total} {total_label}, {built} with data...",
                    end="\r", flush=True,
                )
    print()
    return results


def phase_build_r2(prefix_len, r2_config, version, workers):
    """Build shards from R2 staging, upload to R2."""
    # Pre-install httpfs so workers only need LOAD
    con = duckdb.connect()
    con.execute("INSTALL httpfs;")
    con.close()

    tmp_dir = "tmp-id-shards"
    os.makedirs(tmp_dir, exist_ok=True)

    shard_count = 16 ** prefix_len
    prefixes = [format(i, f'0{prefix_len}x') for i in range(shard_count)]
    work = [(p, r2_config, version, tmp_dir) for p in prefixes]

    print(f"  Processing {shard_count} prefixes ({workers} workers)...")
    results = _run_pool(_worker_build_r2, work, "checked", workers)

    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return results


def phase_build_local(prefix_len, version, workers):
    """Build shards from local partitions."""
    output_dir = SHARDS_DIR / version / "id-index"
    output_dir.mkdir(parents=True, exist_ok=True)

    work = []
    for d in sorted(PARTITIONED_DIR.iterdir()):
        if not d.is_dir():
            continue
        prefix = d.name.split("=", 1)[-1]
        work.append((prefix, str(d), str(output_dir / f"{prefix}.db")))

    print(f"  Building {len(work)} shards ({workers} workers)...")
    return _run_pool(_worker_build_local, work, "built", workers)


# ---------------------------------------------------------------------------
# Phase 3: Metadata
# ---------------------------------------------------------------------------

def phase_metadata(results, prefix_len, version, release_version, r2_config, skip_upload):
    """Generate id-collection.json and optionally upload to R2."""
    shard_infos = {}
    total_records = 0
    errors = []

    for prefix, count, size, err in results:
        if err:
            errors.append((prefix, err))
        elif count > 0:
            shard_infos[prefix] = {"record_count": count, "size_bytes": size}
            total_records += count

    if errors:
        print(f"  {len(errors)} shard errors:")
        for p, e in errors[:5]:
            print(f"    {p}: {e}")

    now = datetime.now(timezone.utc).isoformat()
    collection = {
        "type": "Collection",
        "stac_version": "1.1.0",
        "id": f"geocoder-id-index-{version}",
        "title": f"Overture GERS ID Index {version}",
        "description": "UUID-prefix-sharded SQLite index mapping GERS IDs to bounding boxes",
        "license": "CDLA-Permissive-2.0",
        "extent": {
            "spatial": {"bbox": [[-180, -90, 180, 90]]},
            "temporal": {"interval": [[now, None]]},
        },
        "summaries": {
            "shard_count": len(shard_infos),
            "total_records": total_records,
            "total_size_bytes": sum(s["size_bytes"] for s in shard_infos.values()),
            "prefix_len": prefix_len,
            "overture_release": release_version,
        },
        "items": {
            p: {
                "record_count": s["record_count"],
                "size_bytes": s["size_bytes"],
                "href": f"./id-index/{p}.db",
            }
            for p, s in sorted(shard_infos.items())
        },
        "links": [
            {"rel": "root", "href": "../catalog.json", "type": "application/json"},
            {"rel": "parent", "href": "../catalog.json", "type": "application/json"},
            {"rel": "self", "href": "./id-collection.json", "type": "application/json"},
        ],
    }

    if skip_upload:
        path = SHARDS_DIR / version / "id-collection.json"
        write_json(path, collection)
        print(f"  Wrote {path}")
    else:
        tmp = Path("tmp-id-collection.json")
        write_json(tmp, collection)
        err = _upload_to_r2(tmp, f"geocoder-shards/{version}/id-collection.json")
        tmp.unlink(missing_ok=True)
        if err:
            print(f"  ERROR uploading id-collection.json: {err}")
            sys.exit(1)
        print("  Uploaded id-collection.json to R2")

    return shard_infos, total_records, errors


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def build_id_index(args):
    if args.version:
        version = args.version
    else:
        version = get_version(args.version_suffix)

    print("Discovering latest Overture release...")
    release_version = get_latest_release()
    print(f"  Release: {release_version}")

    # Determine types
    if args.types:
        types = [t.strip() for t in args.types.split(",")]
        for t in types:
            if t not in REGISTRY_TYPES and t not in RELEASE_TYPES:
                print(f"Error: Unknown type '{t}'")
                sys.exit(1)
    else:
        types = REGISTRY_TYPES

    registry_types = [t for t in types if t in REGISTRY_TYPES]
    release_types = [t for t in types if t in RELEASE_TYPES]

    shard_count = 16 ** args.prefix_len
    print(f"  Types: {', '.join(types)}")
    print(f"  Sharding: {args.prefix_len} hex = {shard_count} shards")
    print(f"  Version: {version}")

    if args.dry_run:
        dry_run(types, args.prefix_len, release_version)
        return

    if release_types:
        print(f"  NOTE: Release types not yet implemented: {', '.join(release_types)}")
        if not registry_types:
            return

    # Determine mode
    r2_config = None if args.skip_upload else get_r2_config(args)
    use_r2 = r2_config is not None

    if not use_r2 and not args.skip_upload:
        print("\nWARNING: R2 credentials not found, using local mode")
        print("  Set R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, CLOUDFLARE_ACCOUNT_ID")

    print(f"\nMode: {'R2 staging' if use_r2 else 'local'}")
    print(f"  Workers: {args.workers}")

    phases = args.phase.split(",") if args.phase else ["all"]
    run_all = "all" in phases
    t_total = time.time()

    # === Phase 1: Partition ===
    if run_all or "partition" in phases:
        if args.skip_partition:
            print("\nPhase 1: Skipped (--skip-partition)")
        else:
            print(f"\nPhase 1: Partition registry (release {release_version})")
            if use_r2:
                phase_partition_r2(
                    registry_types, args.prefix_len, release_version,
                    r2_config, version,
                )
            else:
                phase_partition_local(
                    registry_types, args.prefix_len, release_version,
                )

    # === Phase 2: Build shards ===
    results = None
    if run_all or "build" in phases:
        print(f"\nPhase 2: Build SQLite shards")
        t0 = time.time()
        if use_r2:
            results = phase_build_r2(
                args.prefix_len, r2_config, version, args.workers,
            )
        else:
            results = phase_build_local(
                args.prefix_len, version, args.workers,
            )
        elapsed = time.time() - t0

        built = sum(1 for r in results if r[1] > 0)
        records = sum(r[1] for r in results)
        errs = sum(1 for r in results if r[3] is not None)
        print(f"  {built} shards, {records:,} records in {elapsed:.0f}s")
        if errs:
            print(f"  {errs} upload errors")

    # === Phase 3: Metadata ===
    if (run_all or "metadata" in phases) and results:
        print(f"\nPhase 3: Metadata")
        shard_infos, total_records, errors = phase_metadata(
            results, args.prefix_len, version, release_version,
            r2_config, args.skip_upload,
        )

        total_size = sum(s["size_bytes"] for s in shard_infos.values())
        max_shard = max((s["size_bytes"] for s in shard_infos.values()), default=0)

        print(f"\nDone! ({time.time() - t_total:.0f}s)")
        print(f"  Shards: {len(shard_infos)} / {shard_count}")
        print(f"  Records: {total_records:,}")
        print(f"  Total: {total_size / 1024 / 1024:.1f} MB")
        if max_shard:
            print(f"  Max shard: {max_shard / 1024 / 1024:.1f} MB")
        if max_shard > 128 * 1024 * 1024:
            print(f"\n  WARNING: Exceeds 128MB! Use --prefix-len {args.prefix_len + 1}")


def main():
    default_workers = max(1, (os.cpu_count() or 4) - 1)

    p = argparse.ArgumentParser(description="Build GERS ID -> bbox index")
    p.add_argument("--version", help="Version string (default: date-based)")
    p.add_argument("--version-suffix", default="0", help="Version suffix")
    p.add_argument("--types", help="Comma-separated types to index")
    p.add_argument("--prefix-len", type=int, default=3,
                   help="Hex prefix length (default: 3 = 4096 shards)")
    p.add_argument("--dry-run", action="store_true", help="Count records only")
    p.add_argument("--workers", type=int, default=default_workers,
                   help=f"Parallel workers (default: {default_workers})")

    # R2 configuration
    p.add_argument("--r2-account-id", help="Cloudflare account ID")
    p.add_argument("--r2-access-key", help="R2 S3 API access key")
    p.add_argument("--r2-secret-key", help="R2 S3 API secret key")
    p.add_argument("--r2-bucket", default="geocoder-shards",
                   help="R2 bucket name (default: geocoder-shards)")

    # Pipeline control
    p.add_argument("--phase",
                   help="Run specific phase: partition, build, metadata, or all")
    p.add_argument("--skip-partition", action="store_true",
                   help="Skip Phase 1 (reuse existing staging)")
    p.add_argument("--skip-upload", action="store_true",
                   help="Local build only (no R2)")

    args = p.parse_args()
    build_id_index(args)


if __name__ == "__main__":
    main()
