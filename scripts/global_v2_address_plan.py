#!/usr/bin/env python3
"""Validate global address map outputs and build the stable reduce matrix.

This is the address map/reduce trust boundary.  It accepts only the exact task
set from a canonical row-group inventory, verifies every completion, manifest,
fragment header and content digest, and stages or references immutable reduce
inputs. Maximum-bucket aggregation uses a bounded-cache disk table; exact
lookup fanout is completed distributively by the stable leaf reducers.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import global_v2_address_map as address_map  # noqa: E402
import inventory_address_rowgroups as address_inventory  # noqa: E402
import v2_release_manifest  # noqa: E402
from address_partition import (  # noqa: E402
    AddressPartition,
    COUNT_SCHEMA,
    DEFAULT_MAXIMUM_HASH_BITS,
    DEFAULT_SHARD_ROW_CAP,
    NORMALIZATION_VERSION,
    PLAN_SCHEMA,
    PARTITION_SCHEME,
    partition_id,
    validate_split_ids,
    validate_plan as validate_partition_plan,
)
from experiment_address_reduce import (  # noqa: E402
    STRICT_REJECTION_PRECEDENCE,
    canonical_json,
    decode_record,
    encode_record,
    record_key,
    sha256_file,
)


FANIN_SCHEMA = "overture-global-v2-address-fanin-completion-v1"
REDUCE_PLAN_SCHEMA = "overture-global-v2-address-reduce-plan-v1"
REDUCE_JOB_KIND = "address-reduce-job-v1"
PARTITION_LINEAGE_SCHEMA = "overture-global-v2-address-partition-lineage-v1"
RUNTIME_CONTRACT_SCHEMA = "overture-address-serving-runtime-v1"
SEMANTIC_BINDING_SCHEMA = address_map.SEMANTIC_BINDING_SCHEMA
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_MAP_TASKS = 128
MAX_REDUCE_JOBS = 256
MAX_SERVING_ROUTES = 4_096
MAX_FRAGMENT_JOB_REFERENCES = 8
MAX_SOURCE_FRAGMENTS = 1_000_000
DEFAULT_PAGE_ROWS = 256
DEFAULT_SPARSE_STRIDE = 512
DEFAULT_MAX_PAGE_ROWS = 10_000
DEFAULT_BUCKET_DB_CACHE_KIB = 64 * 1024
MAX_BUCKET_DB_CACHE_KIB = 256 * 1024
REJECTION_KEYS = tuple(STRICT_REJECTION_PRECEDENCE)
DUCKDB_RUNTIME_EVIDENCE_SCHEMA = "overture-duckdb-runtime-evidence-v1"
DUCKDB_STAGE_CONFIGURATION = {
    "address-summary-aggregation-v1": {
        "threads": 2,
        "memory_limit": "512MiB",
        "max_temp_directory_size": "8GiB",
        "preserve_insertion_order": False,
    }
}


SemanticAccumulator = address_map.SemanticAccumulator
validate_semantic_binding = address_map.validate_semantic_binding


def configure_duckdb_stage(
    database: Any, *, stage: str, temp_directory: Path
) -> dict[str, Any]:
    """Apply and report the effective bounded settings for one DuckDB stage."""

    requested = DUCKDB_STAGE_CONFIGURATION.get(stage)
    if requested is None:
        raise ValueError("unknown address DuckDB stage")
    spill_directory = temp_directory / f"duckdb-{stage}"
    spill_directory.mkdir(parents=True, exist_ok=True)

    def quoted(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    database.execute(f"SET threads={requested['threads']}")
    database.execute(f"SET memory_limit={quoted(requested['memory_limit'])}")
    database.execute(f"SET temp_directory={quoted(str(spill_directory))}")
    database.execute(
        "SET max_temp_directory_size="
        f"{quoted(requested['max_temp_directory_size'])}"
    )
    database.execute(
        "SET preserve_insertion_order="
        + ("true" if requested["preserve_insertion_order"] else "false")
    )
    names = (
        "threads",
        "memory_limit",
        "temp_directory",
        "max_temp_directory_size",
        "preserve_insertion_order",
    )
    effective = dict(
        database.execute(
            "SELECT name, value FROM duckdb_settings() "
            f"WHERE name IN ({','.join(quoted(name) for name in names)}) "
            "ORDER BY name"
        ).fetchall()
    )
    if (
        set(effective) != set(names)
        or effective["threads"] != str(requested["threads"])
        or effective["temp_directory"] != str(spill_directory)
        or effective["preserve_insertion_order"] != "false"
    ):
        raise ValueError("effective DuckDB settings differ from bounded request")
    return {
        "schema": DUCKDB_RUNTIME_EVIDENCE_SCHEMA,
        "stage": stage,
        "engine": "duckdb",
        "version": database.execute("SELECT version()").fetchone()[0],
        "requested": {
            **requested,
            "temp_directory": f"task-local/{stage}",
        },
        "effective": {
            **{
                name: value
                for name, value in effective.items()
                if name != "temp_directory"
            },
            "temp_directory": f"task-local/{stage}",
            "temp_directory_matches_requested": True,
        },
    }


def combine_semantic_bindings(
    bindings: Iterable[dict[str, Any]], *, expected_records: int | None = None
) -> dict[str, Any]:
    accumulator = SemanticAccumulator()
    for binding in bindings:
        accumulator.combine(binding)
    result = accumulator.finish()
    if expected_records is not None and result["records"] != expected_records:
        raise ValueError("combined address semantic binding records differ")
    return result


def canonical_semantic_payload(payload: bytes, expected_key: tuple[str, ...]) -> bytes:
    record = decode_record(payload)
    if (
        record["key"] != expected_key
        or record_key(record) != expected_key
        or encode_record(record) != payload
    ):
        raise ValueError("address record is not a canonical full semantic encoding")
    return payload


def value_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def require_sha256(value: Any, field: str) -> str:
    address_map.require_sha256(value, field)
    return value


def safe_relative_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field} must be a safe relative path")
    return path


def load_json(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
    if (
        not path.is_file()
        or path.stat().st_size <= 0
        or path.stat().st_size > max_bytes
    ):
        raise ValueError(f"JSON input is absent or outside its byte cap: {path}")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"JSON input must contain an object: {path}")
    return value


def json_payload(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def write_create_or_verify(path: Path, value: Any) -> dict[str, Any]:
    payload = json_payload(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise ValueError(f"existing immutable JSON output differs: {path}")
    else:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
            ) as output:
                temporary = Path(output.name)
                output.write(payload)
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return {
        "relative_path": path.as_posix(),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _publish_existing_file(path: Path, temporary: Path) -> dict[str, Any]:
    size = temporary.stat().st_size
    digest = sha256_file(temporary)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.stat().st_size != size or sha256_file(path) != digest:
            raise ValueError(f"existing immutable output differs: {path}")
        temporary.unlink()
    else:
        os.replace(temporary, path)
    return {"relative_path": path.as_posix(), "bytes": size, "sha256": digest}


def write_bucket_counts(
    path: Path,
    connection: sqlite3.Connection,
    *,
    release: str,
    maximum_hash_bits: int,
    retained_rows: int,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    entries = rows = 0
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(
                canonical_json(
                    {
                        "schema": COUNT_SCHEMA,
                        "overture_release": release,
                        "maximum_hash_bits": maximum_hash_bits,
                        "scope": "global",
                        "encoding": "canonical-json-lines-v1",
                    }
                )
                + b"\n"
            )
            for country, bucket, count in connection.execute(
                "SELECT country, bucket, expected FROM counts ORDER BY country, bucket"
            ):
                output.write(
                    canonical_json(
                        {"country": country, "bucket": bucket, "rows": count}
                    )
                    + b"\n"
                )
                entries += 1
                rows += count
        if rows != retained_rows or entries <= 0:
            raise ValueError("global address bucket-count artifact does not reconcile")
        identity = _publish_existing_file(path, temporary)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    identity.update({"entries": entries, "records": rows, "schema": COUNT_SCHEMA})
    return identity


def build_partition_plan_from_counts(
    connection: sqlite3.Connection,
    *,
    release: str,
    maximum_hash_bits: int,
    row_cap: int,
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    sticky_values: list[str] = []
    sticky_countries: set[str] = set()
    if previous is not None:
        previous_leaves = validate_partition_plan(
            previous, maximum_hash_bits_at_most=maximum_hash_bits
        )
        if len(previous_leaves) > MAX_SERVING_ROUTES:
            raise ValueError("address predecessor exceeds the serving-route hard cap")
        sticky_values = previous["partition"]["split_ids"]
        sticky_countries = {item["country"] for item in previous_leaves}
    sticky = validate_split_ids(sticky_values, maximum_hash_bits=maximum_hash_bits)
    if type(row_cap) is not int or row_cap < 1:
        raise ValueError("address partition row cap must be a positive integer")

    # Query only one prefix total at a time.  The earlier list-based planner
    # materialized every populated maximum-bit bucket for a country, which made
    # memory proportional to bucket cardinality despite the disk-backed count
    # table.  Prefix-range SUMs use the table's (country, bucket) primary key and
    # keep live Python state proportional to the bounded serving tree.
    partitions: list[AddressPartition] = []
    splits = set(sticky)

    def plan_prefix(country: str, prefix: str) -> None:
        shift = maximum_hash_bits - len(prefix)
        prefix_value = int(prefix or "0", 2)
        bucket_start = prefix_value << shift
        bucket_end = ((prefix_value + 1) << shift) - 1
        result = connection.execute(
            "SELECT COALESCE(SUM(expected), 0) FROM counts "
            "WHERE country=? AND bucket BETWEEN ? AND ?",
            (country, bucket_start, bucket_end),
        ).fetchone()
        rows = result[0]
        if type(rows) is not int or rows < 0:
            raise ValueError("address bucket prefix total is invalid")
        must_split = (country, prefix) in sticky or rows > row_cap
        if must_split:
            if len(prefix) >= maximum_hash_bits:
                raise ValueError(
                    f"address partition {partition_id(country, prefix)} has "
                    f"{rows} rows above cap {row_cap} at the maximum hash level"
                )
            splits.add((country, prefix))
            plan_prefix(country, prefix + "0")
            plan_prefix(country, prefix + "1")
            return
        start = prefix_value << (64 - len(prefix)) if prefix else 0
        end = start + (1 << (64 - len(prefix))) - 1
        partitions.append(
            AddressPartition(
                id=partition_id(country, prefix),
                country=country,
                hash_prefix=prefix,
                hash_bits=len(prefix),
                hash_start=start,
                hash_end=end,
                rows=rows,
            )
        )
        if len(partitions) > MAX_SERVING_ROUTES:
            raise ValueError(
                "address partition count exceeds the serving-route hard cap"
            )

    seen_countries: set[str] = set()
    for (country,) in connection.execute(
        "SELECT DISTINCT country FROM counts ORDER BY country"
    ):
        address_map.validate_country(country)
        plan_prefix(country, "")
        seen_countries.add(country)
    for country in sorted(sticky_countries - seen_countries):
        address_map.validate_country(country)
        plan_prefix(country, "")
    if not partitions:
        raise ValueError("address partition input contains no retained rows")
    partitions.sort(key=lambda item: (item.country, item.hash_start))
    split_ids = sorted(f"{country}:{prefix}" for country, prefix in splits)
    return {
        "schema": PLAN_SCHEMA,
        "overture_release": release,
        "normalization_version": NORMALIZATION_VERSION,
        "partition": {
            "scheme": PARTITION_SCHEME,
            "maximum_hash_bits": maximum_hash_bits,
            "split_row_cap": row_cap,
            "split_ids": split_ids,
        },
        "totals": {
            "retained_rows": sum(item.rows for item in partitions),
            "partitions": len(partitions),
            "nonempty_partitions": sum(item.rows > 0 for item in partitions),
            "empty_partitions": sum(item.rows == 0 for item in partitions),
        },
        "partitions": [asdict(item) for item in partitions],
    }


def serving_runtime_contract() -> dict[str, Any]:
    import platform
    import zlib

    producer_names = (
        "experiment_address_reduce.py",
        "experiment_address_compression.py",
        "build_address_shard.py",
    )
    return {
        "schema": RUNTIME_CONTRACT_SCHEMA,
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform_system": platform.system(),
        "platform_machine": platform.machine(),
        "byteorder": sys.byteorder,
        "zlib_compile_version": zlib.ZLIB_VERSION,
        "zlib_runtime_version": zlib.ZLIB_RUNTIME_VERSION,
        "gzip": {"compresslevel": 6, "mtime": 0},
        "producer_sha256": {
            name: sha256_file(SCRIPT_DIR / name) for name in producer_names
        },
    }


def parse_fetch_command(value: str | None) -> list[str] | None:
    """Parse a no-shell argv JSON adapter used for one-object-at-a-time fetches."""

    if value is None:
        return None
    try:
        argv = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("fragment fetch command must be a JSON argv array") from exc
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
        or sum(item.count("{output}") for item in argv) != 1
        or sum(item.count("{object_key}") for item in argv) < 1
    ):
        raise ValueError(
            "fragment fetch command must be non-empty argv with {object_key} and one {output}"
        )
    return argv


def materialized_fragment(
    fragment: dict[str, Any],
    temporary_dir: Path,
    *,
    fetch_command: list[str] | None,
) -> tuple[Path, bool]:
    """Resolve a local fragment or fetch exactly one remote object without a shell."""

    source = fragment.get("source_path")
    if isinstance(source, Path) and source.is_file():
        return source, False
    if fetch_command is None:
        raise ValueError(
            "address fragment is not local and no fetch adapter was supplied"
        )
    object_key = fragment.get("object_key")
    if not isinstance(object_key, str) or not object_key:
        raise ValueError("remote address fragment omits its object key")
    temporary_dir.mkdir(parents=True, exist_ok=True)
    output = temporary_dir / f"{fragment['sha256']}.bin"
    if output.exists():
        raise ValueError("fragment fetch adapter target unexpectedly exists")
    argv = [
        item.replace("{object_key}", object_key).replace("{output}", str(output))
        for item in fetch_command
    ]
    try:
        subprocess.run(argv, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        output.unlink(missing_ok=True)
        raise ValueError("fragment fetch adapter failed") from exc
    if not output.is_file():
        raise ValueError("fragment fetch adapter did not create its output")
    return output, True


def _copy_content_addressed(source: Path, target: Path, digest: str, size: int) -> None:
    if (
        not source.is_file()
        or source.stat().st_size != size
        or sha256_file(source) != digest
    ):
        raise ValueError("address map fragment content differs from its manifest")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.stat().st_size != size or sha256_file(target) != digest:
            raise ValueError("existing content-addressed reduce input differs")
        return
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".address-reduce-input-",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as output:
            temporary = Path(output.name)
        try:
            temporary.unlink()
            os.link(source, temporary)
        except OSError:
            temporary.unlink(missing_ok=True)
            shutil.copyfile(source, temporary)
        if temporary.stat().st_size != size or sha256_file(temporary) != digest:
            raise ValueError("staged address reduce input differs from its source")
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _expected_task_identity(
    inventory_sha256: str, task: dict[str, Any]
) -> dict[str, Any]:
    return {
        "inventory_sha256": inventory_sha256,
        "task_index": task["index"],
        "task_digest_sha256": task["task_digest_sha256"],
        "task_source_digest_sha256": task["source_digest_sha256"],
        "execution_bucket": task["execution_bucket"],
    }


def _validate_accounting(accounting: Any) -> dict[str, Any]:
    if not isinstance(accounting, dict):
        raise ValueError("address map completion omits accounting")
    input_rows = accounting.get("input_rows")
    retained_rows = accounting.get("retained_rows")
    rejected_rows = accounting.get("rejected_rows")
    rejections = accounting.get("rejections")
    if (
        any(
            type(value) is not int or value < 0
            for value in (input_rows, retained_rows, rejected_rows)
        )
        or accounting.get("reconciles") is not True
        or input_rows != retained_rows + rejected_rows
        or not isinstance(rejections, dict)
        or set(rejections) != set(REJECTION_KEYS)
        or any(type(value) is not int or value < 0 for value in rejections.values())
        or rejected_rows != sum(rejections.values())
    ):
        raise ValueError("address map accounting does not reconcile exactly")
    return accounting


def _validate_counts(
    completion: dict[str, Any], maximum_hash_bits: int
) -> list[dict[str, int | str]]:
    counts = completion.get("partition_counts")
    if (
        not isinstance(counts, dict)
        or counts.get("schema") != COUNT_SCHEMA
        or counts.get("overture_release") != completion.get("overture_release")
        or counts.get("maximum_hash_bits") != maximum_hash_bits
        or counts.get("scope") != "execution_bucket"
        or not isinstance(counts.get("counts"), list)
    ):
        raise ValueError("address map partition counts are incompatible")
    previous: tuple[str, int] | None = None
    rows = 0
    for item in counts["counts"]:
        if not isinstance(item, dict) or set(item) != {"country", "bucket", "rows"}:
            raise ValueError("address map count entry fields are invalid")
        country, bucket, count = item["country"], item["bucket"], item["rows"]
        address_map.validate_country(country)
        identity = (country, bucket)
        if (
            type(bucket) is not int
            or not 0 <= bucket < 1 << maximum_hash_bits
            or type(count) is not int
            or count <= 0
            or (previous is not None and identity <= previous)
        ):
            raise ValueError("address map counts are not strictly ordered and bounded")
        previous = identity
        rows += count
    if rows != counts.get("rows") or rows != completion["accounting"]["retained_rows"]:
        raise ValueError("address map count rows differ from retained rows")
    return counts["counts"]


def _validate_map_task(
    completion_path: Path,
    map_root: Path,
    *,
    inventory: dict[str, Any],
    task: dict[str, Any],
    maximum_hash_bits: int,
) -> tuple[
    dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]
]:
    completion = load_json(completion_path)
    task_identity = _expected_task_identity(inventory["inventory_sha256"], task)
    execution = {
        "id": task["execution_bucket"],
        "kind": address_map.EXECUTION_BUCKET_KIND,
        "is_serving_shard_id": False,
    }
    source = completion.get("source")
    projected = source.get("projected_input") if isinstance(source, dict) else None
    if (
        completion.get("schema") != address_map.COMPLETION_SCHEMA
        or completion.get("family") != "addresses"
        or completion.get("overture_release") != inventory["release"]
        or completion.get("normalization_version") != NORMALIZATION_VERSION
        or completion.get("wire_encoding") != address_map.WIRE_ENCODING
        or completion.get("duplicate_id_policy") != address_map.DUPLICATE_ID_POLICY
        or completion.get("execution") != execution
        or completion.get("address_task_identity") != task_identity
        or not isinstance(source, dict)
        or source.get("inventory_sha256") != inventory["source_inventory_sha256"]
        or source.get("schema_fingerprint_sha256")
        != inventory["schema_contract"]["fingerprint_sha256"]
        or source.get("inventory") != inventory["source_inventory"]
        or not isinstance(projected, dict)
        or type(projected.get("bytes")) is not int
        or projected["bytes"] <= 0
        or type(projected.get("records")) is not int
        or projected["records"] < 0
    ):
        raise ValueError("address map completion differs from its canonical task")
    require_sha256(projected.get("sha256"), "projected input sha256")
    configuration = completion.get("configuration")
    if (
        not isinstance(configuration, dict)
        or configuration.get("maximum_hash_bits") != maximum_hash_bits
    ):
        raise ValueError("address map maximum hash level differs")
    accounting = _validate_accounting(completion.get("accounting"))
    declared_counts = _validate_counts(completion, maximum_hash_bits)
    fanout = completion.get("exact_lookup_fanout")
    if (
        not isinstance(fanout, dict)
        or fanout.get("scope") != "execution_bucket"
        or type(fanout.get("maximum_candidates")) is not int
        or not 0 <= fanout["maximum_candidates"] <= accounting["retained_rows"]
        or (
            fanout["maximum_candidates"] == 0
            and fanout.get("normalized_lookup_key") is not None
        )
        or (
            fanout["maximum_candidates"] > 0
            and (
                not isinstance(fanout.get("normalized_lookup_key"), list)
                or len(fanout["normalized_lookup_key"]) != 8
                or any(
                    not isinstance(value, str)
                    for value in fanout["normalized_lookup_key"]
                )
            )
        )
    ):
        raise ValueError("address map fanout summary is invalid")

    manifest_identity = completion.get("fragment_manifest")
    if not isinstance(manifest_identity, dict):
        raise ValueError("address map completion omits its fragment manifest")
    relative_manifest = safe_relative_path(
        manifest_identity.get("relative_path"), "fragment manifest path"
    )
    manifest_path = map_root / relative_manifest
    if (
        not manifest_path.is_file()
        or type(manifest_identity.get("bytes")) is not int
        or manifest_identity["bytes"] <= 0
        or manifest_path.stat().st_size != manifest_identity["bytes"]
        or sha256_file(manifest_path) != manifest_identity.get("sha256")
    ):
        raise ValueError("address map fragment manifest identity differs")
    manifest = load_json(manifest_path)
    ownership_contract = {
        "kind": address_map.FRAGMENT_OWNERSHIP_KIND,
        "maximum_hash_bits": maximum_hash_bits,
        "is_serving_shard_id": False,
    }
    fragments = manifest.get("data_packs")
    if (
        manifest.get("schema") != address_map.FRAGMENT_MANIFEST_SCHEMA
        or manifest.get("overture_release") != inventory["release"]
        or manifest.get("source_inventory_sha256")
        != inventory["source_inventory_sha256"]
        or manifest.get("schema_fingerprint_sha256")
        != inventory["schema_contract"]["fingerprint_sha256"]
        or manifest.get("wire_encoding") != address_map.WIRE_ENCODING
        or manifest.get("execution") != execution
        or manifest.get("address_task_identity") != task_identity
        or manifest.get("intermediate_ownership") != ownership_contract
        or not isinstance(fragments, list)
        or manifest.get("fragments") != fragments
        or len(fragments) != manifest_identity.get("records")
    ):
        raise ValueError("address map fragment manifest differs from completion")
    total_bytes = total_records = 0
    normalized: list[dict[str, Any]] = []
    for expected_index, fragment in enumerate(fragments):
        if not isinstance(fragment, dict) or fragment.get("index") != expected_index:
            raise ValueError("address map fragment indexes are not exact")
        digest = require_sha256(fragment.get("sha256"), "address fragment sha256")
        size, records = fragment.get("bytes"), fragment.get("records")
        relative = safe_relative_path(
            fragment.get("relative_path"), "address fragment path"
        )
        ownership = fragment.get("intermediate_ownership")
        row_groups = fragment.get("row_groups")
        if (
            type(size) is not int
            or size <= 0
            or type(records) is not int
            or records <= 0
            or not isinstance(ownership, dict)
            or ownership
            != address_map.intermediate_ownership(
                ownership.get("country"),
                ownership.get("minimum_bucket"),
                ownership.get("maximum_bucket"),
                maximum_hash_bits,
            )
            or fragment.get("object_key")
            != f"map/address-data-packs/{relative.relative_to('data-packs').as_posix()}"
            or not isinstance(row_groups, list)
            or not row_groups
        ):
            raise ValueError("address map fragment manifest entry is invalid")
        row_group_records = 0
        previous_row_group_end: int | None = None
        for row_group_index, row_group in enumerate(row_groups):
            group_ownership = (
                row_group.get("intermediate_ownership")
                if isinstance(row_group, dict)
                else None
            )
            if (
                not isinstance(row_group, dict)
                or row_group.get("index") != row_group_index
                or type(row_group.get("records")) is not int
                or row_group["records"] <= 0
                or type(row_group.get("compressed_column_bytes")) is not int
                or row_group["compressed_column_bytes"] <= 0
                or not isinstance(group_ownership, dict)
                or group_ownership
                != address_map.intermediate_ownership(
                    group_ownership.get("country"),
                    group_ownership.get("minimum_bucket"),
                    group_ownership.get("maximum_bucket"),
                    maximum_hash_bits,
                )
                or group_ownership["country"] != ownership["country"]
                or group_ownership["minimum_bucket"] < ownership["minimum_bucket"]
                or group_ownership["maximum_bucket"] > ownership["maximum_bucket"]
                or (
                    previous_row_group_end is not None
                    and group_ownership["minimum_bucket"] < previous_row_group_end
                )
                or row_group.get("integrity")
                != {
                    "kind": "canonical-row-multiset-binding-v1",
                    "order_verified_by_consumer": True,
                }
            ):
                raise ValueError("address data-pack row-group proof is invalid")
            validate_semantic_binding(
                row_group.get("semantic_binding"),
                expected_records=row_group["records"],
            )
            row_group_records += row_group["records"]
            previous_row_group_end = group_ownership["maximum_bucket"]
        if row_group_records != records:
            raise ValueError("address data-pack row-group records do not reconcile")
        layout_binding = address_map.validate_parquet_layout_binding(
            fragment.get("parquet_layout_binding"),
            expected_records=records,
            expected_row_groups=len(row_groups),
        )
        object_key = fragment.get("object_key")
        if not isinstance(object_key, str) or not object_key:
            raise ValueError("address map fragment object key is invalid")
        normalized.append(
            {
                "source_path": map_root / relative,
                "object_key": object_key,
                "source_task_index": task["index"],
                "source_fragment_index": expected_index,
                "sha256": digest,
                "bytes": size,
                "records": records,
                "row_groups": row_groups,
                "parquet_layout_binding": layout_binding,
                "intermediate_ownership": ownership,
                "address_task_identity": task_identity,
            }
        )
        total_bytes += size
        total_records += records
    totals = manifest.get("totals")
    expected_totals = {
        "fragments": len(fragments),
        "bytes": total_bytes,
        "records": total_records,
    }
    if (
        totals != expected_totals
        or completion.get("fragment_totals") != expected_totals
        or completion.get("data_packs")
        != {
            "schema": address_map.FRAGMENT_MANIFEST_SCHEMA,
            "objects": fragments,
            "totals": expected_totals,
        }
        or total_records != accounting["retained_rows"]
    ):
        raise ValueError("address map fragment totals do not reconcile")

    summary_identity = completion.get("summary")
    if (
        not isinstance(summary_identity, dict)
        or manifest.get("summary") != summary_identity
        or summary_identity.get("schema") != address_map.SUMMARY_SCHEMA
        or summary_identity.get("object_key")
        != f"map/address-summaries/sha256/{summary_identity.get('sha256')}.parquet"
    ):
        raise ValueError("address map completion omits its exact semantic summary")
    summary_relative = safe_relative_path(
        summary_identity.get("relative_path"), "address summary path"
    )
    summary_path = map_root / summary_relative
    expected_summary_header = {
        "schema": address_map.SUMMARY_SCHEMA,
        "overture_release": inventory["release"],
        "source_inventory_sha256": inventory["source_inventory_sha256"],
        "schema_fingerprint_sha256": inventory["schema_contract"][
            "fingerprint_sha256"
        ],
        "address_task_identity": task_identity,
        "maximum_hash_bits": maximum_hash_bits,
        "entries": summary_identity.get("entries"),
        "records": accounting["retained_rows"],
        "semantic_binding_schema": SEMANTIC_BINDING_SCHEMA,
    }
    _, summary_rows = address_map.read_semantic_summary(
        summary_path,
        expected_identity=summary_identity,
        expected_header=expected_summary_header,
    )
    summary_counts = [
        {
            "country": item["country"],
            "bucket": item["bucket"],
            "rows": item["records"],
        }
        for item in summary_rows
    ]
    if summary_counts != declared_counts:
        raise ValueError("address semantic summary differs from declared bucket counts")
    summary_descriptor = {
        **summary_identity,
        "source_path": summary_path,
        "address_task_identity": task_identity,
        "expected_header": expected_summary_header,
    }
    return completion, manifest_identity, normalized, summary_descriptor, summary_rows


def _combine_summary_bindings(
    summaries: Iterable[dict[str, Any]],
    *,
    leaves: list[dict[str, Any]],
    maximum_hash_bits: int,
) -> tuple[int, dict[str, dict[str, Any]]]:
    """Derive stable-leaf bindings using only compact task summaries."""

    accumulators = {leaf["id"]: SemanticAccumulator() for leaf in leaves}
    routes: dict[str, tuple[list[int], list[dict[str, Any]]]] = {}
    for country in sorted({leaf["country"] for leaf in leaves}):
        country_leaves = sorted(
            [leaf for leaf in leaves if leaf["country"] == country],
            key=lambda leaf: leaf["hash_start"],
        )
        routes[country] = (
            [leaf["hash_start"] for leaf in country_leaves],
            country_leaves,
        )
    rows = 0
    for summary in summaries:
        _, summary_rows = address_map.read_semantic_summary(
            summary["source_path"],
            expected_identity=summary,
            expected_header=summary["expected_header"],
        )
        for item in summary_rows:
            starts, country_leaves = routes[item["country"]]
            bucket_start = item["bucket"] << (64 - maximum_hash_bits)
            route_index = bisect.bisect_right(starts, bucket_start) - 1
            if route_index < 0:
                raise ValueError("address summary bucket has no stable leaf")
            leaf = country_leaves[route_index]
            bucket_end = ((item["bucket"] + 1) << (64 - maximum_hash_bits)) - 1
            if bucket_end > leaf["hash_end"]:
                raise ValueError("address summary bucket crosses stable leaves")
            accumulators[leaf["id"]].combine(item["semantic_binding"])
            rows += item["records"]
    bindings = {identifier: value.finish() for identifier, value in accumulators.items()}
    for leaf in leaves:
        validate_semantic_binding(bindings[leaf["id"]], expected_records=leaf["rows"])
    return rows, bindings


def _aggregate_summary_counts_duckdb(
    summaries: Iterable[dict[str, Any]],
    connection: sqlite3.Connection,
    *,
    temp_directory: Path,
) -> dict[str, Any]:
    """Use bounded DuckDB execution for the planet-wide typed aggregation."""

    import duckdb

    paths = [str(item["source_path"]) for item in summaries]
    if not paths:
        raise ValueError("address summary aggregation has no task inputs")
    database = duckdb.connect(str(temp_directory / "summary-aggregation.duckdb"))
    try:
        runtime = configure_duckdb_stage(
            database,
            stage="address-summary-aggregation-v1",
            temp_directory=temp_directory,
        )
        database.read_parquet(paths).create_view("address_task_summaries")
        reader = database.execute(
            "SELECT country, maximum_bucket, SUM(records)::UBIGINT AS records "
            "FROM address_task_summaries GROUP BY country, maximum_bucket "
            "ORDER BY country, maximum_bucket"
        ).to_arrow_reader(65_536)
        entries = rows = 0
        for batch in reader:
            values = [
                (item["country"], item["maximum_bucket"], item["records"])
                for item in batch.to_pylist()
            ]
            connection.executemany(
                "INSERT INTO counts(country, bucket, expected) VALUES (?, ?, ?)",
                values,
            )
            entries += len(values)
            rows += sum(item[2] for item in values)
    finally:
        database.close()
    return {
        **runtime,
        "package_version": duckdb.__version__,
        "stream_batch_rows": 65_536,
        "entries": entries,
        "records": rows,
    }


def _predecessor_manifest_identity(value: Any) -> dict[str, Any]:
    """Normalize the request-bound predecessor family-manifest triple."""

    if value is None:
        value = {"object_key": None, "bytes": None, "sha256": None}
    if not isinstance(value, dict) or set(value) != {"object_key", "bytes", "sha256"}:
        raise ValueError("address predecessor family manifest must be an exact triple")
    values = (value["object_key"], value["bytes"], value["sha256"])
    if all(item is None for item in values):
        return {"object_key": None, "bytes": None, "sha256": None}
    if any(item is None for item in values):
        raise ValueError("address predecessor family manifest triple is partially null")
    key = value["object_key"]
    parts = key.split("/") if isinstance(key, str) else []
    if (
        not isinstance(key, str)
        or key.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or len(parts) != 4
        or not (
            v2_release_manifest.BUILD_RE.fullmatch(parts[0])
            or v2_release_manifest.SLICE_RE.fullmatch(parts[0])
        )
        or parts[1:] != ["families", "addresses", "family-manifest.json"]
        or type(value["bytes"]) is not int
        or not 1 <= value["bytes"] <= 2**63 - 1
    ):
        raise ValueError("address predecessor family manifest triple is invalid")
    require_sha256(value["sha256"], "address predecessor family manifest sha256")
    return {"object_key": key, "bytes": value["bytes"], "sha256": value["sha256"]}


def partition_lineage(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate and return the durable address partition lineage."""

    build = plan.get("build")
    if not isinstance(build, dict) or set(build) != {
        "sequence",
        "lineage_generation",
        "predecessor",
    }:
        raise ValueError("address partition plan has no exact lineage generation")
    sequence = build["sequence"]
    generation = build["lineage_generation"]
    if type(sequence) is not int or sequence <= 0:
        raise ValueError("address build number must be positive")
    if type(generation) is not int or generation <= 0:
        raise ValueError("address lineage generation must be positive")
    predecessor = build["predecessor"]
    if generation == 1:
        if predecessor is not None:
            raise ValueError("address lineage generation 1 must have no predecessor")
    else:
        if not isinstance(predecessor, dict) or set(predecessor) != {
            "overture_release",
            "lineage_generation",
            "partition_plan_sha256",
            "family_manifest",
        }:
            raise ValueError("address continuation has no exact predecessor provenance")
        if (
            not isinstance(predecessor["overture_release"], str)
            or not predecessor["overture_release"]
            or type(predecessor["lineage_generation"]) is not int
            or predecessor["lineage_generation"] != generation - 1
        ):
            raise ValueError("address predecessor generation is skipped or replayed")
        require_sha256(
            predecessor["partition_plan_sha256"],
            "address predecessor partition plan sha256",
        )
        identity = _predecessor_manifest_identity(predecessor["family_manifest"])
        if identity["object_key"] is None:
            raise ValueError("address continuation predecessor triple must be all-set")
        if predecessor["family_manifest"] != identity:
            raise ValueError("address predecessor family manifest triple is not canonical")
    return {
        "schema": PARTITION_LINEAGE_SCHEMA,
        "lineage_generation": generation,
        "predecessor": predecessor,
    }


