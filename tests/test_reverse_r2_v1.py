"""Contract tests for the reverse R2 bucket reducer and binary catalog."""

from __future__ import annotations

import copy
import importlib.util
import json
import random
import subprocess
import sys
from pathlib import Path

import pytest


pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pytest.importorskip("duckdb")

ROOT = Path(__file__).parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


R2 = _load("reverse_r2_test_driver", "scripts/reverse_r2_v1.py")

REQUEST = "a" * 64


@pytest.fixture(scope="module")
def binaries() -> dict[str, Path]:
    subprocess.run(
        [
            "cargo",
            "build",
            "-p",
            "geocoder-construction",
            "--bin",
            "reverse-encode-v1",
            "--bin",
            "reverse-verify-v1",
        ],
        cwd=ROOT / "crates",
        check=True,
    )
    target = ROOT / "crates/target/debug"
    return {
        "encode": target / "reverse-encode-v1",
        "verify": target / "reverse-verify-v1",
    }


def point_e7(cell: str, index: int) -> tuple[int, int]:
    generator = random.Random(f"{cell}:{index}")
    y, x = R2.REVERSE.cell_yx(cell)
    longitude = (
        x * R2.REVERSE.LONGITUDE_E7_PER_CELL
        - R2.REVERSE.LONGITUDE_E7_ORIGIN
        + generator.randrange(R2.REVERSE.LONGITUDE_E7_PER_CELL)
    )
    latitude = (
        y * R2.REVERSE.LATITUDE_E7_PER_CELL
        - R2.REVERSE.LATITUDE_E7_ORIGIN
        + generator.randrange(R2.REVERSE.LATITUDE_E7_PER_CELL)
    )
    return longitude, latitude


def places_rows(cell: str, count: int, start: int = 0) -> list[dict]:
    rows = []
    for offset in range(count):
        index = start + offset
        longitude, latitude = point_e7(cell, index)
        rows.append(
            {
                "feature_id": index.to_bytes(16, "big"),
                "partition_cell": cell,
                "longitude": longitude / 1e7,
                "latitude": latitude / 1e7,
                "primary_name": f"Place {index}",
                "brand_name": "",
                "category": "eat_and_drink",
                "locality": "Test",
                "region": "",
                "country": "XX",
                "confidence_rank": index % 256,
                "source_object_index": 7,
                "source_row_group": 3,
                "source_row_index": index,
            }
        )
    return rows


def address_rows(cell: str, count: int, start: int = 0) -> list[dict]:
    rows = []
    for offset in range(count):
        index = start + offset
        longitude, latitude = point_e7(cell, index)
        rows.append(
            {
                "feature_id": index.to_bytes(16, "big"),
                "partition_cell": cell,
                "longitude_e7": longitude,
                "latitude_e7": latitude,
                "display_country": "XX",
                "postal_city": "Test",
                "postcode": "12345",
                "street": "Main Street",
                "number": str(index),
                "unit": "",
                "address_levels": ["Region"],
                "source_object_index": 8,
                "source_row_group": 4,
                "source_row_index": index,
            }
        )
    return rows


