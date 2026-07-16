#!/usr/bin/env python3
"""ID-index staging protocol: markers, inventories, and the locator dictionary.

This is the single home for the correctness contract that keeps the
UUID-prefix-sharded ID index resumable and tamper-evident. Every phase of
``build_id_index.py`` (and the ``gen_id_collection`` / ``patch_failed_shards``
helpers) goes through this module rather than re-deriving the rules, so the
"data before marker" ordering, the fail-closed validation, and the
content-addressing are defined exactly once.

The coherent API, in the order a pipeline run touches it:

* R2 / DuckDB I/O primitives the protocol is built on --- ``_r2_con``,
  ``_glob_files``, ``_retry_transient``, ``_upload_to_r2``, ``_read_r2_json``,
  ``_read_optional_r2_json``.
* Staging markers (write == attest, always after the data it certifies;
  read == verify) --- ``_write_staging_marker`` / ``_read_staging_marker`` /
  ``_marker_is_current``.
* Content-addressed stage inventories and their permanent set ---
  ``_make_stage_inventory`` / ``_validate_stage_inventory`` /
  ``_stage_inventory_reference`` and the ``_inventory_set_*`` family, plus the
  ``_publish_*`` / ``_load_*`` I/O that binds each object to its SHA-256.
* The compact locator dictionary and its manifest ---
  ``_make_locator_dictionary`` / ``_validate_locator_dictionary`` /
  ``_validate_locator_manifest`` and ``_load_locator_manifest_and_dictionary``.
* The producer build-range marker inventory the finalizer trusts ---
  ``_read_current_build_markers``, ``_build_marker_shard_inventory``,
  ``_shard_marker_entry`` / ``_build_marker_range`` / ``_prefix_in_build_range``
  / ``_patch_update_build_markers``, and the SHA cross-checks
  ``_validate_build_marker_dictionary_sha`` / ``_sum_build_marker_records``.
* The build-phase staging guard --- ``reconcile_build_release_staging``.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).parent))
from common import write_json

# ID-index format v3 appends compact locator IDs after the five v1 positional
# columns. The content-addressed dictionary keeps the authoritative theme,
# type, filename, and historical release strings once per shard set.
ID_INDEX_FORMAT_VERSION = 3

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

# Substrings in wrangler errors that mean "object genuinely absent".
_R2_ABSENT_MARKERS = ("does not exist", "not found", "404", "10007")


def _type_theme_metadata():
    """Return the deterministic, versioned type-to-theme contract."""
    return {
        "version": TYPE_THEME_MAP_VERSION,
        "types": dict(sorted(TYPE_THEME_MAP.items())),
    }


# ---------------------------------------------------------------------------
# Retry / connection primitives
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


# ---------------------------------------------------------------------------
# R2 object I/O (wrangler)
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


# ---------------------------------------------------------------------------
# Staging markers (write == attest after data, read == verify)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Content-addressed stage inventories and dictionary
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Inventory / dictionary I/O (publish before marker, load with SHA binding)
# ---------------------------------------------------------------------------

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


def _load_locator_dictionary_reference(r2_config, version, release_version):
    _, _, reference = _load_locator_manifest_and_dictionary(
        r2_config, version, release_version)
    return reference


def _load_locator_dictionary_binding(r2_config, version, release_version):
    _, dictionary, reference = _load_locator_manifest_and_dictionary(
        r2_config, version, release_version
    )
    return reference, _require_locator_input_inventory_set_sha(dictionary)


# ---------------------------------------------------------------------------
# Producer build-range marker inventory (the finalizer's canonical source)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Build-phase staging guard
# ---------------------------------------------------------------------------

def _release_staging_dir(path):
    """Return the ``id-release-<theme>-<type>`` staging dir a staged file lives in."""
    tail = path.split("/staging/", 1)[-1]
    return tail.split("/", 1)[0]


def reconcile_build_release_staging(r2_config, version, release_files):
    """Fail closed unless staged release files exactly match their markers.

    The build phase consumes whatever release parquet the raw staging glob
    returns; that glob alone trusts staged files without proving a completed
    per-type ``_SUCCESS`` marker still stands behind them. A half-staged,
    cleared, or orphaned type could otherwise be silently merged into (or
    dropped from) the shards. Reconcile both directions before any file is
    read: every current per-type marker must have staged files, and every
    staged file must have a current per-type marker.
    """
    con = _r2_con(r2_config)
    try:
        marker_paths = _retry_transient(lambda: _glob_files(
            con,
            f"s3://{r2_config['bucket']}/{version}/staging/id-release-*/_SUCCESS",
        ))()
    finally:
        con.close()
    marker_dirs = set()
    for marker_path in marker_paths:
        staging_dir = marker_path.split("/staging/", 1)[-1].rsplit("/_SUCCESS", 1)[0]
        if _marker_is_current(_read_staging_marker(r2_config, version, staging_dir)):
            marker_dirs.add(staging_dir)
    file_dirs = {_release_staging_dir(path) for path in release_files}
    marker_without_files = sorted(marker_dirs - file_dirs)
    files_without_marker = sorted(file_dirs - marker_dirs)
    if marker_without_files or files_without_marker:
        raise RuntimeError(
            "Build release staging does not reconcile with per-type markers: "
            f"marker-present/file-missing={marker_without_files[:10]}, "
            f"file-present/marker-missing={files_without_marker[:10]}"
        )
