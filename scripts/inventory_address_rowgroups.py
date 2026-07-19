#!/usr/bin/env python3
"""Inventory every address Parquet footer and plan bounded contiguous ranges.

The command reads only the public S3 listing and Parquet metadata. It does not
read feature columns, write cloud objects, or touch a catalog. The resulting
inventory is suitable for the global-build control plane, while the embedded
row-group plan answers whether the pinned release fits the hosted-runner task
count and per-task row/byte gates.

Alongside the per-row-group ``country`` statistics the inventory now records the
row group's ``bbox`` extent (``xmin_min``/``xmax_max``/``ymin_min``/``ymax_max``)
so a regional build can prune the plan to the row groups whose bbox statistics
intersect a query box. Missing bbox statistics are recorded as ``null`` and are
treated as "cannot prune" (conservatively intersecting), never silently dropped.

bbox scope is *row-group approximate*, not exact. The map producer
(``experiment_hosted_rowgroups.py``) reads whole row groups and its
source-accounting invariant reconciles the measured ``(rows, compressed,
uncompressed)`` of each task against the planned footer statistics, deriving a
per-row ``source_row_index`` over the *entire* row group. A row-level bbox
predicate would make the emitted row count fall below the row group's
``num_rows`` and shift those locators, breaking that reconciliation. A
bbox-scoped plan therefore prunes at row-group granularity only and records
``bbox_scope: row_group_approximate``: every intersecting group is read in full,
so the scope is a superset of the box, never a subset.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import statistics
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any


BUCKET = "overturemaps-us-west-2"
REGION = "us-west-2"
SCHEMA = "overture-address-rowgroup-inventory-v1"
PLAN_SCHEMA = "overture-address-rowgroup-plan-v1"
SOURCE_INVENTORY_SCHEMA = "overture-global-source-inventory-v1"
SCHEMA_CONTRACT_VERSION = "overture-address-required-schema-v1"
INVENTORY_IDENTITY_VERSION = "overture-address-inventory-identity-v1"
TASK_IDENTITY_VERSION = "overture-address-rowgroup-task-identity-v1"
SOURCE_SELECTION_VERSION = "overture-address-rowgroup-source-selection-v1"
INVENTORY_METADATA_KEY = b"overture.address_inventory_sha256"
TASK_INDEX_METADATA_KEY = b"overture.address_task_index"
TASK_DIGEST_METADATA_KEY = b"overture.address_task_digest_sha256"
TASK_SOURCE_DIGEST_METADATA_KEY = b"overture.address_task_source_digest_sha256"
EXECUTION_BUCKET_METADATA_KEY = b"overture.address_execution_bucket"
SELECTED_COLUMN_ROOTS = {
    "id",
    "street",
    "number",
    "unit",
    "postcode",
    "postal_city",
    "address_levels",
    "country",
    "geometry",
}
REQUIRED_FIELD_TYPES: dict[str, str] = {
    "address_levels": "list<struct>",
    "address_levels[].value": "string",
    "country": "string",
    "geometry": "binary",
    "id": "string",
    "number": "string",
    "postal_city": "string",
    "postcode": "string",
    "street": "string",
    "unit": "string",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def address_execution_bucket(task_index: int) -> str:
    if type(task_index) is not int or task_index < 0:
        raise ValueError("address task index must be a non-negative integer")
    return f"address-map-task-{task_index:03d}"


def plan_with_task_digests(plan: dict[str, Any]) -> dict[str, Any]:
    """Return a canonical plan whose exact task/range identities are hashed."""

    if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
        raise ValueError("address row-group plan must contain tasks")
    result = json.loads(canonical_json(plan))
    for expected_index, task in enumerate(result["tasks"]):
        if (
            not isinstance(task, dict)
            or task.get("index") != expected_index
            or not isinstance(task.get("ranges"), list)
            or not task["ranges"]
            or "source_digest_sha256" in task
            or "task_digest_sha256" in task
        ):
            raise ValueError("address plan task identity is invalid")
        source_identity = {
            "version": SOURCE_SELECTION_VERSION,
            "ranges": task["ranges"],
        }
        task["source_digest_sha256"] = sha256_value(source_identity)
        task["execution_bucket"] = address_execution_bucket(expected_index)
        task["task_digest_sha256"] = sha256_value(
            {
                "version": TASK_IDENTITY_VERSION,
                "task": {
                    key: value
                    for key, value in task.items()
                    if key != "task_digest_sha256"
                },
            }
        )
    return result


def inventory_identity_payload(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": INVENTORY_IDENTITY_VERSION,
        "source_inventory": report.get("source_inventory"),
        "schema_contract": report.get("schema_contract"),
        "objects": report.get("objects"),
        "plan": report.get("plan"),
    }


def validate_footer_facts(
    release: str,
    objects: Any,
    schema_contract: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate the immutable per-object/row-group facts used to rebuild tasks."""

    if not isinstance(objects, list) or not objects:
        raise ValueError("address footer facts must contain source objects")
    approved_prefix = f"s3://{BUCKET}/release/{release}/theme=addresses/type=address/"
    expected_object_fields = {
        "uri",
        "etag",
        "bytes",
        "records",
        "row_groups",
        "selected_compressed_bytes",
        "selected_uncompressed_bytes",
        "schema_contract",
        "groups",
    }
    expected_group_fields = {
        "index",
        "rows",
        "all_compressed_bytes",
        "all_uncompressed_bytes",
        "selected_compressed_bytes",
        "selected_uncompressed_bytes",
        "country_min",
        "country_max",
        "exact_country",
        "bbox_xmin_min",
        "bbox_xmax_max",
        "bbox_ymin_min",
        "bbox_ymax_max",
        "bbox_stats_complete",
    }
    previous_uri: str | None = None
    for source in objects:
        if not isinstance(source, dict) or set(source) != expected_object_fields:
            raise ValueError("address footer object fields are invalid")
        uri = source["uri"]
        relative = uri.removeprefix(approved_prefix) if isinstance(uri, str) else ""
        if (
            not relative
            or relative == uri
            or not relative.endswith(".parquet")
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or "?" in relative
            or "#" in relative
            or (previous_uri is not None and uri <= previous_uri)
            or not isinstance(source["etag"], str)
            or not source["etag"]
            or any(
                type(source[field]) is not int or source[field] <= 0
                for field in ("bytes", "records", "row_groups")
            )
            or source["schema_contract"] != schema_contract
            or not isinstance(source["groups"], list)
            or len(source["groups"]) != source["row_groups"]
        ):
            raise ValueError("address footer object identity is invalid")
        compressed = uncompressed = rows = 0
        for expected_index, group in enumerate(source["groups"]):
            numeric_extent = (
                (
                    group.get("bbox_xmin_min"),
                    group.get("bbox_xmax_max"),
                    group.get("bbox_ymin_min"),
                    group.get("bbox_ymax_max"),
                )
                if isinstance(group, dict)
                else ()
            )
            if (
                not isinstance(group, dict)
                or set(group) != expected_group_fields
                or group.get("index") != expected_index
                or any(
                    type(group.get(field)) is not int or group[field] <= 0
                    for field in ("rows", "all_uncompressed_bytes")
                )
                or any(
                    type(group.get(field)) is not int or group[field] < 0
                    for field in (
                        "all_compressed_bytes",
                        "selected_compressed_bytes",
                        "selected_uncompressed_bytes",
                    )
                )
                or group["selected_compressed_bytes"] > group["all_compressed_bytes"]
                or group["selected_uncompressed_bytes"]
                > group["all_uncompressed_bytes"]
                or any(
                    value is not None
                    and (
                        not isinstance(value, (int, float)) or not math.isfinite(value)
                    )
                    for value in numeric_extent
                )
                or type(group.get("bbox_stats_complete")) is not bool
                or group["bbox_stats_complete"]
                != all(value is not None for value in numeric_extent)
                or any(
                    value is not None and not isinstance(value, str)
                    for value in (
                        group.get("country_min"),
                        group.get("country_max"),
                        group.get("exact_country"),
                    )
                )
                or group.get("exact_country")
                != (
                    group.get("country_min")
                    if group.get("country_min") is not None
                    and group.get("country_min") == group.get("country_max")
                    else None
                )
            ):
                raise ValueError("address row-group footer facts are invalid")
            rows += group["rows"]
            compressed += group["selected_compressed_bytes"]
            uncompressed += group["selected_uncompressed_bytes"]
        if (
            rows != source["records"]
            or compressed != source["selected_compressed_bytes"]
            or uncompressed != source["selected_uncompressed_bytes"]
        ):
            raise ValueError("address footer object totals do not reconcile")
        previous_uri = uri
    return objects


