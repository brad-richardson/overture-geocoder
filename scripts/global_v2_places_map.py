#!/usr/bin/env python3
"""Strict, bounded map primitive for a pinned global Overture Places inventory.

Each invocation consumes exactly one inventory task (a deterministic set of
Parquet row-group ranges), applies an exclusive validation policy, and writes
content-addressed fragments. A fragment contains exactly one fixed level-4
execution group and is sorted by maximum-level cell plus serving order. Every
stable serving leaf (level 6 or deeper) belongs wholly to one such group, so a
reducer fetches only its ancestor group's bounded fragments. Neither task nor
execution-group identities become serving shard IDs.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import struct
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from global_v2_places_inventory import (  # noqa: E402
    REGION,
    schema_contract_from_arrow,
    sha256_value,
    validate_inventory,
)
from experiment_places_compact_index import place_from_row  # noqa: E402
from experiment_places_locality_head import (  # noqa: E402
    HEAD_PREFIX_LENGTHS,
    place_terms,
)
from places_partition import (  # noqa: E402
    DEFAULT_MAXIMUM_LEVEL,
    DEFAULT_MINIMUM_LEVEL,
    morton_quadkey,
    point_morton,
    validate_levels,
)


MAP_REPORT_SCHEMA = "overture-global-v2-places-map-report-v3"
SUMMARY_ARTIFACT_SCHEMA = "overture-global-v2-places-map-summary-v1"
FRAGMENT_SCHEMA = "overture-global-v2-places-map-pack-v2"
SUPPORTED_OPERATING_STATUSES = frozenset({"open", "temporarily_closed"})
EXECUTION_GROUP_LEVEL = 4
DEFAULT_MAX_TASK_FRAGMENTS = 512
DEFAULT_TARGET_FRAGMENT_INPUT_BYTES = 256_000_000
DEFAULT_MAX_FRAGMENT_BYTES = 512_000_000
DEFAULT_MAX_FRAGMENT_INPUT_BYTES = 512_000_000
DEFAULT_FRAGMENT_ROWS = 1_000_000
DEFAULT_SORT_RUN_ROWS = 50_000
MAP_OUTPUT_BATCH_ROWS = 8_192
MAX_PACK_ROW_GROUP_INPUT_BYTES = 32_000_000
MAP_SUMMARY_FAMOUS_CAP = 1_024
MAX_MAP_SUMMARY_KEYS = 5_000_000
MAX_MAP_SUMMARY_KEY_BYTES = 512_000_000
REQUIRED_DUCKDB_VERSION = "1.5.1"
MAP_CENSUS_MEMORY_LIMIT_BYTES = 512_000_000
MAP_CENSUS_MAX_SCRATCH_BYTES = 10_000_000_000
DEFAULT_MAX_MAP_WORKSPACE_BYTES = 20_000_000_000
MAP_CENSUS_BATCH_ROWS = 8_192
SUMMARY_WRITE_BATCH_ROWS = 8_192

# This is an API: keep the order stable.  A row is assigned exactly the first
# applicable reason, so retained + sum(reasons) always equals source input.
REJECTION_PRECEDENCE = (
    "missing_gers_id",
    "invalid_gers_id",
    "missing_geometry",
    "non_point_geometry",
    "invalid_geometry",
    "nonfinite_coordinates",
    "coordinates_out_of_world",
    "blank_primary_name",
    "missing_operating_status",
    "unsupported_operating_status",
)


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _nested(value: Any, *parts: str) -> Any:
    current = value
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _uuid_id(value: Any) -> tuple[str | None, str | None]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "missing_gers_id"
    if not isinstance(value, (str, uuid.UUID)):
        return None, "invalid_gers_id"
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None, "invalid_gers_id"
    # UUID parsing accepts several alternate spellings; the serving identity is
    # emitted canonically, but is never synthesized from row position/content.
    return str(parsed), None


def _wkb_point(value: bytes) -> tuple[float, float] | str:
    if len(value) < 5:
        return "invalid_geometry"
    byte_order = value[0]
    if byte_order == 0:
        prefix = ">"
    elif byte_order == 1:
        prefix = "<"
    else:
        return "invalid_geometry"
    raw_type = struct.unpack_from(prefix + "I", value, 1)[0]
    # GeoParquet uses WKB. Reject dimensional/EWKB variants rather than
    # silently discarding dimensions or an embedded SRID.
    if raw_type != 1:
        # Both ISO SQL/MM dimensional offsets (1001/2001/3001) and EWKB's
        # high-bit flags still describe a Point. They are unsupported here and
        # therefore invalid, rather than falsely reported as another geometry.
        unflagged_type = raw_type & 0x0FFFFFFF
        base_type = unflagged_type % 1000 if unflagged_type >= 1000 else unflagged_type
        return "non_point_geometry" if base_type != 1 else "invalid_geometry"
    if len(value) != 21:
        return "invalid_geometry"
    longitude, latitude = struct.unpack_from(prefix + "dd", value, 5)
    return longitude, latitude


def _coordinates(value: Any) -> tuple[float, float] | str:
    if value is None:
        return "missing_geometry"
    if isinstance(value, (bytes, bytearray, memoryview)):
        parsed = _wkb_point(bytes(value))
        if isinstance(parsed, str):
            return parsed
        longitude, latitude = parsed
    elif isinstance(value, dict):
        if value.get("type") != "Point":
            return "non_point_geometry"
        coordinates = value.get("coordinates")
        if not isinstance(coordinates, (list, tuple)) or len(coordinates) != 2:
            return "invalid_geometry"
        longitude, latitude = coordinates
        if (
            isinstance(longitude, bool)
            or isinstance(latitude, bool)
            or not isinstance(longitude, (int, float))
            or not isinstance(latitude, (int, float))
        ):
            return "invalid_geometry"
        longitude = float(longitude)
        latitude = float(latitude)
    else:
        return "invalid_geometry"
    if not math.isfinite(longitude) or not math.isfinite(latitude):
        return "nonfinite_coordinates"
    if not -180.0 <= longitude <= 180.0 or not -90.0 <= latitude <= 90.0:
        return "coordinates_out_of_world"
    return longitude, latitude


def _common_names(value: Any, primary: str) -> str:
    if isinstance(value, dict):
        pairs = value.items()
    elif isinstance(value, (list, tuple)):
        pairs = value
    else:
        pairs = ()
    normalized: list[tuple[str, str]] = []
    for item in pairs:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        language, name = item
        language_text = _text(language)
        name_text = _text(name)
        if language_text and name_text and name_text != primary:
            normalized.append((language_text, name_text))
    return " ".join(name for _, name in sorted(set(normalized)))


def _first_address(value: Any) -> dict[str, Any]:
    if not isinstance(value, (list, tuple)):
        return {}
    return next((item for item in value if isinstance(item, dict)), {})


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.5
    result = float(value)
    if not math.isfinite(result):
        return 0.5
    return min(1.0, max(0.0, result))


def project_row(
    row: Any,
    *,
    maximum_level: int,
    source_uri: str,
    row_group: int,
    row_index: int,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return one canonical serving row or its first exclusive rejection."""

    if not isinstance(row, dict):
        # A decoder producing a non-object is malformed input, not a mutable
        # row-level value. Treat it as the earliest invalid identity reason.
        return None, "invalid_gers_id"
    place_id, reason = _uuid_id(row.get("id"))
    if reason is not None:
        return None, reason
    coordinates = _coordinates(row.get("geometry"))
    if isinstance(coordinates, str):
        return None, coordinates
    longitude, latitude = coordinates
    primary_name = _text(_nested(row, "names", "primary"))
    if not primary_name:
        return None, "blank_primary_name"
    raw_status = row.get("operating_status")
    if raw_status is None or (isinstance(raw_status, str) and not raw_status.strip()):
        return None, "missing_operating_status"
    if (
        not isinstance(raw_status, str)
        or raw_status.strip() not in SUPPORTED_OPERATING_STATUSES
    ):
        return None, "unsupported_operating_status"
    operating_status = raw_status.strip()
    morton = point_morton(longitude, latitude, maximum_level)
    maximum_cell = morton_quadkey(morton, maximum_level)
    address = _first_address(row.get("addresses"))
    common_names = _nested(row, "names", "common")
    return (
        {
            "gers_id": place_id,
            "primary_name": primary_name,
            "alt_names": _common_names(common_names, primary_name),
            "brand_name": _text(_nested(row, "brand", "names", "primary")),
            "category_primary": _text(_nested(row, "categories", "primary")),
            "basic_category": _text(row.get("basic_category")),
            "locality": _text(address.get("locality")),
            "region": _text(address.get("region")),
            "country": _text(address.get("country")),
            "lat": latitude,
            "lon": longitude,
            "confidence": _confidence(row.get("confidence")),
            "operating_status": operating_status,
            "partition_key": morton,
            "partition_cell": maximum_cell,
            "execution_group": maximum_cell[:EXECUTION_GROUP_LEVEL],
            "source_uri": source_uri,
            "source_row_group": row_group,
            "source_row_index": row_index,
        },
        None,
    )


