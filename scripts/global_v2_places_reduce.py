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
import hashlib
import json
import math
import os
import platform
import shutil
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
from experiment_places_compact_index import (  # noqa: E402
    common_prefix,
    encode_record,
    encode_varint,
    place_from_row,
    tokens,
)
from experiment_places_compact_shard import (  # noqa: E402
    FIELD_BITS,
    MAGIC,
    PREAMBLE,
    RECORD_INDEX,
    TOKENIZER_VERSION,
    encode_projection,
)
from experiment_places_locality_head import HEAD_PREFIX_LENGTHS  # noqa: E402
from experiment_places_head_repack import READER_MAX_HEAD_ENTRY_BYTES  # noqa: E402
from global_v2_places_plan import (  # noqa: E402
    REQUIRED_PYARROW_VERSION,
    REQUIRED_DUCKDB_VERSION,
    REQUIRED_PYTHON_VERSION,
    REDUCE_MAX_BUFFER_ROWS,
    REDUCE_DUCKDB_MEMORY_LIMIT_BYTES,
    REDUCE_MAX_ACTIVE_LEAF_PARTITIONS,
    REDUCE_MAX_LEAF_INPUT_BYTES,
    REDUCE_MAX_LEAF_PROJECTION_BYTES,
    REDUCE_MAX_LEAF_ROWS_IN_MEMORY,
    REDUCE_MAX_LEAF_TOKEN_OCCURRENCES,
    REDUCE_MAX_OPEN_FRAGMENT_FILES,
    REDUCE_MAX_SCRATCH_BYTES,
    REDUCE_MAX_WORKSPACE_BYTES,
    HEAD_CANDIDATE_WRITE_BATCH_ROWS,
    HEAD_CANDIDATE_WRITE_BATCH_BYTES,
    HEAD_CANDIDATE_MAX_ROW_BYTES,
    HEAD_CANDIDATE_MAX_PROJECTION_BYTES_IN_MEMORY,
    HEAD_DUPLICATE_GERS_POLICY,
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

MAX_HEAD_SINGLE_PROJECTION_BYTES = (
    READER_MAX_HEAD_ENTRY_BYTES - len(encode_varint(READER_MAX_HEAD_ENTRY_BYTES))
)

REDUCE_REPORT_SCHEMA = "overture-global-v2-places-reduce-report-v2"
REDUCE_VERSION = "2"
HEAD_CANDIDATE_SCHEMA = "overture-global-v2-places-head-candidates-v1"


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
        self.whole_pack_fetches = 0
        self.selective_pack_fetches = 0
        self.selected_row_groups = 0
        self.selected_compressed_bytes = 0
        self.selected_uncompressed_bytes = 0
        self.maximum_materialized_bytes = 0

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink(missing_ok=True)

    @contextlib.contextmanager
    def path(self, fragment: dict[str, Any]) -> Iterator[tuple[Path, bool]]:
        local = safe_artifact_path(self.artifact_root, fragment["object_key"])
        temporary: Path | None = None
        proof_path: Path | None = None
        selective = fragment["fetch_mode"] == "selective"
        self.selected_row_groups += len(fragment["selected_row_groups"])
        self.selected_compressed_bytes += fragment["selected_compressed_bytes"]
        self.selected_uncompressed_bytes += fragment["selected_uncompressed_bytes"]
        self.maximum_materialized_bytes = max(
            self.maximum_materialized_bytes,
            fragment["maximum_materialized_bytes"],
        )
        if not local.is_file():
            if self.fetch_command is None:
                raise ValueError(
                    f"missing Places reduce fragment: {fragment['object_key']}"
                )
            self.observe_workspace(fragment["maximum_materialized_bytes"])
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
            if selective:
                proof_path = temporary.with_suffix(".proof.json")
                argv.extend(
                    [
                        "--row-groups",
                        json.dumps(fragment["selected_row_groups"], separators=(",", ":")),
                        "--expected-bytes",
                        str(fragment["bytes"]),
                        "--expected-sha256",
                        fragment["sha256"],
                        "--proof",
                        str(proof_path),
                        "--artifact-family",
                        "places",
                    ]
                )
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
            if not selective and (actual_bytes, actual_sha256) != (
                fragment["bytes"], fragment["sha256"]
            ):
                raise ValueError(
                    f"Places reduce fragment identity mismatch: {fragment['object_key']}"
                )
            if selective and temporary is not None:
                if proof_path is None or not proof_path.is_file():
                    raise ValueError("Places selective fetch omitted its proof")
                proof = json.loads(proof_path.read_text())
                _validate_selective_proof(fragment, proof, actual_bytes, actual_sha256)
            if temporary is not None:
                self.fetched_fragments += 1
                self.fetched_bytes += actual_bytes
                if selective:
                    self.selective_pack_fetches += 1
                else:
                    self.whole_pack_fetches += 1
                self.peak_materialized_fragment_bytes = max(
                    self.peak_materialized_fragment_bytes, actual_bytes
                )
            yield local, selective and temporary is not None
        finally:
            if temporary is not None:
                self._remove(temporary)
                if proof_path is not None:
                    self._remove(proof_path)
                self.observe_workspace(0)


def _validate_selective_proof(
    fragment: dict[str, Any], proof: Any, actual_bytes: int, actual_sha256: str
) -> None:
    if not isinstance(proof, dict):
        raise ValueError("Places selective fetch proof is invalid")
    mapping = proof.get("materialized_row_groups")
    expected_mapping = [
        {
            "materialized_index": materialized,
            "original_index": original,
            "records": fragment["row_groups"][original]["records"],
            "compressed_column_bytes": fragment["row_groups"][original]["compressed_bytes"],
        }
        for materialized, original in enumerate(fragment["selected_row_groups"])
    ]
    materialized = proof.get("materialized")
    if (
        proof.get("schema") != "overture-r2-selective-parquet-v1"
        or proof.get("version") != 1
        or proof.get("artifact_family") != "places"
        or proof.get("object_key") != fragment["object_key"]
        or proof.get("whole_object")
        != {"bytes": fragment["bytes"], "sha256": fragment["sha256"], "metadata_verified": True}
        or proof.get("selected_original_row_groups") != fragment["selected_row_groups"]
        or not isinstance(proof.get("source_footer"), dict)
        or proof["source_footer"].get("binding_sha256") != fragment["footer_sha256"]
        or proof["source_footer"].get("serialized_size") != fragment["footer_bytes"]
        or mapping != expected_mapping
        or not isinstance(materialized, dict)
        or materialized.get("bytes") != actual_bytes
        or materialized.get("sha256") != actual_sha256
        or materialized.get("records") != fragment["records"]
        or materialized.get("row_groups") != len(fragment["selected_row_groups"])
        or proof.get("transport")
        != {
            "kind": "pyarrow-s3-random-access",
            "whole_object_downloaded": False,
            "whole_object_fallback_allowed": False,
        }
    ):
        raise ValueError("Places selective fetch proof differs from the reduce plan")


def _semantic_row_bytes(row: dict[str, Any]) -> bytes:
    return (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()


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
        self, fragment: dict[str, Any], path: Path
    ) -> Iterator[dict[str, Any]]:
        parquet = self.pq.ParquetFile(path)
        created_by = parquet.metadata.created_by
        self.created_by.add(created_by or "(missing-created-by)")
        physical_groups = (
            list(range(parquet.metadata.num_row_groups))
            if fragment.get("_materialized_selective")
            else fragment["selected_row_groups"]
        )
        for position, physical_index in enumerate(physical_groups):
            original_index = (
                fragment["selected_row_groups"][position]
                if fragment.get("_materialized_selective")
                else physical_index
            )
            for batch in parquet.iter_batches(
                batch_size=16_384, row_groups=[physical_index], use_threads=False
            ):
                for row in batch.to_pylist():
                    if not isinstance(row, dict):
                        raise ValueError("Places fragment row must be an object")
                    row["_source_pack_row_group"] = original_index
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
    ROW_COLUMNS = (
        "gers_id", "primary_name", "alt_names", "brand_name",
        "category_primary", "basic_category", "locality", "region", "country",
        "lat", "lon", "confidence", "operating_status", "partition_key",
        "partition_cell", "execution_group", "source_uri", "source_row_group",
        "source_row_index",
    )
    INSERT_COLUMNS = (
        "leaf", "partition_cell", "partition_key", "confidence_rank", "gers_id",
        "primary_name", "alt_names", "brand_name", "category_primary",
        "basic_category", "locality", "region", "country", "lat", "lon",
        "confidence", "operating_status", "execution_group", "source_uri",
        "source_row_group", "source_row_index", "normalized_bytes",
    )

    def __init__(
        self,
        scratch_dir: Path,
        workspace_observer: Callable[[int], None] | None = None,
    ) -> None:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        self.scratch_dir = scratch_dir
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - hosted dependency boundary
            raise RuntimeError("Places reduction requires DuckDB") from exc
        if duckdb.__version__ != REQUIRED_DUCKDB_VERSION:
            raise RuntimeError(
                "Places reduction requires DuckDB "
                f"{REQUIRED_DUCKDB_VERSION}, found {duckdb.__version__}"
            )
        self.duckdb_version = duckdb.__version__
        self.path = scratch_dir / "places-reduce.duckdb"
        self.temp_directory = scratch_dir / "duckdb-spill"
        self.temp_directory.mkdir(exist_ok=True)
        self.connection = duckdb.connect(str(self.path))
        self.connection.execute("SET threads = 1")
        self.connection.execute("SET preserve_insertion_order = false")
        self.connection.execute(
            "SET max_memory = ?", [f"{REDUCE_DUCKDB_MEMORY_LIMIT_BYTES}B"]
        )
        self.connection.execute("SET temp_directory = ?", [str(self.temp_directory)])
        self.connection.execute(
            "SET max_temp_directory_size = ?", [f"{REDUCE_MAX_SCRATCH_BYTES}B"]
        )
        self.connection.execute(
            """
            CREATE TABLE rows (
                leaf VARCHAR NOT NULL,
                partition_cell VARCHAR NOT NULL,
                partition_key UBIGINT NOT NULL,
                confidence_rank SMALLINT NOT NULL,
                gers_id VARCHAR NOT NULL,
                primary_name VARCHAR NOT NULL,
                alt_names VARCHAR NOT NULL,
                brand_name VARCHAR NOT NULL,
                category_primary VARCHAR NOT NULL,
                basic_category VARCHAR NOT NULL,
                locality VARCHAR NOT NULL,
                region VARCHAR NOT NULL,
                country VARCHAR NOT NULL,
                lat DOUBLE NOT NULL,
                lon DOUBLE NOT NULL,
                confidence DOUBLE NOT NULL,
                operating_status VARCHAR NOT NULL,
                execution_group VARCHAR NOT NULL,
                source_uri VARCHAR NOT NULL,
                source_row_group INTEGER NOT NULL,
                source_row_index BIGINT NOT NULL,
                normalized_bytes BIGINT NOT NULL
            )
            """
        )
        self.pending: list[tuple[Any, ...]] = []
        self.workspace_observer = workspace_observer
        self.peak_scratch_bytes = 0
        self.peak_database_bytes = 0
        self.peak_pending_rows = 0
        self.arrow_append_batches = 0
        self.observe_scratch()

    def scratch_bytes(self) -> int:
        return sum(
            path.stat().st_size
            for path in self.scratch_dir.rglob("*")
            if path.is_file()
        )

    def observe_scratch(self) -> int:
        physical = self.scratch_bytes()
        database_bytes = self.path.stat().st_size if self.path.exists() else 0
        current = physical
        self.peak_scratch_bytes = max(self.peak_scratch_bytes, current)
        self.peak_database_bytes = max(self.peak_database_bytes, database_bytes)
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
                row["primary_name"],
                row["alt_names"],
                row["brand_name"],
                row["category_primary"],
                row["basic_category"],
                row["locality"],
                row["region"],
                row["country"],
                row["lat"],
                row["lon"],
                row["confidence"],
                row["operating_status"],
                row["execution_group"],
                row["source_uri"],
                row["source_row_group"],
                row["source_row_index"],
                len(canonical_json_bytes(row)),
            )
        )
        if len(self.pending) >= REDUCE_MAX_BUFFER_ROWS:
            self.flush()

    def flush(self) -> None:
        if not self.pending:
            return
        if len(self.pending) > REDUCE_MAX_BUFFER_ROWS:
            raise ValueError("Places reducer typed batch exceeded its memory contract")
        self.peak_pending_rows = max(self.peak_pending_rows, len(self.pending))
        try:
            import pyarrow as pa

            types = (
                pa.string(), pa.string(), pa.uint64(), pa.int16(), pa.string(),
                pa.string(), pa.string(), pa.string(), pa.string(), pa.string(),
                pa.string(), pa.string(), pa.string(), pa.float64(), pa.float64(),
                pa.float64(), pa.string(), pa.string(), pa.string(), pa.int32(),
                pa.int64(), pa.int64(),
            )
            columns = zip(*self.pending, strict=True)
            table = pa.Table.from_arrays(
                [pa.array(column, type=field_type) for column, field_type in zip(
                    columns, types, strict=True
                )],
                names=self.INSERT_COLUMNS,
            )
            self.connection.register("reduce_leaf_batch", table)
            try:
                self.connection.execute("INSERT INTO rows SELECT * FROM reduce_leaf_batch")
            finally:
                self.connection.unregister("reduce_leaf_batch")
            self.arrow_append_batches += 1
        except Exception as exc:
            raise ValueError(
                "Places reducer DuckDB store exceeded its hard scratch cap"
            ) from exc
        self.pending = []
        self.observe_scratch()

    def finish(self) -> None:
        self.flush()
        try:
            duplicate = self.connection.execute(
                "SELECT 1 FROM rows GROUP BY source_uri, source_row_group, "
                "source_row_index HAVING count(*) > 1 LIMIT 1"
            ).fetchone()
            if duplicate is not None:
                raise ValueError("duplicate/replayed Places source row identity")
            self.connection.execute("CHECKPOINT")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(
                "Places reducer DuckDB ordering exceeded its hard scratch cap"
            ) from exc
        self.observe_scratch()

    def leaf_stats(self) -> dict[str, tuple[int, int]]:
        return {
            leaf: (records, payload_bytes)
            for leaf, records, payload_bytes in self.connection.execute(
                """
                SELECT leaf, count(*), sum(normalized_bytes)
                FROM rows GROUP BY leaf ORDER BY leaf
                """
            ).fetchall()
        }

    def iter_leaf(self, leaf: str) -> Iterator[dict[str, Any]]:
        selected = ", ".join(self.ROW_COLUMNS)
        cursor = self.connection.execute(
            """
            SELECT {selected} FROM rows WHERE leaf = ?
            ORDER BY
                partition_cell,
                partition_key,
                confidence_rank,
                gers_id,
                source_uri,
                source_row_group,
                source_row_index
            """.format(selected=selected),
            (leaf,),
        )
        self.observe_scratch()
        while values := cursor.fetchmany(REDUCE_MAX_BUFFER_ROWS):
            self.observe_scratch()
            for value in values:
                yield dict(zip(self.ROW_COLUMNS, value, strict=True))

    def close(self) -> None:
        self.connection.close()
        self.path.unlink(missing_ok=True)
        Path(f"{self.path}.wal").unlink(missing_ok=True)
        for path in self.temp_directory.glob("*"):
            if path.is_file():
                path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            self.temp_directory.rmdir()


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
    group = value.get("execution_group")
    selected_ownership = [
        fragment["row_groups"][index] for index in fragment["selected_row_groups"]
    ]
    owning_ranges = [
        item
        for item in selected_ownership
        if item["execution_group"] == group
        and item["minimum_maximum_level_cell"] <= cell <= item["maximum_maximum_level_cell"]
    ]
    if (
        not isinstance(cell, str)
        or len(cell) != maximum_level
        or any(digit not in "0123" for digit in cell)
        or not isinstance(group, str)
        or not cell.startswith(group)
        or not owning_ranges
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


def _candidate_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    rank = round(row["confidence"] * 255)
    return (
        -rank,
        row["partition_key"],
        -rank,
        row["gers_id"],
        row["source_uri"],
        row["source_row_group"],
        row["source_row_index"],
    )


def _push_candidate(
    candidates: dict[str, list[tuple[tuple[Any, ...], bytes]]],
    key: str,
    sort_key: tuple[Any, ...],
    projection: bytes,
    limit: int = 10,
) -> int:
    values = candidates.setdefault(key, [])
    item = (sort_key, projection)
    duplicate_index = next(
        (
            index
            for index, (existing_sort_key, _) in enumerate(values)
            if existing_sort_key[3] == sort_key[3]
        ),
        None,
    )
    if duplicate_index is not None:
        if sort_key < values[duplicate_index][0]:
            previous_bytes = len(values[duplicate_index][1])
            values[duplicate_index] = item
            return len(projection) - previous_bytes
        return 0
    if len(values) < limit:
        values.append(item)
        return len(projection)
    worst = max(range(len(values)), key=lambda index: values[index][0])
    if sort_key < values[worst][0]:
        previous_bytes = len(values[worst][1])
        values[worst] = item
        return len(projection) - previous_bytes
    return 0


class _AdmittedHeadMatcher:
    def __init__(self, plan: dict[str, Any]) -> None:
        self.exact: set[str] = set()
        self.prefixes: set[str] = set()
        self.pairs_by_token: dict[str, list[tuple[str, str]]] = {}
        self.candidate_projection_bytes = 0
        self.peak_candidate_projection_bytes = 0
        for item in plan["head_admission"]["keys"]:
            key = item["key"]
            if key.startswith("e2:"):
                low, separator, high = key.removeprefix("e2:").partition(" ")
                if not separator or not low or not high:
                    raise ValueError("Places admitted pair key is malformed")
                self.pairs_by_token.setdefault(low, []).append((key, high))
                self.pairs_by_token.setdefault(high, []).append((key, low))
            elif key.startswith("e:"):
                self.exact.add(key.removeprefix("e:"))
            elif key.startswith("p:"):
                self.prefixes.add(key.removeprefix("p:"))

    def add(
        self,
        candidates: dict[str, list[tuple[tuple[Any, ...], bytes]]],
        row: dict[str, Any],
        place: Any,
    ) -> None:
        from experiment_places_locality_head import place_terms  # noqa: PLC0415

        terms = place_terms(place)
        sort_key = _candidate_sort_key(row)
        matched_keys = {f"e:{token}" for token in terms & self.exact}
        matched_keys.update(
            f"p:{prefix}"
            for prefix in {
                token[:length]
                for token in terms
                for length in HEAD_PREFIX_LENGTHS
                if len(token) >= length and token[:length] in self.prefixes
            }
        )
        matched_keys.update(
            key
            for token in terms
            for key, other in self.pairs_by_token.get(token, ())
            if other in terms
        )
        if not matched_keys:
            return
        projection = encode_record(place)
        if len(projection) > MAX_HEAD_SINGLE_PROJECTION_BYTES:
            raise ValueError("Places admitted head candidate exceeds the Worker entry cap")
        def push(key: str) -> None:
            self.candidate_projection_bytes += _push_candidate(
                candidates, key, sort_key, projection
            )
            self.peak_candidate_projection_bytes = max(
                self.peak_candidate_projection_bytes,
                self.candidate_projection_bytes,
            )
            if (
                self.candidate_projection_bytes
                > HEAD_CANDIDATE_MAX_PROJECTION_BYTES_IN_MEMORY
            ):
                raise ValueError(
                    "Places head candidate projections exceeded their memory cap"
                )

        for key in matched_keys:
            push(key)


def _write_head_candidates(
    candidates: dict[str, list[tuple[tuple[Any, ...], bytes]]],
    *,
    output_dir: Path,
    job_index: int,
    plan_sha256: str,
    admission_sha256: str,
    maximum_candidates: int,
    candidate_projection_bytes: int,
    peak_candidate_projection_bytes: int,
) -> dict[str, Any]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - hosted dependency boundary
        raise RuntimeError("Places reducer head candidates require pyarrow") from exc
    schema = pa.schema(
        [
            ("key", pa.string()), ("rank", pa.int16()),
            ("partition_key", pa.uint64()), ("gers_id", pa.string()),
            ("source_uri", pa.string()), ("source_row_group", pa.int32()),
            ("source_row_index", pa.int64()), ("projection", pa.binary()),
        ],
        metadata={
            b"artifact_schema": HEAD_CANDIDATE_SCHEMA.encode(),
            b"plan_sha256": plan_sha256.encode(),
            b"head_admission_sha256": admission_sha256.encode(),
            b"job_index": str(job_index).encode(),
        },
    )
    staging = output_dir / "head-candidates"
    staging.mkdir(parents=True, exist_ok=True)
    temporary = staging / f"job-{job_index:03d}.tmp.parquet"
    rows: list[dict[str, Any]] = []
    candidate_count = 0
    peak_batch_rows = 0
    batch_bytes = 0
    peak_batch_bytes = 0
    peak_projection_bytes = 0
    with pq.ParquetWriter(
        temporary,
        schema,
        compression="zstd",
        compression_level=9,
        use_dictionary=True,
        write_statistics=True,
    ) as writer:
        def flush() -> None:
            nonlocal rows, peak_batch_rows, batch_bytes, peak_batch_bytes
            if not rows:
                return
            peak_batch_rows = max(peak_batch_rows, len(rows))
            peak_batch_bytes = max(peak_batch_bytes, batch_bytes)
            writer.write_table(pa.Table.from_pylist(rows, schema=schema))
            rows = []
            batch_bytes = 0

        for key in sorted(candidates, key=lambda value: value.encode("utf-8")):
            for sort_key, projection in sorted(candidates[key], key=lambda item: item[0]):
                projection_bytes = len(projection)
                if projection_bytes > MAX_HEAD_SINGLE_PROJECTION_BYTES:
                    raise ValueError("Places head candidate exceeds the Worker entry cap")
                row_bytes = (
                    len(key.encode("utf-8"))
                    + len(sort_key[3].encode("utf-8"))
                    + len(sort_key[4].encode("utf-8"))
                    + projection_bytes
                    + 32
                )
                if row_bytes > HEAD_CANDIDATE_MAX_ROW_BYTES:
                    raise ValueError("Places head candidate exceeds its writer byte cap")
                if rows and batch_bytes + row_bytes > HEAD_CANDIDATE_WRITE_BATCH_BYTES:
                    flush()
                rows.append(
                    {
                        "key": key,
                        "rank": sort_key[0],
                        "partition_key": sort_key[1],
                        "gers_id": sort_key[3],
                        "source_uri": sort_key[4],
                        "source_row_group": sort_key[5],
                        "source_row_index": sort_key[6],
                        "projection": projection,
                    }
                )
                batch_bytes += row_bytes
                peak_projection_bytes = max(peak_projection_bytes, projection_bytes)
                candidate_count += 1
                if candidate_count > maximum_candidates:
                    raise ValueError("Places head candidates exceeded their memory contract")
                if len(rows) >= HEAD_CANDIDATE_WRITE_BATCH_ROWS:
                    flush()
        flush()
    sha256, size = sha256_file(temporary)
    relative = Path("head-candidates") / "sha256" / f"{sha256}.parquet"
    destination = output_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temporary, destination)
    return {
        "schema": HEAD_CANDIDATE_SCHEMA,
        "object_key": relative.as_posix(),
        "bytes": size,
        "sha256": sha256,
        "keys": len(candidates),
        "candidates": candidate_count,
        "maximum_candidates": maximum_candidates,
        "candidate_projection_bytes": candidate_projection_bytes,
        "maximum_candidate_projection_bytes_in_memory": (
            HEAD_CANDIDATE_MAX_PROJECTION_BYTES_IN_MEMORY
        ),
        "peak_candidate_projection_bytes_in_memory": peak_candidate_projection_bytes,
        "result_cap_per_key": 10,
        "writer": {
            "kind": "pyarrow-parquet-writer-batches-v1",
            "maximum_batch_rows": HEAD_CANDIDATE_WRITE_BATCH_ROWS,
            "maximum_batch_bytes": HEAD_CANDIDATE_WRITE_BATCH_BYTES,
            "maximum_row_bytes": HEAD_CANDIDATE_MAX_ROW_BYTES,
            "peak_batch_rows": peak_batch_rows,
            "peak_batch_bytes": peak_batch_bytes,
            "maximum_projection_bytes": MAX_HEAD_SINGLE_PROJECTION_BYTES,
            "peak_projection_bytes": peak_projection_bytes,
            "full_table_materialized": False,
        },
        "head_admission_sha256": admission_sha256,
        "duplicate_gers_policy": HEAD_DUPLICATE_GERS_POLICY,
    }


