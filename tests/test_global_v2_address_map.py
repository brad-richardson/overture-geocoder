from __future__ import annotations

import hashlib
import importlib.util
import json
import struct
import uuid
from collections import defaultdict
from pathlib import Path

import pytest


pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

SCRIPT = Path(__file__).parents[1] / "scripts" / "global_v2_address_map.py"
SPEC = importlib.util.spec_from_file_location("global_v2_address_map", SCRIPT)
assert SPEC and SPEC.loader
address_map = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(address_map)


RELEASE = "2026-06-17.0"
SCHEMA_FINGERPRINT = "b" * 64
INVENTORY_DIGEST = "c" * 64
TASK_DIGEST = "d" * 64
TASK_SOURCE_DIGEST = "e" * 64
TASK_INDEX = 7
EXECUTION_BUCKET = "address-map-task-007"


def point(lon: float = -71.0, lat: float = 42.0) -> bytes:
    return b"\x01" + struct.pack("<Idd", 1, lon, lat)


def row(feature_id: str | None, **changes):
    value = {
        "id": feature_id,
        "street": "Main Street",
        "number": "10",
        "unit": "",
        "postcode": "02180",
        "postal_city": "Stoneham",
        "address_levels": [{"value": "MA"}, {"value": "Stoneham"}],
        "country": "US",
        "geometry": point(),
        "source_object_index": 0,
        "source_row_group": 2,
        "source_row_index": 0,
    }
    value.update(changes)
    return value


def write_projected(path: Path, rows: list[dict], *, complete_source: bool = True):
    inventory_object = {
        "uri": (
            f"s3://overturemaps-us-west-2/release/{RELEASE}/"
            "theme=addresses/type=address/part-00000.parquet"
        ),
        "etag": "source-etag",
        "bytes": 123_456,
        "records": len(rows),
        "row_groups": 3,
        "sha256": "a" * 64,
    }
    if not complete_source:
        del inventory_object["records"]
    inventory = {
        "schema": address_map.SOURCE_INVENTORY_SCHEMA,
        "release": RELEASE,
        "family": "addresses",
        "theme": "addresses",
        "type": "address",
        "schema_version": "2026-06-18",
        "discovery": {"kind": "test-fixture", "source": "test"},
        "objects": [inventory_object],
    }
    inventory_json = json.dumps(
        inventory, sort_keys=True, separators=(",", ":")
    ).encode()
    table = pa.Table.from_pylist(rows).replace_schema_metadata(
        {
            b"overture.source_inventory_sha256": hashlib.sha256(inventory_json)
            .hexdigest()
            .encode(),
            b"overture.source_inventory_json": inventory_json,
            b"overture.release": RELEASE.encode(),
            b"overture.family": b"addresses",
            address_map.SCHEMA_FINGERPRINT_METADATA_KEY: SCHEMA_FINGERPRINT.encode(),
            address_map.address_inventory.INVENTORY_METADATA_KEY: (
                INVENTORY_DIGEST.encode()
            ),
            address_map.address_inventory.TASK_INDEX_METADATA_KEY: str(
                TASK_INDEX
            ).encode(),
            address_map.address_inventory.TASK_DIGEST_METADATA_KEY: TASK_DIGEST.encode(),
            address_map.address_inventory.TASK_SOURCE_DIGEST_METADATA_KEY: (
                TASK_SOURCE_DIGEST.encode()
            ),
            address_map.address_inventory.EXECUTION_BUCKET_METADATA_KEY: (
                EXECUTION_BUCKET.encode()
            ),
        }
    )
    pq.write_table(table, path, row_group_size=3)
    return inventory


def build(path: Path, output_dir: Path, completion: Path):
    return address_map.build_map(
        path,
        output_dir,
        completion,
        execution_bucket=EXECUTION_BUCKET,
        expected_release=RELEASE,
        expected_schema_fingerprint_sha256=SCHEMA_FINGERPRINT,
        expected_inventory_sha256=INVENTORY_DIGEST,
        expected_task_index=TASK_INDEX,
        expected_task_digest_sha256=TASK_DIGEST,
        expected_task_source_digest_sha256=TASK_SOURCE_DIGEST,
        maximum_hash_bits=4,
        scan_batch_rows=2,
        max_fragment_rows=1,
        max_fragment_bytes=20_000,
        max_rows=100,
    )


