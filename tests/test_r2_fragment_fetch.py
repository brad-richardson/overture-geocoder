from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import r2_fragment_fetch as fetcher


pa = pytest.importorskip("pyarrow")
pafs = pytest.importorskip("pyarrow.fs")
pq = pytest.importorskip("pyarrow.parquet")

import global_v2_address_map as address_map  # noqa: E402
import global_v2_address_reduce as address_reduce  # noqa: E402
import global_v2_places_map as places_map  # noqa: E402
import global_v2_places_reduce as places_reduce  # noqa: E402


def test_safe_key_keeps_fragment_under_execution_prefix():
    assert fetcher.safe_key(
        "staging/global-v2/abc/immutable/map/addresses/objects",
        "fragments/sha256/abcd/body.bin",
    ) == (
        "staging/global-v2/abc/immutable/map/addresses/objects/"
        "fragments/sha256/abcd/body.bin"
    )


def test_safe_key_accepts_canonical_places_group_component():
    digest = "a" * 64
    assert fetcher.safe_key(
        "staging/global-v2/build/immutable/map/places/objects",
        f"fragments/group=0123/sha256/{digest}/part-00000.parquet",
    ).endswith(f"/fragments/group=0123/sha256/{digest}/part-00000.parquet")


@pytest.mark.parametrize(
    "value",
    [
        "../catalog.json",
        "/absolute",
        "a//b",
        "a/./b",
        "a/$bad",
        "a/group=0123;touch",
        "a/=leading",
    ],
)
def test_safe_key_rejects_escape_or_shell_metacharacters(value):
    with pytest.raises(ValueError):
        fetcher.safe_key("staging/global-v2/safe", value)


def test_fetch_uses_argv_without_shell_and_removes_failed_output(tmp_path, monkeypatch):
    output = tmp_path / "fragment.bin"
    observed = {}

    def run(command, *, check):
        observed["command"] = command
        observed["check"] = check
        output.write_bytes(b"body")

    monkeypatch.setattr(fetcher.subprocess, "run", run)
    fetcher.fetch(
        bucket="geocoder-shards",
        prefix="staging/global-v2/safe/immutable",
        object_key="fragment.bin",
        output=output,
        endpoint_url="https://account.r2.cloudflarestorage.com",
    )

    assert output.read_bytes() == b"body"
    assert observed["check"] is True
    assert observed["command"][0:2] == ["aws", "s3api"]
    assert observed["command"][observed["command"].index("--key") + 1].startswith(
        "staging/global-v2/safe/"
    )


def _address_pack(tmp_path, monkeypatch):
    monkeypatch.setattr(address_map, "PARQUET_ROW_GROUP_ROWS", 1)
    records = []
    for index in range(1, 5):
        record = {
            "id": str(uuid.UUID(int=index)),
            "lon": -71.0,
            "lat": 42.0,
            "source_object_index": 0,
            "source_row_group": 0,
            "source_row_index": index,
            "country": "US",
            "postal_city": "Stoneham",
            "postcode": "02180",
            "street": "Main Street",
            "number": str(index),
            "unit": "",
            "address_levels": ["MA", "Stoneham"],
        }
        payload = address_map.encode_record(record)
        records.append((address_map.decode_record(payload)["key"], payload))
    records.sort(
        key=lambda item: (*address_map.record_ownership(item[0], 4), item[0])
    )
    buckets = [address_map.record_ownership(key, 4)[1] for key, _ in records]
    identity = address_map.write_content_fragment(
        tmp_path / "pack-output",
        records,
        source_inventory_sha256="a" * 64,
        schema_fingerprint_sha256="b" * 64,
        release="2026-06-17.0",
        execution_bucket="address-map-task-000",
        task_identity={
            "inventory_sha256": "c" * 64,
            "task_index": 0,
            "task_digest_sha256": "d" * 64,
            "task_source_digest_sha256": "e" * 64,
            "execution_bucket": "address-map-task-000",
        },
        ownership=address_map.intermediate_ownership(
            "us", min(buckets), max(buckets), 4
        ),
        index=0,
        max_fragment_bytes=10_000_000,
    )
    return tmp_path / "pack-output" / identity["relative_path"], identity


