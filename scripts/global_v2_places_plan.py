#!/usr/bin/env python3
"""Validate Places map fan-in, plan stable leaves/jobs, and finalize the family.

The build command is the sole fan-in authority for Places map completions. It
validates the canonical inventory and immutable request, consumes every
expected map identity exactly once, hashes every referenced intermediate, and
uses a disk-backed count aggregation to derive sticky world-quadkey leaves.

The finalize command consumes every planned reduce completion exactly once,
proves leaf/shard reconciliation, writes the existing Worker-readable
``catalog.pcat``, and emits a deterministic Places family manifest. It does not
publish or promote any object.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_places_region_shards as places_builder  # noqa: E402
import global_build_manifest  # noqa: E402
import global_v2_build_request  # noqa: E402
from experiment_places_compact_index import (  # noqa: E402
    Place,
    common_prefix,
    decode_record,
    decode_varint,
    encode_record,
    encode_varint,
)
from experiment_places_compact_shard import (  # noqa: E402
    FIELD_BITS as PCSH_FIELD_BITS,
    MAGIC as PCSH_MAGIC,
    PREAMBLE as PCSH_PREAMBLE,
    RECORD_INDEX as PCSH_RECORD_INDEX,
    TOKENIZER_VERSION as PCSH_TOKENIZER_VERSION,
    decode_lexicon_block,
    decode_projection,
    encode_projection,
)
from experiment_places_head_repack import (  # noqa: E402
    HEAD_ADMISSION_MARKER,
    HEAD_KEY_FAMILIES,
    MAGIC as PHRP_MAGIC,
    PREAMBLE as PHRP_PREAMBLE,
    READER_MAX_HEAD_ENTRY_BYTES,
    READER_MAX_HEAD_INDEX_BYTES,
    READER_MAX_HEAD_KEYS,
    READER_MAX_KEY_BYTES,
    encode_key_index,
)
from global_v2_places_inventory import validate_inventory  # noqa: E402
from global_v2_places_map import (  # noqa: E402
    EXECUTION_GROUP_LEVEL,
    MAP_REPORT_SCHEMA,
    MAP_SUMMARY_FAMOUS_CAP,
    MAP_CENSUS_BATCH_ROWS,
    MAP_CENSUS_MAX_SCRATCH_BYTES,
    MAP_CENSUS_MEMORY_LIMIT_BYTES,
    MAP_OUTPUT_BATCH_ROWS,
    DEFAULT_MAX_MAP_WORKSPACE_BYTES,
    REQUIRED_DUCKDB_VERSION as MAP_REQUIRED_DUCKDB_VERSION,
    REJECTION_PRECEDENCE,
    SUMMARY_ARTIFACT_SCHEMA,
    _summary_candidate_sort_key,
    read_map_summary,
)
from experiment_places_compact_index import place_from_row  # noqa: E402
from experiment_places_locality_head import (  # noqa: E402
    FAMOUS_PAIR_TOKEN_LIMIT,
    famous_name_brand_tokens,
    famous_pair_token_key,
)
from places_partition import (  # noqa: E402
    PARTITION_SCHEME,
    plan_partition_cells,
    validate_quadkey,
    validate_split_cells,
)


PLAN_SCHEMA = "overture-global-v2-places-executor-plan-v3"
FINAL_REPORT_SCHEMA = "overture-global-v2-places-final-report-v1"
ARTIFACT_LISTING_SCHEMA = "overture-global-v2-intermediate-listing-v1"
PLAN_VERSION = "3"
HEAD_ADMISSION_VERSION = "head-admission-budgeted-v2"
HEAD_DUPLICATE_GERS_POLICY = "pcsh-preserve-phrp-best-source-occurrence-v1"
HEAD_MAX_ENTRIES_BYTES = 2_000_000_000
HEAD_INDEX_FAMILY_BUDGETS = {
    "famous_exact": 100_000,
    "famous_pair": 150_000,
    "count_exact": 500_000,
    "prefix": READER_MAX_HEAD_INDEX_BYTES - 750_000,
}
REQUIRED_PYTHON_VERSION = "3.11.14"
REQUIRED_PYARROW_VERSION = "25.0.0"
REQUIRED_DUCKDB_VERSION = "1.5.1"
REQUIRED_RUNTIME = {
    "python_version": REQUIRED_PYTHON_VERSION,
    "pyarrow_version": REQUIRED_PYARROW_VERSION,
    "duckdb_version": REQUIRED_DUCKDB_VERSION,
}
MAX_RAW_FRAGMENTS_PER_EXECUTION_GROUP = 2_048
MAX_INPUT_FRAGMENTS_PER_REDUCE_JOB = 65_536
MAX_INPUT_BYTES_PER_REDUCE_JOB = 12_000_000_000
MAX_SELECTED_UNCOMPRESSED_BYTES_PER_REDUCE_JOB = 12_000_000_000
MAX_SIMULTANEOUS_MATERIALIZED_BYTES = 2_000_000_000
SELECTIVE_FETCH_RANGE_ALLOWANCE_BYTES = 64 * 1024
MAX_RETAINED_ROWS_PER_REDUCE_JOB = 12_000_000
PLAN_MAX_SCRATCH_BYTES = 12_000_000_000
PLAN_DUCKDB_MEMORY_LIMIT_BYTES = 256_000_000
PLAN_AGGREGATION_BATCH_ROWS = 10_000
PLAN_MAX_FAMOUS_CANDIDATES_IN_MEMORY = 131_072
REDUCE_MAX_OPEN_FRAGMENT_FILES = 1
REDUCE_MAX_BUFFER_ROWS = 10_000
REDUCE_DUCKDB_MEMORY_LIMIT_BYTES = 512_000_000
REDUCE_MAX_ACTIVE_LEAF_PARTITIONS = 1
HEAD_CANDIDATE_WRITE_BATCH_ROWS = 8_192
HEAD_CANDIDATE_WRITE_BATCH_BYTES = 64_000_000
HEAD_CANDIDATE_MAX_ROW_BYTES = 1_000_000
HEAD_CANDIDATE_MAX_PROJECTION_BYTES_IN_MEMORY = 512_000_000
REDUCE_MAX_LEAF_INPUT_BYTES = 2_000_000_000
REDUCE_MAX_SCRATCH_BYTES = 10_000_000_000
REDUCE_MAX_WORKSPACE_BYTES = 12_000_000_000
REDUCE_MAX_LEAF_ROWS_IN_MEMORY = 1_500_000
REDUCE_MAX_LEAF_TOKEN_OCCURRENCES = 24_000_000
REDUCE_MAX_LEAF_PROJECTION_BYTES = 1_000_000_000
FINALIZE_MAX_STAGED_ARTIFACT_BYTES = 12_000_000_000
MAX_EXECUTION_GROUPS_IN_MEMORY = 4**EXECUTION_GROUP_LEVEL
SHA256_RE = set("0123456789abcdef")
WORLD = [-180.0, -90.0, 180.0, 90.0]


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def require_sha256(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in SHA256_RE for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def require_int(
    value: Any, field: str, *, minimum: int = 0, maximum: int = 2**63 - 1
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def require_exact(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    if set(value) != fields:
        raise ValueError(
            f"{name} fields differ: missing={sorted(fields - set(value))}, "
            f"unknown={sorted(set(value) - fields)}"
        )
    return value


def safe_artifact_path(root: Path, object_key: str) -> Path:
    if (
        not isinstance(object_key, str)
        or object_key.startswith("/")
        or any(part in {"", ".", ".."} for part in object_key.split("/"))
    ):
        raise ValueError("Places artifact key is not a canonical relative path")
    resolved_root = root.resolve()
    path = (resolved_root / object_key).resolve()
    if not path.is_relative_to(resolved_root):
        raise ValueError("Places artifact key escaped its artifact root")
    return path


def verify_artifact(
    root: Path, object_key: str, expected_bytes: int, expected_sha256: str
) -> Path:
    require_int(expected_bytes, f"bytes for {object_key}", minimum=1)
    require_sha256(expected_sha256, f"sha256 for {object_key}")
    path = safe_artifact_path(root, object_key)
    if not path.is_file():
        raise ValueError(f"missing Places intermediate: {object_key}")
    actual_sha256, actual_bytes = sha256_file(path)
    if (actual_bytes, actual_sha256) != (expected_bytes, expected_sha256):
        raise ValueError(f"Places intermediate identity mismatch: {object_key}")
    return path


def normalize_artifact_listing(value: Any | None) -> dict[str, tuple[int, str]] | None:
    if value is None:
        return None
    listing = require_exact(value, {"schema", "objects"}, "intermediate listing")
    if listing["schema"] != ARTIFACT_LISTING_SCHEMA or not isinstance(
        listing["objects"], list
    ):
        raise ValueError(
            f"intermediate listing schema must be {ARTIFACT_LISTING_SCHEMA}"
        )
    result: dict[str, tuple[int, str]] = {}
    for raw in listing["objects"]:
        item = require_exact(
            raw, {"object_key", "bytes", "sha256"}, "intermediate listing object"
        )
        key = item["object_key"]
        safe_artifact_path(Path("."), key)
        require_int(item["bytes"], f"listing bytes for {key}", minimum=1)
        require_sha256(item["sha256"], f"listing sha256 for {key}")
        if key in result:
            raise ValueError(f"duplicate intermediate listing key: {key}")
        result[key] = (item["bytes"], item["sha256"])
    return result


def verify_listed_artifact(
    listing: dict[str, tuple[int, str]] | None,
    object_key: str,
    expected_bytes: int,
    expected_sha256: str,
) -> None:
    if listing is None:
        return
    if listing.get(object_key) != (expected_bytes, expected_sha256):
        raise ValueError(f"remote Places intermediate identity mismatch: {object_key}")


def request_sha256(request: dict[str, Any]) -> str:
    return hashlib.sha256(global_v2_build_request.canonical_json(request)).hexdigest()


class _CountStore:
    ORDERED_QUERY = "SELECT cell, records FROM counts ORDER BY cell"

    def __init__(self, scratch_dir: Path) -> None:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        self.scratch_dir = scratch_dir
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - hosted dependency boundary
            raise RuntimeError("Places planning requires DuckDB") from exc
        if duckdb.__version__ != REQUIRED_DUCKDB_VERSION:
            raise RuntimeError(
                "Places planning requires DuckDB "
                f"{REQUIRED_DUCKDB_VERSION}, found {duckdb.__version__}"
            )
        self.duckdb_version = duckdb.__version__
        self.path = scratch_dir / "places-count-fanin.duckdb"
        self.temp_directory = scratch_dir / "duckdb-spill"
        self.temp_directory.mkdir(exist_ok=True)
        self.connection = duckdb.connect(str(self.path))
        self.connection.execute("SET threads = 1")
        self.connection.execute("SET preserve_insertion_order = false")
        self.connection.execute(
            "SET max_memory = ?", [f"{PLAN_DUCKDB_MEMORY_LIMIT_BYTES}B"]
        )
        self.connection.execute(
            "SET temp_directory = ?", [str(self.temp_directory)]
        )
        self.connection.execute(
            "SET max_temp_directory_size = ?", [f"{PLAN_MAX_SCRATCH_BYTES}B"]
        )
        self.connection.execute(
            "CREATE TABLE counts (cell VARCHAR PRIMARY KEY, records BIGINT NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE exact_counts (value VARCHAR PRIMARY KEY, records BIGINT NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE prefix_counts (value VARCHAR PRIMARY KEY, records BIGINT NOT NULL)"
        )
        self.famous_rows: list[dict[str, Any]] = []
        self.summary_exact_rows = 0
        self.summary_prefix_rows = 0
        self.peak_scratch_bytes = 0
        self.peak_database_bytes = 0
        self.peak_pending_rows = 0
        self.arrow_append_batches = 0
        self.peak_famous_candidates = 0
        self.observe_scratch()

    def observe_scratch(self) -> int:
        current = sum(
            path.stat().st_size
            for path in self.scratch_dir.rglob("*")
            if path.is_file()
        )
        database_bytes = self.path.stat().st_size if self.path.exists() else 0
        self.peak_scratch_bytes = max(self.peak_scratch_bytes, current)
        self.peak_database_bytes = max(self.peak_database_bytes, database_bytes)
        if current > PLAN_MAX_SCRATCH_BYTES:
            raise ValueError("Places planner count store exceeded its hard scratch cap")
        return current

    def evidence(self) -> dict[str, Any]:
        """Return run-local resource observations for diagnostics only."""
        self.observe_scratch()
        return {
            "kind": "duckdb-typed-external-fanin-v1",
            "engine": "duckdb",
            "engine_version": self.duckdb_version,
            "maximum_memory_bytes": PLAN_DUCKDB_MEMORY_LIMIT_BYTES,
            "maximum_scratch_bytes": PLAN_MAX_SCRATCH_BYTES,
            "peak_database_bytes": self.peak_database_bytes,
            "peak_scratch_bytes": self.peak_scratch_bytes,
            "maximum_batch_rows": PLAN_AGGREGATION_BATCH_ROWS,
            "peak_batch_rows": self.peak_pending_rows,
            "arrow_append_batches": self.arrow_append_batches,
            "registered_arrow_batches": True,
            "maximum_famous_candidates_in_memory": (
                PLAN_MAX_FAMOUS_CANDIDATES_IN_MEMORY
            ),
            "peak_famous_candidates_in_memory": self.peak_famous_candidates,
            "group_aggregation": "typed-ordered-external-stream-v1",
            "maximum_execution_groups_in_memory": MAX_EXECUTION_GROUPS_IN_MEMORY,
            "ordered_scan": "duckdb-order-by-cell-v1",
        }

    def deterministic_contract(self) -> dict[str, Any]:
        """Return only stable planner implementation and hard-limit fields."""
        return {
            "kind": "duckdb-typed-external-fanin-v1",
            "engine": "duckdb",
            "engine_version": self.duckdb_version,
            "maximum_memory_bytes": PLAN_DUCKDB_MEMORY_LIMIT_BYTES,
            "maximum_scratch_bytes": PLAN_MAX_SCRATCH_BYTES,
            "maximum_batch_rows": PLAN_AGGREGATION_BATCH_ROWS,
            "registered_arrow_batches": True,
            "maximum_famous_candidates_in_memory": (
                PLAN_MAX_FAMOUS_CANDIDATES_IN_MEMORY
            ),
            "group_aggregation": "typed-ordered-external-stream-v1",
            "maximum_execution_groups_in_memory": MAX_EXECUTION_GROUPS_IN_MEMORY,
            "ordered_scan": "duckdb-order-by-cell-v1",
        }

    def add_artifact(
        self,
        path: Path,
        *,
        maximum_level: int,
        inventory_sha256: str,
        task_digest: str,
    ) -> dict[str, int]:
        cells = records = 0
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:  # pragma: no cover - hosted dependency boundary
            raise RuntimeError("Places summary planning requires pyarrow") from exc
        parquet = pq.ParquetFile(path)
        metadata = {
            key.decode(): value.decode()
            for key, value in (parquet.schema_arrow.metadata or {}).items()
        }
        expected_base_metadata = {
            "artifact_schema": SUMMARY_ARTIFACT_SCHEMA,
            "maximum_level": str(maximum_level),
            "inventory_sha256": inventory_sha256,
            "map_task_digest": task_digest,
            "tokenizer_source": "frozen-python-places-v3",
            "prefix_lengths": "2,3,4,5,6,7,8",
            "famous_candidate_cap": str(MAP_SUMMARY_FAMOUS_CAP),
            "famous_candidate_identity": "gers_id",
            "famous_deduplicate_before_cap": "true",
        }
        if any(metadata.get(key) != value for key, value in expected_base_metadata.items()):
            raise ValueError("Places summary provenance differs from its task")
        pending: dict[str, list[tuple[str, int]]] = {
            "cell": [],
            "exact": [],
            "prefix": [],
        }
        observed = Counter()
        observed_key_bytes = 0
        for value in read_map_summary(path):
            kind = value["kind"]
            key = value["key"]
            count = value["records"]
            if kind == "cell":
                if (
                    len(key) != maximum_level
                    or any(digit not in "0123" for digit in key)
                ):
                    raise ValueError("Places maximum-level summary cell is invalid")
                cells += 1
                records += count
                observed["cell_rows"] += 1
                observed["cell_records"] += count
                observed_key_bytes += len(key.encode("utf-8"))
                pending[kind].append((key, count))
            elif kind in {"exact", "prefix"}:
                pending[kind].append((key, count))
                if kind == "exact":
                    self.summary_exact_rows += 1
                    observed["exact_rows"] += 1
                else:
                    self.summary_prefix_rows += 1
                    observed["prefix_rows"] += 1
                observed_key_bytes += len(key.encode("utf-8"))
            else:
                row = {name: value[name] for name in (
                    "gers_id", "primary_name", "alt_names", "brand_name",
                    "category_primary", "basic_category", "locality", "region",
                    "country", "lat", "lon", "confidence", "operating_status",
                    "partition_key", "partition_cell", "execution_group", "source_uri",
                    "source_row_group", "source_row_index",
                )}
                self.famous_rows.append(row)
                observed["famous_rows"] += 1
                self.peak_famous_candidates = max(
                    self.peak_famous_candidates, len(self.famous_rows)
                )
                if len(self.famous_rows) > PLAN_MAX_FAMOUS_CANDIDATES_IN_MEMORY:
                    raise ValueError(
                        "Places planner famous candidate memory cap was exceeded"
                    )
            if sum(map(len, pending.values())) >= PLAN_AGGREGATION_BATCH_ROWS:
                self._add(pending["cell"])
                self._add_named("exact_counts", pending["exact"])
                self._add_named("prefix_counts", pending["prefix"])
                pending = {"cell": [], "exact": [], "prefix": []}
        self._add(pending["cell"])
        self._add_named("exact_counts", pending["exact"])
        self._add_named("prefix_counts", pending["prefix"])
        observed["summary_rows"] = sum(
            observed[field]
            for field in ("cell_rows", "exact_rows", "prefix_rows", "famous_rows")
        )
        expected_statistics = {
            **{field: str(observed[field]) for field in (
                "cell_rows", "cell_records", "exact_rows", "prefix_rows",
                "famous_rows", "summary_rows",
            )},
            "key_bytes": str(observed_key_bytes),
        }
        if any(metadata.get(key) != value for key, value in expected_statistics.items()):
            raise ValueError("Places summary metadata does not reconcile to its rows")
        return {
            "cells": cells,
            "records": records,
            "exact_keys": observed["exact_rows"],
            "prefix_keys": observed["prefix_rows"],
            "famous_candidates": observed["famous_rows"],
            "summary_rows": observed["summary_rows"],
            "key_bytes": observed_key_bytes,
        }

    def _add(self, values: list[tuple[str, int]]) -> None:
        if not values:
            return
        if len(values) > PLAN_AGGREGATION_BATCH_ROWS:
            raise ValueError("Places planner count batch exceeded its memory contract")
        self.peak_pending_rows = max(self.peak_pending_rows, len(values))
        try:
            import pyarrow as pa

            keys, records = zip(*values, strict=True)
            batch = pa.table(
                {"cell": pa.array(keys, type=pa.string()),
                 "records": pa.array(records, type=pa.int64())}
            )
            self.connection.register("planner_count_batch", batch)
            try:
                self.connection.execute(
                    "INSERT INTO counts SELECT * FROM planner_count_batch "
                    "ON CONFLICT(cell) DO UPDATE SET "
                    "records = counts.records + excluded.records"
                )
            finally:
                self.connection.unregister("planner_count_batch")
            self.arrow_append_batches += 1
        except Exception as exc:
            raise ValueError(
                "Places planner count store exceeded its hard scratch cap"
            ) from exc
        self.observe_scratch()

    def _add_named(self, table: str, values: list[tuple[str, int]]) -> None:
        if not values:
            return
        if table not in {"exact_counts", "prefix_counts"}:
            raise AssertionError("invalid Places summary aggregation table")
        if len(values) > PLAN_AGGREGATION_BATCH_ROWS:
            raise ValueError("Places planner summary batch exceeded its memory contract")
        self.peak_pending_rows = max(self.peak_pending_rows, len(values))
        try:
            import pyarrow as pa

            keys, records = zip(*values, strict=True)
            batch = pa.table(
                {"value": pa.array(keys, type=pa.string()),
                 "records": pa.array(records, type=pa.int64())}
            )
            self.connection.register("planner_summary_batch", batch)
            try:
                self.connection.execute(
                    f"INSERT INTO {table} SELECT * FROM planner_summary_batch "
                    f"ON CONFLICT(value) DO UPDATE SET "
                    f"records = {table}.records + excluded.records"
                )
            finally:
                self.connection.unregister("planner_summary_batch")
            self.arrow_append_batches += 1
        except Exception as exc:
            raise ValueError("Places planner summary store exceeded its scratch cap") from exc
        self.observe_scratch()

    def finish(self) -> None:
        try:
            self.connection.execute("CHECKPOINT")
        except Exception as exc:
            raise ValueError(
                "Places planner count store exceeded its hard scratch cap"
            ) from exc
        self.observe_scratch()

    def ordered(self) -> Iterator[tuple[str, int]]:
        yield from self.query(self.ORDERED_QUERY)

    def query(
        self, statement: str, parameters: tuple[Any, ...] = ()
    ) -> Iterator[tuple[Any, ...]]:
        cursor = self.connection.execute(statement, parameters)
        self.observe_scratch()
        while rows := cursor.fetchmany(PLAN_AGGREGATION_BATCH_ROWS):
            self.observe_scratch()
            yield from rows

    def ordered_query_plan(self) -> tuple[str, ...]:
        return tuple(row[1] for row in self.connection.execute(
            f"EXPLAIN {self.ORDERED_QUERY}"
        ).fetchall())

    def ordered_query_uses_temporary_btree(self) -> bool:
        return False

    def group_totals(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        current_group: str | None = None
        current_records = 0
        for cell, records in self.ordered():
            group = cell[:EXECUTION_GROUP_LEVEL]
            if current_group is not None and group != current_group:
                if len(totals) + 2 > MAX_EXECUTION_GROUPS_IN_MEMORY:
                    raise ValueError(
                        "Places execution-group accumulator exceeded its cap"
                    )
                totals[current_group] = current_records
                current_records = 0
            current_group = group
            current_records += records
        if current_group is not None:
            if len(totals) + 1 > MAX_EXECUTION_GROUPS_IN_MEMORY:
                raise ValueError("Places execution-group accumulator exceeded its cap")
            totals[current_group] = current_records
        self.observe_scratch()
        return totals

    def totals_and_digest(self) -> tuple[int, int, str]:
        digest = hashlib.sha256()
        cells = records = 0
        for cell, count in self.ordered():
            digest.update(
                canonical_json_bytes({"cell": cell, "records": count}) + b"\n"
            )
            cells += 1
            records += count
        return cells, records, digest.hexdigest()

    def close(self) -> None:
        self.connection.close()
        self.path.unlink(missing_ok=True)
        Path(f"{self.path}.wal").unlink(missing_ok=True)
        for path in self.temp_directory.glob("*"):
            if path.is_file():
                path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            self.temp_directory.rmdir()


def _head_index_key_upper_bound(key: str) -> int:
    encoded = key.encode("utf-8")
    # pack_text(key) plus worst-case five-byte offset and three-byte entry
    # length (the Worker caps one entry at 128 KiB).
    return len(encode_varint(len(encoded))) + len(encoded) + 8


def _select_budgeted_keys(
    values: Iterator[tuple[str, int]],
    *,
    prefix: str,
    source: str,
    byte_budget: int,
    excluded: set[str],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used = 0
    for value, records in values:
        key = f"{prefix}:{value}"
        if key in excluded or len(key.encode("utf-8")) > READER_MAX_KEY_BYTES:
            continue
        bound = _head_index_key_upper_bound(key)
        if used + bound > byte_budget:
            continue
        selected.append(
            {
                "key": key,
                "source": source,
                "records": records,
                "index_bytes_upper_bound": bound,
            }
        )
        excluded.add(key)
        used += bound
        if len(excluded) >= READER_MAX_HEAD_KEYS:
            break
    return selected


def _build_head_admission(
    store: _CountStore,
    policy: dict[str, Any],
    *,
    output: Path,
) -> dict[str, Any]:
    minimum = policy["minimum_candidates"]
    famous_cap = policy["famous_cap"]
    famous_by_gers: dict[str, dict[str, Any]] = {}
    for row in store.famous_rows:
        previous = famous_by_gers.get(row["gers_id"])
        if previous is None or _summary_candidate_sort_key(
            row
        ) < _summary_candidate_sort_key(previous):
            famous_by_gers[row["gers_id"]] = row
    famous = sorted(
        famous_by_gers.values(), key=_summary_candidate_sort_key
    )[:famous_cap]
    famous_exact_rank: dict[str, int] = {}
    famous_pairs_rank: dict[str, int] = {}
    for rank, row in enumerate(famous):
        place = place_from_row(row, rank + 1)
        values = famous_name_brand_tokens(place)
        for token in values:
            famous_exact_rank.setdefault(token, rank)
        bounded = values[:FAMOUS_PAIR_TOKEN_LIMIT]
        for first in range(len(bounded)):
            for second in range(first + 1, len(bounded)):
                low, high = sorted((bounded[first], bounded[second]))
                famous_pairs_rank.setdefault(famous_pair_token_key(low, high), rank)

    selected_keys: set[str] = set()
    selected: list[dict[str, Any]] = []
    selected.extend(
        _select_budgeted_keys(
            iter(sorted(famous_exact_rank.items(), key=lambda item: (item[1], item[0].encode("utf-8")))),
            prefix="e",
            source="famous_exact",
            byte_budget=HEAD_INDEX_FAMILY_BUDGETS["famous_exact"],
            excluded=selected_keys,
        )
    )
    selected.extend(
        _select_budgeted_keys(
            iter(sorted(famous_pairs_rank.items(), key=lambda item: (item[1], item[0].encode("utf-8")))),
            prefix="",
            source="famous_pair",
            byte_budget=HEAD_INDEX_FAMILY_BUDGETS["famous_pair"],
            excluded=selected_keys,
        )
    )
    # Pair keys are already wire-formatted (`e2:low high`); remove the empty
    # family separator introduced by the common selector.
    for item in selected:
        if item["source"] == "famous_pair" and item["key"].startswith(":"):
            selected_keys.remove(item["key"])
            item["key"] = item["key"][1:]
            item["index_bytes_upper_bound"] = _head_index_key_upper_bound(item["key"])
            selected_keys.add(item["key"])

    exact_rows = store.query(
        "SELECT value, records FROM exact_counts WHERE records >= ? "
        "ORDER BY records DESC, value",
        (minimum,),
    )
    selected.extend(
        _select_budgeted_keys(
            exact_rows,
            prefix="e",
            source="count_exact",
            byte_budget=HEAD_INDEX_FAMILY_BUDGETS["count_exact"],
            excluded=selected_keys,
        )
    )
    prefix_rows = store.query(
        "SELECT value, records FROM prefix_counts WHERE records >= ? "
        "ORDER BY records DESC, value",
        (minimum,),
    )
    selected.extend(
        _select_budgeted_keys(
            prefix_rows,
            prefix="p",
            source="prefix",
            byte_budget=HEAD_INDEX_FAMILY_BUDGETS["prefix"],
            excluded=selected_keys,
        )
    )
    selected.sort(key=lambda item: item["key"].encode("utf-8"))
    if not selected or len(selected) > READER_MAX_HEAD_KEYS:
        raise ValueError("Places deterministic head admission produced an invalid key set")
    conservative_entries = [
        (item["key"], HEAD_MAX_ENTRIES_BYTES, READER_MAX_HEAD_ENTRY_BYTES)
        for item in selected
    ]
    encoded_index_upper_bound = len(encode_key_index(conservative_entries))
    if encoded_index_upper_bound > READER_MAX_HEAD_INDEX_BYTES:
        raise ValueError("Places admitted head index exceeds the Worker 1 MiB cap")
    document = {
        "schema": "overture-global-v2-places-head-admission-v1",
        "version": HEAD_ADMISSION_VERSION,
        "duplicate_gers_policy": HEAD_DUPLICATE_GERS_POLICY,
        "policy_sha256": digest_value(policy),
        "family_budgets": HEAD_INDEX_FAMILY_BUDGETS,
        "proof": {
            "key_count": len(selected),
            "maximum_key_count": READER_MAX_HEAD_KEYS,
            "encoded_index_upper_bound_bytes": encoded_index_upper_bound,
            "maximum_encoded_index_bytes": READER_MAX_HEAD_INDEX_BYTES,
            "entry_offset_model": "two-gigabyte-maximum-offset-every-key-v2",
        },
        "keys": selected,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_json_bytes(document) + b"\n")
    sha256, size = sha256_file(output)
    return {
        **document,
        "artifact": {
            "object_key": "head-admission.json",
            "bytes": size,
            "sha256": sha256,
        },
    }


def _validate_map_report(
    raw: Any,
    *,
    request: dict[str, Any],
    inventory: dict[str, Any],
    task: dict[str, Any],
    artifact_root: Path,
    artifact_listing: dict[str, tuple[int, str]] | None,
    count_store: _CountStore,
) -> dict[str, Any]:
    report = require_exact(
        raw,
        {
            "schema",
            "release",
            "family",
            "inventory_sha256",
            "source_schema_fingerprint_sha256",
            "execution",
            "source_ranges",
            "partitioning",
            "accounting",
            "summary",
            "fragments",
            "report_sha256",
        },
        "Places map report",
    )
    if report["schema"] != MAP_REPORT_SCHEMA:
        raise ValueError(f"Places map report schema must be {MAP_REPORT_SCHEMA}")
    report_digest = report["report_sha256"]
    require_sha256(report_digest, "Places map report_sha256")
    without_digest = {
        key: value for key, value in report.items() if key != "report_sha256"
    }
    if digest_value(without_digest) != report_digest:
        raise ValueError("Places map report digest differs from its contents")
    source = request["families"]["places"]["source"]
    if (
        report["release"] != request["overture_release"]
        or report["family"] != "places"
        or report["inventory_sha256"] != inventory["inventory_sha256"]
        or report["inventory_sha256"] != source["inventory_sha256"]
        or report["source_schema_fingerprint_sha256"]
        != inventory["schema_contract"]["fingerprint_sha256"]
        or report["source_schema_fingerprint_sha256"]
        != source["schema_fingerprint_sha256"]
    ):
        raise ValueError("Places map report source provenance differs from the request")
    execution = require_exact(
        report["execution"],
        {
            "task_index",
            "task_digest",
            "source_digest",
            "task_identity_is_serving_identity",
            "fragment_grouping",
            "fragment_grouping_is_final_shard_identity",
            "execution_group_level",
            "execution_group_count",
            "maximum_execution_groups",
            "row_group_rows_limit",
            "pack_bytes_target",
            "row_group_input_bytes_limit",
            "pack_bytes_limit",
            "task_pack_count_limit",
            "sort",
            "census",
            "workspace",
            "packs",
        },
        "Places map execution",
    )
    if (
        execution["task_index"] != task["index"]
        or execution["task_digest"] != task["task_digest"]
        or execution["source_digest"] != task["source_digest"]
        or execution["task_identity_is_serving_identity"] is not False
        or execution["fragment_grouping_is_final_shard_identity"] is not False
        or execution["fragment_grouping"] != "coarse-cross-group-parquet-packs-v2"
        or execution["execution_group_level"] != EXECUTION_GROUP_LEVEL
        or execution["maximum_execution_groups"] != 1 << (2 * EXECUTION_GROUP_LEVEL)
    ):
        raise ValueError("Places map execution identity/grouping differs from its task")
    for field in (
        "execution_group_count",
        "row_group_rows_limit",
        "pack_bytes_target",
        "row_group_input_bytes_limit",
        "pack_bytes_limit",
        "task_pack_count_limit",
    ):
        require_int(execution[field], f"Places map {field}", minimum=0)
    sort = require_exact(
        execution["sort"],
        {
            "kind", "engine", "engine_version", "maximum_memory_bytes",
            "maximum_scratch_bytes", "maximum_batch_rows", "peak_pending_rows",
            "insert_batches", "registered_arrow_batches", "python_sorted_runs",
            "python_heap_merge", "threads", "preserve_insertion_order",
            "json_payloads",
        },
        "Places map typed sort",
    )
    if (
        sort["kind"] != "duckdb-arrow-batch-external-sort-v1"
        or sort["engine"] != "duckdb"
        or sort["engine_version"] != REQUIRED_DUCKDB_VERSION
        or sort["maximum_memory_bytes"] != MAP_CENSUS_MEMORY_LIMIT_BYTES
        or sort["maximum_scratch_bytes"] != MAP_CENSUS_MAX_SCRATCH_BYTES
        or type(sort["maximum_batch_rows"]) is not int
        or sort["maximum_batch_rows"] < 1
        or type(sort["peak_pending_rows"]) is not int
        or not 0 <= sort["peak_pending_rows"] <= sort["maximum_batch_rows"]
        or type(sort["insert_batches"]) is not int
        or sort["insert_batches"] < 0
        or sort["registered_arrow_batches"] is not True
        or sort["python_sorted_runs"] is not False
        or sort["python_heap_merge"] is not False
        or sort["threads"] != 1
        or sort["preserve_insertion_order"] is not False
        or sort["json_payloads"] is not False
    ):
        raise ValueError("Places map did not use the typed DuckDB sort contract")
    census = require_exact(
        execution["census"],
        {
            "kind", "engine", "engine_version", "maximum_memory_bytes",
            "maximum_scratch_bytes", "scratch_bytes", "maximum_batch_rows",
            "peak_pending_count_rows", "peak_pending_famous_rows",
            "famous_candidate_cap", "famous_candidate_identity",
            "famous_deduplicate_before_cap", "famous_best_occurrence_order",
        },
        "Places map census evidence",
    )
    if (
        census["kind"] != "duckdb-typed-bounded-task-census-v1"
        or census["engine"] != "duckdb"
        or census["engine_version"] != MAP_REQUIRED_DUCKDB_VERSION
        or census["engine_version"] != REQUIRED_DUCKDB_VERSION
        or census["maximum_memory_bytes"] != MAP_CENSUS_MEMORY_LIMIT_BYTES
        or census["maximum_scratch_bytes"] != MAP_CENSUS_MAX_SCRATCH_BYTES
        or census["maximum_batch_rows"] != MAP_CENSUS_BATCH_ROWS
        or census["famous_candidate_cap"] != MAP_SUMMARY_FAMOUS_CAP
        or census["famous_candidate_identity"] != "gers_id"
        or census["famous_deduplicate_before_cap"] is not True
        or census["famous_best_occurrence_order"] != [
            "confidence-rank-descending",
            "partition-key-ascending",
            "gers-id-ascending",
            "source-uri-ascending",
            "source-row-group-ascending",
            "source-row-index-ascending",
        ]
    ):
        raise ValueError("Places map census contract is invalid")
    for field, maximum in (
        ("scratch_bytes", MAP_CENSUS_MAX_SCRATCH_BYTES),
        ("peak_pending_count_rows", MAP_CENSUS_BATCH_ROWS),
        ("peak_pending_famous_rows", MAP_CENSUS_BATCH_ROWS),
    ):
        observed = require_int(census[field], f"Places map census {field}")
        if observed > maximum:
            raise ValueError("Places map census observed bound exceeds its cap")
    workspace = require_exact(
        execution["workspace"],
        {
            "kind", "maximum_bytes", "peak_bytes", "peak_components",
            "component_peak_bytes", "observations", "includes",
        },
        "Places map workspace evidence",
    )
    component_names = {
        "census_database_bytes", "census_spill_bytes", "sort_database_bytes",
        "sort_spill_bytes", "staged_output_bytes",
    }
    peak_components = require_exact(
        workspace["peak_components"], component_names,
        "Places map peak workspace components",
    )
    component_peaks = require_exact(
        workspace["component_peak_bytes"], component_names,
        "Places map workspace component peaks",
    )
    if (
        workspace["kind"] != "combined-map-workspace-hard-cap-v1"
        or workspace["maximum_bytes"] != DEFAULT_MAX_MAP_WORKSPACE_BYTES
        or workspace["includes"] != [
            "census-database", "census-spill", "sort-database", "sort-spill",
            "staged-fragment-and-summary-output",
        ]
        or type(workspace["observations"]) is not int
        or workspace["observations"] < 1
        or any(type(value) is not int or value < 0 for value in peak_components.values())
        or any(type(value) is not int or value < 0 for value in component_peaks.values())
        or type(workspace["peak_bytes"]) is not int
        or workspace["peak_bytes"] != sum(peak_components.values())
        or workspace["peak_bytes"] > workspace["maximum_bytes"]
        or any(component_peaks[key] < peak_components[key] for key in component_names)
    ):
        raise ValueError("Places map workspace evidence is invalid")
    packs = require_exact(
        execution["packs"],
        {
            "kind", "writer", "maximum_batch_rows", "python_pack_rows_materialized",
            "ordinary_boundary", "target_output_bytes", "hard_row_group_input_bytes",
            "hard_output_bytes", "cell_boundary_flushes", "hot_cell_hard_splits",
            "output_cap_splits", "physical_pack_target_bytes", "row_group_boundary",
            "maximum_row_group_input_bytes", "packs_may_span_execution_groups",
            "ordered_queries", "sort_extent_queries",
        },
        "Places map pack evidence",
    )
    if (
        packs["kind"] != "task-wide-order-coarse-pack-v2"
        or packs["writer"] != "single-duckdb-order-group-aligned-row-groups-v2"
        or packs["maximum_batch_rows"] != MAP_OUTPUT_BATCH_ROWS
        or packs["python_pack_rows_materialized"] is not False
        or packs["ordinary_boundary"] != "execution-group-or-bounded-row-group"
        or packs["physical_pack_target_bytes"] != packs["target_output_bytes"]
        or packs["row_group_boundary"] != "execution-group"
        or packs["maximum_row_group_input_bytes"]
        != min(packs["hard_row_group_input_bytes"], 32_000_000)
        or packs["packs_may_span_execution_groups"] is not True
        or packs["ordered_queries"] != 1
        or packs["sort_extent_queries"] != 0
        or packs["target_output_bytes"] != execution["pack_bytes_target"]
        or packs["hard_row_group_input_bytes"]
        != execution["row_group_input_bytes_limit"]
        or packs["hard_output_bytes"] != execution["pack_bytes_limit"]
    ):
        raise ValueError("Places map pack contract is invalid")
    for field in ("cell_boundary_flushes", "hot_cell_hard_splits", "output_cap_splits"):
        require_int(packs[field], f"Places map packs {field}")
    if report["source_ranges"] != task["ranges"]:
        raise ValueError("Places map source ranges differ from the inventory task")
    partitioning = require_exact(
        report["partitioning"],
        {"scheme", "serving_leaf_minimum_level", "maximum_level"},
        "Places map partitioning",
    )
    requested_partition = request["families"]["places"]["partition"]
    if (
        partitioning["scheme"] != requested_partition["scheme"]
        or partitioning["serving_leaf_minimum_level"]
        != requested_partition["minimum_level"]
        or partitioning["maximum_level"] != requested_partition["maximum_level"]
    ):
        raise ValueError("Places map partitioning differs from the request")
    accounting = require_exact(
        report["accounting"],
        {
            "expected_input_records",
            "input_records",
            "retained_records",
            "rejected_records",
            "rejections_by_precedence",
        },
        "Places map accounting",
    )
    for field in (
        "expected_input_records",
        "input_records",
        "retained_records",
        "rejected_records",
    ):
        require_int(accounting[field], f"Places map {field}")
    if (
        accounting["expected_input_records"] != task["expected_input_records"]
        or accounting["input_records"] != task["expected_input_records"]
        or accounting["input_records"]
        != accounting["retained_records"] + accounting["rejected_records"]
    ):
        raise ValueError(
            "Places map accounting does not reconcile to its inventory task"
        )
    rejection_rows = accounting["rejections_by_precedence"]
    if not isinstance(rejection_rows, list) or rejection_rows != [
        {
            "reason": reason,
            "records": next(
                (
                    item.get("records")
                    for item in rejection_rows
                    if isinstance(item, dict) and item.get("reason") == reason
                ),
                None,
            ),
        }
        for reason in REJECTION_PRECEDENCE
    ]:
        raise ValueError(
            "Places rejection reasons differ from their exclusive precedence"
        )
    rejection_total = 0
    for item in rejection_rows:
        require_exact(item, {"reason", "records"}, "Places rejection row")
        rejection_total += require_int(item["records"], f"rejection {item['reason']}")
    if rejection_total != accounting["rejected_records"]:
        raise ValueError("Places rejection reason counts do not reconcile")
    summary = require_exact(
        report["summary"],
        {
            "object_key", "sha256", "bytes", "cells", "records",
            "exact_keys", "prefix_keys", "famous_candidates",
            "famous_candidate_cap", "key_bytes",
            "maximum_level", "format", "schema",
        },
        "Places summary artifact",
    )
    if (
        summary["maximum_level"] != requested_partition["maximum_level"]
        or summary["format"] != "parquet"
        or summary["schema"] != SUMMARY_ARTIFACT_SCHEMA
        or type(summary["famous_candidate_cap"]) is not int
        or summary["famous_candidate_cap"] != MAP_SUMMARY_FAMOUS_CAP
        or summary["famous_candidate_cap"]
        < request["families"]["places"]["global_head"]["famous_cap"]
    ):
        raise ValueError("Places summary contract differs from the request")
    summary_path = verify_artifact(
        artifact_root, summary["object_key"], summary["bytes"], summary["sha256"]
    )
    verify_listed_artifact(
        artifact_listing,
        summary["object_key"],
        summary["bytes"],
        summary["sha256"],
    )
    actual_summary = count_store.add_artifact(
        summary_path,
        maximum_level=requested_partition["maximum_level"],
        inventory_sha256=inventory["inventory_sha256"],
        task_digest=task["task_digest"],
    )
    if (
        summary["cells"] != actual_summary["cells"]
        or summary["records"] != actual_summary["records"]
        or summary["records"] != accounting["retained_records"]
        or any(
            summary[field] != actual_summary[field]
            for field in (
                "exact_keys", "prefix_keys", "famous_candidates", "key_bytes"
            )
        )
    ):
        raise ValueError("Places summary artifact does not reconcile to retained records")
    for field in ("exact_keys", "prefix_keys", "famous_candidates", "key_bytes"):
        require_int(summary[field], f"Places summary {field}")
    fragments = require_exact(
        report["fragments"],
        {"count", "records", "bytes", "manifest_sha256", "objects"},
        "Places fragment manifest",
    )
    require_sha256(fragments["manifest_sha256"], "Places fragment manifest_sha256")
    objects = fragments["objects"]
    if (
        not isinstance(objects, list)
        or digest_value(objects) != fragments["manifest_sha256"]
    ):
        raise ValueError("Places fragment manifest differs from its object list")
    if len(objects) > execution["task_pack_count_limit"]:
        raise ValueError("Places map report exceeded its declared fragment cap")
    normalized_fragments: list[dict[str, Any]] = []
    groups: set[str] = set()
    previous_pack_extent = None
    for pack_index, fragment in enumerate(objects):
        item = require_exact(
            fragment,
            {
                "object_key",
                "sha256",
                "bytes",
                "records",
                "row_group_count",
                "row_groups",
                "execution_groups",
                "minimum_sort_key",
                "maximum_sort_key",
                "footer_sha256",
                "footer_bytes",
            },
            "Places pack",
        )
        if (
            not isinstance(item["row_groups"], list)
            or not item["row_groups"]
            or item["row_group_count"] != len(item["row_groups"])
            or not isinstance(item["execution_groups"], list)
            or item["execution_groups"] != sorted(set(item["execution_groups"]))
        ):
            raise ValueError("Places pack row-group manifest is invalid")
        require_int(item["records"], "pack records", minimum=1)
        require_int(item["bytes"], "pack bytes", minimum=1)
        if item["bytes"] > execution["pack_bytes_limit"]:
            raise ValueError("Places pack exceeds its declared byte cap")
        require_sha256(item["sha256"], "Places fragment sha256")
        require_sha256(item["footer_sha256"], "Places pack footer_sha256")
        require_int(item["footer_bytes"], "Places pack footer bytes", minimum=1)
        if item["footer_bytes"] > item["bytes"]:
            raise ValueError("Places pack footer exceeds its object")
        expected_suffix = f"fragments/sha256/{item['sha256']}.parquet"
        if not item["object_key"].endswith(expected_suffix):
            raise ValueError(
                "Places pack key differs from its content identity"
            )
        previous_extent = None
        row_group_records = 0
        observed_pack_groups: set[str] = set()
        normalized_row_groups = []
        for index, row_group in enumerate(item["row_groups"]):
            group_item = require_exact(
                row_group,
                {
                    "index", "execution_group", "minimum_maximum_level_cell",
                    "maximum_maximum_level_cell", "records", "compressed_bytes",
                    "minimum_sort_key", "maximum_sort_key", "semantic_sha256",
                    "ownership_layout_sha256", "normalized_input_bytes",
                    "uncompressed_bytes",
                },
                "Places pack row group",
            )
            group = group_item["execution_group"]
            if (
                group_item["index"] != index
                or not isinstance(group, str)
                or len(group) != EXECUTION_GROUP_LEVEL
                or any(digit not in "0123" for digit in group)
                or not isinstance(group_item["minimum_maximum_level_cell"], str)
                or not isinstance(group_item["maximum_maximum_level_cell"], str)
                or not group_item["minimum_maximum_level_cell"].startswith(group)
                or not group_item["maximum_maximum_level_cell"].startswith(group)
                or group_item["minimum_maximum_level_cell"]
                > group_item["maximum_maximum_level_cell"]
                or not isinstance(group_item["minimum_sort_key"], list)
                or not isinstance(group_item["maximum_sort_key"], list)
                or len(group_item["minimum_sort_key"]) != 7
                or len(group_item["maximum_sort_key"]) != 7
                or group_item["minimum_sort_key"] > group_item["maximum_sort_key"]
                or (previous_extent is not None and group_item["minimum_sort_key"] < previous_extent)
            ):
                raise ValueError("Places pack row-group ownership/order is invalid")
            require_int(group_item["records"], "row-group records", minimum=1)
            require_int(group_item["compressed_bytes"], "row-group compressed bytes", minimum=1)
            require_int(group_item["uncompressed_bytes"], "row-group uncompressed bytes", minimum=1)
            require_int(group_item["normalized_input_bytes"], "row-group normalized bytes", minimum=1)
            require_sha256(group_item["semantic_sha256"], "row-group semantic_sha256")
            require_sha256(
                group_item["ownership_layout_sha256"],
                "row-group ownership_layout_sha256",
            )
            bound = {
                key: value
                for key, value in group_item.items()
                if key != "ownership_layout_sha256"
            }
            if group_item["ownership_layout_sha256"] != digest_value(
                {"pack_sha256": item["sha256"], **bound}
            ):
                raise ValueError("Places pack row-group ownership/layout binding differs")
            previous_extent = group_item["maximum_sort_key"]
            row_group_records += group_item["records"]
            observed_pack_groups.add(group)
            normalized_row_groups.append(group_item)
        if (
            row_group_records != item["records"]
            or observed_pack_groups != set(item["execution_groups"])
            or item["minimum_sort_key"] != item["row_groups"][0]["minimum_sort_key"]
            or item["maximum_sort_key"] != item["row_groups"][-1]["maximum_sort_key"]
        ):
            raise ValueError("Places pack rows/groups/extents do not reconcile")
        if previous_pack_extent is not None and item["minimum_sort_key"] < previous_pack_extent:
            raise ValueError("Places task-wide pack order regressed")
        previous_pack_extent = item["maximum_sort_key"]
        verify_listed_artifact(
            artifact_listing,
            item["object_key"],
            item["bytes"],
            item["sha256"],
        )
        if artifact_listing is None:
            verify_artifact(
                artifact_root, item["object_key"], item["bytes"], item["sha256"]
            )
        normalized_fragments.append({
            **item,
            "pack_index": pack_index,
            "map_index": task["index"],
            "map_task_digest": task["task_digest"],
        })
        groups.update(observed_pack_groups)
    if any(
        item["bytes"] < execution["pack_bytes_target"] // 2
        for item in objects[:-1]
    ):
        raise ValueError("non-tail Places physical pack is below its lower bound")
    if (
        fragments["count"] != len(objects)
        or fragments["records"] != sum(item["records"] for item in objects)
        or fragments["records"] != accounting["retained_records"]
        or fragments["bytes"] != sum(item["bytes"] for item in objects)
        or execution["execution_group_count"] != len(groups)
    ):
        raise ValueError("Places fragment records/bytes/groups do not reconcile")
    return {
        "report_sha256": report_digest,
        "task_index": task["index"],
        "task_digest": task["task_digest"],
        "input_records": accounting["input_records"],
        "retained_records": accounting["retained_records"],
        "rejected_records": accounting["rejected_records"],
        "rejections": {item["reason"]: item["records"] for item in rejection_rows},
        "summary_object_key": summary["object_key"],
        "summary_sha256": summary["sha256"],
        "fragments": normalized_fragments,
    }


def _load_predecessor_splits(
    request: dict[str, Any],
    predecessor_family_manifest: dict[str, Any] | None,
    predecessor_catalog: Path | None,
) -> list[str]:
    places = request["families"]["places"]
    if MAP_SUMMARY_FAMOUS_CAP < places["global_head"]["famous_cap"]:
        raise ValueError(
            "Places task famous candidate cap is below the requested global cap"
        )
    generation = require_int(
        places["partition"]["lineage_generation"],
        "Places lineage generation",
        minimum=1,
    )
    predecessor_identity = places["predecessor_family_manifest"]
    expected_sha256 = predecessor_identity["sha256"]
    if generation == 1:
        if any(value is not None for value in predecessor_identity.values()):
            raise ValueError(
                "generation-1 Places lineage must be an all-null bootstrap"
            )
        if predecessor_family_manifest is not None or predecessor_catalog is not None:
            raise ValueError(
                "generation-1 Places request must not supply a predecessor"
            )
        return []
    if any(value is None for value in predecessor_identity.values()):
        raise ValueError(
            "later Places lineage generation requires an exact predecessor"
        )
    if predecessor_family_manifest is None or predecessor_catalog is None:
        raise ValueError(
            "later Places lineage generation requires manifest and catalog predecessor"
        )
    manifest = global_build_manifest.validate_family_manifest(
        predecessor_family_manifest
    )
    manifest_bytes = global_build_manifest.canonical_json(manifest)
    if (
        len(manifest_bytes) != predecessor_identity["bytes"]
        or hashlib.sha256(manifest_bytes).hexdigest() != expected_sha256
        or manifest["family"] != "places"
    ):
        raise ValueError("Places predecessor manifest differs from its pinned identity")
    if (
        manifest["region"]["bbox"] != WORLD
        or manifest["region"]["bbox_scope"] != "exact"
    ):
        raise ValueError("Places predecessor must be exact global coverage")
    catalogs = [
        artifact
        for artifact in manifest["artifacts"]
        if artifact["object_key"].endswith("/catalog.pcat")
        or artifact["object_key"] == "catalog.pcat"
    ]
    if len(catalogs) != 1:
        raise ValueError(
            "Places predecessor manifest must identify exactly one catalog.pcat"
        )
    actual_sha256, actual_bytes = sha256_file(predecessor_catalog)
    if (actual_bytes, actual_sha256) != (catalogs[0]["bytes"], catalogs[0]["sha256"]):
        raise ValueError(
            "Places predecessor catalog differs from its manifest identity"
        )
    payload = places_builder._read_catalog_payload(predecessor_catalog)
    partition = places["partition"]
    previous_partition = payload.get("partition")
    previous_maximum_level = (
        previous_partition.get("maximum_level")
        if isinstance(previous_partition, dict)
        else None
    )
    previous_row_cap = (
        previous_partition.get("split_row_cap")
        if isinstance(previous_partition, dict)
        else None
    )
    previous_generation = (
        previous_partition.get("lineage_generation")
        if isinstance(previous_partition, dict)
        else None
    )
    if (
        payload.get("schema_version") != 2
        or payload.get("coverage") != WORLD
        or not isinstance(previous_partition, dict)
        or previous_partition.get("scheme") != partition["scheme"]
        or previous_partition.get("minimum_level") != partition["minimum_level"]
        or type(previous_generation) is not int
        or previous_generation != generation - 1
        or type(previous_maximum_level) is not int
        or not partition["minimum_level"]
        <= previous_maximum_level
        <= partition["maximum_level"]
        or type(previous_row_cap) is not int
        or previous_row_cap < 1
    ):
        raise ValueError(
            "Places predecessor catalog partition lineage/contract is incompatible"
        )
    splits = places_builder.previous_split_cells(
        predecessor_catalog,
        minimum_level=partition["minimum_level"],
        maximum_level=partition["maximum_level"],
        coverage=WORLD,
    )
    return splits


def _assign_reduce_jobs(
    *,
    groups: dict[str, dict[str, Any]],
    reduce_job_limit: int,
    request_digest: str,
    inventory_sha256: str,
    completion_set_sha256: str,
) -> list[dict[str, Any]]:
    if not groups:
        raise ValueError("Places retained input has no occupied execution groups")
    job_count = min(reduce_job_limit, len(groups))
    buckets = [
        {"index": index, "records": 0, "groups": []} for index in range(job_count)
    ]
    for group, info in sorted(
        groups.items(), key=lambda item: (-item[1]["records"], item[0])
    ):
        target = min(buckets, key=lambda bucket: (bucket["records"], bucket["index"]))
        target["groups"].append(group)
        target["records"] += info["records"]
    jobs = []
    for bucket in buckets:
        execution_groups = sorted(bucket["groups"])
        leaves = sorted(
            (leaf for group in execution_groups for leaf in groups[group]["leaves"]),
            key=lambda leaf: leaf["cell"],
        )
        selections_by_pack: dict[str, dict[str, Any]] = {}
        for group in execution_groups:
            for selection in groups[group]["fragments"]:
                key = selection["object_key"]
                selected = selections_by_pack.get(key)
                if selected is None:
                    selected = {
                        key: value
                        for key, value in selection.items()
                        if key not in {
                            "selected_row_groups", "selected_execution_groups", "records",
                            "selected_compressed_bytes", "selected_uncompressed_bytes",
                        }
                    }
                    selected.update(
                        {
                            "selected_row_groups": [],
                            "selected_execution_groups": [],
                            "records": 0,
                            "selected_compressed_bytes": 0,
                            "selected_uncompressed_bytes": 0,
                        }
                    )
                    selections_by_pack[key] = selected
                selected["selected_row_groups"].extend(selection["selected_row_groups"])
                selected["selected_execution_groups"].append(group)
                selected["records"] += selection["records"]
                selected["selected_compressed_bytes"] += selection["selected_compressed_bytes"]
                selected["selected_uncompressed_bytes"] += selection["selected_uncompressed_bytes"]
        fragments = []
        for selected in selections_by_pack.values():
            selected["selected_row_groups"] = sorted(selected["selected_row_groups"])
            selected["selected_execution_groups"] = sorted(set(selected["selected_execution_groups"]))
            selected["fetch_mode"] = (
                "whole" if len(selected["selected_row_groups"]) == selected["row_group_count"] else "selective"
            )
            selected["planned_fetch_bytes"] = (
                selected["bytes"]
                if selected["fetch_mode"] == "whole"
                else min(
                    selected["bytes"],
                    selected["selected_compressed_bytes"]
                    + selected["footer_bytes"]
                    + SELECTIVE_FETCH_RANGE_ALLOWANCE_BYTES,
                )
            )
            selected["maximum_materialized_bytes"] = (
                selected["bytes"]
                if selected["fetch_mode"] == "whole"
                else max(
                    selected["bytes"],
                    selected["selected_uncompressed_bytes"]
                    + selected["footer_bytes"]
                    + SELECTIVE_FETCH_RANGE_ALLOWANCE_BYTES,
                )
            )
            fragments.append(selected)
        fragments.sort(key=lambda item: (item["map_index"], item["pack_index"], item["sha256"]))
        input_bytes = sum(item["planned_fetch_bytes"] for item in fragments)
        selected_uncompressed_bytes = sum(
            item["selected_uncompressed_bytes"] for item in fragments
        )
        maximum_materialized_bytes = max(
            (item["maximum_materialized_bytes"] for item in fragments), default=0
        )
        if len(fragments) > MAX_INPUT_FRAGMENTS_PER_REDUCE_JOB:
            raise ValueError("Places reduce job exceeds its fragment-count cap")
        if input_bytes > MAX_INPUT_BYTES_PER_REDUCE_JOB:
            raise ValueError("Places reduce job exceeds its input-byte cap")
        if selected_uncompressed_bytes > MAX_SELECTED_UNCOMPRESSED_BYTES_PER_REDUCE_JOB:
            raise ValueError("Places reduce job exceeds its uncompressed decode-byte cap")
        if maximum_materialized_bytes > MAX_SIMULTANEOUS_MATERIALIZED_BYTES:
            raise ValueError("Places reduce job exceeds its simultaneous materialization cap")
        if bucket["records"] > MAX_RETAINED_ROWS_PER_REDUCE_JOB:
            raise ValueError("Places reduce job exceeds its retained-row cap")
        identity = {
            "kind": "places-reduce-job-v1",
            "request_sha256": request_digest,
            "inventory_sha256": inventory_sha256,
            "map_completion_set_sha256": completion_set_sha256,
            "index": bucket["index"],
            "execution_groups": execution_groups,
            "leaves": [{"cell": leaf["cell"], "rows": leaf["rows"]} for leaf in leaves],
            "fragment_selections": [
                {"sha256": item["sha256"], "row_groups": item["selected_row_groups"]}
                for item in fragments
            ],
        }
        jobs.append(
            {
                "index": bucket["index"],
                "job_digest": digest_value(identity),
                "execution_identity_is_serving_identity": False,
                "execution_groups": execution_groups,
                "expected_records": bucket["records"],
                "input_fragments": fragments,
                "input_fragment_count": len(fragments),
                "input_bytes": input_bytes,
                "planned_fetch_bytes": sum(item["planned_fetch_bytes"] for item in fragments),
                "selected_compressed_bytes": sum(item["selected_compressed_bytes"] for item in fragments),
                "selected_uncompressed_bytes": selected_uncompressed_bytes,
                "maximum_materialized_bytes": maximum_materialized_bytes,
                "whole_pack_fetches": sum(item["fetch_mode"] == "whole" for item in fragments),
                "selective_pack_fetches": sum(item["fetch_mode"] == "selective" for item in fragments),
                "leaves": [
                    {"cell": leaf["cell"], "rows": leaf["rows"]} for leaf in leaves
                ],
            }
        )
    return jobs


def build_places_plan(
    request_value: Any,
    inventory_value: Any,
    map_reports: list[Any],
    *,
    artifact_root: Path,
    scratch_dir: Path,
    artifact_listing: Any | None = None,
    predecessor_family_manifest: dict[str, Any] | None = None,
    predecessor_catalog: Path | None = None,
    head_admission_output: Path | None = None,
) -> dict[str, Any]:
    request = global_v2_build_request.validate_request(request_value)
    inventory = validate_inventory(inventory_value)
    places = request["families"]["places"]
    if (
        inventory["release"] != request["overture_release"]
        or inventory["inventory_sha256"] != places["source"]["inventory_sha256"]
        or inventory["schema_contract"]["fingerprint_sha256"]
        != places["source"]["schema_fingerprint_sha256"]
    ):
        raise ValueError("Places inventory differs from the immutable build request")
    tasks = inventory["map_plan"]["tasks"]
    if len(tasks) > request["execution"]["source_task_limit"]:
        raise ValueError("Places inventory map plan exceeds the request task limit")
    predecessor_splits = _load_predecessor_splits(
        request, predecessor_family_manifest, predecessor_catalog
    )
    normalized_listing = normalize_artifact_listing(artifact_listing)
    count_store = _CountStore(scratch_dir)
    try:
        expected_tasks = {task["index"]: task for task in tasks}
        observed: dict[int, dict[str, Any]] = {}
        report_digests: set[str] = set()
        fragment_keys: set[str] = set()
        fragment_digests: set[str] = set()
        summary_keys: set[str] = set()
        summary_digests: set[str] = set()
        for raw in map_reports:
            if not isinstance(raw, dict):
                raise ValueError("Places map completion must be an object")
            execution = raw.get("execution")
            index = execution.get("task_index") if isinstance(execution, dict) else None
            require_int(index, "Places map task index")
            if index not in expected_tasks:
                raise ValueError(f"unexpected Places map task identity: {index}")
            if index in observed:
                raise ValueError(
                    f"duplicate/replayed Places map task identity: {index}"
                )
            normalized = _validate_map_report(
                raw,
                request=request,
                inventory=inventory,
                task=expected_tasks[index],
                artifact_root=artifact_root,
                artifact_listing=normalized_listing,
                count_store=count_store,
            )
            if normalized["report_sha256"] in report_digests:
                raise ValueError("replayed Places map report digest")
            if (
                normalized["summary_object_key"] in summary_keys
                or normalized["summary_sha256"] in summary_digests
            ):
                raise ValueError("replayed Places summary artifact identity")
            report_digests.add(normalized["report_sha256"])
            summary_keys.add(normalized["summary_object_key"])
            summary_digests.add(normalized["summary_sha256"])
            for fragment in normalized["fragments"]:
                if (
                    fragment["object_key"] in fragment_keys
                    or fragment["sha256"] in fragment_digests
                ):
                    raise ValueError("duplicate/replayed Places fragment identity")
                fragment_keys.add(fragment["object_key"])
                fragment_digests.add(fragment["sha256"])
            observed[index] = normalized
        missing = sorted(set(expected_tasks) - set(observed))
        if missing:
            raise ValueError(f"missing Places map task completions: {missing}")
        if normalized_listing is not None:
            expected_artifact_keys = fragment_keys | summary_keys
            if set(normalized_listing) != expected_artifact_keys:
                missing_objects = sorted(
                    expected_artifact_keys - set(normalized_listing)
                )
                unexpected_objects = sorted(
                    set(normalized_listing) - expected_artifact_keys
                )
                raise ValueError(
                    "Places intermediate listing differs from the completion set: "
                    f"missing={missing_objects}, unexpected={unexpected_objects}"
                )
        completions = [observed[index] for index in sorted(observed)]
        count_store.finish()
        total_input = sum(item["input_records"] for item in completions)
        total_retained = sum(item["retained_records"] for item in completions)
        total_rejected = sum(item["rejected_records"] for item in completions)
        if (
            total_input != inventory["totals"]["records"]
            or total_input != total_retained + total_rejected
        ):
            raise ValueError(
                "Places global map accounting does not reconcile to inventory"
            )
        rejections = {
            reason: sum(item["rejections"][reason] for item in completions)
            for reason in REJECTION_PRECEDENCE
        }
        maximum_cells, counted_records, count_digest = count_store.totals_and_digest()
        if counted_records != total_retained:
            raise ValueError("Places aggregated maximum-level counts do not reconcile")
        partition = places["partition"]
        try:
            cells, split_cells = plan_partition_cells(
                count_store.ordered(),
                minimum_level=partition["minimum_level"],
                maximum_level=partition["maximum_level"],
                row_cap=partition["split_row_cap"],
                sticky_splits=predecessor_splits,
            )
        except ValueError as exc:
            raise ValueError(
                "Places density planning failed with immutable evidence: "
                f"maximum_level={partition['maximum_level']}, "
                f"split_row_cap={partition['split_row_cap']}, "
                f"retained_records={total_retained}, "
                f"maximum_level_cells={maximum_cells}, "
                f"count_stream_sha256={count_digest}: {exc}"
            ) from exc
        if sum(cell.rows for cell in cells) != total_retained:
            raise ValueError(
                "Places stable leaf rows do not reconcile to retained rows"
            )
        group_totals = count_store.group_totals()
        fragments_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        fragment_records_by_group: Counter[str] = Counter()
        for completion in completions:
            for fragment in completion["fragments"]:
                row_groups_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for row_group in fragment["row_groups"]:
                    row_groups_by_group[row_group["execution_group"]].append(row_group)
                for group, selected_groups in row_groups_by_group.items():
                    records = sum(item["records"] for item in selected_groups)
                    compressed_bytes = sum(
                        item["compressed_bytes"] for item in selected_groups
                    )
                    uncompressed_bytes = sum(
                        item["uncompressed_bytes"] for item in selected_groups
                    )
                    fragments_by_group[group].append(
                        {
                            **fragment,
                            "selected_row_groups": [item["index"] for item in selected_groups],
                            "selected_execution_groups": [group],
                            "records": records,
                            "selected_compressed_bytes": compressed_bytes,
                            "selected_uncompressed_bytes": uncompressed_bytes,
                        }
                    )
                    fragment_records_by_group[group] += records
        if dict(sorted(fragment_records_by_group.items())) != group_totals:
            raise ValueError(
                "Places fragment group totals differ from exact cell counts"
            )
        leaves_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for cell in cells:
            group = cell.cell[:EXECUTION_GROUP_LEVEL]
            leaves_by_group[group].append({"cell": cell.cell, "rows": cell.rows})
        if set(leaves_by_group) != set(group_totals):
            raise ValueError(
                "Places stable leaves differ from occupied execution groups"
            )
        groups: dict[str, dict[str, Any]] = {}
        for group in sorted(group_totals):
            fragments = fragments_by_group[group]
            if len(fragments) > MAX_RAW_FRAGMENTS_PER_EXECUTION_GROUP:
                raise ValueError(
                    f"Places execution group {group} exceeds raw fragment fan-in cap"
                )
            groups[group] = {
                "records": group_totals[group],
                "leaves": leaves_by_group[group],
                "fragments": fragments,
            }
        completion_set_sha256 = digest_value(
            [item["report_sha256"] for item in completions]
        )
        request_digest = request_sha256(request)
        head_admission = _build_head_admission(
            count_store,
            places["global_head"],
            output=head_admission_output or scratch_dir / "head-admission.json",
        )
        reduce_jobs = _assign_reduce_jobs(
            groups=groups,
            reduce_job_limit=request["execution"]["reduce_job_limit"],
            request_digest=request_digest,
            inventory_sha256=inventory["inventory_sha256"],
            completion_set_sha256=completion_set_sha256,
        )
        pack_job_fanout = Counter(
            item["object_key"] for job in reduce_jobs for item in job["input_fragments"]
        )
        job_by_group = {
            group: job["index"]
            for job in reduce_jobs
            for group in job["execution_groups"]
        }
        leaves = [
            {
                "cell": cell.cell,
                "rows": cell.rows,
                "execution_group": cell.cell[:EXECUTION_GROUP_LEVEL],
                "reduce_job_index": job_by_group[cell.cell[:EXECUTION_GROUP_LEVEL]],
            }
            for cell in cells
        ]
        without_digest = {
            "schema": PLAN_SCHEMA,
            "version": PLAN_VERSION,
            "request": {
                "sha256": request_digest,
                "overture_release": request["overture_release"],
                "geocoder_build": request["geocoder_build"],
                "slice_version": request["slice_version"],
                "producer_commit": request["producer_commit"],
            },
            "inventory": {
                "sha256": inventory["inventory_sha256"],
                "schema_fingerprint_sha256": inventory["schema_contract"][
                    "fingerprint_sha256"
                ],
                "map_tasks": len(tasks),
                "input_records": inventory["totals"]["records"],
            },
            "required_runtime": dict(REQUIRED_RUNTIME),
            "head_admission": head_admission,
            "map_fan_in": {
                "completion_count": len(completions),
                "completion_set_sha256": completion_set_sha256,
                "maximum_level_cells": maximum_cells,
                "maximum_level_count_stream_sha256": count_digest,
                "input_records": total_input,
                "retained_records": total_retained,
                "rejected_records": total_rejected,
                "rejections_by_precedence": [
                    {"reason": reason, "records": rejections[reason]}
                    for reason in REJECTION_PRECEDENCE
                ],
                "fragment_count": len(fragment_keys),
                "fragment_bytes": sum(
                    fragment["bytes"]
                    for completion in completions
                    for fragment in completion["fragments"]
                ),
                "pack_reducer_fanout": {
                    "physical_packs": len(fragment_keys),
                    "pack_job_references": sum(pack_job_fanout.values()),
                    "maximum_reducers_per_pack": max(pack_job_fanout.values(), default=0),
                    "whole_pack_fetches": sum(job["whole_pack_fetches"] for job in reduce_jobs),
                    "selective_pack_fetches": sum(job["selective_pack_fetches"] for job in reduce_jobs),
                    "selected_compressed_bytes": sum(job["selected_compressed_bytes"] for job in reduce_jobs),
                    "selected_uncompressed_bytes": sum(job["selected_uncompressed_bytes"] for job in reduce_jobs),
                    "planned_fetch_bytes": sum(job["planned_fetch_bytes"] for job in reduce_jobs),
                    "maximum_materialized_bytes": max(
                        (job["maximum_materialized_bytes"] for job in reduce_jobs),
                        default=0,
                    ),
                },
                # Immutable plan identity must not include observed database,
                # scratch, or batch peaks. Those measurements are run-local
                # diagnostics even when the logical plan is byte-identical.
                "count_aggregation": count_store.deterministic_contract(),
            },
            "partition": {
                "scheme": PARTITION_SCHEME,
                "minimum_level": partition["minimum_level"],
                "maximum_level": partition["maximum_level"],
                "split_row_cap": partition["split_row_cap"],
                "sticky_splits": True,
                "lineage_generation": partition["lineage_generation"],
                "predecessor_family_manifest_sha256": places[
                    "predecessor_family_manifest_sha256"
                ],
                "predecessor_family_manifest": places["predecessor_family_manifest"],
                "split_cells": split_cells,
            },
            "limits": {
                "max_raw_fragments_per_execution_group": MAX_RAW_FRAGMENTS_PER_EXECUTION_GROUP,
                "max_input_fragments_per_reduce_job": MAX_INPUT_FRAGMENTS_PER_REDUCE_JOB,
                "max_input_bytes_per_reduce_job": MAX_INPUT_BYTES_PER_REDUCE_JOB,
                "max_selected_uncompressed_bytes_per_reduce_job": (
                    MAX_SELECTED_UNCOMPRESSED_BYTES_PER_REDUCE_JOB
                ),
                "max_simultaneous_materialized_bytes": (
                    MAX_SIMULTANEOUS_MATERIALIZED_BYTES
                ),
                "selective_fetch_range_allowance_bytes": (
                    SELECTIVE_FETCH_RANGE_ALLOWANCE_BYTES
                ),
                "max_retained_rows_per_reduce_job": MAX_RETAINED_ROWS_PER_REDUCE_JOB,
                "plan_max_scratch_bytes": PLAN_MAX_SCRATCH_BYTES,
                "plan_duckdb_memory_limit_bytes": PLAN_DUCKDB_MEMORY_LIMIT_BYTES,
                "plan_aggregation_batch_rows": PLAN_AGGREGATION_BATCH_ROWS,
                "reduce_max_open_fragment_files": REDUCE_MAX_OPEN_FRAGMENT_FILES,
                "reduce_max_buffer_rows": REDUCE_MAX_BUFFER_ROWS,
                "reduce_duckdb_memory_limit_bytes": REDUCE_DUCKDB_MEMORY_LIMIT_BYTES,
                "reduce_max_active_leaf_partitions": REDUCE_MAX_ACTIVE_LEAF_PARTITIONS,
                "head_candidate_write_batch_rows": HEAD_CANDIDATE_WRITE_BATCH_ROWS,
                "head_candidate_write_batch_bytes": HEAD_CANDIDATE_WRITE_BATCH_BYTES,
                "head_candidate_max_row_bytes": HEAD_CANDIDATE_MAX_ROW_BYTES,
                "head_candidate_max_projection_bytes_in_memory": (
                    HEAD_CANDIDATE_MAX_PROJECTION_BYTES_IN_MEMORY
                ),
                "reduce_max_leaf_input_bytes": REDUCE_MAX_LEAF_INPUT_BYTES,
                "reduce_max_scratch_bytes": REDUCE_MAX_SCRATCH_BYTES,
                "reduce_max_workspace_bytes": REDUCE_MAX_WORKSPACE_BYTES,
                "reduce_max_leaf_rows_in_memory": REDUCE_MAX_LEAF_ROWS_IN_MEMORY,
                "reduce_max_leaf_token_occurrences": REDUCE_MAX_LEAF_TOKEN_OCCURRENCES,
                "reduce_max_leaf_projection_bytes": REDUCE_MAX_LEAF_PROJECTION_BYTES,
            },
            "leaves": leaves,
            "reduce_jobs": reduce_jobs,
            "totals": {
                "retained_records": total_retained,
                "leaves": len(leaves),
                "split_cells": len(split_cells),
                "execution_groups": len(groups),
                "reduce_jobs": len(reduce_jobs),
                "input_fragments": len(fragment_keys),
            },
        }
        return {**without_digest, "plan_sha256": digest_value(without_digest)}
    finally:
        count_store.close()


def validate_places_plan(value: Any) -> dict[str, Any]:
    plan = require_exact(
        value,
        {
            "schema",
            "version",
            "request",
            "inventory",
            "required_runtime",
            "head_admission",
            "map_fan_in",
            "partition",
            "limits",
            "leaves",
            "reduce_jobs",
            "totals",
            "plan_sha256",
        },
        "Places executor plan",
    )
    if plan["schema"] != PLAN_SCHEMA or plan["version"] != PLAN_VERSION:
        raise ValueError(
            f"Places executor plan schema/version must be {PLAN_SCHEMA}/{PLAN_VERSION}"
        )
    if plan["required_runtime"] != REQUIRED_RUNTIME:
        raise ValueError(
            "Places executor plan runtime differs from the pinned contract"
        )
    if plan["limits"] != {
        "max_raw_fragments_per_execution_group": MAX_RAW_FRAGMENTS_PER_EXECUTION_GROUP,
        "max_input_fragments_per_reduce_job": MAX_INPUT_FRAGMENTS_PER_REDUCE_JOB,
        "max_input_bytes_per_reduce_job": MAX_INPUT_BYTES_PER_REDUCE_JOB,
        "max_selected_uncompressed_bytes_per_reduce_job": (
            MAX_SELECTED_UNCOMPRESSED_BYTES_PER_REDUCE_JOB
        ),
        "max_simultaneous_materialized_bytes": MAX_SIMULTANEOUS_MATERIALIZED_BYTES,
        "selective_fetch_range_allowance_bytes": SELECTIVE_FETCH_RANGE_ALLOWANCE_BYTES,
        "max_retained_rows_per_reduce_job": MAX_RETAINED_ROWS_PER_REDUCE_JOB,
        "plan_max_scratch_bytes": PLAN_MAX_SCRATCH_BYTES,
        "plan_duckdb_memory_limit_bytes": PLAN_DUCKDB_MEMORY_LIMIT_BYTES,
        "plan_aggregation_batch_rows": PLAN_AGGREGATION_BATCH_ROWS,
        "reduce_max_open_fragment_files": REDUCE_MAX_OPEN_FRAGMENT_FILES,
        "reduce_max_buffer_rows": REDUCE_MAX_BUFFER_ROWS,
        "reduce_duckdb_memory_limit_bytes": REDUCE_DUCKDB_MEMORY_LIMIT_BYTES,
        "reduce_max_active_leaf_partitions": REDUCE_MAX_ACTIVE_LEAF_PARTITIONS,
        "head_candidate_write_batch_rows": HEAD_CANDIDATE_WRITE_BATCH_ROWS,
        "head_candidate_write_batch_bytes": HEAD_CANDIDATE_WRITE_BATCH_BYTES,
        "head_candidate_max_row_bytes": HEAD_CANDIDATE_MAX_ROW_BYTES,
        "head_candidate_max_projection_bytes_in_memory": (
            HEAD_CANDIDATE_MAX_PROJECTION_BYTES_IN_MEMORY
        ),
        "reduce_max_leaf_input_bytes": REDUCE_MAX_LEAF_INPUT_BYTES,
        "reduce_max_scratch_bytes": REDUCE_MAX_SCRATCH_BYTES,
        "reduce_max_workspace_bytes": REDUCE_MAX_WORKSPACE_BYTES,
        "reduce_max_leaf_rows_in_memory": REDUCE_MAX_LEAF_ROWS_IN_MEMORY,
        "reduce_max_leaf_token_occurrences": REDUCE_MAX_LEAF_TOKEN_OCCURRENCES,
        "reduce_max_leaf_projection_bytes": REDUCE_MAX_LEAF_PROJECTION_BYTES,
    }:
        raise ValueError("Places executor plan limits differ from the pinned contract")
    require_sha256(plan["plan_sha256"], "Places plan_sha256")
    without_digest = {key: item for key, item in plan.items() if key != "plan_sha256"}
    if digest_value(without_digest) != plan["plan_sha256"]:
        raise ValueError("Places executor plan digest differs from its contents")
    admission = require_exact(
        plan["head_admission"],
        {
            "schema", "version", "duplicate_gers_policy", "policy_sha256",
            "family_budgets", "proof", "keys", "artifact",
        },
        "Places head admission",
    )
    if (
        admission["schema"] != "overture-global-v2-places-head-admission-v1"
        or admission["version"] != HEAD_ADMISSION_VERSION
        or admission["duplicate_gers_policy"] != HEAD_DUPLICATE_GERS_POLICY
        or admission["family_budgets"] != HEAD_INDEX_FAMILY_BUDGETS
    ):
        raise ValueError("Places head admission policy/version is invalid")
    require_sha256(admission["policy_sha256"], "Places head policy sha256")
    admitted_keys = admission["keys"]
    if not isinstance(admitted_keys, list) or not admitted_keys:
        raise ValueError("Places head admission has no keys")
    wire_keys: list[str] = []
    for raw in admitted_keys:
        item = require_exact(
            raw,
            {"key", "source", "records", "index_bytes_upper_bound"},
            "Places admitted head key",
        )
        key = item["key"]
        if (
            not isinstance(key, str)
            or not key.startswith(("e:", "e2:", "p:"))
            or len(key.encode("utf-8")) > READER_MAX_KEY_BYTES
            or item["source"] not in HEAD_INDEX_FAMILY_BUDGETS
        ):
            raise ValueError("Places admitted head key is invalid")
        require_int(item["records"], "Places admitted key records")
        if item["index_bytes_upper_bound"] != _head_index_key_upper_bound(key):
            raise ValueError("Places admitted key byte proof is invalid")
        wire_keys.append(key)
    if wire_keys != sorted(set(wire_keys), key=lambda value: value.encode("utf-8")):
        raise ValueError("Places admitted head keys are duplicate or unordered")
    proof = require_exact(
        admission["proof"],
        {
            "key_count", "maximum_key_count", "encoded_index_upper_bound_bytes",
            "maximum_encoded_index_bytes", "entry_offset_model",
        },
        "Places head admission proof",
    )
    independently_encoded_index_bytes = len(
        encode_key_index(
            [
                (
                    key,
                    HEAD_MAX_ENTRIES_BYTES,
                    READER_MAX_HEAD_ENTRY_BYTES,
                )
                for key in wire_keys
            ]
        )
    )
    if (
        proof["key_count"] != len(wire_keys)
        or proof["maximum_key_count"] != READER_MAX_HEAD_KEYS
        or proof["maximum_encoded_index_bytes"] != READER_MAX_HEAD_INDEX_BYTES
        or proof["encoded_index_upper_bound_bytes"]
        != independently_encoded_index_bytes
        or independently_encoded_index_bytes > READER_MAX_HEAD_INDEX_BYTES
        or proof["entry_offset_model"]
        != "two-gigabyte-maximum-offset-every-key-v2"
    ):
        raise ValueError("Places head admission cap proof is invalid")
    artifact = require_exact(
        admission["artifact"], {"object_key", "bytes", "sha256"},
        "Places head admission artifact",
    )
    if artifact["object_key"] != "head-admission.json":
        raise ValueError("Places head admission artifact key is invalid")
    require_int(artifact["bytes"], "Places head admission bytes", minimum=1)
    require_sha256(artifact["sha256"], "Places head admission sha256")
    admission_document = {key: value for key, value in admission.items() if key != "artifact"}
    encoded_admission = canonical_json_bytes(admission_document) + b"\n"
    if (
        len(encoded_admission) != artifact["bytes"]
        or hashlib.sha256(encoded_admission).hexdigest() != artifact["sha256"]
    ):
        raise ValueError("Places head admission artifact identity is invalid")
    map_fan_in = require_exact(
        plan["map_fan_in"],
        {
            "completion_count",
            "completion_set_sha256",
            "maximum_level_cells",
            "maximum_level_count_stream_sha256",
            "input_records",
            "retained_records",
            "rejected_records",
            "rejections_by_precedence",
            "fragment_count",
            "fragment_bytes",
            "pack_reducer_fanout",
            "count_aggregation",
        },
        "Places map fan-in",
    )
    fanout = require_exact(
        map_fan_in["pack_reducer_fanout"],
        {
            "physical_packs", "pack_job_references", "maximum_reducers_per_pack",
            "whole_pack_fetches", "selective_pack_fetches",
            "selected_compressed_bytes", "planned_fetch_bytes",
            "selected_uncompressed_bytes", "maximum_materialized_bytes",
        },
        "Places pack/reducer fanout",
    )
    for field in fanout:
        require_int(fanout[field], f"Places pack/reducer fanout {field}")
    count_aggregation = require_exact(
        map_fan_in["count_aggregation"],
        {
            "kind",
            "engine",
            "engine_version",
            "maximum_memory_bytes",
            "maximum_scratch_bytes",
            "maximum_batch_rows",
            "registered_arrow_batches",
            "maximum_famous_candidates_in_memory",
            "group_aggregation",
            "maximum_execution_groups_in_memory",
            "ordered_scan",
        },
        "Places count aggregation evidence",
    )
    if (
        count_aggregation.get("kind") != "duckdb-typed-external-fanin-v1"
        or count_aggregation.get("engine") != "duckdb"
        or count_aggregation.get("engine_version") != REQUIRED_DUCKDB_VERSION
        or count_aggregation.get("maximum_memory_bytes")
        != PLAN_DUCKDB_MEMORY_LIMIT_BYTES
        or count_aggregation.get("maximum_scratch_bytes") != PLAN_MAX_SCRATCH_BYTES
        or count_aggregation.get("maximum_batch_rows")
        != PLAN_AGGREGATION_BATCH_ROWS
        or count_aggregation.get("registered_arrow_batches") is not True
        or count_aggregation.get("maximum_famous_candidates_in_memory")
        != PLAN_MAX_FAMOUS_CANDIDATES_IN_MEMORY
        or count_aggregation.get("group_aggregation")
        != "typed-ordered-external-stream-v1"
        or count_aggregation.get("maximum_execution_groups_in_memory")
        != MAX_EXECUTION_GROUPS_IN_MEMORY
        or count_aggregation.get("ordered_scan") != "duckdb-order-by-cell-v1"
    ):
        raise ValueError("Places count aggregation disk evidence is invalid")
    partition = require_exact(
        plan["partition"],
        {
            "scheme",
            "minimum_level",
            "maximum_level",
            "split_row_cap",
            "sticky_splits",
            "lineage_generation",
            "predecessor_family_manifest_sha256",
            "predecessor_family_manifest",
            "split_cells",
        },
        "Places executor partition",
    )
    if partition.get("scheme") != PARTITION_SCHEME:
        raise ValueError("Places executor plan partition scheme is invalid")
    minimum_level = partition.get("minimum_level")
    maximum_level = partition.get("maximum_level")
    row_cap = partition.get("split_row_cap")
    generation = require_int(
        partition.get("lineage_generation"),
        "Places lineage generation",
        minimum=1,
    )
    require_int(minimum_level, "Places minimum level", minimum=1)
    require_int(maximum_level, "Places maximum level", minimum=minimum_level)
    require_int(row_cap, "Places split row cap", minimum=1)
    predecessor = require_exact(
        partition["predecessor_family_manifest"],
        {"object_key", "bytes", "sha256"},
        "Places predecessor manifest identity",
    )
    predecessor_values = tuple(predecessor.values())
    if all(value is None for value in predecessor_values):
        if (
            generation != 1
            or partition["predecessor_family_manifest_sha256"] is not None
        ):
            raise ValueError("Places bootstrap lineage generation is inconsistent")
    elif any(value is None for value in predecessor_values):
        raise ValueError("Places predecessor identity must be all-null or all-set")
    else:
        if (
            generation <= 1
            or not isinstance(predecessor["object_key"], str)
            or not predecessor["object_key"].endswith(
                "/families/places/family-manifest.json"
            )
            or require_int(
                predecessor["bytes"], "Places predecessor manifest bytes", minimum=1
            )
            != predecessor["bytes"]
            or require_sha256(
                predecessor["sha256"], "Places predecessor manifest sha256"
            )
            != partition["predecessor_family_manifest_sha256"]
        ):
            raise ValueError("Places predecessor manifest identity is invalid")
    split_cells = validate_split_cells(
        partition.get("split_cells", []),
        minimum_level=minimum_level,
        maximum_level=maximum_level,
    )
    leaves = plan["leaves"]
    jobs = plan["reduce_jobs"]
    if (
        not isinstance(leaves, list)
        or not leaves
        or not isinstance(jobs, list)
        or not jobs
    ):
        raise ValueError("Places executor plan requires leaves and reduce jobs")
    expected_indices = list(range(len(jobs)))
    if [job.get("index") for job in jobs] != expected_indices or len(jobs) > 256:
        raise ValueError("Places reduce job indices are invalid")
    observed_cells: set[str] = set()
    observed_groups: set[str] = set()
    observed_fragments: set[str] = set()
    observed_pack_row_groups: set[tuple[str, int]] = set()
    total_rows = 0
    for job in jobs:
        if job.get("execution_identity_is_serving_identity") is not False:
            raise ValueError(
                "Places reduce job is incorrectly marked as serving identity"
            )
        require_sha256(job.get("job_digest"), "Places reduce job digest")
        groups = job.get("execution_groups")
        job_leaves = job.get("leaves")
        fragments = job.get("input_fragments")
        if (
            not isinstance(groups, list)
            or groups != sorted(groups)
            or not isinstance(job_leaves, list)
            or not isinstance(fragments, list)
        ):
            raise ValueError("Places reduce job groups/leaves/fragments are invalid")
        for group in groups:
            if (
                not isinstance(group, str)
                or len(group) != EXECUTION_GROUP_LEVEL
                or any(digit not in "0123" for digit in group)
                or group in observed_groups
            ):
                raise ValueError(
                    "Places reduce execution groups are duplicate or invalid"
                )
            observed_groups.add(group)
        job_rows = 0
        for leaf in job_leaves:
            cell = leaf.get("cell") if isinstance(leaf, dict) else None
            rows = leaf.get("rows") if isinstance(leaf, dict) else None
            validate_quadkey(cell, minimum=minimum_level, maximum=maximum_level)
            require_int(rows, f"Places leaf rows {cell}", minimum=1, maximum=row_cap)
            if cell in observed_cells or cell[:EXECUTION_GROUP_LEVEL] not in groups:
                raise ValueError(
                    "Places reduce leaf ownership is duplicate or inconsistent"
                )
            if cell in split_cells:
                raise ValueError("Places split cell cannot also be a leaf")
            observed_cells.add(cell)
            job_rows += rows
        fragment_rows = 0
        for fragment in fragments:
            object_key = (
                fragment.get("object_key") if isinstance(fragment, dict) else None
            )
            if not isinstance(object_key, str):
                raise ValueError("Places reduce input pack is invalid")
            selected_groups = fragment.get("selected_execution_groups")
            selected_row_groups = fragment.get("selected_row_groups")
            if (
                not isinstance(selected_groups, list)
                or selected_groups != sorted(set(selected_groups))
                or not set(selected_groups).issubset(groups)
                or not isinstance(selected_row_groups, list)
                or selected_row_groups != sorted(set(selected_row_groups))
                or not selected_row_groups
                or any(
                    type(index) is not int or not 0 <= index < fragment.get("row_group_count", 0)
                    for index in selected_row_groups
                )
            ):
                raise ValueError(
                    "Places reduce pack selection is assigned to the wrong group"
                )
            require_int(fragment.get("bytes"), "fragment bytes", minimum=1)
            require_sha256(fragment.get("sha256"), "fragment sha256")
            observed_fragments.add(object_key)
            for row_group_index in selected_row_groups:
                identity = (object_key, row_group_index)
                if identity in observed_pack_row_groups:
                    raise ValueError("Places reduce row-group ownership is duplicate")
                observed_pack_row_groups.add(identity)
            fragment_rows += require_int(
                fragment.get("records"), "fragment records", minimum=1
            )
            selected_compressed = require_int(
                fragment.get("selected_compressed_bytes"),
                "selected row-group compressed bytes",
                minimum=1,
            )
            selected_uncompressed = require_int(
                fragment.get("selected_uncompressed_bytes"),
                "selected row-group uncompressed bytes",
                minimum=1,
            )
            planned_fetch = require_int(
                fragment.get("planned_fetch_bytes"), "planned pack fetch bytes", minimum=1
            )
            maximum_materialized = require_int(
                fragment.get("maximum_materialized_bytes"),
                "pack maximum materialized bytes",
                minimum=1,
            )
            expected_mode = (
                "whole"
                if len(selected_row_groups) == fragment["row_group_count"]
                else "selective"
            )
            if (
                fragment.get("fetch_mode") != expected_mode
                or planned_fetch
                != (
                    fragment["bytes"]
                    if expected_mode == "whole"
                    else min(
                        fragment["bytes"],
                        selected_compressed
                        + fragment["footer_bytes"]
                        + SELECTIVE_FETCH_RANGE_ALLOWANCE_BYTES,
                    )
                )
                or maximum_materialized
                != (
                    fragment["bytes"]
                    if expected_mode == "whole"
                    else max(
                        fragment["bytes"],
                        selected_uncompressed
                        + fragment["footer_bytes"]
                        + SELECTIVE_FETCH_RANGE_ALLOWANCE_BYTES,
                    )
                )
            ):
                raise ValueError("Places adaptive pack fetch plan is invalid")
        if (
            job.get("expected_records") != job_rows
            or fragment_rows != job_rows
            or job.get("input_fragment_count") != len(fragments)
            or job.get("input_bytes") != sum(item["planned_fetch_bytes"] for item in fragments)
            or job.get("planned_fetch_bytes")
            != sum(item["planned_fetch_bytes"] for item in fragments)
            or job.get("selected_compressed_bytes")
            != sum(item["selected_compressed_bytes"] for item in fragments)
            or job.get("selected_uncompressed_bytes")
            != sum(item["selected_uncompressed_bytes"] for item in fragments)
            or job.get("maximum_materialized_bytes")
            != max((item["maximum_materialized_bytes"] for item in fragments), default=0)
            or job.get("whole_pack_fetches")
            != sum(item["fetch_mode"] == "whole" for item in fragments)
            or job.get("selective_pack_fetches")
            != sum(item["fetch_mode"] == "selective" for item in fragments)
        ):
            raise ValueError("Places reduce job accounting does not reconcile")
        fragment_bytes = sum(item["planned_fetch_bytes"] for item in fragments)
        group_fanin = Counter(
            group for item in fragments for group in item["selected_execution_groups"]
        )
        leaf_groups = {item["cell"][:EXECUTION_GROUP_LEVEL] for item in job_leaves}
        fragment_groups = set(group_fanin)
        if (
            len(fragments) > MAX_INPUT_FRAGMENTS_PER_REDUCE_JOB
            or fragment_bytes > MAX_INPUT_BYTES_PER_REDUCE_JOB
            or job["selected_uncompressed_bytes"]
            > MAX_SELECTED_UNCOMPRESSED_BYTES_PER_REDUCE_JOB
            or job["maximum_materialized_bytes"]
            > MAX_SIMULTANEOUS_MATERIALIZED_BYTES
            or job_rows > MAX_RETAINED_ROWS_PER_REDUCE_JOB
            or max(group_fanin.values(), default=0)
            > MAX_RAW_FRAGMENTS_PER_EXECUTION_GROUP
            or set(groups) != leaf_groups
            or set(groups) != fragment_groups
        ):
            raise ValueError("Places reduce job exceeds a serialized executor cap")
        expected_job_identity = {
            "kind": "places-reduce-job-v1",
            "request_sha256": plan["request"]["sha256"],
            "inventory_sha256": plan["inventory"]["sha256"],
            "map_completion_set_sha256": plan["map_fan_in"]["completion_set_sha256"],
            "index": job["index"],
            "execution_groups": groups,
            "leaves": [
                {"cell": leaf["cell"], "rows": leaf["rows"]} for leaf in job_leaves
            ],
            "fragment_selections": [
                {"sha256": item["sha256"], "row_groups": item["selected_row_groups"]}
                for item in fragments
            ],
        }
        if job["job_digest"] != digest_value(expected_job_identity):
            raise ValueError("Places reduce job digest differs from its contents")
        total_rows += job_rows
    leaf_cells = [leaf["cell"] for leaf in leaves]
    if leaf_cells != sorted(leaf_cells) or set(leaf_cells) != observed_cells:
        raise ValueError("Places top-level leaf index differs from reduce ownership")
    leaves_by_job = [{item["cell"] for item in job["leaves"]} for job in jobs]
    if any(
        leaf["reduce_job_index"] < 0
        or leaf["reduce_job_index"] >= len(jobs)
        or leaf["execution_group"] != leaf["cell"][:EXECUTION_GROUP_LEVEL]
        or leaf["cell"] not in leaves_by_job[leaf["reduce_job_index"]]
        for leaf in leaves
    ):
        raise ValueError("Places top-level leaf routing differs from reduce jobs")
    if (
        total_rows != plan["totals"]["retained_records"]
        or len(leaves) != plan["totals"]["leaves"]
        or len(jobs) != plan["totals"]["reduce_jobs"]
        or len(observed_fragments) != plan["totals"]["input_fragments"]
    ):
        raise ValueError("Places executor plan totals do not reconcile")
    pack_job_fanout = Counter(
        item["object_key"] for job in jobs for item in job["input_fragments"]
    )
    expected_fanout = {
        "physical_packs": len(observed_fragments),
        "pack_job_references": sum(pack_job_fanout.values()),
        "maximum_reducers_per_pack": max(pack_job_fanout.values(), default=0),
        "whole_pack_fetches": sum(job["whole_pack_fetches"] for job in jobs),
        "selective_pack_fetches": sum(job["selective_pack_fetches"] for job in jobs),
        "selected_compressed_bytes": sum(job["selected_compressed_bytes"] for job in jobs),
        "selected_uncompressed_bytes": sum(job["selected_uncompressed_bytes"] for job in jobs),
        "planned_fetch_bytes": sum(job["planned_fetch_bytes"] for job in jobs),
        "maximum_materialized_bytes": max(
            (job["maximum_materialized_bytes"] for job in jobs), default=0
        ),
    }
    if plan["map_fan_in"]["pack_reducer_fanout"] != expected_fanout:
        raise ValueError("Places pack/reducer fanout evidence does not reconcile")
    cells = sorted(observed_cells)
    if any(right.startswith(left) for left, right in zip(cells, cells[1:])):
        raise ValueError("Places executor plan has overlapping leaf ownership")
    return plan


def _read_json_files(directory: Path) -> list[Any]:
    paths = sorted(directory.rglob("*.json"))
    if not paths:
        raise ValueError(f"no JSON completions found under {directory}")
    return [json.loads(path.read_text()) for path in paths]


def _artifact_identity(path: Path, object_key: str) -> dict[str, Any]:
    digest, size = sha256_file(path)
    if size < 1:
        raise ValueError(f"Places final artifact is empty: {path}")
    return {"object_key": object_key, "bytes": size, "sha256": digest}


def _read_component(
    path: Path, offset: int, length: int, name: str, *, source: Any | None = None
) -> bytes:
    if offset < 0 or length < 0:
        raise ValueError(f"Places {name} range is invalid")
    if source is None:
        with path.open("rb") as opened:
            opened.seek(offset)
            data = opened.read(length)
    else:
        source.seek(offset)
        data = source.read(length)
    if len(data) != length:
        raise ValueError(f"Places {name} range is truncated")
    return data


def _read_serving_directory(
    path: Path, *, magic: bytes, preamble: Any, name: str
) -> tuple[dict[str, Any], int]:
    size = path.stat().st_size
    if size < preamble.size:
        raise ValueError(f"Places {name} preamble is truncated")
    raw = _read_component(path, 0, preamble.size, f"{name} preamble")
    observed_magic, directory_length = preamble.unpack(raw)
    if (
        observed_magic != magic
        or directory_length < 2
        or preamble.size + directory_length > size
        or directory_length > 64 * 1024 * 1024
    ):
        raise ValueError(f"Places {name} framing is invalid")
    try:
        directory = json.loads(
            _read_component(path, preamble.size, directory_length, f"{name} directory")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Places {name} directory is invalid JSON") from exc
    if not isinstance(directory, dict):
        raise ValueError(f"Places {name} directory must be an object")
    return directory, preamble.size + directory_length


def _validate_contiguous_components(
    path: Path,
    directory: dict[str, Any],
    names: tuple[str, ...],
    start: int,
    artifact_name: str,
) -> dict[str, tuple[int, int]]:
    raw = require_exact(
        directory.get("components"), set(names), f"Places {artifact_name} components"
    )
    result: dict[str, tuple[int, int]] = {}
    cursor = start
    for name in names:
        component = require_exact(
            raw[name], {"offset", "length"}, f"Places {artifact_name} {name}"
        )
        offset = require_int(component["offset"], f"Places {name} offset")
        length = require_int(component["length"], f"Places {name} length")
        if offset != cursor:
            raise ValueError(f"Places {artifact_name} components are not contiguous")
        result[name] = (offset, length)
        cursor += length
    if cursor != path.stat().st_size:
        raise ValueError(
            f"Places {artifact_name} has trailing or missing component bytes"
        )
    return result


def _encode_lexicon_block(entries: list[Any]) -> bytes:
    encoded = bytearray(encode_varint(len(entries)))
    previous = b""
    for entry in entries:
        key = entry.token.encode("utf-8")
        shared = common_prefix(previous, key)
        suffix = key[shared:]
        encoded += encode_varint(shared) + encode_varint(len(suffix)) + suffix
        encoded += encode_varint(entry.posting_offset)
        encoded += encode_varint(entry.posting_length)
        encoded += encode_varint(entry.posting_count)
        previous = key
    return bytes(encoded)


def _projection_round_trips(data: bytes) -> bool:
    decoded = decode_projection(data)
    values = (
        decoded.get(field)
        for field in ("name", "category", "locality", "region", "country")
    )
    name, category, locality, region, country = values
    place = Place(
        place_id=decoded["id"],
        name=name,
        brand="",
        category=category,
        locality=locality,
        region=region,
        country=country,
        lat=decoded["lat"],
        lon=decoded["lon"],
        confidence=decoded["confidence"],
    )
    return (
        math.isfinite(place.lat)
        and math.isfinite(place.lon)
        and 0 <= place.confidence <= 1
        and encode_projection(place) == data
    )


def _validate_pcsh_posting(data: bytes, count: int, record_count: int) -> None:
    offset = 0
    previous = -1
    allowed_mask = sum(PCSH_FIELD_BITS.values())
    for index in range(count):
        delta, offset = decode_varint(data, offset)
        doc_id = delta if index == 0 else previous + delta
        if (
            offset + 2 > len(data)
            or doc_id >= record_count
            or doc_id <= previous
            or data[offset] < 1
            or data[offset] & ~allowed_mask
        ):
            raise ValueError("Places PCSH posting content is invalid")
        offset += 2
        previous = doc_id
    if offset != len(data):
        raise ValueError("Places PCSH posting length is invalid")


def _validate_pcsh(path: Path, expected_rows: int) -> dict[str, int]:
    directory, component_start = _read_serving_directory(
        path, magic=PCSH_MAGIC, preamble=PCSH_PREAMBLE, name="PCSH"
    )
    require_exact(
        directory,
        {
            "schema_version",
            "tokenizer_version",
            "record_count",
            "token_count",
            "cell_degrees",
            "field_bits",
            "lexicon_blocks",
            "components",
        },
        "Places PCSH directory",
    )
    record_count = require_int(directory["record_count"], "Places PCSH record count")
    token_count = require_int(directory["token_count"], "Places PCSH token count")
    if (
        directory["schema_version"] != 1
        or directory["tokenizer_version"] != PCSH_TOKENIZER_VERSION
        or record_count != expected_rows
        or directory["cell_degrees"] != 0.25
        or directory["field_bits"] != PCSH_FIELD_BITS
    ):
        raise ValueError("Places PCSH directory contract is invalid")
    components = _validate_contiguous_components(
        path,
        directory,
        ("lexicon", "postings", "record_index", "records"),
        component_start,
        "PCSH",
    )
    if components["record_index"][1] != record_count * PCSH_RECORD_INDEX.size:
        raise ValueError("Places PCSH record index cardinality is invalid")
    blocks = directory["lexicon_blocks"]
    if not isinstance(blocks, list):
        raise ValueError("Places PCSH lexicon blocks must be an array")
    lexicon_base, lexicon_length = components["lexicon"]
    postings_base, postings_length = components["postings"]
    lexicon_cursor = posting_cursor = decoded_tokens = 0
    previous_token: bytes | None = None
    artifact_source = path.open("rb")
    for raw_block in blocks:
        block = require_exact(
            raw_block,
            {"first", "last", "offset", "length", "entries"},
            "Places PCSH lexicon block",
        )
        block_offset = require_int(block["offset"], "Places lexicon block offset")
        block_length = require_int(
            block["length"], "Places lexicon block length", minimum=1
        )
        block_entries = require_int(
            block["entries"], "Places lexicon block entries", minimum=1
        )
        if (
            block_offset != lexicon_cursor
            or block_offset + block_length > lexicon_length
        ):
            raise ValueError("Places PCSH lexicon block coverage is invalid")
        encoded = _read_component(
            path,
            lexicon_base + block_offset,
            block_length,
            "PCSH lexicon block",
            source=artifact_source,
        )
        entries = decode_lexicon_block(encoded)
        if (
            len(entries) != block_entries
            or _encode_lexicon_block(entries) != encoded
            or entries[0].token != block["first"]
            or entries[-1].token != block["last"]
        ):
            raise ValueError("Places PCSH lexicon block content is invalid")
        for entry in entries:
            token = entry.token.encode("utf-8")
            if previous_token is not None and token <= previous_token:
                raise ValueError("Places PCSH lexicon tokens are not strictly ordered")
            if (
                entry.posting_offset != posting_cursor
                or entry.posting_length < 1
                or entry.posting_count < 1
                or posting_cursor + entry.posting_length > postings_length
            ):
                raise ValueError("Places PCSH posting extent is invalid")
            encoded_posting = _read_component(
                path,
                postings_base + entry.posting_offset,
                entry.posting_length,
                "PCSH posting",
                source=artifact_source,
            )
            _validate_pcsh_posting(encoded_posting, entry.posting_count, record_count)
            posting_cursor += entry.posting_length
            previous_token = token
        lexicon_cursor += block_length
        decoded_tokens += len(entries)
    artifact_source.close()
    if (
        lexicon_cursor != lexicon_length
        or posting_cursor != postings_length
        or decoded_tokens != token_count
    ):
        raise ValueError("Places PCSH lexicon/posting totals do not reconcile")
    index_offset, _ = components["record_index"]
    records_offset, records_length = components["records"]
    intervals: list[tuple[int, int]] = []
    with path.open("rb") as source:
        source.seek(index_offset)
        for _ in range(record_count):
            raw = source.read(PCSH_RECORD_INDEX.size)
            if len(raw) != PCSH_RECORD_INDEX.size:
                raise ValueError("Places PCSH record index is truncated")
            offset, length = PCSH_RECORD_INDEX.unpack(raw)
            if length < 1 or offset + length > records_length:
                raise ValueError("Places PCSH record extent is invalid")
            intervals.append((offset, length))
    intervals.sort()
    cursor = 0
    with path.open("rb") as source:
        for offset, length in intervals:
            if offset != cursor:
                raise ValueError("Places PCSH records overlap or have gaps")
            source.seek(records_offset + offset)
            projection = source.read(length)
            try:
                valid_projection = _projection_round_trips(projection)
            except (KeyError, TypeError, ValueError, UnicodeDecodeError):
                valid_projection = False
            if not valid_projection:
                raise ValueError("Places PCSH record projection is invalid")
            cursor += length
    if cursor != records_length:
        raise ValueError("Places PCSH record bytes do not reconcile")
    return {"records": record_count, "tokens": token_count}


def _head_record_round_trips(data: bytes) -> bool:
    decoded = decode_record(data)
    place = Place(
        place_id=decoded["id"],
        name=decoded["name"],
        brand=decoded["brand"],
        category=decoded["category"],
        locality=decoded["locality"],
        region=decoded["region"],
        country=decoded["country"],
        lat=decoded["lat"],
        lon=decoded["lon"],
        confidence=decoded["confidence"],
    )
    return (
        math.isfinite(place.lat)
        and math.isfinite(place.lon)
        and 0 <= place.confidence <= 1
        and encode_record(place) == data
    )


def _validate_phrp(
    path: Path, request: dict[str, Any], plan: dict[str, Any]
) -> dict[str, int]:
    directory, component_start = _read_serving_directory(
        path, magic=PHRP_MAGIC, preamble=PHRP_PREAMBLE, name="PHRP"
    )
    policy = request["families"]["places"]["global_head"]
    expected_fields = {
        "schema_version",
        "magic",
        "key_count",
        "head_limit",
        "provenance",
        "components",
    }
    if policy["famous_cap"] > 0:
        expected_fields.update({"head_famous_cap", "e2_key_count", "admission"})
    require_exact(directory, expected_fields, "Places PHRP directory")
    key_count = require_int(directory["key_count"], "Places PHRP key count")
    if (
        directory["schema_version"] != 1
        or directory["magic"] != PHRP_MAGIC.decode()
        or directory["head_limit"] != policy["result_cap"]
        or key_count > READER_MAX_HEAD_KEYS
        or directory.get("head_famous_cap", 0) != policy["famous_cap"]
        or directory.get("admission", HEAD_ADMISSION_MARKER) != HEAD_ADMISSION_MARKER
        or directory["provenance"]
        != {
            "request_sha256": plan["request"]["sha256"],
            "plan_sha256": plan["plan_sha256"],
            "head_policy_sha256": digest_value(policy),
            "lineage_generation": plan["partition"]["lineage_generation"],
            "predecessor_family_manifest_sha256": plan["partition"][
                "predecessor_family_manifest_sha256"
            ],
            "predecessor_family_manifest": plan["partition"][
                "predecessor_family_manifest"
            ],
        }
    ):
        raise ValueError("Places PHRP directory contract/provenance is invalid")
    components = _validate_contiguous_components(
        path,
        directory,
        ("key_index", "entries"),
        component_start,
        "PHRP",
    )
    index_offset, index_length = components["key_index"]
    entries_offset, entries_length = components["entries"]
    if index_length > READER_MAX_HEAD_INDEX_BYTES:
        raise ValueError("Places PHRP resident index exceeds its reader cap")
    artifact_source = path.open("rb")
    encoded_index = _read_component(
        path,
        index_offset,
        index_length,
        "PHRP key index",
        source=artifact_source,
    )
    index_cursor = entry_cursor = decoded_keys = e2_keys = 0
    decoded_wire_keys: list[str] = []
    previous_key = ""
    while index_cursor < len(encoded_index):
        key_length, index_cursor = decode_varint(encoded_index, index_cursor)
        key_end = index_cursor + key_length
        if key_end > len(encoded_index):
            raise ValueError("Places PHRP key index is truncated")
        key_bytes = encoded_index[index_cursor:key_end]
        index_cursor = key_end
        try:
            key = key_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Places PHRP key is invalid UTF-8") from exc
        offset, index_cursor = decode_varint(encoded_index, index_cursor)
        length, index_cursor = decode_varint(encoded_index, index_cursor)
        family, separator, suffix = key.partition(":")
        if (
            not separator
            or not suffix
            or family not in HEAD_KEY_FAMILIES
            or key <= previous_key
            or key_length > READER_MAX_KEY_BYTES
            or offset != entry_cursor
            or length > READER_MAX_HEAD_ENTRY_BYTES
            or offset + length > entries_length
        ):
            raise ValueError("Places PHRP key/entry extent is invalid")
        encoded_entry = _read_component(
            path,
            entries_offset + offset,
            length,
            "PHRP head entry",
            source=artifact_source,
        )
        record_cursor = records = 0
        identifiers: set[str] = set()
        while record_cursor < len(encoded_entry):
            record_length, record_cursor = decode_varint(encoded_entry, record_cursor)
            record_end = record_cursor + record_length
            if record_end > len(encoded_entry):
                raise ValueError("Places PHRP head record is truncated")
            record = encoded_entry[record_cursor:record_end]
            try:
                decoded = decode_record(record)
                valid_record = _head_record_round_trips(record)
            except (KeyError, TypeError, ValueError, UnicodeDecodeError):
                valid_record = False
                decoded = {}
            if not valid_record or decoded.get("id") in identifiers:
                raise ValueError("Places PHRP head record content is invalid")
            identifiers.add(decoded["id"])
            records += 1
            if records > policy["result_cap"]:
                raise ValueError("Places PHRP head entry exceeds its result cap")
            record_cursor = record_end
        if records < 1:
            raise ValueError("Places PHRP head entry is empty")
        decoded_keys += 1
        decoded_wire_keys.append(key)
        e2_keys += family == "e2"
        entry_cursor += length
        previous_key = key
    artifact_source.close()
    if (
        decoded_keys != key_count
        or decoded_wire_keys != [item["key"] for item in plan["head_admission"]["keys"]]
        or entry_cursor != entries_length
        or directory.get("e2_key_count", 0) != e2_keys
    ):
        raise ValueError("Places PHRP directory/key totals do not reconcile")
    return {
        "key_count": key_count,
        "key_index_bytes": index_length,
        "entries_bytes": entries_length,
    }


class _ServingArtifactMaterializer:
    def __init__(
        self,
        *,
        output_dir: Path,
        scratch_dir: Path,
        fetch_command: list[str] | None,
    ) -> None:
        self.output_dir = output_dir
        self.scratch_dir = scratch_dir
        self.fetch_command = fetch_command
        self.fetched_artifacts = 0
        self.fetched_bytes = 0
        self.peak_staged_artifact_bytes = 0

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink(missing_ok=True)

    @contextlib.contextmanager
    def path(
        self,
        *,
        local_name: str,
        object_key: str,
        expected_bytes: int,
        expected_sha256: str,
    ) -> Iterator[Path]:
        local = self.output_dir / local_name
        temporary: Path | None = None
        if not local.is_file():
            if self.fetch_command is None:
                raise ValueError(f"missing Places serving artifact: {object_key}")
            if expected_bytes > FINALIZE_MAX_STAGED_ARTIFACT_BYTES:
                raise ValueError(
                    "Places serving artifact exceeds finalizer staging cap"
                )
            self.scratch_dir.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix="places-finalize-artifact-",
                suffix=Path(local_name).suffix,
                dir=self.scratch_dir,
            )
            os.close(descriptor)
            temporary = Path(name)
            temporary.unlink()
            argv = [
                item.replace("{object_key}", object_key).replace(
                    "{output}", str(temporary)
                )
                for item in self.fetch_command
            ]
            try:
                subprocess.run(argv, check=True)
            except (OSError, subprocess.CalledProcessError) as exc:
                self._remove(temporary)
                raise ValueError(
                    "Places serving artifact fetch adapter failed"
                ) from exc
            if not temporary.is_file():
                self._remove(temporary)
                raise ValueError(
                    "Places serving artifact fetch adapter produced no file"
                )
            local = temporary
        try:
            actual_sha256, actual_bytes = sha256_file(local)
            if (actual_bytes, actual_sha256) != (expected_bytes, expected_sha256):
                raise ValueError(
                    f"Places serving artifact identity mismatch: {object_key}"
                )
            if temporary is not None:
                self.fetched_artifacts += 1
                self.fetched_bytes += actual_bytes
                self.peak_staged_artifact_bytes = max(
                    self.peak_staged_artifact_bytes, actual_bytes
                )
                if self.peak_staged_artifact_bytes > FINALIZE_MAX_STAGED_ARTIFACT_BYTES:
                    raise ValueError(
                        "Places serving artifact exceeded finalizer staging cap"
                    )
            yield local
        finally:
            if temporary is not None:
                self._remove(temporary)

    def evidence(self) -> dict[str, Any]:
        return {
            "adapter": "local-or-no-shell-argv-v1",
            "remote_fetch_enabled": self.fetch_command is not None,
            "fetched_artifacts": self.fetched_artifacts,
            "fetched_bytes": self.fetched_bytes,
            "maximum_simultaneously_materialized_artifacts": 1,
            "maximum_staged_artifact_bytes": FINALIZE_MAX_STAGED_ARTIFACT_BYTES,
            "peak_staged_artifact_bytes": self.peak_staged_artifact_bytes,
            "identity_verification": "exact-report-bytes-and-sha256",
            "semantic_verification": "independent-pcsh-phrp-parser-v1",
        }


def _validate_final_materialization(
    value: Any, *, expected_artifacts: int, expected_bytes: int, maximum_bytes: int
) -> dict[str, Any]:
    evidence = require_exact(
        value,
        {
            "adapter",
            "remote_fetch_enabled",
            "fetched_artifacts",
            "fetched_bytes",
            "maximum_simultaneously_materialized_artifacts",
            "maximum_staged_artifact_bytes",
            "peak_staged_artifact_bytes",
            "identity_verification",
            "semantic_verification",
        },
        "Places finalizer materialization evidence",
    )
    fetched_artifacts = require_int(
        evidence["fetched_artifacts"], "Places finalizer fetched artifacts"
    )
    fetched_bytes = require_int(
        evidence["fetched_bytes"], "Places finalizer fetched bytes"
    )
    peak = require_int(
        evidence["peak_staged_artifact_bytes"],
        "Places finalizer peak staged artifact bytes",
    )
    if (
        evidence["adapter"] != "local-or-no-shell-argv-v1"
        or type(evidence["remote_fetch_enabled"]) is not bool
        or evidence["maximum_simultaneously_materialized_artifacts"] != 1
        or evidence["maximum_staged_artifact_bytes"]
        != FINALIZE_MAX_STAGED_ARTIFACT_BYTES
        or evidence["identity_verification"] != "exact-report-bytes-and-sha256"
        or evidence["semantic_verification"] != "independent-pcsh-phrp-parser-v1"
        or peak > FINALIZE_MAX_STAGED_ARTIFACT_BYTES
        or (
            evidence["remote_fetch_enabled"]
            and (
                fetched_artifacts != expected_artifacts
                or fetched_bytes != expected_bytes
                or peak != maximum_bytes
            )
        )
        or (
            not evidence["remote_fetch_enabled"]
            and (fetched_artifacts != 0 or fetched_bytes != 0 or peak != 0)
        )
    ):
        raise ValueError("Places finalizer materialization evidence is invalid")
    return evidence


def finalize_places_family(
    request_value: Any,
    plan_value: Any,
    reduce_reports: list[Any],
    head_report: Any,
    *,
    output_dir: Path,
    scratch_dir: Path | None = None,
    fragment_fetch_command: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    request = global_v2_build_request.validate_request(request_value)
    plan = validate_places_plan(plan_value)
    if request_sha256(request) != plan["request"]["sha256"]:
        raise ValueError("Places finalizer request differs from the executor plan")
    from global_v2_places_reduce import (  # noqa: PLC0415
        validate_fetch_command,
        validate_reduce_report,
    )
    from global_v2_places_head import validate_head_report  # noqa: PLC0415

    fragment_fetch_command = validate_fetch_command(fragment_fetch_command)
    materializer = _ServingArtifactMaterializer(
        output_dir=output_dir,
        scratch_dir=scratch_dir or output_dir / ".places-finalize-scratch",
        fetch_command=fragment_fetch_command,
    )

    expected_jobs = {job["index"]: job for job in plan["reduce_jobs"]}
    observed: dict[int, dict[str, Any]] = {}
    shard_cells: set[str] = set()
    shard_objects: set[str] = set()
    routes: list[dict[str, Any]] = []
    shard_artifacts: list[dict[str, Any]] = []
    shard_rows = 0
    for raw in reduce_reports:
        report = validate_reduce_report(raw, plan)
        index = report["job_index"]
        if index not in expected_jobs or index in observed:
            raise ValueError(
                "Places reduce completion is unexpected, duplicate, or replayed"
            )
        for shard in report["shards"]:
            cell = shard["cell"]
            object_name = shard["object"]
            if cell in shard_cells or object_name in shard_objects:
                raise ValueError("Places final shard identity is duplicate/replayed")
            durable_key = f"families/places/{object_name}"
            with materializer.path(
                local_name=object_name,
                object_key=durable_key,
                expected_bytes=shard["bytes"],
                expected_sha256=shard["sha256"],
            ) as path:
                actual = _artifact_identity(path, durable_key)
                parsed_shard = _validate_pcsh(path, shard["rows"])
                if parsed_shard["records"] != shard["rows"]:
                    raise ValueError(
                        "Places parsed PCSH rows differ from reduce completion"
                    )
            route = places_builder._route(cell)
            if route["object"] != object_name:
                raise ValueError("Places final shard route/object is inconsistent")
            routes.append(route)
            shard_artifacts.append(actual)
            shard_cells.add(cell)
            shard_objects.add(object_name)
            shard_rows += shard["rows"]
        observed[index] = report
    missing = sorted(set(expected_jobs) - set(observed))
    if missing:
        raise ValueError(f"missing Places reduce completions: {missing}")
    expected_cells = {leaf["cell"] for leaf in plan["leaves"]}
    if (
        shard_cells != expected_cells
        or shard_rows != plan["totals"]["retained_records"]
    ):
        raise ValueError("Places retained rows do not reconcile to final shard leaves")
    validated_head = validate_head_report(head_report, request, plan)
    head_key = "families/places/head.phrp"
    with materializer.path(
        local_name="head.phrp",
        object_key=head_key,
        expected_bytes=validated_head["artifact"]["bytes"],
        expected_sha256=validated_head["artifact"]["sha256"],
    ) as head_path:
        head_artifact = _artifact_identity(head_path, head_key)
        parsed_head = _validate_phrp(head_path, request, plan)
        if any(
            parsed_head[field] != validated_head["object"].get(field)
            for field in ("key_count", "key_index_bytes", "entries_bytes")
        ):
            raise ValueError(
                "Places parsed PHRP directory differs from head completion"
            )
    partition = plan["partition"]
    routes.sort(key=lambda route: route["cell"])
    catalog_path = output_dir / "catalog.pcat"
    catalog_report = places_builder.build_catalog(
        routes,
        catalog_path,
        coverage=WORLD,
        minimum_level=partition["minimum_level"],
        maximum_level=partition["maximum_level"],
        row_cap=partition["split_row_cap"],
        split_cells=partition["split_cells"],
        lineage_generation=partition["lineage_generation"],
    )
    catalog_artifact = _artifact_identity(catalog_path, "families/places/catalog.pcat")
    artifacts = sorted(
        [*shard_artifacts, catalog_artifact, head_artifact],
        key=lambda item: item["object_key"],
    )
    places = request["families"]["places"]
    family_manifest = global_build_manifest.build_family_manifest(
        "places",
        lineage={
            "overture_release": request["overture_release"],
            "build_id": plan["plan_sha256"],
            "producer_commit": request["producer_commit"],
            "producer_script": "scripts/global_v2_places_plan.py",
            "producer_version": PLAN_VERSION,
        },
        versions={
            "format": places["versions"]["format"],
            "tokenizer": places["versions"]["tokenizer"],
            "normalization": None,
        },
        region={"name": "global", "bbox": WORLD, "bbox_scope": "exact"},
        artifacts=artifacts,
        generated_at=None,
    )
    serving_artifact_bytes = [
        *(artifact["bytes"] for artifact in shard_artifacts),
        head_artifact["bytes"],
    ]
    final_materialization = _validate_final_materialization(
        materializer.evidence(),
        expected_artifacts=len(serving_artifact_bytes),
        expected_bytes=sum(serving_artifact_bytes),
        maximum_bytes=max(serving_artifact_bytes, default=0),
    )
    without_digest = {
        "schema": FINAL_REPORT_SCHEMA,
        "plan_sha256": plan["plan_sha256"],
        "request_sha256": plan["request"]["sha256"],
        "predecessor_family_manifest_sha256": partition[
            "predecessor_family_manifest_sha256"
        ],
        "predecessor_family_manifest": partition["predecessor_family_manifest"],
        "lineage_generation": partition["lineage_generation"],
        "artifact_materialization": final_materialization,
        "accounting": {
            "map_retained_records": plan["map_fan_in"]["retained_records"],
            "planned_leaf_records": sum(leaf["rows"] for leaf in plan["leaves"]),
            "reduced_records": sum(
                report["accounting"]["output_records"] for report in observed.values()
            ),
            "final_shard_records": shard_rows,
            "final_shards": len(shard_cells),
            "reduce_jobs": len(observed),
        },
        "catalog": {**catalog_report, **catalog_artifact},
        "head": validated_head,
        "family_manifest_digest": family_manifest["manifest_digest"],
        "artifacts": artifacts,
    }
    values = list(without_digest["accounting"].values())[:4]
    if len(set(values)) != 1:
        raise AssertionError("Places end-to-end retained row accounting differs")
    final_report = {
        **without_digest,
        "report_sha256": digest_value(without_digest),
    }
    validate_places_final_report(final_report, request, plan)
    return final_report, family_manifest


def validate_places_final_report(
    value: Any, request_value: Any, plan_value: Any
) -> dict[str, Any]:
    request = global_v2_build_request.validate_request(request_value)
    plan = validate_places_plan(plan_value)
    report = require_exact(
        value,
        {
            "schema",
            "plan_sha256",
            "request_sha256",
            "predecessor_family_manifest_sha256",
            "predecessor_family_manifest",
            "lineage_generation",
            "artifact_materialization",
            "accounting",
            "catalog",
            "head",
            "family_manifest_digest",
            "artifacts",
            "report_sha256",
        },
        "Places final report",
    )
    require_sha256(report["report_sha256"], "Places final report sha256")
    without_digest = {
        key: item for key, item in report.items() if key != "report_sha256"
    }
    if (
        report["schema"] != FINAL_REPORT_SCHEMA
        or report["plan_sha256"] != plan["plan_sha256"]
        or report["request_sha256"] != request_sha256(request)
        or report["predecessor_family_manifest_sha256"]
        != plan["partition"]["predecessor_family_manifest_sha256"]
        or report["predecessor_family_manifest"]
        != plan["partition"]["predecessor_family_manifest"]
        or report["lineage_generation"] != plan["partition"]["lineage_generation"]
        or not isinstance(report["catalog"], dict)
        or not isinstance(report["catalog"].get("partition"), dict)
        or report["catalog"]["partition"].get("lineage_generation")
        != plan["partition"]["lineage_generation"]
        or digest_value(without_digest) != report["report_sha256"]
    ):
        raise ValueError("Places final report provenance/digest is invalid")
    accounting = report["accounting"]
    retained = plan["totals"]["retained_records"]
    if accounting != {
        "map_retained_records": retained,
        "planned_leaf_records": retained,
        "reduced_records": retained,
        "final_shard_records": retained,
        "final_shards": plan["totals"]["leaves"],
        "reduce_jobs": plan["totals"]["reduce_jobs"],
    }:
        raise ValueError("Places final report accounting is invalid")
    artifacts = report["artifacts"]
    if not isinstance(artifacts, list):
        raise ValueError("Places final report artifacts must be an array")
    normalized = [
        require_exact(item, {"object_key", "bytes", "sha256"}, "Places final artifact")
        for item in artifacts
    ]
    keys = [item["object_key"] for item in normalized]
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise ValueError("Places final report artifacts are duplicate or unordered")
    for artifact in normalized:
        require_int(artifact["bytes"], "Places final artifact bytes", minimum=1)
        require_sha256(artifact["sha256"], "Places final artifact sha256")
    serving = [
        item
        for item in normalized
        if item["object_key"].endswith(".pcsh")
        or item["object_key"] == "families/places/head.phrp"
    ]
    if (
        len(serving) != plan["totals"]["leaves"] + 1
        or "families/places/catalog.pcat" not in keys
    ):
        raise ValueError("Places final serving artifact set is incomplete")
    _validate_final_materialization(
        report["artifact_materialization"],
        expected_artifacts=len(serving),
        expected_bytes=sum(item["bytes"] for item in serving),
        maximum_bytes=max(item["bytes"] for item in serving),
    )
    from global_v2_places_head import validate_head_report  # noqa: PLC0415

    validate_head_report(report["head"], request, plan)
    require_sha256(
        report["family_manifest_digest"], "Places final family manifest digest"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--request", type=Path, required=True)
    build.add_argument("--inventory", type=Path, required=True)
    build.add_argument("--map-reports-dir", type=Path, required=True)
    build.add_argument("--artifacts-root", type=Path, required=True)
    build.add_argument("--artifact-listing", type=Path)
    build.add_argument("--scratch-dir", type=Path, required=True)
    build.add_argument("--predecessor-family-manifest", type=Path)
    build.add_argument("--predecessor-catalog", type=Path)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--head-admission-output", type=Path)
    validate = commands.add_parser("validate")
    validate.add_argument("--plan", type=Path, required=True)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--request", type=Path, required=True)
    finalize.add_argument("--plan", type=Path, required=True)
    finalize.add_argument("--reduce-reports-dir", type=Path, required=True)
    finalize.add_argument("--head-report", type=Path, required=True)
    finalize.add_argument("--output-dir", type=Path, required=True)
    finalize.add_argument("--scratch-dir", type=Path)
    finalize.add_argument("--fragment-fetch-command-json")
    finalize.add_argument("--report", type=Path, required=True)
    finalize.add_argument("--family-manifest-output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "validate":
        plan = validate_places_plan(json.loads(args.plan.read_text()))
        print(
            json.dumps(
                {"valid": True, "plan_sha256": plan["plan_sha256"]}, sort_keys=True
            )
        )
        return
    if args.command == "build":
        predecessor_manifest = (
            None
            if args.predecessor_family_manifest is None
            else json.loads(args.predecessor_family_manifest.read_text())
        )
        plan = build_places_plan(
            json.loads(args.request.read_text()),
            json.loads(args.inventory.read_text()),
            _read_json_files(args.map_reports_dir),
            artifact_root=args.artifacts_root,
            scratch_dir=args.scratch_dir,
            artifact_listing=(
                None
                if args.artifact_listing is None
                else json.loads(args.artifact_listing.read_text())
            ),
            predecessor_family_manifest=predecessor_manifest,
            predecessor_catalog=args.predecessor_catalog,
            head_admission_output=(
                args.head_admission_output
                or args.output.parent / "head-admission.json"
            ),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        print(
            json.dumps(
                {
                    "plan_sha256": plan["plan_sha256"],
                    "reduce_jobs": len(plan["reduce_jobs"]),
                    "leaves": len(plan["leaves"]),
                    "retained_records": plan["totals"]["retained_records"],
                },
                sort_keys=True,
            )
        )
        return
    from global_v2_places_reduce import parse_fetch_command  # noqa: PLC0415

    final_report, family_manifest = finalize_places_family(
        json.loads(args.request.read_text()),
        json.loads(args.plan.read_text()),
        _read_json_files(args.reduce_reports_dir),
        json.loads(args.head_report.read_text()),
        output_dir=args.output_dir,
        scratch_dir=args.scratch_dir,
        fragment_fetch_command=parse_fetch_command(args.fragment_fetch_command_json),
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(final_report, indent=2, sort_keys=True) + "\n")
    args.family_manifest_output.parent.mkdir(parents=True, exist_ok=True)
    args.family_manifest_output.write_text(
        json.dumps(family_manifest, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {
                "report_sha256": final_report["report_sha256"],
                "family_manifest_digest": family_manifest["manifest_digest"],
                "shards": final_report["accounting"]["final_shards"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
