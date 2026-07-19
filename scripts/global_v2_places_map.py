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
import gzip
import hashlib
import json
import math
import os
import sqlite3
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
    canonical_json_bytes,
    schema_contract_from_arrow,
    sha256_value,
    validate_inventory,
)
from places_partition import (  # noqa: E402
    DEFAULT_MAXIMUM_LEVEL,
    DEFAULT_MINIMUM_LEVEL,
    morton_quadkey,
    point_morton,
    validate_levels,
)


MAP_REPORT_SCHEMA = "overture-global-v2-places-map-report-v1"
COUNT_ARTIFACT_SCHEMA = "overture-global-v2-places-max-cell-counts-v1"
FRAGMENT_SCHEMA = "overture-global-v2-places-map-fragment-v1"
SUPPORTED_OPERATING_STATUSES = frozenset({"open", "temporarily_closed"})
EXECUTION_GROUP_LEVEL = 4
DEFAULT_MAX_TASK_FRAGMENTS = 512
DEFAULT_MAX_FRAGMENT_BYTES = 256_000_000
DEFAULT_MAX_FRAGMENT_INPUT_BYTES = 128_000_000

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


class _IntermediateRowStore:
    """Disk-backed deterministic sort/aggregate state for one map task."""

    def __init__(self, staging: Path) -> None:
        staging.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix="places-map-", suffix=".sqlite3", dir=staging
        )
        os.close(descriptor)
        self.path = Path(name)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=OFF")
        self.connection.execute("PRAGMA synchronous=OFF")
        self.connection.execute("PRAGMA temp_store=FILE")
        self.connection.execute(
            """
            CREATE TABLE retained (
                execution_group TEXT NOT NULL,
                partition_cell TEXT NOT NULL,
                partition_key INTEGER NOT NULL,
                confidence_rank INTEGER NOT NULL,
                gers_id TEXT NOT NULL,
                source_uri TEXT NOT NULL,
                source_row_group INTEGER NOT NULL,
                source_row_index INTEGER NOT NULL,
                payload BLOB NOT NULL
            )
            """
        )
        self.pending: list[tuple[Any, ...]] = []

    def add(self, row: dict[str, Any]) -> None:
        self.pending.append(
            (
                row["execution_group"],
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
        if len(self.pending) >= 10_000:
            self._flush()

    def _flush(self) -> None:
        if not self.pending:
            return
        self.connection.executemany(
            "INSERT INTO retained VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", self.pending
        )
        self.pending = []

    def finish(self) -> None:
        self._flush()
        self.connection.commit()
        self.connection.execute(
            """
            CREATE INDEX retained_serving_order ON retained (
                execution_group,
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

    def iter_rows(self) -> Iterator[tuple[str, int, dict[str, Any]]]:
        cursor = self.connection.execute(
            """
            SELECT execution_group, length(payload), payload
            FROM retained
            ORDER BY
                execution_group,
                partition_cell,
                partition_key,
                confidence_rank,
                gers_id,
                source_uri,
                source_row_group,
                source_row_index
            """
        )
        for execution_group, payload_bytes, payload in cursor:
            row = json.loads(payload)
            if not isinstance(row, dict):
                raise AssertionError("stored Places map row is not an object")
            yield execution_group, payload_bytes, row

    def iter_counts(self) -> Iterator[tuple[str, int]]:
        for cell, records in self.connection.execute(
            """
            SELECT partition_cell, count(*)
            FROM retained
            GROUP BY execution_group, partition_cell
            ORDER BY execution_group, partition_cell
            """
        ):
            yield cell, records

    def close(self) -> None:
        self.connection.close()
        self.path.unlink(missing_ok=True)


def _default_fragment_writer(
    rows: list[dict[str, Any]], path: Path, metadata: dict[str, str]
) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - hosted dependency boundary
        raise RuntimeError("Places Parquet fragment writing requires pyarrow") from exc
    schema = pa.schema(
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
        metadata={
            key.encode(): value.encode() for key, value in sorted(metadata.items())
        },
    )
    table = pa.Table.from_pylist(rows, schema=schema)
    pq.write_table(
        table,
        path,
        compression="zstd",
        compression_level=9,
        row_group_size=min(100_000, len(rows)),
        use_dictionary=True,
        write_statistics=True,
    )


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


def _write_execution_fragments(
    rows: list[dict[str, Any]],
    *,
    execution_group: str,
    output_dir: Path,
    sequence: int,
    inventory_sha256: str,
    task_digest: str,
    writer: Callable[[list[dict[str, Any]], Path, dict[str, str]], None],
    max_fragment_bytes: int,
    remaining_fragments: int,
) -> list[dict[str, Any]]:
    if remaining_fragments < 1:
        raise ValueError("Places task exceeded its hard content-fragment count cap")
    if not rows or any(row["execution_group"] != execution_group for row in rows):
        raise ValueError("Places fragment must contain one non-empty execution group")
    rows.sort(key=_intermediate_sort_key)
    minimum_cell = rows[0]["partition_cell"]
    maximum_cell = rows[-1]["partition_cell"]
    staging = output_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"task-{task_digest[:12]}-{sequence:06d}-",
        suffix=".parquet",
        dir=staging,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        writer(
            rows,
            temporary,
            {
                "artifact_schema": FRAGMENT_SCHEMA,
                "inventory_sha256": inventory_sha256,
                "map_task_digest": task_digest,
                "execution_group": execution_group,
                "execution_group_level": str(EXECUTION_GROUP_LEVEL),
                "minimum_maximum_level_cell": minimum_cell,
                "maximum_maximum_level_cell": maximum_cell,
                "execution_identity_is_serving_identity": "false",
            },
        )
        digest, size = sha256_file(temporary)
        if size > max_fragment_bytes:
            temporary.unlink()
            if len(rows) == 1:
                raise ValueError(
                    "one Places row exceeds the configured fragment byte cap"
                )
            midpoint = len(rows) // 2
            left = _write_execution_fragments(
                rows[:midpoint],
                execution_group=execution_group,
                output_dir=output_dir,
                sequence=sequence,
                inventory_sha256=inventory_sha256,
                task_digest=task_digest,
                writer=writer,
                max_fragment_bytes=max_fragment_bytes,
                remaining_fragments=remaining_fragments,
            )
            right = _write_execution_fragments(
                rows[midpoint:],
                execution_group=execution_group,
                output_dir=output_dir,
                sequence=sequence + len(left),
                inventory_sha256=inventory_sha256,
                task_digest=task_digest,
                writer=writer,
                max_fragment_bytes=max_fragment_bytes,
                remaining_fragments=remaining_fragments - len(left),
            )
            return left + right
        relative = (
            Path("fragments")
            / f"group={execution_group}"
            / "sha256"
            / f"{digest}.parquet"
        )
        digest, size = _install_content_file(temporary, output_dir / relative)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return [
        {
            "execution_group": execution_group,
            "execution_group_level": EXECUTION_GROUP_LEVEL,
            "minimum_maximum_level_cell": minimum_cell,
            "maximum_maximum_level_cell": maximum_cell,
            "maximum_level_cells": len({row["partition_cell"] for row in rows}),
            "object_key": relative.as_posix(),
            "sha256": digest,
            "bytes": size,
            "records": len(rows),
            "minimum_sort_key": list(_intermediate_sort_key(rows[0])),
            "maximum_sort_key": list(_intermediate_sort_key(rows[-1])),
        }
    ]


def _write_counts(
    counts: Iterable[tuple[str, int]],
    *,
    output_dir: Path,
    maximum_level: int,
    inventory_sha256: str,
    task_digest: str,
) -> dict[str, Any]:
    header = {
        "schema": COUNT_ARTIFACT_SCHEMA,
        "maximum_level": maximum_level,
        "inventory_sha256": inventory_sha256,
        "map_task_digest": task_digest,
        "task_identity_is_serving_identity": False,
    }
    staging = output_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="counts-", suffix=".jsonl.gz", dir=staging
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    cells = records = 0
    previous: str | None = None
    try:
        with temporary.open("wb") as raw_target:
            with gzip.GzipFile(
                filename="",
                fileobj=raw_target,
                mode="wb",
                compresslevel=9,
                mtime=0,
            ) as target:
                target.write(canonical_json_bytes(header) + b"\n")
                for cell, count in counts:
                    if (
                        not isinstance(cell, str)
                        or previous is not None
                        and cell <= previous
                        or type(count) is not int
                        or count <= 0
                    ):
                        raise ValueError("Places maximum-level counts are invalid")
                    target.write(
                        canonical_json_bytes({"cell": cell, "records": count}) + b"\n"
                    )
                    previous = cell
                    cells += 1
                    records += count
        digest, size = sha256_file(temporary)
        relative = Path("counts") / "sha256" / f"{digest}.jsonl.gz"
        destination = output_dir / relative
        installed_digest, installed_size = _install_content_file(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if installed_digest != digest or installed_size != size:
        raise AssertionError("Places count content installation changed its bytes")
    return {
        "object_key": relative.as_posix(),
        "sha256": digest,
        "bytes": size,
        "cells": cells,
        "records": records,
        "maximum_level": maximum_level,
    }


def read_maximum_level_counts(path: Path) -> Iterator[tuple[str, int]]:
    with gzip.open(path, "rt", encoding="utf-8") as source:
        header = json.loads(next(source))
        if header.get("schema") != COUNT_ARTIFACT_SCHEMA:
            raise ValueError("Places count artifact schema is invalid")
        previous: str | None = None
        for line in source:
            value = json.loads(line)
            cell = value.get("cell")
            records = value.get("records")
            if (
                not isinstance(cell, str)
                or previous is not None
                and cell <= previous
                or type(records) is not int
                or records <= 0
            ):
                raise ValueError("Places count artifact rows are invalid or unordered")
            previous = cell
            yield cell, records


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
    fragment_rows: int = 100_000,
    max_fragment_input_bytes: int = DEFAULT_MAX_FRAGMENT_INPUT_BYTES,
    max_fragment_bytes: int = DEFAULT_MAX_FRAGMENT_BYTES,
    max_task_fragments: int = DEFAULT_MAX_TASK_FRAGMENTS,
) -> dict[str, Any]:
    inventory = validate_inventory(inventory_value)
    validate_levels(maximum_level, maximum_level)
    if not EXECUTION_GROUP_LEVEL < DEFAULT_MINIMUM_LEVEL <= maximum_level:
        raise ValueError(
            "Places execution groups must be ancestors of every serving leaf"
        )
    if min(fragment_rows, max_fragment_input_bytes, max_fragment_bytes) < 1:
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
    store = _IntermediateRowStore(output_dir / "staging")
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

        fragments: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        pending_input_bytes = 0
        current_group: str | None = None
        sequence = 0

        def flush() -> None:
            nonlocal pending, pending_input_bytes, sequence
            if not pending or current_group is None:
                return
            created = _write_execution_fragments(
                pending,
                execution_group=current_group,
                output_dir=output_dir,
                sequence=sequence,
                inventory_sha256=inventory["inventory_sha256"],
                task_digest=task["task_digest"],
                writer=fragment_writer,
                max_fragment_bytes=max_fragment_bytes,
                remaining_fragments=max_task_fragments - len(fragments),
            )
            if len(fragments) + len(created) > max_task_fragments:
                raise ValueError(
                    "Places task exceeded its hard content-fragment count cap"
                )
            fragments.extend(created)
            sequence += len(created)
            pending = []
            pending_input_bytes = 0

        for execution_group, payload_bytes, row in store.iter_rows():
            if payload_bytes > max_fragment_input_bytes:
                raise ValueError("one Places row exceeds the fragment input byte cap")
            if pending and (
                execution_group != current_group
                or len(pending) >= fragment_rows
                or pending_input_bytes + payload_bytes > max_fragment_input_bytes
            ):
                flush()
            current_group = execution_group
            pending.append(row)
            pending_input_bytes += payload_bytes
        flush()

        fragment_records = sum(item["records"] for item in fragments)
        if retained_records != fragment_records:
            raise AssertionError("Places fragment record accounting does not reconcile")
        count_artifact = _write_counts(
            store.iter_counts(),
            output_dir=output_dir,
            maximum_level=maximum_level,
            inventory_sha256=inventory["inventory_sha256"],
            task_digest=task["task_digest"],
        )
        if retained_records != count_artifact["records"]:
            raise AssertionError("Places count artifact accounting does not reconcile")
        fragments.sort(
            key=lambda item: (
                item["execution_group"],
                item["minimum_sort_key"],
                item["sha256"],
            )
        )
        execution_group_count = len({item["execution_group"] for item in fragments})
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
                "fragment_grouping": "level-4-world-quadkey-execution-v1",
                "fragment_grouping_is_final_shard_identity": False,
                "execution_group_level": EXECUTION_GROUP_LEVEL,
                "execution_group_count": execution_group_count,
                "maximum_execution_groups": 1 << (2 * EXECUTION_GROUP_LEVEL),
                "fragment_rows_limit": fragment_rows,
                "fragment_input_bytes_limit": max_fragment_input_bytes,
                "fragment_bytes_limit": max_fragment_bytes,
                "task_fragment_count_limit": max_task_fragments,
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
            "counts": count_artifact,
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
    parser.add_argument("--fragment-rows", type=int, default=100_000)
    parser.add_argument(
        "--max-fragment-input-bytes", type=int, default=DEFAULT_MAX_FRAGMENT_INPUT_BYTES
    )
    parser.add_argument(
        "--max-fragment-bytes", type=int, default=DEFAULT_MAX_FRAGMENT_BYTES
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
        max_fragment_input_bytes=args.max_fragment_input_bytes,
        max_fragment_bytes=args.max_fragment_bytes,
        max_task_fragments=args.max_task_fragments,
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
