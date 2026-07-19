from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "address_partition.py"
SPEC = importlib.util.spec_from_file_location("address_partition", SCRIPT)
assert SPEC and SPEC.loader
address = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = address
SPEC.loader.exec_module(address)


def test_hash_matches_worker_golden_vectors():
    first = ["us", "ma", "middlesex", "stoneham", "02180", "main street", "10", ""]
    # The first vector pins the fixture used by both the producer and Worker.
    assert address.address_key_hash(first) == 0x0CE4F78442CA30B4
    second = ["fr", "", "", "", "", "rue de la paix", "1", ""]
    assert address.address_key_hash(second) == 0x20FD8A6797BE2B2B


def test_hash_prefix_ranges_are_complete_and_non_overlapping():
    ranges = [address.prefix_range(f"{value:03b}") for value in range(8)]
    assert ranges[0][0] == 0
    assert ranges[-1][1] == 0xFFFFFFFFFFFFFFFF
    for left, right in zip(ranges, ranges[1:]):
        assert left[1] + 1 == right[0]


def test_hot_country_splits_without_moving_cold_country():
    counts = [
        ("ca", 0, 10),
        ("us", 0, 60),
        ("us", 1, 40),
        ("us", 2, 30),
        ("us", 3, 20),
    ]
    partitions, splits = address.plan_partitions(
        counts, maximum_hash_bits=2, row_cap=75
    )
    assert [item.id for item in partitions if item.country == "ca"] == ["a-ca"]
    assert [item.id for item in partitions if item.country == "us"] == [
        "a-us-h-00",
        "a-us-h-01",
        "a-us-h-1",
    ]
    assert splits == ["us:", "us:0"]
    assert sum(item.rows for item in partitions) == 160


def test_sticky_splits_retain_empty_children():
    first, splits = address.plan_partitions(
        [("us", 0, 60), ("us", 1, 40)],
        maximum_hash_bits=2,
        row_cap=75,
    )
    assert splits == ["us:", "us:0"]
    assert [(item.hash_prefix, item.rows) for item in first] == [
        ("00", 60),
        ("01", 40),
        ("1", 0),
    ]

    second, retained = address.plan_partitions(
        [("us", 0, 5)],
        maximum_hash_bits=2,
        row_cap=75,
        sticky_split_ids=splits,
    )
    assert [(item.hash_prefix, item.rows) for item in second] == [
        ("00", 5),
        ("01", 0),
        ("1", 0),
    ]
    assert retained == splits


def test_split_history_rejects_duplicates_or_missing_ancestors():
    with pytest.raises(ValueError, match="unique"):
        address.validate_split_ids(["us:", "us:"], maximum_hash_bits=4)
    with pytest.raises(ValueError, match="ancestor"):
        address.validate_split_ids(["us:0"], maximum_hash_bits=4)


def test_plan_cli_contract_carries_release_and_versions(tmp_path):
    counts = {
        "schema": address.COUNT_SCHEMA,
        "overture_release": "2026-06-17.0",
        "counts": [
            {"country": "ca", "bucket": 0, "rows": 10},
            {"country": "us", "bucket": 0, "rows": 60},
            {"country": "us", "bucket": 1, "rows": 40},
        ],
    }
    plan = address.build_plan(
        counts, maximum_hash_bits=2, row_cap=75
    )
    assert plan["schema"] == address.PLAN_SCHEMA
    assert plan["overture_release"] == "2026-06-17.0"
    assert plan["normalization_version"] == address.NORMALIZATION_VERSION
    assert plan["totals"]["retained_rows"] == 110
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    rebuilt = address.build_plan(
        counts,
        maximum_hash_bits=2,
        row_cap=1_000,
        previous=json.loads(path.read_text()),
    )
    assert rebuilt["partition"]["split_ids"] == plan["partition"]["split_ids"]


