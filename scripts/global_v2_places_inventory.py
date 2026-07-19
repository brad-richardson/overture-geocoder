#!/usr/bin/env python3
"""Inventory the pinned Overture Places source for a global v2 family build.

The inventory is deliberately footer-only.  It lists the exact objects under the
approved public Overture prefix, records every Parquet row group, fingerprints
the required nested projection schema (including nullability), and packs row
groups into bounded map tasks.  Network and footer readers are injectable so the
contract is hermetically testable.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any


BUCKET = "overturemaps-us-west-2"
REGION = "us-west-2"
INVENTORY_SCHEMA = "overture-global-v2-places-inventory-v1"
SCHEMA_CONTRACT_VERSION = "overture-places-required-schema-v1"
TASK_PLAN_SCHEMA = "overture-global-v2-places-map-plan-v1"
RELEASE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+")

# These are the exact source roots consumed by the strict Places mapper.  The
# fingerprint below is narrower than the complete Overture schema: unrelated
# additive properties therefore do not invalidate a build, while any drift in a
# field whose bytes affect serving does.
PROJECTED_COLUMN_ROOTS = {
    "addresses",
    "basic_category",
    "brand",
    "categories",
    "confidence",
    "geometry",
    "id",
    "names",
    "operating_status",
}

# Canonical Arrow type spellings accepted by the current producer.  Nullability
# is intentionally learned from the real release, recorded per path, and then
# required to be identical for every source object.
REQUIRED_FIELD_TYPES: dict[str, str] = {
    "addresses": "list<struct>",
    "addresses[].country": "string",
    "addresses[].locality": "string",
    "addresses[].region": "string",
    "basic_category": "string",
    "brand": "struct",
    "brand.names": "struct",
    "brand.names.primary": "string",
    "categories": "struct",
    "categories.primary": "string",
    "confidence": "float64",
    "geometry": "binary",
    "id": "string",
    "names": "struct",
    "names.common": "map<string,string>",
    "names.primary": "string",
    "operating_status": "string",
}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def approved_prefix(release: str) -> str:
    if not RELEASE_RE.fullmatch(release):
        raise ValueError("release must use YYYY-MM-DD.N")
    return f"s3://{BUCKET}/release/{release}/theme=places/type=place/"


def is_approved_source_uri(uri: Any, source_prefix: str) -> bool:
    if not isinstance(uri, str) or not uri.startswith(source_prefix):
        return False
    relative = uri[len(source_prefix) :]
    parts = relative.split("/")
    return (
        bool(relative)
        and relative.endswith(".parquet")
        and all(part not in {"", ".", ".."} for part in parts)
        and "?" not in relative
        and "#" not in relative
    )


def _listing_url(prefix: str, continuation_token: str | None = None) -> str:
    query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
    if continuation_token is not None:
        query["continuation-token"] = continuation_token
    return (
        f"https://{BUCKET}.s3.{REGION}.amazonaws.com/?{urllib.parse.urlencode(query)}"
    )


def _default_fetch(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "overture-geocoder-places-inventory/1"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def parse_listing_page(payload: bytes) -> tuple[list[dict[str, Any]], str | None]:
    root = ET.fromstring(payload)
    namespace = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    objects: list[dict[str, Any]] = []
    for item in root.findall("s3:Contents", namespace):
        key = item.findtext("s3:Key", namespaces=namespace)
        etag = item.findtext("s3:ETag", namespaces=namespace)
        size = item.findtext("s3:Size", namespaces=namespace)
        if key is None or etag is None or size is None:
            raise ValueError("S3 listing object is missing key, ETag, or size")
        if key.endswith(".parquet") and int(size) > 0:
            objects.append(
                {
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


def list_source_objects(
    release: str, *, fetch: Callable[[str], bytes] = _default_fetch
) -> list[dict[str, Any]]:
    source_prefix = approved_prefix(release)
    key_prefix = source_prefix.removeprefix(f"s3://{BUCKET}/")
    result: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        objects, token = parse_listing_page(fetch(_listing_url(key_prefix, token)))
        result.extend(objects)
        if token is None:
            break
    if not result:
        raise ValueError("Places source listing is empty")
    uris = [item["uri"] for item in result]
    if len(uris) != len(set(uris)):
        raise ValueError("Places source listing contains duplicate objects")
    if any(not is_approved_source_uri(uri, source_prefix) for uri in uris):
        raise ValueError("Places source listing escaped the approved prefix")
    return sorted(result, key=lambda item: item["uri"])


def canonical_schema_contract(fields: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Validate and fingerprint required path/type/nullability descriptors."""

    supplied: dict[str, dict[str, Any]] = {}
    for raw in fields:
        if not isinstance(raw, dict) or set(raw) != {"path", "type", "nullable"}:
            raise ValueError("schema fields must contain path, type, and nullable")
        path = raw["path"]
        field_type = raw["type"]
        nullable = raw["nullable"]
        if not isinstance(path, str) or path in supplied:
            raise ValueError("schema field paths must be unique strings")
        if not isinstance(field_type, str) or type(nullable) is not bool:
            raise ValueError("schema field type/nullability is invalid")
        supplied[path] = {"path": path, "type": field_type, "nullable": nullable}
    if set(supplied) != set(REQUIRED_FIELD_TYPES):
        missing = sorted(set(REQUIRED_FIELD_TYPES) - set(supplied))
        extra = sorted(set(supplied) - set(REQUIRED_FIELD_TYPES))
        raise ValueError(
            f"required Places schema paths differ: missing={missing}, extra={extra}"
        )
    wrong = sorted(
        path
        for path, expected in REQUIRED_FIELD_TYPES.items()
        if supplied[path]["type"] != expected
    )
    if wrong:
        raise ValueError(f"required Places schema types differ: {wrong}")
    normalized = [supplied[path] for path in sorted(supplied)]
    fingerprint_input = {
        "version": SCHEMA_CONTRACT_VERSION,
        "fields": normalized,
    }
    return {
        **fingerprint_input,
        "fingerprint_sha256": sha256_value(fingerprint_input),
    }