def _fragment_sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    return (
        row["partition_key"],
        -round(row["confidence"] * 255),
        row["gers_id"],
    )


def _intermediate_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["partition_cell"],
        *_fragment_sort_key(row),
        row["source_uri"],
        row["source_row_group"],
        row["source_row_index"],
    )


class _WorkspaceBudget:
    """Observe and hard-limit every staged byte owned by one map task."""

    def __init__(self, staging: Path, maximum_bytes: int) -> None:
        if maximum_bytes < 1:
            raise ValueError("Places map workspace byte cap must be positive")
        staging.mkdir(parents=True, exist_ok=True)
        self.staging = staging
        self.maximum_bytes = maximum_bytes
        self.peak_bytes = 0
        self.peak_components: dict[str, int] = {}
        self.component_peak_bytes = {
            "census_database_bytes": 0,
            "census_spill_bytes": 0,
            "sort_database_bytes": 0,
            "sort_spill_bytes": 0,
            "staged_output_bytes": 0,
        }
        self.observations = 0
        self.observe()

    def _components(self) -> dict[str, int]:
        result = dict.fromkeys(self.component_peak_bytes, 0)
        for path in self.staging.rglob("*"):
            if not path.is_file():
                continue
            size = path.stat().st_size
            relative = path.relative_to(self.staging)
            if relative.parts[0] == "places-map-census-spill":
                kind = "census_spill_bytes"
            elif relative.parts[0] == "places-map-sort-spill":
                kind = "sort_spill_bytes"
            elif path.name.startswith("places-map-census.duckdb"):
                kind = "census_database_bytes"
            elif path.name.startswith("places-map-sort.duckdb"):
                kind = "sort_database_bytes"
            else:
                kind = "staged_output_bytes"
            result[kind] += size
        return result

    def observe(self) -> None:
        components = self._components()
        total = sum(components.values())
        self.observations += 1
        for kind, size in components.items():
            self.component_peak_bytes[kind] = max(
                self.component_peak_bytes[kind], size
            )
        if total > self.peak_bytes:
            self.peak_bytes = total
            self.peak_components = components
        if total > self.maximum_bytes:
            raise ValueError("Places map exceeded its hard combined workspace cap")

    def evidence(self) -> dict[str, Any]:
        return {
            "kind": "combined-map-workspace-hard-cap-v1",
            "maximum_bytes": self.maximum_bytes,
            "peak_bytes": self.peak_bytes,
            "peak_components": self.peak_components,
            "component_peak_bytes": self.component_peak_bytes,
            "observations": self.observations,
            "includes": [
                "census-database",
                "census-spill",
                "sort-database",
                "sort-spill",
                "staged-fragment-and-summary-output",
            ],
        }


