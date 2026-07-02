#!/usr/bin/env python3
"""
Build GERS ID -> bbox index as UUID-prefix-sharded parquet files.

Pipeline (R2 staging):
  stage-registry: DuckDB partitions Overture S3 registry -> R2 staging
                  (single COPY ... PARTITION_BY (prefix) pass per range job)
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
import threading
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

# Rows per parquet row group in output shards. Every cold /id lookup
# range-reads one full row group, so this bounds the cold-read size
# (~100k rows ≈ 2.4-3 MB). Chosen deliberately (f78a6a0): ~10 groups per
# shard, and sorted UUIDs + row-group stats already skip ~90% of data per
# lookup. If this shrinks, the worker's FOOTER_SUFFIX_SIZE (stac.rs) must
# grow in the same change — more row groups means a bigger footer.
ROW_GROUP_SIZE = 100_000


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

# DuckDB exception types that indicate transient network/IO trouble
# (HTTP 5xx, connection resets, S3 hiccups). Guarded with getattr so this
# keeps working across duckdb versions.
TRANSIENT_DUCKDB_ERRORS = tuple(
    exc for exc in (
        getattr(duckdb, "HTTPException", None),
        getattr(duckdb, "IOException", None),
        getattr(duckdb, "ConnectionException", None),
        # Raised when the sub-range watchdog interrupts a wedged COPY;
        # retrying is exactly the desired response.
        getattr(duckdb, "InterruptException", None),
    )
    if exc is not None
)


def _is_transient(exc):
    """True if exc looks like a transient network/IO error worth retrying."""
    if not isinstance(exc, TRANSIENT_DUCKDB_ERRORS):
        return False
    # "No files found" IOExceptions mean genuine absence, not flakiness
    if "No files found" in str(exc):
        return False
    # A full disk will not empty itself between attempts; fail fast so the
    # job surfaces the real problem instead of burning retry backoff.
    if "No space left on device" in str(exc):
        return False
    return True


def _retry_transient(fn, retries=3, backoff=30, on_retry=None):
    """Wrap fn to retry on transient HTTP/IO errors (502, 503, resets, etc.).

    on_retry, if given, runs before each retry (not before the first
    attempt) — used to clean up partial output from the failed attempt.
    """
    def _wrapped():
        for attempt in range(retries):
            try:
                return fn()
            except Exception as exc:
                if _is_transient(exc) and attempt < retries - 1:
                    wait = backoff * (2 ** attempt)
                    print(f"    Transient error (attempt {attempt + 1}/{retries}): {exc}")
                    print(f"    Retrying in {wait}s...")
                    if on_retry:
                        on_retry()
                    time.sleep(wait)
                else:
                    raise
    return _wrapped


def _glob_files(con, pattern):
    """List files matching a glob; empty list when none match (not an error)."""
    try:
        rows = con.execute(f"SELECT file FROM glob('{pattern}')").fetchall()
    except duckdb.Error as exc:
        if "No files found" in str(exc):
            return []
        raise
    return sorted(r[0] for r in rows)


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
    # Guard against hung S3 sockets: a staging range job once wedged for
    # 2.5h (siblings: ~40min) on a stalled read. Bound each HTTP request
    # and retry; disable keep-alive so stale pooled connections can't hang.
    con.execute("SET http_timeout = 120000;")  # ms
    con.execute("SET http_retries = 5;")
    con.execute("SET http_keep_alive = false;")
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
    r2_key = f"{r2_config['bucket']}/{version}/staging/{staging_dir}/_SUCCESS"
    err = _upload_to_r2(tmp, r2_key)
    tmp.unlink(missing_ok=True)
    if err:
        print(f"  WARNING: Failed to write staging marker: {err}")


# Substrings in wrangler errors that mean "object genuinely absent"
_R2_ABSENT_MARKERS = ("does not exist", "not found", "404", "10007")


def _read_staging_marker(r2_config, version, staging_dir, retries=3):
    """Read a _SUCCESS marker from R2 staging.

    Returns the parsed marker dict, or None only when the object is
    genuinely absent. Transient read errors are retried, then raised, so a
    network blip can never silently restart a completed phase (or worse,
    skip one).
    """
    r2_key = f"{r2_config['bucket']}/{version}/staging/{staging_dir}/_SUCCESS"
    last_err = None
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["wrangler", "r2", "object", "get", r2_key,
                 "--remote", "--pipe"],
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            last_err = "wrangler r2 object get timed out"
        else:
            if result.returncode == 0:
                return json.loads(result.stdout)
            err_text = f"{result.stderr or ''} {result.stdout or ''}".strip()
            if any(s in err_text.lower() for s in _R2_ABSENT_MARKERS):
                return None  # genuinely absent
            last_err = err_text[:300]
        if attempt < retries - 1:
            wait = 10 * (attempt + 1)
            print(f"    Marker read retry {attempt + 1}/{retries} "
                  f"for {r2_key}: {last_err}")
            time.sleep(wait)
    raise RuntimeError(f"Failed to read staging marker {r2_key}: {last_err}")


def _delete_r2_keys(r2_config, keys):
    """Batch-delete objects from R2 via the S3 API (aws CLI, 1000 keys/call)."""
    if not keys:
        return
    env = os.environ.copy()
    env["AWS_ACCESS_KEY_ID"] = r2_config["key_id"]
    env["AWS_SECRET_ACCESS_KEY"] = r2_config["secret"]
    env["AWS_REQUEST_CHECKSUM_CALCULATION"] = "when_required"
    env["AWS_RESPONSE_CHECKSUM_VALIDATION"] = "when_required"
    endpoint = f"https://{r2_config['endpoint']}"

    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        payload = json.dumps({"Objects": [{"Key": k} for k in batch], "Quiet": True})
        try:
            result = subprocess.run(
                ["aws", "s3api", "delete-objects",
                 "--bucket", r2_config["bucket"],
                 "--delete", payload,
                 "--endpoint-url", endpoint, "--region", "auto"],
                capture_output=True, text=True, timeout=300, env=env,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "aws CLI is required to delete stale staged objects"
            ) from None
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to batch-delete R2 objects: {result.stderr[:300]}")


def _clear_staged_registry(r2_config, version, prefix_len,
                           prefixes=None, prefix_start=None, prefix_end=None):
    """Delete staged registry files for the targeted prefixes.

    A failed partitioned COPY can leave per-partition files behind; a re-run
    may write fewer files per partition than the failed attempt, so stale
    extras would silently duplicate data in the final shards. Clearing first
    makes re-runs exact.
    """
    bucket = r2_config["bucket"]
    con = _r2_con(r2_config)
    try:
        files = _retry_transient(lambda: _glob_files(
            con,
            f"s3://{bucket}/{version}/staging/id-partitioned/prefix=*/*.parquet",
        ))()
    finally:
        con.close()
    if not files:
        return

    if prefixes is not None:
        wanted = set(prefixes)
    elif prefix_start and prefix_end:
        lo, hi = int(prefix_start, 16), int(prefix_end, 16)
        wanted = {format(i, f'0{prefix_len}x') for i in range(lo, hi + 1)}
    else:
        wanted = None  # full run: clear everything

    keys = []
    for f in files:
        prefix = f.split("prefix=", 1)[-1].split("/", 1)[0]
        if wanted is None or prefix in wanted:
            keys.append(f.split(f"s3://{bucket}/", 1)[-1])

    if keys:
        print(f"  [registry] Clearing {len(keys)} stale staged files...")
        _delete_r2_keys(r2_config, keys)


# A partitioned COPY keeps a write buffer per active partition. With up
# to 1024 partitions per range job that OOMs the runner, so each range
# is split into sub-ranges (~SUB_RANGE_PARTITIONS partitions per COPY).
SUB_RANGE_PARTITIONS = 128


def _registry_sub_ranges(prefix_len, prefixes=None,
                         prefix_start=None, prefix_end=None):
    """Build the sub-range plan for registry staging.

    Returns (sub_ranges, label) where each sub-range is a
    (filter_sql, sub_label, clear_kwargs) tuple; clear_kwargs are the
    _clear_staged_registry arguments that target exactly that sub-range's
    partitions, so a retry after an interrupted COPY can remove partial
    output from the failed attempt.

    With neither prefixes nor a range, covers the whole prefix space
    through the same sub-ranged path a CI range job uses, so each COPY
    stays memory-bounded.
    """
    if prefixes is not None:
        # Explicit prefix list (smoke test / patching): prefix-LIKE filters
        # keep predicate pushdown on the sorted registry parquet.
        like_filters = " OR ".join(f"id LIKE '{p}%'" for p in prefixes)
        sub_ranges = [(f"({like_filters})", f"{len(prefixes)} explicit prefixes",
                       {"prefixes": list(prefixes)})]
        return sub_ranges, sub_ranges[0][1]

    if not (prefix_start and prefix_end):
        prefix_start = format(0, f'0{prefix_len}x')
        prefix_end = format(16 ** prefix_len - 1, f'0{prefix_len}x')

    # IDs are lowercase hex UUID strings; for prefix_len <= 8 the first
    # prefix_len chars precede the first hyphen, so plain string bounds
    # cover the range (and push down to row-group stats).
    lo, hi = int(prefix_start, 16), int(prefix_end, 16)
    sub_ranges = []
    for sub_lo in range(lo, hi + 1, SUB_RANGE_PARTITIONS):
        sub_hi = min(sub_lo + SUB_RANGE_PARTITIONS - 1, hi)
        sub_lo_hex = format(sub_lo, f'0{prefix_len}x')
        sub_hi_hex = format(sub_hi, f'0{prefix_len}x')
        cond = f"id >= '{sub_lo_hex}'"
        upper_idx = sub_hi + 1
        if upper_idx < 16 ** prefix_len:
            upper = format(upper_idx, f'0{prefix_len}x')
            cond += f" AND id < '{upper}'"
        # Also bound the partition key itself: the raw-id bounds above
        # assume lowercase hex. A non-lowercase ID string-sorts between
        # '9zz' and 'a00', so without this it would be staged under a
        # partition owned by a PARALLEL range job, racing that job's
        # clear/write cycle.
        cond += (f" AND lower(left(replace(id, '-', ''), {prefix_len}))"
                 f" BETWEEN '{sub_lo_hex}' AND '{sub_hi_hex}'")
        sub_ranges.append(
            (cond, f"{sub_lo_hex}-{sub_hi_hex}",
             {"prefix_start": sub_lo_hex, "prefix_end": sub_hi_hex})
        )
    label = f"range {prefix_start}-{prefix_end} ({len(sub_ranges)} sub-ranges)"
    return sub_ranges, label


def phase_partition_r2(prefix_len, r2_config, version,
                       prefixes=None, prefix_start=None, prefix_end=None):
    """Partition the Overture registry into per-prefix staging files on R2.

    Single COPY ... PARTITION_BY (prefix) pass over the registry per range
    job (instead of one full registry scan per prefix). The hive layout
    (prefix=XXX/data_*.parquet) is unchanged, so the build phase and
    patch tooling read it exactly as before.
    """
    bucket = r2_config["bucket"]
    dest = f"s3://{bucket}/{version}/staging/id-partitioned"

    # A sub-range COPY normally takes ~5 min; interrupt anything past this
    # so the transient-retry wrapper can take over (a wedged S3 read once
    # hung a range job for 2.5h).
    SUBRANGE_COPY_TIMEOUT_S = 20 * 60

    sub_ranges, label = _registry_sub_ranges(
        prefix_len, prefixes=prefixes,
        prefix_start=prefix_start, prefix_end=prefix_end)

    # Re-run safety: remove leftovers from a previous partial attempt
    _clear_staged_registry(r2_config, version, prefix_len,
                           prefixes=prefixes,
                           prefix_start=prefix_start, prefix_end=prefix_end)

    print(f"  [registry] Partitioning registry ({label})...")
    t0 = time.time()

    con = _r2_con(r2_config)
    con.execute("SET memory_limit = '10GB';")
    # Staged file order is irrelevant (the build phase re-sorts), and
    # preserving insertion order is the main memory amplifier in COPY.
    con.execute("SET preserve_insertion_order = false;")
    con.execute("SET s3_region = 'us-west-2';")

    for sub_filter, sub_label, sub_target in sub_ranges:
        def _do_copy(sub_filter=sub_filter):
            # Watchdog: a sub-range COPY normally finishes in ~5 minutes; a
            # range job once wedged for hours on a stalled read despite HTTP
            # timeouts. Interrupt the query past the deadline so
            # _retry_transient can retry it instead of hanging the job.
            watchdog = threading.Timer(SUBRANGE_COPY_TIMEOUT_S, con.interrupt)
            watchdog.start()
            try:
                con.execute(f"""
                    COPY (
                        SELECT
                            id::UUID as id,
                            bbox.xmin::FLOAT as bbox_xmin, bbox.ymin::FLOAT as bbox_ymin,
                            bbox.xmax::FLOAT as bbox_xmax, bbox.ymax::FLOAT as bbox_ymax,
                            lower(left(replace(id, '-', ''), {prefix_len})) as prefix
                        FROM read_parquet('{REGISTRY_S3}*')
                        WHERE id IS NOT NULL AND bbox IS NOT NULL AND bbox.xmin IS NOT NULL
                        AND {sub_filter}
                    ) TO '{dest}'
                    (FORMAT PARQUET, COMPRESSION ZSTD,
                     PARTITION_BY (prefix), OVERWRITE_OR_IGNORE true);
                """)
            finally:
                watchdog.cancel()

        def _clear_partial(sub_target=sub_target):
            # An interrupted COPY may already have flushed some partition
            # files, and the retry can write fewer files per partition —
            # stale extras from the failed attempt would silently duplicate
            # rows in the final shards. Clear this sub-range before retrying.
            _clear_staged_registry(r2_config, version, prefix_len, **sub_target)

        _retry_transient(_do_copy, on_retry=_clear_partial)()
        print(f"  [registry]   sub-range {sub_label} done")
    con.close()

    elapsed = time.time() - t0
    mins, secs = divmod(int(elapsed), 60)
    print(f"  [registry] Done ({label}) in {mins}m{secs:02d}s")


def _release_id_query_for_type(prefix_len, release_version, theme, type_name, limit=None):
    """Query for release IDs from a specific theme/type.

    Includes a computed `prefix` column so the build phase can filter on it
    with predicate pushdown (stored column vs computed expression), and a
    `bucket` column (first hex char) used as the staging partition key: 16
    buckets means at most 16 live write buffers during the partitioned COPY,
    far below the 128 the registry staging sustains, regardless of how large
    release themes grow. (Per-prefix PARTITION_BY — 4,096 buffers — OOMed
    the runners and was retired; see the perf plan doc.)
    """
    source = f"'{RELEASE_S3}{release_version}/theme={theme}/type={type_name}/**/*.parquet'"
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    return f"""
        SELECT
            id::UUID as id,
            bbox.xmin::FLOAT as bbox_xmin, bbox.ymin::FLOAT as bbox_ymin,
            bbox.xmax::FLOAT as bbox_xmax, bbox.ymax::FLOAT as bbox_ymax,
            lower(left(replace(id, '-', ''), {prefix_len})) as prefix,
            lower(left(replace(id, '-', ''), 1)) as bucket
        FROM read_parquet({source}, union_by_name=true)
        WHERE id IS NOT NULL AND bbox IS NOT NULL AND bbox.xmin IS NOT NULL
        {limit_clause}
    """


def _r2_release_staging_exists(r2_config, version):
    """Check if completed release staging data exists for this version."""
    return _read_staging_marker(r2_config, version, "id-release") is not None


def _discover_release_types(release_version, retries=3):
    """Discover type= sub-directories under each release theme from S3.

    Retries transient listing failures and hard-fails if any theme comes
    back empty: a transient AWS CLI failure must never silently drop a
    whole theme (addresses/base) from the index.
    """
    types = set()
    for theme in RELEASE_THEMES:
        prefix = f"release/{release_version}/theme={theme}/type="
        theme_types = set()
        last_err = None
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    ["aws", "s3", "ls", f"s3://overturemaps-us-west-2/{prefix}",
                     "--no-sign-request", "--region", "us-west-2"],
                    capture_output=True, text=True, timeout=60,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                last_err = str(exc)
            else:
                if result.returncode == 0:
                    for line in result.stdout.strip().splitlines():
                        # aws s3 ls output: "                           PRE type=land/"
                        part = line.strip().removeprefix("PRE ").rstrip("/")
                        if part.startswith("type="):
                            type_name = part[len("type="):]
                            if type_name:
                                theme_types.add((theme, type_name))
                    if theme_types:
                        break
                    last_err = "listing succeeded but returned no types"
                else:
                    last_err = (result.stderr or "")[:300]
            if attempt < retries - 1:
                wait = 10 * (attempt + 1)
                print(f"    [release] Type discovery retry "
                      f"{attempt + 1}/{retries} for theme={theme}: {last_err}")
                time.sleep(wait)

        if not theme_types:
            raise RuntimeError(
                f"Failed to discover release types for theme={theme} "
                f"(release {release_version}): {last_err}")
        types.update(theme_types)
    return sorted(types)


def _clear_release_staging(r2_config, version, staging_dir):
    """Delete staged release files for one theme/type staging dir.

    A failed partitioned COPY can leave per-bucket files behind; a re-run
    may write differently-named files, so stale extras would silently
    duplicate data in the final shards. Clearing first makes retries exact.
    """
    bucket = r2_config["bucket"]
    con = _r2_con(r2_config)
    try:
        files = _retry_transient(lambda: _glob_files(
            con,
            f"s3://{bucket}/{version}/staging/{staging_dir}/**/*.parquet",
        ))()
    finally:
        con.close()
    keys = [f.split(f"s3://{bucket}/", 1)[-1] for f in files]
    if keys:
        print(f"    [release] Clearing {len(keys)} stale staged files "
              f"from {staging_dir}...")
        _delete_r2_keys(r2_config, keys)


def _partition_release_type(theme, type_name, prefix_len, release_version,
                            r2_config, version, limit=None):
    """Stage a single release theme/type to R2, partitioned into 16 buckets.

    PARTITION_BY the first hex char of the prefix: build range jobs then
    read only their own buckets (000-3ff -> buckets 0..3) with real
    pushdown, and per-job disk spill stays proportional to a quarter of one
    theme no matter how large the themes grow.
    """
    staging_dir = f"id-release-{theme}-{type_name}"

    # Skip if this type already completed
    marker = _read_staging_marker(r2_config, version, staging_dir)
    if marker is not None:
        print(f"    [release] {theme}/{type_name} already complete, skipping")
        return (theme, type_name)

    dest = f"s3://{r2_config['bucket']}/{version}/staging/{staging_dir}"

    con = _r2_con(r2_config)
    con.execute("SET memory_limit = '4GB';")
    con.execute("SET s3_region = 'us-west-2';")

    query = _release_id_query_for_type(prefix_len, release_version, theme, type_name, limit=limit)

    def _do_copy():
        con.execute(f"""
            COPY ({query})
            TO '{dest}'
            (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (bucket));
        """)

    _retry_transient(
        _do_copy,
        on_retry=lambda: _clear_release_staging(r2_config, version, staging_dir),
    )()
    con.close()

    _write_staging_marker(r2_config, version, staging_dir, 16)
    return (theme, type_name)


def phase_partition_release_r2(prefix_len, release_version, r2_config, version, limit=None):
    """Stage release themes to R2, one parquet file per type.

    Discovers type= sub-directories under each theme and runs them
    in parallel. Each type writes a single file to its own staging
    path so there are no conflicts.
    """
    _ensure_httpfs_installed()
    print(f"  [release] Discovering release types...")
    # Raises if discovery fails or any theme comes back empty
    types = _discover_release_types(release_version)

    print(f"  [release] Found {len(types)} types: "
          f"{', '.join(f'{t}/{n}' for t, n in types)}")

    t0 = time.time()
    errors = []

    # Capped at 3: each worker connection carries a 4GB DuckDB memory limit,
    # and 6 workers on a 16GB runner plausibly host-OOMed the stage-base job
    # (the runner died with no logs). 3 x 4GB leaves comfortable headroom.
    with ThreadPoolExecutor(max_workers=min(len(types), 3)) as executor:
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
    last_err = "unknown error"
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["wrangler", "r2", "object", "put", r2_key,
                 "--file", str(local_path), "--remote"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                return None
            last_err = result.stderr[:200]
        except subprocess.TimeoutExpired:
            last_err = "upload timed out after 120s"
        if attempt < retries - 1:
            wait = 5 * (2 ** attempt)  # 5s, 10s, 20s
            print(f"    Upload retry {attempt + 1}/{retries} for {r2_key}, waiting {wait}s...")
            time.sleep(wait)
    return last_err


# Expected output shard columns, in order. The worker reads these
# positionally: col 0 must be the 16-byte UUID, cols 1-4 the FLOAT bbox.
EXPECTED_SHARD_COLUMNS = [
    ("id", "FIXED_LEN_BYTE_ARRAY"),
    ("bbox_xmin", "FLOAT"),
    ("bbox_ymin", "FLOAT"),
    ("bbox_xmax", "FLOAT"),
    ("bbox_ymax", "FLOAT"),
]


def _assert_shard_schema(con, path):
    """Assert a written shard's parquet footer matches the worker's layout.

    The worker reads columns positionally, so a silent column reorder or
    type change would break every ID lookup. Raises RuntimeError on
    mismatch; call before writing any _SUCCESS marker.
    """
    rows = con.execute(f"""
        SELECT name, type, type_length
        FROM parquet_schema('{path}')
        WHERE type IS NOT NULL
    """).fetchall()
    actual = [(r[0], str(r[1])) for r in rows]
    if actual != EXPECTED_SHARD_COLUMNS:
        raise RuntimeError(
            f"Shard schema mismatch for {path}: "
            f"got {actual}, expected {EXPECTED_SHARD_COLUMNS}")
    uuid_len = str(rows[0][2])
    if uuid_len != "16":
        raise RuntimeError(
            f"Shard {path}: uuid column type_length {uuid_len} != 16")


def _worker_build_r2_batch(args_tuple):
    """Worker: download one staging prefix locally, write one output shard to R2.

    Downloads the registry staging partition once to local disk, merges with
    release data, sorts by ID, and writes the output shard to R2 (uncompressed).

    Release files are pre-downloaded locally by the caller and passed as local
    paths. All reads are local; only output writes go to R2.
    """
    (staging_prefix, output_prefixes, r2_config, version, release_files,
     row_group_size) = args_tuple

    bucket = r2_config['bucket']
    results = []
    t0 = time.time()

    try:
        con = duckdb.connect()
        con.execute("SET memory_limit = '2GB';")
        con.execute("LOAD httpfs;")
        # Same hung-socket guards as _r2_con: without them a wedged R2
        # read/write in a pool worker hangs the whole build job until the
        # 6h Actions limit (the exact failure the staging phase was
        # hardened against).
        con.execute("SET http_timeout = 120000;")  # ms
        con.execute("SET http_retries = 5;")
        con.execute("SET http_keep_alive = false;")
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

        # Download registry staging partition locally (one R2 read instead of N).
        # Distinguish "no staged data for this prefix" (acceptable: probe via
        # a cheap glob listing) from read errors (retried, then propagated as
        # a per-prefix error so the build phase fails loudly).
        registry_r2 = (
            f"s3://{bucket}/{version}/staging/id-partitioned/"
            f"prefix={staging_prefix}/*.parquet"
        )
        local_registry = f"/tmp/build-reg-{staging_prefix}-{os.getpid()}.parquet"

        has_registry = bool(
            _retry_transient(lambda: _glob_files(con, registry_r2))()
        )
        if has_registry:
            def _download_registry():
                con.execute(f"""
                    COPY (
                        SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax
                        FROM read_parquet('{registry_r2}')
                    ) TO '{local_registry}' (FORMAT PARQUET);
                """)
            _retry_transient(_download_registry)()

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

            # Release: probe local files with prefix filter. These are local
            # reads of files we downloaded ourselves; any error here is real
            # (e.g. corrupt download) and must propagate, not be swallowed.
            for release_path in release_files:
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
            def _do_copy():
                con.execute(f"""
                    COPY (
                        SELECT * FROM ({union_query}) ORDER BY id
                    ) TO '{r2_dest}'
                    (FORMAT PARQUET, COMPRESSION UNCOMPRESSED,
                     ROW_GROUP_SIZE {int(row_group_size)});
                """)
            _retry_transient(_do_copy)()

            # Verify the written footer before this shard can count toward
            # a _SUCCESS marker (the worker reads columns positionally)
            _retry_transient(lambda: _assert_shard_schema(con, r2_dest))()

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


def _discover_release_staging_files(con, r2_config, version):
    """Discover all id-release-* staging files in R2.

    Handles both layouts: bucketed (`id-release-*/bucket=X/*.parquet`, the
    current 16-bucket staging) and legacy single-file
    (`id-release-*/data.parquet`, pre-bucketing versions — kept so patch
    runs against older versions still work). An empty listing is acceptable
    (no release staging yet); transient errors are retried and real errors
    propagate.
    """
    bucket = r2_config["bucket"]
    staging_base = f"s3://{bucket}/{version}/staging"
    bucketed = _retry_transient(lambda: _glob_files(
        con, f"{staging_base}/id-release-*/bucket=*/*.parquet"))()
    legacy = _retry_transient(lambda: _glob_files(
        con, f"{staging_base}/id-release-*/data.parquet"))()
    return bucketed + legacy


def _release_files_for_prefixes(release_files, prefixes):
    """Filter staged release files to the buckets covering `prefixes`.

    Bucketed files (`/bucket=X/`) are kept only when X is the first hex
    char of some target prefix — this is the read-side payoff of bucketed
    staging (a 000-3ff range job reads 4 of 16 buckets per theme). Legacy
    single-file paths carry every bucket and are always kept.
    """
    if not prefixes:
        return list(release_files)
    buckets = {p[0] for p in prefixes}
    kept = []
    for f in release_files:
        if "/bucket=" in f:
            bucket_char = f.split("/bucket=", 1)[-1].split("/", 1)[0]
            if bucket_char not in buckets:
                continue
        kept.append(f)
    return kept


def _discover_staging_prefixes(r2_config, version):
    """List prefixes that have data in registry or release staging, plus release file paths."""
    con = _r2_con(r2_config)

    # Find release staging files (single file per type)
    release_files = _discover_release_staging_files(con, r2_config, version)

    # Get registry prefixes (empty listing is fine; real errors propagate)
    prefixes = set()
    registry_files = _retry_transient(lambda: _glob_files(
        con,
        f"s3://{r2_config['bucket']}/{version}/staging/id-partitioned/prefix=*/*.parquet",
    ))()
    for path in registry_files:
        prefix = path.split("prefix=", 1)[-1].split("/", 1)[0]
        if prefix:
            prefixes.add(prefix)

    # Also include prefixes from release staging files (release-only IDs).
    # One query over the whole file list (bucketed staging has ~16 files
    # per theme/type; per-file queries would multiply round-trips).
    if release_files:
        file_list = ", ".join(f"'{p}'" for p in release_files)

        def _distinct_prefixes():
            return con.execute(f"""
                SELECT DISTINCT prefix
                FROM read_parquet([{file_list}], union_by_name=true)
            """).fetchall()
        rows = _retry_transient(_distinct_prefixes)()
        prefixes.update(r[0] for r in rows if r[0])

    con.close()
    return sorted(prefixes), release_files


def phase_build_r2(prefix_len, r2_config, version, workers, prefixes=None,
                   row_group_size=ROW_GROUP_SIZE):
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

    # Filter to the buckets this job actually needs (bucketed staging), then
    # download and merge everything into ONE local file sorted by
    # (prefix, id). One file means one probe per prefix in the workers
    # instead of one per theme/type, and one sorted dataset to range-scan.
    release_files = _release_files_for_prefixes(release_files, prefixes)
    if release_files:
        print(f"  Release files (after bucket filter): {len(release_files)}")

    local_release_files = []
    if release_files:
        print(f"  Downloading and merging release staging locally...")
        # Only this job's prefixes: a range job needs a quarter of the
        # release data, and the ORDER BY's disk spill is proportional to
        # input — sorting the full addresses theme once filled a runner's
        # 46 GB and killed the job. For explicit prefix lists the bounds
        # are a superset (harmless; workers re-filter per prefix).
        release_where = ""
        if prefixes:
            lo, hi = min(prefixes), max(prefixes)
            release_where = f"WHERE prefix >= '{lo}' AND prefix <= '{hi}'"
        local_path = f"/tmp/build-release-merged-{os.getpid()}.parquet"
        file_list = ", ".join(f"'{p}'" for p in release_files)
        t_dl = time.time()
        dl_con = _r2_con(r2_config)
        dl_con.execute("SET memory_limit = '4GB';")

        # Hard-fail if the release data cannot be downloaded: skipping it
        # would silently drop its IDs from the index. Legacy single-file
        # staging carries a `bucket` column that bucketed files store in
        # the path instead; select the shared columns explicitly.
        def _download_release():
            dl_con.execute(f"""
                COPY (
                    SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                           prefix
                    FROM read_parquet([{file_list}], union_by_name=true)
                    {release_where}
                    ORDER BY prefix, id
                ) TO '{local_path}' (FORMAT PARQUET, COMPRESSION ZSTD);
            """)
        _retry_transient(_download_release)()
        dl_con.close()

        size_mb = os.path.getsize(local_path) / 1024 / 1024
        print(f"    merged: {size_mb:.0f} MB ({time.time() - t_dl:.0f}s)")
        local_release_files.append(local_path)

    # Each prefix maps 1:1 to a staging prefix (same prefix-len)
    work = [
        (p, [p], r2_config, version, local_release_files, row_group_size)
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
    record counts. Returns (prefix, None, 0, None) for each shard found;
    a None count means "exists, count unknown" (real totals come from the
    per-range build _SUCCESS markers — see _sum_build_marker_records).
    """
    print("  Discovering existing R2 shards...")
    con = _r2_con(r2_config)

    bucket = r2_config["bucket"]
    glob_path = f"s3://{bucket}/{version}/id-index/*.parquet"
    # Real listing errors must propagate: silently publishing an empty
    # collection would break every ID lookup for this version.
    shard_files = _retry_transient(lambda: _glob_files(con, glob_path))()

    con.close()

    results = []
    for path in shard_files:
        prefix = path.rsplit("/", 1)[-1].replace(".parquet", "")
        results.append((prefix, None, 0, None))

    print(f"  Found {len(results)} shards")
    return results