def _canonical_arrow_type(data_type: Any) -> str:
    """Return the narrow, version-independent type spelling used above."""

    import pyarrow as pa

    if pa.types.is_string(data_type):
        return "string"
    if pa.types.is_binary(data_type):
        return "binary"
    if pa.types.is_float64(data_type):
        return "float64"
    if pa.types.is_struct(data_type):
        return "struct"
    if pa.types.is_list(data_type) and pa.types.is_struct(data_type.value_type):
        return "list<struct>"
    if pa.types.is_map(data_type):
        if pa.types.is_string(data_type.key_type) and pa.types.is_string(
            data_type.item_type
        ):
            return "map<string,string>"
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
            raise ValueError(f"required Places field is not a list: {name}")
        data_type = data_type.value_type
    for part in parts:
        unwrap_list = part.endswith("[]")
        name = part[:-2] if unwrap_list else part
        if not pa.types.is_struct(data_type):
            raise ValueError(f"required Places parent is not a struct: {path}")
        field = data_type.field(name)
        data_type = field.type
        if unwrap_list:
            if not pa.types.is_list(data_type):
                raise ValueError(f"required Places field is not a list: {path}")
            data_type = data_type.value_type
    return field


def schema_contract_from_arrow(schema: Any) -> dict[str, Any]:
    descriptors = []
    for path in sorted(REQUIRED_FIELD_TYPES):
        if path == "addresses":
            field = schema.field("addresses")
        else:
            field = _arrow_field_at_path(schema, path)
        descriptors.append(
            {
                "path": path,
                "type": _canonical_arrow_type(field.type),
                "nullable": bool(field.nullable),
            }
        )
    return canonical_schema_contract(descriptors)