def marker_with_pack(
    *,
    root: Path,
    store,
    family: str,
    task_id: str,
    rows: list[dict],
) -> dict:
    cell = rows[0]["partition_cell"]
    bucket = R2.cell_bucket(cell)
    pack_path = root / f"{task_id}.parquet"
    pq.write_table(
        pa.Table.from_pylist(rows, schema=R2.R1.input_schema(family)),
        pack_path,
        row_group_size=8,
    )
    if family == "places":
        directory = R2.PLACES.positions_directory(
            pack_path, bucket=bucket, bits=8
        )
        pack_prefix = "map/places-v1/positions"
        directory_prefix = "map/places-v1/position-directories"
        artifact_key = "positions"
        artifact_schema = R2.PLACES.POSITIONS_SCHEMA
        admitted_key = "admitted_features"
    else:
        directory = R2.ADDRESS.address_records_directory(
            pack_path, bucket=bucket, bits=8
        )
        pack_prefix = "map/address/records"
        directory_prefix = "map/address/record-directories"
        artifact_key = "address_records"
        artifact_schema = R2.ADDRESS.ADDRESS_RECORDS_SCHEMA
        admitted_key = "admitted_rows"
    directory_path = root / f"{task_id}.directory.json"
    directory_path.write_text(json.dumps(directory, sort_keys=True) + "\n")
    pack = {
        "pack_id": bucket,
        "shuffle_bucket": bucket,
        "records": len(rows),
        "object": store.put_content(pack_path, pack_prefix, ".parquet"),
        "directory_object": store.put_content(
            directory_path, directory_prefix, ".json"
        ),
        "directory": directory,
    }
    return {
        "schema": (
            R2.PLACES.MARKER_SCHEMA
            if family == "places"
            else R2.ADDRESS.MARKER_SCHEMA
        ),
        "request_sha256": REQUEST,
        "task_id": task_id,
        "transform": {admitted_key: len(rows)},
        artifact_key: {
            "schema": artifact_schema,
            "records": len(rows),
            "shuffle_bucket_bits": 8,
            "packs": [pack],
        },
    }


def reduce(
    *,
    family: str,
    markers: list[dict],
    store,
    start: int,
    end: int,
    root: Path,
    binaries: dict[str, Path],
    limits=None,
) -> dict:
    return R2.reduce_bucket_range(
        family=family,
        request_sha256=REQUEST,
        markers=markers,
        store=store,
        bucket_start=start,
        bucket_end=end,
        scratch_root=root / "scratch",
        encoder_binary=binaries["encode"],
        verifier_binary=binaries["verify"],
        limits=limits,
    )


def test_places_two_range_reduce_and_binary_catalog(binaries, tmp_path):
    store = R2.ADDRESS.LocalObjectStore(tmp_path / "store")
    # The reviewed boundary fixture: these adjacent cells intentionally hash
    # into two different shuffle buckets (206 and 108).
    markers = [
        marker_with_pack(
            root=tmp_path,
            store=store,
            family="places",
            task_id="places-map-a",
            rows=places_rows("c085", 13),
        ),
        marker_with_pack(
            root=tmp_path,
            store=store,
            family="places",
            task_id="places-map-b",
            rows=places_rows("c086", 9, 100),
        ),
    ]
    assert R2.cell_bucket("c085") == 206
    assert R2.cell_bucket("c086") == 108
    reductions = [
        reduce(
            family="places",
            markers=markers,
            store=store,
            start=0,
            end=150,
            root=tmp_path / "low",
            binaries=binaries,
        ),
        reduce(
            family="places",
            markers=markers,
            store=store,
            start=151,
            end=255,
            root=tmp_path / "high",
            binaries=binaries,
        ),
    ]
    assert [item["records"] for item in reductions] == [9, 13]
    assert {
        shard["partition_cell"]
        for reduction in reductions
        for shard in reduction["shards"]
    } == {"c085", "c086"}
    assert all(
        reduction["records"]
        == reduction["loaded_records"]
        == reduction["directory_records"]
        for reduction in reductions
    )
    assert all(
        reduction["evidence"]["resources"]["observations"]
        for reduction in reductions
    )
    assert sum(
        reduction["evidence"]["output_bytes"] for reduction in reductions
    ) == sum(
        shard["object"]["bytes"]
        for reduction in reductions
        for shard in reduction["shards"]
    )
    mismatched = copy.deepcopy(reductions)
    mismatched[0]["cells"][0]["records"] += 1
    with pytest.raises(ValueError, match="metadata differs"):
        R2.validate_reduction_cover(
            mismatched,
            family="places",
            request_sha256=REQUEST,
            expected_records=22,
        )

    result = R2.assemble_catalog(
        family="places",
        request_sha256=REQUEST,
        reductions=reductions,
        expected_records=22,
        store=store,
        scratch_root=tmp_path / "catalog",
    )
    assert result["records"] == 22
    assert result["cells"] == 2
    assert result["bucket_ranges"] == 2
    root = R2.parse_catalog_root(store.path(result["root"]["key"]).read_bytes())
    assert root["family"] == "places"
    assert root["records"] == 22
    assert root["cells"] == 2
    assert result["root"]["bytes"] == 688

    shard_id = R2.catalog_shard_id("c085")
    shard = R2.parse_catalog_shard(
        store.path(result["catalog_shards"][shard_id]["key"]).read_bytes()
    )
    assert [cell["partition_cell"] for cell in shard["cells"]] == [
        "c085",
        "c086",
    ]
    assert sum(cell["records"] for cell in shard["cells"]) == 22
    assert store.read_json(R2.catalog_marker_key("places")) == result


