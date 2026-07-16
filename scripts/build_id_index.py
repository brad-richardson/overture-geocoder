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
    python scripts/build_id_index.py --phase dictionaries          # Build global locator dictionary
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
import base64
import hashlib
import json
import multiprocessing
import os
import re
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
HEARTBEAT_INTERVAL_S = 5 * 60
# Dedicated one-type-per-runner staging: a runner owns its DuckDB connection,
# so it may take 8 threads and 10 GB.
RELEASE_STAGE_THREADS = 8
RELEASE_STAGE_MEMORY = "10GB"
# Concurrent staging (all types on one 16 GB runner via a small thread pool):
# each worker connection must fit alongside its siblings. Keep
# CONCURRENT_RELEASE_STAGE_MAX_WORKERS * per-connection GB comfortably under
# the runner's memory so the pool cannot host-OOM.
CONCURRENT_RELEASE_STAGE_THREADS = 4
CONCURRENT_RELEASE_STAGE_MEMORY = "4GB"
CONCURRENT_RELEASE_STAGE_MAX_WORKERS = 3
SHARD_SCHEMA_BATCH_SIZE = 256

# ID-index format v3 appends compact locator IDs after the five v1 positional
# columns. The content-addressed dictionary keeps the authoritative theme,
# type, filename, and historical release strings once per shard set.
ID_INDEX_FORMAT_VERSION = 3

# The bounded smoke registry prefixes are intentionally selected for fast,
# predictable source scans. Historical (path-null) rows are sparse, so those
# prefixes cannot guarantee one is present. This smoke-only row makes the v3
# historical locator path deterministic without changing production builds.
SMOKE_HISTORICAL_ID = "00000000-0000-4000-8000-000000000000"
SMOKE_HISTORICAL_RELEASE = "smoketest-historical"
ID_LOCATOR_MANIFEST = "id-locator-manifest.json"
ID_STAGE_INVENTORY_VERSION = 1
ID_INVENTORY_SET_VERSION = 1
ID_INVENTORY_DIR = "id-inventories"
TYPE_THEME_MAP_VERSION = 1
TYPE_THEME_MAP = {
    "address": "addresses",
    "bathymetry": "base",
    "building": "buildings",
    "building_part": "buildings",
    "connector": "transportation",
    "division": "divisions",
    "division_area": "divisions",
    "division_boundary": "divisions",
    "infrastructure": "base",
    "land": "base",
    "land_cover": "base",
    "land_use": "base",
    "place": "places",
    "segment": "transportation",
    "water": "base",
}


def _format_elapsed(seconds):
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


def _run_with_heartbeat(label, operation, interval=HEARTBEAT_INTERVAL_S):
    """Run a blocking operation with bounded, promptly-stopped CI output."""
    started = time.monotonic()
    stopped = threading.Event()

    def _report():
        while not stopped.wait(interval):
            elapsed = _format_elapsed(time.monotonic() - started)
            print(f"    {label}: still running ({elapsed} elapsed)", flush=True)

    reporter = threading.Thread(
        target=_report, name=f"heartbeat-{label}", daemon=True
    )
    print(f"    {label}: started", flush=True)
    reporter.start()
    try:
        result = operation()
    except BaseException:
        print(
            f"    {label}: failed after "
            f"{_format_elapsed(time.monotonic() - started)}",
            flush=True,
        )
        raise
    finally:
        stopped.set()
        reporter.join(timeout=max(1, min(interval, 5)))
    print(
        f"    {label}: completed in "
        f"{_format_elapsed(time.monotonic() - started)}",
        flush=True,
    )
    return result

# Rows per parquet row group in output shards. Every cold /id lookup
# range-reads one full row group, so this bounds the cold-read size.
# 50k chosen from the 2026-07-02 rowgroup_experiment.py run against live
# shards: 1.67 MB cold read vs 3.24 MB at the old 100k default, p50
# ~198 ms vs ~245 ms — and its ~8.7 KB footer leaves ~3.7x growth headroom
# inside the worker's single 32 KB suffix read (FOOTER_SUFFIX_SIZE,
# stac.rs). 25k measured the same p50 with smaller reads (0.88 MB) but
# only ~2x footer headroom; not worth camping near the window for zero
# measured latency gain. Past the window every lookup pays a second
# round-trip.
ROW_GROUP_SIZE = 50_000


def get_version(suffix="0"):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{date}.{suffix}"


def _type_theme_metadata():
    """Return the deterministic, versioned type-to-theme contract."""
    return {
        "version": TYPE_THEME_MAP_VERSION,
        "types": dict(sorted(TYPE_THEME_MAP.items())),
    }


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


