from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "inventory_address_rowgroups.py"
SPEC = importlib.util.spec_from_file_location("inventory_address_rowgroups", SCRIPT)
assert SPEC and SPEC.loader
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


def test_parse_listing_page_supports_pagination_and_identity():
    payload = b"""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <IsTruncated>true</IsTruncated><NextContinuationToken>next token</NextContinuationToken>
      <Contents><Key>release/x/a.parquet</Key><ETag>\"abc-2\"</ETag><Size>123</Size></Contents>
      <Contents><Key>release/x/ignored.txt</Key><ETag>\"def\"</ETag><Size>4</Size></Contents>
    </ListBucketResult>"""
    objects, token = inventory.parse_listing_page(payload)
    assert token == "next token"
    assert objects == [
        {
            "key": "release/x/a.parquet",
            "uri": "s3://overturemaps-us-west-2/release/x/a.parquet",
            "etag": "abc-2",
            "bytes": 123,
        }
    ]


def test_parse_listing_rejects_truncated_page_without_token():
    payload = b"""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <IsTruncated>true</IsTruncated>
    </ListBucketResult>"""
    with pytest.raises(ValueError, match="continuation token"):
        inventory.parse_listing_page(payload)


def source(uri: str, rows: list[int], byte_values: list[int]) -> dict:
    return {
        "uri": uri,
        "etag": "etag",
        "records": sum(rows),
        "groups": [
            {
                "index": index,
                "rows": row_count,
                "selected_compressed_bytes": byte_count // 2,
                "selected_uncompressed_bytes": byte_count,
            }
            for index, (row_count, byte_count) in enumerate(zip(rows, byte_values))
        ],
    }


def spatial_source(uri: str, groups: list[dict]) -> dict:
    """Build a source whose row groups carry bbox extent statistics.

    Each entry in ``groups`` is ``{"rows", "bytes", "extent"}`` where ``extent``
    is ``(xmin_min, xmax_max, ymin_min, ymax_max)`` or ``None`` to model a row
    group with missing bbox statistics.
    """
    built = []
    for index, group in enumerate(groups):
        extent = group.get("extent")
        xmin_min, xmax_max, ymin_min, ymax_max = extent or (None, None, None, None)
        built.append(
            {
                "index": index,
                "rows": group["rows"],
                "selected_compressed_bytes": group["bytes"] // 2,
                "selected_uncompressed_bytes": group["bytes"],
                "bbox_xmin_min": xmin_min,
                "bbox_xmax_max": xmax_max,
                "bbox_ymin_min": ymin_min,
                "bbox_ymax_max": ymax_max,
                "bbox_stats_complete": extent is not None,
            }
        )
    return {
        "uri": uri,
        "etag": "etag",
        "records": sum(group["rows"] for group in groups),
        "groups": built,
    }


# A US-Northeast-shaped query box: (xmin, ymin, xmax, ymax).
NE_BOX = (-80.5, 38.0, -66.9, 47.5)


def test_group_bbox_intersects_inside_outside_straddle_and_missing():
    # Fully inside the box.
    assert inventory.group_bbox_intersects(
        {
            "bbox_xmin_min": -74.0,
            "bbox_xmax_max": -73.9,
            "bbox_ymin_min": 40.7,
            "bbox_ymax_max": 40.8,
        },
        NE_BOX,
    )
    # Entirely west of the box.
    assert not inventory.group_bbox_intersects(
        {
            "bbox_xmin_min": -125.0,
            "bbox_xmax_max": -122.0,
            "bbox_ymin_min": 37.0,
            "bbox_ymax_max": 38.5,
        },
        NE_BOX,
    )
    # Entirely south of the box (longitude overlaps, latitude does not).
    assert not inventory.group_bbox_intersects(
        {
            "bbox_xmin_min": -75.0,
            "bbox_xmax_max": -74.0,
            "bbox_ymin_min": 25.0,
            "bbox_ymax_max": 30.0,
        },
        NE_BOX,
    )
    # Straddling the western edge still intersects.
    assert inventory.group_bbox_intersects(
        {
            "bbox_xmin_min": -85.0,
            "bbox_xmax_max": -79.0,
            "bbox_ymin_min": 39.0,
            "bbox_ymax_max": 41.0,
        },
        NE_BOX,
    )
    # Missing statistics cannot be pruned: conservatively intersecting.
    assert inventory.group_bbox_intersects(
        {
            "bbox_xmin_min": None,
            "bbox_xmax_max": None,
            "bbox_ymin_min": None,
            "bbox_ymax_max": None,
        },
        NE_BOX,
    )
    # A group with no bbox keys at all is also conservatively kept.
    assert inventory.group_bbox_intersects({}, NE_BOX)


