from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "experiment_hosted_rowgroups.py"
SPEC = importlib.util.spec_from_file_location("experiment_hosted_rowgroups", SCRIPT)
assert SPEC and SPEC.loader
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


def test_list_url_is_scoped_and_encoded():
    url = experiment.list_url(
        "release/2026-06-17.0/theme=addresses/type=address/", 10
    )
    assert url.startswith(
        "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/?"
    )
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
