from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "experiment_hosted_rowgroups.py"
SPEC = importlib.util.spec_from_file_location("experiment_hosted_rowgroups", SCRIPT)
assert SPEC and SPEC.loader
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


def test_list_url_is_scoped_and_encoded():
    url = experiment.list_url("release/2026-06-17.0/theme=addresses/type=address/", 10)
    assert url.startswith("https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/?")
    assert "prefix=release%2F2026-06-17.0%2Ftheme%3Daddresses" in url
    assert "max-keys=10" in url


def test_parse_listing_extracts_identity_and_size():
    payload = b"""<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <Contents><Key>release/x/part.parquet</Key><ETag>"abc-2"</ETag><Size>123</Size></Contents>
    </ListBucketResult>"""
    assert experiment.parse_listing(payload) == [
        {"key": "release/x/part.parquet", "etag": "abc-2", "bytes": 123}
    ]


def test_select_row_groups_stays_within_all_budgets():
    groups = [
        {"index": 0, "rows": 40, "rowgroup_uncompressed_bytes": 50},
        {"index": 1, "rows": 40, "rowgroup_uncompressed_bytes": 50},
        {"index": 2, "rows": 40, "rowgroup_uncompressed_bytes": 50},
    ]
    assert experiment.select_row_groups(
        groups, target_rowgroup_uncompressed_bytes=120, max_rows=100, max_groups=3
    ) == [0, 1]
    assert experiment.select_row_groups(
        groups, target_rowgroup_uncompressed_bytes=200, max_rows=200, max_groups=1
    ) == [0]


def test_select_row_groups_rejects_oversized_first_group():
    with pytest.raises(ValueError, match="first row group exceeds"):
        experiment.select_row_groups(
            [{"index": 0, "rows": 101, "rowgroup_uncompressed_bytes": 50}],
            target_rowgroup_uncompressed_bytes=100,
            max_rows=100,
            max_groups=2,
        )


def test_parse_listing_rejects_incomplete_object():
    payload = b"""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <Contents><Key>missing-size</Key><ETag>"abc"</ETag></Contents>
    </ListBucketResult>"""
    with pytest.raises(ValueError, match="missing"):
        experiment.parse_listing(payload)


def test_parse_network_received_excludes_loopback():
    payload = """Inter-| Receive | Transmit
 face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop
    lo: 100 0 0 0 0 0 0 0 100 0 0 0 0 0 0 0
  eth0: 1234 0 0 0 0 0 0 0 50 0 0 0 0 0 0 0
  eth1: 66 0 0 0 0 0 0 0 25 0 0 0 0 0 0 0
"""
    assert experiment.parse_network_received(payload) == 1300


def test_bounded_writer_refuses_to_cross_limit(tmp_path):
    path = tmp_path / "bounded.bin"
    writer = experiment.BoundedWriter(path, 4)
    assert writer.write(b"1234") == 4
    with pytest.raises(ValueError, match="hard limit"):
        writer.write(b"5")
    writer.close()
    assert path.read_bytes() == b"1234"


def test_schema_fingerprint_validates_new_inventory_contract():
    payload = {
        "version": "overture-address-required-schema-v1",
        "fields": [{"path": "id", "type": "string", "nullable": True}],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    report = {"schema_contract": {**payload, "fingerprint_sha256": digest}}

    assert experiment.schema_fingerprint(report) == digest
    report["schema_contract"]["fingerprint_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="differs"):
        experiment.schema_fingerprint(report)


def test_schema_fingerprint_allows_historical_measurement_report():
    assert experiment.schema_fingerprint({"schema": "historical"}) is None


def canonical_inventory():
    contract = experiment.address_inventory.canonical_schema_contract(
        [
            {"path": path, "type": field_type, "nullable": True}
            for path, field_type in experiment.address_inventory.REQUIRED_FIELD_TYPES.items()
        ]
    )
    source = {
        "uri": (
            "s3://overturemaps-us-west-2/release/2026-06-17.0/"
            "theme=addresses/type=address/a.parquet"
        ),
        "etag": "etag",
        "bytes": 100,
        "records": 10,
        "row_groups": 1,
        "selected_compressed_bytes": 50,
        "selected_uncompressed_bytes": 80,
        "schema_contract": contract,
        "groups": [
            {
                "index": 0,
                "rows": 10,
                "all_compressed_bytes": 60,
                "all_uncompressed_bytes": 100,
                "selected_compressed_bytes": 50,
                "selected_uncompressed_bytes": 80,
                "country_min": "US",
                "country_max": "US",
                "exact_country": "US",
                "bbox_xmin_min": None,
                "bbox_xmax_max": None,
                "bbox_ymin_min": None,
                "bbox_ymax_max": None,
                "bbox_stats_complete": False,
            }
        ],
    }
    plan = experiment.address_inventory.plan_contiguous_ranges(
        [source],
        target_rows=100,
        max_selected_uncompressed_bytes=1_000,
        max_groups=10,
        max_tasks=10,
    )
    return experiment.address_inventory.build_report("2026-06-17.0", [source], plan)


def test_canonical_inventory_task_binds_exact_index_and_digests():
    report = canonical_inventory()

    identity, task = experiment.canonical_inventory_task(
        report, release="2026-06-17.0", task_index=0
    )

    assert identity["inventory_sha256"] == report["inventory_sha256"]
    assert task["index"] == 0
    assert task["execution_bucket"] == "address-map-task-000"
    metadata = experiment.exact_task_metadata(identity, task)
    assert (
        metadata[experiment.address_inventory.INVENTORY_METADATA_KEY]
        == report["inventory_sha256"].encode()
    )
    assert (
        metadata[experiment.address_inventory.TASK_DIGEST_METADATA_KEY]
        == task["task_digest_sha256"].encode()
    )
    forged = json.loads(json.dumps(report))
    forged["plan"]["tasks"][0]["ranges"][0]["rows"] = 9
    with pytest.raises(ValueError, match="task digest differs"):
        experiment.canonical_inventory_task(
            forged, release="2026-06-17.0", task_index=0
        )