def test_previous_plan_allows_maximum_hash_bits_growth():
    counts = {
        "schema": address.COUNT_SCHEMA,
        "overture_release": "2026-06-17.0",
        "counts": [
            {"country": "us", "bucket": 0, "rows": 60},
            {"country": "us", "bucket": 1, "rows": 40},
        ],
    }
    previous = address.build_plan(
        counts, maximum_hash_bits=2, row_cap=75
    )
    current = address.build_plan(
        {
            **counts,
            "overture_release": "2026-07-15.0",
            "counts": [
                {"country": "us", "bucket": 0, "rows": 60},
                {"country": "us", "bucket": 1, "rows": 40},
            ],
        },
        maximum_hash_bits=3,
        row_cap=1_000,
        previous=previous,
    )
    assert current["partition"]["maximum_hash_bits"] == 3
    assert current["partition"]["split_ids"] == previous["partition"]["split_ids"]


def test_previous_plan_rejects_maximum_hash_bits_decrease():
    previous = address.build_plan(
        {
            "schema": address.COUNT_SCHEMA,
            "overture_release": "2026-06-17.0",
            "counts": [{"country": "us", "bucket": 0, "rows": 1}],
        },
        maximum_hash_bits=3,
        row_cap=100,
    )
    with pytest.raises(ValueError, match="limits are incompatible"):
        address.build_plan(
            {
                "schema": address.COUNT_SCHEMA,
                "overture_release": "2026-07-15.0",
                "counts": [{"country": "us", "bucket": 0, "rows": 1}],
            },
            maximum_hash_bits=2,
            row_cap=100,
            previous=previous,
        )


def test_previous_plan_must_prove_leaf_ancestry_and_coverage():
    counts = {
        "schema": address.COUNT_SCHEMA,
        "overture_release": "2026-06-17.0",
        "counts": [
            {"country": "us", "bucket": 0, "rows": 60},
            {"country": "us", "bucket": 1, "rows": 40},
        ],
    }
    previous = address.build_plan(
        counts, maximum_hash_bits=2, row_cap=75
    )
    previous["partitions"][0]["hash_prefix"] = "000"
    with pytest.raises(ValueError, match="inconsistent"):
        address.build_plan(
            counts,
            maximum_hash_bits=2,
            row_cap=1_000,
            previous=previous,
        )


def test_plan_rejects_unused_split_history():
    plan = address.build_plan(
        {
            "schema": address.COUNT_SCHEMA,
            "overture_release": "2026-06-17.0",
            "counts": [{"country": "us", "bucket": 0, "rows": 1}],
        },
        maximum_hash_bits=2,
        row_cap=100,
    )
    plan["partition"]["split_ids"] = ["ca:"]
    with pytest.raises(ValueError, match="differs from the leaf tree"):
        address.validate_plan(plan)


def test_previous_countries_remain_explicit_when_current_rows_are_zero():
    previous = address.build_plan(
        {
            "schema": address.COUNT_SCHEMA,
            "overture_release": "2026-06-17.0",
            "counts": [
                {"country": "ca", "bucket": 0, "rows": 1},
                {"country": "us", "bucket": 0, "rows": 60},
                {"country": "us", "bucket": 1, "rows": 40},
            ],
        },
        maximum_hash_bits=2,
        row_cap=75,
    )
    current = address.build_plan(
        {
            "schema": address.COUNT_SCHEMA,
            "overture_release": "2026-07-15.0",
            "counts": [{"country": "mx", "bucket": 0, "rows": 1}],
        },
        maximum_hash_bits=2,
        row_cap=75,
        previous=previous,
    )
    assert [(item["id"], item["rows"]) for item in current["partitions"]] == [
        ("a-ca", 0),
        ("a-mx", 1),
        ("a-us-h-00", 0),
        ("a-us-h-01", 0),
        ("a-us-h-1", 0),
    ]
    assert current["partition"]["split_ids"] == ["us:", "us:0"]
    address.validate_plan(current)