def test_bbox_plan_prunes_to_intersecting_groups_and_records_scope():
    objects = [
        spatial_source(
            "s3://bucket/a",
            [
                {"rows": 10, "bytes": 10, "extent": (-74.0, -73.0, 40.0, 41.0)},  # in
                {
                    "rows": 20,
                    "bytes": 10,
                    "extent": (-125.0, -122.0, 37.0, 38.5),
                },  # out
                {"rows": 30, "bytes": 10, "extent": (-70.0, -68.0, 42.0, 43.0)},  # in
                {"rows": 40, "bytes": 10, "extent": (10.0, 12.0, 45.0, 46.0)},  # out
                {"rows": 50, "bytes": 10, "extent": None},  # missing -> kept
            ],
        )
    ]
    plan = inventory.plan_contiguous_ranges(
        objects,
        target_rows=1000,
        max_selected_uncompressed_bytes=1000,
        max_groups=72,
        max_tasks=128,
        bbox=NE_BOX,
    )
    assert plan["bbox_scope"] == "row_group_approximate"
    assert plan["bbox"] == {"xmin": -80.5, "ymin": 38.0, "xmax": -66.9, "ymax": 47.5}
    assert plan["bbox_row_groups"] == {
        "total": 5,
        "selected": 3,
        "pruned": 2,
        "no_stats_conservative": 1,
    }
    # Pruned groups break contiguity: 0, 2, and 4 land in three separate ranges.
    ranges = [
        (item["first_row_group"], item["last_row_group"])
        for task in plan["tasks"]
        for item in task["ranges"]
    ]
    assert ranges == [(0, 0), (2, 2), (4, 4)]
    assert plan["bbox_scoped_rows"] == 10 + 30 + 50
    assert sum(task["rows"] for task in plan["tasks"]) == 90


def test_bbox_plan_is_deterministic():
    objects = [
        spatial_source(
            "s3://bucket/a",
            [
                {"rows": 10, "bytes": 10, "extent": (-74.0, -73.0, 40.0, 41.0)},
                {"rows": 20, "bytes": 10, "extent": (-125.0, -122.0, 37.0, 38.5)},
                {"rows": 30, "bytes": 10, "extent": None},
            ],
        ),
        spatial_source(
            "s3://bucket/b",
            [{"rows": 40, "bytes": 10, "extent": (-70.0, -68.0, 42.0, 43.0)}],
        ),
    ]
    first = inventory.plan_contiguous_ranges(
        objects,
        target_rows=1000,
        max_selected_uncompressed_bytes=1000,
        max_groups=72,
        max_tasks=128,
        bbox=NE_BOX,
    )
    second = inventory.plan_contiguous_ranges(
        objects,
        target_rows=1000,
        max_selected_uncompressed_bytes=1000,
        max_groups=72,
        max_tasks=128,
        bbox=NE_BOX,
    )
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_default_plan_is_byte_identical_when_bbox_stats_present():
    plain = [source("s3://bucket/a", [40, 40, 40], [20, 20, 20])]
    spatial = [
        spatial_source(
            "s3://bucket/a",
            [
                {"rows": 40, "bytes": 20, "extent": (-74.0, -73.0, 40.0, 41.0)},
                {"rows": 40, "bytes": 20, "extent": (10.0, 12.0, 45.0, 46.0)},
                {"rows": 40, "bytes": 20, "extent": None},
            ],
        )
    ]
    gates = dict(
        target_rows=1000,
        max_selected_uncompressed_bytes=1000,
        max_groups=72,
        max_tasks=128,
    )
    plain_plan = inventory.plan_contiguous_ranges(plain, **gates)
    spatial_plan = inventory.plan_contiguous_ranges(spatial, **gates)
    # Presence of bbox statistics must not perturb the default (no-bbox) plan.
    assert json.dumps(plain_plan, sort_keys=True) == json.dumps(
        spatial_plan, sort_keys=True
    )
    assert "bbox" not in plain_plan
    assert "bbox_scope" not in plain_plan
    assert "bbox_scoped_rows" not in plain_plan


