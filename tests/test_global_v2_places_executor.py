import copy
import hashlib
import json
import struct
import sys
import uuid
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import global_build_manifest  # noqa: E402
import global_v2_build_request  # noqa: E402
import build_places_region_shards as places_builder  # noqa: E402
import global_v2_places_head as places_head  # noqa: E402
import global_v2_places_plan as places_plan  # noqa: E402
import global_v2_places_reduce as places_reduce  # noqa: E402
from experiment_places_compact_index import place_from_row  # noqa: E402
from experiment_places_compact_shard import build_artifact  # noqa: E402
from experiment_places_head_repack import RepackHead  # noqa: E402
from global_v2_places_head import (  # noqa: E402
    build_global_head,
    validate_head_report,
)
from global_v2_places_inventory import (  # noqa: E402
    REQUIRED_FIELD_TYPES,
    approved_prefix,
    build_inventory,
    canonical_json_bytes,
    canonical_schema_contract,
)
from global_v2_places_map import project_row, run_map_task  # noqa: E402
from global_v2_places_plan import (  # noqa: E402
    ARTIFACT_LISTING_SCHEMA,
    _assign_reduce_jobs,
    _load_predecessor_splits,
    build_places_plan,
    digest_value,
    finalize_places_family,
    sha256_file,
    validate_places_final_report,
    validate_places_plan,
)
from global_v2_places_reduce import (  # noqa: E402
    execute_reduce_job,
    validate_reduce_report,
)
from places_partition import quadkey_bbox  # noqa: E402


RELEASE = "2026-06-18.0"


def wkb_point(longitude, latitude):
    return struct.pack("<BIdd", 1, 1, longitude, latitude)


def row_for_cell(identifier, cell, confidence):
    xmin, ymin, xmax, ymax = quadkey_bbox(cell)
    return {
        "id": str(uuid.UUID(int=identifier)),
        "geometry": wkb_point((xmin + xmax) / 2, (ymin + ymax) / 2),
        "names": {
            "primary": f"Coffee Place {identifier}",
            "common": {"fr": f"Cafe {identifier}"},
        },
        "brand": {"names": {"primary": "Coffee Brand"}},
        "categories": {"primary": "cafe"},
        "basic_category": "eat_and_drink",
        "addresses": [{"locality": "Town", "region": "Region", "country": "US"}],
        "confidence": confidence,
        "operating_status": "open",
    }


def fixture_rows():
    groups = ["0000", "1111", "2222", "3333"]
    first = [
        row_for_cell(index + 1, group + "00" + "000000", 0.6 + index * 0.05)
        for index, group in enumerate(groups)
    ]
    second = [
        row_for_cell(index + 5, group + "00" + "111111", 0.8 + index * 0.04)
        for index, group in enumerate(groups)
    ]
    return [first, second]


def inventory_for_groups(groups):
    rows = [row for group in groups for row in group]
    prefix = approved_prefix(RELEASE)
    schema = canonical_schema_contract(
        [
            {"path": path, "type": field_type, "nullable": True}
            for path, field_type in REQUIRED_FIELD_TYPES.items()
        ]
    )
    row_groups = [
        {
            "index": index,
            "rows": len(group),
            "selected_compressed_bytes": 1_000,
            "selected_uncompressed_bytes": 2_000,
        }
        for index, group in enumerate(groups)
    ]
    details = {
        "records": len(rows),
        "row_group_count": len(row_groups),
        "row_groups": row_groups,
        "schema_contract": schema,
    }
    return build_inventory(
        RELEASE,
        [{"uri": prefix + "part-0.parquet", "etag": "etag-0", "bytes": 10_000}],
        lambda _: details,
        target_rows=len(groups[0]),
        max_selected_uncompressed_bytes=10_000,
        max_groups=1,
        max_tasks=len(groups),
    )


def build_request(inventory, **overrides):
    values = {
        "overture_release": RELEASE,
        "geocoder_build": "2026-07-19.7",
        "slice_version": "slice-2026-07-19.7",
        "legacy_core_version": "2026-07-18.0",
        "legacy_core_overture_release": RELEASE,
        "legacy_core_manifest_key": "2026-07-18.0/release-manifest.json",
        "legacy_core_manifest_sha256": "1" * 64,
        "addresses_inventory_sha256": "2" * 64,
        "addresses_schema_fingerprint_sha256": "3" * 64,
        "addresses_predecessor_family_manifest_sha256": None,
        "addresses_lineage_generation": 1,
        "places_inventory_sha256": inventory["inventory_sha256"],
        "places_schema_fingerprint_sha256": inventory["schema_contract"][
            "fingerprint_sha256"
        ],
        "places_predecessor_family_manifest_sha256": None,
        "places_lineage_generation": 1,
        "producer_commit": "a" * 40,
        "places_split_row_cap": 1,
        "head_minimum_candidates": 2,
        "head_famous_cap": 2,
        "source_task_limit": 2,
        "reduce_job_limit": 2,
    }
    values.update(overrides)
    return global_v2_build_request.build_request(**values)


def fake_fragment_writer(batches, path, metadata):
    lines = [canonical_json_bytes({"metadata": metadata})]
    records = 0
    for batch in batches:
        rows = batch.to_pylist()
        records += len(rows)
        lines.extend(canonical_json_bytes(row) for row in rows)
    path.write_bytes(b"\n".join(lines) + b"\n")
    return records


def json_fragment_reader(fragment, path):
    ownership = [
        fragment["row_groups"][index] for index in fragment["selected_row_groups"]
    ]
    lines = path.read_text().splitlines()
    for line in lines[1:]:
        row = json.loads(line)
        if any(
            item["execution_group"] == row["execution_group"]
            and item["minimum_maximum_level_cell"]
            <= row["partition_cell"]
            <= item["maximum_maximum_level_cell"]
            for item in ownership
        ):
            yield row