def validate_canonical_inventory(report: dict[str, Any]) -> dict[str, Any]:
    """Fail closed unless a global address inventory is exactly self-bound."""

    if not isinstance(report, dict) or report.get("schema") != SCHEMA:
        raise ValueError("unsupported address row-group inventory schema")
    source_inventory = report.get("source_inventory")
    if not isinstance(source_inventory, dict):
        raise ValueError("address inventory omits its source inventory")
    source_digest = report.get("source_inventory_sha256")
    if not isinstance(source_digest, str) or source_digest != sha256_value(
        source_inventory
    ):
        raise ValueError("address source inventory digest differs")
    contract = report.get("schema_contract")
    if (
        not isinstance(contract, dict)
        or canonical_schema_contract(contract.get("fields")) != contract
    ):
        raise ValueError("address schema contract fingerprint differs")
    plan = report.get("plan")
    if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
        raise ValueError("address inventory omits its deterministic plan")
    gates = plan.get("gates")
    if (
        not isinstance(gates, dict)
        or set(gates)
        != {
            "target_rows",
            "max_selected_uncompressed_bytes",
            "max_groups",
            "max_tasks",
        }
        or any(type(value) is not int or value <= 0 for value in gates.values())
        or gates["max_tasks"] > 128
    ):
        raise ValueError("address inventory task gates are invalid")
    if (
        plan.get("safe_at_configured_task_count") is not True
        or plan.get("task_count") != len(plan["tasks"])
        or plan["task_count"] > gates["max_tasks"]
        or plan["task_count"] > 128
    ):
        raise ValueError("address inventory task count exceeds its safe cap")
    inventory_objects = source_inventory.get("objects")
    if not isinstance(inventory_objects, list) or not inventory_objects:
        raise ValueError("address source inventory has no objects")
    for source in inventory_objects:
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("uri"), str)
            or not isinstance(source.get("etag"), str)
            or any(
                type(source.get(field)) is not int or source[field] <= 0
                for field in ("bytes", "records", "row_groups")
            )
        ):
            raise ValueError("address source inventory object identity is invalid")
    objects_by_uri = {
        item.get("uri"): item for item in inventory_objects if isinstance(item, dict)
    }
    if len(objects_by_uri) != len(inventory_objects) or None in objects_by_uri:
        raise ValueError("address source inventory object identity is invalid")
    for expected_index, task in enumerate(plan["tasks"]):
        if not isinstance(task, dict):
            raise ValueError("address inventory task must be an object")
        stored_task_digest = task.get("task_digest_sha256")
        stored_source_digest = task.get("source_digest_sha256")
        expected_source_digest = sha256_value(
            {"version": SOURCE_SELECTION_VERSION, "ranges": task.get("ranges")}
        )
        expected_task_digest = sha256_value(
            {
                "version": TASK_IDENTITY_VERSION,
                "task": {
                    **{
                        key: value
                        for key, value in task.items()
                        if key != "task_digest_sha256"
                    }
                },
            }
        )
        if (
            task.get("index") != expected_index
            or task.get("execution_bucket") != address_execution_bucket(expected_index)
            or stored_source_digest != expected_source_digest
            or stored_task_digest != expected_task_digest
        ):
            raise ValueError("address inventory task digest differs")
        for selected_range in task.get("ranges", []):
            if not isinstance(selected_range, dict):
                raise ValueError("address inventory task range must be an object")
            source = objects_by_uri.get(selected_range.get("uri"))
            if (
                source is None
                or selected_range.get("etag") != source.get("etag")
                or selected_range.get("source_bytes") != source.get("bytes")
                or selected_range.get("source_records") != source.get("records")
                or selected_range.get("source_row_groups") != source.get("row_groups")
                or type(selected_range.get("first_row_group")) is not int
                or type(selected_range.get("last_row_group")) is not int
                or not 0
                <= selected_range["first_row_group"]
                <= selected_range["last_row_group"]
                < source.get("row_groups", -1)
                or selected_range.get("row_groups")
                != selected_range["last_row_group"]
                - selected_range["first_row_group"]
                + 1
                or type(selected_range.get("rows")) is not int
                or selected_range["rows"] <= 0
            ):
                raise ValueError("address task range differs from source inventory")
        if task.get("rows") != sum(
            selected_range["rows"] for selected_range in task.get("ranges", [])
        ):
            raise ValueError("address task rows do not reconcile with its ranges")
    inventory_digest = report.get("inventory_sha256")
    if not isinstance(inventory_digest, str) or inventory_digest != sha256_value(
        inventory_identity_payload(report)
    ):
        raise ValueError("address inventory self-digest differs")
    footer_objects = report.get("objects")
    if not isinstance(report.get("release"), str):
        raise ValueError("address inventory release/plan gates are invalid")
    try:
        rebuilt_plan = plan_contiguous_ranges(
            footer_objects,
            target_rows=gates["target_rows"],
            max_selected_uncompressed_bytes=gates["max_selected_uncompressed_bytes"],
            max_groups=gates["max_groups"],
            max_tasks=gates["max_tasks"],
        )
        rebuilt = build_report(report["release"], footer_objects, rebuilt_plan)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "address inventory deterministic contents are invalid"
        ) from exc
    if rebuilt != report:
        raise ValueError("address inventory differs from deterministic footer contents")
    return {
        "inventory_sha256": inventory_digest,
        "source_inventory_sha256": source_digest,
        "schema_fingerprint_sha256": contract["fingerprint_sha256"],
        "tasks": plan["tasks"],
    }