def inspect_parquet_object(source: dict[str, Any], filesystem: Any) -> dict[str, Any]:
    """Read one Parquet footer and return deterministic inventory metadata."""

    import pyarrow.parquet as pq

    path = source["uri"].removeprefix("s3://")
    parquet = pq.ParquetFile(path, filesystem=filesystem)
    contract = schema_contract_from_arrow(parquet.schema_arrow)
    row_groups: list[dict[str, Any]] = []
    for index in range(parquet.metadata.num_row_groups):
        group = parquet.metadata.row_group(index)
        selected_compressed = 0
        selected_uncompressed = 0
        for column_index in range(group.num_columns):
            column = group.column(column_index)
            root = column.path_in_schema.split(".", 1)[0]
            if root in PROJECTED_COLUMN_ROOTS:
                selected_compressed += column.total_compressed_size
                selected_uncompressed += column.total_uncompressed_size
        row_groups.append(
            {
                "index": index,
                "rows": group.num_rows,
                "selected_compressed_bytes": selected_compressed,
                "selected_uncompressed_bytes": selected_uncompressed,
            }
        )
    if (
        not row_groups
        or sum(group["rows"] for group in row_groups) != parquet.metadata.num_rows
    ):
        raise ValueError(
            f"Places row-group counts do not reconcile for {source['uri']}"
        )
    return {
        "records": parquet.metadata.num_rows,
        "row_group_count": parquet.metadata.num_row_groups,
        "row_groups": row_groups,
        "schema_contract": contract,
    }


def _append_group_range(
    ranges: list[dict[str, Any]],
    object_index: int,
    source: dict[str, Any],
    group: dict[str, Any],
) -> None:
    if (
        ranges
        and ranges[-1]["object_index"] == object_index
        and ranges[-1]["last_row_group"] + 1 == group["index"]
    ):
        current = ranges[-1]
        current["last_row_group"] = group["index"]
        current["row_groups"] += 1
        current["rows"] += group["rows"]
        current["selected_compressed_bytes"] += group["selected_compressed_bytes"]
        current["selected_uncompressed_bytes"] += group["selected_uncompressed_bytes"]
        return
    ranges.append(
        {
            "object_index": object_index,
            "uri": source["uri"],
            "etag": source["etag"],
            "first_row_group": group["index"],
            "last_row_group": group["index"],
            "row_groups": 1,
            "rows": group["rows"],
            "selected_compressed_bytes": group["selected_compressed_bytes"],
            "selected_uncompressed_bytes": group["selected_uncompressed_bytes"],
        }
    )


def plan_map_tasks(
    objects: list[dict[str, Any]],
    *,
    target_rows: int,
    max_selected_uncompressed_bytes: int,
    max_groups: int,
    max_tasks: int,
) -> dict[str, Any]:
    if min(target_rows, max_selected_uncompressed_bytes, max_groups, max_tasks) <= 0:
        raise ValueError("Places task planning limits must be positive")
    if max_tasks > 128:
        raise ValueError("Places map task count cannot exceed 128")
    tasks: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    pending_rows = pending_bytes = pending_groups = 0

    def finish() -> None:
        nonlocal pending, pending_rows, pending_bytes, pending_groups
        if not pending:
            return
        index = len(tasks)
        source_digest = sha256_value(pending)
        tasks.append(
            {
                "index": index,
                "task_digest": sha256_value(
                    {
                        "kind": "places-map",
                        "index": index,
                        "source_digest": source_digest,
                    }
                ),
                "source_digest": source_digest,
                "expected_input_records": pending_rows,
                "selected_uncompressed_bytes": pending_bytes,
                "row_groups": pending_groups,
                "ranges": pending,
            }
        )
        pending = []
        pending_rows = pending_bytes = pending_groups = 0

    for object_index, source in enumerate(objects):
        for group in source["row_groups"]:
            if group["rows"] > target_rows:
                raise ValueError(
                    f"Places row group {group['index']} in {source['uri']} exceeds target rows"
                )
            if group["selected_uncompressed_bytes"] > max_selected_uncompressed_bytes:
                raise ValueError(
                    f"Places row group {group['index']} in {source['uri']} exceeds byte cap"
                )
            if pending and (
                pending_rows + group["rows"] > target_rows
                or pending_bytes + group["selected_uncompressed_bytes"]
                > max_selected_uncompressed_bytes
                or pending_groups >= max_groups
            ):
                finish()
            _append_group_range(pending, object_index, source, group)
            pending_rows += group["rows"]
            pending_bytes += group["selected_uncompressed_bytes"]
            pending_groups += 1
    finish()
    if not tasks or len(tasks) > max_tasks:
        raise ValueError(
            f"Places inventory requires {len(tasks)} map tasks, above configured maximum {max_tasks}"
        )
    planned_rows = sum(task["expected_input_records"] for task in tasks)
    source_rows = sum(source["records"] for source in objects)
    if planned_rows != source_rows:
        raise ValueError("Places map tasks do not reconcile to source records")
    return {
        "schema": TASK_PLAN_SCHEMA,
        "limits": {
            "target_rows": target_rows,
            "max_selected_uncompressed_bytes": max_selected_uncompressed_bytes,
            "max_groups": max_groups,
            "max_tasks": max_tasks,
        },
        "task_count": len(tasks),
        "tasks": tasks,
    }