def test_address_reduce_uses_the_same_spatial_r2_path(binaries, tmp_path):
    store = R2.ADDRESS.LocalObjectStore(tmp_path / "store")
    marker = marker_with_pack(
        root=tmp_path,
        store=store,
        family="addresses",
        task_id="addresses-map-a",
        rows=address_rows("c328", 17),
    )
    reduction = reduce(
        family="addresses",
        markers=[marker],
        store=store,
        start=0,
        end=255,
        root=tmp_path,
        binaries=binaries,
    )
    assert reduction["records"] == 17
    assert reduction["shards"][0]["partition_cell"] == "c328"
    assert 0 < reduction["shards"][0]["dictionary_bytes"] <= 8 * 1024 * 1024
    shard = R2.REVERSE.ReverseShard(
        store.path(reduction["shards"][0]["object"]["key"]).read_bytes()
    )
    assert shard.family == "addresses"
    assert shard.records == 17
    assert shard.dictionary_bytes == reduction["shards"][0]["dictionary_bytes"]
    decoded = [
        record
        for leaf in shard.leaf_ranges()
        for record in shard.decode_leaf(leaf.key)
    ]
    assert len(decoded) == 17
    assert decoded[0]["street"] == "Main Street"
    assert decoded[0]["address_levels"] == ["Region"]

    catalog = R2.assemble_catalog(
        family="addresses",
        request_sha256=REQUEST,
        reductions=[reduction],
        expected_records=17,
        store=store,
        scratch_root=tmp_path / "catalog",
    )
    root = R2.parse_catalog_root(store.path(catalog["root"]["key"]).read_bytes())
    assert root["family"] == "addresses"
    assert root["max_radius_m"] == 500
    catalog_shard = R2.parse_catalog_shard(
        store.path(
            catalog["catalog_shards"][R2.catalog_shard_id("c328")]["key"]
        ).read_bytes()
    )
    assert catalog_shard["cells"][0]["dictionary_bytes"] == shard.dictionary_bytes

    oversized = copy.deepcopy(reduction["shards"][0])
    oversized["dictionary_bytes"] = R2.MAX_ADDRESS_DICTIONARY_BYTES + 1
    oversized["object"]["bytes"] = (
        R2.SHARD_HEADER_BYTES
        + oversized["dictionary_bytes"]
        + oversized["index_bytes"]
        + 1
    )
    with pytest.raises(ValueError, match="catalog cell fields"):
        R2.encode_catalog_shard(
            family="addresses",
            shard_id=R2.catalog_shard_id("c328"),
            cells=[oversized],
        )