def _canonical_arrow_type(data_type: Any) -> str:
    import pyarrow as pa

    if pa.types.is_string(data_type):
        return "string"
    if pa.types.is_binary(data_type):
        return "binary"
    if pa.types.is_struct(data_type):
        return "struct"
    if pa.types.is_list(data_type) and pa.types.is_struct(data_type.value_type):
        return "list<struct>"
    return str(data_type)


def _arrow_field_at_path(schema: Any, path: str) -> Any:
    import pyarrow as pa

    parts = path.split(".")
    first = parts.pop(0)
    unwrap_list = first.endswith("[]")
    name = first[:-2] if unwrap_list else first
    field = schema.field(name)
    data_type = field.type
    if unwrap_list:
        if not pa.types.is_list(data_type):
            raise ValueError(f"required address field is not a list: {name}")
        data_type = data_type.value_type
    for part in parts:
        unwrap_list = part.endswith("[]")
        name = part[:-2] if unwrap_list else part
        if not pa.types.is_struct(data_type):
            raise ValueError(f"required address parent is not a struct: {path}")
        field = data_type.field(name)
        data_type = field.type
        if unwrap_list:
            if not pa.types.is_list(data_type):
                raise ValueError(f"required address field is not a list: {path}")
            data_type = data_type.value_type
    return field


