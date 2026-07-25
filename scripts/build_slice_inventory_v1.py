#!/usr/bin/env python3
"""Build a small REAL Places inventory covering a geographic bbox.

The committed planet inventory is the only Places inventory in the repo, so
every construction-v1 run has been all-or-nothing. This builds a valid inventory
over a single source object with a fine-grained map plan, and reports which task
covers a bbox, so a slice can be run end to end in seconds against real Overture
data instead of fixtures.

Overture's Places parquet carries per-row-group bbox statistics, so the covering
row group is found from footers alone -- no data is downloaded to locate it.

Monaco:

    python scripts/build_slice_inventory_v1.py --release 2026-07-22.0 \\
      --bbox 7.36 43.71 7.47 43.78 --output slice/inventory.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import places_inventory_v1 as INV  # noqa: E402

# plan_map_tasks caps the map plan at 128 tasks, so the finest usable plan over a
# 256-row-group object is two groups per task.
MAX_TASKS = 128


def covering_row_group(filesystem, uri: str, bbox: tuple[float, float, float, float]):
    """First row group whose bbox statistics overlap ``bbox``, or None."""
    import pyarrow.parquet as pq

    x0, y0, x1, y1 = bbox
    with filesystem.open_input_file(uri.removeprefix("s3://")) as handle:
        metadata = pq.ParquetFile(handle).metadata
        first = metadata.row_group(0)
        columns = {
            first.column(i).path_in_schema: i for i in range(first.num_columns)
        }
        needed = ("bbox.xmin", "bbox.xmax", "bbox.ymin", "bbox.ymax")
        if not all(name in columns for name in needed):
            return None
        for index in range(metadata.num_row_groups):
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
                return index
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument(
        "--bbox", nargs=4, type=float, required=True,
        metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--groups-per-task", type=int, default=2)
    args = parser.parse_args(argv)

    import pyarrow.fs as pafs

    filesystem = pafs.S3FileSystem(anonymous=True, region=INV.REGION)
    listed = sorted(INV.list_source_objects(args.release), key=lambda i: i["uri"])

    for object_index, source in enumerate(listed):
        found = covering_row_group(filesystem, source["uri"], tuple(args.bbox))
        if found is None:
            continue
        details = INV.inspect_parquet_object(source, filesystem)
        largest = max(group["rows"] for group in details["row_groups"])
        inventory = INV.build_inventory(
            args.release, [source], lambda _s: details,
            # target_rows must exceed the largest row group or planning rejects
            # it outright; groups-per-task is what actually bounds a task.
            target_rows=largest * args.groups_per_task + 1,
            max_selected_uncompressed_bytes=1_000_000_000,
            max_groups=args.groups_per_task,
            max_tasks=MAX_TASKS,
        )
        INV.validate_inventory(inventory)
        tasks = inventory["map_plan"]["tasks"]
        task_index = next(
            i for i, task in enumerate(tasks)
            if task["ranges"][0]["first_row_group"]
            <= found
            <= task["ranges"][0]["last_row_group"]
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
        print(json.dumps({
            "object_index": object_index,
            "row_group": found,
            "task_index": task_index,
            "task_records": tasks[task_index]["expected_input_records"],
            "tasks": len(tasks),
            "inventory_sha256": inventory["inventory_sha256"],
        }, sort_keys=True))
        return 0

    raise SystemExit(f"no row group overlaps bbox {args.bbox}")


if __name__ == "__main__":
    raise SystemExit(main())
