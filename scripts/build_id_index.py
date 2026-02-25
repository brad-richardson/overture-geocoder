#!/usr/bin/env python3
"""
Build GERS ID -> bbox index as UUID-prefix-sharded parquet files.

Pipeline (R2 staging):
  Phase 1: DuckDB streams Overture S3 -> R2 staging (partitioned parquet)
  Phase 2: Merge, sort, and write final snappy parquet shards to R2
  Phase 3: Generate id-collection.json, upload to R2

Indexes ALL GERS IDs with bounding boxes from the Overture registry
and release themes (base). No type or release filtering —
any valid GERS ID resolves to a bbox.

Final parquet format: UUID column (FIXED_LEN_BYTE_ARRAY(16)),
float bbox columns, snappy compression, sorted by ID.

Usage:
    python scripts/build_id_index.py                      # Full pipeline
    python scripts/build_id_index.py --dry-run             # Count records only
    python scripts/build_id_index.py --phase partition     # Only Phase 1
    python scripts/build_id_index.py --phase metadata      # Regenerate metadata from existing shards

Environment:
    R2_ACCESS_KEY_ID      S3 API key for DuckDB R2 access
    R2_SECRET_ACCESS_KEY  S3 API secret for DuckDB R2 access
    CLOUDFLARE_ACCOUNT_ID Account ID (R2 S3 endpoint)
    CLOUDFLARE_API_TOKEN  API token for wrangler uploads
"""

import argparse
import json
import multiprocessing
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
from stac import get_latest_release

# Load .env file if present (for local dev)
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                os.environ.setdefault(_key.strip(), _val.strip())

REGISTRY_S3 = "s3://overturemaps-us-west-2/registry/"
RELEASE_S3 = "s3://overturemaps-us-west-2/release/"
# Release themes with IDs not in the registry
RELEASE_THEMES = ["base"]


def get_version(suffix="0"):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{date}.{suffix}"


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def get_r2_config(args):
    """Get R2 configuration from args/env."""
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


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------

