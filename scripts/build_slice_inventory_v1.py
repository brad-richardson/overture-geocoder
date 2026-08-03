#!/usr/bin/env python3
"""Build a small REAL construction-v1 inventory covering a geographic bbox.

The committed planet inventories are the only inventories in the repo, so every
construction-v1 run has been all-or-nothing. This builds a valid inventory over a
single source object with a fine-grained map plan, and reports which task covers a
bbox, so a slice can be run end to end in seconds against real Overture data
instead of fixtures.

Both Overture Places and Overture Addresses carry per-row-group bbox statistics,
so the covering row group is found from footers alone -- no feature data is
downloaded to locate it.

Places (Monaco):

    python scripts/build_slice_inventory_v1.py --release 2026-07-22.0 \\
      --bbox 7.36 43.71 7.47 43.78 --output slice/inventory.json

Addresses (Seattle, release 2026-07-22.0 -> object 8, row group 108, task 54,
104,928 rows). The box is east of longitude **-122.34375**, which IS a level-8
cell boundary (x = 41), so the covering row group straddles it and the slice
spans cells ``c328`` and ``c329``:

    python scripts/build_slice_inventory_v1.py --family addresses \\
      --release 2026-07-22.0 --bbox -122.34 47.59 -122.30 47.63 \\
      --output slice/address-inventory.json

The two families keep their own inventory FORMATS -- Places uses
``places_inventory_v1`` (``map_plan``), Addresses uses
``inventory_address_rowgroups`` (``plan``), and each is consumed by its own
projector. Only the slicing STRATEGY is shared: one object, the finest plan its
task cap admits, and the task index that covers the bbox.

``--bbox`` is NOT a spatial slice. It selects the first row group of the first
object whose footer bbox statistics overlap the box, and the map task holding
that row group then runs IN FULL -- there is no row-level clipping, so the slice
is a superset of the box drawn from exactly one task. Widening or narrowing the
box changes WHICH task runs, not how much of it runs. The task index is a
property of the release's row-group layout and must be re-derived when the
release moves; the CI smoke pins it and fails closed on drift.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import inventory_address_rowgroups as AINV  # noqa: E402
import places_inventory_v1 as INV  # noqa: E402

# Both families cap their map plan at 128 tasks, so the finest usable plan over an
# object is ceil(row_groups / 128) groups per task -- two for the common
# 256-row-group object, four for the 512-row-group address objects.
MAX_TASKS = 128
FAMILIES = ("addresses", "places")


def finest_groups_per_task(row_groups: int) -> int:
    """Smallest groups-per-task whose plan still fits ``MAX_TASKS``.

    A plan must cover EVERY row group of the object (both planners reconcile
    planned rows against the object's record count), so the task count is
    ceil(row_groups / groups_per_task) and a fixed 2 hard-fails on the two
    512-row-group address objects in 2026-07-22.0 -- 256 tasks against a cap of
    128. Deriving it keeps the plan as fine as the cap allows on any object.
    """
    if row_groups <= 0:
        raise SystemExit("source object reports no row groups")
    return -(-row_groups // MAX_TASKS)


def covering_row_group(
    filesystem, uri: str, bbox: tuple[float, float, float, float]
) -> tuple[int | None, int]:
    """``(first row group whose bbox statistics overlap bbox, row group count)``.

    The index is ``None`` both when no row group overlaps and when the object
    carries no bbox statistics columns at all: either way this object cannot be
    sliced by footer statistics, and the caller moves to the next one. That is
    deliberately STRICTER than the address planner's own bbox pruning, which
    treats a row group with missing statistics as conservatively intersecting --
    the planner must never drop data it cannot prove is outside the box, whereas
    a harness picking a demonstration slice would rather skip an object than pin
    a task index to a row group it could not actually locate.
    """
    import pyarrow.parquet as pq

    x0, y0, x1, y1 = bbox
    with filesystem.open_input_file(uri.removeprefix("s3://")) as handle:
        metadata = pq.ParquetFile(handle).metadata
        row_groups = metadata.num_row_groups
        first = metadata.row_group(0)
        columns = {
            first.column(i).path_in_schema: i for i in range(first.num_columns)
        }
        needed = ("bbox.xmin", "bbox.xmax", "bbox.ymin", "bbox.ymax")
        if not all(name in columns for name in needed):
            return None, row_groups
        for index in range(row_groups):
            group = metadata.row_group(index)
            stats = {
                name: group.column(columns[name]).statistics for name in needed
            }
            if any(value is None for value in stats.values()):
                continue
            if (
                stats["bbox.xmin"].min <= x1
                and stats["bbox.xmax"].max >= x0
                and stats["bbox.ymin"].min <= y1
                and stats["bbox.ymax"].max >= y0
            ):
                return index, row_groups
    return None, row_groups


def places_slice(
    source: dict[str, Any],
    filesystem,
    *,
    release: str,
    groups_per_task: int,
    schema_profile: str = "auto",
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    details = INV.inspect_parquet_object(
        source, filesystem, profile=schema_profile
    )
    largest = max(group["rows"] for group in details["row_groups"])
    inventory = INV.build_inventory(
        release, [source], lambda _s: details,
        # target_rows must exceed the largest row group or planning rejects
        # it outright; groups-per-task is what actually bounds a task.
        target_rows=largest * groups_per_task + 1,
        max_selected_uncompressed_bytes=1_000_000_000,
        max_groups=groups_per_task,
        max_tasks=MAX_TASKS,
    )
    INV.validate_inventory(inventory)
    return inventory, inventory["map_plan"]["tasks"], inventory["inventory_sha256"]


def addresses_slice(
    source: dict[str, Any], filesystem, *, release: str, groups_per_task: int
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """One-object address inventory in the canonical row-group inventory format.

    Deliberately NOT the ``--xmin/--ymin/--xmax/--ymax`` bbox-scoped plan that
    ``inventory_address_rowgroups`` also knows how to build: that plan carries a
    ``bbox`` key, and both ``build_report`` and ``validate_canonical_inventory``
    reject or fail to reproduce it, so the address projector cannot consume one.
    Scoping to a single OBJECT keeps the plan a plain deterministic packing of
    that object's footers, which the canonical validator reproduces exactly.
    """
    details = AINV.inventory_object(source, filesystem)
    largest = max(group["rows"] for group in details["groups"])
    plan = AINV.plan_contiguous_ranges(
        [details],
        target_rows=largest * groups_per_task + 1,
        max_selected_uncompressed_bytes=1_000_000_000,
        max_groups=groups_per_task,
        max_tasks=MAX_TASKS,
    )
    report = AINV.build_report(release, [details], plan)
    identity = AINV.validate_canonical_inventory(report)
    return report, identity["tasks"], identity["inventory_sha256"]


def task_covering(tasks: list[dict[str, Any]], row_group: int) -> int:
    """Index of the task whose (single-object) range contains ``row_group``."""
    for index, task in enumerate(tasks):
        selected = task["ranges"][0]
        if selected["first_row_group"] <= row_group <= selected["last_row_group"]:
            return index
    # Unreachable while the plan covers every row group of the object, which both
    # planners enforce -- so if it happens the plan and the footer disagree, and
    # saying which row group went missing is the whole diagnosis.
    raise SystemExit(
        f"row group {row_group} is in no task of a {len(tasks)}-task plan; the plan "
        "does not cover its own source object"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--family", choices=FAMILIES, default="places")
    parser.add_argument(
        "--bbox", nargs=4, type=float, required=True,
        metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--schema-profile",
        choices=("auto", "legacy", "taxonomy"),
        default="auto",
        help=(
            "Places category contract. Use taxonomy to exercise the migration "
            "against a dual-field release; addresses accept only auto."
        ),
    )
    parser.add_argument(
        "--groups-per-task", type=int, default=None,
        help="Row groups per map task. Default: the finest value the 128-task cap "
             "admits for the object actually selected (2 for a 256-row-group "
             "object, 4 for the 512-row-group address objects).",
    )
    args = parser.parse_args(argv)

    import pyarrow.fs as pafs

    filesystem = pafs.S3FileSystem(anonymous=True, region=INV.REGION)
    if args.family == "addresses":
        if args.schema_profile != "auto":
            raise SystemExit("--schema-profile applies only to the places family")
        listed = sorted(AINV.list_objects(args.release), key=lambda i: i["uri"])
        build = addresses_slice
    else:
        listed = sorted(INV.list_source_objects(args.release), key=lambda i: i["uri"])
        build = places_slice

    for object_index, source in enumerate(listed):
        found, row_groups = covering_row_group(
            filesystem, source["uri"], tuple(args.bbox)
        )
        if found is None:
            continue
        groups_per_task = args.groups_per_task or finest_groups_per_task(row_groups)
        build_kwargs = {
            "release": args.release,
            "groups_per_task": groups_per_task,
        }
        if args.family == "places":
            build_kwargs["schema_profile"] = args.schema_profile
        inventory, tasks, inventory_sha256 = build(
            source, filesystem, **build_kwargs
        )
        task_index = task_covering(tasks, found)
        task = tasks[task_index]
        # Places records the planned count as expected_input_records, the address
        # plan as rows. Fail closed rather than print a null: the CI drift gate and
        # the PR evidence both read this number.
        task_records = task.get("expected_input_records", task.get("rows"))
        if not isinstance(task_records, int) or task_records <= 0:
            raise SystemExit(
                f"task {task_index} reports no planned record count; the inventory "
                "format changed and this reporter needs updating"
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
        summary = {
            "family": args.family,
            "object_index": object_index,
            "row_group": found,
            "source_row_groups": row_groups,
            "groups_per_task": groups_per_task,
            "task_index": task_index,
            "task_records": task_records,
            "tasks": len(tasks),
            "inventory_sha256": inventory_sha256,
        }
        if args.family == "places":
            summary["schema_profile"] = INV.schema_profile_name(
                inventory["schema_contract"]
            )
        print(json.dumps(summary, sort_keys=True))
        return 0

    raise SystemExit(f"no row group overlaps bbox {args.bbox}")


if __name__ == "__main__":
    raise SystemExit(main())