def _build_streaming_artifact(
    rows: Iterator[dict[str, Any]],
    output: Path,
    *,
    scratch_dir: Path,
    on_place: Callable[[dict[str, Any], Any], None],
    scratch_observer: Callable[[int], None] | None = None,
    block_entries: int = 256,
) -> dict[str, Any]:
    """Assemble PCSH bytes from bounded typed DuckDB external runs."""

    scratch_dir.mkdir(parents=True, exist_ok=True)
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - hosted dependency boundary
        raise RuntimeError("Places PCSH writing requires DuckDB") from exc
    if duckdb.__version__ != REQUIRED_DUCKDB_VERSION:
        raise RuntimeError(
            "Places PCSH writing requires DuckDB "
            f"{REQUIRED_DUCKDB_VERSION}, found {duckdb.__version__}"
        )
    database = scratch_dir / "pcsh-stream.duckdb"
    temp_directory = scratch_dir / "duckdb-spill"
    temp_directory.mkdir(exist_ok=True)
    connection = duckdb.connect(str(database))
    connection.execute("SET threads = 1")
    connection.execute("SET preserve_insertion_order = false")
    connection.execute(
        "SET max_memory = ?", [f"{REDUCE_DUCKDB_MEMORY_LIMIT_BYTES}B"]
    )
    connection.execute("SET temp_directory = ?", [str(temp_directory)])
    connection.execute(
        "SET max_temp_directory_size = ?", [f"{REDUCE_MAX_SCRATCH_BYTES}B"]
    )
    connection.execute(
        "CREATE TABLE postings (token VARCHAR NOT NULL, doc_id BIGINT NOT NULL, "
        "mask UTINYINT NOT NULL, rank UTINYINT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE projections (doc_id BIGINT NOT NULL, rank UTINYINT NOT NULL, "
        "payload BLOB NOT NULL)"
    )
    pending_postings: list[tuple[str, int, int, int]] = []
    pending_projections: list[tuple[int, int, bytes]] = []
    records_count = token_occurrences = projection_bytes = 0
    peak_scratch_bytes = 0
    peak_pending_postings = 0
    peak_pending_projections = 0
    arrow_append_batches = 0

    def observe_scratch() -> int:
        nonlocal peak_scratch_bytes
        current = sum(
            path.stat().st_size
            for path in scratch_dir.rglob("*")
            if path.is_file()
        )
        peak_scratch_bytes = max(peak_scratch_bytes, current)
        if current > REDUCE_MAX_SCRATCH_BYTES:
            raise ValueError("Places PCSH DuckDB spill exceeded its scratch cap")
        if scratch_observer is not None:
            scratch_observer(current)
        return current

    def flush() -> None:
        nonlocal pending_postings, pending_projections
        nonlocal peak_pending_postings, peak_pending_projections, arrow_append_batches
        import pyarrow as pa

        if pending_postings:
            peak_pending_postings = max(peak_pending_postings, len(pending_postings))
            token, doc_id, mask, rank = zip(*pending_postings, strict=True)
            table = pa.table(
                {
                    "token": pa.array(token, type=pa.string()),
                    "doc_id": pa.array(doc_id, type=pa.int64()),
                    "mask": pa.array(mask, type=pa.uint8()),
                    "rank": pa.array(rank, type=pa.uint8()),
                }
            )
            connection.register("pcsh_posting_batch", table)
            try:
                connection.execute(
                    "INSERT INTO postings SELECT * FROM pcsh_posting_batch"
                )
            finally:
                connection.unregister("pcsh_posting_batch")
            arrow_append_batches += 1
            pending_postings = []
        if pending_projections:
            peak_pending_projections = max(
                peak_pending_projections, len(pending_projections)
            )
            doc_id, rank, payload = zip(*pending_projections, strict=True)
            table = pa.table(
                {
                    "doc_id": pa.array(doc_id, type=pa.int64()),
                    "rank": pa.array(rank, type=pa.uint8()),
                    "payload": pa.array(payload, type=pa.binary()),
                }
            )
            connection.register("pcsh_projection_batch", table)
            try:
                connection.execute(
                    "INSERT INTO projections SELECT * FROM pcsh_projection_batch"
                )
            finally:
                connection.unregister("pcsh_projection_batch")
            arrow_append_batches += 1
            pending_projections = []
        observe_scratch()

    try:
        for doc_id, row in enumerate(rows):
            place = place_from_row(row, doc_id + 1)
            rank = round(place.confidence * 255)
            projection = encode_projection(place)
            pending_projections.append((doc_id, rank, projection))
            projection_bytes += len(projection)
            for field, value in place.field_text().items():
                bit = FIELD_BITS[field]
                for token in set(tokens(value)):
                    pending_postings.append((token, doc_id, bit, rank))
                    token_occurrences += 1
                    if len(pending_postings) >= REDUCE_MAX_BUFFER_ROWS * 4:
                        flush()
            on_place(row, place)
            records_count += 1
            if len(pending_projections) >= REDUCE_MAX_BUFFER_ROWS:
                flush()
        flush()
        connection.execute("CHECKPOINT")
        observe_scratch()

        postings_path = scratch_dir / "postings.bin"
        lexicon_path = scratch_dir / "lexicon.bin"
        records_path = scratch_dir / "records.bin"
        index_path = scratch_dir / "record-index.bin"
        blocks: list[dict[str, Any]] = []
        posting_offset = 0
        lexicon_offset = 0
        token_count = 0
        group: list[tuple[str, int, int, int]] = []

        def write_lexicon_group(target: Any) -> None:
            nonlocal group, lexicon_offset
            if not group:
                return
            encoded = bytearray(encode_varint(len(group)))
            previous = b""
            for token, offset, length, count in group:
                key = token.encode("utf-8")
                shared = common_prefix(previous, key)
                suffix = key[shared:]
                encoded += encode_varint(shared) + encode_varint(len(suffix)) + suffix
                encoded += encode_varint(offset) + encode_varint(length) + encode_varint(count)
                previous = key
            target.write(encoded)
            blocks.append(
                {"first": group[0][0], "last": group[-1][0],
                 "offset": lexicon_offset, "length": len(encoded), "entries": len(group)}
            )
            lexicon_offset += len(encoded)
            group = []

        with postings_path.open("wb") as posting_target, lexicon_path.open("wb") as lexicon_target:
            cursor = connection.execute(
                "SELECT token, doc_id, bit_or(mask)::INTEGER, max(rank)::INTEGER "
                "FROM postings GROUP BY token, doc_id ORDER BY token, doc_id"
            )
            observe_scratch()
            current_token: str | None = None
            items: list[tuple[int, int, int]] = []

            def write_posting() -> None:
                nonlocal items, current_token, posting_offset, token_count
                if current_token is None:
                    return
                encoded = bytearray()
                previous_doc = 0
                for index, (doc_id, mask, rank) in enumerate(items):
                    encoded += encode_varint(doc_id if index == 0 else doc_id - previous_doc)
                    encoded += bytes((mask, rank))
                    previous_doc = doc_id
                posting_target.write(encoded)
                group.append((current_token, posting_offset, len(encoded), len(items)))
                posting_offset += len(encoded)
                token_count += 1
                items = []
                if len(group) >= block_entries:
                    write_lexicon_group(lexicon_target)

            while posting_rows := cursor.fetchmany(REDUCE_MAX_BUFFER_ROWS):
                observe_scratch()
                for token, doc_id, mask, rank in posting_rows:
                    if current_token is not None and token != current_token:
                        write_posting()
                    current_token = token
                    items.append((doc_id, mask, rank))
            write_posting()
            write_lexicon_group(lexicon_target)
        observe_scratch()

        with records_path.open("wb") as records_target, index_path.open("w+b") as index_target:
            index_target.truncate(records_count * RECORD_INDEX.size)
            record_offset = 0
            cursor = connection.execute(
                "SELECT doc_id, payload FROM projections ORDER BY rank DESC, doc_id"
            )
            observe_scratch()
            while projection_rows := cursor.fetchmany(REDUCE_MAX_BUFFER_ROWS):
                observe_scratch()
                for doc_id, payload in projection_rows:
                    if record_offset + len(payload) >= 2**32:
                        raise ValueError("record section exceeds 32-bit offset format")
                    records_target.write(payload)
                    index_target.seek(doc_id * RECORD_INDEX.size)
                    index_target.write(RECORD_INDEX.pack(record_offset, len(payload)))
                    record_offset += len(payload)
        observe_scratch()

        components = {
            "lexicon": lexicon_path,
            "postings": postings_path,
            "record_index": index_path,
            "records": records_path,
        }
        directory = {
            "schema_version": 1,
            "tokenizer_version": TOKENIZER_VERSION,
            "record_count": records_count,
            "token_count": token_count,
            "cell_degrees": 0.25,
            "field_bits": FIELD_BITS,
            "lexicon_blocks": blocks,
            "components": {name: {"length": path.stat().st_size} for name, path in components.items()},
        }
        for _ in range(12):
            directory_bytes = json.dumps(directory, sort_keys=True, separators=(",", ":")).encode()
            offset = PREAMBLE.size + len(directory_bytes)
            changed = False
            for name, path in components.items():
                if directory["components"][name].get("offset") != offset:
                    directory["components"][name]["offset"] = offset
                    changed = True
                offset += path.stat().st_size
            if not changed:
                break
        else:
            raise RuntimeError("artifact directory offsets did not stabilize")
        directory_bytes = json.dumps(directory, sort_keys=True, separators=(",", ":")).encode()
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("wb") as target:
            target.write(PREAMBLE.pack(MAGIC, len(directory_bytes)))
            target.write(directory_bytes)
            for path in components.values():
                with path.open("rb") as source:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        return {
            "places": records_count,
            "tokens": token_count,
            "token_occurrences": token_occurrences,
            "projection_bytes": projection_bytes,
            "artifact_bytes": output.stat().st_size,
            "duckdb_version": duckdb.__version__,
            "peak_scratch_bytes": peak_scratch_bytes,
            "peak_pending_postings": peak_pending_postings,
            "peak_pending_projections": peak_pending_projections,
            "arrow_append_batches": arrow_append_batches,
            "registered_arrow_batches": True,
        }
    finally:
        connection.close()
        for path in sorted(scratch_dir.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                with contextlib.suppress(OSError):
                    path.rmdir()


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
    peak_compaction_scratch_bytes = 0

    def observe_combined_workspace(
        scratch_bytes: int, additional_bytes: int = 0
    ) -> None:
        nonlocal peak_workspace_bytes, peak_compaction_scratch_bytes
        current_scratch_bytes = scratch_bytes + additional_bytes
        peak_compaction_scratch_bytes = max(
            peak_compaction_scratch_bytes, current_scratch_bytes
        )
        if current_scratch_bytes > REDUCE_MAX_SCRATCH_BYTES:
            raise ValueError(
                "Places reducer combined DuckDB scratch usage exceeded its hard cap"
            )
        current_workspace_bytes = (
            current_scratch_bytes
            + produced_output_bytes
            + active_materialized_fragment_bytes
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
            for group in fragment["selected_execution_groups"]:
                group_fanin[group] = group_fanin.get(group, 0) + 1
            actual_fragment_records = 0
            semantic_hashes = {
                index: hashlib.sha256() for index in fragment["selected_row_groups"]
            }
            with materializer.path(fragment) as (path, materialized_selective):
                reader_fragment = {
                    **fragment,
                    "_materialized_selective": materialized_selective,
                }
                for raw_row in fragment_reader(reader_fragment, path):
                    if not isinstance(raw_row, dict):
                        raise ValueError("Places fragment row must be an object")
                    raw_row = dict(raw_row)
                    source_row_group = raw_row.pop("_source_pack_row_group", None)
                    if default_reader is not None:
                        if source_row_group not in semantic_hashes:
                            raise ValueError("Places row omitted its selected pack row group")
                        semantic_hashes[source_row_group].update(
                            _semantic_row_bytes(raw_row)
                        )
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
            if default_reader is not None:
                for row_group_index, digest in semantic_hashes.items():
                    if digest.hexdigest() != fragment["row_groups"][row_group_index]["semantic_sha256"]:
                        raise ValueError(
                            "Places selected row-group semantic binding differs"
                        )
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
        head_candidates: dict[str, list[tuple[tuple[Any, ...], bytes]]] = {}
        head_matcher = _AdmittedHeadMatcher(plan)
        peak_writer_rows = 0
        peak_writer_normalized_bytes = 0
        peak_writer_token_occurrences = 0
        peak_writer_projection_bytes = 0
        peak_writer_scratch_bytes = 0
        peak_writer_pending_postings = 0
        peak_writer_pending_projections = 0
        writer_arrow_append_batches = 0
        writer_duckdb_version: str | None = None
        for leaf in sorted(leaves):
            leaf_rows, leaf_normalized_bytes = stats[leaf]
            if (
                leaf_rows > REDUCE_MAX_LEAF_ROWS_IN_MEMORY
                or leaf_normalized_bytes > REDUCE_MAX_LEAF_INPUT_BYTES
            ):
                raise ValueError("Places leaf exceeds the streaming writer input cap")
            conservative_output_bound = (
                64_000_000
                + leaf_normalized_bytes
                + leaf_rows * 512
            )
            preflight_workspace(store.observe_scratch(), conservative_output_bound)
            peak_writer_rows = max(peak_writer_rows, leaf_rows)
            peak_writer_normalized_bytes = max(
                peak_writer_normalized_bytes, leaf_normalized_bytes
            )
            route = _route(leaf)
            output_path = output_dir / route["object"]
            artifact_report = _build_streaming_artifact(
                store.iter_leaf(leaf),
                output_path,
                scratch_dir=scratch_dir / f"pcsh-{leaf}",
                on_place=lambda row, place: head_matcher.add(
                    head_candidates, row, place
                ),
                scratch_observer=lambda writer_scratch: observe_combined_workspace(
                    store.observe_scratch(), writer_scratch
                ),
            )
            token_occurrences = artifact_report["token_occurrences"]
            projection_bytes = artifact_report["projection_bytes"]
            if artifact_report["places"] != leaves[leaf]:
                raise AssertionError("Places PCSH writer changed leaf row cardinality")
            if (
                token_occurrences > REDUCE_MAX_LEAF_TOKEN_OCCURRENCES
                or projection_bytes > REDUCE_MAX_LEAF_PROJECTION_BYTES
            ):
                raise ValueError("Places leaf exceeds a streaming writer expansion cap")
            peak_writer_token_occurrences = max(
                peak_writer_token_occurrences, token_occurrences
            )
            peak_writer_projection_bytes = max(
                peak_writer_projection_bytes, projection_bytes
            )
            peak_writer_scratch_bytes = max(
                peak_writer_scratch_bytes, artifact_report["peak_scratch_bytes"]
            )
            peak_writer_pending_postings = max(
                peak_writer_pending_postings,
                artifact_report["peak_pending_postings"],
            )
            peak_writer_pending_projections = max(
                peak_writer_pending_projections,
                artifact_report["peak_pending_projections"],
            )
            writer_duckdb_version = artifact_report["duckdb_version"]
            writer_arrow_append_batches += artifact_report["arrow_append_batches"]
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
        if output_records != job["expected_records"]:
            raise AssertionError(
                "Places final shard rows differ from reduce input rows"
            )
        head_candidate_artifact = _write_head_candidates(
            head_candidates,
            output_dir=output_dir,
            job_index=job_index,
            plan_sha256=plan["plan_sha256"],
            admission_sha256=plan["head_admission"]["artifact"]["sha256"],
            maximum_candidates=len(plan["head_admission"]["keys"]) * 10,
            candidate_projection_bytes=head_matcher.candidate_projection_bytes,
            peak_candidate_projection_bytes=(
                head_matcher.peak_candidate_projection_bytes
            ),
        )
        produced_output_bytes += head_candidate_artifact["bytes"]
        observe_workspace(0)
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
                [
                    {"sha256": fragment["sha256"], "row_groups": fragment["selected_row_groups"]}
                    for fragment in job["input_fragments"]
                ]
            ),
            "runtime": {
                "required_python_version": REQUIRED_PYTHON_VERSION,
                "required_pyarrow_version": REQUIRED_PYARROW_VERSION,
                "required_duckdb_version": REQUIRED_DUCKDB_VERSION,
                "actual_python_version": platform.python_version(),
                "actual_duckdb_version": store.duckdb_version,
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
                "kind": "duckdb-typed-external-streaming-pcsh-v3",
                "engine": "duckdb",
                "engine_version": store.duckdb_version,
                "maximum_memory_bytes": REDUCE_DUCKDB_MEMORY_LIMIT_BYTES,
                "typed_rows": True,
                "json_payloads": False,
                "raw_input_fragments": len(job["input_fragments"]),
                "maximum_raw_group_fanin": max(group_fanin.values(), default=0),
                "compacted_spools_per_leaf": 1,
                "maximum_open_fragment_files": REDUCE_MAX_OPEN_FRAGMENT_FILES,
                "maximum_buffer_rows": REDUCE_MAX_BUFFER_ROWS,
                "peak_buffer_rows": store.peak_pending_rows,
                "arrow_append_batches": store.arrow_append_batches,
                "registered_arrow_batches": True,
                "maximum_active_leaf_partitions": REDUCE_MAX_ACTIVE_LEAF_PARTITIONS,
                "peak_active_leaf_partitions": 1 if leaves else 0,
                "maximum_leaf_input_bytes": REDUCE_MAX_LEAF_INPUT_BYTES,
                "maximum_scratch_bytes": REDUCE_MAX_SCRATCH_BYTES,
                "maximum_workspace_bytes": REDUCE_MAX_WORKSPACE_BYTES,
                "fragment_materialization": {
                    "adapter": "local-or-no-shell-argv-v1",
                    "remote_fetch_enabled": fragment_fetch_command is not None,
                    "fetched_fragments": materializer.fetched_fragments,
                    "fetched_bytes": materializer.fetched_bytes,
                    "whole_pack_fetches": materializer.whole_pack_fetches,
                    "selective_pack_fetches": materializer.selective_pack_fetches,
                    "selected_row_groups": materializer.selected_row_groups,
                    "selected_compressed_bytes": materializer.selected_compressed_bytes,
                    "selected_uncompressed_bytes": (
                        materializer.selected_uncompressed_bytes
                    ),
                    "maximum_materialized_bytes": (
                        materializer.maximum_materialized_bytes
                    ),
                    "maximum_simultaneously_materialized_fragments": 1,
                    "peak_materialized_fragment_bytes": (
                        materializer.peak_materialized_fragment_bytes
                    ),
                    "identity_verification": "whole-sha-or-footer-bound-selective-proof-v2",
                },
                "writer_materialization": {
                    "kind": "duckdb-typed-external-streaming-pcsh-writer-v2",
                    "engine_version": writer_duckdb_version,
                    "measurement": "deterministic-stream-token-projection-bounds-v1",
                    "maximum_memory_bytes": REDUCE_DUCKDB_MEMORY_LIMIT_BYTES,
                    "maximum_pending_postings": REDUCE_MAX_BUFFER_ROWS * 4,
                    "maximum_pending_projections": REDUCE_MAX_BUFFER_ROWS,
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
                    "peak_pending_postings": peak_writer_pending_postings,
                    "peak_pending_projections": peak_writer_pending_projections,
                    "peak_scratch_bytes": peak_writer_scratch_bytes,
                    "arrow_append_batches": writer_arrow_append_batches,
                    "registered_arrow_batches": True,
                },
                "peak_database_bytes": store.peak_database_bytes,
                "peak_scratch_bytes": peak_compaction_scratch_bytes,
                "peak_workspace_bytes": peak_workspace_bytes,
            },
            "head_candidates": head_candidate_artifact,
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
            "head_candidates",
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
        != digest_value(
            [
                {"sha256": item["sha256"], "row_groups": item["selected_row_groups"]}
                for item in job["input_fragments"]
            ]
        )
    ):
        raise ValueError("Places reduce report identity differs from its planned job")
    runtime = report["runtime"]
    if (
        not isinstance(runtime, dict)
        or runtime.get("required_python_version") != REQUIRED_PYTHON_VERSION
        or runtime.get("required_pyarrow_version") != REQUIRED_PYARROW_VERSION
        or runtime.get("required_duckdb_version") != REQUIRED_DUCKDB_VERSION
        or not isinstance(runtime.get("actual_python_version"), str)
        or not runtime["actual_python_version"]
        or runtime.get("actual_duckdb_version") != REQUIRED_DUCKDB_VERSION
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
            "engine",
            "engine_version",
            "maximum_memory_bytes",
            "typed_rows",
            "json_payloads",
            "raw_input_fragments",
            "maximum_raw_group_fanin",
            "compacted_spools_per_leaf",
            "maximum_open_fragment_files",
            "maximum_buffer_rows",
            "peak_buffer_rows",
            "arrow_append_batches",
            "registered_arrow_batches",
            "maximum_active_leaf_partitions",
            "peak_active_leaf_partitions",
            "maximum_leaf_input_bytes",
            "maximum_scratch_bytes",
            "maximum_workspace_bytes",
            "fragment_materialization",
            "writer_materialization",
            "peak_database_bytes",
            "peak_scratch_bytes",
            "peak_workspace_bytes",
        },
        "Places reduce compaction",
    )
    expected_group_fanin = max(
        Counter(
            group
            for item in job["input_fragments"]
            for group in item["selected_execution_groups"]
        ).values(),
        default=0,
    )
    if (
        compaction.get("kind") != "duckdb-typed-external-streaming-pcsh-v3"
        or compaction.get("engine") != "duckdb"
        or compaction.get("engine_version") != REQUIRED_DUCKDB_VERSION
        or compaction.get("maximum_memory_bytes")
        != REDUCE_DUCKDB_MEMORY_LIMIT_BYTES
        or compaction.get("typed_rows") is not True
        or compaction.get("json_payloads") is not False
        or compaction.get("maximum_open_fragment_files")
        != REDUCE_MAX_OPEN_FRAGMENT_FILES
        or compaction.get("maximum_buffer_rows") != REDUCE_MAX_BUFFER_ROWS
        or type(compaction.get("peak_buffer_rows")) is not int
        or not 0 <= compaction["peak_buffer_rows"] <= REDUCE_MAX_BUFFER_ROWS
        or type(compaction.get("arrow_append_batches")) is not int
        or compaction["arrow_append_batches"] < 0
        or compaction.get("registered_arrow_batches") is not True
        or compaction.get("maximum_active_leaf_partitions")
        != REDUCE_MAX_ACTIVE_LEAF_PARTITIONS
        or compaction.get("peak_active_leaf_partitions") != 1
        or compaction.get("maximum_leaf_input_bytes") != REDUCE_MAX_LEAF_INPUT_BYTES
        or compaction.get("maximum_scratch_bytes") != REDUCE_MAX_SCRATCH_BYTES
        or compaction.get("maximum_workspace_bytes") != REDUCE_MAX_WORKSPACE_BYTES
        or type(compaction.get("peak_database_bytes")) is not int
        or not 0 <= compaction["peak_database_bytes"] <= REDUCE_MAX_SCRATCH_BYTES
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
            "whole_pack_fetches",
            "selective_pack_fetches",
            "selected_row_groups",
            "selected_compressed_bytes",
            "selected_uncompressed_bytes",
            "maximum_materialized_bytes",
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
    whole_fetches = require_int(
        materialization["whole_pack_fetches"], "Places whole pack fetches"
    )
    selective_fetches = require_int(
        materialization["selective_pack_fetches"], "Places selective pack fetches"
    )
    selected_groups = require_int(
        materialization["selected_row_groups"], "Places selected row groups"
    )
    selected_bytes = require_int(
        materialization["selected_compressed_bytes"],
        "Places selected compressed bytes",
    )
    selected_uncompressed_bytes = require_int(
        materialization["selected_uncompressed_bytes"],
        "Places selected uncompressed bytes",
    )
    maximum_materialized_bytes = require_int(
        materialization["maximum_materialized_bytes"],
        "Places maximum materialized bytes",
    )
    if (
        materialization["adapter"] != "local-or-no-shell-argv-v1"
        or type(materialization["remote_fetch_enabled"]) is not bool
        or materialization["maximum_simultaneously_materialized_fragments"] != 1
        or materialization["identity_verification"]
        != "whole-sha-or-footer-bound-selective-proof-v2"
        or selected_groups
        != sum(len(item["selected_row_groups"]) for item in job["input_fragments"])
        or selected_bytes != job["selected_compressed_bytes"]
        or selected_uncompressed_bytes != job["selected_uncompressed_bytes"]
        or maximum_materialized_bytes != job["maximum_materialized_bytes"]
        or (
            materialization["remote_fetch_enabled"]
            and (
                fetched_fragments != len(job["input_fragments"])
                or whole_fetches != job["whole_pack_fetches"]
                or selective_fetches != job["selective_pack_fetches"]
                or fetched_bytes <= 0
                or fetched_bytes
                > sum(
                    item["maximum_materialized_bytes"]
                    for item in job["input_fragments"]
                )
                or peak_materialized <= 0
                or peak_materialized > job["maximum_materialized_bytes"]
            )
        )
        or (
            not materialization["remote_fetch_enabled"]
            and (
                fetched_fragments != 0 or fetched_bytes != 0 or peak_materialized != 0
                or whole_fetches != 0 or selective_fetches != 0
            )
        )
    ):
        raise ValueError("Places fragment materialization evidence is invalid")
    writer = require_exact(
        compaction["writer_materialization"],
        {
            "kind",
            "engine_version",
            "measurement",
            "maximum_memory_bytes",
            "maximum_pending_postings",
            "maximum_pending_projections",
            "maximum_leaf_rows",
            "maximum_leaf_normalized_bytes",
            "maximum_leaf_token_occurrences",
            "maximum_leaf_projection_bytes",
            "peak_leaf_rows",
            "peak_leaf_normalized_bytes",
            "peak_leaf_token_occurrences",
            "peak_leaf_projection_bytes",
            "peak_pending_postings",
            "peak_pending_projections",
            "peak_scratch_bytes",
            "arrow_append_batches",
            "registered_arrow_batches",
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
        writer["kind"] != "duckdb-typed-external-streaming-pcsh-writer-v2"
        or writer["engine_version"] != REQUIRED_DUCKDB_VERSION
        or writer["measurement"] != "deterministic-stream-token-projection-bounds-v1"
        or writer["maximum_memory_bytes"] != REDUCE_DUCKDB_MEMORY_LIMIT_BYTES
        or writer["maximum_pending_postings"] != REDUCE_MAX_BUFFER_ROWS * 4
        or writer["maximum_pending_projections"] != REDUCE_MAX_BUFFER_ROWS
        or type(writer["arrow_append_batches"]) is not int
        or writer["arrow_append_batches"] < 0
        or writer["registered_arrow_batches"] is not True
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
    for peak_field, cap in (
        ("peak_pending_postings", REDUCE_MAX_BUFFER_ROWS * 4),
        ("peak_pending_projections", REDUCE_MAX_BUFFER_ROWS),
        ("peak_scratch_bytes", REDUCE_MAX_SCRATCH_BYTES),
    ):
        peak = require_int(writer[peak_field], f"Places {peak_field}")
        if peak > cap:
            raise ValueError("Places PCSH writer bounded peak exceeds its cap")
    if writer["peak_leaf_rows"] != max(item["rows"] for item in job["leaves"]):
        raise ValueError("Places PCSH writer row peak differs from its planned leaves")
    candidates = require_exact(
        report["head_candidates"],
        {
            "schema", "object_key", "bytes", "sha256", "keys", "candidates",
            "maximum_candidates", "candidate_projection_bytes",
            "maximum_candidate_projection_bytes_in_memory",
            "peak_candidate_projection_bytes_in_memory", "result_cap_per_key", "writer",
            "head_admission_sha256",
            "duplicate_gers_policy",
        },
        "Places reducer head candidates",
    )
    if (
        candidates["schema"] != HEAD_CANDIDATE_SCHEMA
        or not isinstance(candidates["object_key"], str)
        or not candidates["object_key"].endswith(
            f"/sha256/{candidates['sha256']}.parquet"
        )
        or candidates["result_cap_per_key"] != 10
        or candidates["maximum_candidates"]
        != len(plan["head_admission"]["keys"]) * 10
        or candidates["maximum_candidate_projection_bytes_in_memory"]
        != HEAD_CANDIDATE_MAX_PROJECTION_BYTES_IN_MEMORY
        or candidates["head_admission_sha256"]
        != plan["head_admission"]["artifact"]["sha256"]
        or candidates["duplicate_gers_policy"] != HEAD_DUPLICATE_GERS_POLICY
    ):
        raise ValueError("Places reducer head candidate identity is invalid")
    require_int(candidates["bytes"], "Places head candidate bytes", minimum=1)
    require_sha256(candidates["sha256"], "Places head candidate sha256")
    require_int(candidates["keys"], "Places head candidate keys")
    candidate_count = require_int(candidates["candidates"], "Places head candidates")
    candidate_projection_bytes = require_int(
        candidates["candidate_projection_bytes"],
        "Places head candidate projection bytes",
    )
    peak_candidate_projection_bytes = require_int(
        candidates["peak_candidate_projection_bytes_in_memory"],
        "Places peak head candidate projection bytes",
    )
    candidate_writer = require_exact(
        candidates["writer"],
        {
            "kind", "maximum_batch_rows", "maximum_batch_bytes", "maximum_row_bytes",
            "peak_batch_rows", "peak_batch_bytes", "maximum_projection_bytes",
            "peak_projection_bytes", "full_table_materialized",
        },
        "Places head candidate writer",
    )
    if (
        candidate_count > candidates["keys"] * 10
        or candidate_count > candidates["maximum_candidates"]
        or candidate_projection_bytes > peak_candidate_projection_bytes
        or peak_candidate_projection_bytes
        > HEAD_CANDIDATE_MAX_PROJECTION_BYTES_IN_MEMORY
        or candidate_writer["kind"] != "pyarrow-parquet-writer-batches-v1"
        or candidate_writer["maximum_batch_rows"] != HEAD_CANDIDATE_WRITE_BATCH_ROWS
        or candidate_writer["maximum_batch_bytes"] != HEAD_CANDIDATE_WRITE_BATCH_BYTES
        or candidate_writer["maximum_row_bytes"] != HEAD_CANDIDATE_MAX_ROW_BYTES
        or type(candidate_writer["peak_batch_rows"]) is not int
        or not 0 <= candidate_writer["peak_batch_rows"] <= HEAD_CANDIDATE_WRITE_BATCH_ROWS
        or type(candidate_writer["peak_batch_bytes"]) is not int
        or not 0 <= candidate_writer["peak_batch_bytes"] <= HEAD_CANDIDATE_WRITE_BATCH_BYTES
        or candidate_writer["maximum_projection_bytes"]
        != MAX_HEAD_SINGLE_PROJECTION_BYTES
        or type(candidate_writer["peak_projection_bytes"]) is not int
        or not 0 <= candidate_writer["peak_projection_bytes"]
        <= MAX_HEAD_SINGLE_PROJECTION_BYTES
        or candidate_writer["full_table_materialized"] is not False
    ):
        raise ValueError("Places reducer head candidates exceed their per-key cap")
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