def test_selective_fetch_materializes_only_requested_original_row_groups(
    tmp_path, monkeypatch
):
    source, identity = _address_pack(tmp_path, monkeypatch)
    bucket = "geocoder-shards"
    prefix = "staging/global-v2/x/immutable/map/addresses/objects"
    object_key = identity["object_key"]
    remote_root = tmp_path / "remote"
    remote = remote_root / bucket / fetcher.safe_key(prefix, object_key)
    remote.parent.mkdir(parents=True)
    remote.write_bytes(source.read_bytes())
    filesystem = pafs.SubTreeFileSystem(
        str(remote_root), pafs.LocalFileSystem()
    )
    output, proof_path = tmp_path / "selected.parquet", tmp_path / "proof.json"

    proof = fetcher.materialize_selected_row_groups(
        bucket=bucket,
        prefix=prefix,
        object_key=object_key,
        output=output,
        proof=proof_path,
        endpoint_url="https://example.invalid",
        row_groups=[1, 3],
        expected_bytes=identity["bytes"],
        expected_sha256=identity["sha256"],
        filesystem=filesystem,
        remote_identity={
            "bytes": identity["bytes"], "sha256": identity["sha256"]
        },
    )

    assert pq.ParquetFile(output).metadata.num_row_groups == 2
    assert proof == __import__("json").loads(proof_path.read_text())
    assert proof["selected_original_row_groups"] == [1, 3]
    assert [
        (item["materialized_index"], item["original_index"], item["records"])
        for item in proof["materialized_row_groups"]
    ] == [(0, 1, 1), (1, 3, 1)]
    assert proof["whole_object"] == {
        "bytes": identity["bytes"],
        "sha256": identity["sha256"],
        "metadata_verified": True,
    }
    assert proof["transport"] == {
        "kind": "pyarrow-s3-random-access",
        "whole_object_downloaded": False,
        "whole_object_fallback_allowed": False,
    }
    assert proof["source_footer"]["row_groups"] == 4
    assert (
        proof["source_footer"]["binding_sha256"]
        == identity["parquet_layout_binding"]["sha256"]
    )
    assert proof["materialized"]["sha256"] == fetcher.sha256_file(output)
    header = proof["source_header"]
    raw_input = {
        "object_key": object_key,
        "bytes": identity["bytes"],
        "sha256": identity["sha256"],
        "records": identity["records"],
            "row_groups": identity["row_groups"],
            "parquet_layout_binding": identity["parquet_layout_binding"],
        "intermediate_ownership": identity["intermediate_ownership"],
        "address_task_identity": header["address_task_identity"],
        "source_fragment_index": header["fragment_index"],
    }
    reduce = {
        "overture_release": header["overture_release"],
        "source": {
            "source_inventory_sha256": header["source_inventory_sha256"],
            "schema_fingerprint_sha256": header["schema_fingerprint_sha256"],
        },
    }
    assert address_reduce._validate_selective_fetch_proof(  # noqa: SLF001
        proof_path,
        output,
        raw_input=raw_input,
        reduce=reduce,
        selected_row_groups=[1, 3],
    ) == proof