def _write_staging_marker(r2_config, version, staging_dir, partition_count,
                          extra=None, format_version=ID_INDEX_FORMAT_VERSION):
    """Write a _SUCCESS marker to R2 staging after a completed phase."""
    marker = {
        "status": "complete",
        "partitions": partition_count,
        "format_version": format_version,
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
        raise RuntimeError(f"Failed to write required staging marker {r2_key}: {err}")


def _marker_is_current(marker):
    """Only v3 markers may resume a v3 pipeline run."""
    return (
        marker is not None
        and marker.get("status") == "complete"
        and marker.get("format_version") == ID_INDEX_FORMAT_VERSION
    )


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
        # Explicit prefix list (smoke test / patching). The LIKE filters
        # select the prefixes, but OR-of-LIKEs does not reliably push down
        # to parquet zone maps — five scattered prefixes once forced a full
        # registry scan that could not finish inside the sub-range watchdog
        # and retried itself to death. Bound the scan with plain id-range
        # predicates (min prefix .. max prefix inclusive), which always
        # push down on the id-sorted registry; the bound is tight when the
        # prefixes cluster and merely harmless when they are scattered.
        like_filters = " OR ".join(f"id LIKE '{p}%'" for p in prefixes)
        lo = min(prefixes)
        upper_idx = int(max(prefixes), 16) + 1
        cond = f"({like_filters}) AND id >= '{lo}'"
        if upper_idx < 16 ** prefix_len:
            upper = format(upper_idx, f'0{prefix_len}x')
            cond += f" AND id < '{upper}'"
        sub_ranges = [(cond, f"{len(prefixes)} explicit prefixes",
                       {"prefixes": list(prefixes)})]
        return sub_ranges, sub_ranges[0][1]

    # A single-sided range is a caller mistake (e.g. a dispatch that set only
    # --prefix-start); silently widening it to the full prefix space would
    # stage the entire registry under one range job.
    if bool(prefix_start) != bool(prefix_end):
        raise ValueError(
            "registry staging requires both prefix_start and prefix_end "
            "(or neither, for the full prefix space)")
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


def _registry_id_query(prefix_len, sub_filter):
    """Build the registry staging query used to derive v3 compact IDs.

    Registry ``path`` is authoritative for current-release membership. A null
    path deliberately produces null feature_type/filename while retaining
    last_seen_release for historical context.
    """
    return f"""
        SELECT
            id::UUID as id,
            bbox.xmin::FLOAT as bbox_xmin, bbox.ymin::FLOAT as bbox_ymin,
            bbox.xmax::FLOAT as bbox_xmax, bbox.ymax::FLOAT as bbox_ymax,
            NULLIF(regexp_extract(path, '(^|/)type=([^/]+)/', 2), '')::VARCHAR
                as feature_type,
            NULLIF(regexp_extract(path, '([^/]+)$', 1), '')::VARCHAR as filename,
            last_seen::VARCHAR as last_seen_release,
            true::BOOLEAN as registry_member,
            NULLIF(regexp_extract(path, '(^|/)theme=([^/]+)/', 2), '')::VARCHAR
                as source_theme,
            lower(left(replace(id, '-', ''), {prefix_len})) as prefix
        FROM read_parquet('{REGISTRY_S3}*')
        WHERE id IS NOT NULL AND bbox IS NOT NULL AND bbox.xmin IS NOT NULL
        AND {sub_filter}
    """


def _smoke_historical_registry_query():
    """Return one explicit historical row for the bounded v3 smoke only."""
    return f"""
        SELECT
            '{SMOKE_HISTORICAL_ID}'::UUID as id,
            0::FLOAT as bbox_xmin, 0::FLOAT as bbox_ymin,
            0::FLOAT as bbox_xmax, 0::FLOAT as bbox_ymax,
            NULL::VARCHAR as feature_type,
            NULL::VARCHAR as filename,
            '{SMOKE_HISTORICAL_RELEASE}'::VARCHAR as last_seen_release,
            true::BOOLEAN as registry_member,
            NULL::VARCHAR as source_theme
    """


def _write_smoke_historical_registry_row(con, dest, prefix_len):
    """Publish the deterministic historical sentinel into smoke staging."""
    prefix = SMOKE_HISTORICAL_ID.replace("-", "")[:prefix_len]
    path = f"{dest}/prefix={prefix}/smoke-historical.parquet"
    query = _smoke_historical_registry_query()
    con.execute(f"""
        COPY ({query}) TO '{path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, OVERWRITE_OR_IGNORE true);
    """)


def phase_partition_r2(prefix_len, r2_config, version,
                       prefixes=None, prefix_start=None, prefix_end=None,
                       smoke_history=False):
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
                query = _registry_id_query(prefix_len, sub_filter)
                con.execute(f"""
                    COPY ({query}) TO '{dest}'
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

        _run_with_heartbeat(
            f"[registry] sub-range {sub_label} COPY",
            _retry_transient(_do_copy, on_retry=_clear_partial),
        )
        print(f"  [registry]   sub-range {sub_label} done")

    if smoke_history:
        _retry_transient(
            lambda: _write_smoke_historical_registry_row(
                con, dest, prefix_len
            )
        )()
        print("  [registry]   added smoke-only historical v3 sentinel")
    con.close()

    elapsed = time.time() - t0
    mins, secs = divmod(int(elapsed), 60)
    print(f"  [registry] Done ({label}) in {mins}m{secs:02d}s")


def _registry_inventory_scope(
    prefix_len, prefixes=None, prefix_start=None, prefix_end=None
):
    if prefixes is not None:
        normalized = sorted(set(prefixes))
        if not normalized:
            raise RuntimeError("Registry inventory scope has no prefixes")
        return {"prefixes": normalized}
    start = prefix_start or ("0" * prefix_len)
    end = prefix_end or ("f" * prefix_len)
    return {"prefix_start": start, "prefix_end": end}


def _registry_staged_files_for_scope(con, r2_config, version, scope):
    files = _glob_files(
        con,
        f"s3://{r2_config['bucket']}/{version}/staging/"
        "id-partitioned/prefix=*/*.parquet",
    )
    if "prefixes" in scope:
        wanted = set(scope["prefixes"])
    else:
        lo, hi = int(scope["prefix_start"], 16), int(scope["prefix_end"], 16)
        width = len(scope["prefix_start"])
        wanted = {format(value, f"0{width}x") for value in range(lo, hi + 1)}
    selected = [
        path
        for path in files
        if path.split("prefix=", 1)[-1].split("/", 1)[0] in wanted
    ]
    found = {path.split("prefix=", 1)[-1].split("/", 1)[0] for path in selected}
    missing = sorted(wanted - found)
    # Empty prefixes are valid in theory, so completeness is ultimately tied
    # to the stage job's scope marker rather than requiring one file per prefix.
    # In the real 3-hex registry every prefix is populated; smoke tests likewise
    # expect their contiguous sample prefixes to exist.
    if missing:
        raise RuntimeError(
            f"Registry inventory scope is missing staged prefixes: {missing[:10]}"
        )
    return sorted(selected)


def _build_registry_stage_inventory(r2_config, version, release_version, scope):
    """Discover only path-null historical releases for one completed range."""
    con = _r2_con(r2_config)
    try:
        files = _registry_staged_files_for_scope(con, r2_config, version, scope)
        file_sql = ", ".join(f"'{path}'" for path in files)
        rows = _retry_transient(
            lambda: con.execute(f"""
            SELECT DISTINCT last_seen_release
            FROM read_parquet([{file_sql}], union_by_name=true)
            WHERE filename IS NULL AND last_seen_release IS NOT NULL
            ORDER BY last_seen_release
        """).fetchall()
        )()
    finally:
        con.close()
    payload = _make_stage_inventory(
        "registry_range",
        release_version,
        scope,
        last_seen_releases=[row[0] for row in rows],
    )
    return _publish_stage_inventory(r2_config, version, payload)


def _release_id_query_for_type(
    prefix_len, release_version, theme, type_name, limit=None, prefixes=None
):
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
    type_literal = type_name.replace("'", "''")
    release_literal = release_version.replace("'", "''")
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    prefix_expression = f"lower(left(replace(id, '-', ''), {prefix_len}))"
    prefix_filter = ""
    if prefixes is not None:
        normalized = sorted(set(prefixes))
        if not normalized:
            raise ValueError("release prefix filter cannot be empty")
        expected_width = prefix_len
        if any(
            len(prefix) != expected_width
            or any(char not in "0123456789abcdef" for char in prefix)
            for prefix in normalized
        ):
            raise ValueError("release prefix filter contains an invalid prefix")
        prefix_values = ", ".join(f"'{prefix}'" for prefix in normalized)
        prefix_filter = f"AND {prefix_expression} IN ({prefix_values})"
    return f"""
        SELECT
            id::UUID as id,
            bbox.xmin::FLOAT as bbox_xmin, bbox.ymin::FLOAT as bbox_ymin,
            bbox.xmax::FLOAT as bbox_xmax, bbox.ymax::FLOAT as bbox_ymax,
            '{type_literal}'::VARCHAR as feature_type,
            NULLIF(regexp_extract(filename, '([^/]+)$', 1), '')::VARCHAR as filename,
            '{release_literal}'::VARCHAR as last_seen_release,
            false::BOOLEAN as registry_member,
            '{theme.replace("'", "''")}'::VARCHAR as source_theme,
            {prefix_expression} as prefix,
            lower(left(replace(id, '-', ''), 1)) as bucket
        FROM read_parquet({source}, union_by_name=true, filename=true)
        WHERE id IS NOT NULL AND bbox IS NOT NULL AND bbox.xmin IS NOT NULL
        {prefix_filter}
        {limit_clause}
    """


def _r2_release_staging_exists(r2_config, version):
    """Check if completed release staging data exists for this version."""
    return _marker_is_current(
        _read_staging_marker(r2_config, version, "id-release"))


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
                            r2_config, version, limit=None, prefixes=None,
                            memory_limit=RELEASE_STAGE_MEMORY,
                            threads=RELEASE_STAGE_THREADS):
    """Stage a single release theme/type to R2, partitioned into 16 buckets.

    PARTITION_BY the first hex char of the prefix: build range jobs then
    read only their own buckets (000-3ff -> buckets 0..3) with real
    pushdown, and per-job disk spill stays proportional to a quarter of one
    theme no matter how large the themes grow.
    """
    expected_theme = TYPE_THEME_MAP.get(type_name)
    if expected_theme != theme:
        raise RuntimeError(
            f"Unsupported release theme/type {theme}/{type_name}; "
            f"type map expects {expected_theme!r}")
    staging_dir = f"id-release-{theme}-{type_name}"

    # Skip if this type already completed
    marker = _read_staging_marker(r2_config, version, staging_dir)
    if _marker_is_current(marker):
        print(f"    [release] {theme}/{type_name} already complete, skipping")
        return (theme, type_name)

    dest = f"s3://{r2_config['bucket']}/{version}/staging/{staging_dir}"

    phase_durations = {}

    def _timed(label, operation):
        started = time.monotonic()
        result = _run_with_heartbeat(
            f"[release] {theme}/{type_name} {label}", operation
        )
        phase_durations[label] = time.monotonic() - started
        return result

    con = _r2_con(r2_config)
    try:
        # Tuning is caller-supplied so the dedicated one-type-per-runner path
        # can claim the whole runner (10 GB / 8 threads) while the concurrent
        # path stays within a shared runner's memory: several connections run
        # at once, so each takes a fraction of RAM (see
        # CONCURRENT_RELEASE_STAGE_* and phase_partition_release_r2).
        con.execute(f"SET memory_limit = '{memory_limit}';")
        con.execute(f"SET threads = {threads};")
        con.execute("SET preserve_insertion_order = false;")
        con.execute("SET s3_region = 'us-west-2';")
        settings = con.execute(
            "SELECT current_setting('threads'), current_setting('memory_limit')"
        ).fetchone()
        print(
            f"    [release] {theme}/{type_name} tuning: "
            f"threads={settings[0]}, memory_limit={settings[1]}",
            flush=True,
        )

        source_files_before = _timed(
            "source inventory (before)",
            lambda: _release_type_source_files(
                con, release_version, theme, type_name
            ),
        )

        query = _release_id_query_for_type(
            prefix_len, release_version, theme, type_name,
            limit=limit, prefixes=prefixes,
        )

        # Re-run safety: a cancelled run can leave marker-less partial staging
        # (including legacy single-file data.parquet from older code) that the
        # build's dual-layout discovery would double-count alongside fresh
        # buckets. Clear anything unmarked before writing.
        _timed(
            "stale-output cleanup",
            lambda: _clear_release_staging(r2_config, version, staging_dir),
        )

        def _do_copy():
            con.execute(f"""
                COPY ({query})
                TO '{dest}'
                (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (bucket));
            """)

        _timed(
            "S3-to-R2 COPY",
            _retry_transient(
                _do_copy,
                on_retry=lambda: _clear_release_staging(
                    r2_config, version, staging_dir
                ),
            ),
        )
        source_files_after = _timed(
            "source inventory (after)",
            lambda: _release_type_source_files(
                con, release_version, theme, type_name
            ),
        )
    finally:
        con.close()

    if source_files_before != source_files_after:
        raise RuntimeError(
            f"Current-release file inventory changed while staging {theme}/{type_name}"
        )
    inventory = _make_stage_inventory(
        "release_type",
        release_version,
        {"theme": theme, "feature_type": type_name},
        source_files=source_files_after,
    )
    inventory_reference = _timed(
        "inventory publication",
        lambda: _publish_stage_inventory(r2_config, version, inventory),
    )

    _timed(
        "success marker publication",
        lambda: _write_staging_marker(
            r2_config,
            version,
            staging_dir,
            16,
            extra={"locator_inventory": inventory_reference},
        ),
    )
    timing_summary = ", ".join(
        f"{label}={_format_elapsed(duration)}"
        for label, duration in phase_durations.items()
    )
    print(
        f"    [release] {theme}/{type_name} timing: {timing_summary}",
        flush=True,
    )
    return (theme, type_name)


def phase_partition_release_type_r2(
    theme, type_name, prefix_len, release_version, r2_config, version,
    limit=None, prefixes=None,
):
    """Stage exactly one discovered release type on its dedicated runner."""
    discovered = set(_discover_release_types(release_version))
    requested = (theme, type_name)
    if requested not in discovered:
        raise RuntimeError(
            f"Requested release type {theme}/{type_name} was not discovered in "
            f"release {release_version}"
        )
    return _partition_release_type(
        theme, type_name, prefix_len, release_version, r2_config, version,
        limit=limit, prefixes=prefixes,
    )


def phase_finalize_release_r2(release_version, r2_config, version):
    """Prove the exact discovered per-type marker set before fan-in."""
    types = _discover_release_types(release_version)
    expected = {f"id-release-{theme}-{type_name}" for theme, type_name in types}
    staging_base = f"s3://{r2_config['bucket']}/{version}/staging"
    con = _r2_con(r2_config)
    try:
        marker_paths = _retry_transient(lambda: _glob_files(
            con,
            f"{staging_base}/id-release-*/_SUCCESS",
        ))()
        # A _SUCCESS marker is only trustworthy if its staged data still
        # exists: a marker whose partitioned/legacy files were cleared or
        # never landed would otherwise pass fan-in and silently drop a type.
        staged_paths = _retry_transient(lambda: _glob_files(
            con,
            f"{staging_base}/id-release-*/bucket=*/*.parquet",
        ))() + _retry_transient(lambda: _glob_files(
            con,
            f"{staging_base}/id-release-*/data.parquet",
        ))()
    finally:
        con.close()
    actual = {
        path.split("/staging/", 1)[-1].rsplit("/_SUCCESS", 1)[0]
        for path in marker_paths
    }
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise RuntimeError(
            "Release staging marker set differs from discovered release types: "
            f"missing={missing}, extra={extra}"
        )
    staged_dirs = {
        path.split("/staging/", 1)[-1].split("/", 1)[0]
        for path in staged_paths
    }
    missing_data = sorted(expected - staged_dirs)
    if missing_data:
        raise RuntimeError(
            "Release staging markers present but no staged files for: "
            f"{missing_data}"
        )

    release_types = []
    for theme, type_name in types:
        staging_dir = f"id-release-{theme}-{type_name}"
        marker = _read_staging_marker(r2_config, version, staging_dir)
        if not _marker_is_current(marker):
            raise RuntimeError(
                f"Required release staging marker {staging_dir} is missing/stale"
            )
        if marker.get("partitions") != 16:
            raise RuntimeError(
                f"Release marker {staging_dir} has invalid partition count"
            )
        if not isinstance(marker.get("locator_inventory"), dict):
            raise RuntimeError(
                f"Release marker {staging_dir} has no locator inventory"
            )
        release_types.append(f"{theme}/{type_name}")

    _write_staging_marker(
        r2_config,
        version,
        "id-release",
        len(types),
        extra={"release_types": release_types},
    )
    print(
        f"  [release] Verified and finalized {len(types)} types: "
        f"{', '.join(release_types)}",
        flush=True,
    )


def phase_partition_release_r2(
    prefix_len, release_version, r2_config, version, limit=None, prefixes=None
):
    """Stage release themes to R2, one parquet file per type.

    Discovers type= sub-directories under each theme and runs them
    in parallel. Each type writes a single file to its own staging
    path so there are no conflicts.
    """
    _ensure_httpfs_installed()
    print("  [release] Discovering release types...")
    # Raises if discovery fails or any theme comes back empty
    types = _discover_release_types(release_version)

    print(f"  [release] Found {len(types)} types: "
          f"{', '.join(f'{t}/{n}' for t, n in types)}")

    t0 = time.time()
    errors = []

    # All types share one runner here, so each connection is given a small
    # memory limit and the pool is capped so the concurrent footprint
    # (CONCURRENT_RELEASE_STAGE_MAX_WORKERS x CONCURRENT_RELEASE_STAGE_MEMORY)
    # stays well under the runner's RAM. An earlier 6-worker/10 GB combination
    # host-OOMed the stage-base job (the runner died with no logs).
    max_workers = min(len(types), CONCURRENT_RELEASE_STAGE_MAX_WORKERS)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for theme, type_name in types:
            f = executor.submit(
                _partition_release_type,
                theme, type_name, prefix_len, release_version,
                r2_config, version, limit=limit, prefixes=prefixes,
                memory_limit=CONCURRENT_RELEASE_STAGE_MEMORY,
                threads=CONCURRENT_RELEASE_STAGE_THREADS,
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
    phase_finalize_release_r2(release_version, r2_config, version)


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


def _file_sha256(path):
    """Return the SHA-256 of an exact output object before it is uploaded."""
    digest = hashlib.sha256()
    with open(path, "rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_md5_hex(path):
    """Return the hex MD5 of an output object.

    R2 stores this as the single-part object ETag, so recording it lets the
    finalizer bind each catalogued shard to the exact bytes the producer
    uploaded (see _upload_id_shard_to_r2's Content-MD5 header).
    """
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _upload_id_shard_to_r2(local_path, r2_config, version, prefix, retries=3):
    """Upload one ID shard with an R2-validated content digest."""
    md5 = hashlib.md5(usedforsecurity=False)
    with open(local_path, "rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            md5.update(chunk)
    content_md5 = base64.b64encode(md5.digest()).decode("ascii")
    env = os.environ.copy()
    env["AWS_ACCESS_KEY_ID"] = r2_config["key_id"]
    env["AWS_SECRET_ACCESS_KEY"] = r2_config["secret"]
    endpoint = f"https://{r2_config['endpoint']}"
    key = f"{version}/id-index/{prefix}.parquet"
    last_err = "unknown error"
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["aws", "s3api", "put-object", "--bucket", r2_config["bucket"],
                 "--key", key, "--body", str(local_path),
                 "--content-md5", content_md5,
                 "--endpoint-url", endpoint, "--region", "auto"],
                capture_output=True, text=True, timeout=600, env=env,
            )
            if result.returncode == 0:
                return None
            last_err = f"{result.stderr or ''} {result.stdout or ''}".strip()[:300]
        except subprocess.TimeoutExpired:
            last_err = "ID shard upload timed out after 600s"
        except FileNotFoundError:
            return "aws CLI is required for checksum-validated ID shard uploads"
        if attempt < retries - 1:
            time.sleep(5 * (2 ** attempt))
    return last_err


def _read_r2_json(r2_config, version, filename, expected_sha256=None,
                  expected_size_bytes=None, retries=3):
    """Read one required versioned JSON object; absence and corruption fail."""
    r2_key = f"{r2_config['bucket']}/{version}/{filename}"
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
                raw = result.stdout.encode("utf-8")
                if len(raw) > 1024 * 1024:
                    raise RuntimeError(f"Required JSON {r2_key} exceeds 1 MiB")
                if expected_size_bytes is not None and len(raw) != expected_size_bytes:
                    raise RuntimeError(
                        f"Size mismatch for {r2_key}: {len(raw)} "
                        f"!= {expected_size_bytes}")
                actual_sha256 = hashlib.sha256(raw).hexdigest()
                if expected_sha256 is not None and actual_sha256 != expected_sha256:
                    raise RuntimeError(
                        f"Checksum mismatch for {r2_key}: {actual_sha256} "
                        f"!= {expected_sha256}")
                try:
                    return json.loads(result.stdout)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"Invalid JSON in {r2_key}: {exc}") from exc
            last_err = f"{result.stderr or ''} {result.stdout or ''}".strip()[:300]
        if attempt < retries - 1:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Failed to read required {r2_key}: {last_err}")


def _read_optional_r2_json(r2_config, version, filename, retries=3):
    """Read a version-root JSON object, returning None only for true absence."""
    r2_key = f"{r2_config['bucket']}/{version}/{filename}"
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
                raw = result.stdout.encode("utf-8")
                if len(raw) > 1024 * 1024:
                    raise RuntimeError(f"Required JSON {r2_key} exceeds 1 MiB")
                try:
                    return json.loads(result.stdout)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"Invalid JSON in {r2_key}: {exc}") from exc
            err_text = f"{result.stderr or ''} {result.stdout or ''}".strip()
            if any(value in err_text.lower() for value in _R2_ABSENT_MARKERS):
                return None
            last_err = err_text[:300]
        if attempt < retries - 1:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Failed to read optional {r2_key}: {last_err}")


def _publish_stage_inventory(r2_config, version, payload):
    """Publish an immutable inventory before its mutable stage marker."""
    reference, raw = _stage_inventory_reference(payload)
    relative_href = reference["href"].removeprefix("./")
    tmp = Path(f"tmp-id-inventory-{os.getpid()}-{time.time_ns()}.json")
    tmp.write_bytes(raw)
    try:
        err = _upload_to_r2(tmp, f"{r2_config['bucket']}/{version}/{relative_href}")
    finally:
        tmp.unlink(missing_ok=True)
    if err:
        raise RuntimeError(f"Failed to upload locator inventory: {err}")
    return reference


def _publish_inventory_set(r2_config, version, payload):
    """Publish the permanent, auditable set of stage inventory references."""
    reference, raw = _inventory_set_reference(payload)
    relative_href = reference["href"].removeprefix("./")
    tmp = Path(f"tmp-id-inventory-set-{os.getpid()}-{time.time_ns()}.json")
    tmp.write_bytes(raw)
    try:
        err = _upload_to_r2(tmp, f"{r2_config['bucket']}/{version}/{relative_href}")
    finally:
        tmp.unlink(missing_ok=True)
    if err:
        raise RuntimeError(f"Failed to upload locator inventory set: {err}")
    return reference


def _load_inventory_set(r2_config, version, reference, release_version):
    reference = _validate_inventory_set_reference(reference)
    href = reference["href"].removeprefix("./")
    payload = _validate_inventory_set(
        _read_r2_json(
            r2_config,
            version,
            href,
            expected_sha256=reference["sha256"],
            expected_size_bytes=reference["size_bytes"],
        ),
        release_version,
    )
    expected, _ = _inventory_set_reference(payload)
    if expected != reference:
        raise RuntimeError("Locator inventory set payload/reference mismatch")
    return payload


def _load_stage_inventory(r2_config, version, reference, release_version):
    reference = _validate_stage_inventory_reference(reference)
    href = reference["href"].removeprefix("./")
    payload = _validate_stage_inventory(
        _read_r2_json(
            r2_config,
            version,
            href,
            expected_sha256=reference["sha256"],
            expected_size_bytes=reference["size_bytes"],
        ),
        release_version,
    )
    expected, _ = _stage_inventory_reference(payload)
    if expected != reference:
        raise RuntimeError("Locator inventory payload/reference mismatch")
    return payload


def _release_source_pattern(release_version, theme, feature_type):
    return (
        f"{RELEASE_S3}{release_version}/theme={theme}/type={feature_type}/**/*.parquet"
    )


def _release_type_source_files(con, release_version, theme, feature_type):
    expected_theme = TYPE_THEME_MAP.get(feature_type)
    if expected_theme != theme:
        raise RuntimeError(
            f"Unsupported release theme/type {theme}/{feature_type}; "
            f"type map expects {expected_theme!r}"
        )
    paths = _glob_files(
        con, _release_source_pattern(release_version, theme, feature_type)
    )
    if not paths:
        raise RuntimeError(f"No current-release files for {theme}/{feature_type}")
    tuples = []
    for path in paths:
        filename = path.rsplit("/", 1)[-1]
        if not _validate_source_filename(filename):
            raise RuntimeError(f"Invalid current-release source path {path!r}")
        tuples.append((theme, feature_type, filename))
    if len(set(tuples)) != len(tuples):
        raise RuntimeError(
            f"Duplicate current-release basenames for {theme}/{feature_type}"
        )
    return sorted(tuples)


def _discover_current_release_source_files(con, release_version, retries=3):
    """Return a stable exact S3 file inventory for every mapped type.

    Two identical complete listings are required. A transient partial listing
    can therefore fail or retry, but can never silently shrink the dictionary.
    """
    previous = None
    last_error = None
    for attempt in range(retries):
        try:
            current = []
            for feature_type, theme in sorted(TYPE_THEME_MAP.items()):
                current.extend(
                    _release_type_source_files(
                        con, release_version, theme, feature_type
                    )
                )
            current = sorted(set(current))
        except Exception as exc:
            last_error = exc
            previous = None
        else:
            if previous == current:
                return current
            previous = current
            last_error = RuntimeError("Current-release inventory was not stable")
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(
        f"Failed to obtain two stable current-release inventories: {last_error}"
    )


def _canonical_json_bytes(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _make_stage_inventory(
    kind, release_version, scope, source_files=(), last_seen_releases=()
):
    """Create one canonical, scope-bound staging inventory.

    Inventories intentionally contain only low-cardinality dictionary values,
    never IDs or feature rows. Registry ranges contribute historical releases;
    release/current-file scopes contribute authoritative source tuples.
    """
    if kind not in {"registry_range", "release_type", "current_release_files"}:
        raise RuntimeError(f"Invalid locator inventory kind {kind!r}")
    if not isinstance(release_version, str) or not release_version:
        raise RuntimeError("Invalid locator inventory release")
    if not isinstance(scope, dict):
        raise RuntimeError("Invalid locator inventory scope")
    if kind == "registry_range":
        allowed = ({"prefix_start", "prefix_end"}, {"prefixes"})
        if set(scope) not in allowed or source_files:
            raise RuntimeError("Invalid registry inventory scope/values")
        if "prefixes" in scope:
            prefixes = scope["prefixes"]
            if (
                not isinstance(prefixes, list)
                or not prefixes
                or prefixes != sorted(set(prefixes))
            ):
                raise RuntimeError("Invalid registry inventory prefixes")
        else:
            if scope["prefix_start"] > scope["prefix_end"]:
                raise RuntimeError("Invalid registry inventory range")
    elif kind == "release_type":
        if set(scope) != {"theme", "feature_type"} or last_seen_releases:
            raise RuntimeError("Invalid release-type inventory scope/values")
    else:
        if set(scope) != {"universe"} or scope["universe"] != "all_mapped_types":
            raise RuntimeError("Invalid current-release inventory scope")
        if last_seen_releases:
            raise RuntimeError("Current-release inventory cannot contain history")

    normalized_files = sorted(set(tuple(value) for value in source_files))
    normalized_releases = sorted(set(last_seen_releases))
    # Reuse the production dictionary validator for theme/type/basename and
    # historical release shape. The result is discarded; this is validation.
    _make_locator_dictionary(
        normalized_files,
        normalized_releases,
        release_version,
        input_inventory_set_sha256=None,
    )
    if kind == "release_type":
        expected = (scope["theme"], scope["feature_type"])
        if any(
            (theme, feature_type) != expected
            for theme, feature_type, _ in normalized_files
        ):
            raise RuntimeError("Release inventory tuple escapes its scope")
    return {
        "format_version": ID_INDEX_FORMAT_VERSION,
        "inventory_version": ID_STAGE_INVENTORY_VERSION,
        "kind": kind,
        "overture_release": release_version,
        "scope": scope,
        "source_files": [
            {"theme": theme, "feature_type": feature_type, "filename": filename}
            for theme, feature_type, filename in normalized_files
        ],
        "last_seen_releases": normalized_releases,
        "source_files_count": len(normalized_files),
        "last_seen_releases_count": len(normalized_releases),
    }


def _validate_stage_inventory(payload, release_version=None):
    if not isinstance(payload, dict) or set(payload) != {
        "format_version",
        "inventory_version",
        "kind",
        "overture_release",
        "scope",
        "source_files",
        "last_seen_releases",
        "source_files_count",
        "last_seen_releases_count",
    }:
        raise RuntimeError("Invalid locator stage inventory fields")
    files = payload.get("source_files")
    releases = payload.get("last_seen_releases")
    if not isinstance(files, list) or not isinstance(releases, list):
        raise RuntimeError("Invalid locator stage inventory arrays")
    if (
        release_version is not None
        and payload.get("overture_release") != release_version
    ):
        raise RuntimeError("Locator stage inventory release mismatch")
    rebuilt = _make_stage_inventory(
        payload.get("kind"),
        payload.get("overture_release"),
        payload.get("scope"),
        [
            (item.get("theme"), item.get("feature_type"), item.get("filename"))
            for item in files
            if isinstance(item, dict)
        ],
        releases,
    )
    if rebuilt != payload:
        raise RuntimeError("Invalid locator stage inventory content/order")
    return payload


def _stage_inventory_reference(payload):
    payload = _validate_stage_inventory(payload)
    raw = _canonical_json_bytes(payload)
    if len(raw) > 1024 * 1024:
        raise RuntimeError("Locator stage inventory exceeds 1 MiB")
    sha256 = hashlib.sha256(raw).hexdigest()
    scope_sha = hashlib.sha256(_canonical_json_bytes(payload["scope"])).hexdigest()[:16]
    href = f"{ID_INVENTORY_DIR}/{payload['kind']}-{scope_sha}-{sha256}.json"
    return {
        "href": f"./{href}",
        "sha256": sha256,
        "size_bytes": len(raw),
        "kind": payload["kind"],
        "scope": payload["scope"],
    }, raw


def _validate_stage_inventory_reference(reference):
    if not isinstance(reference, dict) or set(reference) != {
        "href",
        "sha256",
        "size_bytes",
        "kind",
        "scope",
    }:
        raise RuntimeError("Invalid locator inventory reference fields")
    sha256 = reference.get("sha256")
    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or any(char not in "0123456789abcdef" for char in sha256)
    ):
        raise RuntimeError("Invalid locator inventory reference SHA")
    scope_sha = hashlib.sha256(
        _canonical_json_bytes(reference.get("scope"))
    ).hexdigest()[:16]
    expected = f"./{ID_INVENTORY_DIR}/{reference.get('kind')}-{scope_sha}-{sha256}.json"
    if reference.get("href") != expected:
        raise RuntimeError("Invalid locator inventory reference href")
    size = reference.get("size_bytes")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 < size <= 1024 * 1024
    ):
        raise RuntimeError("Invalid locator inventory reference size")
    return reference


def _inventory_set_sha256(references):
    normalized = sorted(
        (
            _validate_stage_inventory_reference(dict(reference))
            for reference in references
        ),
        key=lambda item: item["href"],
    )
    if len({item["href"] for item in normalized}) != len(normalized):
        raise RuntimeError("Duplicate locator inventory reference")
    return hashlib.sha256(_canonical_json_bytes(normalized)).hexdigest()


def _make_inventory_set(references, release_version):
    normalized = sorted(
        (
            _validate_stage_inventory_reference(dict(reference))
            for reference in references
        ),
        key=lambda item: item["href"],
    )
    references_sha256 = _inventory_set_sha256(normalized)
    return {
        "format_version": ID_INDEX_FORMAT_VERSION,
        "inventory_set_version": ID_INVENTORY_SET_VERSION,
        "overture_release": release_version,
        "inventories": normalized,
        "inventories_count": len(normalized),
        "inventory_references_sha256": references_sha256,
    }


def _validate_inventory_set(payload, release_version=None):
    if not isinstance(payload, dict) or set(payload) != {
        "format_version",
        "inventory_set_version",
        "overture_release",
        "inventories",
        "inventories_count",
        "inventory_references_sha256",
    }:
        raise RuntimeError("Invalid locator inventory set fields")
    references = payload.get("inventories")
    if not isinstance(references, list):
        raise RuntimeError("Invalid locator inventory set references")
    rebuilt = _make_inventory_set(references, payload.get("overture_release"))
    if payload != rebuilt:
        raise RuntimeError("Invalid locator inventory set content/order")
    if release_version is not None and payload["overture_release"] != release_version:
        raise RuntimeError("Locator inventory set release mismatch")
    return payload


def _inventory_set_reference(payload):
    payload = _validate_inventory_set(payload)
    raw = _canonical_json_bytes(payload)
    if len(raw) > 1024 * 1024:
        raise RuntimeError("Locator inventory set exceeds 1 MiB")
    sha256 = hashlib.sha256(raw).hexdigest()
    return {
        "href": f"./{ID_INVENTORY_DIR}/inventory-set-{sha256}.json",
        "sha256": sha256,
        "size_bytes": len(raw),
        "inventories_count": payload["inventories_count"],
        "inventory_references_sha256": payload["inventory_references_sha256"],
    }, raw


def _validate_inventory_set_reference(reference):
    if not isinstance(reference, dict) or set(reference) != {
        "href",
        "sha256",
        "size_bytes",
        "inventories_count",
        "inventory_references_sha256",
    }:
        raise RuntimeError("Invalid locator inventory set reference fields")
    sha256 = reference.get("sha256")
    references_sha256 = reference.get("inventory_references_sha256")
    if any(
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
        for value in (sha256, references_sha256)
    ):
        raise RuntimeError("Invalid locator inventory set reference SHA")
    if reference.get("href") != (f"./{ID_INVENTORY_DIR}/inventory-set-{sha256}.json"):
        raise RuntimeError("Invalid locator inventory set reference href")
    size = reference.get("size_bytes")
    count = reference.get("inventories_count")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or not 0 < size <= 1024 * 1024
    ):
        raise RuntimeError("Invalid locator inventory set reference size")
    if not isinstance(count, int) or isinstance(count, bool) or not 0 < count <= 65_535:
        raise RuntimeError("Invalid locator inventory set reference count")
    return reference


def _sha256_json(value):
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _validate_source_filename(filename):
    return (
        isinstance(filename, str)
        and 0 < len(filename) <= 255
        and "/" not in filename
        and "\\" not in filename
        and filename not in {".", ".."}
        and filename.endswith(".parquet")
    )


def _make_locator_dictionary(
    source_files,
    last_seen_releases,
    release_version,
    input_inventory_set_sha256=None,
    input_inventory_set=None,
):
    """Build and validate a deterministic compact locator dictionary."""
    for entry in source_files:
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) != 3
            or not all(isinstance(value, str) for value in entry)
        ):
            raise RuntimeError(f"Invalid source-file dictionary entry {entry!r}")
    if not all(isinstance(value, str) for value in last_seen_releases):
        raise RuntimeError("Invalid release dictionary entry")
    if input_inventory_set_sha256 is not None and (
        not isinstance(input_inventory_set_sha256, str)
        or len(input_inventory_set_sha256) != 64
        or any(char not in "0123456789abcdef" for char in input_inventory_set_sha256)
    ):
        raise RuntimeError("Invalid input inventory set SHA")
    if input_inventory_set is not None:
        input_inventory_set = _validate_inventory_set_reference(
            dict(input_inventory_set)
        )
        if (
            input_inventory_set_sha256
            != input_inventory_set["inventory_references_sha256"]
        ):
            raise RuntimeError("Input inventory set reference/SHA mismatch")
    elif input_inventory_set_sha256 is not None:
        raise RuntimeError("Input inventory set SHA requires a permanent reference")
    normalized_files = sorted({tuple(entry) for entry in source_files})
    normalized_releases = sorted(set(last_seen_releases))
    if len(normalized_files) > 65_535:
        raise RuntimeError(
            f"source-file dictionary has {len(normalized_files)} entries; "
            "IDs 1..65535 are available")
    if len(normalized_releases) > 65_535:
        raise RuntimeError(
            f"release dictionary has {len(normalized_releases)} entries; "
            "IDs 1..65535 are available")
    for theme, feature_type, filename in normalized_files:
        if TYPE_THEME_MAP.get(feature_type) != theme:
            raise RuntimeError(
                f"Invalid locator theme/type {theme!r}/{feature_type!r}")
        if not _validate_source_filename(filename):
            raise RuntimeError(f"Invalid source filename {filename!r}")
    for historical_release in normalized_releases:
        if not historical_release:
            raise RuntimeError(
                f"Invalid historical last-seen release {historical_release!r}")

    source_entries = [
        {"theme": theme, "feature_type": feature_type, "filename": filename}
        for theme, feature_type, filename in normalized_files
    ]
    payload = {
        "format_version": ID_INDEX_FORMAT_VERSION,
        "dictionary_version": 1,
        "overture_release": release_version,
        "type_theme_map": _type_theme_metadata(),
        "source_files": source_entries,
        "last_seen_releases": normalized_releases,
        "source_files_count": len(source_entries),
        "last_seen_releases_count": len(normalized_releases),
        "source_file_id_bounds": ([1, len(source_entries)] if source_entries else None),
        "last_seen_release_id_bounds": (
            [1, len(normalized_releases)] if normalized_releases else None
        ),
        "source_files_sha256": _sha256_json(source_entries),
        "last_seen_releases_sha256": _sha256_json(normalized_releases),
        "input_inventory_set_sha256": input_inventory_set_sha256,
        "input_inventory_set": input_inventory_set,
    }
    return payload


def _validate_locator_dictionary(payload, release_version=None):
    """Fail closed on malformed, reordered, oversized, or corrupted dictionaries."""
    if payload.get("format_version") != ID_INDEX_FORMAT_VERSION:
        raise RuntimeError("Unsupported locator dictionary format_version")
    if payload.get("dictionary_version") != 1:
        raise RuntimeError("Unsupported locator dictionary_version")
    if (
        release_version is not None
        and payload.get("overture_release") != release_version
    ):
        raise RuntimeError("Locator dictionary release does not match build release")
    source_files = payload.get("source_files")
    releases = payload.get("last_seen_releases")
    if not isinstance(source_files, list) or not isinstance(releases, list):
        raise RuntimeError("Locator dictionaries must be arrays")
    rebuilt = _make_locator_dictionary(
        [
            (entry.get("theme"), entry.get("feature_type"), entry.get("filename"))
            for entry in source_files
            if isinstance(entry, dict)
        ],
        releases,
        payload.get("overture_release"),
        payload.get("input_inventory_set_sha256"),
        payload.get("input_inventory_set"),
    )
    for key in (
        "source_files",
        "last_seen_releases",
        "source_files_count",
        "last_seen_releases_count",
        "source_file_id_bounds",
        "last_seen_release_id_bounds",
        "source_files_sha256",
        "last_seen_releases_sha256",
        "type_theme_map",
        "input_inventory_set_sha256",
        "input_inventory_set",
    ):
        if payload.get(key) != rebuilt.get(key):
            raise RuntimeError(f"Invalid locator dictionary field {key}")
    return payload


def _locator_dictionary_marker_reference(marker):
    """Return and validate the marker's exact content-addressed reference."""
    sha256 = marker.get("dictionary_sha256")
    href = marker.get("dictionary_href")
    size_bytes = marker.get("dictionary_size_bytes")
    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or any(char not in "0123456789abcdef" for char in sha256)
        or href != f"id-locator-dictionary-{sha256}.json"
        or not isinstance(size_bytes, int)
        or not 0 < size_bytes <= 1024 * 1024
    ):
        raise RuntimeError("Invalid locator dictionary marker reference")
    return href, sha256, size_bytes


def _dictionary_reference(payload, href, sha256, size_bytes):
    return {
        "href": f"./{href}",
        "sha256": sha256,
        "size_bytes": size_bytes,
        "dictionary_version": payload["dictionary_version"],
        "source_files_count": payload["source_files_count"],
        "last_seen_releases_count": payload["last_seen_releases_count"],
        "source_file_id_bounds": payload["source_file_id_bounds"],
        "last_seen_release_id_bounds": payload["last_seen_release_id_bounds"],
    }


def _validate_locator_manifest(manifest, release_version=None):
    if not isinstance(manifest, dict):
        raise RuntimeError("Invalid ID locator manifest")
    if set(manifest) != {
        "format_version", "overture_release", "locator_dictionary",
    }:
        raise RuntimeError("Invalid ID locator manifest fields")
    if manifest.get("format_version") != ID_INDEX_FORMAT_VERSION:
        raise RuntimeError("Invalid ID locator manifest format_version")
    manifest_release = manifest.get("overture_release")
    if not isinstance(manifest_release, str) or not manifest_release:
        raise RuntimeError("Invalid ID locator manifest release")
    if release_version is not None and manifest_release != release_version:
        raise RuntimeError("ID locator manifest release does not match build release")
    reference = manifest.get("locator_dictionary")
    if not isinstance(reference, dict):
        raise RuntimeError("Invalid ID locator manifest dictionary reference")
    if set(reference) != {
        "href", "sha256", "size_bytes", "dictionary_version",
        "source_files_count", "last_seen_releases_count",
        "source_file_id_bounds", "last_seen_release_id_bounds",
    }:
        raise RuntimeError("Invalid ID locator manifest dictionary fields")
    href = reference.get("href")
    marker_shape = {
        "dictionary_href": href[2:] if isinstance(href, str) and href.startswith("./") else None,
        "dictionary_sha256": reference.get("sha256"),
        "dictionary_size_bytes": reference.get("size_bytes"),
    }
    artifact_href, sha256, size_bytes = (
        _locator_dictionary_marker_reference(marker_shape))
    if reference.get("dictionary_version") != 1:
        raise RuntimeError("Invalid ID locator manifest dictionary_version")
    for count_key, bounds_key in (
        ("source_files_count", "source_file_id_bounds"),
        ("last_seen_releases_count", "last_seen_release_id_bounds"),
    ):
        count = reference.get(count_key)
        bounds = reference.get(bounds_key)
        if not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= 65_535:
            raise RuntimeError(f"Invalid ID locator manifest {count_key}")
        expected_bounds = [1, count] if count else None
        if bounds != expected_bounds:
            raise RuntimeError(f"Invalid ID locator manifest {bounds_key}")
    return artifact_href, sha256, size_bytes, reference


def _load_locator_manifest_and_dictionary(r2_config, version, release_version):
    manifest = _read_optional_r2_json(r2_config, version, ID_LOCATOR_MANIFEST)
    if manifest is None:
        raise RuntimeError(f"Required {ID_LOCATOR_MANIFEST} is missing for {version}")
    href, sha256, size_bytes, reference = _validate_locator_manifest(
        manifest, release_version
    )
    payload = _validate_locator_dictionary(
        _read_r2_json(
            r2_config,
            version,
            href,
            expected_sha256=sha256,
            expected_size_bytes=size_bytes,
        ),
        release_version,
    )
    inventory_set_reference = payload.get("input_inventory_set")
    if inventory_set_reference is not None:
        inventory_set = _load_inventory_set(
            r2_config, version, inventory_set_reference, release_version
        )
        if inventory_set["inventory_references_sha256"] != payload.get(
            "input_inventory_set_sha256"
        ):
            raise RuntimeError(
                "ID locator dictionary inventory set payload does not match "
                "its aggregate SHA"
            )
    expected_reference = _dictionary_reference(payload, href, sha256, size_bytes)
    if reference != expected_reference:
        raise RuntimeError("ID locator manifest does not match dictionary payload")
    return manifest, payload, reference


# Expected output shard columns, in order. The worker reads these
# positionally: col 0 must be the 16-byte UUID, cols 1-4 the FLOAT bbox.
V1_SHARD_COLUMNS = [
    ("id", "FIXED_LEN_BYTE_ARRAY"),
    ("bbox_xmin", "FLOAT"),
    ("bbox_ymin", "FLOAT"),
    ("bbox_xmax", "FLOAT"),
    ("bbox_ymax", "FLOAT"),
]
EXPECTED_SHARD_COLUMNS = V1_SHARD_COLUMNS + [
    ("source_file_id", "INT32"),
    ("last_seen_release_id", "INT32"),
    ("registry_member", "BOOLEAN"),
]
# parquet_schema row order (especially across a batched multi-file scan) is
# not guaranteed, so the classifier compares the name->type mapping rather
# than arrival order. Positional correctness of the written files is
# guaranteed by the fixed COPY column list in phase_build_r2.
V1_SHARD_SCHEMA = dict(V1_SHARD_COLUMNS)
EXPECTED_SHARD_SCHEMA = dict(EXPECTED_SHARD_COLUMNS)


def _shard_schema(con, path):
    """Read physical leaf columns and the UUID length from one footer.

    Row order is not relied upon: the id column is found by name.
    """
    rows = con.execute(f"""
        SELECT name, type, type_length
        FROM parquet_schema('{path}')
        WHERE type IS NOT NULL
    """).fetchall()
    columns = [(r[0], str(r[1])) for r in rows]
    uuid_len = next((str(r[2]) for r in rows if r[0] == "id"), None)
    return columns, uuid_len


def _classify_shard_schema(con, path):
    """Return 1 or 3 for an exact known shard schema; reject everything else."""
    actual, uuid_len = _shard_schema(con, path)
    return _classify_shard_schema_values(path, actual, uuid_len)


def _classify_shard_schema_values(path, actual, uuid_len):
    """Classify already-read footer columns without another remote query.

    `actual` is an unordered list of (name, physical_type) leaf columns.
    """
    if uuid_len != "16":
        raise RuntimeError(
            f"Shard {path}: uuid column type_length {uuid_len} != 16")
    schema = dict(actual)
    if len(schema) != len(actual):
        raise RuntimeError(
            f"Shard {path}: duplicate column names in schema {actual}")
    if schema == V1_SHARD_SCHEMA:
        return 1
    if schema == EXPECTED_SHARD_SCHEMA:
        return ID_INDEX_FORMAT_VERSION
    raise RuntimeError(
        f"Shard schema mismatch for {path}: got {sorted(schema.items())}, "
        f"expected v1={sorted(V1_SHARD_SCHEMA.items())} or "
        f"v3={sorted(EXPECTED_SHARD_SCHEMA.items())}")


def _classify_shard_set(con, paths):
    """Inspect every footer in bounded batches and require one uniform format."""
    if not paths:
        raise RuntimeError("No ID-index shards found")
    formats = {}
    total_batches = (len(paths) + SHARD_SCHEMA_BATCH_SIZE - 1) // SHARD_SCHEMA_BATCH_SIZE
    for batch_index, offset in enumerate(
        range(0, len(paths), SHARD_SCHEMA_BATCH_SIZE), start=1
    ):
        batch = paths[offset:offset + SHARD_SCHEMA_BATCH_SIZE]

        def _read_batch():
            return con.execute(
                """
                SELECT file_name, name, type, type_length
                FROM parquet_schema(?)
                WHERE type IS NOT NULL
                """,
                [batch],
            ).fetchall()

        rows = _run_with_heartbeat(
            f"[metadata] schema batch {batch_index}/{total_batches}",
            _retry_transient(_read_batch),
        )
        schemas = {path: [] for path in batch}
        uuid_lengths = {}
        for file_name, name, physical_type, type_length in rows:
            if file_name not in schemas:
                raise RuntimeError(
                    f"Unexpected shard returned by schema scan: {file_name}"
                )
            schemas[file_name].append((name, str(physical_type)))
            # Bind the UUID length to the id column explicitly: the first
            # arriving row for a file is not guaranteed to be id.
            if name == "id":
                uuid_lengths[file_name] = str(type_length)
        for path in batch:
            if not schemas[path]:
                raise RuntimeError(f"No physical schema columns found for shard {path}")
            format_version = _classify_shard_schema_values(
                path, schemas[path], uuid_lengths.get(path)
            )
            formats.setdefault(format_version, []).append(path)
        print(
            f"    [metadata] schema progress: "
            f"{min(offset + len(batch), len(paths))}/{len(paths)} shards",
            flush=True,
        )
    if len(formats) != 1:
        detail = ", ".join(
            f"v{version}={len(version_paths)}"
            for version, version_paths in sorted(formats.items()))
        raise RuntimeError(f"Mixed ID shard formats: {detail}")
    return next(iter(formats))


def _assert_shard_schema(con, path):
    """Assert a written shard's parquet footer matches the v3 layout.

    The worker reads columns positionally, so a silent column reorder or
    type change would break every ID lookup. Raises RuntimeError on
    mismatch; call before writing any _SUCCESS marker.
    """
    format_version = _classify_shard_schema(con, path)
    if format_version != ID_INDEX_FORMAT_VERSION:
        raise RuntimeError(
            f"Shard {path}: wrote legacy format v{format_version}, expected v3"
        )


def _assert_shard_locator_footer_stats(con, path, source_count, release_count):
    """Footer-only bounds and aggregate null-count defense-in-depth.

    These statistics cannot prove row-level XOR: a both-null row could cancel
    a both-present row. `_assert_compact_locator_mapping` remains the primary
    row-level proof before COPY.
    """
    rows = con.execute(
        """
        SELECT row_group_id, path_in_schema, row_group_num_rows,
               stats_min, stats_max, stats_null_count
        FROM parquet_metadata(?)
        WHERE path_in_schema IN ('source_file_id', 'last_seen_release_id')
        ORDER BY row_group_id, path_in_schema
    """,
        [path],
    ).fetchall()
    grouped = {}
    for row_group, column, row_count, minimum, maximum, null_count in rows:
        grouped.setdefault(row_group, {"rows": row_count})[column] = {
            "min": minimum,
            "max": maximum,
            "nulls": null_count,
        }
    if not grouped:
        raise RuntimeError(f"Shard {path}: locator footer stats are missing")
    bounds = {
        "source_file_id": source_count,
        "last_seen_release_id": release_count,
    }
    for row_group, values in grouped.items():
        if set(values) != {
            "rows",
            "source_file_id",
            "last_seen_release_id",
        }:
            raise RuntimeError(
                f"Shard {path}: incomplete locator stats in row group {row_group}"
            )
        total_nulls = 0
        for column, upper in bounds.items():
            stats = values[column]
            if stats["nulls"] is None:
                raise RuntimeError(f"Shard {path}: missing null stats for {column}")
            total_nulls += int(stats["nulls"])
            if stats["min"] is not None:
                minimum, maximum = int(stats["min"]), int(stats["max"])
                if minimum < 1 or maximum > upper or minimum > maximum:
                    raise RuntimeError(
                        f"Shard {path}: {column} stats {minimum}..{maximum} "
                        f"outside dictionary 1..{upper}"
                    )
        if total_nulls != int(values["rows"]):
            raise RuntimeError(
                f"Shard {path}: aggregate locator null-count invariant failed "
                f"in row group {row_group}"
            )


def _assert_locator_rows(con, union_query, prefix, release_version):
    """Fail closed on ambiguous IDs or locator metadata we cannot serve."""
    duplicate = con.execute(f"""
        SELECT id, COUNT(*) AS copies
        FROM ({union_query})
        GROUP BY id
        HAVING COUNT(*) > 1
        LIMIT 1
    """).fetchone()
    if duplicate:
        raise RuntimeError(
            f"Duplicate ID in shard {prefix}: {duplicate[0]} "
            f"appears {duplicate[1]} times")

    known_types = ", ".join(
        "'" + value.replace("'", "''") + "'" for value in TYPE_THEME_MAP)
    theme_case = "CASE feature_type " + " ".join(
        "WHEN '" + feature_type.replace("'", "''") + "' THEN '"
        + theme.replace("'", "''") + "'"
        for feature_type, theme in TYPE_THEME_MAP.items()
    ) + " END"
    invalid = con.execute(f"""
        SELECT id, feature_type, filename, last_seen_release, source_theme
        FROM ({union_query})
        WHERE
            (feature_type IS NOT NULL AND feature_type NOT IN ({known_types}))
            OR (filename IS NOT NULL AND feature_type IS NULL)
            OR (filename LIKE '%/%')
            OR (filename LIKE '%\\%')
            OR (filename IN ('.', '..'))
            OR (filename IS NOT NULL AND filename NOT LIKE '%.parquet')
            OR (length(filename) > 255)
            OR (filename IS NOT NULL AND last_seen_release IS DISTINCT FROM ?)
            OR (feature_type IS NOT NULL
                AND source_theme IS DISTINCT FROM ({theme_case}))
        LIMIT 1
    """, [release_version]).fetchone()
    if invalid:
        raise RuntimeError(
            f"Invalid locator metadata in shard {prefix}: id={invalid[0]} "
            f"type={invalid[1]!r} filename={invalid[2]!r} "
            f"last_seen={invalid[3]!r} theme={invalid[4]!r}; "
            f"expected release {release_version}")


def _compact_locator_query(union_query, source_dictionary_path,
                           release_dictionary_path):
    return f"""
        SELECT u.id, u.bbox_xmin, u.bbox_ymin, u.bbox_xmax, u.bbox_ymax,
               sf.source_file_id::INTEGER AS source_file_id,
               CASE WHEN u.filename IS NULL
                    THEN lr.last_seen_release_id::INTEGER END
                   AS last_seen_release_id,
               u.registry_member
        FROM ({union_query}) u
        LEFT JOIN read_parquet('{source_dictionary_path}') sf
          ON u.source_theme = sf.source_theme
         AND u.feature_type = sf.feature_type
         AND u.filename = sf.filename
        LEFT JOIN read_parquet('{release_dictionary_path}') lr
          ON u.filename IS NULL
         AND u.last_seen_release = lr.last_seen_release
    """


def _assert_compact_locator_mapping(con, mapped_query, prefix):
    unmapped = con.execute(f"""
        SELECT id FROM ({mapped_query})
        WHERE (source_file_id IS NULL AND last_seen_release_id IS NULL)
           OR (source_file_id IS NOT NULL AND last_seen_release_id IS NOT NULL)
           OR source_file_id NOT BETWEEN 1 AND 65535
           OR last_seen_release_id NOT BETWEEN 1 AND 65535
        LIMIT 1
    """).fetchone()
    if unmapped:
        raise RuntimeError(
            f"ID {unmapped[0]} in shard {prefix} is not representable by the "
            "immutable locator dictionary")


def _worker_build_r2_batch(args_tuple):
    """Worker: download one staging prefix locally, write one output shard to R2.

    Downloads the registry staging partition once to local disk, merges with
    release data, sorts by ID, and writes the output shard to R2 (uncompressed).

    Release files are pre-downloaded locally by the caller and passed as local
    paths. All reads are local; only output writes go to R2.
    """
    (staging_prefix, output_prefixes, r2_config, version, release_version,
     release_files, source_dictionary_path, release_dictionary_path,
     row_group_size) = args_tuple

    bucket = r2_config['bucket']
    results = []

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
        source_dictionary_count = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{source_dictionary_path}')"
        ).fetchone()[0]
        release_dictionary_count = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{release_dictionary_path}')"
        ).fetchone()[0]

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
                        SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                               feature_type, filename, last_seen_release,
                               registry_member, source_theme
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
                    f"SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax, "
                    f"feature_type, filename, last_seen_release, registry_member, "
                    f"source_theme "
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
                        f"SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax, "
                        f"feature_type, filename, last_seen_release, registry_member, "
                        f"source_theme "
                        f"FROM read_parquet('{release_path}') "
                        f"WHERE prefix = '{staging_prefix}' {id_filter}"
                    )

            if not sources:
                results.append((prefix, 0, 0, None, None, None))
                continue

            union_query = " UNION ALL ".join(sources)
            _assert_locator_rows(con, union_query, prefix, release_version)
            mapped_query = _compact_locator_query(
                union_query, source_dictionary_path, release_dictionary_path)
            _assert_compact_locator_mapping(con, mapped_query, prefix)

            # Count locally (fast, avoids R2 read-back)
            count = con.execute(
                f"SELECT COUNT(*) FROM ({union_query})"
            ).fetchone()[0]
            if count == 0:
                results.append((prefix, 0, 0, None, None, None))
                continue

            # Sort locally so the producer can hash the exact bytes that are
            # uploaded. Direct DuckDB-to-R2 COPY made it impossible for the
            # finalizer to distinguish the intended shard from a later valid-
            # looking replacement with the same schema and approximate size.
            r2_dest = f"s3://{bucket}/{version}/id-index/{prefix}.parquet"
            local_output = f"/tmp/build-id-{prefix}-{os.getpid()}.parquet"
            def _do_copy():
                try:
                    os.unlink(local_output)
                except FileNotFoundError:
                    pass
                con.execute(f"""
                    COPY (
                        SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                               source_file_id, last_seen_release_id,
                               registry_member
                        FROM ({mapped_query}) ORDER BY id
                    ) TO '{local_output}'
                    (FORMAT PARQUET, COMPRESSION UNCOMPRESSED,
                     ROW_GROUP_SIZE {int(row_group_size)});
                """)
            _retry_transient(_do_copy)()

            try:
                _assert_shard_schema(con, local_output)
                _assert_shard_locator_footer_stats(
                    con, local_output, source_dictionary_count, release_dictionary_count
                )
                size = os.path.getsize(local_output)
                sha256 = _file_sha256(local_output)
                content_md5 = _file_md5_hex(local_output)
                err = _upload_id_shard_to_r2(
                    Path(local_output), r2_config, version, prefix
                )
                if err:
                    raise RuntimeError(f"Failed to upload ID shard {prefix}: {err}")

                # Read back the persisted footer before this shard can count
                # toward a _SUCCESS marker. The release finalizer later binds
                # the R2 object size/ETag to this producer SHA-256.
                _retry_transient(lambda: _assert_shard_schema(con, r2_dest))()
                _retry_transient(
                    lambda: _assert_shard_locator_footer_stats(
                        con, r2_dest,
                        source_dictionary_count, release_dictionary_count,
                    )
                )()
                results.append((prefix, count, size, None, sha256, content_md5))
            finally:
                try:
                    os.unlink(local_output)
                except FileNotFoundError:
                    pass

        # Cleanup
        if has_registry:
            try:
                os.unlink(local_registry)
            except Exception:
                pass
        con.close()

        return results

    except Exception as e:
        return [(p, 0, 0, str(e), None, None) for p in output_prefixes]


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