def test_compact_plan_drops_embedded_directories_and_drives_reduce(
    binaries, tmp_path
):
    store = R2.ADDRESS.LocalObjectStore(tmp_path / "store")
    markers = [
        marker_with_pack(
            root=tmp_path,
            store=store,
            family="places",
            task_id="places-map-a",
            rows=places_rows("c085", 5),
        ),
        marker_with_pack(
            root=tmp_path,
            store=store,
            family="places",
            task_id="places-map-b",
            rows=places_rows("c086", 7, start=5),
        ),
    ]
    plan = R2.build_plan(
        family="places",
        request_sha256=REQUEST,
        markers=iter(markers),
    )
    assert plan["expected_records"] == 12
    assert plan["task_ids"] == ["places-map-a", "places-map-b"]
    assert all("directory" not in pack for pack in plan["packs"])

    reduction = R2.reduce_bucket_range(
        family="places",
        request_sha256=REQUEST,
        plan=plan,
        store=store,
        bucket_start=0,
        bucket_end=255,
        scratch_root=tmp_path / "reduce-from-plan",
        encoder_binary=binaries["encode"],
        verifier_binary=binaries["verify"],
    )
    assert reduction["records"] == plan["expected_records"]
    assert {cell["partition_cell"] for cell in reduction["cells"]} == {
        "c085",
        "c086",
    }


def test_content_addressed_directory_can_be_shared_but_data_pack_cannot(
    binaries, tmp_path
):
    store = R2.ADDRESS.LocalObjectStore(tmp_path / "store")
    markers = [
        marker_with_pack(
            root=tmp_path,
            store=store,
            family="places",
            task_id="places-map-a",
            rows=places_rows("be85", 1),
        ),
        marker_with_pack(
            root=tmp_path,
            store=store,
            family="places",
            task_id="places-map-b",
            rows=places_rows("be85", 1, start=100),
        ),
    ]
    first = markers[0]["positions"]["packs"][0]
    second = markers[1]["positions"]["packs"][0]
    assert first["directory_object"] == second["directory_object"]
    assert first["object"]["key"] != second["object"]["key"]

    plan = R2.build_plan(
        family="places",
        request_sha256=REQUEST,
        markers=markers,
    )
    assert plan["expected_records"] == 2
    assert len(plan["packs"]) == 2
    reduction = R2.reduce_bucket_range(
        family="places",
        request_sha256=REQUEST,
        plan=plan,
        store=store,
        bucket_start=0,
        bucket_end=255,
        scratch_root=tmp_path / "shared-directory",
        encoder_binary=binaries["encode"],
        verifier_binary=binaries["verify"],
    )
    assert reduction["source_packs"][0] != reduction["source_packs"][1]
    assert reduction["source_directories"][0] == reduction["source_directories"][1]
    assert R2.validate_reduction_cover(
        [reduction],
        family="places",
        request_sha256=REQUEST,
        expected_records=2,
    ) == [reduction]

    repeated_data = copy.deepcopy(markers)
    repeated_data[1]["positions"]["packs"][0]["object"] = first["object"]
    with pytest.raises(ValueError, match="repeats a logical data pack object"):
        R2.per_record_packs(
            repeated_data,
            family="places",
            request_sha256=REQUEST,
        )


def test_r2_replay_requires_store_computed_content_md5():
    class Destination:
        scheme = "r2"

        @staticmethod
        def identity(_key):
            return {
                "bytes": 7,
                "sha256": "b" * 64,
                "content_md5": "c" * 32,
            }

    publisher = object.__new__(R2.DirectPublishedArtifactStore)
    publisher.destination = Destination()
    publisher.family_prefix = "slice-2026-07-29.0/families/places"
    with pytest.raises(ValueError, match="store-computed content MD5"):
        publisher.verify_identity(
            {
                "key": "slice-2026-07-29.0/families/places/reverse/x",
                "bytes": 7,
                "sha256": "b" * 64,
            }
        )
    publisher.verify_identity(
        {
            "key": "slice-2026-07-29.0/families/places/reverse/x",
            "bytes": 7,
            "sha256": "b" * 64,
            "content_md5": "c" * 32,
        }
    )
    with pytest.raises(ValueError, match="escapes the claimed family slice"):
        publisher.verify_identity(
            {
                "key": "slice-2026-07-30.0/families/places/reverse/x",
                "bytes": 7,
                "sha256": "b" * 64,
                "content_md5": "c" * 32,
            }
        )