def test_strict_map_reconciles_exclusive_rejections_counts_and_fanout(tmp_path):
    retained_ids = [str(uuid.UUID(int=1)), str(uuid.UUID(int=2))]
    duplicate_key = (
        "us",
        "ma",
        "stoneham",
        "stoneham",
        "02180",
        "main street",
        "10",
        "",
    )
    duplicate_bucket = address_map.hash_bucket(
        address_map.record_hash(duplicate_key), 4
    )
    different_number = next(
        str(number)
        for number in range(11, 100)
        if address_map.hash_bucket(
            address_map.record_hash((*duplicate_key[:6], str(number), "")), 4
        )
        != duplicate_bucket
    )
    rows = [
        row(retained_ids[0], source_row_index=0),
        row(retained_ids[1], source_row_index=1),
        row(
            str(uuid.UUID(int=3)),
            number=different_number,
            source_row_index=2,
        ),
        row(None, street="", geometry=b"bad", country="", source_row_index=3),
        row(None, geometry=b"bad", country="", source_row_index=4),
        row(None, country=" \t", source_row_index=5),
        row(None, country="United States", source_row_index=6),
        row(None, source_row_index=7),
        row("not-a-uuid", source_row_index=8),
        row(str(uuid.UUID(int=9)), source_row_index=None),
        row(str(uuid.UUID(int=10)), source_object_index=1, source_row_index=10),
    ]
    source_path = tmp_path / "projected.parquet"
    inventory = write_projected(source_path, rows)
    output_dir = tmp_path / "map"
    completion_path = tmp_path / "done.json"

    report = build(source_path, output_dir, completion_path)

    assert report["source"]["inventory"] == inventory
    assert report["source"]["schema_fingerprint_sha256"] == SCHEMA_FINGERPRINT
    assert report["source"]["projected_input"] == {
        "sha256": address_map.sha256_file(source_path),
        "bytes": source_path.stat().st_size,
        "records": len(rows),
    }
    assert report["execution"] == {
        "id": EXECUTION_BUCKET,
        "kind": address_map.EXECUTION_BUCKET_KIND,
        "is_serving_shard_id": False,
    }
    assert report["address_task_identity"] == {
        "inventory_sha256": INVENTORY_DIGEST,
        "task_index": TASK_INDEX,
        "task_digest_sha256": TASK_DIGEST,
        "task_source_digest_sha256": TASK_SOURCE_DIGEST,
        "execution_bucket": EXECUTION_BUCKET,
    }
    assert report["accounting"] == {
        "input_rows": 11,
        "retained_rows": 3,
        "rejected_rows": 8,
        "rejections": {
            "missing_street_or_number": 1,
            "invalid_geometry": 1,
            "blank_country": 1,
            "invalid_country": 1,
            "missing_uuid": 1,
            "invalid_uuid": 1,
            "invalid_source_locator": 2,
            "record_too_large": 0,
            "invalid_record": 0,
        },
        "reconciles": True,
    }
    different_key = (*duplicate_key[:6], different_number, "")
    different_bucket = address_map.hash_bucket(
        address_map.record_hash(different_key), 4
    )
    assert report["partition_counts"] == {
        "schema": address_map.COUNT_SCHEMA,
        "overture_release": RELEASE,
        "maximum_hash_bits": 4,
        "scope": "execution_bucket",
        "counts": sorted(
            [
                {"country": "us", "bucket": duplicate_bucket, "rows": 2},
                {"country": "us", "bucket": different_bucket, "rows": 1},
            ],
            key=lambda item: item["bucket"],
        ),
        "rows": 3,
    }
    assert report["exact_lookup_fanout"] == {
        "scope": "execution_bucket",
        "maximum_candidates": 2,
        "normalized_lookup_key": list(duplicate_key),
    }

    manifest_path = output_dir / report["fragment_manifest"]["relative_path"]
    assert report["fragment_manifest"]["sha256"] == address_map.sha256_file(
        manifest_path
    )
    manifest = json.loads(manifest_path.read_text())
    assert manifest["schema_fingerprint_sha256"] == SCHEMA_FINGERPRINT
    assert manifest["intermediate_ownership"] == {
        "kind": address_map.FRAGMENT_OWNERSHIP_KIND,
        "maximum_hash_bits": 4,
        "is_serving_shard_id": False,
    }
    assert manifest["totals"]["records"] == 3
    assert manifest["totals"]["fragments"] == 3
    seen_owners = set()
    observed_bindings = defaultdict(address_map.SemanticAccumulator)
    for fragment in manifest["fragments"]:
        content = output_dir / fragment["relative_path"]
        assert content.suffix == ".parquet"
        assert content.stat().st_size == fragment["bytes"]
        assert address_map.sha256_file(content) == fragment["sha256"]
        assert pq.ParquetFile(content).schema_arrow.remove_metadata().equals(
            address_map.shuffle_schema()
        )
        reader = address_map.CountryFragmentReader(content, maximum_hash_bits=4)
        try:
            assert reader.header["execution_bucket_is_serving_shard_id"] is False
            assert reader.header["schema_fingerprint_sha256"] == SCHEMA_FINGERPRINT
            owner = fragment["intermediate_ownership"]
            assert owner["is_serving_shard_id"] is False
            assert reader.header["intermediate_ownership"] == owner
            assert (
                reader.header["address_task_identity"]
                == report["address_task_identity"]
            )
            item = reader.next()
            assert item is not None
            key = item[0]
            assert key[0] == owner["country"]
            assert (
                address_map.hash_bucket(address_map.record_hash(key), 4)
                == owner["minimum_bucket"]
                == owner["maximum_bucket"]
            )
            seen_owners.add((owner["country"], owner["minimum_bucket"]))
            observed_bindings[(owner["country"], owner["minimum_bucket"])].add(
                item[1]
            )
            assert reader.next() is None
        finally:
            reader.close()
    assert seen_owners == {
        ("us", duplicate_bucket),
        ("us", different_bucket),
    }
    summary_path = output_dir / report["summary"]["relative_path"]
    header, summaries = address_map.read_semantic_summary(
        summary_path, expected_identity=report["summary"]
    )
    assert header["records"] == 3
    assert pq.ParquetFile(summary_path).schema_arrow.remove_metadata().equals(
        address_map.summary_schema()
    )
    assert {
        (item["country"], item["bucket"]): item["semantic_binding"]
        for item in summaries
    } == {
        identity: accumulator.finish()
        for identity, accumulator in observed_bindings.items()
    }