def _previous_provenance(
    previous: dict[str, Any] | None,
    *,
    build_number: int,
    lineage_generation: int,
    predecessor_family_manifest: dict[str, Any] | None,
    expected_previous_sha256: str | None,
    maximum_hash_bits: int,
) -> dict[str, Any] | None:
    if type(build_number) is not int or build_number <= 0:
        raise ValueError("address build number must be positive")
    if type(lineage_generation) is not int or lineage_generation <= 0:
        raise ValueError("address lineage generation must be positive")
    manifest_identity = _predecessor_manifest_identity(predecessor_family_manifest)
    if lineage_generation == 1:
        if (
            previous is not None
            or expected_previous_sha256 is not None
            or manifest_identity["object_key"] is not None
        ):
            raise ValueError("address lineage generation 1 must have a null predecessor")
        return None
    if (
        previous is None
        or expected_previous_sha256 is None
        or manifest_identity["object_key"] is None
    ):
        raise ValueError("address continuation requires an exact predecessor triple")
    require_sha256(expected_previous_sha256, "expected predecessor sha256")
    validate_partition_plan(previous, maximum_hash_bits_at_most=maximum_hash_bits)
    previous_lineage = partition_lineage(previous)
    digest = value_sha256(previous)
    contract = previous["partition"]
    if (
        digest != expected_previous_sha256
        or previous_lineage["lineage_generation"] != lineage_generation - 1
        or previous.get("normalization_version") != NORMALIZATION_VERSION
        or contract.get("scheme") != PARTITION_SCHEME
    ):
        raise ValueError("address predecessor is not the exact compatible prior build")
    return {
        "overture_release": previous["overture_release"],
        "lineage_generation": lineage_generation - 1,
        "partition_plan_sha256": digest,
        "family_manifest": manifest_identity,
    }