def test_direct_publication_claims_slice_and_never_copies_reverse_artifacts(
    binaries, tmp_path
):
    construction = R2.ADDRESS.LocalObjectStore(tmp_path / "construction")
    destination = R2.PROMOTION.LocalTree(tmp_path / "published")
    publisher = R2.DirectPublishedArtifactStore(
        destination=destination,
        version="slice-2026-07-29.0",
        family="places",
        request_sha256=REQUEST,
        overture_release="2026-07-22.0",
    )
    marker = marker_with_pack(
        root=tmp_path,
        store=construction,
        family="places",
        task_id="places-map-direct",
        rows=places_rows("be85", 7),
    )
    reduction = R2.reduce_bucket_range(
        family="places",
        request_sha256=REQUEST,
        markers=[marker],
        store=construction,
        artifact_store=publisher,
        bucket_start=0,
        bucket_end=255,
        scratch_root=tmp_path / "reduce",
        encoder_binary=binaries["encode"],
        verifier_binary=binaries["verify"],
    )
    assert R2.reduce_bucket_range(
        family="places",
        request_sha256=REQUEST,
        markers=[marker],
        store=construction,
        artifact_store=publisher,
        bucket_start=0,
        bucket_end=255,
        scratch_root=tmp_path / "resume-must-not-run",
        encoder_binary=tmp_path / "missing-encoder",
        verifier_binary=tmp_path / "missing-verifier",
    ) == reduction
    shard = reduction["shards"][0]["object"]
    assert shard["key"].startswith(
        "slice-2026-07-29.0/families/places/reverse/shards/sha256/"
    )
    assert destination.identity(shard["key"]) == {
        "bytes": shard["bytes"],
        "sha256": shard["sha256"],
    }
    assert construction.read_json(R2.range_marker_key("places", 0, 255)) == reduction
    assert reduction["slice_claim"] == publisher.slice_claim
    retry_publisher = R2.DirectPublishedArtifactStore(
        destination=destination,
        version="slice-2026-07-30.0",
        family="places",
        request_sha256=REQUEST,
        overture_release="2026-07-22.0",
    )
    with pytest.raises(
        ValueError, match="durable reverse range marker differs"
    ):
        R2.reduce_bucket_range(
            family="places",
            request_sha256=REQUEST,
            markers=[marker],
            store=construction,
            artifact_store=retry_publisher,
            bucket_start=0,
            bucket_end=255,
            scratch_root=tmp_path / "cross-slice-resume-must-not-run",
            encoder_binary=tmp_path / "missing-encoder",
            verifier_binary=tmp_path / "missing-verifier",
        )

    catalog = R2.assemble_catalog(
        family="places",
        request_sha256=REQUEST,
        reductions=[reduction],
        expected_records=7,
        store=construction,
        artifact_store=publisher,
        scratch_root=tmp_path / "catalog",
    )
    assert R2.assemble_catalog(
        family="places",
        request_sha256=REQUEST,
        reductions=[reduction],
        expected_records=7,
        store=construction,
        artifact_store=publisher,
        scratch_root=tmp_path / "catalog-resume-must-not-run",
    ) == catalog
    assert catalog["root"]["key"] == (
        "slice-2026-07-29.0/families/places/reverse-catalog.rcat"
    )
    assert catalog["root"]["bytes"] == 688
    assert len(catalog["catalog_shards"]) == 16
    assert len(catalog["artifacts"]) == 18
    assert construction.read_json(R2.catalog_marker_key("places")) == catalog
    family_keys = destination.list_prefix(
        "slice-2026-07-29.0/families/places/"
    )
    assert set(family_keys) == {
        artifact["key"] for artifact in catalog["artifacts"]
    }
    claim = json.loads(
        destination.read_bytes(
            "slice-2026-07-29.0/claims/places.json"
        )
    )
    assert claim == {
        "schema": R2.SLICE_CLAIM_SCHEMA,
        "version": "slice-2026-07-29.0",
        "family": "places",
        "request_sha256": REQUEST,
        "overture_release": "2026-07-22.0",
    }
    with pytest.raises(ValueError, match="differs"):
        R2.DirectPublishedArtifactStore(
            destination=destination,
            version="slice-2026-07-29.0",
            family="places",
            request_sha256="b" * 64,
            overture_release="2026-07-22.0",
        )