def test_content_identities_are_deterministic_across_output_directories(tmp_path):
    source_path = tmp_path / "projected.parquet"
    write_projected(
        source_path,
        [row(str(uuid.UUID(int=2))), row(str(uuid.UUID(int=1)))],
    )

    first = build(source_path, tmp_path / "first", tmp_path / "first.json")
    second = build(source_path, tmp_path / "second", tmp_path / "second.json")

    assert first == second
    assert (tmp_path / "first.json").read_bytes() == (
        tmp_path / "second.json"
    ).read_bytes()


def test_spill_merge_compacts_one_owner_across_scan_batches(tmp_path):
    source_path = tmp_path / "projected.parquet"
    write_projected(
        source_path,
        [row(str(uuid.UUID(int=index))) for index in range(1, 6)],
    )

    report = address_map.build_map(
        source_path,
        tmp_path / "map",
        tmp_path / "done.json",
        execution_bucket=EXECUTION_BUCKET,
        expected_release=RELEASE,
        expected_schema_fingerprint_sha256=SCHEMA_FINGERPRINT,
        expected_inventory_sha256=INVENTORY_DIGEST,
        expected_task_index=TASK_INDEX,
        expected_task_digest_sha256=TASK_DIGEST,
        expected_task_source_digest_sha256=TASK_SOURCE_DIGEST,
        maximum_hash_bits=4,
        scan_batch_rows=2,
        max_fragment_rows=100,
        max_fragment_bytes=20_000,
        max_rows=100,
    )

    assert report["fragment_totals"]["fragments"] == 1
    assert report["fragment_totals"]["records"] == 5
    assert report["exact_lookup_fanout"]["maximum_candidates"] == 5


