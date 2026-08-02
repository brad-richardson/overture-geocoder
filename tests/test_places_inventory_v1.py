import copy
import json
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from places_inventory_v1 import (  # noqa: E402
    REQUIRED_FIELD_TYPES,
    TAXONOMY_REQUIRED_FIELD_TYPES,
    TAXONOMY_SCHEMA_CONTRACT_VERSION,
    approved_prefix,
    build_inventory,
    canonical_schema_contract,
    list_source_objects,
    projected_column_roots,
    schema_profile_name,
    validate_inventory,
)


RELEASE = "2026-06-18.0"


def contract(*, nullable_override=None):
    nullable_override = nullable_override or {}
    return canonical_schema_contract(
        [
            {
                "path": path,
                "type": field_type,
                "nullable": nullable_override.get(path, True),
            }
            for path, field_type in REQUIRED_FIELD_TYPES.items()
        ]
    )


def taxonomy_contract():
    return canonical_schema_contract(
        [
            {"path": path, "type": field_type, "nullable": True}
            for path, field_type in TAXONOMY_REQUIRED_FIELD_TYPES.items()
        ]
    )


def test_taxonomy_contract_is_a_new_generation_not_a_legacy_rewrite():
    legacy = contract()
    taxonomy = taxonomy_contract()
    assert legacy["version"] != taxonomy["version"]
    assert taxonomy["version"] == TAXONOMY_SCHEMA_CONTRACT_VERSION
    assert schema_profile_name(legacy) == "legacy"
    assert schema_profile_name(taxonomy) == "taxonomy"
    assert "categories" in projected_column_roots(legacy)
    assert "taxonomy" not in projected_column_roots(legacy)
    assert "taxonomy" in projected_column_roots(taxonomy)
    assert "categories" not in projected_column_roots(taxonomy)


def footer(rows, *, schema=None):
    groups = [
        {
            "index": index,
            "rows": count,
            "selected_compressed_bytes": count * 10,
            "selected_uncompressed_bytes": count * 20,
        }
        for index, count in enumerate(rows)
    ]
    return {
        "records": sum(rows),
        "row_group_count": len(groups),
        "row_groups": groups,
        "schema_contract": schema or contract(),
    }


def test_inventory_pins_objects_schema_rowgroups_and_bounded_plan():
    prefix = approved_prefix(RELEASE)
    listed = [
        {"uri": prefix + "b.parquet", "etag": "etag-b", "bytes": 200},
        {"uri": prefix + "a.parquet", "etag": "etag-a", "bytes": 100},
    ]
    details = {
        prefix + "a.parquet": footer([3, 2]),
        prefix + "b.parquet": footer([4]),
    }
    inventory = build_inventory(
        RELEASE,
        listed,
        lambda source: details[source["uri"]],
        target_rows=5,
        max_selected_uncompressed_bytes=1_000,
        max_groups=2,
        max_tasks=4,
    )

    assert validate_inventory(inventory) is inventory
    assert [item["uri"] for item in inventory["objects"]] == sorted(details)
    assert inventory["totals"] == {
        "objects": 2,
        "bytes": 300,
        "records": 9,
        "row_groups": 3,
        "selected_compressed_bytes": 90,
        "selected_uncompressed_bytes": 180,
    }
    tasks = inventory["map_plan"]["tasks"]
    assert [item["expected_input_records"] for item in tasks] == [5, 4]
    assert tasks[0]["ranges"] == [
        {
            "object_index": 0,
            "uri": prefix + "a.parquet",
            "etag": "etag-a",
            "first_row_group": 0,
            "last_row_group": 1,
            "row_groups": 2,
            "rows": 5,
            "selected_compressed_bytes": 50,
            "selected_uncompressed_bytes": 100,
        }
    ]
    # Serialization is part of the canonical digest contract.
    round_trip = json.loads(json.dumps(inventory, sort_keys=True))
    assert validate_inventory(round_trip) == inventory


def test_inventory_rejects_per_object_required_schema_drift():
    prefix = approved_prefix(RELEASE)
    listed = [
        {"uri": prefix + "a.parquet", "etag": "a", "bytes": 10},
        {"uri": prefix + "b.parquet", "etag": "b", "bytes": 20},
    ]
    details = {
        prefix + "a.parquet": footer([1]),
        prefix + "b.parquet": footer(
            [1], schema=contract(nullable_override={"names.primary": False})
        ),
    }
    with pytest.raises(ValueError, match="schema drifted"):
        build_inventory(RELEASE, listed, lambda source: details[source["uri"]])


def test_inventory_rejects_prefix_escape_and_tampering():
    prefix = approved_prefix(RELEASE)
    with pytest.raises(ValueError, match="invalid object identity"):
        build_inventory(
            RELEASE,
            [{"uri": prefix + "../address.parquet", "etag": "a", "bytes": 10}],
            lambda _: footer([1]),
        )

    inventory = build_inventory(
        RELEASE,
        [{"uri": prefix + "a.parquet", "etag": "a", "bytes": 10}],
        lambda _: footer([1]),
    )
    tampered = copy.deepcopy(inventory)
    tampered["objects"][0]["etag"] = "changed"
    with pytest.raises(ValueError, match="digest differs"):
        validate_inventory(tampered)


def test_s3_listing_is_paginated_injected_and_exactly_filtered():
    key_prefix = f"release/{RELEASE}/theme=places/type=place/"
    pages = [
        f"""<?xml version="1.0" encoding="UTF-8"?>
        <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
          <IsTruncated>true</IsTruncated>
          <Contents><Key>{key_prefix}b.parquet</Key><ETag>"b"</ETag><Size>20</Size></Contents>
          <Contents><Key>{key_prefix}_metadata</Key><ETag>"m"</ETag><Size>5</Size></Contents>
          <NextContinuationToken>next token</NextContinuationToken>
        </ListBucketResult>""".encode(),
        f"""<?xml version="1.0" encoding="UTF-8"?>
        <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
          <IsTruncated>false</IsTruncated>
          <Contents><Key>{key_prefix}a.parquet</Key><ETag>"a"</ETag><Size>10</Size></Contents>
        </ListBucketResult>""".encode(),
    ]
    urls = []

    def fetch(url):
        urls.append(url)
        return pages[len(urls) - 1]

    objects = list_source_objects(RELEASE, fetch=fetch)
    assert [item["uri"] for item in objects] == [
        approved_prefix(RELEASE) + "a.parquet",
        approved_prefix(RELEASE) + "b.parquet",
    ]
    assert "continuation-token=next+token" in urls[1]
