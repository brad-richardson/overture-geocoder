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


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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
        **source,
        "records": metadata.num_rows,
        "row_groups": metadata.num_row_groups,
        "selected_compressed_bytes": sum(
            group["selected_compressed_bytes"] for group in groups
        ),
        "selected_uncompressed_bytes": sum(
            group["selected_uncompressed_bytes"] for group in groups
        ),
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
        "schema_version": release,
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
    return {
        "schema": SCHEMA,
        "release": release,
        "selected_column_roots": sorted(SELECTED_COLUMN_ROOTS),
        "source_inventory": source_inventory,
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