def test_typed_pack_row_groups_have_independent_semantic_integrity(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(address_map, "PARQUET_ROW_GROUP_ROWS", 2)
    source_path = tmp_path / "projected.parquet"
    write_projected(
        source_path,
        [
            row(str(uuid.UUID(int=index + 1)), source_row_index=index)
            for index in range(5)
        ],
    )
    report = address_map.build_map(
        source_path,
        tmp_path / "map",
        tmp_path / "done.json",
        execution_bucket=EXECUTION_BUCKET,
        expected_release=RELEASE,
        expected_schema_fingerprint_sha256=SCHEMA_FINGERPRINT,
        expected_inventory_sha256=INVENTORY_DIGEST,
        expected_task_index=TASK_INDEX,
        expected_task_digest_sha256=TASK_DIGEST,
        expected_task_source_digest_sha256=TASK_SOURCE_DIGEST,
        maximum_hash_bits=4,
        scan_batch_rows=2,
        max_fragment_rows=100,
        max_fragment_bytes=100_000,
        max_rows=100,
    )
    manifest = json.loads(
        (tmp_path / "map" / report["fragment_manifest"]["relative_path"]).read_text()
    )
    pack = manifest["data_packs"][0]
    assert [item["records"] for item in pack["row_groups"]] == [2, 2, 1]
    assert all(
        item["integrity"]["kind"] == "canonical-row-multiset-binding-v1"
        for item in pack["row_groups"]
    )
    reader = address_map.CountryFragmentReader(
        tmp_path / "map" / pack["relative_path"],
        maximum_hash_bits=4,
        row_groups=[1],
    )
    try:
        assert sum(reader.next() is not None for _ in range(3)) == 2
        assert reader.semantic_binding() == pack["row_groups"][1][
            "semantic_binding"
        ]
    finally:
        reader.close()


def test_typed_shuffle_preserves_duplicate_source_rows_with_the_same_id(tmp_path):
    feature_id = str(uuid.UUID(int=99))
    source_path = tmp_path / "projected.parquet"
    write_projected(
        source_path,
        [
            row(feature_id, source_row_index=0),
            row(feature_id, source_row_index=1),
        ],
    )
    output_dir = tmp_path / "map"
    completion_path = tmp_path / "done.json"

    report = build(source_path, output_dir, completion_path)

    assert report["duplicate_id_policy"] == address_map.DUPLICATE_ID_POLICY
    assert report["accounting"]["retained_rows"] == 2
    manifest = json.loads(
        (output_dir / report["fragment_manifest"]["relative_path"]).read_text()
    )
    observed = []
    for pack in manifest["data_packs"]:
        reader = address_map.CountryFragmentReader(
            output_dir / pack["relative_path"], maximum_hash_bits=4
        )
        try:
            while (item := reader.next()) is not None:
                observed.append(address_map.decode_record(item[1]))
        finally:
            reader.close()
    assert [item["id"] for item in observed] == [feature_id, feature_id]
    assert sorted(item["source_row_index"] for item in observed) == [0, 1]


def test_duplicate_total_order_is_independent_of_spill_topology(tmp_path):
    feature_id = str(uuid.UUID(int=101))
    source_path = tmp_path / "projected.parquet"
    write_projected(
        source_path,
        [row(feature_id, source_row_index=index) for index in (3, 1, 2, 0)],
    )
    identities = []
    observed_orders = []
    for scan_batch_rows in (1, 3):
        output_dir = tmp_path / f"map-{scan_batch_rows}"
        report = address_map.build_map(
            source_path,
            output_dir,
            output_dir / "completion.json",
            execution_bucket=EXECUTION_BUCKET,
            expected_release=RELEASE,
            expected_schema_fingerprint_sha256=SCHEMA_FINGERPRINT,
            expected_inventory_sha256=INVENTORY_DIGEST,
            expected_task_index=TASK_INDEX,
            expected_task_digest_sha256=TASK_DIGEST,
            expected_task_source_digest_sha256=TASK_SOURCE_DIGEST,
            maximum_hash_bits=4,
            scan_batch_rows=scan_batch_rows,
            max_fragment_rows=100,
            max_fragment_bytes=100_000,
            max_rows=100,
        )
        manifest = json.loads(
            (output_dir / report["fragment_manifest"]["relative_path"]).read_text()
        )
        assert len(manifest["data_packs"]) == 1
        pack = manifest["data_packs"][0]
        identities.append((pack["sha256"], pack["parquet_layout_binding"]))
        reader = address_map.CountryFragmentReader(
            output_dir / pack["relative_path"], maximum_hash_bits=4
        )
        order = []
        try:
            while (item := reader.next()) is not None:
                order.append(address_map.decode_record(item[1])["source_row_index"])
        finally:
            reader.close()
        observed_orders.append(order)
    assert identities[0] == identities[1]
    assert observed_orders == [[0, 1, 2, 3], [0, 1, 2, 3]]


def test_pack_reader_rejects_reversed_duplicate_source_topology(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(address_map, "PARQUET_ROW_GROUP_ROWS", 64_000)
    feature_id = str(uuid.UUID(int=102))
    payloads = [
        address_map.encode_record(
            {
                **row(feature_id, source_row_index=index),
                "lon": -71.0,
                "lat": 42.0,
                "address_levels": ["MA", "Stoneham"],
            }
        )
        for index in range(3)
    ]
    records = [(address_map.decode_record(payload)["key"], payload) for payload in payloads]
    owner = address_map.record_ownership(records[0][0], 4)
    identity = address_map.write_content_fragment(
        tmp_path,
        records,
        source_inventory_sha256="a" * 64,
        schema_fingerprint_sha256="b" * 64,
        release=RELEASE,
        execution_bucket=EXECUTION_BUCKET,
        task_identity={
            "inventory_sha256": INVENTORY_DIGEST,
            "task_index": TASK_INDEX,
            "task_digest_sha256": TASK_DIGEST,
            "task_source_digest_sha256": TASK_SOURCE_DIGEST,
            "execution_bucket": EXECUTION_BUCKET,
        },
        ownership=address_map.intermediate_ownership(
            owner[0], owner[1], owner[1], 4
        ),
        index=0,
        max_fragment_bytes=100_000,
    )
    original = tmp_path / identity["relative_path"]
    reversed_path = tmp_path / "reversed.parquet"
    table = pq.ParquetFile(original).read()
    pq.write_table(table.take(pa.array([2, 1, 0])), reversed_path, row_group_size=3)
    reader = address_map.CountryFragmentReader(reversed_path, maximum_hash_bits=4)
    try:
        assert reader.next() is not None
        with pytest.raises(ValueError, match="not bucket/key sorted"):
            reader.next()
    finally:
        reader.close()


def test_all_rejected_map_emits_zero_row_summary_without_data_packs(tmp_path):
    source_path = tmp_path / "projected.parquet"
    write_projected(
        source_path,
        [
            row(None, street="", source_row_index=0),
            row("not-a-uuid", source_row_index=1),
        ],
    )
    output_dir = tmp_path / "map"
    report = build(source_path, output_dir, tmp_path / "done.json")

    assert report["accounting"]["retained_rows"] == 0
    assert report["accounting"]["rejected_rows"] == 2
    assert report["fragment_totals"] == {"fragments": 0, "bytes": 0, "records": 0}
    assert report["summary"]["entries"] == 0
    assert report["summary"]["records"] == 0
    assert report["exact_lookup_fanout"] == {
        "scope": "execution_bucket",
        "maximum_candidates": 0,
        "normalized_lookup_key": None,
    }


def test_many_unique_buckets_have_bounded_country_fragments_and_exact_counts(
    tmp_path,
):
    maximum_hash_bits = 12
    selected: list[tuple[int, str]] = []
    seen_buckets = set()
    for number in range(1, 100_000):
        key = (
            "us",
            "ma",
            "stoneham",
            "stoneham",
            "02180",
            "main street",
            str(number),
            "",
        )
        bucket = address_map.hash_bucket(
            address_map.record_hash(key), maximum_hash_bits
        )
        if bucket not in seen_buckets:
            seen_buckets.add(bucket)
            selected.append((number, str(uuid.UUID(int=len(selected) + 1))))
        if len(selected) == 256:
            break
    source_path = tmp_path / "projected.parquet"
    write_projected(
        source_path,
        [row(feature_id, number=str(number)) for number, feature_id in selected],
    )

    report = address_map.build_map(
        source_path,
        tmp_path / "map",
        tmp_path / "done.json",
        execution_bucket=EXECUTION_BUCKET,
        expected_release=RELEASE,
        expected_schema_fingerprint_sha256=SCHEMA_FINGERPRINT,
        expected_inventory_sha256=INVENTORY_DIGEST,
        expected_task_index=TASK_INDEX,
        expected_task_digest_sha256=TASK_DIGEST,
        expected_task_source_digest_sha256=TASK_SOURCE_DIGEST,
        maximum_hash_bits=maximum_hash_bits,
        scan_batch_rows=64,
        max_fragment_rows=32,
        max_fragment_bytes=100_000,
        max_rows=512,
    )

    assert report["partition_counts"]["rows"] == 256
    assert len(report["partition_counts"]["counts"]) == 256
    assert report["fragment_totals"]["fragments"] == 8
    assert report["fragment_totals"]["fragments"] < len(
        report["partition_counts"]["counts"]
    )
    assert report["fragment_totals"]["records"] == 256
    assert report["exact_lookup_fanout"]["maximum_candidates"] == 1
    assert report["configuration"]["max_fragments"] == address_map.fragment_count_cap(
        512, 32
    )


def test_map_rejects_task_replay_under_another_execution_bucket(tmp_path):
    source_path = tmp_path / "projected.parquet"
    write_projected(source_path, [row(str(uuid.UUID(int=1)))])

    with pytest.raises(ValueError, match="task identity differs"):
        address_map.build_map(
            source_path,
            tmp_path / "map",
            tmp_path / "done.json",
            execution_bucket="address-map-task-008",
            expected_release=RELEASE,
            expected_schema_fingerprint_sha256=SCHEMA_FINGERPRINT,
            expected_inventory_sha256=INVENTORY_DIGEST,
            expected_task_index=8,
            expected_task_digest_sha256=TASK_DIGEST,
            expected_task_source_digest_sha256=TASK_SOURCE_DIGEST,
            maximum_hash_bits=4,
            scan_batch_rows=2,
            max_fragment_rows=100,
            max_fragment_bytes=20_000,
            max_rows=100,
        )


def test_map_bounds_spill_file_descriptors(tmp_path):
    source_path = tmp_path / "projected.parquet"
    write_projected(
        source_path,
        [row(str(uuid.UUID(int=index))) for index in range(1, 130)],
    )

    with pytest.raises(ValueError, match="spill count"):
        address_map.build_map(
            source_path,
            tmp_path / "map",
            tmp_path / "done.json",
            execution_bucket=EXECUTION_BUCKET,
            expected_release=RELEASE,
            expected_schema_fingerprint_sha256=SCHEMA_FINGERPRINT,
            expected_inventory_sha256=INVENTORY_DIGEST,
            expected_task_index=TASK_INDEX,
            expected_task_digest_sha256=TASK_DIGEST,
            expected_task_source_digest_sha256=TASK_SOURCE_DIGEST,
            maximum_hash_bits=4,
            scan_batch_rows=1,
            max_fragment_rows=100,
            max_fragment_bytes=20_000,
            max_rows=200,
        )


def test_production_map_requires_complete_source_object_identity(tmp_path):
    source_path = tmp_path / "projected.parquet"
    write_projected(
        source_path,
        [row(str(uuid.UUID(int=1)))],
        complete_source=False,
    )

    with pytest.raises(ValueError, match="records must be positive"):
        build(source_path, tmp_path / "map", tmp_path / "done.json")


def test_production_map_requires_requested_schema_fingerprint(tmp_path):
    source_path = tmp_path / "projected.parquet"
    write_projected(source_path, [row(str(uuid.UUID(int=1)))])

    with pytest.raises(ValueError, match="schema fingerprint differs"):
        address_map.build_map(
            source_path,
            tmp_path / "map",
            tmp_path / "done.json",
            execution_bucket=EXECUTION_BUCKET,
            expected_release=RELEASE,
            expected_schema_fingerprint_sha256="c" * 64,
            expected_inventory_sha256=INVENTORY_DIGEST,
            expected_task_index=TASK_INDEX,
            expected_task_digest_sha256=TASK_DIGEST,
            expected_task_source_digest_sha256=TASK_SOURCE_DIGEST,
            maximum_hash_bits=4,
            scan_batch_rows=2,
            max_fragment_rows=1,
            max_fragment_bytes=20_000,
            max_rows=100,
        )