def build_inventory(
    release: str,
    listed_objects: list[dict[str, Any]],
    inspect: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    target_rows: int = 1_000_000,
    max_selected_uncompressed_bytes: int = 1_000_000_000,
    max_groups: int = 64,
    max_tasks: int = 128,
) -> dict[str, Any]:
    source_prefix = approved_prefix(release)
    if not listed_objects:
        raise ValueError("Places inventory requires source objects")
    normalized_listing = sorted(listed_objects, key=lambda item: item["uri"])
    if any(
        set(item) != {"uri", "etag", "bytes"}
        or not isinstance(item["uri"], str)
        or not is_approved_source_uri(item["uri"], source_prefix)
        or not isinstance(item["etag"], str)
        or not item["etag"]
        or type(item["bytes"]) is not int
        or item["bytes"] <= 0
        for item in normalized_listing
    ):
        raise ValueError("Places listing contains an invalid object identity")
    uris = [item["uri"] for item in normalized_listing]
    if len(uris) != len(set(uris)):
        raise ValueError("Places listing contains duplicate object URIs")

    objects: list[dict[str, Any]] = []
    schema_contract: dict[str, Any] | None = None
    for source in normalized_listing:
        details = inspect(source)
        if not isinstance(details, dict):
            raise ValueError("Places footer inspector returned an invalid result")
        contract = details.get("schema_contract")
        if not isinstance(contract, dict):
            raise ValueError("Places footer omitted its schema contract")
        # Rebuild it so a caller cannot inject a self-inconsistent fingerprint.
        contract = canonical_schema_contract(contract.get("fields", []))
        if schema_contract is None:
            schema_contract = contract
        elif contract != schema_contract:
            raise ValueError(f"Places required schema drifted at {source['uri']}")
        row_groups = details.get("row_groups")
        records = details.get("records")
        if (
            not isinstance(row_groups, list)
            or not row_groups
            or type(records) is not int
            or records <= 0
            or details.get("row_group_count") != len(row_groups)
        ):
            raise ValueError(f"Places footer metadata is invalid for {source['uri']}")
        for expected_index, group in enumerate(row_groups):
            if (
                not isinstance(group, dict)
                or set(group)
                != {
                    "index",
                    "rows",
                    "selected_compressed_bytes",
                    "selected_uncompressed_bytes",
                }
                or group["index"] != expected_index
                or any(
                    type(group[name]) is not int or group[name] < 0
                    for name in (
                        "rows",
                        "selected_compressed_bytes",
                        "selected_uncompressed_bytes",
                    )
                )
                or group["rows"] <= 0
            ):
                raise ValueError(
                    f"Places row-group metadata is invalid for {source['uri']}"
                )
        if sum(group["rows"] for group in row_groups) != records:
            raise ValueError(
                f"Places row-group rows do not reconcile for {source['uri']}"
            )
        objects.append(
            {
                **source,
                "records": records,
                "row_group_count": len(row_groups),
                "schema_fingerprint_sha256": contract["fingerprint_sha256"],
                "row_groups": row_groups,
            }
        )
    assert schema_contract is not None
    plan = plan_map_tasks(
        objects,
        target_rows=target_rows,
        max_selected_uncompressed_bytes=max_selected_uncompressed_bytes,
        max_groups=max_groups,
        max_tasks=max_tasks,
    )
    inventory_without_digest = {
        "schema": INVENTORY_SCHEMA,
        "release": release,
        "family": "places",
        "theme": "places",
        "type": "place",
        "source_prefix": source_prefix,
        "schema_contract": schema_contract,
        "objects": objects,
        "totals": {
            "objects": len(objects),
            "bytes": sum(item["bytes"] for item in objects),
            "records": sum(item["records"] for item in objects),
            "row_groups": sum(item["row_group_count"] for item in objects),
            "selected_compressed_bytes": sum(
                group["selected_compressed_bytes"]
                for item in objects
                for group in item["row_groups"]
            ),
            "selected_uncompressed_bytes": sum(
                group["selected_uncompressed_bytes"]
                for item in objects
                for group in item["row_groups"]
            ),
        },
        "map_plan": plan,
    }
    return {
        **inventory_without_digest,
        "inventory_sha256": sha256_value(inventory_without_digest),
    }


