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
import gzip
import hashlib
import json
import math
import os
import sqlite3
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
)
from global_v2_places_inventory import validate_inventory  # noqa: E402
from global_v2_places_map import (  # noqa: E402
    COUNT_ARTIFACT_SCHEMA,
    EXECUTION_GROUP_LEVEL,
    MAP_REPORT_SCHEMA,
    REJECTION_PRECEDENCE,
)
from places_partition import (  # noqa: E402
    PARTITION_SCHEME,
    plan_partition_cells,
    validate_quadkey,
    validate_split_cells,
)


PLAN_SCHEMA = "overture-global-v2-places-executor-plan-v1"
FINAL_REPORT_SCHEMA = "overture-global-v2-places-final-report-v1"
ARTIFACT_LISTING_SCHEMA = "overture-global-v2-intermediate-listing-v1"
PLAN_VERSION = "1"
REQUIRED_PYTHON_VERSION = "3.11.14"
REQUIRED_PYARROW_VERSION = "25.0.0"
REQUIRED_RUNTIME = {
    "python_version": REQUIRED_PYTHON_VERSION,
    "pyarrow_version": REQUIRED_PYARROW_VERSION,
}
MAX_RAW_FRAGMENTS_PER_EXECUTION_GROUP = 2_048
MAX_INPUT_FRAGMENTS_PER_REDUCE_JOB = 65_536
MAX_INPUT_BYTES_PER_REDUCE_JOB = 12_000_000_000
MAX_RETAINED_ROWS_PER_REDUCE_JOB = 12_000_000
PLAN_MAX_SCRATCH_BYTES = 12_000_000_000
REDUCE_MAX_OPEN_FRAGMENT_FILES = 1
REDUCE_MAX_BUFFER_ROWS = 10_000
REDUCE_MAX_LEAF_INPUT_BYTES = 2_000_000_000
REDUCE_MAX_SCRATCH_BYTES = 12_000_000_000
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
        descriptor, name = tempfile.mkstemp(
            prefix="places-count-fanin-", suffix=".sqlite3", dir=scratch_dir
        )
        os.close(descriptor)
        self.path = Path(name)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute("PRAGMA temp_store=FILE")
        self.connection.execute("PRAGMA cache_size=-65536")
        self.page_size = self.connection.execute("PRAGMA page_size").fetchone()[0]
        requested_max_pages = PLAN_MAX_SCRATCH_BYTES // self.page_size
        self.maximum_page_count = self.connection.execute(
            f"PRAGMA max_page_count={requested_max_pages}"
        ).fetchone()[0]
        if self.maximum_page_count > requested_max_pages:
            raise ValueError("Places planner SQLite page cap was not enforced")
        self.connection.execute(
            "CREATE TABLE counts (cell TEXT PRIMARY KEY, records INTEGER NOT NULL)"
        )
        self.peak_scratch_bytes = 0
        self.peak_database_pages = 0
        self.peak_database_bytes = 0
        self.observe_scratch()

    def observe_scratch(self) -> int:
        current = sum(
            path.stat().st_size
            for path in self.scratch_dir.rglob("*")
            if path.is_file()
        )
        pages = self.connection.execute("PRAGMA page_count").fetchone()[0]
        database_bytes = pages * self.page_size
        self.peak_scratch_bytes = max(self.peak_scratch_bytes, current)
        self.peak_database_pages = max(self.peak_database_pages, pages)
        self.peak_database_bytes = max(self.peak_database_bytes, database_bytes)
        if (
            current > PLAN_MAX_SCRATCH_BYTES
            or pages > self.maximum_page_count
            or database_bytes > PLAN_MAX_SCRATCH_BYTES
        ):
            raise ValueError("Places planner count store exceeded its hard scratch cap")
        return current

    def evidence(self) -> dict[str, Any]:
        self.observe_scratch()
        uses_temporary_btree = self.ordered_query_uses_temporary_btree()
        if uses_temporary_btree:
            raise ValueError("Places planner ordered count scan uses temporary storage")
        return {
            "kind": "sqlite-count-fanin-v1",
            "maximum_scratch_bytes": PLAN_MAX_SCRATCH_BYTES,
            "sqlite_page_size": self.page_size,
            "sqlite_maximum_page_count": self.maximum_page_count,
            "peak_sqlite_page_count": self.peak_database_pages,
            "peak_sqlite_database_bytes": self.peak_database_bytes,
            "peak_scratch_bytes": self.peak_scratch_bytes,
            "group_aggregation": "indexed-cell-stream-v1",
            "maximum_execution_groups_in_memory": MAX_EXECUTION_GROUPS_IN_MEMORY,
            "ordered_scan_uses_temporary_btree": uses_temporary_btree,
        }

    def add_artifact(
        self,
        path: Path,
        *,
        maximum_level: int,
        inventory_sha256: str,
        task_digest: str,
    ) -> tuple[int, int]:
        cells = records = 0
        previous: str | None = None
        with gzip.open(path, "rt", encoding="utf-8") as source:
            try:
                header = json.loads(next(source))
            except StopIteration as exc:
                raise ValueError("Places count artifact is empty") from exc
            require_exact(
                header,
                {
                    "schema",
                    "maximum_level",
                    "inventory_sha256",
                    "map_task_digest",
                    "task_identity_is_serving_identity",
                },
                "Places count header",
            )
            if header != {
                "schema": COUNT_ARTIFACT_SCHEMA,
                "maximum_level": maximum_level,
                "inventory_sha256": inventory_sha256,
                "map_task_digest": task_digest,
                "task_identity_is_serving_identity": False,
            }:
                raise ValueError(
                    "Places count artifact provenance differs from its task"
                )
            pending: list[tuple[str, int]] = []
            for line in source:
                value = json.loads(line)
                require_exact(value, {"cell", "records"}, "Places count row")
                cell = value["cell"]
                count = value["records"]
                if (
                    not isinstance(cell, str)
                    or len(cell) != maximum_level
                    or any(digit not in "0123" for digit in cell)
                    or previous is not None
                    and cell <= previous
                ):
                    raise ValueError(
                        "Places maximum-level counts are invalid or unordered"
                    )
                require_int(count, f"count for cell {cell}", minimum=1)
                pending.append((cell, count))
                if len(pending) >= 10_000:
                    self._add(pending)
                    pending = []
                previous = cell
                cells += 1
                records += count
            self._add(pending)
        return cells, records

    def _add(self, values: list[tuple[str, int]]) -> None:
        try:
            self.connection.executemany(
                """
                INSERT INTO counts(cell, records) VALUES (?, ?)
                ON CONFLICT(cell) DO UPDATE SET records = records + excluded.records
                """,
                values,
            )
        except sqlite3.OperationalError as exc:
            raise ValueError(
                "Places planner count store exceeded its hard scratch cap"
            ) from exc
        self.observe_scratch()

    def finish(self) -> None:
        try:
            self.connection.commit()
        except sqlite3.OperationalError as exc:
            raise ValueError(
                "Places planner count store exceeded its hard scratch cap"
            ) from exc
        self.observe_scratch()

    def ordered(self) -> Iterator[tuple[str, int]]:
        yield from self.connection.execute(self.ORDERED_QUERY)

    def ordered_query_plan(self) -> tuple[str, ...]:
        return tuple(
            row[3]
            for row in self.connection.execute(
                f"EXPLAIN QUERY PLAN {self.ORDERED_QUERY}"
            )
        )

    def ordered_query_uses_temporary_btree(self) -> bool:
        return any(
            "TEMP B-TREE" in detail.upper() for detail in self.ordered_query_plan()
        )

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
            "counts",
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
            "fragment_rows_limit",
            "fragment_input_bytes_limit",
            "fragment_bytes_limit",
            "task_fragment_count_limit",
        },
        "Places map execution",
    )
    if (
        execution["task_index"] != task["index"]
        or execution["task_digest"] != task["task_digest"]
        or execution["source_digest"] != task["source_digest"]
        or execution["task_identity_is_serving_identity"] is not False
        or execution["fragment_grouping_is_final_shard_identity"] is not False
        or execution["fragment_grouping"] != "level-4-world-quadkey-execution-v1"
        or execution["execution_group_level"] != EXECUTION_GROUP_LEVEL
        or execution["maximum_execution_groups"] != 1 << (2 * EXECUTION_GROUP_LEVEL)
    ):
        raise ValueError("Places map execution identity/grouping differs from its task")
    for field in (
        "execution_group_count",
        "fragment_rows_limit",
        "fragment_input_bytes_limit",
        "fragment_bytes_limit",
        "task_fragment_count_limit",
    ):
        require_int(execution[field], f"Places map {field}", minimum=0)
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
    counts = require_exact(
        report["counts"],
        {"object_key", "sha256", "bytes", "cells", "records", "maximum_level"},
        "Places count artifact",
    )
    if counts["maximum_level"] != requested_partition["maximum_level"]:
        raise ValueError("Places count maximum level differs from the request")
    count_path = verify_artifact(
        artifact_root, counts["object_key"], counts["bytes"], counts["sha256"]
    )
    verify_listed_artifact(
        artifact_listing,
        counts["object_key"],
        counts["bytes"],
        counts["sha256"],
    )
    actual_cells, actual_count_records = count_store.add_artifact(
        count_path,
        maximum_level=requested_partition["maximum_level"],
        inventory_sha256=inventory["inventory_sha256"],
        task_digest=task["task_digest"],
    )
    if (
        counts["cells"] != actual_cells
        or counts["records"] != actual_count_records
        or counts["records"] != accounting["retained_records"]
    ):
        raise ValueError("Places count artifact does not reconcile to retained records")
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
    if len(objects) > execution["task_fragment_count_limit"]:
        raise ValueError("Places map report exceeded its declared fragment cap")
    normalized_fragments: list[dict[str, Any]] = []
    groups: set[str] = set()
    for fragment in objects:
        item = require_exact(
            fragment,
            {
                "execution_group",
                "execution_group_level",
                "minimum_maximum_level_cell",
                "maximum_maximum_level_cell",
                "maximum_level_cells",
                "object_key",
                "sha256",
                "bytes",
                "records",
                "minimum_sort_key",
                "maximum_sort_key",
            },
            "Places fragment",
        )
        group = item["execution_group"]
        if (
            not isinstance(group, str)
            or len(group) != EXECUTION_GROUP_LEVEL
            or any(digit not in "0123" for digit in group)
            or item["execution_group_level"] != EXECUTION_GROUP_LEVEL
            or not isinstance(item["minimum_maximum_level_cell"], str)
            or not isinstance(item["maximum_maximum_level_cell"], str)
            or not item["minimum_maximum_level_cell"].startswith(group)
            or not item["maximum_maximum_level_cell"].startswith(group)
            or item["minimum_maximum_level_cell"] > item["maximum_maximum_level_cell"]
        ):
            raise ValueError("Places fragment execution-group/cell range is invalid")
        require_int(
            item["maximum_level_cells"], "fragment maximum-level cells", minimum=1
        )
        require_int(item["records"], "fragment records", minimum=1)
        require_int(item["bytes"], "fragment bytes", minimum=1)
        if item["bytes"] > execution["fragment_bytes_limit"]:
            raise ValueError("Places fragment exceeds its declared byte cap")
        require_sha256(item["sha256"], "Places fragment sha256")
        expected_suffix = f"group={group}/sha256/{item['sha256']}.parquet"
        if not item["object_key"].endswith(expected_suffix):
            raise ValueError(
                "Places fragment key differs from its content/group identity"
            )
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
        normalized_fragments.append(
            {**item, "map_index": task["index"], "map_task_digest": task["task_digest"]}
        )
        groups.add(group)
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
        "count_object_key": counts["object_key"],
        "count_sha256": counts["sha256"],
        "fragments": normalized_fragments,
    }


