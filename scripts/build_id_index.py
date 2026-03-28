#!/usr/bin/env python3
"""
Build GERS ID -> bbox index as UUID-prefix-sharded parquet files.

Pipeline (R2 staging):
  stage-registry: DuckDB streams Overture S3 registry -> R2 staging (per-prefix)
  stage-base:     DuckDB streams Overture S3 release themes -> R2 staging (per-type)
  build:          Merge, sort, and write final parquet shards to R2
  metadata:       Generate id-collection.json, upload to R2

Each phase writes a _SUCCESS marker on completion and skips if the marker
already exists, making the pipeline idempotent and resumable.

Supports range-based parallelism via --prefix-start/--prefix-end for
splitting stage-registry and build across multiple CI jobs. Each range
gets its own success marker (e.g. id-partitioned-000-3ff/_SUCCESS).

Indexes ALL GERS IDs with bounding boxes from the Overture registry
and release themes (base). No type or release filtering —
any valid GERS ID resolves to a bbox.

Final parquet format: UUID column (FIXED_LEN_BYTE_ARRAY(16)),
float bbox columns, uncompressed, sorted by ID.

Usage:
    python scripts/build_id_index.py                              # Full pipeline
    python scripts/build_id_index.py --dry-run                     # Count records only
    python scripts/build_id_index.py --phase stage-registry        # Only registry staging
    python scripts/build_id_index.py --phase stage-base            # Only release staging
    python scripts/build_id_index.py --phase build                 # Only build shards
    python scripts/build_id_index.py --phase metadata              # Regenerate metadata
    python scripts/build_id_index.py --phase stage-registry,build  # Multiple phases

    # Range-based parallelism (CI matrix):
    python scripts/build_id_index.py --phase stage-registry --prefix-start 000 --prefix-end 3ff
    python scripts/build_id_index.py --phase build --prefix-start 000 --prefix-end 3ff

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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import duckdb

# Force line-buffered stdout so CI shows output in real time
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

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
RELEASE_THEMES = ["addresses", "base"]


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
# Retry helper
# ---------------------------------------------------------------------------
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
# Stage: Partition registry + release data to R2
# ---------------------------------------------------------------------------

def _ensure_httpfs_installed():
    """Install httpfs extension once so worker processes can just LOAD it."""
    con = duckdb.connect()
    con.execute("INSTALL httpfs;")
    con.close()


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


def _write_staging_marker(r2_config, version, staging_dir, partition_count, extra=None):
    """Write a _SUCCESS marker to R2 staging after a completed phase."""
    marker = {
        "status": "complete",
        "partitions": partition_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        marker.update(extra)
    tmp = Path(f"tmp-staging-marker-{staging_dir}-{os.getpid()}.json")
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


def _worker_partition_prefix(args):
    """Worker: partition a single registry prefix to R2 staging."""
    prefix, r2_config, version = args
    staging = f"s3://{r2_config['bucket']}/{version}/staging/id-partitioned"
    dest = f"{staging}/prefix={prefix}/data_0.parquet"
    query = _id_query_for_prefix(prefix)
    retries = 3
    con = None

    try:
        con = _r2_con(r2_config)
        con.execute("SET memory_limit = '2GB';")
        con.execute("SET s3_region = 'us-west-2';")

        for attempt in range(retries):
            try:
                row = con.execute(f"SELECT 1 FROM ({query}) LIMIT 1").fetchone()
                if row is None:
                    return (prefix, 0, None)

                # Single-file COPY TO on S3/R2 is an atomic PUT; naturally
                # overwrites if the file exists from a previous partial run.
                con.execute(f"""
                    COPY ({query})
                    TO '{dest}'
                    (FORMAT PARQUET, COMPRESSION ZSTD);
                """)
                return (prefix, 1, None)
            except duckdb.HTTPException as exc:
                if attempt < retries - 1:
                    wait = 30 * (2 ** attempt)
                    print(f"    [registry] Transient error on prefix {prefix} "
                          f"(attempt {attempt + 1}/{retries}): {exc}")
                    time.sleep(wait)
                else:
                    return (prefix, 0, str(exc))
    except Exception as exc:
        return (prefix, 0, str(exc))
    finally:
        if con:
            con.close()


def phase_partition_r2(prefix_len, r2_config, version, prefixes=None, workers=4):
    """Scan Overture S3 registry per-prefix, writing each partition to R2.

    Iterates over hex prefixes individually, using id LIKE '{prefix}%' to
    leverage predicate pushdown on sorted registry parquet. Each prefix is
    independently retryable; reruns will reprocess and overwrite existing
    staged data for that prefix.
    """
    shard_count = 16 ** prefix_len
    if prefixes is None:
        prefixes = [format(i, f'0{prefix_len}x') for i in range(shard_count)]

    print(f"  [registry] Writing {len(prefixes)} prefixes ({workers} workers)...")

    _ensure_httpfs_installed()
    work = [(p, r2_config, version) for p in prefixes]
    t0 = time.time()
    wrote = 0
    errors = []

    with multiprocessing.Pool(workers) as pool:
        for i, result in enumerate(pool.imap_unordered(_worker_partition_prefix, work)):
            prefix, count, err = result
            wrote += count
            if err:
                errors.append((prefix, err))

            done = i + 1
            if done % 100 == 0 or done == len(prefixes):
                elapsed = time.time() - t0
                mins, secs = divmod(int(elapsed), 60)
                rate = done / elapsed if elapsed > 0 else 0
                if rate > 0 and done < len(prefixes) and done >= len(prefixes) // 10:
                    remaining = (len(prefixes) - done) / rate
                    mins_r, secs_r = divmod(int(remaining), 60)
                    eta = f", ~{mins_r}m{secs_r:02d}s remaining"
                else:
                    eta = ""
                print(f"    [registry] {done}/{len(prefixes)} prefixes "
                      f"({wrote} written) [{mins:02d}:{secs:02d}{eta}]")

    elapsed = time.time() - t0
    mins, secs = divmod(int(elapsed), 60)

    if errors:
        print(f"  [registry] {len(errors)} errors:")
        for prefix, err in errors[:10]:
            print(f"    {prefix}: {err}")
        raise RuntimeError(f"Registry partitioning failed: {len(errors)} prefix errors")

    print(f"  [registry] Done: {wrote} prefixes with data in {mins}m{secs:02d}s")


def _release_id_query_for_type(prefix_len, release_version, theme, type_name, limit=None):
    """Query for release IDs from a specific theme/type.

    Includes a computed `prefix` column so the build phase can filter
    on it with predicate pushdown (stored column vs computed expression).
    """
    source = f"'{RELEASE_S3}{release_version}/theme={theme}/type={type_name}/**/*.parquet'"
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    return f"""
        SELECT
            id::UUID as id,
            bbox.xmin::FLOAT as bbox_xmin, bbox.ymin::FLOAT as bbox_ymin,
            bbox.xmax::FLOAT as bbox_xmax, bbox.ymax::FLOAT as bbox_ymax,
            lower(left(replace(id, '-', ''), {prefix_len})) as prefix
        FROM read_parquet({source}, union_by_name=true)
        WHERE id IS NOT NULL AND bbox IS NOT NULL AND bbox.xmin IS NOT NULL
        {limit_clause}
    """


def _r2_release_staging_exists(r2_config, version):
    """Check if completed release staging data exists for this version."""
    return _read_staging_marker(r2_config, version, "id-release") is not None


def _discover_release_types(release_version):
    """Discover type= sub-directories under each release theme from S3."""
    types = set()
    for theme in RELEASE_THEMES:
        prefix = f"release/{release_version}/theme={theme}/type="
        try:
            result = subprocess.run(
                ["aws", "s3", "ls", f"s3://overturemaps-us-west-2/{prefix}",
                 "--no-sign-request", "--region", "us-west-2"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    # aws s3 ls output: "                           PRE type=land/"
                    part = line.strip().removeprefix("PRE ").rstrip("/")
                    if part.startswith("type="):
                        type_name = part[len("type="):]
                        if type_name:
                            types.add((theme, type_name))
        except Exception:
            pass
    return sorted(types)


def _partition_release_type(theme, type_name, prefix_len, release_version,
                            r2_config, version, limit=None):
    """Stage a single release theme/type as one parquet file on R2."""
    staging_dir = f"id-release-{theme}-{type_name}"

    # Skip if this type already completed
    marker = _read_staging_marker(r2_config, version, staging_dir)
    if marker is not None:
        print(f"    [release] {theme}/{type_name} already complete, skipping")
        return (theme, type_name)

    dest = f"s3://{r2_config['bucket']}/{version}/staging/{staging_dir}/data.parquet"

    con = _r2_con(r2_config)
    con.execute("SET memory_limit = '4GB';")
    con.execute("SET s3_region = 'us-west-2';")

    query = _release_id_query_for_type(prefix_len, release_version, theme, type_name, limit=limit)

    def _do_copy():
        con.execute(f"""
            COPY ({query})
            TO '{dest}'
            (FORMAT PARQUET, COMPRESSION ZSTD);
        """)

    _retry_transient(_do_copy)()
    con.close()

    _write_staging_marker(r2_config, version, staging_dir, 1)
    return (theme, type_name)


def phase_partition_release_r2(prefix_len, release_version, r2_config, version, limit=None):
    """Stage release themes to R2, one parquet file per type.

    Discovers type= sub-directories under each theme and runs them
    in parallel. Each type writes a single file to its own staging
    path so there are no conflicts.
    """
    _ensure_httpfs_installed()
    print(f"  [release] Discovering release types...")
    types = _discover_release_types(release_version)
    if not types:
        print(f"  [release] No release types found, skipping")
        return

    print(f"  [release] Found {len(types)} types: "
          f"{', '.join(f'{t}/{n}' for t, n in types)}")

    t0 = time.time()
    errors = []

    with ThreadPoolExecutor(max_workers=min(len(types), 6)) as executor:
        futures = {}
        for theme, type_name in types:
            f = executor.submit(
                _partition_release_type,
                theme, type_name, prefix_len, release_version,
                r2_config, version, limit=limit,
            )
            futures[f] = (theme, type_name)

        for future in as_completed(futures):
            theme, type_name = futures[future]
            try:
                future.result()
                print(f"    [release] {theme}/{type_name} complete")
            except Exception as exc:
                errors.append((theme, type_name, str(exc)))
                print(f"    [release] {theme}/{type_name} FAILED: {exc}")

    elapsed = time.time() - t0
    mins, secs = divmod(int(elapsed), 60)

    if errors:
        print(f"  [release] {len(errors)} type errors:")
        for theme, type_name, err in errors:
            print(f"    {theme}/{type_name}: {err}")
        raise RuntimeError(f"Release partitioning failed: {len(errors)} type errors")

    print(f"  [release] Done: {len(types)} types in {mins}m{secs:02d}s")
    _write_staging_marker(r2_config, version, "id-release", len(types))


# ---------------------------------------------------------------------------
# Build: Merge, sort, and write final snappy parquet shards
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


def _worker_build_r2_batch(args_tuple):
    """Worker: download one staging prefix locally, write one output shard to R2.

    Downloads the registry staging partition once to local disk, merges with
    release data, sorts by ID, and writes the output shard to R2 (uncompressed).

    Release files are pre-downloaded locally by the caller and passed as local
    paths. All reads are local; only output writes go to R2.
    """
    staging_prefix, output_prefixes, r2_config, version, release_files = args_tuple

    bucket = r2_config['bucket']
    results = []
    t0 = time.time()

    try:
        con = duckdb.connect()
        con.execute("SET memory_limit = '2GB';")
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

        # Download registry staging partition locally (one R2 read instead of N)
        registry_r2 = (
            f"s3://{bucket}/{version}/staging/id-partitioned/"
            f"prefix={staging_prefix}/*.parquet"
        )
        local_registry = f"/tmp/build-reg-{staging_prefix}-{os.getpid()}.parquet"
        has_registry = False

        try:
            con.execute(f"""
                COPY (
                    SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax
                    FROM read_parquet('{registry_r2}')
                ) TO '{local_registry}' (FORMAT PARQUET);
            """)
            has_registry = True
        except Exception:
            pass

        for prefix in output_prefixes:
            needs_filter = len(prefix) > len(staging_prefix)
            id_filter = f"AND id::VARCHAR LIKE '{prefix}%'" if needs_filter else ""

            sources = []

            # Registry: read from LOCAL file
            if has_registry:
                sources.append(
                    f"SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax "
                    f"FROM read_parquet('{local_registry}') WHERE 1=1 {id_filter}"
                )

            # Release: read from local files with prefix filter
            for release_path in release_files:
                try:
                    row = con.execute(
                        f"SELECT 1 FROM read_parquet('{release_path}') "
                        f"WHERE prefix = '{staging_prefix}' {id_filter} LIMIT 1"
                    ).fetchone()
                    if row:
                        sources.append(
                            f"SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax "
                            f"FROM read_parquet('{release_path}') "
                            f"WHERE prefix = '{staging_prefix}' {id_filter}"
                        )
                except Exception:
                    pass

            if not sources:
                results.append((prefix, 0, 0, None))
                continue

            union_query = " UNION ALL ".join(sources)

            # Count locally (fast, avoids R2 read-back)
            count = con.execute(
                f"SELECT COUNT(*) FROM ({union_query})"
            ).fetchone()[0]
            if count == 0:
                results.append((prefix, 0, 0, None))
                continue

            # Sort and write to R2
            r2_dest = f"s3://{bucket}/{version}/id-index/{prefix}.parquet"
            con.execute(f"""
                COPY (
                    SELECT * FROM ({union_query}) ORDER BY id
                ) TO '{r2_dest}'
                (FORMAT PARQUET, COMPRESSION UNCOMPRESSED, ROW_GROUP_SIZE 100000);
            """)

            size = count * (16 + 4 * 4)
            results.append((prefix, count, size, None))

        # Cleanup
        if has_registry:
            try:
                os.unlink(local_registry)
            except Exception:
                pass
        con.close()

        return results

    except Exception as e:
        return [(p, 0, 0, str(e)) for p in output_prefixes]


def _run_pool(worker_fn, work_items, total_label, workers):
    """Run multiprocessing pool with progress reporting."""
    _ensure_httpfs_installed()
    total = len(work_items)
    results = []
    t0 = time.time()
    # More frequent progress for large sets
    progress_interval = 50 if total > 500 else (10 if total > 50 else 5)

    with multiprocessing.Pool(workers) as pool:
        for i, result in enumerate(pool.imap_unordered(worker_fn, work_items)):
            results.append(result)
            done = i + 1
            if done % progress_interval == 0 or done == total:
                built = sum(1 for r in results if r[1] > 0)
                records = sum(r[1] for r in results)
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
                    f", {records:,} records"
                    f" ({mins_e}m{secs_e:02d}s elapsed{eta})",
                    flush=True,
                )
    return results


def _discover_release_staging_files(con, r2_config, version):
    """Discover all id-release-* staging files in R2.

    Returns a list of S3 paths to single-file release staging parquets.
    """
    bucket = r2_config["bucket"]
    staging_base = f"s3://{bucket}/{version}/staging"
    files = []
    try:
        rows = con.execute(f"""
            SELECT file
            FROM glob('{staging_base}/id-release-*/data.parquet')
        """).fetchall()
        files = [row[0] for row in rows]
    except Exception:
        pass
    return sorted(files)


def _discover_staging_prefixes(r2_config, version):
    """List prefixes that have data in registry or release staging, plus release file paths."""
    con = _r2_con(r2_config)

    # Find release staging files (single file per type)
    release_files = _discover_release_staging_files(con, r2_config, version)

    # Get registry prefixes
    prefixes = set()
    try:
        rows = con.execute(f"""
            SELECT DISTINCT
                regexp_extract(file, 'prefix=([^/]+)', 1) as prefix
            FROM glob(
                's3://{r2_config["bucket"]}/{version}/staging/id-partitioned/prefix=*/*.parquet'
            )
        """).fetchall()
        prefixes.update(r[0] for r in rows if r[0])
    except Exception:
        pass

    # Also include prefixes from release staging files (release-only IDs)
    for release_path in release_files:
        try:
            rows = con.execute(f"""
                SELECT DISTINCT prefix
                FROM read_parquet('{release_path}')
            """).fetchall()
            prefixes.update(r[0] for r in rows if r[0])
        except Exception:
            pass

    con.close()
    return sorted(prefixes), release_files


def phase_build_r2(prefix_len, r2_config, version, workers, prefixes=None):
    """Build parquet shards from R2 staging, upload to R2.

    If prefixes is provided, only build those specific prefixes (for parallel
    range-based builds). Otherwise, discover from staging or build all.
    """

    # Pre-install httpfs so workers only need LOAD
    con = duckdb.connect()
    con.execute("INSTALL httpfs;")
    con.close()

    shard_count = 16 ** prefix_len
    all_prefixes = [format(i, f'0{prefix_len}x') for i in range(shard_count)]

    if prefixes is not None:
        # Range-based build: use provided prefixes directly, discover release files
        con = _r2_con(r2_config)
        release_files = _discover_release_staging_files(con, r2_config, version)
        con.close()
    else:
        # Full build: discover which prefixes have staging data
        staged, release_files = _discover_staging_prefixes(r2_config, version)
        if staged and len(staged) < shard_count:
            print(f"  Found {len(staged)}/{shard_count} prefixes with staging data")
            prefixes = staged
        else:
            prefixes = all_prefixes

    if release_files:
        print(f"  Release files: {len(release_files)}")

    # Download release files locally, sorted by (prefix, id) for fast sequential access
    local_release_files = []
    if release_files:
        print(f"  Downloading and sorting {len(release_files)} release files locally...")
        dl_con = _r2_con(r2_config)
        dl_con.execute("SET memory_limit = '4GB';")
        for rf in release_files:
            name = rf.split("/staging/")[-1].split("/")[0]
            local_path = f"/tmp/build-release-{name}.parquet"
            try:
                t_dl = time.time()
                dl_con.execute(f"""
                    COPY (
                        SELECT * FROM read_parquet('{rf}')
                        ORDER BY prefix, id
                    ) TO '{local_path}' (FORMAT PARQUET, COMPRESSION ZSTD);
                """)
                size_mb = os.path.getsize(local_path) / 1024 / 1024
                print(f"    {name}: {size_mb:.0f} MB ({time.time() - t_dl:.0f}s)")
                local_release_files.append(local_path)
            except Exception as e:
                print(f"    WARNING: Failed to download {name}: {e}")
        dl_con.close()

    # Each prefix maps 1:1 to a staging prefix (same prefix-len)
    work = [
        (p, [p], r2_config, version, local_release_files)
        for p in prefixes
    ]

    print(f"  Processing {len(prefixes)} shards ({workers} workers)...")

    # Run batch workers (each processes one staging prefix -> one output shard)
    _ensure_httpfs_installed()
    results = []
    t0 = time.time()
    progress_interval = 10 if len(work) > 100 else 5

    with multiprocessing.Pool(workers) as pool:
        for i, batch_results in enumerate(
            pool.imap_unordered(_worker_build_r2_batch, work)
        ):
            results.extend(batch_results)
            done = i + 1
            if done % progress_interval == 0 or done == len(work):
                built = sum(1 for r in results if r[1] > 0)
                records = sum(r[1] for r in results)
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                if rate > 0 and done < len(work) and done >= len(work) // 10:
                    remaining = (len(work) - done) / rate
                    mins_r, secs_r = divmod(int(remaining), 60)
                    eta = f", ~{mins_r}m{secs_r:02d}s remaining"
                else:
                    eta = ""
                mins_e, secs_e = divmod(int(elapsed), 60)
                print(
                    f"    {done}/{len(work)} shards, "
                    f"{built} with data, {records:,} records"
                    f" ({mins_e}m{secs_e:02d}s elapsed{eta})",
                    flush=True,
                )

    # Cleanup local release files
    for lf in local_release_files:
        try:
            os.unlink(lf)
        except Exception:
            pass

    # Add empty results for skipped prefixes (for metadata accuracy)
    if len(prefixes) < shard_count:
        processed = {r[0] for r in results}
        for p in all_prefixes:
            if p not in processed:
                results.append((p, 0, 0, None))

    return results


# ---------------------------------------------------------------------------
# Metadata: Generate id-collection.json
# ---------------------------------------------------------------------------

def _gather_shard_info_from_r2(prefix_len, r2_config, version):
    """Discover existing R2 shards via glob (for metadata-only runs).

    Only checks which shards exist — does not read individual files for
    record counts. Returns (prefix, 1, 0, None) for each shard found.
    """
    print("  Discovering existing R2 shards...")
    con = _r2_con(r2_config)

    bucket = r2_config["bucket"]
    glob_path = f"s3://{bucket}/{version}/id-index/*.parquet"
    try:
        rows = con.execute(f"SELECT file FROM glob('{glob_path}')").fetchall()
        shard_files = [row[0] for row in rows]
    except Exception:
        shard_files = []

    con.close()

    results = []
    for path in shard_files:
        prefix = path.rsplit("/", 1)[-1].replace(".parquet", "")
        results.append((prefix, 1, 0, None))

    print(f"  Found {len(results)} shards")
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
                k: v for k, v in [
                    ("href", f"./id-index/{p}.parquet"),
                    ("record_count", s.get("record_count")),
                    ("size_bytes", s.get("size_bytes")),
                ] if v
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

    # Upload id-meta.json (tiny metadata file for fast worker prefix_len lookup)
    meta = {"prefix_len": prefix_len, "shard_count": len(shard_infos)}
    tmp_meta = Path("tmp-id-meta.json")
    write_json(tmp_meta, meta)
    err = _upload_to_r2(tmp_meta, f"geocoder-shards/{version}/id-meta.json")
    tmp_meta.unlink(missing_ok=True)
    if err:
        print(f"  ERROR uploading id-meta.json: {err}")
        sys.exit(1)
    print("  Uploaded id-meta.json to R2")

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

    # Compute prefix range for parallelism
    all_prefixes = [format(i, f'0{args.prefix_len}x') for i in range(shard_count)]
    if args.prefixes:
        # Explicit individual prefixes (for patching failed runs)
        range_prefixes = [p.strip() for p in args.prefixes.split(",")]
        invalid = [p for p in range_prefixes if p not in all_prefixes]
        if invalid:
            print(f"ERROR: invalid prefixes: {', '.join(invalid)}")
            sys.exit(1)
        print(f"  Explicit prefixes: {', '.join(range_prefixes)} ({len(range_prefixes)} prefixes)")
        range_suffix = ""  # no implicit range suffix; use --marker-ranges instead
    elif bool(args.prefix_start) != bool(args.prefix_end):
        print("ERROR: --prefix-start and --prefix-end must be provided together")
        sys.exit(1)
    elif args.prefix_start and args.prefix_end:
        start_idx = int(args.prefix_start, 16)
        end_idx = int(args.prefix_end, 16)
        if start_idx > end_idx:
            print(f"ERROR: --prefix-start ({args.prefix_start}) must be <= --prefix-end ({args.prefix_end})")
            sys.exit(1)
        range_prefixes = [p for p in all_prefixes if start_idx <= int(p, 16) <= end_idx]
        print(f"  Prefix range: {args.prefix_start}-{args.prefix_end} ({len(range_prefixes)} prefixes)")
        range_suffix = f"-{args.prefix_start}-{args.prefix_end}"
    else:
        range_prefixes = None
        range_suffix = ""

    # Smoke test: pick ~5 evenly-spaced prefixes for registry staging
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

    # === Stage registry ===
    if run_all or "stage-registry" in phases:
        marker_ranges = None
        if getattr(args, 'marker_ranges', None):
            marker_ranges = [r.strip() for r in args.marker_ranges.split(",")]

        staging_marker_key = f"id-partitioned{range_suffix}"
        # When patching with --prefixes + --marker-ranges, skip the marker check
        if marker_ranges is None and _read_staging_marker(r2_config, version, staging_marker_key) is not None:
            print(f"\nStage registry: Skipped ({staging_marker_key} complete for {version})")
        else:
            print(f"\nStage registry: Partition registry")
            t0 = time.time()
            stage_prefixes = smoke_prefixes or range_prefixes
            phase_partition_r2(args.prefix_len, r2_config, version,
                               prefixes=stage_prefixes, workers=args.workers)

            # Write markers for explicit ranges (patching) or the computed range
            if marker_ranges:
                for mr in marker_ranges:
                    mk = f"id-partitioned-{mr}"
                    _write_staging_marker(r2_config, version, mk,
                                          len(stage_prefixes) if stage_prefixes else shard_count)
                    print(f"  Wrote marker: {mk}")
            else:
                _write_staging_marker(r2_config, version, staging_marker_key,
                                      len(stage_prefixes) if stage_prefixes else shard_count)
            phase_times["Stage registry"] = time.time() - t0

    # === Stage base (release themes) ===
    if run_all or "stage-base" in phases:
        if _r2_release_staging_exists(r2_config, version):
            print(f"\nStage base: Skipped (release staging complete for {version})")
        else:
            print(f"\nStage base: Partition release themes ({', '.join(RELEASE_THEMES)})")
            t0 = time.time()
            phase_partition_release_r2(
                args.prefix_len, release_version, r2_config, version,
                limit=smoke_release_limit,
            )
            phase_times["Stage base"] = time.time() - t0

    # === Build shards ===
    results = None
    if run_all or "build" in phases:
        build_marker_key = f"build{range_suffix}"
        marker = _read_staging_marker(r2_config, version, build_marker_key)
        if marker is not None:
            print(f"\nBuild: Skipped ({build_marker_key} complete for {version})")
        else:
            print(f"\nBuild: Build parquet shards")
            t0 = time.time()
            results = phase_build_r2(
                args.prefix_len, r2_config, version, args.workers,
                prefixes=range_prefixes,
            )
            elapsed_p2 = time.time() - t0
            phase_times["Build"] = elapsed_p2

            built = sum(1 for r in results if r[1] > 0)
            records = sum(r[1] for r in results)
            errs = sum(1 for r in results if r[3] is not None)
            mins, secs = divmod(int(elapsed_p2), 60)
            print(f"  {built} shards, {records:,} records in {mins}m{secs:02d}s")
            if errs:
                print(f"  {errs} upload errors — not marking build complete")
                for r in results:
                    if r[3] is not None:
                        print(f"    {r[0]}: {r[3]}")
                raise RuntimeError(f"Build failed: {errs} shard errors")

            _write_staging_marker(r2_config, version, build_marker_key, built,
                                  extra={"records": records})

    # === Metadata ===
    if run_all or "metadata" in phases:
        meta_marker = _read_staging_marker(r2_config, version, "metadata")
        if meta_marker is not None:
            print(f"\nMetadata: Skipped (metadata complete for {version})")
        else:
            t_meta = time.time()
            # If build phase didn't run, gather shard info from existing R2 data
            if results is None:
                results = _gather_shard_info_from_r2(
                    args.prefix_len, r2_config, version,
                )

            print(f"\nMetadata: Generate id-collection.json")
            shard_infos, total_records, errors = phase_metadata(
                results, args.prefix_len, version, release_version, r2_config,
            )
            phase_times["Metadata"] = time.time() - t_meta

            _write_staging_marker(r2_config, version, "metadata", len(shard_infos),
                                  extra={"records": total_records})

            total_size = sum(s["size_bytes"] for s in shard_infos.values())
            max_shard = max((s["size_bytes"] for s in shard_infos.values()), default=0)

            print(f"  Shards: {len(shard_infos)} / {shard_count}")
            print(f"  Records: {total_records:,}")
            print(f"  Total: {total_size / 1024 / 1024:.1f} MB")
            if max_shard:
                print(f"  Max shard: {max_shard / 1024 / 1024:.1f} MB")
            if max_shard > 128 * 1024 * 1024:
                print(f"\n  WARNING: Exceeds 128MB! Use --prefix-len {args.prefix_len + 1}")

    total_elapsed = time.time() - t_total
    total_mins, total_secs = divmod(int(total_elapsed), 60)
    print(f"\nDone! ({total_mins}m{total_secs:02d}s total)")

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
                   help="Hex prefix length for output shards (default: 3 = 4096 shards)")
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
                   help="Run specific phase(s): stage-registry, stage-base, build, metadata, or all (comma-separated)")
    p.add_argument("--prefix-start",
                   help="Start prefix inclusive (hex, e.g. '000') for range-based parallelism")
    p.add_argument("--prefix-end",
                   help="End prefix inclusive (hex, e.g. '3ff') for range-based parallelism")
    p.add_argument("--prefixes",
                   help="Comma-separated individual prefixes to process (e.g. '001,401,801')")
    p.add_argument("--marker-ranges",
                   help="Comma-separated ranges to write _SUCCESS markers for (e.g. '000-3ff,400-7ff')")

    args = p.parse_args()
    build_id_index(args)


if __name__ == "__main__":
    main()