def _human_bytes(n):
    """Format byte count as human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _check_staging_progress(r2_config, staging_path):
    """Check partition files and total bytes written to R2 staging.

    Uses parquet_metadata() to read footers (not data) for file count
    and total compressed size. Falls back to glob() for just file count
    if metadata reading is too slow or fails.
    """
    con = None
    try:
        con = _r2_con(r2_config)
        # Try parquet_metadata for file count + total bytes (reads footers only)
        try:
            result = con.execute(f"""
                SELECT
                    COUNT(DISTINCT file_name) as files,
                    COALESCE(SUM(total_compressed_size), 0) as total_bytes
                FROM parquet_metadata('{staging_path}/prefix=*/*.parquet')
            """).fetchone()
            if result:
                return result[0], result[1]
        except Exception:
            pass
        # Fall back to glob for file count only (single ListObjects call)
        result = con.execute(f"""
            SELECT COUNT(*) FROM glob('{staging_path}/prefix=*/*.parquet')
        """).fetchone()
        return (result[0] if result else 0), None
    except Exception:
        return None, None
    finally:
        if con:
            con.close()


def _retry_transient(fn, retries=3, backoff=30):
    """Wrap fn to retry on transient HTTP errors (502, 503, etc.)."""
    def _wrapped():
        for attempt in range(retries):
            try:
                return fn()
            except duckdb.HTTPException as exc:
                if attempt < retries - 1:
                    wait = backoff * (2 ** attempt)
                    print(f"    Transient HTTP error (attempt {attempt + 1}/{retries}): {exc}")
                    print(f"    Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    raise
    return _wrapped


def _run_with_r2_progress(fn, r2_config, staging_path, total_partitions, label, interval=60):
    """Run fn() while polling R2 staging to report write progress."""
    t0 = time.time()
    stop = threading.Event()

    def _monitor():
        while not stop.wait(interval):
            elapsed = time.time() - t0
            mins, secs = divmod(int(elapsed), 60)
            count, total_bytes = _check_staging_progress(r2_config, staging_path)
            if count is not None:
                parts = [f"{count}/{total_partitions} partitions"]
                if total_bytes is not None:
                    parts.append(_human_bytes(total_bytes))
                print(
                    f"    [{mins:02d}:{secs:02d}] {label}: {', '.join(parts)}",
                    flush=True,
                )
            else:
                print(f"    [{mins:02d}:{secs:02d}] {label}...", flush=True)

    thread = threading.Thread(target=_monitor, daemon=True)
    thread.start()
    try:
        return fn()
    finally:
        stop.set()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def dry_run(prefix_len):
    print("Dry run: counting all registry IDs with bbox\n")

    con = duckdb.connect()
    con.execute("SET memory_limit = '8GB';")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region = 'us-west-2';")

    t0 = time.time()
    result = con.execute(f"""
        SELECT COUNT(*) as cnt
        FROM read_parquet('{REGISTRY_S3}*')
        WHERE id IS NOT NULL AND bbox IS NOT NULL AND bbox.xmin IS NOT NULL
    """).fetchone()
    total = result[0]
    print(f"  IDs with bbox: {total:,}")
    print(f"  (scanned in {time.time() - t0:.1f}s)")

    shard_count = 16 ** prefix_len
    avg = total // shard_count if total else 0
    est_mb = avg * 50 / 1024 / 1024
    print(f"\n  Sharding: {prefix_len} hex = {shard_count} shards")
    print(f"  ~{avg:,} records/shard, ~{est_mb:.1f} MB/shard")
    con.close()

    print(f"\n  Total: {total:,}")


# ---------------------------------------------------------------------------
# Phase 1: Stage + Partition
# ---------------------------------------------------------------------------

def _r2_con(r2_config):
    """Create a DuckDB connection with R2 credentials configured."""
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
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
    return con


def _write_staging_marker(r2_config, version, staging_dir, partition_count):
    """Write a _SUCCESS marker to R2 staging after a completed COPY."""
    marker = {
        "status": "complete",
        "partitions": partition_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    tmp = Path("tmp-staging-marker.json")
    write_json(tmp, marker)
    r2_key = f"geocoder-shards/{version}/staging/{staging_dir}/_SUCCESS"
    err = _upload_to_r2(tmp, r2_key)
    tmp.unlink(missing_ok=True)
    if err:
        print(f"  WARNING: Failed to write staging marker: {err}")


def _read_staging_marker(r2_config, version, staging_dir):
    """Check if a _SUCCESS marker exists for this staging path."""
    try:
        result = subprocess.run(
            ["wrangler", "r2", "object", "get",
             f"geocoder-shards/{version}/staging/{staging_dir}/_SUCCESS",
             "--remote", "--pipe"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception:
        pass
    return None


def _r2_staging_exists(r2_config, version):
    """Check if completed R2 staging data exists for this version."""
    return _read_staging_marker(r2_config, version, "id-partitioned") is not None


def _id_query_for_prefix(prefix, limit=None):
    """Return a SELECT for a single prefix, filtering by id LIKE '{prefix}%'.

    On sorted registry parquet, DuckDB can use row group min/max stats
    to skip irrelevant data (predicate pushdown).
    """
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    return f"""
        SELECT
            id::UUID as id,
            bbox.xmin::FLOAT as bbox_xmin, bbox.ymin::FLOAT as bbox_ymin,
            bbox.xmax::FLOAT as bbox_xmax, bbox.ymax::FLOAT as bbox_ymax
        FROM read_parquet('{REGISTRY_S3}*')
        WHERE id IS NOT NULL AND bbox IS NOT NULL AND bbox.xmin IS NOT NULL
          AND id::VARCHAR LIKE '{prefix}%'
        {limit_clause}
    """


def _get_existing_staging_prefixes(con, staging):
    """Return set of prefixes already written to R2 staging."""
    try:
        rows = con.execute(f"""
            SELECT file_name FROM glob('{staging}/prefix=*/*.parquet')
        """).fetchall()
        prefixes = set()
        for (path,) in rows:
            # Extract prefix value from .../prefix=XXX/data_0.parquet
            for part in path.split("/"):
                if part.startswith("prefix="):
                    prefixes.add(part[len("prefix="):])
        return prefixes
    except Exception:
        return set()


def phase_partition_r2(prefix_len, r2_config, version, prefixes=None):
    """Scan Overture S3 registry per-prefix, writing each partition to R2.

    Iterates over hex prefixes individually, using id LIKE '{prefix}%' to
    leverage predicate pushdown on sorted registry parquet. Each prefix is
    independently retryable, and already-staged prefixes are skipped on resume.
    """
    shard_count = 16 ** prefix_len
    if prefixes is None:
        prefixes = [format(i, f'0{prefix_len}x') for i in range(shard_count)]

    con = _r2_con(r2_config)
    con.execute("SET memory_limit = '10GB';")
    con.execute("SET s3_region = 'us-west-2';")

    staging = f"s3://{r2_config['bucket']}/{version}/staging/id-partitioned"

    # Check which prefixes already exist
    existing = _get_existing_staging_prefixes(con, staging)
    remaining = [p for p in prefixes if p not in existing]
    if existing:
        print(f"  Resuming: {len(existing)} already staged, {len(remaining)} remaining")
    else:
        print(f"  Writing {len(remaining)} prefixes...")

    t0 = time.time()
    wrote = 0
    retries = 3

    for i, prefix in enumerate(remaining):
        dest = f"{staging}/prefix={prefix}/data_0.parquet"
        query = _id_query_for_prefix(prefix)

        for attempt in range(retries):
            try:
                # Check if any records exist for this prefix
                row = con.execute(f"SELECT 1 FROM ({query}) LIMIT 1").fetchone()
                if row is None:
                    break

                con.execute(f"""
                    COPY ({query})
                    TO '{dest}'
                    (FORMAT PARQUET, COMPRESSION ZSTD, OVERWRITE_OR_IGNORE);
                """)
                wrote += 1
                break
            except duckdb.HTTPException as exc:
                if attempt < retries - 1:
                    wait = 30 * (2 ** attempt)
                    print(f"    Transient error on prefix {prefix} "
                          f"(attempt {attempt + 1}/{retries}): {exc}")
                    print(f"    Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    raise

        done = i + 1
        if done % 100 == 0 or done == len(remaining):
            elapsed = time.time() - t0
            mins, secs = divmod(int(elapsed), 60)
            print(f"    {done}/{len(remaining)} prefixes "
                  f"({wrote} with data) [{mins:02d}:{secs:02d}]")

    con.close()
    elapsed = time.time() - t0
    mins, secs = divmod(int(elapsed), 60)
    print(f"  Done: {wrote} prefixes with data in {mins}m{secs:02d}s")

    _write_staging_marker(r2_config, version, "id-partitioned", shard_count)


def _release_id_query(prefix_len, release_version, limit=None):
    """Query for release theme IDs (base) not in the registry."""
    sources = ", ".join(
        f"'{RELEASE_S3}{release_version}/theme={t}/**/*.parquet'"
        for t in RELEASE_THEMES
    )
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    return f"""
        SELECT
            id::UUID as id,
            bbox.xmin::FLOAT as bbox_xmin, bbox.ymin::FLOAT as bbox_ymin,
            bbox.xmax::FLOAT as bbox_xmax, bbox.ymax::FLOAT as bbox_ymax,
            lower(left(replace(id, '-', ''), {prefix_len})) as prefix
        FROM read_parquet([{sources}], union_by_name=true)
        WHERE id IS NOT NULL AND bbox IS NOT NULL AND bbox.xmin IS NOT NULL
        {limit_clause}
    """


def _r2_release_staging_exists(r2_config, version):
    """Check if completed release staging data exists for this version."""
    return _read_staging_marker(r2_config, version, "id-release") is not None


def phase_partition_release_r2(prefix_len, release_version, r2_config, version, limit=None):
    """Scan release themes (base) and partition to R2.

    Writes to a separate staging path from the registry data so both
    can run independently.
    """
    shard_count = 16 ** prefix_len
    limit_msg = f" (limit {limit})" if limit else ""
    print(f"  Scanning release themes ({', '.join(RELEASE_THEMES)}){limit_msg}...")

    con = _r2_con(r2_config)
    con.execute("SET memory_limit = '10GB';")
    con.execute("SET preserve_insertion_order = false;")
    con.execute("SET s3_region = 'us-west-2';")

    staging = f"s3://{r2_config['bucket']}/{version}/staging/id-release"

    t0 = time.time()

    def _do_copy():
        con.execute(f"""
            COPY ({_release_id_query(prefix_len, release_version, limit=limit)})
            TO '{staging}'
            (FORMAT PARQUET, PARTITION_BY (prefix), COMPRESSION ZSTD, OVERWRITE_OR_IGNORE);
        """)

    _run_with_r2_progress(
        _retry_transient(_do_copy), r2_config, staging, shard_count,
        "Release themes scan", interval=60,
    )
    con.close()
    elapsed = time.time() - t0
    mins, secs = divmod(int(elapsed), 60)
    print(f"  Done in {mins}m{secs:02d}s ({elapsed:.0f}s)")

    _write_staging_marker(r2_config, version, "id-release", shard_count)


# ---------------------------------------------------------------------------
# Phase 2: Build parquet shards (sorted, snappy, UUID + float bbox)
# ---------------------------------------------------------------------------


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
    """Multiprocessing worker: read staging parquet, merge/sort, write final snappy parquet."""
    prefix, r2_config, version, tmp_dir = args_tuple

    bucket = r2_config['bucket']
    staging_paths = [
        f"s3://{bucket}/{version}/staging/id-partitioned/prefix={prefix}/*.parquet",
        f"s3://{bucket}/{version}/staging/id-release/prefix={prefix}/*.parquet",
    ]

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

        # Find which staging paths have data
        sources = []
        for path in staging_paths:
            try:
                cnt = con.execute(
                    f"SELECT COUNT(*) FROM read_parquet('{path}')"
                ).fetchone()[0]
                if cnt > 0:
                    sources.append(path)
            except Exception:
                pass

        if not sources:
            con.close()
            return (prefix, 0, 0, None)

        # Build UNION ALL from available sources
        union_parts = [
            f"SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax "
            f"FROM read_parquet('{s}')"
            for s in sources
        ]
        union_query = " UNION ALL ".join(union_parts)

        # Count rows
        count = con.execute(
            f"SELECT COUNT(*) FROM ({union_query})"
        ).fetchone()[0]

        # Sort and write — UUID + FLOAT types already set in Phase 1
        local_path = f"{tmp_dir}/{prefix}.parquet"
        con.execute(f"""
            COPY (
                SELECT * FROM ({union_query}) ORDER BY id
            ) TO '{local_path}'
            (FORMAT PARQUET, COMPRESSION SNAPPY, ROW_GROUP_SIZE 100000);
        """)
        con.close()

        size = os.path.getsize(local_path)

        r2_key = f"geocoder-shards/{version}/id-index/{prefix}.parquet"
        err = _upload_to_r2(local_path, r2_key)
        os.unlink(local_path)

        if err:
            return (prefix, count, size, f"Upload failed: {err}")
        return (prefix, count, size, None)

    except Exception as e:
        return (prefix, 0, 0, str(e))


def _run_pool(worker_fn, work_items, total_label, workers):
    """Run multiprocessing pool with progress reporting."""
    total = len(work_items)
    results = []
    t0 = time.time()

    with multiprocessing.Pool(workers) as pool:
        for i, result in enumerate(pool.imap_unordered(worker_fn, work_items)):
            results.append(result)
            done = i + 1
            if done % 100 == 0 or done == total:
                built = sum(1 for r in results if r[1] > 0)
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                if rate > 0 and done < total and done >= total // 10:
                    remaining = (total - done) / rate
                    mins_r, secs_r = divmod(int(remaining), 60)
                    eta = f", ~{mins_r}m{secs_r:02d}s remaining"
                else:
                    eta = ""
                mins_e, secs_e = divmod(int(elapsed), 60)
                print(
                    f"    {done}/{total} {total_label}, {built} with data"
                    f" ({mins_e}m{secs_e:02d}s elapsed{eta})",
                    flush=True,
                )
    return results


def _discover_staging_prefixes(r2_config, version):
    """List prefixes that have data in either staging path."""
    con = _r2_con(r2_config)
    prefixes = set()
    for staging_dir in ["id-partitioned", "id-release"]:
        try:
            rows = con.execute(f"""
                SELECT DISTINCT prefix
                FROM read_parquet(
                    's3://{r2_config["bucket"]}/{version}/staging/{staging_dir}/*/*'
                )
            """).fetchall()
            prefixes.update(r[0] for r in rows)
        except Exception:
            pass
    con.close()
    return sorted(prefixes)


def phase_build_r2(prefix_len, r2_config, version, workers):
    """Build parquet shards from R2 staging, upload to R2."""
    # Pre-install httpfs so workers only need LOAD
    con = duckdb.connect()
    con.execute("INSTALL httpfs;")
    con.close()

    tmp_dir = "tmp-id-shards"
    os.makedirs(tmp_dir, exist_ok=True)

    shard_count = 16 ** prefix_len
    all_prefixes = [format(i, f'0{prefix_len}x') for i in range(shard_count)]

    # Discover which prefixes actually have staging data to avoid
    # checking all 4096 shards via HTTP when most are empty (e.g. --smoke-test)
    staged = _discover_staging_prefixes(r2_config, version)
    if staged and len(staged) < shard_count:
        print(f"  Found {len(staged)}/{shard_count} prefixes with staging data")
        prefixes = staged
    else:
        prefixes = all_prefixes

    work = [(p, r2_config, version, tmp_dir) for p in prefixes]

    print(f"  Processing {len(prefixes)} prefixes ({workers} workers)...")
    results = _run_pool(_worker_build_r2, work, "checked", workers)

    # Add empty results for skipped prefixes (for metadata accuracy)
    if len(prefixes) < shard_count:
        processed = {r[0] for r in results}
        for p in all_prefixes:
            if p not in processed:
                results.append((p, 0, 0, None))

    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return results


# ---------------------------------------------------------------------------
# Phase 3: Metadata
# ---------------------------------------------------------------------------

def _gather_shard_info_from_r2(prefix_len, r2_config, version):
    """Gather shard info from existing R2 shards (for metadata-only runs)."""
    print("  Scanning existing R2 shards...")
    shard_count = 16 ** prefix_len
    prefixes = [format(i, f'0{prefix_len}x') for i in range(shard_count)]

    con = _r2_con(r2_config)
    results = []
    for i, prefix in enumerate(prefixes):
        path = f"s3://{r2_config['bucket']}/{version}/id-index/{prefix}.parquet"
        try:
            row = con.execute(
                f"SELECT COUNT(*) as cnt, "
                f"SUM(LENGTH(id) + 4*4)::BIGINT as est_size "
                f"FROM read_parquet('{path}')"
            ).fetchone()
            if row and row[0] > 0:
                results.append((prefix, row[0], row[1] or 0, None))
            else:
                results.append((prefix, 0, 0, None))
        except Exception:
            results.append((prefix, 0, 0, None))
        if (i + 1) % 100 == 0 or (i + 1) == shard_count:
            found = sum(1 for r in results if r[1] > 0)
            print(
                f"    {i+1}/{shard_count} scanned, {found} with data...",
                flush=True,
            )
    con.close()
    return results


def phase_metadata(results, prefix_len, version, release_version, r2_config):
    """Generate id-collection.json and upload to R2."""
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
        "description": "UUID-prefix-sharded parquet index mapping GERS IDs to bounding boxes",
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
                "href": f"./id-index/{p}.parquet",
            }
            for p, s in sorted(shard_infos.items())
        },
        "links": [
            {"rel": "root", "href": "../catalog.json", "type": "application/json"},
            {"rel": "parent", "href": "../catalog.json", "type": "application/json"},
            {"rel": "self", "href": "./id-collection.json", "type": "application/json"},
        ],
    }

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

    shard_count = 16 ** args.prefix_len
    print(f"  Sharding: {args.prefix_len} hex = {shard_count} shards")
    print(f"  Version: {version}")

    if args.dry_run:
        dry_run(args.prefix_len)
        return

    r2_config = get_r2_config(args)
    if r2_config is None:
        print("\nERROR: R2 credentials required")
        print("  Set R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, CLOUDFLARE_ACCOUNT_ID")
        sys.exit(1)

    smoke = getattr(args, 'smoke_test', False)
    print(f"\n  Workers: {args.workers}")
    if smoke:
        print(f"  Mode: smoke test")

    # Smoke test: pick ~5 evenly-spaced prefixes for Phase 1a
    smoke_prefixes = None
    smoke_release_limit = None
    if smoke:
        indices = [0, shard_count // 4, shard_count // 2,
                   3 * shard_count // 4, shard_count - 1]
        smoke_prefixes = [format(i, f'0{args.prefix_len}x') for i in indices]
        smoke_release_limit = 50

    phases = args.phase.split(",") if args.phase else ["all"]
    run_all = "all" in phases
    t_total = time.time()
    phase_times = {}

    # === Phase 1: Partition (1a registry + 1b release themes in parallel) ===
    from concurrent.futures import ThreadPoolExecutor, as_completed

    phase1_futures = {}
    if run_all or "partition" in phases or args.release_only:
        t_phase1 = time.time()
        executor = ThreadPoolExecutor(max_workers=2)

        # Phase 1a: Registry
        if not args.release_only:
            if args.skip_partition:
                print("\nPhase 1a: Skipped (--skip-partition)")
            elif _r2_staging_exists(r2_config, version):
                print(f"\nPhase 1a: Skipped (registry staging complete for {version})")
            else:
                print(f"\nPhase 1a: Partition registry")
                phase1_futures["1a"] = executor.submit(
                    phase_partition_r2, args.prefix_len, r2_config, version,
                    prefixes=smoke_prefixes,
                )

        # Phase 1b: Release themes
        if args.skip_partition:
            print("\nPhase 1b: Skipped (--skip-partition)")
        elif _r2_release_staging_exists(r2_config, version):
            print(f"\nPhase 1b: Skipped (release staging complete for {version})")
        else:
            print(f"\nPhase 1b: Partition release themes ({', '.join(RELEASE_THEMES)})")
            phase1_futures["1b"] = executor.submit(
                phase_partition_release_r2,
                args.prefix_len, release_version, r2_config, version,
                limit=smoke_release_limit,
            )

        # Wait for all Phase 1 tasks; fail fast if any phase errors
        first_error = None
        for future in as_completed(phase1_futures.values()):
            try:
                future.result()
            except Exception as exc:
                first_error = exc
                # Cancel any remaining futures
                for f in phase1_futures.values():
                    f.cancel()
                break

        if first_error is not None:
            executor.shutdown(wait=False, cancel_futures=True)
            raise first_error

        executor.shutdown(wait=False)
        phase_times["Phase 1 (partition)"] = time.time() - t_phase1

    if args.release_only:
        elapsed = time.time() - t_total
        mins, secs = divmod(int(elapsed), 60)
        print(f"\nDone! ({mins}m{secs:02d}s)")
        return

    # === Phase 2: Build shards ===
    results = None
    if run_all or "build" in phases:
        print(f"\nPhase 2: Build parquet shards")
        t0 = time.time()
        results = phase_build_r2(
            args.prefix_len, r2_config, version, args.workers,
        )
        elapsed_p2 = time.time() - t0
        phase_times["Phase 2 (build)"] = elapsed_p2

        built = sum(1 for r in results if r[1] > 0)
        records = sum(r[1] for r in results)
        errs = sum(1 for r in results if r[3] is not None)
        mins, secs = divmod(int(elapsed_p2), 60)
        print(f"  {built} shards, {records:,} records in {mins}m{secs:02d}s")
        if errs:
            print(f"  {errs} upload errors")

    # === Phase 3: Metadata ===
    if run_all or "metadata" in phases:
        t_phase3 = time.time()
        # If Phase 2 didn't run, gather shard info from existing R2 data
        if results is None:
            results = _gather_shard_info_from_r2(
                args.prefix_len, r2_config, version,
            )

        print(f"\nPhase 3: Metadata")
        shard_infos, total_records, errors = phase_metadata(
            results, args.prefix_len, version, release_version, r2_config,
        )
        phase_times["Phase 3 (metadata)"] = time.time() - t_phase3

        total_size = sum(s["size_bytes"] for s in shard_infos.values())
        max_shard = max((s["size_bytes"] for s in shard_infos.values()), default=0)

        total_elapsed = time.time() - t_total
        total_mins, total_secs = divmod(int(total_elapsed), 60)
        print(f"\nDone! ({total_mins}m{total_secs:02d}s total)")
        print(f"  Shards: {len(shard_infos)} / {shard_count}")
        print(f"  Records: {total_records:,}")
        print(f"  Total: {total_size / 1024 / 1024:.1f} MB")
        if max_shard:
            print(f"  Max shard: {max_shard / 1024 / 1024:.1f} MB")
        if max_shard > 128 * 1024 * 1024:
            print(f"\n  WARNING: Exceeds 128MB! Use --prefix-len {args.prefix_len + 1}")

        # Per-phase timing breakdown
        if phase_times:
            print(f"\n  Timing breakdown:")
            for name, t in phase_times.items():
                mins, secs = divmod(int(t), 60)
                pct = t * 100 / total_elapsed if total_elapsed > 0 else 0
                print(f"    {name}: {mins}m{secs:02d}s ({pct:.0f}%)")


def main():
    default_workers = max(1, (os.cpu_count() or 4) - 1)

    p = argparse.ArgumentParser(description="Build GERS ID -> bbox index")
    p.add_argument("--version", help="Version string (default: date-based)")
    p.add_argument("--version-suffix", default="0", help="Version suffix")
    p.add_argument("--prefix-len", type=int, default=3,
                   help="Hex prefix length (default: 3 = 4096 shards)")
    p.add_argument("--dry-run", action="store_true", help="Count records only")
    p.add_argument("--smoke-test", action="store_true",
                   help="Quick validation: ~5 prefixes for registry, limited release records")
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
    p.add_argument("--release-only", action="store_true",
                   help="Only run Phase 1b (release theme staging)")

    args = p.parse_args()
    build_id_index(args)


if __name__ == "__main__":
    main()