def _load_predecessor_splits(
    request: dict[str, Any],
    predecessor_family_manifest: dict[str, Any] | None,
    predecessor_catalog: Path | None,
) -> list[str]:
    places = request["families"]["places"]
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
        fragments = sorted(
            (
                fragment
                for group in execution_groups
                for fragment in groups[group]["fragments"]
            ),
            key=lambda item: (
                item["execution_group"],
                item["map_index"],
                item["minimum_sort_key"],
                item["sha256"],
            ),
        )
        input_bytes = sum(item["bytes"] for item in fragments)
        if len(fragments) > MAX_INPUT_FRAGMENTS_PER_REDUCE_JOB:
            raise ValueError("Places reduce job exceeds its fragment-count cap")
        if input_bytes > MAX_INPUT_BYTES_PER_REDUCE_JOB:
            raise ValueError("Places reduce job exceeds its input-byte cap")
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
            "fragment_sha256s": [item["sha256"] for item in fragments],
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
        count_keys: set[str] = set()
        count_digests: set[str] = set()
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
                normalized["count_object_key"] in count_keys
                or normalized["count_sha256"] in count_digests
            ):
                raise ValueError("replayed Places count artifact identity")
            report_digests.add(normalized["report_sha256"])
            count_keys.add(normalized["count_object_key"])
            count_digests.add(normalized["count_sha256"])
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
            expected_artifact_keys = fragment_keys | count_keys
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
                group = fragment["execution_group"]
                fragments_by_group[group].append(fragment)
                fragment_records_by_group[group] += fragment["records"]
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
        reduce_jobs = _assign_reduce_jobs(
            groups=groups,
            reduce_job_limit=request["execution"]["reduce_job_limit"],
            request_digest=request_digest,
            inventory_sha256=inventory["inventory_sha256"],
            completion_set_sha256=completion_set_sha256,
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
                "count_aggregation": count_store.evidence(),
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
                "max_retained_rows_per_reduce_job": MAX_RETAINED_ROWS_PER_REDUCE_JOB,
                "plan_max_scratch_bytes": PLAN_MAX_SCRATCH_BYTES,
                "reduce_max_open_fragment_files": REDUCE_MAX_OPEN_FRAGMENT_FILES,
                "reduce_max_buffer_rows": REDUCE_MAX_BUFFER_ROWS,
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
        "max_retained_rows_per_reduce_job": MAX_RETAINED_ROWS_PER_REDUCE_JOB,
        "plan_max_scratch_bytes": PLAN_MAX_SCRATCH_BYTES,
        "reduce_max_open_fragment_files": REDUCE_MAX_OPEN_FRAGMENT_FILES,
        "reduce_max_buffer_rows": REDUCE_MAX_BUFFER_ROWS,
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
            "count_aggregation",
        },
        "Places map fan-in",
    )
    count_aggregation = require_exact(
        map_fan_in["count_aggregation"],
        {
            "kind",
            "maximum_scratch_bytes",
            "sqlite_page_size",
            "sqlite_maximum_page_count",
            "peak_sqlite_page_count",
            "peak_sqlite_database_bytes",
            "peak_scratch_bytes",
            "group_aggregation",
            "maximum_execution_groups_in_memory",
            "ordered_scan_uses_temporary_btree",
        },
        "Places count aggregation evidence",
    )
    page_size = count_aggregation.get("sqlite_page_size")
    maximum_pages = count_aggregation.get("sqlite_maximum_page_count")
    peak_pages = count_aggregation.get("peak_sqlite_page_count")
    peak_database_bytes = count_aggregation.get("peak_sqlite_database_bytes")
    peak_scratch = count_aggregation.get("peak_scratch_bytes")
    if (
        count_aggregation.get("kind") != "sqlite-count-fanin-v1"
        or count_aggregation.get("maximum_scratch_bytes") != PLAN_MAX_SCRATCH_BYTES
        or type(page_size) is not int
        or page_size < 512
        or type(maximum_pages) is not int
        or maximum_pages > PLAN_MAX_SCRATCH_BYTES // page_size
        or type(peak_pages) is not int
        or not 0 <= peak_pages <= maximum_pages
        or peak_database_bytes != peak_pages * page_size
        or peak_database_bytes > PLAN_MAX_SCRATCH_BYTES
        or type(peak_scratch) is not int
        or not peak_database_bytes <= peak_scratch <= PLAN_MAX_SCRATCH_BYTES
        or count_aggregation.get("group_aggregation") != "indexed-cell-stream-v1"
        or count_aggregation.get("maximum_execution_groups_in_memory")
        != MAX_EXECUTION_GROUPS_IN_MEMORY
        or count_aggregation.get("ordered_scan_uses_temporary_btree") is not False
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
            if not isinstance(object_key, str) or object_key in observed_fragments:
                raise ValueError("Places reduce input fragment is duplicate or invalid")
            if fragment.get("execution_group") not in groups:
                raise ValueError(
                    "Places reduce fragment is assigned to the wrong group"
                )
            require_int(fragment.get("bytes"), "fragment bytes", minimum=1)
            require_sha256(fragment.get("sha256"), "fragment sha256")
            observed_fragments.add(object_key)
            fragment_rows += require_int(
                fragment.get("records"), "fragment records", minimum=1
            )
        if (
            job.get("expected_records") != job_rows
            or fragment_rows != job_rows
            or job.get("input_fragment_count") != len(fragments)
            or job.get("input_bytes") != sum(item["bytes"] for item in fragments)
        ):
            raise ValueError("Places reduce job accounting does not reconcile")
        fragment_bytes = sum(item["bytes"] for item in fragments)
        group_fanin = Counter(item["execution_group"] for item in fragments)
        leaf_groups = {item["cell"][:EXECUTION_GROUP_LEVEL] for item in job_leaves}
        fragment_groups = set(group_fanin)
        if (
            len(fragments) > MAX_INPUT_FRAGMENTS_PER_REDUCE_JOB
            or fragment_bytes > MAX_INPUT_BYTES_PER_REDUCE_JOB
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
            "fragment_sha256s": [item["sha256"] for item in fragments],
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
        e2_keys += family == "e2"
        entry_cursor += length
        previous_key = key
    artifact_source.close()
    if (
        decoded_keys != key_count
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