def canonical_schema_contract(fields: Any) -> dict[str, Any]:
    supplied: dict[str, dict[str, Any]] = {}
    if not isinstance(fields, list):
        raise ValueError("address schema contract fields must be a list")
    for raw in fields:
        if not isinstance(raw, dict) or set(raw) != {"path", "type", "nullable"}:
            raise ValueError("address schema field descriptor is invalid")
        path = raw["path"]
        if (
            not isinstance(path, str)
            or path in supplied
            or raw["type"] != REQUIRED_FIELD_TYPES.get(path)
            or type(raw["nullable"]) is not bool
        ):
            raise ValueError("required address schema fields differ")
        supplied[path] = dict(raw)
    if set(supplied) != set(REQUIRED_FIELD_TYPES):
        raise ValueError("required address schema fields differ")
    fingerprint_input = {
        "version": SCHEMA_CONTRACT_VERSION,
        "fields": [supplied[path] for path in sorted(supplied)],
    }
    return {
        **fingerprint_input,
        "fingerprint_sha256": sha256_value(fingerprint_input),
    }


def schema_contract_from_arrow(schema: Any) -> dict[str, Any]:
    """Fingerprint only source fields whose bytes affect address serving."""
    descriptors = []
    for path, expected_type in sorted(REQUIRED_FIELD_TYPES.items()):
        try:
            field = (
                schema.field(path)
                if "." not in path
                else _arrow_field_at_path(schema, path)
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(
                f"required address schema field is missing: {path}"
            ) from exc
        actual_type = _canonical_arrow_type(field.type)
        if actual_type != expected_type:
            raise ValueError(
                f"required address schema type differs for {path}: "
                f"expected {expected_type}, got {actual_type}"
            )
        descriptors.append(
            {"path": path, "type": actual_type, "nullable": bool(field.nullable)}
        )
    return canonical_schema_contract(descriptors)


def listing_url(prefix: str, continuation_token: str | None = None) -> str:
    query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
    if continuation_token:
        query["continuation-token"] = continuation_token
    return (
        f"https://{BUCKET}.s3.{REGION}.amazonaws.com/?{urllib.parse.urlencode(query)}"
    )


def parse_listing_page(payload: bytes) -> tuple[list[dict[str, Any]], str | None]:
    root = ET.fromstring(payload)
    namespace = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    objects = []
    for item in root.findall("s3:Contents", namespace):
        key = item.findtext("s3:Key", namespaces=namespace)
        etag = item.findtext("s3:ETag", namespaces=namespace)
        size = item.findtext("s3:Size", namespaces=namespace)
        if key is None or etag is None or size is None:
            raise ValueError("S3 listing object is missing key, ETag, or size")
        if key.endswith(".parquet") and int(size) > 0:
            objects.append(
                {
                    "key": key,
                    "uri": f"s3://{BUCKET}/{key}",
                    "etag": etag.strip('"'),
                    "bytes": int(size),
                }
            )
    truncated = root.findtext("s3:IsTruncated", default="false", namespaces=namespace)
    token = root.findtext("s3:NextContinuationToken", namespaces=namespace)
    if truncated == "true" and not token:
        raise ValueError("truncated S3 listing omitted its continuation token")
    return objects, token if truncated == "true" else None


def list_objects(release: str) -> list[dict[str, Any]]:
    prefix = f"release/{release}/theme=addresses/type=address/"
    result: list[dict[str, Any]] = []
    token = None
    while True:
        request = urllib.request.Request(
            listing_url(prefix, token),
            headers={"User-Agent": "overture-geocoder-address-inventory/1"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            objects, token = parse_listing_page(response.read())
        result.extend(objects)
        if token is None:
            break
    if not result:
        raise ValueError("address source listing is empty")
    uris = [item["uri"] for item in result]
    if len(uris) != len(set(uris)):
        raise ValueError("address source listing contains duplicate objects")
    return sorted(result, key=lambda item: item["uri"])


def statistic_value(statistics: Any, name: str) -> Any:
    if statistics is None or not statistics.has_min_max:
        return None
    value = getattr(statistics, name)
    return value.decode("utf-8") if isinstance(value, bytes) else value


def inventory_object(source: dict[str, Any], filesystem: Any) -> dict[str, Any]:
    import pyarrow.parquet as pq

    path = source["uri"].removeprefix("s3://")
    parquet = pq.ParquetFile(path, filesystem=filesystem)
    metadata = parquet.metadata
    schema_contract = schema_contract_from_arrow(parquet.schema_arrow)
    groups = []
    for group_index in range(metadata.num_row_groups):
        group = metadata.row_group(group_index)
        selected_compressed = selected_uncompressed = all_compressed = 0
        country_min = country_max = None
        bbox_xmin_min = bbox_xmax_max = bbox_ymin_min = bbox_ymax_max = None
        for column_index in range(group.num_columns):
            column = group.column(column_index)
            all_compressed += column.total_compressed_size
            root = column.path_in_schema.split(".", 1)[0]
            if root in SELECTED_COLUMN_ROOTS:
                selected_compressed += column.total_compressed_size
                selected_uncompressed += column.total_uncompressed_size
            if column.path_in_schema == "country":
                country_min = statistic_value(column.statistics, "min")
                country_max = statistic_value(column.statistics, "max")
            elif column.path_in_schema == "bbox.xmin":
                bbox_xmin_min = statistic_value(column.statistics, "min")
            elif column.path_in_schema == "bbox.xmax":
                bbox_xmax_max = statistic_value(column.statistics, "max")
            elif column.path_in_schema == "bbox.ymin":
                bbox_ymin_min = statistic_value(column.statistics, "min")
            elif column.path_in_schema == "bbox.ymax":
                bbox_ymax_max = statistic_value(column.statistics, "max")
        groups.append(
            {
                "index": group_index,
                "rows": group.num_rows,
                "all_compressed_bytes": all_compressed,
                "all_uncompressed_bytes": group.total_byte_size,
                "selected_compressed_bytes": selected_compressed,
                "selected_uncompressed_bytes": selected_uncompressed,
                "country_min": country_min,
                "country_max": country_max,
                "exact_country": (
                    country_min
                    if country_min is not None and country_min == country_max
                    else None
                ),
                # bbox extent from footer statistics: the leftmost xmin, rightmost
                # xmax, lowest ymin, highest ymax across the row group's features.
                # Any missing statistic stays null so the group cannot be pruned.
                "bbox_xmin_min": bbox_xmin_min,
                "bbox_xmax_max": bbox_xmax_max,
                "bbox_ymin_min": bbox_ymin_min,
                "bbox_ymax_max": bbox_ymax_max,
                "bbox_stats_complete": None
                not in (bbox_xmin_min, bbox_xmax_max, bbox_ymin_min, bbox_ymax_max),
            }
        )
    if sum(group["rows"] for group in groups) != metadata.num_rows:
        raise ValueError(f"row-group counts do not reconcile for {source['uri']}")
    return {
        "uri": source["uri"],
        "etag": source["etag"],
        "bytes": source["bytes"],
        "records": metadata.num_rows,
        "row_groups": metadata.num_row_groups,
        "selected_compressed_bytes": sum(
            group["selected_compressed_bytes"] for group in groups
        ),
        "selected_uncompressed_bytes": sum(
            group["selected_uncompressed_bytes"] for group in groups
        ),
        "schema_contract": schema_contract,
        "groups": groups,
    }


def percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def distribution(values: list[int]) -> dict[str, int | float]:
    if not values:
        return {"min": 0, "p50": 0, "p95": 0, "max": 0, "mean": 0}
    return {
        "min": min(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": max(values),
        "mean": round(statistics.mean(values), 3),
    }


def group_bbox_intersects(
    group: dict[str, Any], bbox: tuple[float, float, float, float]
) -> bool:
    """Return whether a row group's bbox extent can intersect the query box.

    ``bbox`` is ``(xmin, ymin, xmax, ymax)``. A row group whose bbox statistics
    are incomplete cannot be pruned and is treated as intersecting so no source
    row is ever silently dropped by the plan.
    """
    xmin_min = group.get("bbox_xmin_min")
    xmax_max = group.get("bbox_xmax_max")
    ymin_min = group.get("bbox_ymin_min")
    ymax_max = group.get("bbox_ymax_max")
    if None in (xmin_min, xmax_max, ymin_min, ymax_max):
        return True
    qxmin, qymin, qxmax, qymax = bbox
    return (
        xmin_min <= qxmax
        and xmax_max >= qxmin
        and ymin_min <= qymax
        and ymax_max >= qymin
    )


def plan_contiguous_ranges(
    objects: list[dict[str, Any]],
    *,
    target_rows: int,
    max_selected_uncompressed_bytes: int,
    max_groups: int,
    max_tasks: int,
    bbox: tuple[float, float, float, float] | None = None,
) -> dict[str, Any]:
    if (
        min(
            target_rows,
            max_selected_uncompressed_bytes,
            max_groups,
            max_tasks,
        )
        <= 0
    ):
        raise ValueError("row-group planning gates must be positive")
    if max_tasks > 128:
        raise ValueError("address map task count cannot exceed 128")
    tasks: list[dict[str, Any]] = []
    pending_ranges: list[dict[str, Any]] = []
    pending_rows = pending_compressed = pending_uncompressed = pending_groups = 0
    pending_country_rows: Counter[str] = Counter()
    pending_mixed_rows = 0

    def finish() -> None:
        nonlocal pending_ranges, pending_rows, pending_compressed
        nonlocal pending_uncompressed, pending_groups, pending_country_rows
        nonlocal pending_mixed_rows
        if not pending_ranges:
            return
        tasks.append(
            {
                "index": len(tasks),
                "source_objects": len(pending_ranges),
                "row_groups": pending_groups,
                "rows": pending_rows,
                "selected_compressed_bytes": pending_compressed,
                "selected_uncompressed_bytes": pending_uncompressed,
                "exact_country_rows": dict(sorted(pending_country_rows.items())),
                "mixed_or_unknown_country_rows": pending_mixed_rows,
                "ranges": pending_ranges,
            }
        )
        pending_ranges = []
        pending_rows = pending_compressed = pending_uncompressed = pending_groups = 0
        pending_country_rows = Counter()
        pending_mixed_rows = 0

    for source in objects:
        for group in source["groups"]:
            # bbox-scoped plans skip non-intersecting groups. A skipped group
            # naturally breaks contiguity, so the surviving groups still pack
            # into contiguous per-object ranges. When bbox is None the guard is
            # inert and the default plan is byte-identical.
            if bbox is not None and not group_bbox_intersects(group, bbox):
                continue
            next_rows = pending_rows + group["rows"]
            next_bytes = pending_uncompressed + group["selected_uncompressed_bytes"]
            if pending_ranges and (
                next_rows > target_rows
                or next_bytes > max_selected_uncompressed_bytes
                or pending_groups >= max_groups
            ):
                finish()
            if group["selected_uncompressed_bytes"] > max_selected_uncompressed_bytes:
                raise ValueError(
                    f"row group {group['index']} in {source['uri']} exceeds the task byte cap"
                )
            if group["rows"] > target_rows:
                raise ValueError(
                    f"row group {group['index']} in {source['uri']} exceeds the task row cap"
                )
            if (
                pending_ranges
                and pending_ranges[-1]["uri"] == source["uri"]
                and pending_ranges[-1]["last_row_group"] + 1 == group["index"]
            ):
                pending_ranges[-1]["last_row_group"] = group["index"]
                pending_ranges[-1]["row_groups"] += 1
                pending_ranges[-1]["rows"] += group["rows"]
                pending_ranges[-1]["selected_compressed_bytes"] += group[
                    "selected_compressed_bytes"
                ]
                pending_ranges[-1]["selected_uncompressed_bytes"] += group[
                    "selected_uncompressed_bytes"
                ]
            else:
                pending_ranges.append(
                    {
                        "uri": source["uri"],
                        "etag": source["etag"],
                        "source_bytes": source.get("bytes"),
                        "source_records": source.get("records"),
                        "source_row_groups": source.get(
                            "row_groups", len(source["groups"])
                        ),
                        "first_row_group": group["index"],
                        "last_row_group": group["index"],
                        "row_groups": 1,
                        "rows": group["rows"],
                        "selected_compressed_bytes": group["selected_compressed_bytes"],
                        "selected_uncompressed_bytes": group[
                            "selected_uncompressed_bytes"
                        ],
                    }
                )
            pending_rows += group["rows"]
            pending_compressed += group["selected_compressed_bytes"]
            pending_uncompressed += group["selected_uncompressed_bytes"]
            pending_groups += 1
            if group.get("exact_country"):
                pending_country_rows[group["exact_country"]] += group["rows"]
            else:
                pending_mixed_rows += group["rows"]
    finish()
    planned_rows = sum(task["rows"] for task in tasks)
    if bbox is None:
        source_rows = sum(source["records"] for source in objects)
        if planned_rows != source_rows:
            raise ValueError("planned rows do not reconcile with the source inventory")
    else:
        scoped_rows = sum(
            group["rows"]
            for source in objects
            for group in source["groups"]
            if group_bbox_intersects(group, bbox)
        )
        if planned_rows != scoped_rows:
            raise ValueError(
                "planned rows do not reconcile with the bbox-scoped inventory"
            )
    safe = len(tasks) <= max_tasks
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "gates": {
            "target_rows": target_rows,
            "max_selected_uncompressed_bytes": max_selected_uncompressed_bytes,
            "max_groups": max_groups,
            "max_tasks": max_tasks,
        },
        "safe_at_configured_task_count": safe,
        "task_count": len(tasks),
        "task_rows": distribution([task["rows"] for task in tasks]),
        "task_selected_compressed_bytes": distribution(
            [task["selected_compressed_bytes"] for task in tasks]
        ),
        "task_selected_uncompressed_bytes": distribution(
            [task["selected_uncompressed_bytes"] for task in tasks]
        ),
        "tasks": tasks,
    }
    if bbox is not None:
        qxmin, qymin, qxmax, qymax = bbox
        all_groups = [group for source in objects for group in source["groups"]]
        selected_groups = [
            group for group in all_groups if group_bbox_intersects(group, bbox)
        ]
        no_stats_groups = [
            group for group in selected_groups if not group.get("bbox_stats_complete")
        ]
        # Every intersecting row group is read in full, so the served rows are a
        # superset of the box; see the module docstring for why v1 prunes at
        # row-group granularity only.
        plan["bbox_scope"] = "row_group_approximate"
        plan["bbox"] = {
            "xmin": qxmin,
            "ymin": qymin,
            "xmax": qxmax,
            "ymax": qymax,
        }
        plan["bbox_row_groups"] = {
            "total": len(all_groups),
            "selected": len(selected_groups),
            "pruned": len(all_groups) - len(selected_groups),
            "no_stats_conservative": len(no_stats_groups),
        }
        plan["bbox_scoped_rows"] = planned_rows
    return plan


def build_report(
    release: str,
    objects: list[dict[str, Any]],
    plan: dict[str, Any],
) -> dict[str, Any]:
    if not objects:
        raise ValueError("address inventory must contain at least one object")
    schema_contract = objects[0].get("schema_contract")
    if not isinstance(schema_contract, dict):
        raise ValueError("address inventory object is missing its schema contract")
    if any(source.get("schema_contract") != schema_contract for source in objects):
        raise ValueError("required address schema differs across source objects")
    if canonical_schema_contract(schema_contract.get("fields")) != schema_contract:
        raise ValueError("address schema contract fingerprint differs")
    validate_footer_facts(release, objects, schema_contract)
    if "bbox" in plan:
        raise ValueError("global address inventory cannot use a bbox-scoped plan")
    gates = plan.get("gates") if isinstance(plan, dict) else None
    if not isinstance(gates, dict) or set(gates) != {
        "target_rows",
        "max_selected_uncompressed_bytes",
        "max_groups",
        "max_tasks",
    }:
        raise ValueError("address map plan gates are invalid")
    rebuilt_plan = plan_contiguous_ranges(
        objects,
        target_rows=gates["target_rows"],
        max_selected_uncompressed_bytes=gates["max_selected_uncompressed_bytes"],
        max_groups=gates["max_groups"],
        max_tasks=gates["max_tasks"],
    )
    if rebuilt_plan != plan:
        raise ValueError("address map plan differs from deterministic footer packing")
    if (
        not plan.get("safe_at_configured_task_count")
        or plan.get("task_count") != len(plan.get("tasks", []))
        or plan["task_count"] > gates["max_tasks"]
        or plan["task_count"] > 128
    ):
        raise ValueError("address map plan exceeds its configured task cap")
    plan = plan_with_task_digests(plan)
    groups = [group for source in objects for group in source["groups"]]
    exact_countries: Counter[str] = Counter()
    exact_country_rows: Counter[str] = Counter()
    for group in groups:
        if group["exact_country"]:
            exact_countries[group["exact_country"]] += 1
            exact_country_rows[group["exact_country"]] += group["rows"]
    source_inventory = {
        "schema": SOURCE_INVENTORY_SCHEMA,
        "release": release,
        "family": "addresses",
        "theme": "addresses",
        "type": "address",
        "schema_version": SCHEMA_CONTRACT_VERSION,
        "discovery": {
            "kind": "overture-s3-listing",
            "source": (
                f"s3://{BUCKET}/release/{release}/theme=addresses/type=address/"
            ),
        },
        "objects": [
            {
                key: source[key]
                for key in ("uri", "etag", "bytes", "records", "row_groups")
            }
            for source in objects
        ],
    }
    source_inventory_sha256 = sha256_value(source_inventory)
    report = {
        "schema": SCHEMA,
        "release": release,
        "selected_column_roots": sorted(SELECTED_COLUMN_ROOTS),
        "source_inventory": source_inventory,
        "source_inventory_sha256": source_inventory_sha256,
        "schema_contract": schema_contract,
        "totals": {
            "objects": len(objects),
            "source_bytes": sum(source["bytes"] for source in objects),
            "records": sum(source["records"] for source in objects),
            "row_groups": len(groups),
            "selected_compressed_bytes": sum(
                group["selected_compressed_bytes"] for group in groups
            ),
            "selected_uncompressed_bytes": sum(
                group["selected_uncompressed_bytes"] for group in groups
            ),
            "exact_country_row_groups": sum(exact_countries.values()),
            "mixed_or_unknown_country_row_groups": len(groups)
            - sum(exact_countries.values()),
        },
        "distributions": {
            "object_source_bytes": distribution(
                [source["bytes"] for source in objects]
            ),
            "object_records": distribution([source["records"] for source in objects]),
            "row_group_rows": distribution([group["rows"] for group in groups]),
            "row_group_selected_compressed_bytes": distribution(
                [group["selected_compressed_bytes"] for group in groups]
            ),
            "row_group_selected_uncompressed_bytes": distribution(
                [group["selected_uncompressed_bytes"] for group in groups]
            ),
        },
        "exact_country_row_groups": dict(sorted(exact_countries.items())),
        "exact_country_rows": dict(sorted(exact_country_rows.items())),
        "plan": plan,
        "objects": objects,
        "limitations": [
            "country counts come from exact min=max Parquet statistics; mixed row groups are not attributed",
            "footer bytes describe encoded source columns, not Python/Arrow heap amplification",
            "the plan proves deterministic range sizes, not hosted execution or output-shard skew",
        ],
    }
    report["inventory_sha256"] = sha256_value(inventory_identity_payload(report))
    return report


def format_bytes(value: int) -> str:
    return f"{value / 1_000_000_000:.3f} GB"


def markdown(report: dict[str, Any]) -> str:
    totals = report["totals"]
    plan = report["plan"]
    country_rows = sorted(
        report["exact_country_rows"].items(), key=lambda item: (-item[1], item[0])
    )[:12]
    lines = [
        "# Address row-group inventory and task-plan report",
        "",
        f"Release: `{report['release']}`",
        "",
        "## Decision",
        "",
        (
            f"The contiguous row-group plan uses **{plan['task_count']} tasks** "
            f"against the configured maximum of {plan['gates']['max_tasks']}: "
            f"**{'PASS' if plan['safe_at_configured_task_count'] else 'FAIL'}**."
        ),
        "",
        "This is a footer-only planning result. Hosted execution and compact-output "
        "skew remain separate gates.",
        "",
    ]
    if "bbox" in plan:
        box = plan["bbox"]
        scoped = plan["bbox_row_groups"]
        lines.extend(
            [
                (
                    f"Scope: **bbox {box['xmin']},{box['ymin']},{box['xmax']},"
                    f"{box['ymax']}** (`{plan['bbox_scope']}`). "
                    f"{scoped['selected']:,} of {scoped['total']:,} row groups "
                    f"intersect the box "
                    f"({scoped['no_stats_conservative']:,} kept conservatively for "
                    f"missing statistics); {scoped['pruned']:,} pruned. Every "
                    f"intersecting group is read in full, so the served rows are a "
                    f"superset of the box."
                ),
                "",
            ]
        )
    lines += [
        "## Complete source inventory",
        "",
        f"- Objects: {totals['objects']:,}",
        f"- Source bytes: {format_bytes(totals['source_bytes'])}",
        f"- Records: {totals['records']:,}",
        f"- Row groups: {totals['row_groups']:,}",
        f"- Selected compressed column bytes: {format_bytes(totals['selected_compressed_bytes'])}",
        f"- Selected uncompressed column bytes: {format_bytes(totals['selected_uncompressed_bytes'])}",
        f"- Exact-country row groups: {totals['exact_country_row_groups']:,}",
        f"- Mixed/unknown-country row groups: {totals['mixed_or_unknown_country_row_groups']:,}",
        "",
        "## Planned task tails",
        "",
        "| measure | min | p50 | p95 | max |",
        "|---|---:|---:|---:|---:|",
    ]
    for label, values in (
        ("rows", plan["task_rows"]),
        ("selected compressed bytes", plan["task_selected_compressed_bytes"]),
        ("selected uncompressed bytes", plan["task_selected_uncompressed_bytes"]),
    ):
        lines.append(
            f"| {label} | {values['min']:,} | {values['p50']:,} | "
            f"{values['p95']:,} | {values['max']:,} |"
        )
    lines.extend(
        [
            "",
            "## Largest exact-country populations observable from footer statistics",
            "",
            "| country | rows in exact-country row groups |",
            "|---|---:|",
        ]
    )
    lines.extend(f"| {country} | {rows:,} |" for country, rows in country_rows)
    lines.extend(
        [
            "",
            "## Remaining gate",
            "",
            "Run small, median, large, and non-US planned ranges through projection, "
            "compact assembly, strict decode, and Worker reads. Footer statistics "
            "cannot establish retained-row coverage, heap, or compact-shard skew.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--target-rows", type=int, default=4_000_000)
    parser.add_argument(
        "--max-selected-uncompressed-bytes", type=int, default=400_000_000
    )
    parser.add_argument("--max-groups", type=int, default=72)
    parser.add_argument("--max-tasks", type=int, default=128)
    parser.add_argument(
        "--xmin",
        type=float,
        help="bbox-scoped plan: western longitude bound (with --ymin/--xmax/--ymax)",
    )
    parser.add_argument("--ymin", type=float, help="bbox-scoped plan: southern bound")
    parser.add_argument("--xmax", type=float, help="bbox-scoped plan: eastern bound")
    parser.add_argument("--ymax", type=float, help="bbox-scoped plan: northern bound")
    args = parser.parse_args()
    if args.workers <= 0 or args.workers > 32:
        raise SystemExit("--workers must be between 1 and 32")
    bbox_flags = (args.xmin, args.ymin, args.xmax, args.ymax)
    if any(value is not None for value in bbox_flags) and not all(
        value is not None for value in bbox_flags
    ):
        raise SystemExit("--xmin, --ymin, --xmax, and --ymax must be supplied together")
    bbox: tuple[float, float, float, float] | None = None
    if all(value is not None for value in bbox_flags):
        if args.xmin >= args.xmax or args.ymin >= args.ymax:
            raise SystemExit("bbox must have xmin<xmax and ymin<ymax")
        bbox = (args.xmin, args.ymin, args.xmax, args.ymax)

    try:
        import pyarrow.fs as pafs
    except ImportError as exc:  # pragma: no cover - hosted dependency boundary
        raise SystemExit("inventory_address_rowgroups.py requires pyarrow") from exc

    sources = list_objects(args.release)
    filesystem = pafs.S3FileSystem(anonymous=True, region=REGION)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        objects = list(
            executor.map(lambda item: inventory_object(item, filesystem), sources)
        )
    objects.sort(key=lambda item: item["uri"])
    plan = plan_contiguous_ranges(
        objects,
        target_rows=args.target_rows,
        max_selected_uncompressed_bytes=args.max_selected_uncompressed_bytes,
        max_groups=args.max_groups,
        max_tasks=args.max_tasks,
        bbox=bbox,
    )
    report = build_report(args.release, objects, plan)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    args.markdown_out.write_text(markdown(report))
    print(
        canonical_json(
            {
                "totals": report["totals"],
                "plan": {
                    key: plan[key]
                    for key in (
                        "task_count",
                        "safe_at_configured_task_count",
                        "task_rows",
                        "task_selected_uncompressed_bytes",
                    )
                },
            }
        )
    )


if __name__ == "__main__":
    main()