@pytest.mark.parametrize(
    "manifest",
    (
        "slice-2026-07-29.0/slice-manifest.json",
        "slice-2026-07-29.0/families/places/family-manifest.json",
    ),
)
def test_direct_publication_refuses_finalized_destination(tmp_path, manifest):
    destination = R2.PROMOTION.LocalTree(tmp_path / "published")
    destination.put_bytes_create_only(manifest, b"finalized\n")
    with pytest.raises(ValueError, match="already finalized"):
        R2.DirectPublishedArtifactStore(
            destination=destination,
            version="slice-2026-07-29.0",
            family="places",
            request_sha256=REQUEST,
            overture_release="2026-07-22.0",
            claim_slice=False,
        )


def test_direct_publication_dry_run_admits_fresh_slice_without_claiming_it(
    tmp_path,
):
    destination = R2.PROMOTION.LocalTree(tmp_path / "published")
    publisher = R2.DirectPublishedArtifactStore(
        destination=destination,
        version="slice-2026-07-29.0",
        family="places",
        request_sha256=REQUEST,
        overture_release="2026-07-22.0",
        claim_slice=False,
    )
    assert publisher.admission_state == "fresh"
    assert publisher.slice_claim is None
    assert (
        destination.identity("slice-2026-07-29.0/claims/places.json")
        is None
    )


def test_reducer_cross_checks_real_pack_count_against_directory(binaries, tmp_path):
    store = R2.ADDRESS.LocalObjectStore(tmp_path / "store")
    marker = marker_with_pack(
        root=tmp_path,
        store=store,
        family="places",
        task_id="places-map-a",
        rows=places_rows("be85", 4),
    )
    lying = copy.deepcopy(marker)
    pack = lying["positions"]["packs"][0]
    pack["records"] = 5
    pack["directory"]["records"] = 5
    pack["directory"]["cells"][0]["records"] = 5
    pack["directory"]["row_groups"][0]["records"] += 1
    lying["positions"]["records"] = 5
    lying["transform"]["admitted_features"] = 5
    directory_path = tmp_path / "lying-directory.json"
    directory_path.write_text(json.dumps(pack["directory"], sort_keys=True) + "\n")
    pack["directory_object"] = store.put_content(
        directory_path, "map/places-v1/position-directories", ".json"
    )
    with pytest.raises(ValueError, match=r"COUNT\(\*\) differs"):
        reduce(
            family="places",
            markers=[lying],
            store=store,
            start=0,
            end=255,
            root=tmp_path,
            binaries=binaries,
        )


def test_reducer_enforces_aggregate_output_cap(binaries, tmp_path):
    store = R2.ADDRESS.LocalObjectStore(tmp_path / "store")
    marker = marker_with_pack(
        root=tmp_path,
        store=store,
        family="addresses",
        task_id="addresses-map-a",
        rows=address_rows("c328", 2),
    )
    with pytest.raises(ValueError, match="output exceeds"):
        reduce(
            family="addresses",
            markers=[marker],
            store=store,
            start=0,
            end=255,
            root=tmp_path,
            binaries=binaries,
            limits=R2.ADDRESS.Limits(max_output_bytes=1),
        )
    assert store.read_json(R2.range_marker_key("addresses", 0, 255)) is None


def test_catalog_rejects_a_bucket_cover_gap():
    reduction = {
        "schema": R2.RANGE_SCHEMA,
        "family": "places",
        "request_sha256": REQUEST,
        "bucket_start": 1,
        "bucket_end": 255,
        "records": 1,
        "loaded_records": 1,
        "directory_records": 1,
        "source_packs": [],
        "cells": [],
        "shards": [],
    }
    with pytest.raises(ValueError, match="exactly cover"):
        R2.validate_reduction_cover(
            [reduction],
            family="places",
            request_sha256=REQUEST,
            expected_records=1,
        )