def _has_v3_id_build_state(r2_config, version):
    """Detect any ID output that makes creating a new v3 manifest unsafe."""
    con = _r2_con(r2_config)
    try:
        shard_paths = _retry_transient(lambda: _glob_files(
            con,
            f"s3://{r2_config['bucket']}/{version}/id-index/*.parquet",
        ))()
        # A positional-format upgrade must always use a new version prefix;
        # even v1 shards at these keys may be cached by deployed Workers.
        if shard_paths:
            return True

        marker_paths = _retry_transient(lambda: _glob_files(
            con,
            f"s3://{r2_config['bucket']}/{version}/staging/build*/_SUCCESS",
        ))()
        return bool(marker_paths)
    finally:
        con.close()


def _scope_prefixes(scope, prefix_len):
    if "prefixes" in scope:
        values = scope["prefixes"]
    else:
        lo = int(scope["prefix_start"], 16)
        hi = int(scope["prefix_end"], 16)
        values = [format(value, f"0{prefix_len}x") for value in range(lo, hi + 1)]
    for value in values:
        if (
            not isinstance(value, str)
            or len(value) != prefix_len
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise RuntimeError(f"Invalid inventory prefix {value!r}")
    return values


def _load_required_marker_inventory(
    r2_config, version, staging_dir, release_version, expected_kind
):
    marker = _read_staging_marker(r2_config, version, staging_dir)
    if not _marker_is_current(marker):
        raise RuntimeError(f"Required staging marker {staging_dir} is missing/stale")
    reference = marker.get("locator_inventory")
    payload = _load_stage_inventory(r2_config, version, reference, release_version)
    if payload["kind"] != expected_kind:
        raise RuntimeError(
            f"Staging marker {staging_dir} has {payload['kind']} inventory; "
            f"expected {expected_kind}"
        )
    return marker, reference, payload


def _load_registry_inventory_fan_in(
    r2_config, version, release_version, prefix_len, smoke=False
):
    con = _r2_con(r2_config)
    try:
        marker_paths = _glob_files(
            con,
            f"s3://{r2_config['bucket']}/{version}/staging/id-partitioned*/_SUCCESS",
        )
    finally:
        con.close()
    if not marker_paths:
        raise RuntimeError("No registry range inventory markers found")
    staging_dirs = sorted(
        {
            path.split("/staging/", 1)[-1].rsplit("/_SUCCESS", 1)[0]
            for path in marker_paths
        }
    )
    seen = set()
    references = []
    releases = []
    for staging_dir in staging_dirs:
        marker, reference, payload = _load_required_marker_inventory(
            r2_config, version, staging_dir, release_version, "registry_range"
        )
        prefixes = _scope_prefixes(payload["scope"], prefix_len)
        overlap = seen.intersection(prefixes)
        if overlap:
            raise RuntimeError(
                f"Overlapping registry inventory scopes: {sorted(overlap)[:10]}"
            )
        if marker.get("partitions") != len(prefixes):
            raise RuntimeError(
                f"Registry marker {staging_dir} partition/scope mismatch"
            )
        seen.update(prefixes)
        references.append(reference)
        releases.extend(payload["last_seen_releases"])
    expected = {format(value, f"0{prefix_len}x") for value in range(16**prefix_len)}
    if not smoke and seen != expected:
        missing = sorted(expected - seen)
        extra = sorted(seen - expected)
        raise RuntimeError(
            f"Registry inventory fan-in is incomplete: missing={missing[:10]}, "
            f"extra={extra[:10]}"
        )
    if smoke and not seen:
        raise RuntimeError("Smoke registry inventory fan-in is empty")
    return sorted(set(releases)), references, seen


def _load_release_inventory_fan_in(
    r2_config, version, release_version, global_source_files
):
    expected_types = _discover_release_types(release_version)
    global_set = set(global_source_files)
    staged_universe = {item for item in global_set if item[:2] in set(expected_types)}
    references = []
    covered = set()
    for theme, feature_type in expected_types:
        staging_dir = f"id-release-{theme}-{feature_type}"
        marker, reference, payload = _load_required_marker_inventory(
            r2_config, version, staging_dir, release_version, "release_type"
        )
        if marker.get("partitions") != 16:
            raise RuntimeError(
                f"Release marker {staging_dir} has invalid partition count"
            )
        scope = payload["scope"]
        if scope != {"theme": theme, "feature_type": feature_type}:
            raise RuntimeError(f"Release marker {staging_dir} scope mismatch")
        tuples = {
            (item["theme"], item["feature_type"], item["filename"])
            for item in payload["source_files"]
        }
        expected = {item for item in global_set if item[:2] == (theme, feature_type)}
        if tuples != expected:
            raise RuntimeError(f"Release marker {staging_dir} file inventory mismatch")
        if covered.intersection(tuples):
            raise RuntimeError("Duplicate release inventory tuples")
        covered.update(tuples)
        references.append(reference)
    if covered != staged_universe:
        missing = sorted(staged_universe - covered)
        extra = sorted(covered - staged_universe)
        raise RuntimeError(
            "Release inventory fan-in does not cover the current-release "
            f"universe: missing={missing[:10]}, extra={extra[:10]}"
        )
    return references


def phase_build_locator_dictionary(
    r2_config, version, release_version, prefix_len=3, smoke=False
):
    """Build the one global dictionary required by every parallel build range."""
    manifest = _read_optional_r2_json(r2_config, version, ID_LOCATOR_MANIFEST)
    if manifest is not None:
        _, payload, _ = _load_locator_manifest_and_dictionary(
            r2_config, version, release_version
        )
        _require_locator_input_inventory_set_sha(payload)
        print("  [dictionary] Existing immutable manifest is valid; skipping")
        return payload
    if _has_v3_id_build_state(r2_config, version):
        raise RuntimeError(
            "Refusing to create a missing ID locator manifest after ID "
            "shards or build markers already exist; use a new version"
        )

    # Source tuples are the exact, immutable pinned-release file universe. A
    # stable double listing is dramatically cheaper than DISTINCT over feature
    # rows and is a safe superset: unused entries have no semantics, while any
    # staged row outside the inventory still fails _assert_compact_locator_mapping.
    con = _r2_con(r2_config)
    try:
        source_files = _discover_current_release_source_files(con, release_version)
    finally:
        con.close()

    historical_releases, registry_references, _ = _load_registry_inventory_fan_in(
        r2_config, version, release_version, prefix_len, smoke=smoke
    )
    release_references = _load_release_inventory_fan_in(
        r2_config, version, release_version, source_files
    )
    current_inventory = _make_stage_inventory(
        "current_release_files",
        release_version,
        {"universe": "all_mapped_types"},
        source_files=source_files,
    )
    current_reference = _publish_stage_inventory(r2_config, version, current_inventory)
    inventory_references = (
        registry_references + release_references + [current_reference]
    )
    inventory_set = _make_inventory_set(inventory_references, release_version)
    input_inventory_set_reference = _publish_inventory_set(
        r2_config, version, inventory_set
    )
    input_inventory_set_sha256 = inventory_set["inventory_references_sha256"]

    payload = _make_locator_dictionary(
        source_files,
        historical_releases,
        release_version,
        input_inventory_set_sha256=input_inventory_set_sha256,
        input_inventory_set=input_inventory_set_reference,
    )
    artifact_bytes = _canonical_json_bytes(payload)
    if len(artifact_bytes) > 1024 * 1024:
        raise RuntimeError("Locator dictionary exceeds the 1 MiB hard limit")
    dictionary_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    dictionary_href = f"id-locator-dictionary-{dictionary_sha256}.json"
    tmp = Path(f"tmp-id-locator-meta-{os.getpid()}.json")
    tmp.write_bytes(artifact_bytes)
    try:
        err = _upload_to_r2(
            tmp, f"{r2_config['bucket']}/{version}/{dictionary_href}")
    finally:
        tmp.unlink(missing_ok=True)
    if err:
        raise RuntimeError(f"Failed to upload {dictionary_href}: {err}")

    reference = _dictionary_reference(
        payload, dictionary_href, dictionary_sha256, len(artifact_bytes))
    manifest = {
        "format_version": ID_INDEX_FORMAT_VERSION,
        "overture_release": release_version,
        "locator_dictionary": reference,
    }
    _validate_locator_manifest(manifest, release_version)
    manifest_tmp = Path(f"tmp-id-locator-manifest-{os.getpid()}.json")
    manifest_tmp.write_bytes(_canonical_json_bytes(manifest))
    try:
        err = _upload_to_r2(
            manifest_tmp,
            f"{r2_config['bucket']}/{version}/{ID_LOCATOR_MANIFEST}",
        )
    finally:
        manifest_tmp.unlink(missing_ok=True)
    if err:
        raise RuntimeError(f"Failed to upload {ID_LOCATOR_MANIFEST}: {err}")

    _write_staging_marker(
        r2_config,
        version,
        "id-dictionaries",
        payload["source_files_count"] + payload["last_seen_releases_count"],
        extra={
            "dictionary_sha256": dictionary_sha256,
            "dictionary_href": dictionary_href,
            "dictionary_size_bytes": len(artifact_bytes),
            "source_files_count": payload["source_files_count"],
            "last_seen_releases_count": payload["last_seen_releases_count"],
            "input_inventory_set_sha256": input_inventory_set_sha256,
            "input_inventory_count": len(inventory_references),
        },
    )
    return payload


def _write_local_dictionary_tables(payload):
    """Materialize tiny dictionary tables once for forked DuckDB workers."""
    token = f"{os.getpid()}-{int(time.time() * 1000)}"
    source_path = f"/tmp/id-source-files-{token}.parquet"
    release_path = f"/tmp/id-last-seen-releases-{token}.parquet"
    con = duckdb.connect()
    try:
        con.execute("""
            CREATE TABLE source_files (
                source_file_id INTEGER, source_theme VARCHAR,
                feature_type VARCHAR, filename VARCHAR)
        """)
        source_rows = [
            (index, entry["theme"], entry["feature_type"], entry["filename"])
            for index, entry in enumerate(payload["source_files"], start=1)
        ]
        if source_rows:
            con.executemany(
                "INSERT INTO source_files VALUES (?, ?, ?, ?)", source_rows)
        con.execute(f"COPY source_files TO '{source_path}' (FORMAT PARQUET)")
        con.execute("""
            CREATE TABLE releases (
                last_seen_release_id INTEGER, last_seen_release VARCHAR)
        """)
        release_rows = list(enumerate(payload["last_seen_releases"], start=1))
        if release_rows:
            con.executemany(
                "INSERT INTO releases VALUES (?, ?)", release_rows)
        con.execute(f"COPY releases TO '{release_path}' (FORMAT PARQUET)")
    finally:
        con.close()
    return source_path, release_path


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


def phase_build_r2(prefix_len, r2_config, version, release_version, workers, prefixes=None,
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

    _, dictionary, _ = _load_locator_manifest_and_dictionary(
        r2_config, version, release_version)
    source_dictionary_path, release_dictionary_path = (
        _write_local_dictionary_tables(dictionary))

    # Filter to the buckets this job actually needs (bucketed staging), then
    # download and merge everything into ONE local file sorted by
    # (prefix, id). One file means one probe per prefix in the workers
    # instead of one per theme/type, and one sorted dataset to range-scan.
    release_files = _release_files_for_prefixes(release_files, prefixes)
    if release_files:
        print(f"  Release files (after bucket filter): {len(release_files)}")

    local_release_files = []
    if release_files:
        print("  Downloading and merging release staging locally...")
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
        try:
            dl_con.execute("SET memory_limit = '4GB';")

            # Hard-fail if the release data cannot be downloaded: skipping it
            # would silently drop its IDs from the index. Legacy single-file
            # staging carries a `bucket` column that bucketed files store in
            # the path instead; select the shared columns explicitly.
            def _download_release():
                dl_con.execute(f"""
                    COPY (
                        SELECT id, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                               feature_type, filename, last_seen_release,
                               registry_member, source_theme, prefix
                        FROM read_parquet([{file_list}], union_by_name=true)
                        {release_where}
                        ORDER BY prefix, id
                    ) TO '{local_path}' (FORMAT PARQUET, COMPRESSION ZSTD);
                """)
            _run_with_heartbeat(
                "[build] download, range-filter, sort, and merge release staging",
                _retry_transient(_download_release),
            )
        finally:
            dl_con.close()

        size_mb = os.path.getsize(local_path) / 1024 / 1024
        print(f"    merged: {size_mb:.0f} MB ({time.time() - t_dl:.0f}s)")
        local_release_files.append(local_path)

    # Each prefix maps 1:1 to a staging prefix (same prefix-len)
    work = [
        (p, [p], r2_config, version, release_version, local_release_files,
         source_dictionary_path, release_dictionary_path, row_group_size)
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

    # Cleanup local release and dictionary files
    for lf in local_release_files + [source_dictionary_path, release_dictionary_path]:
        try:
            os.unlink(lf)
        except Exception:
            pass

    # Add empty results for skipped prefixes (for metadata accuracy)
    if len(prefixes) < shard_count:
        processed = {r[0] for r in results}
        for p in all_prefixes:
            if p not in processed:
                results.append((p, 0, 0, None, None))

    return results


# ---------------------------------------------------------------------------
# Metadata: Generate id-collection.json
# ---------------------------------------------------------------------------

def _gather_shard_info_from_r2(prefix_len, r2_config, version):
    """Discover existing R2 shards via glob (for metadata-only runs).

    Reads every Parquet footer to recover exact object sizes, but not individual
    record counts. Returns (prefix, None, size, None) for each shard found; a
    None count means "exists, count unknown" (real totals come from the per-range
    build _SUCCESS markers — see _sum_build_marker_records). Exact sizes make
    id-collection.json usable as an immutable output inventory instead of
    reporting a misleading zero-byte fleet.
    """
    print("  Discovering existing R2 shards...")
    con = _r2_con(r2_config)
    try:
        bucket = r2_config["bucket"]
        glob_path = f"s3://{bucket}/{version}/id-index/*.parquet"
        # Real listing errors must propagate: silently publishing an empty
        # collection would break every ID lookup for this version.
        shard_files = _run_with_heartbeat(
            "[metadata] list existing R2 shards",
            _retry_transient(lambda: _glob_files(con, glob_path)),
        )

        sizes = {}
        if shard_files:
            rows = _run_with_heartbeat(
                "[metadata] read shard file sizes",
                _retry_transient(lambda: con.execute(
                    "SELECT file_name, file_size_bytes FROM parquet_file_metadata(?)",
                    [shard_files],
                ).fetchall()),
            )
            sizes = {path: int(size) for path, size in rows}
    finally:
        con.close()

    intended_shards = _build_marker_shard_inventory(r2_config, version)
    actual_prefixes = {
        path.rsplit("/", 1)[-1].removesuffix(".parquet")
        for path in shard_files
    }
    if actual_prefixes != set(intended_shards):
        missing = sorted(set(intended_shards) - actual_prefixes)[:10]
        extra = sorted(actual_prefixes - set(intended_shards))[:10]
        raise RuntimeError(
            f"ID shard inventory differs from producer markers: "
            f"missing={missing}, extra={extra}"
        )

    results = []
    for path in shard_files:
        prefix = path.rsplit("/", 1)[-1].replace(".parquet", "")
        size = sizes.get(path)
        if size is None or size <= 0:
            raise RuntimeError(f"Missing valid file size for ID shard {path}")
        intended = intended_shards[prefix]
        if size != intended["size_bytes"]:
            raise RuntimeError(f"ID shard {prefix} size differs from producer marker")
        results.append(
            (prefix, intended["record_count"], size, None, intended["sha256"],
             intended.get("content_md5"))
        )

    print(f"  Found {len(results)} shards")
    return results


def _read_current_build_markers(r2_config, version):
    bucket = r2_config["bucket"]
    con = _r2_con(r2_config)
    try:
        marker_files = _retry_transient(lambda: _glob_files(
            con, f"s3://{bucket}/{version}/staging/build*/_SUCCESS"))()
        markers = []
        for marker_path in marker_files:
            content = _retry_transient(lambda path=marker_path: con.execute(
                f"SELECT content FROM read_text('{path}')").fetchone()[0])()
            data = json.loads(content)
            if _marker_is_current(data):
                markers.append((marker_path, data))
        return markers
    finally:
        con.close()


def _build_marker_shard_inventory(r2_config, version):
    """Return the producer-bound shard inventory from current build markers."""
    combined = {}
    for marker_path, marker in _read_current_build_markers(r2_config, version):
        shards = marker.get("shards")
        if not isinstance(shards, dict) or not shards:
            raise RuntimeError(f"Build marker {marker_path} has no shard inventory")
        for prefix, info in shards.items():
            if prefix in combined:
                raise RuntimeError(f"Duplicate ID shard {prefix} in build markers")
            if not isinstance(info, dict):
                raise RuntimeError(f"Invalid ID shard inventory for {prefix}")
            record_count = info.get("record_count")
            size_bytes = info.get("size_bytes")
            sha256 = info.get("sha256")
            if not isinstance(record_count, int) or record_count <= 0:
                raise RuntimeError(f"Invalid record count for ID shard {prefix}")
            if not isinstance(size_bytes, int) or size_bytes <= 0:
                raise RuntimeError(f"Invalid size for ID shard {prefix}")
            if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise RuntimeError(f"Invalid SHA-256 for ID shard {prefix}")
            entry = {
                "record_count": record_count,
                "size_bytes": size_bytes,
                "sha256": sha256,
            }
            # Older markers predate the content MD5; carry it only when the
            # producer recorded a well-formed hex digest so the finalizer can
            # bind the R2 ETag without breaking backward compatibility.
            content_md5 = info.get("content_md5")
            if content_md5 is not None:
                if not isinstance(content_md5, str) or not re.fullmatch(
                    r"[0-9a-f]{32}", content_md5
                ):
                    raise RuntimeError(
                        f"Invalid content MD5 for ID shard {prefix}"
                    )
                entry["content_md5"] = content_md5
            combined[prefix] = entry
    if not combined:
        raise RuntimeError("No current ID build marker shard inventory exists")
    return combined


def _shard_marker_entry(result):
    """Build one producer shard inventory entry, recording MD5 when present."""
    entry = {
        "record_count": result[1],
        "size_bytes": result[2],
        "sha256": result[4],
    }
    content_md5 = result[5] if len(result) > 5 else None
    if content_md5:
        entry["content_md5"] = content_md5
    return entry


def _build_marker_range(staging_dir):
    """Return the inclusive (start, end) prefix ints a build marker covers.

    A suffix-less "build" marker (a completed full run) covers every prefix
    (end is None); "build-<start>-<end>" covers that hex range.
    """
    suffix = staging_dir[len("build"):]
    if not suffix:
        return (0, None)
    if not suffix.startswith("-"):
        raise RuntimeError(f"Unrecognized build marker directory {staging_dir!r}")
    start, separator, end = suffix[1:].partition("-")
    if not separator:
        raise RuntimeError(f"Invalid build marker range in {staging_dir!r}")
    return (int(start, 16), int(end, 16))


def _prefix_in_build_range(prefix, prefix_range):
    start, end = prefix_range
    value = int(prefix, 16)
    return start <= value and (end is None or value <= end)


def _patch_update_build_markers(
    r2_config, version, results, dictionary_sha256, input_inventory_set_sha256
):
    """Fold patched shards back into their containing build-range markers.

    A `--prefixes` patch build overwrites R2 shards but owns no range marker.
    Producer markers are the finalizer's canonical size/sha/content_md5
    source, so each rebuilt prefix's entry is refreshed in the marker whose
    range contains it and that marker is rewritten (its shard data was already
    uploaded by phase_build_r2). A prefix with no containing range marker means
    that range was never built as a whole; refuse rather than publish an
    inconsistent inventory.
    """
    rebuilt = {
        result[0]: _shard_marker_entry(result)
        for result in results
        if result[1] > 0
    }
    if not rebuilt:
        return
    ranges = []
    for marker_path, marker in _read_current_build_markers(r2_config, version):
        staging_dir = marker_path.split("/staging/", 1)[-1].rsplit(
            "/_SUCCESS", 1)[0]
        ranges.append((staging_dir, _build_marker_range(staging_dir), marker))

    touched = {}
    for prefix, entry in sorted(rebuilt.items()):
        containing = [
            (staging_dir, marker)
            for staging_dir, prefix_range, marker in ranges
            if _prefix_in_build_range(prefix, prefix_range)
        ]
        if not containing:
            raise RuntimeError(
                f"Patched prefix {prefix} has no containing build-range marker "
                f"for {version}; rebuild the whole range instead"
            )
        if len(containing) > 1:
            names = ", ".join(name for name, _ in containing)
            raise RuntimeError(
                f"Patched prefix {prefix} maps to multiple build-range markers "
                f"({names}); build ranges must be disjoint"
            )
        staging_dir, marker = containing[0]
        if marker.get("dictionary_sha256") != dictionary_sha256:
            raise RuntimeError(
                f"Build marker {staging_dir} does not match the patch locator "
                "manifest SHA"
            )
        if marker.get("input_inventory_set_sha256") != input_inventory_set_sha256:
            raise RuntimeError(
                f"Build marker {staging_dir} does not match the patch locator "
                "input inventory set SHA"
            )
        shards = marker.get("shards")
        if not isinstance(shards, dict) or not shards:
            raise RuntimeError(
                f"Build marker {staging_dir} has no shard inventory to patch"
            )
        shards[prefix] = entry
        touched[staging_dir] = marker

    for staging_dir, marker in touched.items():
        shards = marker["shards"]
        _write_staging_marker(
            r2_config,
            version,
            staging_dir,
            len(shards),
            extra={
                "records": sum(s["record_count"] for s in shards.values()),
                "dictionary_sha256": dictionary_sha256,
                "input_inventory_set_sha256": input_inventory_set_sha256,
                "shards": shards,
            },
        )
        print(f"  Updated build marker {staging_dir} for patched prefixes")


def _validate_build_marker_dictionary_sha(
    r2_config, version, expected_sha256, expected_input_inventory_set_sha256
):
    for marker_path, marker in _read_current_build_markers(r2_config, version):
        if marker.get("dictionary_sha256") != expected_sha256:
            raise RuntimeError(
                f"Build marker {marker_path} does not match locator manifest SHA"
            )
        if (
            marker.get("input_inventory_set_sha256")
            != expected_input_inventory_set_sha256
        ):
            raise RuntimeError(
                f"Build marker {marker_path} does not match locator input "
                "inventory set SHA"
            )


def _sum_build_marker_records(
    r2_config, version, expected_sha256=None, expected_input_inventory_set_sha256=None
):
    """Sum real record counts from per-range build _SUCCESS markers.

    Returns the total, or None if no build markers with record counts exist.
    """
    total = 0
    found = False
    for marker_path, data in _read_current_build_markers(r2_config, version):
        if (
            expected_sha256 is not None
            and data.get("dictionary_sha256") != expected_sha256
        ):
            raise RuntimeError(
                f"Build marker {marker_path} does not match locator manifest SHA"
            )
        if (
            expected_input_inventory_set_sha256 is not None
            and data.get("input_inventory_set_sha256")
            != expected_input_inventory_set_sha256
        ):
            raise RuntimeError(
                f"Build marker {marker_path} does not match locator input "
                "inventory set SHA"
            )
        if "records" in data:
            total += int(data["records"])
            found = True
    return total if found else None


def _detect_output_shard_format(r2_config, version):
    """Inspect every output footer before metadata can be published."""
    con = _r2_con(r2_config)
    try:
        paths = _retry_transient(lambda: _glob_files(
            con,
            f"s3://{r2_config['bucket']}/{version}/id-index/*.parquet",
        ))()
        return _retry_transient(lambda: _classify_shard_set(con, paths))()
    finally:
        con.close()


def _format_metadata(format_version, release_version, dictionary_reference=None):
    """Return only metadata supported by the uniform shard format."""
    if format_version == 1:
        return {}
    if format_version != ID_INDEX_FORMAT_VERSION:
        raise RuntimeError(f"Unsupported ID-index format v{format_version}")
    if not isinstance(dictionary_reference, dict):
        raise RuntimeError("Format v3 requires a locator dictionary reference")
    return {
        "format_version": ID_INDEX_FORMAT_VERSION,
        "overture_release": release_version,
        "locator_dictionary": dictionary_reference,
    }


def _load_locator_dictionary_reference(r2_config, version, release_version):
    _, _, reference = _load_locator_manifest_and_dictionary(
        r2_config, version, release_version)
    return reference


def _require_locator_input_inventory_set_sha(dictionary):
    value = dictionary.get("input_inventory_set_sha256")
    reference = dictionary.get("input_inventory_set")
    if not value or not reference:
        raise RuntimeError(
            "Locator dictionary predates stage-inventory binding; build "
            "dictionaries under a new version before building or publishing "
            "v3 metadata"
        )
    reference = _validate_inventory_set_reference(reference)
    if reference["inventory_references_sha256"] != value:
        raise RuntimeError("Locator dictionary inventory set binding mismatch")
    return value


def _load_locator_dictionary_binding(r2_config, version, release_version):
    _, dictionary, reference = _load_locator_manifest_and_dictionary(
        r2_config, version, release_version
    )
    return reference, _require_locator_input_inventory_set_sha(dictionary)


def phase_metadata(results, prefix_len, version, release_version, r2_config):
    """Generate id-collection.json and upload to R2."""
    # This check intentionally precedes creation/upload of either metadata
    # object. A resumed metadata-only run must never label v1 or mixed shards
    # as v3 based on the currently checked-out producer.
    format_version = _detect_output_shard_format(r2_config, version)
    dictionary_reference = None
    input_inventory_set_sha256 = None
    if format_version == ID_INDEX_FORMAT_VERSION:
        dictionary_reference, input_inventory_set_sha256 = (
            _load_locator_dictionary_binding(r2_config, version, release_version)
        )
    if dictionary_reference is not None:
        _validate_build_marker_dictionary_sha(
            r2_config,
            version,
            dictionary_reference["sha256"],
            input_inventory_set_sha256,
        )
    format_metadata = _format_metadata(
        format_version, release_version, dictionary_reference
    )
    shard_infos = {}
    total_records = 0
    counts_known = True
    errors = []

    for result in results:
        prefix, count, size, err = result[:4]
        sha256 = result[4] if len(result) > 4 else None
        content_md5 = result[5] if len(result) > 5 else None
        if err:
            errors.append((prefix, err))
        elif count is None:
            # Shard exists but per-shard count unknown (metadata-only run)
            shard_infos[prefix] = {
                "size_bytes": size,
                "sha256": sha256,
                "content_md5": content_md5,
            }
            counts_known = False
        elif count > 0:
            shard_infos[prefix] = {
                "record_count": count,
                "size_bytes": size,
                "sha256": sha256,
                "content_md5": content_md5,
            }
            total_records += count

    # When run standalone, recover the real totals from the per-range build
    # markers instead of fabricating per-shard counts.
    if not counts_known:
        marker_total = _sum_build_marker_records(
            r2_config,
            version,
            dictionary_reference["sha256"] if dictionary_reference else None,
            input_inventory_set_sha256,
        )
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
            **format_metadata,
        },
        "items": {
            p: {
                k: v for k, v in [
                    ("href", f"./id-index/{p}.parquet"),
                    ("record_count", s.get("record_count")),
                    ("size_bytes", s.get("size_bytes")),
                    ("sha256", s.get("sha256")),
                    ("content_md5", s.get("content_md5")),
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
    meta = {
        "prefix_len": prefix_len,
        "shard_count": len(shard_infos),
        **format_metadata,
    }
    tmp_meta = Path("tmp-id-meta.json")
    write_json(tmp_meta, meta)
    err = _upload_to_r2(tmp_meta, f"{bucket}/{version}/id-meta.json")
    tmp_meta.unlink(missing_ok=True)
    if err:
        print(f"  ERROR uploading id-meta.json: {err}")
        sys.exit(1)
    print("  Uploaded id-meta.json to R2")

    return shard_infos, total_records, errors, format_version


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
        print("  Mode: smoke test")

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

    # Smoke test: 5 CONTIGUOUS prefixes so the explicit-prefix bounding
    # range covers only the head of the id-sorted registry (evenly-spaced
    # prefixes span the whole file and forced a full-registry scan that
    # outlived the staging watchdog). Contiguity loses nothing: the smoke
    # test validates the code path, not data coverage.
    smoke_prefixes = None
    smoke_release_limit = None
    if smoke:
        smoke_prefixes = [format(i, f'0{args.prefix_len}x') for i in range(5)]
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
        skip = (
            not args.prefixes
            and marker_ranges is None
            and _marker_is_current(
                _read_staging_marker(r2_config, version, staging_marker_key))
        )
        if skip:
            print(f"\nStage registry: Skipped ({staging_marker_key} complete for {version})")
        else:
            print("\nStage registry: Partition registry")
            t0 = time.time()
            stage_prefixes = smoke_prefixes or range_prefixes
            if smoke_prefixes is not None or args.prefixes:
                # Explicit prefix list (smoke test / patching)
                phase_partition_r2(args.prefix_len, r2_config, version,
                                   prefixes=stage_prefixes,
                                   smoke_history=smoke_prefixes is not None)
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
                    range_start, separator, range_end = mr.partition("-")
                    if not separator:
                        raise RuntimeError(f"Invalid marker range {mr!r}")
                    scope = _registry_inventory_scope(
                        args.prefix_len, prefix_start=range_start, prefix_end=range_end
                    )
                    inventory_reference = _build_registry_stage_inventory(
                        r2_config, version, release_version, scope
                    )
                    _write_staging_marker(
                        r2_config,
                        version,
                        mk,
                        len(_scope_prefixes(scope, args.prefix_len)),
                        extra={"locator_inventory": inventory_reference},
                    )
                    print(f"  Wrote marker: {mk}")
            elif not args.prefixes:
                scope = _registry_inventory_scope(
                    args.prefix_len,
                    prefixes=stage_prefixes if smoke_prefixes is not None else None,
                    prefix_start=(
                        args.prefix_start if smoke_prefixes is None else None
                    ),
                    prefix_end=(args.prefix_end if smoke_prefixes is None else None),
                )
                inventory_reference = _build_registry_stage_inventory(
                    r2_config, version, release_version, scope
                )
                _write_staging_marker(
                    r2_config,
                    version,
                    staging_marker_key,
                    len(stage_prefixes) if stage_prefixes else shard_count,
                    extra={"locator_inventory": inventory_reference},
                )
            phase_times["Stage registry"] = time.time() - t0

    # === Stage base (release themes) ===
    if run_all or "stage-base" in phases:
        release_type = getattr(args, "release_type", None)
        if release_type:
            theme, separator, type_name = release_type.partition("/")
            if not separator or not theme or not type_name or "/" in type_name:
                raise RuntimeError(
                    "--release-type must be one theme/type pair, for example "
                    "base/land_cover"
                )
            print(f"\nStage base type: Partition {theme}/{type_name}")
            t0 = time.time()
            phase_partition_release_type_r2(
                theme,
                type_name,
                args.prefix_len,
                release_version,
                r2_config,
                version,
                limit=smoke_release_limit,
                prefixes=smoke_prefixes,
            )
            phase_times[f"Stage base {theme}/{type_name}"] = time.time() - t0
        elif _r2_release_staging_exists(r2_config, version):
            print(f"\nStage base: Skipped (release staging complete for {version})")
        else:
            print(f"\nStage base: Partition release themes ({', '.join(RELEASE_THEMES)})")
            t0 = time.time()
            phase_partition_release_r2(
                args.prefix_len, release_version, r2_config, version,
                limit=smoke_release_limit,
                prefixes=smoke_prefixes,
            )
            phase_times["Stage base"] = time.time() - t0

    if "stage-base-finalize" in phases:
        if getattr(args, "release_type", None):
            raise RuntimeError(
                "--release-type cannot be combined with stage-base-finalize"
            )
        print("\nStage base finalize: Verify all discovered release types")
        t0 = time.time()
        phase_finalize_release_r2(release_version, r2_config, version)
        phase_times["Stage base finalize"] = time.time() - t0

    # === Global locator dictionaries ===
    if run_all or "dictionaries" in phases:
        print("\nDictionaries: Build compact global locator dictionaries")
        t0 = time.time()
        phase_build_locator_dictionary(
            r2_config, version, release_version, prefix_len=args.prefix_len, smoke=smoke
        )
        phase_times["Dictionaries"] = time.time() - t0

    # === Build shards ===
    results = None
    if run_all or "build" in phases:
        _, build_dictionary, build_dictionary_reference = (
            _load_locator_manifest_and_dictionary(r2_config, version, release_version)
        )
        build_dictionary_sha256 = build_dictionary_reference["sha256"]
        build_inventory_set_sha256 = _require_locator_input_inventory_set_sha(
            build_dictionary
        )
        build_marker_key = f"build{range_suffix}"
        # Explicit-prefix patch builds bypass markers entirely: they must
        # re-run unconditionally, and a suffix-less "build" marker would both
        # block future patch builds and corrupt _sum_build_marker_records
        # (its build*/_SUCCESS glob would double-count the patched prefixes).
        marker = (
            None
            if args.prefixes
            else _read_staging_marker(r2_config, version, build_marker_key)
        )
        if _marker_is_current(marker):
            if marker.get("dictionary_sha256") != build_dictionary_sha256:
                raise RuntimeError(
                    f"Build marker {build_marker_key} does not match "
                    "locator manifest SHA"
                )
            if marker.get("input_inventory_set_sha256") != build_inventory_set_sha256:
                raise RuntimeError(
                    f"Build marker {build_marker_key} does not match "
                    "locator input inventory set SHA"
                )
            print(f"\nBuild: Skipped ({build_marker_key} complete for {version})")
        else:
            print("\nBuild: Build parquet shards")
            t0 = time.time()
            results = phase_build_r2(
                args.prefix_len,
                r2_config,
                version,
                release_version,
                args.workers,
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
                _write_staging_marker(
                    r2_config,
                    version,
                    build_marker_key,
                    built,
                    extra={
                        "records": records,
                        "dictionary_sha256": build_dictionary_sha256,
                        "input_inventory_set_sha256": build_inventory_set_sha256,
                        "shards": {
                            result[0]: _shard_marker_entry(result)
                            for result in results
                            if result[1] > 0
                        },
                    },
                )
            else:
                # A patch build overwrote a subset of shards. Refresh their
                # entries in the containing build-range marker(s) so the
                # producer inventory the finalizer trusts stays consistent
                # with the bytes now in R2.
                _patch_update_build_markers(
                    r2_config,
                    version,
                    results,
                    build_dictionary_sha256,
                    build_inventory_set_sha256,
                )

    # === Metadata ===
    if run_all or "metadata" in phases:
        # An explicitly requested metadata phase always regenerates (it is
        # idempotent and cheap): after a patch build the existing marker
        # describes stale metadata. The marker only short-circuits resumed
        # full-pipeline runs.
        explicit_metadata = "metadata" in phases
        meta_marker = (
            None
            if explicit_metadata
            else _read_staging_marker(r2_config, version, "metadata")
        )
        if _marker_is_current(meta_marker):
            (metadata_dictionary_reference, metadata_inventory_set_sha256) = (
                _load_locator_dictionary_binding(r2_config, version, release_version)
            )
            _validate_build_marker_dictionary_sha(
                r2_config,
                version,
                metadata_dictionary_reference["sha256"],
                metadata_inventory_set_sha256,
            )
            if (
                meta_marker.get("dictionary_sha256")
                != metadata_dictionary_reference["sha256"]
            ):
                raise RuntimeError(
                    "Metadata marker does not match locator manifest SHA"
                )
            if (
                meta_marker.get("input_inventory_set_sha256")
                != metadata_inventory_set_sha256
            ):
                raise RuntimeError(
                    "Metadata marker does not match locator input inventory set SHA"
                )
            print(f"\nMetadata: Skipped (metadata complete for {version})")
        else:
            t_meta = time.time()
            # If build phase didn't run, gather shard info from existing R2 data
            if results is None:
                results = _gather_shard_info_from_r2(
                    args.prefix_len,
                    r2_config,
                    version,
                )

            print("\nMetadata: Generate id-collection.json")
            shard_infos, total_records, errors, output_format = phase_metadata(
                results,
                args.prefix_len,
                version,
                release_version,
                r2_config,
            )
            phase_times["Metadata"] = time.time() - t_meta

            metadata_extra = {"records": total_records}
            if output_format == ID_INDEX_FORMAT_VERSION:
                (metadata_dictionary_reference, metadata_inventory_set_sha256) = (
                    _load_locator_dictionary_binding(
                        r2_config, version, release_version
                    )
                )
                metadata_extra.update(
                    {
                        "dictionary_sha256": metadata_dictionary_reference["sha256"],
                        "input_inventory_set_sha256": metadata_inventory_set_sha256,
                    }
                )
            _write_staging_marker(
                r2_config,
                version,
                "metadata",
                len(shard_infos),
                extra=metadata_extra,
                format_version=output_format,
            )

            total_size = sum(s.get("size_bytes", 0) for s in shard_infos.values())
            max_shard = max(
                (s.get("size_bytes", 0) for s in shard_infos.values()), default=0
            )

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
        print("\n  Timing breakdown:")
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
                   help="Run specific phase(s): stage-registry, stage-base, "
                        "stage-base-finalize, dictionaries, build, metadata, "
                        "or all (comma-separated)")
    p.add_argument(
        "--release-type",
        help="With --phase stage-base, stage one discovered theme/type pair",
    )
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
