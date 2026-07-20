#!/usr/bin/env python3
"""Execute one replaceable Places reduce job into Worker-readable ``.pcsh`` leaves.

Input fragments are opened and verified one at a time, then compacted into a
disk-backed serving-order store. The reduce job identity is deliberately not a
serving identity: each output remains the stable ``q-<world-quadkey>.pcsh``
owned by the partition plan.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_places_region_shards import _route  # noqa: E402
from experiment_places_compact_index import place_from_row, tokens  # noqa: E402
from experiment_places_compact_shard import (  # noqa: E402
    MAGIC,
    TOKENIZER_VERSION,
    build_artifact,
    encode_projection,
)
from global_v2_places_plan import (  # noqa: E402
    REQUIRED_PYARROW_VERSION,
    REQUIRED_PYTHON_VERSION,
    REDUCE_MAX_BUFFER_ROWS,
    REDUCE_MAX_LEAF_INPUT_BYTES,
    REDUCE_MAX_LEAF_PROJECTION_BYTES,
    REDUCE_MAX_LEAF_ROWS_IN_MEMORY,
    REDUCE_MAX_LEAF_TOKEN_OCCURRENCES,
    REDUCE_MAX_OPEN_FRAGMENT_FILES,
    REDUCE_MAX_SCRATCH_BYTES,
    REDUCE_MAX_WORKSPACE_BYTES,
    canonical_json_bytes,
    digest_value,
    require_exact,
    require_int,
    require_sha256,
    safe_artifact_path,
    sha256_file,
    validate_places_plan,
)
from places_partition import morton_quadkey, point_morton  # noqa: E402


REDUCE_REPORT_SCHEMA = "overture-global-v2-places-reduce-report-v1"
REDUCE_VERSION = "1"


FragmentReader = Callable[[dict[str, Any], Path], Iterator[dict[str, Any]]]


def validate_fetch_command(argv: Any) -> list[str] | None:
    if argv is not None and (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
        or sum(item.count("{output}") for item in argv) != 1
        or sum(item.count("{object_key}") for item in argv) < 1
    ):
        raise ValueError(
            "Places fragment fetch command must be non-empty argv with "
            "{object_key} and one {output}"
        )
    return argv


def parse_fetch_command(value: str | None) -> list[str] | None:
    """Parse the no-shell argv adapter for one-object-at-a-time fetches."""

    if value is None:
        return None
    try:
        argv = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Places fragment fetch command must be a JSON argv array"
        ) from exc
    return validate_fetch_command(argv)


class _ArtifactMaterializer:
    def __init__(
        self,
        *,
        artifact_root: Path,
        temporary_dir: Path,
        fetch_command: list[str] | None,
        observe_workspace: Callable[[int], None],
    ) -> None:
        self.artifact_root = artifact_root
        self.temporary_dir = temporary_dir
        self.fetch_command = fetch_command
        self.observe_workspace = observe_workspace
        self.fetched_fragments = 0
        self.fetched_bytes = 0
        self.peak_materialized_fragment_bytes = 0

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink(missing_ok=True)

    @contextlib.contextmanager
    def path(self, fragment: dict[str, Any]) -> Iterator[Path]:
        local = safe_artifact_path(self.artifact_root, fragment["object_key"])
        temporary: Path | None = None
        if not local.is_file():
            if self.fetch_command is None:
                raise ValueError(
                    f"missing Places reduce fragment: {fragment['object_key']}"
                )
            self.observe_workspace(fragment["bytes"])
            self.temporary_dir.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix="places-reduce-fragment-",
                suffix=".parquet",
                dir=self.temporary_dir,
            )
            os.close(descriptor)
            temporary = Path(name)
            temporary.unlink()
            argv = [
                item.replace("{object_key}", fragment["object_key"]).replace(
                    "{output}", str(temporary)
                )
                for item in self.fetch_command
            ]
            try:
                subprocess.run(argv, check=True)
            except (OSError, subprocess.CalledProcessError) as exc:
                self._remove(temporary)
                raise ValueError("Places fragment fetch adapter failed") from exc
            if not temporary.is_file():
                self._remove(temporary)
                raise ValueError("Places fragment fetch adapter produced no file")
            local = temporary
            self.observe_workspace(local.stat().st_size)
        try:
            actual_sha256, actual_bytes = sha256_file(local)
            if (actual_bytes, actual_sha256) != (
                fragment["bytes"],
                fragment["sha256"],
            ):
                raise ValueError(
                    f"Places reduce fragment identity mismatch: {fragment['object_key']}"
                )
            if temporary is not None:
                self.fetched_fragments += 1
                self.fetched_bytes += actual_bytes
                self.peak_materialized_fragment_bytes = max(
                    self.peak_materialized_fragment_bytes, actual_bytes
                )
            yield local
        finally:
            if temporary is not None:
                self._remove(temporary)
                self.observe_workspace(0)


class PyArrowFragmentReader:
    def __init__(self) -> None:
        actual_python = platform.python_version()
        if actual_python != REQUIRED_PYTHON_VERSION:
            raise RuntimeError(
                "Places production executor requires Python "
                f"{REQUIRED_PYTHON_VERSION}; found {actual_python}"
            )
        try:
            import pyarrow
            import pyarrow.parquet as pq
        except ImportError as exc:  # pragma: no cover - hosted dependency boundary
            raise RuntimeError("Places reduce input requires pyarrow") from exc
        if pyarrow.__version__ != REQUIRED_PYARROW_VERSION:
            raise RuntimeError(
                "Places production executor requires PyArrow "
                f"{REQUIRED_PYARROW_VERSION}; found {pyarrow.__version__}"
            )
        self.pyarrow = pyarrow
        self.pq = pq
        self.created_by: set[str] = set()

    def __call__(
        self, _fragment: dict[str, Any], path: Path
    ) -> Iterator[dict[str, Any]]:
        parquet = self.pq.ParquetFile(path)
        created_by = parquet.metadata.created_by
        self.created_by.add(created_by or "(missing-created-by)")
        for batch in parquet.iter_batches(batch_size=16_384, use_threads=False):
            for row in batch.to_pylist():
                if not isinstance(row, dict):
                    raise ValueError("Places fragment row must be an object")
                yield row

    def provenance(self) -> dict[str, Any]:
        if len(self.created_by) != 1:
            raise ValueError(
                "Places input fragments do not share one pinned Parquet writer runtime"
            )
        created_by = next(iter(self.created_by))
        if f"version {REQUIRED_PYARROW_VERSION}" not in created_by:
            raise ValueError(
                "Places fragments were not written by the pinned PyArrow/Parquet "
                f"runtime {REQUIRED_PYARROW_VERSION}: {created_by}"
            )
        return {
            "reader": "pyarrow-parquet-iter-batches-v1",
            "pyarrow_version": self.pyarrow.__version__,
            "parquet_created_by": created_by,
        }


class _LeafStore:
    def __init__(
        self,
        scratch_dir: Path,
        workspace_observer: Callable[[int], None] | None = None,
    ) -> None:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        self.scratch_dir = scratch_dir
        descriptor, name = tempfile.mkstemp(
            prefix="places-reduce-", suffix=".sqlite3", dir=scratch_dir
        )
        os.close(descriptor)
        self.path = Path(name)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute("PRAGMA temp_store=FILE")
        self.connection.execute("PRAGMA cache_size=-65536")
        self.page_size = self.connection.execute("PRAGMA page_size").fetchone()[0]
        self.connection.execute(
            f"PRAGMA max_page_count={REDUCE_MAX_SCRATCH_BYTES // self.page_size}"
        )
        self.connection.execute(
            """
            CREATE TABLE rows (
                leaf TEXT NOT NULL,
                partition_cell TEXT NOT NULL,
                partition_key INTEGER NOT NULL,
                confidence_rank INTEGER NOT NULL,
                gers_id TEXT NOT NULL,
                source_uri TEXT NOT NULL,
                source_row_group INTEGER NOT NULL,
                source_row_index INTEGER NOT NULL,
                payload BLOB NOT NULL,
                UNIQUE(source_uri, source_row_group, source_row_index)
            )
            """
        )
        self.pending: list[tuple[Any, ...]] = []
        self.workspace_observer = workspace_observer
        self.peak_scratch_bytes = 0
        self.observe_scratch()

    def scratch_bytes(self) -> int:
        return sum(
            path.stat().st_size
            for path in self.scratch_dir.rglob("*")
            if path.is_file()
        )

    def observe_scratch(self) -> int:
        physical = self.scratch_bytes()
        database_file_bytes = self.path.stat().st_size
        database_bytes = (
            self.connection.execute("PRAGMA page_count").fetchone()[0] * self.page_size
        )
        current = physical - database_file_bytes + max(
            database_file_bytes, database_bytes
        )
        self.peak_scratch_bytes = max(self.peak_scratch_bytes, current)
        if current > REDUCE_MAX_SCRATCH_BYTES:
            raise ValueError(
                "Places reducer actual scratch usage exceeded its hard cap: "
                f"observed={current}, cap={REDUCE_MAX_SCRATCH_BYTES}"
            )
        if self.workspace_observer is not None:
            self.workspace_observer(current)
        return current

    def add(self, leaf: str, row: dict[str, Any]) -> None:
        self.pending.append(
            (
                leaf,
                row["partition_cell"],
                row["partition_key"],
                -round(row["confidence"] * 255),
                row["gers_id"],
                row["source_uri"],
                row["source_row_group"],
                row["source_row_index"],
                canonical_json_bytes(row),
            )
        )
        if len(self.pending) >= REDUCE_MAX_BUFFER_ROWS:
            self.flush()

    def flush(self) -> None:
        if not self.pending:
            return
        try:
            self.connection.executemany(
                "INSERT INTO rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", self.pending
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("duplicate/replayed Places source row identity") from exc
        except sqlite3.OperationalError as exc:
            raise ValueError(
                "Places reducer SQLite store exceeded its hard scratch cap"
            ) from exc
        self.pending = []
        self.observe_scratch()

    def finish(self) -> None:
        self.flush()
        self.connection.commit()
        try:
            self.connection.execute(
                """
                CREATE INDEX rows_serving_order ON rows (
                    leaf,
                    partition_cell,
                    partition_key,
                    confidence_rank,
                    gers_id,
                    source_uri,
                    source_row_group,
                    source_row_index
                )
                """
            )
        except sqlite3.OperationalError as exc:
            raise ValueError(
                "Places reducer SQLite index exceeded its hard scratch cap"
            ) from exc
        self.connection.commit()
        self.observe_scratch()

    def leaf_stats(self) -> dict[str, tuple[int, int]]:
        return {
            leaf: (records, payload_bytes)
            for leaf, records, payload_bytes in self.connection.execute(
                """
                SELECT leaf, count(*), sum(length(payload))
                FROM rows GROUP BY leaf ORDER BY leaf
                """
            )
        }

    def iter_leaf(self, leaf: str) -> Iterator[dict[str, Any]]:
        for (payload,) in self.connection.execute(
            """
            SELECT payload FROM rows WHERE leaf = ?
            ORDER BY
                partition_cell,
                partition_key,
                confidence_rank,
                gers_id,
                source_uri,
                source_row_group,
                source_row_index
            """,
            (leaf,),
        ):
            value = json.loads(payload)
            if not isinstance(value, dict):
                raise AssertionError("stored Places reduce row is not an object")
            yield value

    def close(self) -> None:
        self.connection.close()
        self.path.unlink(missing_ok=True)


def _leaf_for(cell: str, leaves: set[str], minimum_level: int) -> str:
    for length in range(minimum_level, len(cell) + 1):
        candidate = cell[:length]
        if candidate in leaves:
            return candidate
    raise ValueError(f"Places row cell {cell} has no owner in its reduce job")


def _validate_fragment_row(
    row: Any,
    *,
    fragment: dict[str, Any],
    maximum_level: int,
    leaves: set[str],
    minimum_level: int,
) -> tuple[str, dict[str, Any]]:
    required = {
        "gers_id",
        "primary_name",
        "alt_names",
        "brand_name",
        "category_primary",
        "basic_category",
        "locality",
        "region",
        "country",
        "lat",
        "lon",
        "confidence",
        "operating_status",
        "partition_key",
        "partition_cell",
        "execution_group",
        "source_uri",
        "source_row_group",
        "source_row_index",
    }
    value = require_exact(row, required, "Places normalized fragment row")
    cell = value["partition_cell"]
    group = fragment["execution_group"]
    if (
        not isinstance(cell, str)
        or len(cell) != maximum_level
        or any(digit not in "0123" for digit in cell)
        or not cell.startswith(group)
        or value["execution_group"] != group
        or not fragment["minimum_maximum_level_cell"]
        <= cell
        <= fragment["maximum_maximum_level_cell"]
    ):
        raise ValueError("Places fragment row differs from its group/cell provenance")
    partition_key = require_int(value["partition_key"], "Places partition key")
    if morton_quadkey(partition_key, maximum_level) != cell:
        raise ValueError("Places fragment row partition key differs from its cell")
    longitude = value["lon"]
    latitude = value["lat"]
    if (
        isinstance(longitude, bool)
        or isinstance(latitude, bool)
        or not isinstance(longitude, (int, float))
        or not isinstance(latitude, (int, float))
        or not math.isfinite(longitude)
        or not math.isfinite(latitude)
        or point_morton(float(longitude), float(latitude), maximum_level)
        != partition_key
    ):
        raise ValueError(
            "Places fragment row coordinates differ from its partition key"
        )
    try:
        identifier = str(uuid.UUID(value["gers_id"]))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("Places normalized row has an invalid UUID identity") from exc
    if identifier != value["gers_id"]:
        raise ValueError("Places normalized row UUID identity is not canonical")
    if not isinstance(value["primary_name"], str) or not value["primary_name"]:
        raise ValueError("Places normalized row has no primary name")
    for field in ("source_row_group", "source_row_index"):
        require_int(value[field], f"Places {field}")
    leaf = _leaf_for(cell, leaves, minimum_level)
    return leaf, value


def _module_sha256(name: str) -> str:
    return sha256_file(SCRIPT_DIR / name)[0]


def execute_reduce_job(
    plan_value: Any,
    *,
    job_index: int,
    artifact_root: Path,
    scratch_dir: Path,
    output_dir: Path,
    fragment_fetch_command: list[str] | None = None,
    fragment_reader: FragmentReader | None = None,
    runtime_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = validate_places_plan(plan_value)
    fragment_fetch_command = validate_fetch_command(fragment_fetch_command)
    require_int(job_index, "Places reduce job index")
    if not 0 <= job_index < len(plan["reduce_jobs"]):
        raise ValueError("Places reduce job index is outside the executor plan")
    job = plan["reduce_jobs"][job_index]
    output_dir.mkdir(parents=True, exist_ok=True)
    if fragment_reader is None:
        default_reader = PyArrowFragmentReader()
        fragment_reader = default_reader
    else:
        default_reader = None
        if runtime_provenance is None:
            raise ValueError(
                "injected Places fragment reader requires runtime provenance"
            )
    leaves = {leaf["cell"]: leaf["rows"] for leaf in job["leaves"]}
    input_records = 0
    group_fanin: dict[str, int] = {}
    produced_output_bytes = 0
    active_materialized_fragment_bytes = 0
    peak_workspace_bytes = 0

    def observe_combined_workspace(
        scratch_bytes: int, additional_bytes: int = 0
    ) -> None:
        nonlocal peak_workspace_bytes
        current_workspace_bytes = (
            scratch_bytes
            + produced_output_bytes
            + active_materialized_fragment_bytes
            + additional_bytes
        )
        peak_workspace_bytes = max(
            peak_workspace_bytes,
            current_workspace_bytes,
        )
        if peak_workspace_bytes > REDUCE_MAX_WORKSPACE_BYTES:
            raise ValueError(
                "Places reducer actual workspace usage exceeded its hard cap: "
                f"observed={peak_workspace_bytes}, cap={REDUCE_MAX_WORKSPACE_BYTES}"
            )

    def preflight_workspace(scratch_bytes: int, additional_bytes: int) -> None:
        projected = (
            scratch_bytes
            + produced_output_bytes
            + active_materialized_fragment_bytes
            + additional_bytes
        )
        if projected > REDUCE_MAX_WORKSPACE_BYTES:
            raise ValueError(
                "Places reducer conservative output bound exceeded its workspace "
                f"cap: projected={projected}, cap={REDUCE_MAX_WORKSPACE_BYTES}"
            )

    store = _LeafStore(
        scratch_dir / "store", workspace_observer=observe_combined_workspace
    )

    def observe_workspace(materialized_fragment_bytes: int) -> None:
        nonlocal active_materialized_fragment_bytes
        active_materialized_fragment_bytes = materialized_fragment_bytes
        store.observe_scratch()

    observe_workspace(0)
    materializer = _ArtifactMaterializer(
        artifact_root=artifact_root,
        temporary_dir=scratch_dir / "fragments",
        fetch_command=fragment_fetch_command,
        observe_workspace=observe_workspace,
    )
    try:
        for fragment in job["input_fragments"]:
            group = fragment["execution_group"]
            group_fanin[group] = group_fanin.get(group, 0) + 1
            actual_fragment_records = 0
            with materializer.path(fragment) as path:
                for raw_row in fragment_reader(fragment, path):
                    leaf, row = _validate_fragment_row(
                        raw_row,
                        fragment=fragment,
                        maximum_level=plan["partition"]["maximum_level"],
                        leaves=set(leaves),
                        minimum_level=plan["partition"]["minimum_level"],
                    )
                    store.add(leaf, row)
                    actual_fragment_records += 1
                    input_records += 1
            if actual_fragment_records != fragment["records"]:
                raise ValueError(
                    f"Places fragment record count differs: {fragment['object_key']}"
                )
            observe_workspace(0)
        if input_records != job["expected_records"]:
            raise ValueError("Places reduce input records differ from its planned job")
        store.finish()
        observe_workspace(0)
        stats = store.leaf_stats()
        if set(stats) != set(leaves):
            raise ValueError(
                "Places compacted leaf set differs from its planned ownership"
            )
        if any(stats[cell][0] != rows for cell, rows in leaves.items()):
            raise ValueError(
                "Places compacted leaf rows differ from exact planned counts"
            )
        if any(
            payload_bytes > REDUCE_MAX_LEAF_INPUT_BYTES
            for _, payload_bytes in stats.values()
        ):
            raise ValueError(
                "Places leaf exceeds the reducer's normalized input byte cap"
            )
        shards: list[dict[str, Any]] = []
        output_records = 0
        peak_writer_rows = 0
        peak_writer_normalized_bytes = 0
        peak_writer_token_occurrences = 0
        peak_writer_projection_bytes = 0
        for leaf in sorted(leaves):
            leaf_rows, leaf_normalized_bytes = stats[leaf]
            if (
                leaf_rows > REDUCE_MAX_LEAF_ROWS_IN_MEMORY
                or leaf_normalized_bytes > REDUCE_MAX_LEAF_INPUT_BYTES
            ):
                raise ValueError("Places leaf exceeds an in-memory writer input cap")
            token_occurrences = 0
            projection_bytes = 0
            for index, row in enumerate(store.iter_leaf(leaf), start=1):
                place = place_from_row(row, index)
                token_occurrences += sum(
                    len(set(tokens(value))) for value in place.field_text().values()
                )
                projection_bytes += len(encode_projection(place))
                if (
                    token_occurrences > REDUCE_MAX_LEAF_TOKEN_OCCURRENCES
                    or projection_bytes > REDUCE_MAX_LEAF_PROJECTION_BYTES
                ):
                    raise ValueError(
                        "Places leaf exceeds an in-memory writer expansion cap"
                    )
            conservative_output_bound = (
                64_000_000
                + leaf_normalized_bytes
                + projection_bytes
                + token_occurrences * 32
                + leaf_rows * 64
            )
            preflight_workspace(store.observe_scratch(), conservative_output_bound)
            places = [
                place_from_row(row, index)
                for index, row in enumerate(store.iter_leaf(leaf), start=1)
            ]
            if len(places) != leaf_rows:
                raise AssertionError("Places writer materialization changed leaf rows")
            peak_writer_rows = max(peak_writer_rows, leaf_rows)
            peak_writer_normalized_bytes = max(
                peak_writer_normalized_bytes, leaf_normalized_bytes
            )
            peak_writer_token_occurrences = max(
                peak_writer_token_occurrences, token_occurrences
            )
            peak_writer_projection_bytes = max(
                peak_writer_projection_bytes, projection_bytes
            )
            route = _route(leaf)
            output_path = output_dir / route["object"]
            ordered, artifact_report = build_artifact(
                places, output_path, preserve_input_order=True
            )
            if (
                len(ordered) != leaves[leaf]
                or artifact_report["places"] != leaves[leaf]
            ):
                raise AssertionError("Places PCSH writer changed leaf row cardinality")
            artifact_sha256, artifact_bytes = sha256_file(output_path)
            produced_output_bytes += artifact_bytes
            observe_workspace(0)
            shards.append(
                {
                    "cell": leaf,
                    "object": route["object"],
                    "rows": leaves[leaf],
                    "bytes": artifact_bytes,
                    "sha256": artifact_sha256,
                    "bbox": route["bbox"],
                    "center": route["center"],
                    "format_version": MAGIC.decode(),
                    "tokenizer_version": TOKENIZER_VERSION,
                }
            )
            output_records += leaves[leaf]
            del places, ordered
        if output_records != job["expected_records"]:
            raise AssertionError(
                "Places final shard rows differ from reduce input rows"
            )
        if default_reader is not None:
            reader_provenance = default_reader.provenance()
        else:
            reader_provenance = runtime_provenance
        without_digest = {
            "schema": REDUCE_REPORT_SCHEMA,
            "version": REDUCE_VERSION,
            "plan_sha256": plan["plan_sha256"],
            "job_index": job_index,
            "job_digest": job["job_digest"],
            "execution_identity_is_serving_identity": False,
            "status": "complete",
            "input_fragment_set_sha256": digest_value(
                [fragment["sha256"] for fragment in job["input_fragments"]]
            ),
            "runtime": {
                "required_python_version": REQUIRED_PYTHON_VERSION,
                "required_pyarrow_version": REQUIRED_PYARROW_VERSION,
                "actual_python_version": platform.python_version(),
                "fragment_reader": reader_provenance,
                "pcsh_writer": {
                    "format": MAGIC.decode(),
                    "tokenizer": TOKENIZER_VERSION,
                    "module_sha256": _module_sha256(
                        "experiment_places_compact_shard.py"
                    ),
                },
                "executor_module_sha256": _module_sha256("global_v2_places_reduce.py"),
            },
            "compaction": {
                "kind": "sqlite-external-serving-order-v1",
                "raw_input_fragments": len(job["input_fragments"]),
                "maximum_raw_group_fanin": max(group_fanin.values(), default=0),
                "compacted_spools_per_leaf": 1,
                "maximum_open_fragment_files": REDUCE_MAX_OPEN_FRAGMENT_FILES,
                "maximum_buffer_rows": REDUCE_MAX_BUFFER_ROWS,
                "maximum_leaf_input_bytes": REDUCE_MAX_LEAF_INPUT_BYTES,
                "maximum_scratch_bytes": REDUCE_MAX_SCRATCH_BYTES,
                "maximum_workspace_bytes": REDUCE_MAX_WORKSPACE_BYTES,
                "fragment_materialization": {
                    "adapter": "local-or-no-shell-argv-v1",
                    "remote_fetch_enabled": fragment_fetch_command is not None,
                    "fetched_fragments": materializer.fetched_fragments,
                    "fetched_bytes": materializer.fetched_bytes,
                    "maximum_simultaneously_materialized_fragments": 1,
                    "peak_materialized_fragment_bytes": (
                        materializer.peak_materialized_fragment_bytes
                    ),
                    "identity_verification": "exact-plan-bytes-and-sha256",
                },
                "writer_materialization": {
                    "kind": "single-place-list-required-by-pcsh-writer-v1",
                    "measurement": "deterministic-row-token-projection-bounds-v1",
                    "maximum_leaf_rows": REDUCE_MAX_LEAF_ROWS_IN_MEMORY,
                    "maximum_leaf_normalized_bytes": REDUCE_MAX_LEAF_INPUT_BYTES,
                    "maximum_leaf_token_occurrences": (
                        REDUCE_MAX_LEAF_TOKEN_OCCURRENCES
                    ),
                    "maximum_leaf_projection_bytes": (REDUCE_MAX_LEAF_PROJECTION_BYTES),
                    "peak_leaf_rows": peak_writer_rows,
                    "peak_leaf_normalized_bytes": peak_writer_normalized_bytes,
                    "peak_leaf_token_occurrences": peak_writer_token_occurrences,
                    "peak_leaf_projection_bytes": peak_writer_projection_bytes,
                },
                "peak_scratch_bytes": store.peak_scratch_bytes,
                "peak_workspace_bytes": peak_workspace_bytes,
            },
            "accounting": {
                "expected_records": job["expected_records"],
                "input_fragment_records": input_records,
                "compacted_records": sum(records for records, _ in stats.values()),
                "output_records": output_records,
                "leaves": len(shards),
            },
            "shards": shards,
        }
        return {
            **without_digest,
            "report_sha256": digest_value(without_digest),
        }
    finally:
        store.close()


def validate_reduce_report(value: Any, plan_value: Any) -> dict[str, Any]:
    plan = validate_places_plan(plan_value)
    report = require_exact(
        value,
        {
            "schema",
            "version",
            "plan_sha256",
            "job_index",
            "job_digest",
            "execution_identity_is_serving_identity",
            "status",
            "input_fragment_set_sha256",
            "runtime",
            "compaction",
            "accounting",
            "shards",
            "report_sha256",
        },
        "Places reduce report",
    )
    if report["schema"] != REDUCE_REPORT_SCHEMA or report["version"] != REDUCE_VERSION:
        raise ValueError("Places reduce report schema/version is invalid")
    require_sha256(report["report_sha256"], "Places reduce report_sha256")
    without_digest = {
        key: item for key, item in report.items() if key != "report_sha256"
    }
    if digest_value(without_digest) != report["report_sha256"]:
        raise ValueError("Places reduce report digest differs from its contents")
    index = require_int(report["job_index"], "Places reduce job index")
    if not 0 <= index < len(plan["reduce_jobs"]):
        raise ValueError("Places reduce report job index is unexpected")
    job = plan["reduce_jobs"][index]
    if (
        report["plan_sha256"] != plan["plan_sha256"]
        or report["job_digest"] != job["job_digest"]
        or report["execution_identity_is_serving_identity"] is not False
        or report["status"] != "complete"
        or report["input_fragment_set_sha256"]
        != digest_value([item["sha256"] for item in job["input_fragments"]])
    ):
        raise ValueError("Places reduce report identity differs from its planned job")
    runtime = report["runtime"]
    if (
        not isinstance(runtime, dict)
        or runtime.get("required_python_version") != REQUIRED_PYTHON_VERSION
        or runtime.get("required_pyarrow_version") != REQUIRED_PYARROW_VERSION
        or not isinstance(runtime.get("actual_python_version"), str)
        or not runtime["actual_python_version"]
        or not isinstance(runtime.get("fragment_reader"), dict)
        or not runtime["fragment_reader"]
        or not isinstance(runtime.get("pcsh_writer"), dict)
        or not isinstance(runtime.get("executor_module_sha256"), str)
    ):
        raise ValueError("Places reduce runtime provenance is required")
    compaction = require_exact(
        report["compaction"],
        {
            "kind",
            "raw_input_fragments",
            "maximum_raw_group_fanin",
            "compacted_spools_per_leaf",
            "maximum_open_fragment_files",
            "maximum_buffer_rows",
            "maximum_leaf_input_bytes",
            "maximum_scratch_bytes",
            "maximum_workspace_bytes",
            "fragment_materialization",
            "writer_materialization",
            "peak_scratch_bytes",
            "peak_workspace_bytes",
        },
        "Places reduce compaction",
    )
    expected_group_fanin = max(
        Counter(item["execution_group"] for item in job["input_fragments"]).values(),
        default=0,
    )
    if (
        compaction.get("kind") != "sqlite-external-serving-order-v1"
        or compaction.get("maximum_open_fragment_files")
        != REDUCE_MAX_OPEN_FRAGMENT_FILES
        or compaction.get("maximum_buffer_rows") != REDUCE_MAX_BUFFER_ROWS
        or compaction.get("maximum_leaf_input_bytes") != REDUCE_MAX_LEAF_INPUT_BYTES
        or compaction.get("maximum_scratch_bytes") != REDUCE_MAX_SCRATCH_BYTES
        or compaction.get("maximum_workspace_bytes") != REDUCE_MAX_WORKSPACE_BYTES
        or type(compaction.get("peak_scratch_bytes")) is not int
        or not 0 <= compaction["peak_scratch_bytes"] <= REDUCE_MAX_SCRATCH_BYTES
        or type(compaction.get("peak_workspace_bytes")) is not int
        or compaction["peak_workspace_bytes"] < compaction["peak_scratch_bytes"]
        or compaction["peak_workspace_bytes"] > REDUCE_MAX_WORKSPACE_BYTES
        or compaction.get("compacted_spools_per_leaf") != 1
        or compaction.get("raw_input_fragments") != len(job["input_fragments"])
        or compaction.get("maximum_raw_group_fanin") != expected_group_fanin
    ):
        raise ValueError("Places reduce compaction caps/provenance are invalid")
    materialization = require_exact(
        compaction["fragment_materialization"],
        {
            "adapter",
            "remote_fetch_enabled",
            "fetched_fragments",
            "fetched_bytes",
            "maximum_simultaneously_materialized_fragments",
            "peak_materialized_fragment_bytes",
            "identity_verification",
        },
        "Places fragment materialization evidence",
    )
    fetched_fragments = require_int(
        materialization["fetched_fragments"], "Places fetched fragments"
    )
    fetched_bytes = require_int(
        materialization["fetched_bytes"], "Places fetched fragment bytes"
    )
    peak_materialized = require_int(
        materialization["peak_materialized_fragment_bytes"],
        "Places peak materialized fragment bytes",
    )
    if (
        materialization["adapter"] != "local-or-no-shell-argv-v1"
        or type(materialization["remote_fetch_enabled"]) is not bool
        or materialization["maximum_simultaneously_materialized_fragments"] != 1
        or materialization["identity_verification"] != "exact-plan-bytes-and-sha256"
        or (
            materialization["remote_fetch_enabled"]
            and (
                fetched_fragments != len(job["input_fragments"])
                or fetched_bytes != job["input_bytes"]
                or peak_materialized
                != max(item["bytes"] for item in job["input_fragments"])
            )
        )
        or (
            not materialization["remote_fetch_enabled"]
            and (fetched_fragments != 0 or fetched_bytes != 0 or peak_materialized != 0)
        )
    ):
        raise ValueError("Places fragment materialization evidence is invalid")
    writer = require_exact(
        compaction["writer_materialization"],
        {
            "kind",
            "measurement",
            "maximum_leaf_rows",
            "maximum_leaf_normalized_bytes",
            "maximum_leaf_token_occurrences",
            "maximum_leaf_projection_bytes",
            "peak_leaf_rows",
            "peak_leaf_normalized_bytes",
            "peak_leaf_token_occurrences",
            "peak_leaf_projection_bytes",
        },
        "Places PCSH writer materialization evidence",
    )
    writer_caps = {
        "maximum_leaf_rows": REDUCE_MAX_LEAF_ROWS_IN_MEMORY,
        "maximum_leaf_normalized_bytes": REDUCE_MAX_LEAF_INPUT_BYTES,
        "maximum_leaf_token_occurrences": REDUCE_MAX_LEAF_TOKEN_OCCURRENCES,
        "maximum_leaf_projection_bytes": REDUCE_MAX_LEAF_PROJECTION_BYTES,
    }
    if (
        writer["kind"] != "single-place-list-required-by-pcsh-writer-v1"
        or writer["measurement"] != "deterministic-row-token-projection-bounds-v1"
        or any(writer.get(field) != cap for field, cap in writer_caps.items())
    ):
        raise ValueError("Places PCSH writer materialization caps are invalid")
    for peak_field, cap_field in (
        ("peak_leaf_rows", "maximum_leaf_rows"),
        ("peak_leaf_normalized_bytes", "maximum_leaf_normalized_bytes"),
        ("peak_leaf_token_occurrences", "maximum_leaf_token_occurrences"),
        ("peak_leaf_projection_bytes", "maximum_leaf_projection_bytes"),
    ):
        peak = require_int(writer[peak_field], f"Places {peak_field}")
        if peak > writer[cap_field]:
            raise ValueError("Places PCSH writer materialization peak exceeds its cap")
    if writer["peak_leaf_rows"] != max(item["rows"] for item in job["leaves"]):
        raise ValueError("Places PCSH writer row peak differs from its planned leaves")
    accounting = report["accounting"]
    expected = job["expected_records"]
    if accounting != {
        "expected_records": expected,
        "input_fragment_records": expected,
        "compacted_records": expected,
        "output_records": expected,
        "leaves": len(job["leaves"]),
    }:
        raise ValueError("Places reduce report accounting does not reconcile")
    shards = report["shards"]
    if not isinstance(shards, list) or len(shards) != len(job["leaves"]):
        raise ValueError("Places reduce report shard set differs from its leaves")
    expected_leaves = {leaf["cell"]: leaf["rows"] for leaf in job["leaves"]}
    seen: set[str] = set()
    for shard in shards:
        item = require_exact(
            shard,
            {
                "cell",
                "object",
                "rows",
                "bytes",
                "sha256",
                "bbox",
                "center",
                "format_version",
                "tokenizer_version",
            },
            "Places reduced shard",
        )
        cell = item["cell"]
        route = _route(cell)
        if (
            cell in seen
            or expected_leaves.get(cell) != item["rows"]
            or item["object"] != route["object"]
            or item["bbox"] != route["bbox"]
            or item["center"] != route["center"]
            or item["format_version"] != MAGIC.decode()
            or item["tokenizer_version"] != TOKENIZER_VERSION
        ):
            raise ValueError("Places reduced shard identity/route is invalid")
        require_int(item["bytes"], f"Places shard bytes {cell}", minimum=1)
        require_sha256(item["sha256"], f"Places shard sha256 {cell}")
        seen.add(cell)
    if set(expected_leaves) != seen:
        raise ValueError("Places reduce report is missing planned leaves")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--job-index", type=int, required=True)
    parser.add_argument("--artifacts-root", type=Path, required=True)
    parser.add_argument("--scratch-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--fragment-fetch-command-json")
    args = parser.parse_args()
    report = execute_reduce_job(
        json.loads(args.plan.read_text()),
        job_index=args.job_index,
        artifact_root=args.artifacts_root,
        scratch_dir=args.scratch_dir,
        output_dir=args.output_dir,
        fragment_fetch_command=parse_fetch_command(args.fragment_fetch_command_json),
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "report_sha256": report["report_sha256"],
                "job_index": report["job_index"],
                "records": report["accounting"]["output_records"],
                "shards": len(report["shards"]),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