class _IntermediateRowStore:
    """Pinned DuckDB external sort fed only by bounded typed Arrow batches."""

    def __init__(
        self,
        staging: Path,
        *,
        workspace: _WorkspaceBudget,
        run_rows: int = DEFAULT_SORT_RUN_ROWS,
    ) -> None:
        staging.mkdir(parents=True, exist_ok=True)
        if run_rows < 1:
            raise ValueError("Places DuckDB input batch must contain at least one row")
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - hosted dependency boundary
            raise RuntimeError("Places typed map sorting requires DuckDB") from exc
        if duckdb.__version__ != REQUIRED_DUCKDB_VERSION:
            raise RuntimeError(
                "Places typed map sorting requires DuckDB "
                f"{REQUIRED_DUCKDB_VERSION}, found {duckdb.__version__}"
            )
        self.staging = staging
        self.workspace = workspace
        self.run_rows = run_rows
        self.pending: list[dict[str, Any]] = []
        self.peak_pending_rows = 0
        self.insert_batches = 0
        self.path = staging / "places-map-sort.duckdb"
        self.temp_directory = staging / "places-map-sort-spill"
        self.temp_directory.mkdir(exist_ok=True)
        self.connection = duckdb.connect(str(self.path))
        self.connection.execute("SET threads = 1")
        self.connection.execute("SET preserve_insertion_order = false")
        self.connection.execute(
            "SET max_memory = ?", [f"{MAP_CENSUS_MEMORY_LIMIT_BYTES}B"]
        )
        self.connection.execute("SET temp_directory = ?", [str(self.temp_directory)])
        self.connection.execute(
            "SET max_temp_directory_size = ?",
            [f"{MAP_CENSUS_MAX_SCRATCH_BYTES}B"],
        )
        fields = [
            f'"{field.name}" {_duckdb_type(field.type)} NOT NULL'
            for field in _fragment_arrow_schema()
        ]
        fields.extend(
            ["confidence_rank SMALLINT NOT NULL", "normalized_bytes BIGINT NOT NULL"]
        )
        self.connection.execute(f"CREATE TABLE sorted_rows ({', '.join(fields)})")
        self.workspace.observe()

    def add(self, row: dict[str, Any]) -> None:
        normalized_bytes = sum(
            len(item.encode("utf-8")) if isinstance(item, str) else 8
            for item in row.values()
        )
        self.pending.append(
            {
                **row,
                "confidence_rank": -round(row["confidence"] * 255),
                "normalized_bytes": normalized_bytes,
            }
        )
        if len(self.pending) >= self.run_rows:
            self._flush()

    def _flush(self) -> None:
        if not self.pending:
            return
        try:
            import pyarrow as pa
        except ImportError as exc:  # pragma: no cover - hosted dependency boundary
            raise RuntimeError("Places typed map sorting requires pyarrow") from exc
        schema = _fragment_arrow_schema().append(
            pa.field("confidence_rank", pa.int16())
        ).append(pa.field("normalized_bytes", pa.int64()))
        table = pa.Table.from_pylist(self.pending, schema=schema)
        self.peak_pending_rows = max(self.peak_pending_rows, len(self.pending))
        self.connection.register("map_sort_batch", table)
        try:
            self.connection.execute("INSERT INTO sorted_rows SELECT * FROM map_sort_batch")
        finally:
            self.connection.unregister("map_sort_batch")
        self.insert_batches += 1
        self.pending = []
        self.workspace.observe()

    def finish(self) -> None:
        self._flush()
        self.connection.execute("CHECKPOINT")
        self.workspace.observe()

    def ordered_reader(self) -> Any:
        """Return the one and only task-wide physical ordering stream."""

        columns = ", ".join(f'"{field.name}"' for field in _fragment_arrow_schema())
        reader = self.connection.execute(
            f"SELECT {columns} FROM sorted_rows "
            "ORDER BY execution_group, partition_cell, partition_key, "
            "confidence_rank, gers_id, source_uri, source_row_group, "
            "source_row_index"
        ).to_arrow_reader(batch_size=MAP_OUTPUT_BATCH_ROWS)
        self.workspace.observe()
        return reader

    def evidence(self) -> dict[str, Any]:
        return {
            "kind": "duckdb-arrow-batch-external-sort-v1",
            "engine": "duckdb",
            "engine_version": REQUIRED_DUCKDB_VERSION,
            "maximum_memory_bytes": MAP_CENSUS_MEMORY_LIMIT_BYTES,
            "maximum_scratch_bytes": MAP_CENSUS_MAX_SCRATCH_BYTES,
            "maximum_batch_rows": self.run_rows,
            "peak_pending_rows": self.peak_pending_rows,
            "insert_batches": self.insert_batches,
            "registered_arrow_batches": True,
            "python_sorted_runs": False,
            "python_heap_merge": False,
            "threads": 1,
            "preserve_insertion_order": False,
        }

    def close(self) -> None:
        self.connection.close()
        self.path.unlink(missing_ok=True)
        Path(f"{self.path}.wal").unlink(missing_ok=True)
        for path in sorted(self.temp_directory.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                with contextlib.suppress(OSError):
                    path.rmdir()
        with contextlib.suppress(OSError):
            self.temp_directory.rmdir()


def _fragment_arrow_schema(metadata: dict[str, str] | None = None) -> Any:
    try:
        import pyarrow as pa
    except ImportError as exc:  # pragma: no cover - hosted dependency boundary
        raise RuntimeError("Places Parquet fragment writing requires pyarrow") from exc
    return pa.schema(
        [
            ("gers_id", pa.string()),
            ("primary_name", pa.string()),
            ("alt_names", pa.string()),
            ("brand_name", pa.string()),
            ("category_primary", pa.string()),
            ("basic_category", pa.string()),
            ("locality", pa.string()),
            ("region", pa.string()),
            ("country", pa.string()),
            ("lat", pa.float64()),
            ("lon", pa.float64()),
            ("confidence", pa.float64()),
            ("operating_status", pa.string()),
            ("partition_key", pa.uint64()),
            ("partition_cell", pa.string()),
            ("execution_group", pa.string()),
            ("source_uri", pa.string()),
            ("source_row_group", pa.int32()),
            ("source_row_index", pa.int64()),
        ],
        metadata=(
            {key.encode(): value.encode() for key, value in sorted(metadata.items())}
            if metadata is not None
            else None
        ),
    )


def _default_fragment_writer(
    batches: Iterable[Any], path: Path, metadata: dict[str, str]
) -> int:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - hosted dependency boundary
        raise RuntimeError("Places Parquet fragment writing requires pyarrow") from exc
    schema = _fragment_arrow_schema(metadata)
    records = 0
    with pq.ParquetWriter(
        path,
        schema,
        compression="zstd",
        compression_level=9,
        use_dictionary=True,
        write_statistics=True,
    ) as writer:
        for batch in batches:
            if batch.schema != schema.remove_metadata():
                batch = pa.RecordBatch.from_arrays(batch.columns, schema=schema.remove_metadata())
            writer.write_batch(batch)
            records += batch.num_rows
    return records


def _row_sort_key_from_columns(batch: Any, index: int) -> list[Any]:
    values = {
        name: batch.column(batch.schema.get_field_index(name))[index].as_py()
        for name in (
            "partition_cell",
            "partition_key",
            "confidence",
            "gers_id",
            "source_uri",
            "source_row_group",
            "source_row_index",
        )
    }
    return [
        values["partition_cell"],
        values["partition_key"],
        -round(values["confidence"] * 255),
        values["gers_id"],
        values["source_uri"],
        values["source_row_group"],
        values["source_row_index"],
    ]


def _split_batch_at_execution_groups(batch: Any) -> Iterator[Any]:
    """Yield non-empty ordered slices which each belong to exactly one group."""

    groups = batch.column(batch.schema.get_field_index("execution_group"))
    start = 0
    for index in range(1, batch.num_rows):
        if groups[index].as_py() != groups[index - 1].as_py():
            yield batch.slice(start, index - start)
            start = index
    if start < batch.num_rows:
        yield batch.slice(start)


def _normalized_row_bytes(row: dict[str, Any]) -> int:
    return sum(
        len(value.encode("utf-8")) if isinstance(value, str) else 8
        for value in row.values()
    )


def _row_group_semantic_sha256(rows: Iterable[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )
    return digest.hexdigest()


def _bounded_group_slices(
    batch: Any, *, row_limit: int, input_byte_limit: int
) -> Iterator[tuple[Any, int]]:
    """Split one execution group into deterministic, bounded Parquet row groups."""

    hard_slice_bytes = min(input_byte_limit, MAX_PACK_ROW_GROUP_INPUT_BYTES)
    rows = batch.to_pylist()
    start = 0
    pending_bytes = 0
    for index, row in enumerate(rows):
        row_bytes = _normalized_row_bytes(row)
        if row_bytes > input_byte_limit:
            raise ValueError("one Places row exceeds the pack input byte cap")
        if index > start and (
            index - start >= row_limit or pending_bytes + row_bytes > hard_slice_bytes
        ):
            yield batch.slice(start, index - start), pending_bytes
            start = index
            pending_bytes = 0
        pending_bytes += row_bytes
    if start < batch.num_rows:
        yield batch.slice(start), pending_bytes


def _physical_pack_target_reached(
    *, physical_bytes: int, target_bytes: int
) -> bool:
    """Packing is governed only by physical bytes, never logical compressibility."""

    return physical_bytes >= target_bytes


def _footer_binding_for_pack(
    path: Path,
) -> tuple[list[dict[str, int]], str, int]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Places Parquet pack inspection requires pyarrow") from exc

    parquet = pq.ParquetFile(path)
    physical: list[dict[str, int]] = []
    for index in range(parquet.metadata.num_row_groups):
        group = parquet.metadata.row_group(index)
        physical.append(
            {
                "index": index,
                "records": group.num_rows,
                "compressed_bytes": sum(
                    group.column(column).total_compressed_size
                    for column in range(group.num_columns)
                ),
                "uncompressed_bytes": sum(
                    group.column(column).total_uncompressed_size
                    for column in range(group.num_columns)
                ),
            }
        )
    binding_groups = []
    for index in range(parquet.metadata.num_row_groups):
        group = parquet.metadata.row_group(index)
        binding_groups.append(
            {
                "index": index,
                "records": group.num_rows,
                "total_byte_size": group.total_byte_size,
                "compressed_column_bytes": sum(
                    group.column(column).total_compressed_size
                    for column in range(group.num_columns)
                ),
                "columns": [
                    {
                        "path": group.column(column).path_in_schema,
                        "compressed_bytes": group.column(column).total_compressed_size,
                        "uncompressed_bytes": group.column(column).total_uncompressed_size,
                        "data_page_offset": group.column(column).data_page_offset,
                        "dictionary_page_offset": group.column(column).dictionary_page_offset,
                    }
                    for column in range(group.num_columns)
                ],
            }
        )
    footer = {
        "created_by": parquet.metadata.created_by,
        "format_version": parquet.metadata.format_version,
        "serialized_size": parquet.metadata.serialized_size,
        "records": parquet.metadata.num_rows,
        "row_groups": parquet.metadata.num_row_groups,
        "columns": parquet.metadata.num_columns,
        "schema_sha256": hashlib.sha256(
            str(parquet.schema_arrow.remove_metadata()).encode()
        ).hexdigest(),
        "groups": binding_groups,
    }
    footer_bytes = (
        json.dumps(footer, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    return (
        physical,
        hashlib.sha256(footer_bytes).hexdigest(),
        parquet.metadata.serialized_size,
    )


def _write_ordered_packs(
    *,
    store: _IntermediateRowStore,
    output_dir: Path,
    inventory_sha256: str,
    task_digest: str,
    writer: Callable[[Iterable[Any], Path, dict[str, str]], int],
    target_bytes: int,
    maximum_bytes: int,
    maximum_input_bytes: int,
    row_limit: int,
    maximum_packs: int,
    workspace: _WorkspaceBudget,
) -> list[dict[str, Any]]:
    """Consume one task-wide ordered stream into coarse, group-aligned packs."""

    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Places Parquet pack writing requires pyarrow") from exc

    staging = output_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    production_writer = writer is _default_fragment_writer
    packs: list[dict[str, Any]] = []
    temporary: Path | None = None
    parquet_writer: Any | None = None
    buffered: list[Any] = []
    ownership: list[dict[str, Any]] = []
    pack_records = 0
    normalized_bytes = 0

    metadata = {
        "artifact_schema": FRAGMENT_SCHEMA,
        "inventory_sha256": inventory_sha256,
        "map_task_digest": task_digest,
        "execution_group_level": str(EXECUTION_GROUP_LEVEL),
        "physical_order": (
            "execution_group,partition_cell,partition_key,confidence_rank,gers_id,"
            "source_uri,source_row_group,source_row_index"
        ),
        "row_groups_cross_execution_groups": "false",
    }
    metadata["overture.places_pack_header"] = json.dumps(
        {
            "artifact_schema": FRAGMENT_SCHEMA,
            "inventory_sha256": inventory_sha256,
            "map_task_digest": task_digest,
            "execution_group_level": EXECUTION_GROUP_LEVEL,
            "physical_order": metadata["physical_order"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    def begin() -> None:
        nonlocal temporary, parquet_writer
        descriptor, name = tempfile.mkstemp(
            prefix=f"task-{task_digest[:12]}-pack-", suffix=".parquet", dir=staging
        )
        os.close(descriptor)
        temporary = Path(name)
        if production_writer:
            parquet_writer = pq.ParquetWriter(
                temporary,
                _fragment_arrow_schema(metadata),
                compression="zstd",
                compression_level=9,
                use_dictionary=True,
                write_statistics=True,
            )

    def close() -> None:
        nonlocal temporary, parquet_writer, buffered, ownership, pack_records
        nonlocal normalized_bytes
        if temporary is None or pack_records == 0:
            return
        if production_writer:
            assert parquet_writer is not None
            parquet_writer.close()
            parquet_writer = None
            physical, footer_sha256, footer_bytes = _footer_binding_for_pack(temporary)
        else:
            actual = writer(iter(buffered), temporary, metadata)
            if actual != pack_records:
                raise AssertionError("Places streamed pack row count changed")
            size_for_groups = max(1, temporary.stat().st_size // len(ownership))
            physical = [
                {
                    "index": index,
                    "records": item["records"],
                    "compressed_bytes": size_for_groups,
                    "uncompressed_bytes": item["normalized_input_bytes"],
                }
                for index, item in enumerate(ownership)
            ]
            footer_sha256 = sha256_value(
                {"fixture": True, "records": pack_records, "row_groups": physical}
            )
            footer_bytes = min(temporary.stat().st_size, 4_096)
        workspace.observe()
        digest, size = sha256_file(temporary)
        if size > maximum_bytes:
            raise ValueError("Places physical pack exceeds its hard byte cap")
        if len(physical) != len(ownership):
            raise AssertionError("Places pack footer row groups differ from ownership")
        row_groups = []
        for expected_index, (semantic, actual) in enumerate(zip(ownership, physical, strict=True)):
            if actual["index"] != expected_index or actual["records"] != semantic["records"]:
                raise AssertionError("Places pack row-group records differ")
            bound = {
                **semantic,
                "index": expected_index,
                "compressed_bytes": actual["compressed_bytes"],
                "uncompressed_bytes": actual["uncompressed_bytes"],
            }
            row_groups.append({
                **bound,
                "ownership_layout_sha256": sha256_value(
                    {"pack_sha256": digest, **bound}
                ),
            })
        relative = Path("fragments") / "sha256" / f"{digest}.parquet"
        _install_content_file(temporary, output_dir / relative)
        workspace.observe()
        packs.append(
            {
                "object_key": relative.as_posix(),
                "sha256": digest,
                "bytes": size,
                "records": pack_records,
                "row_group_count": len(row_groups),
                "row_groups": row_groups,
                "execution_groups": sorted({item["execution_group"] for item in row_groups}),
                "minimum_sort_key": row_groups[0]["minimum_sort_key"],
                "maximum_sort_key": row_groups[-1]["maximum_sort_key"],
                "footer_sha256": footer_sha256,
                "footer_bytes": footer_bytes,
            }
        )
        temporary = None
        buffered = []
        ownership = []
        pack_records = 0
        normalized_bytes = 0

    begin()
    previous_maximum: list[Any] | None = None
    try:
        for source_batch in store.ordered_reader():
            for batch in _split_batch_at_execution_groups(source_batch):
                for group_batch, group_input_bytes in _bounded_group_slices(
                    batch,
                    row_limit=row_limit,
                    input_byte_limit=maximum_input_bytes,
                ):
                    if temporary is None:
                        if len(packs) >= maximum_packs:
                            raise ValueError(
                                "Places task exceeded its hard content-pack count cap"
                            )
                        begin()
                    minimum = _row_sort_key_from_columns(group_batch, 0)
                    maximum = _row_sort_key_from_columns(group_batch, group_batch.num_rows - 1)
                    if previous_maximum is not None and minimum < previous_maximum:
                        raise AssertionError("Places task-wide ordered stream regressed")
                    previous_maximum = maximum
                    group = group_batch.column(
                        group_batch.schema.get_field_index("execution_group")
                    )[0].as_py()
                    cells = group_batch.column(
                        group_batch.schema.get_field_index("partition_cell")
                    )
                    ownership.append(
                        {
                            "execution_group": group,
                            "minimum_maximum_level_cell": cells[0].as_py(),
                            "maximum_maximum_level_cell": cells[group_batch.num_rows - 1].as_py(),
                            "records": group_batch.num_rows,
                            "normalized_input_bytes": group_input_bytes,
                            "minimum_sort_key": minimum,
                            "maximum_sort_key": maximum,
                            "semantic_sha256": _row_group_semantic_sha256(
                                group_batch.to_pylist()
                            ),
                        }
                    )
                    pack_records += group_batch.num_rows
                    normalized_bytes += group_input_bytes
                    if production_writer:
                        schema = _fragment_arrow_schema(metadata)
                        table = pa.Table.from_batches([group_batch], schema=schema.remove_metadata())
                        parquet_writer.write_table(
                            table.replace_schema_metadata(schema.metadata),
                            row_group_size=max(1, group_batch.num_rows),
                        )
                    else:
                        buffered.append(group_batch)
                    workspace.observe()
                    physical_size = temporary.stat().st_size if production_writer else normalized_bytes
                    if _physical_pack_target_reached(
                        physical_bytes=physical_size, target_bytes=target_bytes
                    ):
                        close()
        close()
        if temporary is not None and pack_records == 0:
            if parquet_writer is not None:
                parquet_writer.close()
                parquet_writer = None
            temporary.unlink(missing_ok=True)
            temporary = None
    except BaseException:
        if parquet_writer is not None:
            parquet_writer.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    if len(packs) > maximum_packs:
        raise ValueError("Places task exceeded its hard content-pack count cap")
    if production_writer and any(
        pack["bytes"] < target_bytes // 2 for pack in packs[:-1]
    ):
        raise AssertionError("non-tail Places physical pack is below its lower bound")
    return packs


def _install_content_file(staged: Path, destination: Path) -> tuple[str, int]:
    digest, size = sha256_file(staged)
    if not destination.name.startswith(f"{digest}."):
        raise ValueError("content-addressed destination differs from staged digest")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        existing_digest, existing_size = sha256_file(destination)
        if existing_digest != digest or existing_size != size:
            raise ValueError(
                f"existing content-addressed object is corrupt: {destination}"
            )
        staged.unlink()
    else:
        os.replace(staged, destination)
    return digest, size


def _summary_candidate_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
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


def _summary_schema(metadata: dict[str, str] | None = None) -> Any:
    try:
        import pyarrow as pa
    except ImportError as exc:  # pragma: no cover - hosted dependency boundary
        raise RuntimeError("Places map summary requires pyarrow") from exc
    return pa.schema(
        [
            ("kind", pa.string()),
            ("key", pa.string()),
            ("records", pa.int64()),
            *_fragment_arrow_schema(),
        ],
        metadata=(
            {key.encode(): value.encode() for key, value in sorted(metadata.items())}
            if metadata is not None
            else None
        ),
    )


class _SummaryCensusStore:
    """Typed, bounded task census backed by pinned DuckDB from its first batch."""

    COUNT_TABLES = {
        "cell": "cell_counts",
        "exact": "exact_counts",
        "prefix": "prefix_counts",
    }

    def __init__(self, staging: Path, *, workspace: _WorkspaceBudget) -> None:
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - hosted dependency boundary
            raise RuntimeError("Places map census requires DuckDB") from exc
        if duckdb.__version__ != REQUIRED_DUCKDB_VERSION:
            raise RuntimeError(
                "Places map census requires DuckDB "
                f"{REQUIRED_DUCKDB_VERSION}, found {duckdb.__version__}"
            )
        staging.mkdir(parents=True, exist_ok=True)
        self.staging = staging
        self.workspace = workspace
        self.path = staging / "places-map-census.duckdb"
        self.temp_directory = staging / "places-map-census-spill"
        self.temp_directory.mkdir(exist_ok=True)
        self.connection = duckdb.connect(str(self.path))
        self.connection.execute("SET threads = 1")
        self.connection.execute("SET preserve_insertion_order = false")
        self.connection.execute(
            "SET max_memory = ?", [f"{MAP_CENSUS_MEMORY_LIMIT_BYTES}B"]
        )
        self.connection.execute(
            "SET temp_directory = ?", [str(self.temp_directory)]
        )
        self.connection.execute(
            "SET max_temp_directory_size = ?",
            [f"{MAP_CENSUS_MAX_SCRATCH_BYTES}B"],
        )
        for table in self.COUNT_TABLES.values():
            self.connection.execute(
                f"CREATE TABLE {table} (value VARCHAR PRIMARY KEY, records BIGINT NOT NULL)"
            )
        projection_columns = ", ".join(
            f'"{field.name}" {_duckdb_type(field.type)} NOT NULL'
            for field in _fragment_arrow_schema()
        )
        self.connection.execute(
            f"""
            CREATE TABLE famous (
                sequence BIGINT PRIMARY KEY,
                confidence_rank SMALLINT NOT NULL,
                {projection_columns}
            )
            """
        )
        self.pending_counts: dict[str, list[str]] = {
            kind: [] for kind in self.COUNT_TABLES
        }
        self.pending_famous: list[dict[str, Any]] = []
        self.peak_pending_count_rows = 0
        self.peak_pending_famous_rows = 0
        self.workspace.observe()

    def add(
        self,
        row: dict[str, Any],
        terms: Iterable[str],
        *,
        sequence: int,
    ) -> None:
        self._add_count("cell", row["partition_cell"])
        for token in sorted(terms, key=lambda value: value.encode("utf-8")):
            self._add_count("exact", token)
            for length in HEAD_PREFIX_LENGTHS:
                if len(token) >= length:
                    self._add_count("prefix", token[:length])
        self.pending_famous.append(
            {
                "sequence": sequence,
                "confidence_rank": -round(row["confidence"] * 255),
                **row,
            }
        )
        self.peak_pending_famous_rows = max(
            self.peak_pending_famous_rows, len(self.pending_famous)
        )
        if len(self.pending_famous) >= MAP_CENSUS_BATCH_ROWS:
            self._flush_famous()

    def _add_count(self, kind: str, value: str) -> None:
        pending = self.pending_counts[kind]
        pending.append(value)
        self.peak_pending_count_rows = max(
            self.peak_pending_count_rows, len(pending)
        )
        if len(pending) >= MAP_CENSUS_BATCH_ROWS:
            self._flush_counts(kind)

    def _flush_counts(self, kind: str) -> None:
        values = self.pending_counts[kind]
        if not values:
            return
        try:
            import pyarrow as pa
        except ImportError as exc:  # pragma: no cover - hosted dependency boundary
            raise RuntimeError("Places map census requires pyarrow") from exc
        batch = pa.table({"value": pa.array(values, type=pa.string())})
        self.connection.register("map_count_batch", batch)
        table = self.COUNT_TABLES[kind]
        try:
            self.connection.execute(
                f"""
                INSERT INTO {table}(value, records)
                SELECT value, count(*) FROM map_count_batch GROUP BY value
                ON CONFLICT(value) DO UPDATE
                SET records = {table}.records + excluded.records
                """
            )
        finally:
            self.connection.unregister("map_count_batch")
        self.pending_counts[kind] = []
        self.workspace.observe()

    def _flush_famous(self) -> None:
        if not self.pending_famous:
            return
        try:
            import pyarrow as pa
        except ImportError as exc:  # pragma: no cover - hosted dependency boundary
            raise RuntimeError("Places map census requires pyarrow") from exc
        schema = pa.schema(
            [
                ("sequence", pa.int64()),
                ("confidence_rank", pa.int16()),
                *_fragment_arrow_schema(),
            ]
        )
        batch = pa.Table.from_pylist(self.pending_famous, schema=schema)
        self.connection.register("map_famous_batch", batch)
        try:
            self.connection.execute("INSERT INTO famous SELECT * FROM map_famous_batch")
            self.connection.execute(
                """
                DELETE FROM famous
                USING (
                    SELECT sequence, row_number() OVER (
                        PARTITION BY gers_id
                        ORDER BY confidence_rank, partition_key, gers_id,
                            source_uri, source_row_group, source_row_index
                    ) AS occurrence_position
                    FROM famous
                ) duplicate
                WHERE famous.sequence = duplicate.sequence
                  AND duplicate.occurrence_position > 1
                """
            )
            self.connection.execute(
                f"""
                DELETE FROM famous
                USING (
                    SELECT sequence, row_number() OVER (
                        ORDER BY confidence_rank, partition_key, gers_id,
                            source_uri, source_row_group, source_row_index
                    ) AS position
                    FROM famous
                ) discarded
                WHERE famous.sequence = discarded.sequence
                  AND discarded.position > {MAP_SUMMARY_FAMOUS_CAP}
                """
            )
        finally:
            self.connection.unregister("map_famous_batch")
        self.pending_famous = []
        self.workspace.observe()

    def finish(self) -> None:
        for kind in self.COUNT_TABLES:
            self._flush_counts(kind)
        self._flush_famous()
        self.connection.execute("CHECKPOINT")
        self.workspace.observe()
        if self.scratch_bytes() > MAP_CENSUS_MAX_SCRATCH_BYTES:
            raise ValueError("Places map census exceeded its hard scratch cap")

    def scratch_bytes(self) -> int:
        return sum(
            path.stat().st_size for path in self.staging.rglob("*") if path.is_file()
        )

    def statistics(self) -> dict[str, int]:
        result: dict[str, int] = {}
        key_bytes = 0
        for kind, table in self.COUNT_TABLES.items():
            keys, records, encoded_bytes = self.connection.execute(
                f"""
                SELECT count(*), coalesce(sum(records), 0),
                    coalesce(sum(octet_length(encode(value))), 0)
                FROM {table}
                """
            ).fetchone()
            result[f"{kind}_keys"] = int(keys)
            result[f"{kind}_records"] = int(records)
            key_bytes += int(encoded_bytes)
        result["famous_candidates"] = int(
            self.connection.execute("SELECT count(*) FROM famous").fetchone()[0]
        )
        result["key_bytes"] = key_bytes
        result["summary_rows"] = (
            result["cell_keys"]
            + result["exact_keys"]
            + result["prefix_keys"]
            + result["famous_candidates"]
        )
        return result

    def evidence(self) -> dict[str, Any]:
        return {
            "kind": "duckdb-typed-bounded-task-census-v1",
            "engine": "duckdb",
            "engine_version": REQUIRED_DUCKDB_VERSION,
            "maximum_memory_bytes": MAP_CENSUS_MEMORY_LIMIT_BYTES,
            "maximum_scratch_bytes": MAP_CENSUS_MAX_SCRATCH_BYTES,
            "scratch_bytes": self.scratch_bytes(),
            "maximum_batch_rows": MAP_CENSUS_BATCH_ROWS,
            "peak_pending_count_rows": self.peak_pending_count_rows,
            "peak_pending_famous_rows": self.peak_pending_famous_rows,
            "famous_candidate_cap": MAP_SUMMARY_FAMOUS_CAP,
            "famous_candidate_identity": "gers_id",
            "famous_deduplicate_before_cap": True,
            "famous_best_occurrence_order": [
                "confidence-rank-descending",
                "partition-key-ascending",
                "gers-id-ascending",
                "source-uri-ascending",
                "source-row-group-ascending",
                "source-row-index-ascending",
            ],
        }

    def iter_rows(self) -> Iterator[dict[str, Any]]:
        empty_projection = {name: None for name in _fragment_arrow_schema().names}
        for kind, table in self.COUNT_TABLES.items():
            cursor = self.connection.execute(
                f"SELECT value, records FROM {table} ORDER BY value"
            )
            while rows := cursor.fetchmany(SUMMARY_WRITE_BATCH_ROWS):
                for key, records in rows:
                    yield {
                        "kind": kind,
                        "key": key,
                        "records": records,
                        **empty_projection,
                    }
        fields = _fragment_arrow_schema().names
        cursor = self.connection.execute(
            f"""
            SELECT {', '.join(f'"{field}"' for field in fields)}
            FROM famous
            ORDER BY confidence_rank, partition_key, gers_id,
                source_uri, source_row_group, source_row_index
            """
        )
        while rows := cursor.fetchmany(SUMMARY_WRITE_BATCH_ROWS):
            for values in rows:
                row = dict(zip(fields, values, strict=True))
                yield {"kind": "famous", "key": row["gers_id"], "records": 1, **row}

    def close(self) -> None:
        self.connection.close()
        self.path.unlink(missing_ok=True)
        for path in sorted(self.temp_directory.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.temp_directory.rmdir()


def _duckdb_type(arrow_type: Any) -> str:
    try:
        import pyarrow as pa
    except ImportError as exc:  # pragma: no cover - hosted dependency boundary
        raise RuntimeError("Places map census requires pyarrow") from exc
    if pa.types.is_string(arrow_type):
        return "VARCHAR"
    if pa.types.is_float64(arrow_type):
        return "DOUBLE"
    if pa.types.is_uint64(arrow_type):
        return "UBIGINT"
    if pa.types.is_int32(arrow_type):
        return "INTEGER"
    if pa.types.is_int64(arrow_type):
        return "BIGINT"
    raise TypeError(f"unsupported Places census field type: {arrow_type}")


def _write_summary(
    *,
    census: _SummaryCensusStore,
    output_dir: Path,
    maximum_level: int,
    inventory_sha256: str,
    task_digest: str,
    workspace: _WorkspaceBudget,
) -> dict[str, Any]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - hosted dependency boundary
        raise RuntimeError("Places map summary requires pyarrow") from exc
    statistics = census.statistics()
    total_keys = (
        statistics["cell_keys"]
        + statistics["exact_keys"]
        + statistics["prefix_keys"]
    )
    total_key_bytes = statistics["key_bytes"]
    if total_keys > MAX_MAP_SUMMARY_KEYS or total_key_bytes > MAX_MAP_SUMMARY_KEY_BYTES:
        raise ValueError("Places task summary exceeded its bounded key census")
    staging = output_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="summary-", suffix=".parquet", dir=staging
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        metadata = {
            "artifact_schema": SUMMARY_ARTIFACT_SCHEMA,
            "maximum_level": str(maximum_level),
            "inventory_sha256": inventory_sha256,
            "map_task_digest": task_digest,
            "tokenizer_source": "frozen-python-places-v3",
            "prefix_lengths": ",".join(map(str, HEAD_PREFIX_LENGTHS)),
            "famous_candidate_cap": str(MAP_SUMMARY_FAMOUS_CAP),
            "famous_candidate_identity": "gers_id",
            "famous_deduplicate_before_cap": "true",
            "cell_rows": str(statistics["cell_keys"]),
            "cell_records": str(statistics["cell_records"]),
            "exact_rows": str(statistics["exact_keys"]),
            "prefix_rows": str(statistics["prefix_keys"]),
            "famous_rows": str(statistics["famous_candidates"]),
            "summary_rows": str(statistics["summary_rows"]),
            "key_bytes": str(total_key_bytes),
        }
        schema = _summary_schema(metadata)
        writer = pq.ParquetWriter(
            temporary,
            schema,
            compression="zstd",
            compression_level=9,
            use_dictionary=True,
            write_statistics=True,
        )
        pending: list[dict[str, Any]] = []
        try:
            for row in census.iter_rows():
                pending.append(row)
                if len(pending) == SUMMARY_WRITE_BATCH_ROWS:
                    writer.write_table(pa.Table.from_pylist(pending, schema=schema))
                    pending = []
                    workspace.observe()
            if pending:
                writer.write_table(pa.Table.from_pylist(pending, schema=schema))
                workspace.observe()
        finally:
            writer.close()
        workspace.observe()
        parquet = pq.ParquetFile(temporary)
        if parquet.metadata.num_rows != statistics["summary_rows"]:
            raise AssertionError("Places summary physical rows do not reconcile")
        digest, size = sha256_file(temporary)
        relative = Path("summaries") / "sha256" / f"{digest}.parquet"
        destination = output_dir / relative
        installed_digest, installed_size = _install_content_file(temporary, destination)
        workspace.observe()
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if installed_digest != digest or installed_size != size:
        raise AssertionError("Places count content installation changed its bytes")
    return {
        "object_key": relative.as_posix(),
        "sha256": digest,
        "bytes": size,
        "cells": statistics["cell_keys"],
        "records": statistics["cell_records"],
        "exact_keys": statistics["exact_keys"],
        "prefix_keys": statistics["prefix_keys"],
        "famous_candidates": statistics["famous_candidates"],
        "famous_candidate_cap": MAP_SUMMARY_FAMOUS_CAP,
        "key_bytes": total_key_bytes,
        "maximum_level": maximum_level,
        "format": "parquet",
        "schema": SUMMARY_ARTIFACT_SCHEMA,
    }


def read_map_summary(path: Path) -> Iterator[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - hosted dependency boundary
        raise RuntimeError("Places map summary requires pyarrow") from exc
    parquet = pq.ParquetFile(path)
    metadata = {
        key.decode(): value.decode()
        for key, value in (parquet.schema_arrow.metadata or {}).items()
    }
    if metadata.get("artifact_schema") != SUMMARY_ARTIFACT_SCHEMA:
        raise ValueError("Places map summary schema is invalid")
    previous: tuple[str, bytes, tuple[Any, ...]] | None = None
    for batch in parquet.iter_batches(batch_size=16_384, use_threads=False):
        for value in batch.to_pylist():
            kind = value.get("kind")
            key = value.get("key")
            records = value.get("records")
            if kind not in {"cell", "exact", "prefix", "famous"} or not isinstance(key, str):
                raise ValueError("Places map summary row kind/key is invalid")
            candidate_order = (
                _summary_candidate_sort_key(value) if kind == "famous" else ()
            )
            kind_order = {"cell": 0, "exact": 1, "prefix": 2, "famous": 3}[kind]
            order = (
                kind_order,
                b"" if kind == "famous" else key.encode("utf-8"),
                candidate_order,
            )
            if previous is not None and order <= previous:
                raise ValueError("Places map summary rows are not strictly ordered")
            if type(records) is not int or records <= 0:
                raise ValueError("Places map summary record count is invalid")
            previous = order
            yield value


def read_maximum_level_counts(path: Path) -> Iterator[tuple[str, int]]:
    for value in read_map_summary(path):
        if value["kind"] == "cell":
            yield value["key"], value["records"]


BatchReader = Callable[
    [dict[str, Any], dict[str, Any]], Iterable[tuple[int, int, dict[str, Any]]]
]


def run_map_task(
    inventory_value: Any,
    *,
    task_index: int,
    output_dir: Path,
    batch_reader: BatchReader,
    fragment_writer: Callable[
        [list[dict[str, Any]], Path, dict[str, str]], None
    ] = _default_fragment_writer,
    maximum_level: int = DEFAULT_MAXIMUM_LEVEL,
    fragment_rows: int = DEFAULT_FRAGMENT_ROWS,
    target_fragment_input_bytes: int | None = None,
    max_fragment_input_bytes: int = DEFAULT_MAX_FRAGMENT_INPUT_BYTES,
    max_fragment_bytes: int = DEFAULT_MAX_FRAGMENT_BYTES,
    max_task_fragments: int = DEFAULT_MAX_TASK_FRAGMENTS,
    max_workspace_bytes: int = DEFAULT_MAX_MAP_WORKSPACE_BYTES,
) -> dict[str, Any]:
    inventory = validate_inventory(inventory_value)
    validate_levels(maximum_level, maximum_level)
    if not EXECUTION_GROUP_LEVEL < DEFAULT_MINIMUM_LEVEL <= maximum_level:
        raise ValueError(
            "Places execution groups must be ancestors of every serving leaf"
        )
    if target_fragment_input_bytes is None:
        target_fragment_input_bytes = min(
            DEFAULT_TARGET_FRAGMENT_INPUT_BYTES, max_fragment_input_bytes
        )
    if min(
        fragment_rows,
        target_fragment_input_bytes,
        max_fragment_input_bytes,
        max_fragment_bytes,
        max_workspace_bytes,
    ) < 1:
        raise ValueError("Places fragment row/byte limits must be positive")
    if not 1 <= max_task_fragments <= 1024:
        raise ValueError("Places per-task fragment cap must be between 1 and 1024")
    tasks = inventory["map_plan"]["tasks"]
    if not 0 <= task_index < len(tasks):
        raise ValueError("Places map task index is outside the inventory plan")
    task = tasks[task_index]
    objects = inventory["objects"]
    output_dir.mkdir(parents=True, exist_ok=True)
    rejections = Counter({reason: 0 for reason in REJECTION_PRECEDENCE})
    input_records = retained_records = 0
    workspace = _WorkspaceBudget(output_dir / "staging", max_workspace_bytes)
    store = _IntermediateRowStore(output_dir / "staging", workspace=workspace)
    census = _SummaryCensusStore(output_dir / "staging", workspace=workspace)
    try:
        for row_range in task["ranges"]:
            source_object = objects[row_range["object_index"]]
            if (
                source_object["uri"] != row_range["uri"]
                or source_object["etag"] != row_range["etag"]
            ):
                raise ValueError("Places task range differs from object identity")
            actual_range_records = 0
            expected_row_group = row_range["first_row_group"]
            expected_row_index = 0
            for row_group, row_index, row in batch_reader(source_object, row_range):
                while (
                    expected_row_group <= row_range["last_row_group"]
                    and expected_row_index
                    == source_object["row_groups"][expected_row_group]["rows"]
                ):
                    expected_row_group += 1
                    expected_row_index = 0
                if (row_group, row_index) != (expected_row_group, expected_row_index):
                    raise ValueError(
                        "Places batch reader emitted an invalid source locator"
                    )
                expected_row_index += 1
                actual_range_records += 1
                input_records += 1
                retained, reason = project_row(
                    row,
                    maximum_level=maximum_level,
                    source_uri=source_object["uri"],
                    row_group=row_group,
                    row_index=row_index,
                )
                if reason is not None:
                    if reason not in rejections:
                        raise AssertionError(f"unnamed Places rejection: {reason}")
                    rejections[reason] += 1
                    continue
                assert retained is not None
                retained_records += 1
                store.add(retained)
                place = place_from_row(retained, retained_records)
                terms = place_terms(place)
                census.add(retained, terms, sequence=retained_records)
            if actual_range_records != row_range["rows"]:
                raise ValueError(
                    f"Places range input differs: expected {row_range['rows']}, "
                    f"read {actual_range_records}"
                )

        expected = task["expected_input_records"]
        rejected_records = sum(rejections.values())
        if input_records != expected:
            raise ValueError(
                f"Places task input differs: expected {expected}, read {input_records}"
            )
        if input_records != retained_records + rejected_records:
            raise AssertionError(
                "Places retained/rejected accounting does not reconcile"
            )
        store.finish()
        census.finish()

        split_evidence = {
            "cell_boundary_flushes": 0,
            "hot_cell_hard_splits": 0,
            "output_cap_splits": 0,
        }
        fragments = _write_ordered_packs(
            store=store,
            output_dir=output_dir,
            inventory_sha256=inventory["inventory_sha256"],
            task_digest=task["task_digest"],
            writer=fragment_writer,
            target_bytes=target_fragment_input_bytes,
            maximum_bytes=max_fragment_bytes,
            maximum_input_bytes=max_fragment_input_bytes,
            row_limit=fragment_rows,
            maximum_packs=max_task_fragments,
            workspace=workspace,
        )

        fragment_records = sum(item["records"] for item in fragments)
        if retained_records != fragment_records:
            raise AssertionError("Places fragment record accounting does not reconcile")
        summary_artifact = _write_summary(
            census=census,
            output_dir=output_dir,
            maximum_level=maximum_level,
            inventory_sha256=inventory["inventory_sha256"],
            task_digest=task["task_digest"],
            workspace=workspace,
        )
        if retained_records != summary_artifact["records"]:
            raise AssertionError("Places summary accounting does not reconcile")
        execution_group_count = len(
            {
                group
                for item in fragments
                for group in item["execution_groups"]
            }
        )
        if execution_group_count > 1 << (2 * EXECUTION_GROUP_LEVEL):
            raise AssertionError("Places task exceeded the execution-group universe")
        fragments_sha256 = sha256_value(fragments)
        report_without_digest = {
            "schema": MAP_REPORT_SCHEMA,
            "release": inventory["release"],
            "family": "places",
            "inventory_sha256": inventory["inventory_sha256"],
            "source_schema_fingerprint_sha256": inventory["schema_contract"][
                "fingerprint_sha256"
            ],
            "execution": {
                "task_index": task_index,
                "task_digest": task["task_digest"],
                "source_digest": task["source_digest"],
                "task_identity_is_serving_identity": False,
                "fragment_grouping": "coarse-cross-group-parquet-packs-v2",
                "fragment_grouping_is_final_shard_identity": False,
                "execution_group_level": EXECUTION_GROUP_LEVEL,
                "execution_group_count": execution_group_count,
                "maximum_execution_groups": 1 << (2 * EXECUTION_GROUP_LEVEL),
                "row_group_rows_limit": fragment_rows,
                "pack_bytes_target": target_fragment_input_bytes,
                "row_group_input_bytes_limit": max_fragment_input_bytes,
                "pack_bytes_limit": max_fragment_bytes,
                "task_pack_count_limit": max_task_fragments,
                "sort": {
                    **store.evidence(),
                    "json_payloads": False,
                },
                "census": census.evidence(),
                "workspace": workspace.evidence(),
                "packs": {
                    "kind": "task-wide-order-coarse-pack-v2",
                    "writer": "single-duckdb-order-group-aligned-row-groups-v2",
                    "maximum_batch_rows": MAP_OUTPUT_BATCH_ROWS,
                    "python_pack_rows_materialized": False,
                    "ordinary_boundary": "execution-group-or-bounded-row-group",
                    "physical_pack_target_bytes": target_fragment_input_bytes,
                    "row_group_boundary": "execution-group",
                    "maximum_row_group_input_bytes": min(
                        max_fragment_input_bytes, MAX_PACK_ROW_GROUP_INPUT_BYTES
                    ),
                    "packs_may_span_execution_groups": True,
                    "ordered_queries": 1,
                    "sort_extent_queries": 0,
                    "target_output_bytes": target_fragment_input_bytes,
                    "hard_row_group_input_bytes": max_fragment_input_bytes,
                    "hard_output_bytes": max_fragment_bytes,
                    **split_evidence,
                },
            },
            "source_ranges": task["ranges"],
            "partitioning": {
                "scheme": "world-quadkey-v1",
                "serving_leaf_minimum_level": DEFAULT_MINIMUM_LEVEL,
                "maximum_level": maximum_level,
            },
            "accounting": {
                "expected_input_records": expected,
                "input_records": input_records,
                "retained_records": retained_records,
                "rejected_records": rejected_records,
                "rejections_by_precedence": [
                    {"reason": reason, "records": rejections[reason]}
                    for reason in REJECTION_PRECEDENCE
                ],
            },
            "summary": summary_artifact,
            "fragments": {
                "count": len(fragments),
                "records": fragment_records,
                "bytes": sum(item["bytes"] for item in fragments),
                "manifest_sha256": fragments_sha256,
                "objects": fragments,
            },
        }
        return {
            **report_without_digest,
            "report_sha256": sha256_value(report_without_digest),
        }
    finally:
        census.close()
        store.close()


def _default_head_source(uri: str) -> dict[str, Any]:
    prefix = "s3://"
    if not uri.startswith(prefix):
        raise ValueError("Places source URI must use s3://")
    bucket, separator, key = uri[len(prefix) :].partition("/")
    if not separator or not bucket or not key:
        raise ValueError("Places source URI is invalid")
    encoded_key = urllib.parse.quote(key, safe="/")
    url = f"https://{bucket}.s3.{REGION}.amazonaws.com/{encoded_key}"
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "overture-geocoder-places-map/1"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        etag = response.headers.get("ETag")
        size = response.headers.get("Content-Length")
    if etag is None or size is None:
        raise ValueError(f"Places source HEAD omitted ETag/length: {uri}")
    return {"etag": etag.strip('"'), "bytes": int(size)}


def make_pyarrow_batch_reader(
    filesystem: Any,
    *,
    head_source: Callable[[str], dict[str, Any]] = _default_head_source,
) -> BatchReader:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - hosted dependency boundary
        raise RuntimeError("Places Parquet input requires pyarrow") from exc

    checked_identities: set[str] = set()

    def read(
        source_object: dict[str, Any], row_range: dict[str, Any]
    ) -> Iterator[tuple[int, int, dict[str, Any]]]:
        path = source_object["uri"].removeprefix("s3://")
        if source_object["uri"] not in checked_identities:
            current_identity = head_source(source_object["uri"])
            if current_identity != {
                "etag": source_object["etag"],
                "bytes": source_object["bytes"],
            }:
                raise ValueError(
                    f"Places object identity changed after inventory: {source_object['uri']}"
                )
            checked_identities.add(source_object["uri"])
        info = filesystem.get_file_info(path)
        if info.size != source_object["bytes"]:
            raise ValueError(
                f"Places object size changed after inventory: {source_object['uri']}"
            )
        parquet = pq.ParquetFile(path, filesystem=filesystem)
        contract = schema_contract_from_arrow(parquet.schema_arrow)
        if contract["fingerprint_sha256"] != source_object["schema_fingerprint_sha256"]:
            raise ValueError(
                f"Places schema changed after inventory: {source_object['uri']}"
            )
        column_roots = [
            "addresses",
            "basic_category",
            "brand",
            "categories",
            "confidence",
            "geometry",
            "id",
            "names",
            "operating_status",
        ]
        for row_group in range(
            row_range["first_row_group"], row_range["last_row_group"] + 1
        ):
            expected_rows = source_object["row_groups"][row_group]["rows"]
            emitted = 0
            for batch in parquet.iter_batches(
                batch_size=16_384,
                row_groups=[row_group],
                columns=column_roots,
                use_threads=False,
            ):
                for row in batch.to_pylist():
                    yield row_group, emitted, row
                    emitted += 1
            if emitted != expected_rows:
                raise ValueError(
                    f"Places row group changed after inventory: {source_object['uri']}#{row_group}"
                )

    return read


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--task-index", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--maximum-level", type=int, default=DEFAULT_MAXIMUM_LEVEL)
    parser.add_argument("--fragment-rows", type=int, default=DEFAULT_FRAGMENT_ROWS)
    parser.add_argument(
        "--target-fragment-input-bytes",
        type=int,
        default=DEFAULT_TARGET_FRAGMENT_INPUT_BYTES,
    )
    parser.add_argument(
        "--max-fragment-input-bytes", type=int, default=DEFAULT_MAX_FRAGMENT_INPUT_BYTES
    )
    parser.add_argument(
        "--max-fragment-bytes", type=int, default=DEFAULT_MAX_FRAGMENT_BYTES
    )
    parser.add_argument(
        "--max-workspace-bytes", type=int, default=DEFAULT_MAX_MAP_WORKSPACE_BYTES
    )
    parser.add_argument(
        "--max-task-fragments", type=int, default=DEFAULT_MAX_TASK_FRAGMENTS
    )
    args = parser.parse_args()
    try:
        import pyarrow.fs as pafs
    except ImportError as exc:  # pragma: no cover - hosted dependency boundary
        raise SystemExit("global_v2_places_map.py requires pyarrow") from exc
    inventory = json.loads(args.inventory.read_text())
    filesystem = pafs.S3FileSystem(anonymous=True, region=REGION)
    report = run_map_task(
        inventory,
        task_index=args.task_index,
        output_dir=args.output_dir,
        batch_reader=make_pyarrow_batch_reader(filesystem),
        maximum_level=args.maximum_level,
        fragment_rows=args.fragment_rows,
        target_fragment_input_bytes=args.target_fragment_input_bytes,
        max_fragment_input_bytes=args.max_fragment_input_bytes,
        max_fragment_bytes=args.max_fragment_bytes,
        max_task_fragments=args.max_task_fragments,
        max_workspace_bytes=args.max_workspace_bytes,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "report_sha256": report["report_sha256"],
                "input_records": report["accounting"]["input_records"],
                "retained_records": report["accounting"]["retained_records"],
                "rejected_records": report["accounting"]["rejected_records"],
                "fragments": report["fragments"]["count"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