def _sum_build_marker_records(r2_config, version):
    """Sum real record counts from per-range build _SUCCESS markers.

    Returns the total, or None if no build markers with record counts exist.
    """
    bucket = r2_config["bucket"]
    con = _r2_con(r2_config)
    try:
        marker_files = _retry_transient(lambda: _glob_files(
            con, f"s3://{bucket}/{version}/staging/build*/_SUCCESS"))()
        total = 0
        found = False
        for marker_path in marker_files:
            def _read_marker(path=marker_path):
                return con.execute(
                    f"SELECT content FROM read_text('{path}')").fetchone()[0]
            content = _retry_transient(_read_marker)()
            data = json.loads(content)
            if "records" in data:
                total += int(data["records"])
                found = True
        return total if found else None
    finally:
        con.close()


def phase_metadata(results, prefix_len, version, release_version, r2_config):
    """Generate id-collection.json and upload to R2."""
    shard_infos = {}
    total_records = 0
    counts_known = True
    errors = []

    for prefix, count, size, err in results:
        if err:
            errors.append((prefix, err))
        elif count is None:
            # Shard exists but per-shard count unknown (metadata-only run)
            shard_infos[prefix] = {}
            counts_known = False
        elif count > 0:
            shard_infos[prefix] = {"record_count": count, "size_bytes": size}
            total_records += count

    # When run standalone, recover the real totals from the per-range build
    # markers instead of fabricating per-shard counts.
    if not counts_known:
        marker_total = _sum_build_marker_records(r2_config, version)
        if marker_total is not None:
            total_records = marker_total
            print(f"  Total records from build markers: {total_records:,}")

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
            "total_size_bytes": sum(s.get("size_bytes", 0) for s in shard_infos.values()),
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

    bucket = r2_config["bucket"]
    tmp = Path("tmp-id-collection.json")
    write_json(tmp, collection)
    err = _upload_to_r2(tmp, f"{bucket}/{version}/id-collection.json")
    tmp.unlink(missing_ok=True)
    if err:
        print(f"  ERROR uploading id-collection.json: {err}")
        sys.exit(1)
    print("  Uploaded id-collection.json to R2")

    # Upload id-meta.json (tiny metadata file for fast worker prefix_len lookup)
    meta = {"prefix_len": prefix_len, "shard_count": len(shard_infos)}
    tmp_meta = Path("tmp-id-meta.json")
    write_json(tmp_meta, meta)
    err = _upload_to_r2(tmp_meta, f"{bucket}/{version}/id-meta.json")
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

    smoke = getattr(args, 'smoke_test', False)
    # Smoke tests must never write into (or leave _SUCCESS markers under) a
    # real version: force a distinct suffix unless one is already present.
    if smoke and "smoke" not in version:
        version = f"{version}-smoke"
        print(f"Smoke test: using isolated version {version}")

    if args.release:
        release_version = args.release
        print(f"Using provided Overture release: {release_version}")
    else:
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
        # Explicit-prefix patch runs never consult run-level markers: the
        # whole point of a patch is to re-stage those prefixes, and the
        # suffix-less full-run marker must be neither read (it would skip
        # the patch) nor written (it would make every later patch a no-op).
        skip = (not args.prefixes and marker_ranges is None
                and _read_staging_marker(r2_config, version, staging_marker_key) is not None)
        if skip:
            print(f"\nStage registry: Skipped ({staging_marker_key} complete for {version})")
        else:
            print(f"\nStage registry: Partition registry")
            t0 = time.time()
            stage_prefixes = smoke_prefixes or range_prefixes
            if smoke_prefixes is not None or args.prefixes:
                # Explicit prefix list (smoke test / patching)
                phase_partition_r2(args.prefix_len, r2_config, version,
                                   prefixes=stage_prefixes)
            elif args.prefix_start and args.prefix_end:
                # Contiguous range (CI matrix job)
                phase_partition_r2(args.prefix_len, r2_config, version,
                                   prefix_start=args.prefix_start,
                                   prefix_end=args.prefix_end)
            else:
                phase_partition_r2(args.prefix_len, r2_config, version)

            # Write markers for explicit ranges (patching) or the computed
            # range. A --prefixes run without --marker-ranges writes NO
            # marker: it only re-staged a subset, so no range is complete.
            if marker_ranges:
                for mr in marker_ranges:
                    mk = f"id-partitioned-{mr}"
                    _write_staging_marker(r2_config, version, mk,
                                          len(stage_prefixes) if stage_prefixes else shard_count)
                    print(f"  Wrote marker: {mk}")
            elif not args.prefixes:
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
        # Explicit-prefix patch builds bypass markers entirely: they must
        # re-run unconditionally, and a suffix-less "build" marker would both
        # block future patch builds and corrupt _sum_build_marker_records
        # (its build*/_SUCCESS glob would double-count the patched prefixes).
        marker = (None if args.prefixes
                  else _read_staging_marker(r2_config, version, build_marker_key))
        if marker is not None:
            print(f"\nBuild: Skipped ({build_marker_key} complete for {version})")
        else:
            print(f"\nBuild: Build parquet shards")
            t0 = time.time()
            results = phase_build_r2(
                args.prefix_len, r2_config, version, args.workers,
                prefixes=range_prefixes,
                row_group_size=getattr(args, "row_group_size", ROW_GROUP_SIZE),
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

            if not args.prefixes:
                _write_staging_marker(r2_config, version, build_marker_key, built,
                                      extra={"records": records})

    # === Metadata ===
    if run_all or "metadata" in phases:
        # An explicitly requested metadata phase always regenerates (it is
        # idempotent and cheap): after a patch build the existing marker
        # describes stale metadata. The marker only short-circuits resumed
        # full-pipeline runs.
        explicit_metadata = "metadata" in phases
        meta_marker = (None if explicit_metadata
                       else _read_staging_marker(r2_config, version, "metadata"))
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

            total_size = sum(s.get("size_bytes", 0) for s in shard_infos.values())
            max_shard = max((s.get("size_bytes", 0) for s in shard_infos.values()), default=0)

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
    p.add_argument("--release",
                   help="Overture release version (default: discover latest from STAC)")
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
    p.add_argument("--row-group-size", type=int, default=ROW_GROUP_SIZE,
                   help=f"Rows per parquet row group in output shards "
                        f"(default: {ROW_GROUP_SIZE}); bounds the cold /id "
                        f"read size. See ROW_GROUP_SIZE doc before changing.")

    args = p.parse_args()
    build_id_index(args)


if __name__ == "__main__":
    main()