def test_inventory_object_records_bbox_extent_and_missing_stats(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pafs = pytest.importorskip("pyarrow.fs")
    import pyarrow.parquet as pq

    filesystem = pafs.LocalFileSystem()

    # A parquet with a bbox struct column and complete statistics.
    with_bbox = tmp_path / "with_bbox.parquet"
    bbox_array = pa.StructArray.from_arrays(
        [
            pa.array([-74.0, -70.5], type=pa.float64()),
            pa.array([-73.5, -70.0], type=pa.float64()),
            pa.array([40.0, 42.5], type=pa.float64()),
            pa.array([40.5, 43.0], type=pa.float64()),
        ],
        names=["xmin", "xmax", "ymin", "ymax"],
    )
    table = pa.table(
        {
            "id": pa.array(["a", "b"]),
            "country": pa.array(["US", "US"]),
            "bbox": bbox_array,
        }
    )
    pq.write_table(table, with_bbox)
    record = inventory.inventory_object(
        {"uri": f"s3://{with_bbox}", "etag": "e", "bytes": with_bbox.stat().st_size},
        filesystem,
    )
    group = record["groups"][0]
    assert group["bbox_xmin_min"] == pytest.approx(-74.0)
    assert group["bbox_xmax_max"] == pytest.approx(-70.0)
    assert group["bbox_ymin_min"] == pytest.approx(40.0)
    assert group["bbox_ymax_max"] == pytest.approx(43.0)
    assert group["bbox_stats_complete"] is True
    assert inventory.group_bbox_intersects(group, NE_BOX)

    # A parquet with no bbox column: statistics are absent, so the group is
    # recorded as null and treated as conservatively intersecting.
    without_bbox = tmp_path / "without_bbox.parquet"
    pq.write_table(
        pa.table({"id": pa.array(["a"]), "country": pa.array(["US"])}), without_bbox
    )
    record = inventory.inventory_object(
        {
            "uri": f"s3://{without_bbox}",
            "etag": "e",
            "bytes": without_bbox.stat().st_size,
        },
        filesystem,
    )
    group = record["groups"][0]
    assert group["bbox_xmin_min"] is None
    assert group["bbox_xmax_max"] is None
    assert group["bbox_ymin_min"] is None
    assert group["bbox_ymax_max"] is None
    assert group["bbox_stats_complete"] is False
    assert inventory.group_bbox_intersects(group, NE_BOX)


def test_contiguous_plan_splits_on_rows_bytes_and_group_count():
    objects = [source("s3://bucket/a", [40, 40, 40, 40], [20, 20, 81, 20])]
    plan = inventory.plan_contiguous_ranges(
        objects,
        target_rows=100,
        max_selected_uncompressed_bytes=100,
        max_groups=2,
        max_tasks=4,
    )
    assert [
        (
            task["ranges"][0]["first_row_group"],
            task["ranges"][0]["last_row_group"],
        )
        for task in plan["tasks"]
    ] == [
        (0, 1),
        (2, 2),
        (3, 3),
    ]
    assert plan["safe_at_configured_task_count"] is True
    assert sum(task["rows"] for task in plan["tasks"]) == 160


def test_plan_reports_task_count_failure_without_relaxing_gates():
    objects = [source("s3://bucket/a", [10, 10, 10], [1, 1, 1])]
    plan = inventory.plan_contiguous_ranges(
        objects,
        target_rows=10,
        max_selected_uncompressed_bytes=10,
        max_groups=1,
        max_tasks=2,
    )
    assert plan["task_count"] == 3
    assert plan["safe_at_configured_task_count"] is False


def test_plan_combines_object_tails_as_separate_contiguous_ranges():
    objects = [
        source("s3://bucket/a", [40, 40], [20, 20]),
        source("s3://bucket/b", [10, 10], [5, 5]),
    ]
    plan = inventory.plan_contiguous_ranges(
        objects,
        target_rows=100,
        max_selected_uncompressed_bytes=100,
        max_groups=4,
        max_tasks=1,
    )
    assert plan["task_count"] == 1
    assert plan["tasks"][0]["source_objects"] == 2
    assert [item["uri"] for item in plan["tasks"][0]["ranges"]] == [
        "s3://bucket/a",
        "s3://bucket/b",
    ]


def test_plan_rejects_one_row_group_over_byte_cap():
    objects = [source("s3://bucket/a", [10], [101])]
    with pytest.raises(ValueError, match="exceeds the task byte cap"):
        inventory.plan_contiguous_ranges(
            objects,
            target_rows=100,
            max_selected_uncompressed_bytes=100,
            max_groups=2,
            max_tasks=2,
        )


def test_plan_rejects_one_row_group_over_row_cap():
    objects = [source("s3://bucket/a", [101], [10])]
    with pytest.raises(ValueError, match="exceeds the task row cap"):
        inventory.plan_contiguous_ranges(
            objects,
            target_rows=100,
            max_selected_uncompressed_bytes=100,
            max_groups=2,
            max_tasks=2,
        )