def test_selective_fetch_supports_places_pack_schema(tmp_path):
    schema = places_map._fragment_arrow_schema(  # noqa: SLF001
        {
            "artifact_schema": places_map.FRAGMENT_SCHEMA,
            "overture.places_pack_header": (
                '{"artifact_schema":"overture-global-v2-places-map-pack-v2",'
                '"execution_group_level":4,"inventory_sha256":"'
                + "a" * 64
                + '","map_task_digest":"'
                + "b" * 64
                + '","physical_order":"execution_group,partition_cell"}'
            ),
        }
    )
    rows = []
    for index, group in enumerate(("0000", "3333"), start=1):
        rows.append(
            {
                "gers_id": str(uuid.UUID(int=index)), "primary_name": "Place",
                "alt_names": "", "brand_name": "", "category_primary": "cafe",
                "basic_category": "eat_and_drink", "locality": "Town",
                "region": "Region", "country": "US", "lat": 0.0, "lon": 0.0,
                "confidence": 0.5, "operating_status": "open", "partition_key": index,
                "partition_cell": group + "0" * 8, "execution_group": group,
                "source_uri": "s3://fixture/places.parquet", "source_row_group": 0,
                "source_row_index": index,
            }
        )
    source = tmp_path / "places.parquet"
    with pq.ParquetWriter(source, schema, compression="zstd") as writer:
        for row in rows:
            writer.write_table(pa.Table.from_pylist([row], schema=schema), row_group_size=1)
    digest = fetcher.sha256_file(source)
    bucket, prefix, object_key = "geocoder-shards", "staging/global-v2/x", "places.parquet"
    remote_root = tmp_path / "places-remote"
    remote = remote_root / bucket / fetcher.safe_key(prefix, object_key)
    remote.parent.mkdir(parents=True)
    remote.write_bytes(source.read_bytes())
    output, proof_path = tmp_path / "places-selected.parquet", tmp_path / "places-proof.json"
    proof = fetcher.materialize_selected_row_groups(
        bucket=bucket, prefix=prefix, object_key=object_key, output=output,
        proof=proof_path, endpoint_url="https://example.invalid", row_groups=[1],
        expected_bytes=source.stat().st_size, expected_sha256=digest,
        filesystem=pafs.SubTreeFileSystem(str(remote_root), pafs.LocalFileSystem()),
        remote_identity={"bytes": source.stat().st_size, "sha256": digest},
        artifact_family="places",
    )
    assert proof["artifact_family"] == "places"
    assert proof["selected_original_row_groups"] == [1]
    assert pq.read_table(output).column("execution_group").to_pylist() == ["3333"]
    physical, footer_sha256, footer_bytes = places_map._footer_binding_for_pack(  # noqa: SLF001
        source
    )
    fragment = {
        "object_key": object_key,
        "bytes": source.stat().st_size,
        "sha256": digest,
        "records": 1,
        "footer_sha256": footer_sha256,
        "footer_bytes": footer_bytes,
        "selected_row_groups": [1],
        "row_groups": physical,
    }
    places_reduce._validate_selective_proof(  # noqa: SLF001
        fragment,
        proof,
        output.stat().st_size,
        fetcher.sha256_file(output),
    )


def test_selective_fetch_fails_before_reads_on_identity_mismatch(
    tmp_path, monkeypatch
):
    source, identity = _address_pack(tmp_path, monkeypatch)
    output, proof = tmp_path / "selected.parquet", tmp_path / "proof.json"
    with pytest.raises(ValueError, match="whole-object identity"):
        fetcher.materialize_selected_row_groups(
            bucket="geocoder-shards",
            prefix="staging/global-v2/x/immutable/map/addresses/objects",
            object_key=identity["object_key"],
            output=output,
            proof=proof,
            endpoint_url="https://example.invalid",
            row_groups=[0],
            expected_bytes=source.stat().st_size,
            expected_sha256=identity["sha256"],
            remote_identity={
                "bytes": source.stat().st_size, "sha256": "f" * 64
            },
        )
    assert not output.exists() and not proof.exists()


@pytest.mark.parametrize("value", ["[1, 0]", "[0,0]", "[0, 2]", "[]", "0"])
def test_selective_row_group_json_must_be_compact_canonical(value):
    with pytest.raises(ValueError):
        fetcher.parse_row_groups(value)
