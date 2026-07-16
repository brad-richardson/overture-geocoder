from __future__ import annotations

import importlib.util
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