def predecessor_catalog(
    tmp_path,
    *,
    lineage_generation=1,
    maximum_level=11,
    minimum_level=6,
    row_cap=1,
    scheme="world-quadkey-v1",
    tokenizer="previous-tokenizer-v1",
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "catalog.pcat"
    split_cell = "0" * minimum_level
    places_builder.build_catalog(
        [places_builder._route(split_cell + digit) for digit in "0123"],
        path,
        coverage=[-180.0, -90.0, 180.0, 90.0],
        minimum_level=minimum_level,
        maximum_level=maximum_level,
        row_cap=row_cap,
        split_cells=[split_cell],
        lineage_generation=(
            lineage_generation
            if lineage_generation is None or type(lineage_generation) is int
            else None
        ),
    )
    payload = places_builder._read_catalog_payload(path)
    if lineage_generation is not None and type(lineage_generation) is not int:
        payload["partition"]["lineage_generation"] = lineage_generation
    payload["tokenizer_version"] = tokenizer
    payload["partition"]["scheme"] = scheme
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(
        places_builder.CATALOG_PREAMBLE.pack(places_builder.CATALOG_MAGIC, len(encoded))
        + encoded
    )
    sha256, size = sha256_file(path)
    manifest = global_build_manifest.build_family_manifest(
        "places",
        lineage={
            "overture_release": RELEASE,
            "build_id": "b" * 64,
            "producer_commit": "a" * 40,
            "producer_script": "scripts/global_v2_places_plan.py",
            "producer_version": "1",
        },
        versions={
            "format": "PCSH0001",
            "tokenizer": tokenizer,
            "normalization": None,
        },
        region={
            "name": "global",
            "bbox": [-180.0, -90.0, 180.0, 90.0],
            "bbox_scope": "exact",
        },
        artifacts=[{"object_key": "catalog.pcat", "bytes": size, "sha256": sha256}],
    )
    return path, manifest, split_cell


def predecessor_request_fields(manifest):
    encoded = global_build_manifest.canonical_json(manifest)
    return {
        "places_lineage_generation": 2,
        "places_predecessor_family_manifest_key": (
            "slice-2026-07-19.6/families/places/family-manifest.json"
        ),
        "places_predecessor_family_manifest_bytes": len(encoded),
        "places_predecessor_family_manifest_sha256": hashlib.sha256(
            encoded
        ).hexdigest(),
    }


def completed_outputs(built, name):
    family_dir = built["tmp_path"] / name
    reduce_reports = [
        execute_reduce_job(
            built["plan"],
            job_index=job["index"],
            artifact_root=built["artifact_root"],
            scratch_dir=built["tmp_path"] / f"{name}-reduce-{job['index']}",
            output_dir=family_dir,
            fragment_reader=json_fragment_reader,
            runtime_provenance={"reader": "json", "writer": "fixture"},
        )
        for job in built["plan"]["reduce_jobs"]
    ]
    head_report = build_global_head(
        built["request"],
        built["plan"],
        reduce_reports=reduce_reports,
        artifact_root=family_dir,
        scratch_dir=built["tmp_path"] / f"{name}-head",
        output=family_dir / "head.phrp",
    )
    return family_dir, reduce_reports, head_report


def build_map_outputs(tmp_path, inventory, groups):
    artifact_root = tmp_path / "intermediates"

    def reader(_source, row_range):
        for row_group in range(
            row_range["first_row_group"], row_range["last_row_group"] + 1
        ):
            for index, row in enumerate(groups[row_group]):
                yield row_group, index, row

    reports = [
        run_map_task(
            inventory,
            task_index=index,
            output_dir=artifact_root,
            batch_reader=reader,
            fragment_writer=fake_fragment_writer,
            fragment_rows=100,
        )
        for index in range(len(groups))
    ]
    objects = []
    for report in reports:
        count = report["summary"]
        objects.append(
            {
                "object_key": count["object_key"],
                "bytes": count["bytes"],
                "sha256": count["sha256"],
            }
        )
        objects.extend(
            {
                "object_key": fragment["object_key"],
                "bytes": fragment["bytes"],
                "sha256": fragment["sha256"],
            }
            for fragment in report["fragments"]["objects"]
        )
    listing = {
        "schema": ARTIFACT_LISTING_SCHEMA,
        "objects": sorted(objects, key=lambda item: item["object_key"]),
    }
    return artifact_root, reports, listing


@pytest.fixture
def built(tmp_path):
    groups = fixture_rows()
    inventory = inventory_for_groups(groups)
    request = build_request(inventory)
    artifact_root, reports, listing = build_map_outputs(tmp_path, inventory, groups)
    plan = build_places_plan(
        request,
        inventory,
        list(reversed(reports)),
        artifact_root=artifact_root,
        artifact_listing=listing,
        scratch_dir=tmp_path / "scratch-plan",
    )
    return {
        "tmp_path": tmp_path,
        "groups": groups,
        "inventory": inventory,
        "request": request,
        "artifact_root": artifact_root,
        "reports": reports,
        "listing": listing,
        "plan": plan,
    }


def test_fanin_plan_is_exact_sticky_bounded_and_deterministic(built):
    plan = built["plan"]
    assert validate_places_plan(plan) is plan
    assert plan["totals"] == {
        "retained_records": 8,
        "leaves": 8,
        "split_cells": 4,
        "execution_groups": 4,
        "reduce_jobs": 2,
        "input_fragments": 2,
    }
    assert plan["map_fan_in"]["input_records"] == 8
    assert plan["map_fan_in"]["retained_records"] == 8
    assert plan["map_fan_in"]["rejected_records"] == 0
    aggregation = plan["map_fan_in"]["count_aggregation"]
    assert aggregation["maximum_scratch_bytes"] == places_plan.PLAN_MAX_SCRATCH_BYTES
    assert aggregation["kind"] == "duckdb-typed-external-fanin-v1"
    assert aggregation["engine"] == "duckdb"
    assert aggregation["maximum_memory_bytes"] == places_plan.PLAN_DUCKDB_MEMORY_LIMIT_BYTES
    assert aggregation["peak_scratch_bytes"] >= aggregation["peak_database_bytes"]
    assert aggregation["peak_batch_rows"] <= aggregation["maximum_batch_rows"]
    assert aggregation["registered_arrow_batches"] is True
    assert aggregation["arrow_append_batches"] > 0
    assert aggregation["group_aggregation"] == "typed-ordered-external-stream-v1"
    assert aggregation["maximum_execution_groups_in_memory"] == 256
    assert aggregation["ordered_scan"] == "duckdb-order-by-cell-v1"
    assert all(
        job["execution_identity_is_serving_identity"] is False
        for job in plan["reduce_jobs"]
    )
    assert sum(job["expected_records"] for job in plan["reduce_jobs"]) == 8
    assert {
        group for job in plan["reduce_jobs"] for group in job["execution_groups"]
    } == {
        "0000",
        "1111",
        "2222",
        "3333",
    }

    repeated = build_places_plan(
        built["request"],
        copy.deepcopy(built["inventory"]),
        built["reports"],
        artifact_root=built["artifact_root"],
        artifact_listing=copy.deepcopy(built["listing"]),
        scratch_dir=built["tmp_path"] / "scratch-repeat",
    )
    assert repeated == plan


def test_all_rejected_map_completion_restores_through_planner(tmp_path):
    groups = fixture_rows()
    for row in groups[1]:
        row["names"]["primary"] = " "
    inventory = inventory_for_groups(groups)
    request = build_request(inventory)
    artifact_root, reports, listing = build_map_outputs(tmp_path, inventory, groups)

    assert reports[1]["accounting"]["retained_records"] == 0
    assert reports[1]["fragments"] == {
        "count": 0,
        "records": 0,
        "bytes": 0,
        "manifest_sha256": digest_value([]),
        "objects": [],
    }
    plan = build_places_plan(
        request,
        inventory,
        reports,
        artifact_root=artifact_root,
        artifact_listing=listing,
        scratch_dir=tmp_path / "all-rejected-plan",
    )
    assert validate_places_plan(plan) is plan
    assert plan["map_fan_in"]["input_records"] == 8
    assert plan["map_fan_in"]["retained_records"] == 4
    assert plan["map_fan_in"]["rejected_records"] == 4


def test_fanin_rejects_missing_duplicate_and_remote_listing_replay(built):
    with pytest.raises(ValueError, match="missing Places map task"):
        build_places_plan(
            built["request"],
            built["inventory"],
            built["reports"][:1],
            artifact_root=built["artifact_root"],
            artifact_listing=built["listing"],
            scratch_dir=built["tmp_path"] / "scratch-missing",
        )
    with pytest.raises(ValueError, match="duplicate/replayed Places map task"):
        build_places_plan(
            built["request"],
            built["inventory"],
            [built["reports"][0], built["reports"][0]],
            artifact_root=built["artifact_root"],
            artifact_listing=built["listing"],
            scratch_dir=built["tmp_path"] / "scratch-duplicate",
        )
    listing = copy.deepcopy(built["listing"])
    listing["objects"].append(
        {"object_key": "unexpected/object", "bytes": 1, "sha256": "0" * 64}
    )
    with pytest.raises(ValueError, match="intermediate listing differs"):
        build_places_plan(
            built["request"],
            built["inventory"],
            built["reports"],
            artifact_root=built["artifact_root"],
            artifact_listing=listing,
            scratch_dir=built["tmp_path"] / "scratch-listing",
        )


def test_plan_rejects_task_famous_cap_below_requested_global_cap(
    built, monkeypatch
):
    monkeypatch.setattr(places_plan, "MAP_SUMMARY_FAMOUS_CAP", 1)
    with pytest.raises(ValueError, match="below the requested global cap"):
        build_places_plan(
            built["request"],
            built["inventory"],
            built["reports"],
            artifact_root=built["artifact_root"],
            artifact_listing=built["listing"],
            scratch_dir=built["tmp_path"] / "scratch-famous-cap",
        )


def test_planner_allows_famous_pending_peak_above_cap_but_below_batch(built):
    reports = copy.deepcopy(built["reports"])
    reports[0]["execution"]["census"]["peak_pending_famous_rows"] = (
        places_plan.MAP_SUMMARY_FAMOUS_CAP + 1
    )
    reports[0]["report_sha256"] = digest_value(
        {key: value for key, value in reports[0].items() if key != "report_sha256"}
    )
    plan = build_places_plan(
        built["request"],
        built["inventory"],
        reports,
        artifact_root=built["artifact_root"],
        artifact_listing=built["listing"],
        scratch_dir=built["tmp_path"] / "scratch-famous-pending-valid",
    )
    assert validate_places_plan(plan) is plan

    reports[0]["execution"]["census"]["peak_pending_famous_rows"] = (
        places_plan.MAP_CENSUS_BATCH_ROWS + 1
    )
    reports[0]["report_sha256"] = digest_value(
        {key: value for key, value in reports[0].items() if key != "report_sha256"}
    )
    with pytest.raises(ValueError, match="observed bound exceeds"):
        build_places_plan(
            built["request"],
            built["inventory"],
            reports,
            artifact_root=built["artifact_root"],
            artifact_listing=built["listing"],
            scratch_dir=built["tmp_path"] / "scratch-famous-pending-invalid",
        )


def test_skewed_execution_groups_assign_whole_groups_not_shard_ids():
    groups = {
        "0000": {
            "records": 100,
            "leaves": [{"cell": "000000", "rows": 100}],
            "fragments": [],
        },
        "1111": {
            "records": 10,
            "leaves": [{"cell": "111111", "rows": 10}],
            "fragments": [],
        },
        "2222": {
            "records": 9,
            "leaves": [{"cell": "222222", "rows": 9}],
            "fragments": [],
        },
        "3333": {
            "records": 8,
            "leaves": [{"cell": "333333", "rows": 8}],
            "fragments": [],
        },
    }
    jobs = _assign_reduce_jobs(
        groups=groups,
        reduce_job_limit=2,
        request_digest="1" * 64,
        inventory_sha256="2" * 64,
        completion_set_sha256="3" * 64,
    )
    assert [job["expected_records"] for job in jobs] == [100, 27]
    assert all(job["execution_identity_is_serving_identity"] is False for job in jobs)
    assert (
        _assign_reduce_jobs(
            groups=copy.deepcopy(groups),
            reduce_job_limit=2,
            request_digest="1" * 64,
            inventory_sha256="2" * 64,
            completion_set_sha256="3" * 64,
        )
        == jobs
    )


def test_diffuse_coarse_packs_charge_selected_work_not_repeated_whole_objects():
    groups = {}
    for group_index in range(256):
        group = "".join(
            str((group_index >> shift) & 3) for shift in (6, 4, 2, 0)
        )
        fragments = []
        for map_index in range(128):
            fragments.append(
                {
                    "object_key": f"fragments/sha256/pack-{map_index}.parquet",
                    "sha256": f"{map_index + 1:064x}",
                    "bytes": 256_000_000,
                    "footer_bytes": 64_000,
                    "row_group_count": 256,
                    "map_index": map_index,
                    "pack_index": 0,
                    "selected_row_groups": [group_index],
                    "selected_execution_groups": [group],
                    "records": 1,
                    "selected_compressed_bytes": 1_000_000,
                    "selected_uncompressed_bytes": 2_000_000,
                }
            )
        groups[group] = {
            "records": 128,
            "leaves": [{"cell": group + "00", "rows": 128}],
            "fragments": fragments,
        }

    jobs = _assign_reduce_jobs(
        groups=groups,
        reduce_job_limit=82,
        request_digest="1" * 64,
        inventory_sha256="2" * 64,
        completion_set_sha256="3" * 64,
    )

    assert len(jobs) == 82
    assert max(job["input_bytes"] for job in jobs) < 1_000_000_000
    assert max(job["selected_uncompressed_bytes"] for job in jobs) <= 1_024_000_000
    assert max(job["maximum_materialized_bytes"] for job in jobs) == 256_000_000
    assert max(job["input_fragment_count"] for job in jobs) == 128


def test_reduce_head_and_finalize_reconcile_to_worker_artifacts(built):
    family_dir = built["tmp_path"] / "family"
    reduce_reports = []
    for job in built["plan"]["reduce_jobs"]:
        report = execute_reduce_job(
            built["plan"],
            job_index=job["index"],
            artifact_root=built["artifact_root"],
            scratch_dir=built["tmp_path"] / f"scratch-reduce-{job['index']}",
            output_dir=family_dir,
            fragment_reader=json_fragment_reader,
            runtime_provenance={"reader": "hermetic-json-v1", "writer": "fixture"},
        )
        assert validate_reduce_report(report, built["plan"]) is report
        assert (
            report["compaction"]["peak_workspace_bytes"]
            >= report["compaction"]["peak_scratch_bytes"]
        )
        assert report["compaction"]["writer_materialization"]["peak_leaf_rows"] == 1
        assert (
            report["compaction"]["writer_materialization"]["kind"]
            == "duckdb-typed-external-streaming-pcsh-writer-v2"
        )
        assert report["compaction"]["maximum_active_leaf_partitions"] == 1
        assert report["compaction"]["registered_arrow_batches"] is True
        assert report["compaction"]["arrow_append_batches"] > 0
        assert report["compaction"]["peak_active_leaf_partitions"] == 1
        assert report["head_candidates"]["writer"]["full_table_materialized"] is False
        assert (
            report["compaction"]["writer_materialization"][
                "registered_arrow_batches"
            ]
            is True
        )
        assert (
            report["compaction"]["writer_materialization"]["arrow_append_batches"]
            > 0
        )
        assert (
            report["head_candidates"]["writer"]["peak_batch_rows"]
            <= places_plan.HEAD_CANDIDATE_WRITE_BATCH_ROWS
        )
        excessive = copy.deepcopy(report)
        excessive["compaction"]["peak_workspace_bytes"] = (
            excessive["compaction"]["maximum_workspace_bytes"] + 1
        )
        excessive["report_sha256"] = digest_value(
            {key: value for key, value in excessive.items() if key != "report_sha256"}
        )
        with pytest.raises(ValueError, match="compaction caps/provenance"):
            validate_reduce_report(excessive, built["plan"])
        reduce_reports.append(report)
    assert sum(report["accounting"]["output_records"] for report in reduce_reports) == 8
    assert sum(len(report["shards"]) for report in reduce_reports) == 8
    assert all(
        (family_dir / shard["object"]).read_bytes().startswith(b"PCSH0001")
        for report in reduce_reports
        for shard in report["shards"]
    )


    head_report = build_global_head(
        built["request"],
        built["plan"],
        reduce_reports=reduce_reports,
        artifact_root=family_dir,
        scratch_dir=built["tmp_path"] / "scratch-head",
        output=family_dir / "head.phrp",
    )
    assert (
        validate_head_report(head_report, built["request"], built["plan"])
        is head_report
    )
    assert head_report["accounting"]["map_fragment_reads"] == 0
    assert head_report["accounting"]["candidate_artifacts"] == len(reduce_reports)
    assert (
        head_report["usage"]["retained_candidate_bytes"]
        <= head_report["usage"]["peak_retained_candidate_bytes"]
        <= places_head.MAX_HEAD_RETAINED_CANDIDATE_BYTES
    )
    assert (
        head_report["candidate_materialization"]["peak_read_batch_bytes"]
        <= places_head.MAX_HEAD_CANDIDATE_READ_BATCH_BYTES
    )
    head = RepackHead(family_dir / "head.phrp")
    assert list(head.load_resident_index()) == [
        item["key"] for item in built["plan"]["head_admission"]["keys"]
    ]
    assert head.directory["provenance"] == {
        "request_sha256": built["plan"]["request"]["sha256"],
        "plan_sha256": built["plan"]["plan_sha256"],
        "head_policy_sha256": digest_value(
            built["request"]["families"]["places"]["global_head"]
        ),
        "lineage_generation": 1,
        "predecessor_family_manifest_sha256": None,
        "predecessor_family_manifest": {
            "object_key": None,
            "bytes": None,
            "sha256": None,
        },
    }
    assert head.query_resident("e:coffee")["hit"] is True

    final_report, family_manifest = finalize_places_family(
        built["request"],
        built["plan"],
        list(reversed(reduce_reports)),
        head_report,
        output_dir=family_dir,
    )
    assert final_report["accounting"] == {
        "map_retained_records": 8,
        "planned_leaf_records": 8,
        "reduced_records": 8,
        "final_shard_records": 8,
        "final_shards": 8,
        "reduce_jobs": 2,
    }
    assert final_report["lineage_generation"] == 1
    assert final_report["catalog"]["partition"]["lineage_generation"] == 1
    assert (
        places_builder._read_catalog_payload(family_dir / "catalog.pcat")["partition"][
            "lineage_generation"
        ]
        == 1
    )
    assert (family_dir / "catalog.pcat").read_bytes().startswith(b"PCAT0001")
    assert (
        global_build_manifest.validate_family_manifest(family_manifest)
        is family_manifest
    )
    assert {item["object_key"] for item in family_manifest["artifacts"]} == {
        "families/places/catalog.pcat",
        "families/places/head.phrp",
        *{f"families/places/q-{leaf['cell']}.pcsh" for leaf in built["plan"]["leaves"]},
    }

    repeated_head_path = built["tmp_path"] / "head-repeat" / "head.phrp"
    second_head = build_global_head(
        built["request"],
        built["plan"],
        reduce_reports=reduce_reports,
        artifact_root=family_dir,
        scratch_dir=built["tmp_path"] / "scratch-head-repeat",
        output=repeated_head_path,
    )
    assert second_head["artifact"] == head_report["artifact"]
    assert repeated_head_path.read_bytes() == (family_dir / "head.phrp").read_bytes()


def test_duckdb_streaming_pcsh_is_byte_identical_across_boundaries(
    tmp_path, monkeypatch
):
    normalized = []
    for index, confidence in enumerate((0.9, 0.5, 0.9), start=1):
        row, reason = project_row(
            row_for_cell(index, "000000000000", confidence),
            maximum_level=12,
            source_uri="s3://fixture/places.parquet",
            row_group=0,
            row_index=index - 1,
        )
        assert reason is None
        row["primary_name"] = "東京 咖啡 " + " ".join(
            f"token{token}" for token in range(270)
        )
        normalized.append(row)
    duplicate = {**normalized[0], "source_uri": "s3://fixture/duplicate.parquet"}
    normalized.append(duplicate)
    normalized.sort(
        key=lambda row: (
            row["partition_cell"],
            row["partition_key"],
            -round(row["confidence"] * 255),
            row["gers_id"],
            row["source_uri"],
            row["source_row_group"],
            row["source_row_index"],
        )
    )
    expected = tmp_path / "expected.pcsh"
    actual = tmp_path / "actual.pcsh"
    build_artifact(
        [place_from_row(row, index + 1) for index, row in enumerate(normalized)],
        expected,
        block_entries=2,
        preserve_input_order=True,
    )
    monkeypatch.setattr(places_reduce, "REDUCE_MAX_BUFFER_ROWS", 1)
    evidence = places_reduce._build_streaming_artifact(
        iter(normalized),
        actual,
        scratch_dir=tmp_path / "pcsh-scratch",
        on_place=lambda _row, _place: None,
        block_entries=2,
    )
    assert actual.read_bytes() == expected.read_bytes()
    assert evidence["peak_pending_postings"] <= places_plan.REDUCE_MAX_BUFFER_ROWS * 4
    assert evidence["peak_pending_projections"] <= places_plan.REDUCE_MAX_BUFFER_ROWS


def test_duckdb_leaf_store_is_typed_without_json_payload(tmp_path):
    store = places_reduce._LeafStore(tmp_path / "typed-leaf-store")
    try:
        columns = {
            row[1]: row[2]
            for row in store.connection.execute("PRAGMA table_info('rows')").fetchall()
        }
        assert "payload" not in columns
        assert columns["partition_key"] == "UBIGINT"
        assert columns["confidence"] == "DOUBLE"
        assert columns["source_row_index"] == "BIGINT"
        assert store.duckdb_version
    finally:
        store.close()


def test_duckdb_runtime_version_is_enforced_at_constructor(tmp_path, monkeypatch):
    import duckdb

    monkeypatch.setattr(duckdb, "__version__", "0.0.0")
    with pytest.raises(RuntimeError, match="requires DuckDB 1.5.1"):
        places_plan._CountStore(tmp_path / "wrong-plan-duckdb")
    with pytest.raises(RuntimeError, match="requires DuckDB 1.5.1"):
        places_reduce._LeafStore(tmp_path / "wrong-reduce-duckdb")


def test_unmatched_place_does_not_apply_head_entry_cap(monkeypatch):
    matcher = places_reduce._AdmittedHeadMatcher(
        {"head_admission": {"keys": [{"key": "e:admitted"}]}}
    )
    row = {
        "confidence": 1.0,
        "partition_key": 0,
        "gers_id": str(uuid.UUID(int=1)),
        "source_uri": "source",
        "source_row_group": 0,
        "source_row_index": 0,
    }
    place = place_from_row(
        {
            "gers_id": row["gers_id"],
            "primary_name": "unmatched " + "x" * 200_000,
            "confidence": 1.0,
        },
        1,
    )
    monkeypatch.setattr(
        places_reduce,
        "encode_record",
        lambda _place: (_ for _ in ()).throw(AssertionError("must not encode")),
    )
    candidates = {}
    matcher.add(candidates, row, place)
    assert candidates == {}


def test_duplicate_gers_candidates_preserve_pcsh_but_dedupe_phrp_topk():
    first = (-255, 1, -255, "same-gers", "a", 0, 0)
    later = (-200, 2, -200, "same-gers", "b", 0, 1)
    reducer_candidates = {}
    assert places_reduce._push_candidate(
        reducer_candidates, "e:test", later, b"later"
    ) == len(b"later")
    assert places_reduce._push_candidate(
        reducer_candidates, "e:test", first, b"first"
    ) == 0
    assert reducer_candidates["e:test"] == [(first, b"first")]

    head_candidates = {}
    retained = places_head._push_candidate(
        head_candidates, "e:test", later, b"later", 10
    )
    retained += places_head._push_candidate(
        head_candidates, "e:test", first, b"first", 10
    )
    assert head_candidates["e:test"] == [(first, b"first")]
    assert retained == places_head._retained_candidate_bytes(
        "e:test", first, b"first"
    )
    for rank in range(20):
        sort_key = (-rank, rank, -rank, f"gers-{rank}", "source", 0, rank)
        places_head._push_candidate(
            head_candidates, "e:boundary", sort_key, bytes((rank,)), 10
        )
    assert len(head_candidates["e:boundary"]) == 10
    assert sorted(item[0][1] for item in head_candidates["e:boundary"]) == list(
        range(10, 20)
    )


def test_head_candidate_parquet_writer_flushes_bounded_batches(tmp_path):
    candidate_count = places_plan.HEAD_CANDIDATE_WRITE_BATCH_ROWS + 1
    candidates = {
        f"e:key-{index:05d}": [
            ((-255, index, -255, f"id-{index}", "source", 0, index), b"x")
        ]
        for index in range(candidate_count)
    }
    artifact = places_reduce._write_head_candidates(
        candidates,
        output_dir=tmp_path,
        job_index=0,
        plan_sha256="1" * 64,
        admission_sha256="2" * 64,
        maximum_candidates=candidate_count,
        candidate_projection_bytes=candidate_count,
        peak_candidate_projection_bytes=candidate_count,
    )
    assert artifact["candidates"] == candidate_count
    writer = artifact["writer"]
    assert writer["kind"] == "pyarrow-parquet-writer-batches-v1"
    assert writer["maximum_batch_rows"] == places_plan.HEAD_CANDIDATE_WRITE_BATCH_ROWS
    assert writer["peak_batch_rows"] == places_plan.HEAD_CANDIDATE_WRITE_BATCH_ROWS
    assert writer["maximum_batch_bytes"] == places_plan.HEAD_CANDIDATE_WRITE_BATCH_BYTES
    assert writer["maximum_row_bytes"] == places_plan.HEAD_CANDIDATE_MAX_ROW_BYTES
    assert writer["peak_batch_bytes"] <= writer["maximum_batch_bytes"]
    assert (
        writer["maximum_projection_bytes"]
        == places_reduce.MAX_HEAD_SINGLE_PROJECTION_BYTES
    )
    assert writer["peak_projection_bytes"] == 1
    assert writer["full_table_materialized"] is False
    import pyarrow.parquet as pq

    assert pq.ParquetFile(tmp_path / artifact["object_key"]).metadata.num_row_groups == 2


def test_exact_predecessor_is_required_after_build_one(built):
    family_dir = built["tmp_path"] / "predecessor-family"
    reduce_reports = [
        execute_reduce_job(
            built["plan"],
            job_index=job["index"],
            artifact_root=built["artifact_root"],
            scratch_dir=built["tmp_path"] / f"pred-reduce-{job['index']}",
            output_dir=family_dir,
            fragment_reader=json_fragment_reader,
            runtime_provenance={"reader": "json", "writer": "fixture"},
        )
        for job in built["plan"]["reduce_jobs"]
    ]
    head_report = build_global_head(
        built["request"],
        built["plan"],
        reduce_reports=reduce_reports,
        artifact_root=family_dir,
        scratch_dir=built["tmp_path"] / "pred-head",
        output=family_dir / "head.phrp",
    )
    _, manifest = finalize_places_family(
        built["request"],
        built["plan"],
        reduce_reports,
        head_report,
        output_dir=family_dir,
    )
    next_request = build_request(
        built["inventory"],
        **predecessor_request_fields(manifest),
    )
    next_plan = build_places_plan(
        next_request,
        built["inventory"],
        built["reports"],
        artifact_root=built["artifact_root"],
        artifact_listing=built["listing"],
        scratch_dir=built["tmp_path"] / "next-plan",
        predecessor_family_manifest=manifest,
        predecessor_catalog=family_dir / "catalog.pcat",
    )
    assert (
        next_plan["partition"]["split_cells"]
        == built["plan"]["partition"]["split_cells"]
    )
    with pytest.raises(ValueError, match="requires manifest and catalog"):
        build_places_plan(
            next_request,
            built["inventory"],
            built["reports"],
            artifact_root=built["artifact_root"],
            artifact_listing=built["listing"],
            scratch_dir=built["tmp_path"] / "missing-predecessor",
        )


def test_predecessor_growth_row_cap_and_tokenizer_rotation_retain_splits(built):
    catalog, manifest, split_cell = predecessor_catalog(
        built["tmp_path"] / "compatible-predecessor",
        maximum_level=11,
        row_cap=1,
        tokenizer="previous-tokenizer-v9",
    )
    request = build_request(
        built["inventory"],
        **predecessor_request_fields(manifest),
        places_maximum_level=12,
        places_split_row_cap=10_000,
    )

    assert _load_predecessor_splits(request, manifest, catalog) == [split_cell]


def test_later_generation_cannot_bootstrap_without_a_predecessor(built):
    request = copy.deepcopy(built["request"])
    request["families"]["places"]["partition"]["lineage_generation"] = 2

    with pytest.raises(ValueError, match="requires an exact predecessor"):
        _load_predecessor_splits(request, None, None)


@pytest.mark.parametrize(
    ("current_generation", "predecessor_generation"),
    [(3, 1), (2, 2)],
    ids=["skipped-generation", "replayed-generation"],
)
def test_predecessor_generation_must_be_exactly_current_minus_one(
    built, current_generation, predecessor_generation
):
    catalog, manifest, _ = predecessor_catalog(
        built["tmp_path"] / f"lineage-{current_generation}-{predecessor_generation}",
        lineage_generation=predecessor_generation,
    )
    identity = predecessor_request_fields(manifest)
    identity["places_lineage_generation"] = current_generation
    request = build_request(
        built["inventory"],
        **identity,
    )

    with pytest.raises(ValueError, match="partition lineage/contract is incompatible"):
        _load_predecessor_splits(request, manifest, catalog)


def test_continuation_rejects_predecessor_catalog_without_lineage_generation(built):
    catalog, manifest, _ = predecessor_catalog(
        built["tmp_path"] / "missing-lineage-generation",
        lineage_generation=None,
    )
    request = build_request(
        built["inventory"],
        **predecessor_request_fields(manifest),
    )

    with pytest.raises(ValueError, match="partition lineage/contract is incompatible"):
        _load_predecessor_splits(request, manifest, catalog)


@pytest.mark.parametrize("invalid_generation", [True, 1.0])
def test_continuation_rejects_non_integer_catalog_generation(built, invalid_generation):
    catalog, manifest, _ = predecessor_catalog(
        built["tmp_path"] / f"invalid-lineage-{invalid_generation!r}",
        lineage_generation=invalid_generation,
    )
    request = build_request(
        built["inventory"],
        **predecessor_request_fields(manifest),
    )

    with pytest.raises(ValueError, match="partition lineage/contract is incompatible"):
        _load_predecessor_splits(request, manifest, catalog)


@pytest.mark.parametrize(
    ("request_overrides", "catalog_overrides"),
    [
        ({"places_maximum_level": 10}, {}),
        ({"places_minimum_level": 7}, {}),
        ({}, {"scheme": "world-quadkey-v2"}),
    ],
    ids=["maximum-level-decrease", "minimum-level-drift", "partition-scheme-drift"],
)
def test_predecessor_rejects_partition_contract_drift(
    built, request_overrides, catalog_overrides
):
    catalog, manifest, _ = predecessor_catalog(
        built["tmp_path"]
        / f"incompatible-{next(iter(request_overrides or catalog_overrides))}",
        **catalog_overrides,
    )
    request = build_request(
        built["inventory"],
        **predecessor_request_fields(manifest),
        **request_overrides,
    )

    with pytest.raises(ValueError, match="partition lineage/contract is incompatible"):
        _load_predecessor_splits(request, manifest, catalog)


def test_predecessor_catalog_requires_exact_manifest_bytes_and_sha(built):
    catalog, manifest, _ = predecessor_catalog(built["tmp_path"] / "identity-mismatch")
    request = build_request(
        built["inventory"],
        **predecessor_request_fields(manifest),
    )
    catalog.write_bytes(catalog.read_bytes() + b"corrupt")

    with pytest.raises(ValueError, match="differs from its manifest identity"):
        _load_predecessor_splits(request, manifest, catalog)


def test_predecessor_manifest_requires_exact_pinned_object_sha(built):
    catalog, manifest, _ = predecessor_catalog(
        built["tmp_path"] / "manifest-identity-mismatch"
    )
    identity = predecessor_request_fields(manifest)
    identity["places_predecessor_family_manifest_sha256"] = "f" * 64
    request = build_request(built["inventory"], **identity)

    with pytest.raises(ValueError, match="manifest differs from its pinned identity"):
        _load_predecessor_splits(request, manifest, catalog)


@pytest.mark.parametrize(
    ("constant", "limit_field", "new_limit"),
    [
        ("MAX_INPUT_FRAGMENTS_PER_REDUCE_JOB", "max_input_fragments_per_reduce_job", 0),
        ("MAX_INPUT_BYTES_PER_REDUCE_JOB", "max_input_bytes_per_reduce_job", 1),
        (
            "MAX_SELECTED_UNCOMPRESSED_BYTES_PER_REDUCE_JOB",
            "max_selected_uncompressed_bytes_per_reduce_job",
            1,
        ),
        (
            "MAX_SIMULTANEOUS_MATERIALIZED_BYTES",
            "max_simultaneous_materialized_bytes",
            1,
        ),
        ("MAX_RETAINED_ROWS_PER_REDUCE_JOB", "max_retained_rows_per_reduce_job", 3),
        (
            "MAX_RAW_FRAGMENTS_PER_EXECUTION_GROUP",
            "max_raw_fragments_per_execution_group",
            0,
        ),
    ],
    ids=[
        "fragment-count", "input-bytes", "uncompressed-bytes",
        "simultaneous-materialization", "retained-rows", "group-fanin",
    ],
)
def test_serialized_plan_reenforces_reduce_caps(
    built, monkeypatch, constant, limit_field, new_limit
):
    tampered = copy.deepcopy(built["plan"])
    monkeypatch.setattr(places_plan, constant, new_limit)
    tampered["limits"][limit_field] = new_limit
    tampered["plan_sha256"] = digest_value(
        {key: value for key, value in tampered.items() if key != "plan_sha256"}
    )

    with pytest.raises(ValueError, match="serialized executor cap"):
        validate_places_plan(tampered)


def test_plan_rejects_tampered_count_aggregation_evidence(built):
    tampered = copy.deepcopy(built["plan"])
    tampered["map_fan_in"]["count_aggregation"]["peak_database_bytes"] = (
        places_plan.PLAN_MAX_SCRATCH_BYTES + 1
    )
    tampered["plan_sha256"] = digest_value(
        {key: value for key, value in tampered.items() if key != "plan_sha256"}
    )
    with pytest.raises(ValueError, match="count aggregation disk evidence"):
        validate_places_plan(tampered)


def test_planner_count_store_enforces_duckdb_scratch_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(places_plan, "PLAN_MAX_SCRATCH_BYTES", 64 * 1024)
    store = places_plan._CountStore(tmp_path / "planner-count-cap")
    try:
        values = [(f"{index:012x}".replace("a", "0"), 1) for index in range(10_000)]
        with pytest.raises(ValueError, match="hard scratch cap"):
            store._add(values)
    finally:
        store.close()


def test_planner_group_totals_stream_typed_external_order(tmp_path):
    store = places_plan._CountStore(tmp_path / "planner-group-stream")
    try:
        store._add(
            [
                ("000000000000", 2),
                ("000011111111", 3),
                ("123000000000", 5),
                ("333333333333", 7),
            ]
        )
        store.finish()
        plan_details = store.ordered_query_plan()
        assert plan_details
        assert any("ORDER_BY" in detail for detail in plan_details)
        assert store.group_totals() == {"0000": 5, "1230": 5, "3333": 7}
        evidence = store.evidence()
        assert evidence["group_aggregation"] == "typed-ordered-external-stream-v1"
        assert evidence["maximum_execution_groups_in_memory"] == 4**4
        assert evidence["maximum_memory_bytes"] == places_plan.PLAN_DUCKDB_MEMORY_LIMIT_BYTES
        assert evidence["peak_batch_rows"] <= places_plan.PLAN_AGGREGATION_BATCH_ROWS
        assert evidence["peak_scratch_bytes"] <= places_plan.PLAN_MAX_SCRATCH_BYTES
    finally:
        store.close()


def test_planner_group_totals_enforces_accumulator_cap(tmp_path):
    store = places_plan._CountStore(tmp_path / "planner-group-cap")
    try:
        store._add([(f"{index:04x}" + "0" * 8, 1) for index in range(257)])
        with pytest.raises(ValueError, match="accumulator exceeded its cap"):
            store.group_totals()
    finally:
        store.close()


def test_reduce_store_observes_staged_bytes_during_flush(tmp_path, monkeypatch):
    active_bytes = 0
    limit = places_reduce.REDUCE_MAX_WORKSPACE_BYTES

    def observe(scratch_bytes):
        if scratch_bytes + active_bytes > limit:
            raise ValueError("combined cap crossed")

    store = places_reduce._LeafStore(tmp_path / "reduce-combined", observe)
    try:
        initial = store.observe_scratch()
        active_bytes = 1_000
        limit = initial + active_bytes
        monkeypatch.setattr(places_reduce, "REDUCE_MAX_BUFFER_ROWS", 1)
        row = {
            "partition_cell": "0" * 12,
            "partition_key": 1,
            "confidence": 0.5,
            "gers_id": "a" * 36,
            "primary_name": "Place",
            "alt_names": "",
            "brand_name": "",
            "category_primary": "",
            "basic_category": "",
            "locality": "",
            "region": "",
            "country": "",
            "lat": 0.0,
            "lon": 0.0,
            "operating_status": "open",
            "execution_group": "0000",
            "source_uri": "s" * 200_000,
            "source_row_group": 0,
            "source_row_index": 0,
        }
        with pytest.raises(ValueError, match="combined cap crossed"):
            store.add("000000", row)
    finally:
        store.close()


def test_reduce_preflights_pcsh_output_before_allocation(built, monkeypatch):
    monkeypatch.setattr(places_reduce, "REDUCE_MAX_WORKSPACE_BYTES", 10_000_000)
    with pytest.raises(ValueError, match="conservative output bound"):
        execute_reduce_job(
            built["plan"],
            job_index=0,
            artifact_root=built["artifact_root"],
            scratch_dir=built["tmp_path"] / "reduce-preflight-scratch",
            output_dir=built["tmp_path"] / "reduce-preflight-output",
            fragment_reader=json_fragment_reader,
            runtime_provenance={"reader": "json", "writer": "fixture"},
        )


def test_reduce_streams_remote_fragments_one_at_a_time_and_unlinks(built, monkeypatch):
    job = built["plan"]["reduce_jobs"][0]
    scratch = built["tmp_path"] / "remote-reduce-scratch"
    observed_outputs = []

    def fake_fetch(argv, check):
        assert check is True
        object_key, output = argv[1:3]
        assert all(not path.exists() for path in observed_outputs)
        output_path = Path(output)
        assert not output_path.exists()
        output_path.write_bytes((built["artifact_root"] / object_key).read_bytes())
        if "--proof" in argv:
            Path(argv[argv.index("--proof") + 1]).write_text("{}")
        observed_outputs.append(output_path)

    monkeypatch.setattr(places_reduce.subprocess, "run", fake_fetch)
    monkeypatch.setattr(places_reduce, "_validate_selective_proof", lambda *_: None)
    report = execute_reduce_job(
        built["plan"],
        job_index=job["index"],
        artifact_root=built["tmp_path"] / "empty-remote-root",
        scratch_dir=scratch,
        output_dir=built["tmp_path"] / "remote-family",
        fragment_fetch_command=["fixture-fetch", "{object_key}", "{output}"],
        fragment_reader=json_fragment_reader,
        runtime_provenance={"reader": "json", "writer": "fixture"},
    )

    assert validate_reduce_report(report, built["plan"]) is report
    materialization = report["compaction"]["fragment_materialization"]
    assert materialization["fetched_fragments"] == len(job["input_fragments"])
    assert materialization["fetched_bytes"] == sum(
        item["bytes"] for item in job["input_fragments"]
    )
    assert materialization["maximum_simultaneously_materialized_fragments"] == 1
    assert materialization["peak_materialized_fragment_bytes"] == max(
        item["bytes"] for item in job["input_fragments"]
    )
    assert all(not path.exists() for path in observed_outputs)


def test_materialization_validators_reject_fabricated_remote_zero_evidence(built):
    family_dir, reduce_reports, head_report = completed_outputs(built, "zero-evidence")
    reduce_report = copy.deepcopy(reduce_reports[0])
    reduce_report["compaction"]["fragment_materialization"]["remote_fetch_enabled"] = (
        True
    )
    reduce_report["report_sha256"] = digest_value(
        {key: value for key, value in reduce_report.items() if key != "report_sha256"}
    )
    with pytest.raises(ValueError, match="materialization evidence"):
        validate_reduce_report(reduce_report, built["plan"])

    fabricated_head = copy.deepcopy(head_report)
    fabricated_head["candidate_materialization"]["remote_fetch_enabled"] = True
    fabricated_head["report_sha256"] = digest_value(
        {key: value for key, value in fabricated_head.items() if key != "report_sha256"}
    )
    with pytest.raises(ValueError, match="materialization evidence"):
        validate_head_report(fabricated_head, built["request"], built["plan"])

    final_report, _ = finalize_places_family(
        built["request"],
        built["plan"],
        reduce_reports,
        head_report,
        output_dir=family_dir,
    )
    fabricated_final = copy.deepcopy(final_report)
    fabricated_final["artifact_materialization"]["remote_fetch_enabled"] = True
    fabricated_final["report_sha256"] = digest_value(
        {
            key: value
            for key, value in fabricated_final.items()
            if key != "report_sha256"
        }
    )
    with pytest.raises(ValueError, match="materialization evidence"):
        validate_places_final_report(fabricated_final, built["request"], built["plan"])


def test_remote_fetch_adapter_requires_safe_no_shell_placeholders(built):
    with pytest.raises(ValueError, match=r"one \{output\}"):
        execute_reduce_job(
            built["plan"],
            job_index=0,
            artifact_root=built["artifact_root"],
            scratch_dir=built["tmp_path"] / "invalid-fetch-scratch",
            output_dir=built["tmp_path"] / "invalid-fetch-output",
            fragment_fetch_command=["fetch", "{object_key}"],
            fragment_reader=json_fragment_reader,
            runtime_provenance={"reader": "json"},
        )


def test_head_admission_query_is_bounded_and_page_evidence_is_observed(tmp_path):
    counts = places_head._CountDatabase(tmp_path / "head-counts")
    try:
        counts.add_counts("token_counts", {"a": 2, "b": 2, "c": 2})
        counts.finish()
        with pytest.raises(ValueError, match="admitted head keys exceed"):
            counts.admitted_values("token_counts", 2, 2)
        assert counts.peak_database_pages > 0
        assert counts.peak_database_bytes <= places_head.MAX_HEAD_SCRATCH_BYTES
    finally:
        counts.close()


def test_head_count_database_observes_staged_bytes_during_growth(tmp_path):
    active_bytes = 0
    limit = places_head.MAX_HEAD_SCRATCH_BYTES

    def observe(scratch_bytes):
        if scratch_bytes + active_bytes > limit:
            raise ValueError("combined head cap crossed")

    counts = places_head._CountDatabase(tmp_path / "head-combined", observe)
    try:
        initial = counts.observe_scratch()
        active_bytes = 1_000
        limit = initial + active_bytes
        values = {f"token-{index}-{'x' * 1000}": 1 for index in range(100)}
        with pytest.raises(ValueError, match="combined head cap crossed"):
            counts.add_counts("token_counts", values)
    finally:
        counts.close()


def test_head_admission_proves_key_and_encoded_index_caps(built):
    admission = built["plan"]["head_admission"]
    assert (
        admission["duplicate_gers_policy"]
        == places_plan.HEAD_DUPLICATE_GERS_POLICY
    )
    assert admission["proof"]["key_count"] <= places_head.READER_MAX_HEAD_KEYS
    assert (
        admission["proof"]["encoded_index_upper_bound_bytes"]
        <= places_head.READER_MAX_HEAD_INDEX_BYTES
    )
    assert admission["artifact"]["object_key"] == "head-admission.json"
    assert admission["proof"]["encoded_index_upper_bound_bytes"] == len(
        places_head.encode_key_index(
            [
                (
                    item["key"],
                    places_plan.HEAD_MAX_ENTRIES_BYTES,
                    places_head.READER_MAX_HEAD_ENTRY_BYTES,
                )
                for item in admission["keys"]
            ]
        )
    )

    tampered = copy.deepcopy(built["plan"])
    tampered["head_admission"]["proof"]["encoded_index_upper_bound_bytes"] -= 1
    document = {
        key: value
        for key, value in tampered["head_admission"].items()
        if key != "artifact"
    }
    encoded = canonical_json_bytes(document) + b"\n"
    tampered["head_admission"]["artifact"]["bytes"] = len(encoded)
    tampered["head_admission"]["artifact"]["sha256"] = hashlib.sha256(encoded).hexdigest()
    tampered["plan_sha256"] = digest_value(
        {key: value for key, value in tampered.items() if key != "plan_sha256"}
    )
    with pytest.raises(ValueError, match="cap proof"):
        validate_places_plan(tampered)


def test_global_famous_cap_dedupes_gers_before_concentrated_topk(built):
    store = places_plan._CountStore(built["tmp_path"] / "famous-global-dedupe")
    try:
        rows = []
        for index, (identifier, confidence, name) in enumerate(
            (
                (1, 0.99, "best"),
                (1, 0.98, "duplicateeviction"),
                (2, 0.97, "second"),
            )
        ):
            row, reason = project_row(
                row_for_cell(identifier, "000000000000", confidence),
                maximum_level=12,
                source_uri=f"s3://fixture/{index}.parquet",
                row_group=0,
                row_index=index,
            )
            assert reason is None
            row["primary_name"] = name
            rows.append(row)
        store.famous_rows = rows
        admission = places_plan._build_head_admission(
            store,
            {**built["request"]["families"]["places"]["global_head"], "famous_cap": 2},
            output=built["tmp_path"] / "famous-global-admission.json",
        )
        keys = {item["key"] for item in admission["keys"]}
        assert "e:best" in keys
        assert "e:second" in keys
        assert "e:duplicateeviction" not in keys
    finally:
        store.close()


def test_ci_pins_places_duckdb_runtime():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    assert "requirements-hosted-rowgroup.txt" in workflow


def test_head_object_keeps_ranked_prefix_that_fits_entry_cap(tmp_path):
    projection = b"x" * 70_000
    report = places_head._write_head_object(
        {
            "e:test": [
                ((rank,), projection)
                for rank in range(10)
            ]
        },
        tmp_path / "bounded-head.phrp",
        famous_cap=0,
        existing_scratch_bytes=0,
        durable_provenance={},
    )

    assert report["candidate_slots_emitted"] == 1
    assert report["candidate_slots_dropped_for_entry_cap"] == 9
    assert report["entry_size_distribution"]["max"] <= (
        places_head.READER_MAX_HEAD_ENTRY_BYTES
    )


def test_head_object_preflight_rejects_workspace_crossing(tmp_path, monkeypatch):
    monkeypatch.setattr(places_head, "MAX_HEAD_SCRATCH_BYTES", 1_000)
    output = tmp_path / "preflight-head" / "head.phrp"
    with pytest.raises(ValueError, match="conservative object bound"):
        places_head._write_head_object(
            {"e:test": [((0,), b"projection")]},
            output,
            famous_cap=0,
            existing_scratch_bytes=0,
            durable_provenance={},
        )
    assert not output.exists()


def test_head_streams_remote_candidate_artifacts_once_without_map_fragments(
    built, monkeypatch
):
    family_dir, reduce_reports, _ = completed_outputs(built, "remote-candidates-source")
    observed_outputs = []

    def fake_fetch(argv, check):
        assert check is True
        object_key, output = argv[1:]
        assert all(not path.exists() for path in observed_outputs)
        output_path = Path(output)
        output_path.write_bytes((family_dir / object_key).read_bytes())
        observed_outputs.append(output_path)

    monkeypatch.setattr(places_head.subprocess, "run", fake_fetch)
    report = build_global_head(
        built["request"],
        built["plan"],
        reduce_reports=reduce_reports,
        artifact_root=built["tmp_path"] / "empty-head-remote-root",
        scratch_dir=built["tmp_path"] / "remote-head-scratch",
        output=built["tmp_path"] / "remote-head" / "head.phrp",
        fragment_fetch_command=["fixture-fetch", "{object_key}", "{output}"],
    )

    assert validate_head_report(report, built["request"], built["plan"]) is report
    materialization = report["candidate_materialization"]
    assert (
        materialization["fetched_artifacts"]
        == len(built["plan"]["reduce_jobs"])
    )
    assert materialization["map_fragments_opened"] == 0
    assert all(not path.exists() for path in observed_outputs)


def test_finalizer_rejects_self_consistent_but_corrupt_pcsh(built):
    family_dir, reduce_reports, head_report = completed_outputs(built, "corrupt-pcsh")
    shard = reduce_reports[0]["shards"][0]
    path = family_dir / shard["object"]
    encoded = path.read_bytes()
    assert b'"record_count":1' in encoded
    path.write_bytes(encoded.replace(b'"record_count":1', b'"record_count":2', 1))
    shard["sha256"], shard["bytes"] = sha256_file(path)
    reduce_reports[0]["report_sha256"] = digest_value(
        {
            key: value
            for key, value in reduce_reports[0].items()
            if key != "report_sha256"
        }
    )

    with pytest.raises(ValueError, match="PCSH directory contract"):
        finalize_places_family(
            built["request"],
            built["plan"],
            reduce_reports,
            head_report,
            output_dir=family_dir,
        )


def test_finalizer_rejects_self_consistent_but_corrupt_phrp(built):
    family_dir, reduce_reports, head_report = completed_outputs(built, "corrupt-phrp")
    path = family_dir / "head.phrp"
    encoded = path.read_bytes()
    assert b'"head_limit":10' in encoded
    path.write_bytes(encoded.replace(b'"head_limit":10', b'"head_limit":11', 1))
    head_report["artifact"]["sha256"], head_report["artifact"]["bytes"] = sha256_file(
        path
    )
    head_report["report_sha256"] = digest_value(
        {key: value for key, value in head_report.items() if key != "report_sha256"}
    )

    with pytest.raises(ValueError, match="PHRP directory contract/provenance"):
        finalize_places_family(
            built["request"],
            built["plan"],
            reduce_reports,
            head_report,
            output_dir=family_dir,
        )


def test_finalizer_streams_remote_serving_artifacts_one_at_a_time(built, monkeypatch):
    family_dir, reduce_reports, head_report = completed_outputs(
        built, "remote-finalize"
    )
    remote_objects = {}
    for report in reduce_reports:
        for shard in report["shards"]:
            path = family_dir / shard["object"]
            remote_objects[f"families/places/{shard['object']}"] = path.read_bytes()
            path.unlink()
    head_path = family_dir / "head.phrp"
    remote_objects["families/places/head.phrp"] = head_path.read_bytes()
    head_path.unlink()
    observed_outputs = []

    def fake_fetch(argv, check):
        assert check is True
        object_key, output = argv[1:]
        assert all(not path.exists() for path in observed_outputs)
        output_path = Path(output)
        output_path.write_bytes(remote_objects[object_key])
        observed_outputs.append(output_path)

    monkeypatch.setattr(places_plan.subprocess, "run", fake_fetch)
    final_report, _ = finalize_places_family(
        built["request"],
        built["plan"],
        reduce_reports,
        head_report,
        output_dir=family_dir,
        scratch_dir=built["tmp_path"] / "remote-finalize-scratch",
        fragment_fetch_command=["fixture-fetch", "{object_key}", "{output}"],
    )

    assert (
        validate_places_final_report(final_report, built["request"], built["plan"])
        is final_report
    )
    evidence = final_report["artifact_materialization"]
    assert evidence["fetched_artifacts"] == len(remote_objects)
    assert evidence["fetched_bytes"] == sum(map(len, remote_objects.values()))
    assert evidence["maximum_simultaneously_materialized_artifacts"] == 1
    assert all(not path.exists() for path in observed_outputs)