def _assign_jobs(
    leaves: list[dict[str, Any]],
    inputs: list[dict[str, Any]],
    *,
    max_jobs: int,
) -> list[dict[str, Any]]:
    nonempty = [leaf for leaf in leaves if leaf["rows"] > 0]
    if not nonempty:
        raise ValueError("address reduce plan has no non-empty leaves")
    if not 1 <= max_jobs <= MAX_REDUCE_JOBS:
        raise ValueError("address reduce job cap is outside hard bounds")
    job_count = min(max_jobs, len(nonempty))
    ordered = sorted(nonempty, key=lambda item: (item["country"], item["hash_start"]))
    assignments: list[list[dict[str, Any]]] = []
    position = 0
    remaining_rows = sum(item["rows"] for item in ordered)
    for job_index in range(job_count):
        remaining_jobs = job_count - job_index
        if remaining_jobs == 1:
            assigned = ordered[position:]
            assignments.append(assigned)
            break
        target = remaining_rows / remaining_jobs
        assigned = []
        assigned_rows = 0
        while len(ordered) - position > remaining_jobs - 1:
            leaf = ordered[position]
            if assigned and abs(assigned_rows - target) <= abs(
                assigned_rows + leaf["rows"] - target
            ):
                break
            assigned.append(leaf)
            assigned_rows += leaf["rows"]
            position += 1
        if not assigned:
            assigned = [ordered[position]]
            assigned_rows = assigned[0]["rows"]
            position += 1
        assignments.append(assigned)
        remaining_rows -= assigned_rows
    jobs = []
    for index, assigned in enumerate(assignments):
        input_row_groups = []
        for item in inputs:
            selected = [
                row_group["index"]
                for row_group in item["row_groups"]
                if any(
                    row_group["intermediate_ownership"]["country"] == leaf["country"]
                    and row_group["intermediate_ownership"]["hash_start"]
                    <= leaf["hash_end"]
                    and row_group["intermediate_ownership"]["hash_end"]
                    >= leaf["hash_start"]
                    for leaf in assigned
                )
            ]
            if selected:
                input_row_groups.append(
                    {"input_index": item["index"], "row_group_indexes": selected}
                )
        input_indexes = [item["input_index"] for item in input_row_groups]
        jobs.append(
            {
                "index": index,
                "id": f"address-reduce-job-{index:03d}",
                "kind": REDUCE_JOB_KIND,
                "is_serving_shard_id": False,
                "partition_ids": [leaf["id"] for leaf in assigned],
                "input_indexes": input_indexes,
                "input_row_groups": input_row_groups,
                "expected_rows": sum(leaf["rows"] for leaf in assigned),
                "expected_semantic_binding": combine_semantic_bindings(
                    [leaf["semantic_binding"] for leaf in assigned],
                    expected_records=sum(leaf["rows"] for leaf in assigned),
                ),
            }
        )
    references: Counter[tuple[int, int]] = Counter(
        (assignment["input_index"], row_group_index)
        for job in jobs
        for assignment in job["input_row_groups"]
        for row_group_index in assignment["row_group_indexes"]
    )
    if references and max(references.values()) > MAX_FRAGMENT_JOB_REFERENCES:
        if job_count == 1:
            raise ValueError("address fragment/reduce-job fanout exceeds its hard cap")
        return _assign_jobs(
            leaves,
            inputs,
            max_jobs=max(1, job_count // 2),
        )
    return jobs


def build_fanin_plan(
    inventory: dict[str, Any],
    map_tasks: Iterable[tuple[Path, Path]],
    output_root: Path,
    *,
    build_number: int,
    lineage_generation: int = 1,
    predecessor_family_manifest: dict[str, Any] | None = None,
    previous_plan: dict[str, Any] | None = None,
    expected_previous_sha256: str | None = None,
    maximum_hash_bits: int = DEFAULT_MAXIMUM_HASH_BITS,
    row_cap: int = DEFAULT_SHARD_ROW_CAP,
    max_reduce_jobs: int = MAX_REDUCE_JOBS,
    max_source_fragments: int = MAX_SOURCE_FRAGMENTS,
    fragment_fetch_command: list[str] | None = None,
    stage_local_fragments: bool = True,
    page_rows: int = DEFAULT_PAGE_ROWS,
    sparse_stride: int = DEFAULT_SPARSE_STRIDE,
    max_page_rows: int = DEFAULT_MAX_PAGE_ROWS,
    bucket_db_cache_kib: int = DEFAULT_BUCKET_DB_CACHE_KIB,
) -> dict[str, Any]:
    identity = address_inventory.validate_canonical_inventory(inventory)
    tasks = identity["tasks"]
    supplied = list(map_tasks)
    if not tasks or len(tasks) > MAX_MAP_TASKS or len(supplied) != len(tasks):
        raise ValueError("address map completion matrix is missing or outside its cap")
    if (
        not 1 <= page_rows <= 4_096
        or sparse_stride <= 0
        or max_page_rows <= 0
        or page_rows > max_page_rows
        or not 1 <= bucket_db_cache_kib <= MAX_BUCKET_DB_CACHE_KIB
        or type(max_source_fragments) is not int
        or not 1 <= max_source_fragments <= MAX_SOURCE_FRAGMENTS
    ):
        raise ValueError("address fan-in configuration is outside hard bounds")
    predecessor = _previous_provenance(
        previous_plan,
        build_number=build_number,
        lineage_generation=lineage_generation,
        predecessor_family_manifest=predecessor_family_manifest,
        expected_previous_sha256=expected_previous_sha256,
        maximum_hash_bits=maximum_hash_bits,
    )
    completion_paths = [path.resolve() for path, _ in supplied]
    if len(set(completion_paths)) != len(completion_paths):
        raise ValueError("address map completion paths must be unique")
    output_root.mkdir(parents=True, exist_ok=True)

    bucket_temp = tempfile.TemporaryDirectory(
        prefix=".address-bucket-counts-", dir=output_root
    )
    connection = sqlite3.connect(Path(bucket_temp.name) / "counts.sqlite3")
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA mmap_size=0")
    connection.execute(f"PRAGMA cache_size=-{bucket_db_cache_kib}")
    connection.execute(
        "CREATE TABLE counts ("
        "country TEXT NOT NULL, bucket INTEGER NOT NULL, "
        "expected INTEGER NOT NULL, observed INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (country, bucket)) WITHOUT ROWID"
    )
    by_index: dict[int, dict[str, Any]] = {}
    manifest_digests: set[str] = set()
    fragment_digests: set[str] = set()
    fragments: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    aggregate_rejections: Counter[str] = Counter()
    input_rows = retained_rows = rejected_rows = 0
    for completion_path, map_root in supplied:
        raw = load_json(completion_path)
        raw_identity = raw.get("address_task_identity")
        task_index = (
            raw_identity.get("task_index") if isinstance(raw_identity, dict) else None
        )
        if (
            type(task_index) is not int
            or not 0 <= task_index < len(tasks)
            or task_index in by_index
        ):
            raise ValueError(
                "address map task identity is missing, duplicated, or replayed"
            )
        validated = _validate_map_task(
            completion_path,
            map_root,
            inventory=inventory,
            task=tasks[task_index],
            maximum_hash_bits=maximum_hash_bits,
        )
        (
            completion,
            manifest_identity,
            task_fragments,
            summary_descriptor,
            _summary_rows,
        ) = validated
        manifest_digest = manifest_identity["sha256"]
        if manifest_digest in manifest_digests:
            raise ValueError("address fragment manifest was replayed")
        manifest_digests.add(manifest_digest)
        for fragment in task_fragments:
            if fragment["sha256"] in fragment_digests:
                raise ValueError("address map fragment content was replayed")
            fragment_digests.add(fragment["sha256"])
        if len(fragments) + len(task_fragments) > max_source_fragments:
            raise ValueError("address source fragment count exceeds its hard cap")
        fragments.extend(task_fragments)
        summaries.append(summary_descriptor)
        accounting = completion["accounting"]
        input_rows += accounting["input_rows"]
        retained_rows += accounting["retained_rows"]
        rejected_rows += accounting["rejected_rows"]
        aggregate_rejections.update(accounting["rejections"])
        by_index[task_index] = {
            "fragment_manifest": manifest_identity,
            "summary": {
                key: value
                for key, value in summary_descriptor.items()
                if key not in {"source_path", "expected_header", "address_task_identity"}
            },
            "maximum_candidates": completion["exact_lookup_fanout"][
                "maximum_candidates"
            ],
        }
    if set(by_index) != set(range(len(tasks))):
        raise ValueError("address map completion matrix is not exact")
    if input_rows != retained_rows + rejected_rows or rejected_rows != sum(
        aggregate_rejections.values()
    ):
        raise ValueError("global address map accounting does not reconcile")

    duckdb_aggregation = _aggregate_summary_counts_duckdb(
        summaries,
        connection,
        temp_directory=Path(bucket_temp.name),
    )
    if duckdb_aggregation["records"] != retained_rows:
        raise ValueError("DuckDB address summary aggregation does not reconcile")
    connection.commit()
    partition_plan = build_partition_plan_from_counts(
        connection,
        release=inventory["release"],
        maximum_hash_bits=maximum_hash_bits,
        row_cap=row_cap,
        previous=previous_plan,
    )
    leaves = validate_partition_plan(partition_plan)
    scanned_rows, semantic_bindings = _combine_summary_bindings(
        summaries,
        leaves=leaves,
        maximum_hash_bits=maximum_hash_bits,
    )
    connection.commit()
    if scanned_rows != retained_rows:
        raise ValueError("global summary aggregation differs from exact map counts")
    for leaf in partition_plan["partitions"]:
        leaf["semantic_binding"] = semantic_bindings[leaf["id"]]

    staged_inputs = []
    for index, fragment in enumerate(
        sorted(
            fragments,
            key=lambda item: (
                item["intermediate_ownership"]["country"],
                item["intermediate_ownership"]["minimum_bucket"],
                item["source_task_index"],
                item["source_fragment_index"],
            ),
        )
    ):
        relative: Path | None = None
        if stage_local_fragments:
            if (
                fragment_fetch_command is not None
                and not fragment["source_path"].is_file()
            ):
                raise ValueError(
                    "remote fragment staging would exceed the streaming contract"
                )
            relative = (
                Path("families/addresses/reduce-inputs/sha256")
                / f"{fragment['sha256']}.parquet"
            )
            _copy_content_addressed(
                fragment["source_path"],
                output_root / relative,
                fragment["sha256"],
                fragment["bytes"],
            )
        item = {
            "index": index,
            "format": address_map.WIRE_ENCODING,
            "object_key": fragment["object_key"],
            "sha256": fragment["sha256"],
            "bytes": fragment["bytes"],
            "records": fragment["records"],
            "row_groups": fragment["row_groups"],
            "parquet_layout_binding": fragment["parquet_layout_binding"],
            "source_task_index": fragment["source_task_index"],
            "source_fragment_index": fragment["source_fragment_index"],
            "address_task_identity": fragment["address_task_identity"],
            "intermediate_ownership": fragment["intermediate_ownership"],
        }
        if relative is not None:
            item["relative_path"] = relative.as_posix()
        staged_inputs.append(item)

    bucket_counts_path = output_root / "families/addresses/maximum-bucket-counts.jsonl"
    bucket_counts_identity = write_bucket_counts(
        bucket_counts_path,
        connection,
        release=inventory["release"],
        maximum_hash_bits=maximum_hash_bits,
        retained_rows=retained_rows,
    )
    bucket_counts_identity["relative_path"] = bucket_counts_path.relative_to(
        output_root
    ).as_posix()
    partition_plan.update(
        {
            "build": {
                "sequence": build_number,
                "lineage_generation": lineage_generation,
                "predecessor": predecessor,
            },
            "source": {
                "inventory_sha256": inventory["inventory_sha256"],
                "source_inventory_sha256": inventory["source_inventory_sha256"],
                "schema_fingerprint_sha256": inventory["schema_contract"][
                    "fingerprint_sha256"
                ],
                "map_completion_set_sha256": value_sha256(
                    [
                        {
                            "data_pack_manifest": by_index[index]["fragment_manifest"],
                            "summary": by_index[index]["summary"],
                        }
                        for index in sorted(by_index)
                    ]
                ),
            },
            "accounting": {
                "input_rows": input_rows,
                "retained_rows": retained_rows,
                "rejected_rows": rejected_rows,
                "rejections": dict(sorted(aggregate_rejections.items())),
                "exact_lookup_fanout": {
                    "scope": "global",
                    "status": "computed-by-exact-leaf-reducers",
                    "task_maximum_lower_bound": max(
                        by_index[index]["maximum_candidates"] for index in by_index
                    ),
                },
                "maximum_bucket_counts": bucket_counts_identity,
            },
        }
    )
    connection.close()
    bucket_temp.cleanup()
    leaves = validate_partition_plan(partition_plan)
    lineage = partition_lineage(partition_plan)
    for leaf in leaves:
        validate_semantic_binding(
            leaf.get("semantic_binding"), expected_records=leaf["rows"]
        )
    if (
        len(leaves) > MAX_SERVING_ROUTES
        or sum(leaf["rows"] for leaf in leaves) != retained_rows
    ):
        raise ValueError(
            "address retained rows do not map exactly once to stable leaves"
        )

    partition_path = output_root / "families/addresses/partition-plan.json"
    partition_identity = write_create_or_verify(partition_path, partition_plan)
    partition_identity["relative_path"] = partition_path.relative_to(
        output_root
    ).as_posix()
    jobs = _assign_jobs(leaves, staged_inputs, max_jobs=max_reduce_jobs)
    runtime = serving_runtime_contract()
    reduce_plan = {
        "schema": REDUCE_PLAN_SCHEMA,
        "overture_release": inventory["release"],
        "normalization_version": NORMALIZATION_VERSION,
        "partition_lineage": lineage,
        "source": partition_plan["source"],
        "partition_plan": partition_identity,
        "serving_runtime_contract": runtime,
        "serving_configuration": {
            "variant": "useful_gzip",
            "page_rows": page_rows,
            "sparse_stride": sparse_stride,
            "max_page_rows": max_page_rows,
        },
        "inputs": staged_inputs,
        "jobs": jobs,
        "totals": {
            "inputs": len(staged_inputs),
            "input_bytes": sum(item["bytes"] for item in staged_inputs),
            "input_records": sum(item["records"] for item in staged_inputs),
            "jobs": len(jobs),
            "nonempty_partitions": sum(leaf["rows"] > 0 for leaf in leaves),
            "expected_rows": sum(job["expected_rows"] for job in jobs),
            "input_references": sum(
                len(assignment["row_group_indexes"])
                for job in jobs
                for assignment in job["input_row_groups"]
            ),
            "input_reference_bytes": sum(
                staged_inputs[assignment["input_index"]]["row_groups"][row_group_index][
                    "compressed_column_bytes"
                ]
                for job in jobs
                for assignment in job["input_row_groups"]
                for row_group_index in assignment["row_group_indexes"]
            ),
            "maximum_job_inputs": max(
                sum(
                    len(assignment["row_group_indexes"])
                    for assignment in job["input_row_groups"]
                )
                for job in jobs
            ),
            "maximum_fragment_job_references": max(
                Counter(
                    (assignment["input_index"], row_group_index)
                    for job in jobs
                    for assignment in job["input_row_groups"]
                    for row_group_index in assignment["row_group_indexes"]
                ).values(),
                default=0,
            ),
        },
    }
    if (
        reduce_plan["totals"]["input_records"] != retained_rows
        or reduce_plan["totals"]["expected_rows"] != retained_rows
    ):
        raise ValueError("address reduce matrix does not preserve retained rows")
    reduce_path = output_root / "families/addresses/reduce-plan.json"
    reduce_identity = write_create_or_verify(reduce_path, reduce_plan)
    reduce_identity["relative_path"] = reduce_path.relative_to(output_root).as_posix()
    fanin = {
        "schema": FANIN_SCHEMA,
        "overture_release": inventory["release"],
        "inventory_sha256": inventory["inventory_sha256"],
        "source_inventory_sha256": inventory["source_inventory_sha256"],
        "schema_fingerprint_sha256": inventory["schema_contract"]["fingerprint_sha256"],
        "partition_lineage": lineage,
        "map_tasks": {
            "expected": len(tasks),
            "completed": len(by_index),
            "completion_set_sha256": partition_plan["source"][
                "map_completion_set_sha256"
            ],
        },
        "accounting": partition_plan["accounting"],
        "partition_plan": partition_identity,
        "reduce_plan": reduce_identity,
        "runtime": {
            "serving_runtime_contract": runtime,
            "bucket_aggregation": {
                "kind": "typed-parquet-summary-only-v1",
                "payload_data_packs_opened": 0,
                "engine": duckdb_aggregation,
                "sqlite_runtime_version": sqlite3.sqlite_version,
                "cache_kib_at_most": bucket_db_cache_kib,
            },
        },
    }
    fanin_path = output_root / "families/addresses/fanin-completion.json"
    write_create_or_verify(fanin_path, fanin)
    return fanin


def parse_map_task(value: str) -> tuple[Path, Path]:
    completion, separator, root = value.partition("=")
    if not separator or not completion or not root:
        raise ValueError("--map-task must be COMPLETION_PATH=MAP_ROOT")
    return Path(completion), Path(root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--map-task", action="append", default=[], required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--build-number", type=int, required=True)
    parser.add_argument("--lineage-generation", type=int, required=True)
    parser.add_argument("--predecessor-family-manifest-identity", type=Path)
    parser.add_argument("--previous-plan", type=Path)
    parser.add_argument("--expected-previous-sha256")
    parser.add_argument(
        "--maximum-hash-bits", type=int, default=DEFAULT_MAXIMUM_HASH_BITS
    )
    parser.add_argument("--shard-row-cap", type=int, default=DEFAULT_SHARD_ROW_CAP)
    parser.add_argument("--max-reduce-jobs", type=int, default=MAX_REDUCE_JOBS)
    parser.add_argument(
        "--max-source-fragments", type=int, default=MAX_SOURCE_FRAGMENTS
    )
    parser.add_argument("--page-rows", type=int, default=DEFAULT_PAGE_ROWS)
    parser.add_argument("--sparse-stride", type=int, default=DEFAULT_SPARSE_STRIDE)
    parser.add_argument("--max-page-rows", type=int, default=DEFAULT_MAX_PAGE_ROWS)
    parser.add_argument(
        "--bucket-db-cache-kib", type=int, default=DEFAULT_BUCKET_DB_CACHE_KIB
    )
    parser.add_argument(
        "--fragment-fetch-command-json",
        help="no-shell JSON argv with {object_key} and exactly one {output}",
    )
    parser.add_argument(
        "--stream-remote-fragments",
        action="store_true",
        help="keep R2 map fragments in place instead of staging local copies",
    )
    args = parser.parse_args()
    previous = load_json(args.previous_plan) if args.previous_plan else None
    predecessor_manifest_identity = (
        load_json(args.predecessor_family_manifest_identity)
        if args.predecessor_family_manifest_identity
        else None
    )
    result = build_fanin_plan(
        load_json(args.inventory),
        [parse_map_task(value) for value in args.map_task],
        args.output_root,
        build_number=args.build_number,
        lineage_generation=args.lineage_generation,
        predecessor_family_manifest=predecessor_manifest_identity,
        previous_plan=previous,
        expected_previous_sha256=args.expected_previous_sha256,
        maximum_hash_bits=args.maximum_hash_bits,
        row_cap=args.shard_row_cap,
        max_reduce_jobs=args.max_reduce_jobs,
        max_source_fragments=args.max_source_fragments,
        fragment_fetch_command=parse_fetch_command(args.fragment_fetch_command_json),
        stage_local_fragments=not args.stream_remote_fragments,
        page_rows=args.page_rows,
        sparse_stride=args.sparse_stride,
        max_page_rows=args.max_page_rows,
        bucket_db_cache_kib=args.bucket_db_cache_kib,
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