def validate_inventory(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != INVENTORY_SCHEMA:
        raise ValueError(f"Places inventory schema must be {INVENTORY_SCHEMA}")
    release = value.get("release")
    if not isinstance(release, str) or value.get("source_prefix") != approved_prefix(
        release
    ):
        raise ValueError("Places inventory source prefix differs from its release")
    digest = value.get("inventory_sha256")
    without_digest = {
        key: item for key, item in value.items() if key != "inventory_sha256"
    }
    if not isinstance(digest, str) or digest != sha256_value(without_digest):
        raise ValueError("Places inventory digest differs from its contents")
    contract = value.get("schema_contract")
    if (
        not isinstance(contract, dict)
        or canonical_schema_contract(contract.get("fields", [])) != contract
    ):
        raise ValueError("Places inventory schema contract is invalid")
    objects = value.get("objects")
    plan = value.get("map_plan")
    if not isinstance(objects, list) or not objects or not isinstance(plan, dict):
        raise ValueError("Places inventory object/task sets are invalid")
    limits = plan.get("limits")
    if not isinstance(limits, dict):
        raise ValueError("Places map plan limits are invalid")
    # Re-run the constructor from only the recorded immutable object/footer
    # facts. This validates every object, row group, aggregate, and task range;
    # a forged but self-hashed document cannot bypass semantic validation.
    try:
        listed = [
            {"uri": item["uri"], "etag": item["etag"], "bytes": item["bytes"]}
            for item in objects
        ]
        details_by_uri = {
            item["uri"]: {
                "records": item["records"],
                "row_group_count": item["row_group_count"],
                "row_groups": item["row_groups"],
                "schema_contract": contract,
            }
            for item in objects
        }
        rebuilt = build_inventory(
            release,
            listed,
            lambda source: details_by_uri[source["uri"]],
            **limits,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Places inventory contents are invalid") from exc
    if rebuilt != value:
        raise ValueError("Places inventory differs from its deterministic contents")
    return value


def _percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--target-rows", type=int, default=1_000_000)
    parser.add_argument(
        "--max-selected-uncompressed-bytes", type=int, default=1_000_000_000
    )
    parser.add_argument("--max-groups", type=int, default=64)
    parser.add_argument("--max-tasks", type=int, default=128)
    args = parser.parse_args()
    if not 1 <= args.workers <= 32:
        raise SystemExit("--workers must be between 1 and 32")
    try:
        import pyarrow.fs as pafs
    except ImportError as exc:  # pragma: no cover - hosted dependency boundary
        raise SystemExit("global_v2_places_inventory.py requires pyarrow") from exc
    filesystem = pafs.S3FileSystem(anonymous=True, region=REGION)
    listed = list_source_objects(args.release)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        inspected = list(
            executor.map(
                lambda source: inspect_parquet_object(source, filesystem), listed
            )
        )
    by_uri = {source["uri"]: details for source, details in zip(listed, inspected)}
    inventory = build_inventory(
        args.release,
        listed,
        lambda source: by_uri[source["uri"]],
        target_rows=args.target_rows,
        max_selected_uncompressed_bytes=args.max_selected_uncompressed_bytes,
        max_groups=args.max_groups,
        max_tasks=args.max_tasks,
    )
    validate_inventory(inventory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    task_rows = [
        task["expected_input_records"] for task in inventory["map_plan"]["tasks"]
    ]
    print(
        json.dumps(
            {
                "inventory_sha256": inventory["inventory_sha256"],
                "objects": inventory["totals"]["objects"],
                "records": inventory["totals"]["records"],
                "row_groups": inventory["totals"]["row_groups"],
                "map_tasks": len(task_rows),
                "task_rows_p50": _percentile(task_rows, 0.50),
                "task_rows_max": max(task_rows),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
