from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "global_build_manifest.py"
SPEC = importlib.util.spec_from_file_location("global_build_manifest", SCRIPT)
assert SPEC and SPEC.loader
manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest)


def inventory() -> dict:
    prefix = "s3://bucket/release/2026-07-02.3/theme=addresses/type=address"
    return {
        "schema": manifest.SOURCE_INVENTORY_SCHEMA,
        "release": "2026-07-02.3",
        "family": "addresses",
        "theme": "addresses",
        "type": "address",
        "schema_version": "2026-07-02.3",
        "discovery": {"kind": "test-fixture", "source": "unit-test"},
        "objects": [
            {
                "uri": f"{prefix}/b.parquet",
                "etag": "b1",
                "bytes": 20,
                "records": 2,
                "row_groups": 2,
            },
            {
                "uri": f"{prefix}/a.parquet",
                "etag": "a1",
                "bytes": 10,
                "records": 1,
                "row_groups": 1,
            },
        ],
    }


def plan(items=None, *, partitions=3):
    return manifest.build_plan(
        inventory() if items is None else items,
        release="2026-07-02.3",
        producer_commit="abc123",
        family="addresses",
        map_tasks=2,
        partitions=partitions,
    )


def map_completions(build_plan):
    return [
        {
            "schema": manifest.MAP_COMPLETION_SCHEMA,
            "build_id": build_plan["build_id"],
            "index": task["index"],
            "task_id": task["task_id"],
            "source_digest": task["source_digest"],
            "status": "complete",
            "input_records": task["expected_input_records"],
            "selected_records": task["expected_input_records"],
            "rejected_records": 0,
            "rejected_reasons": {},
            "output_records": task["expected_input_records"],
            "fragments": (
                [
                    {
                        "partition": 0,
                        "object_key": (
                            f"staging/{build_plan['build_id']}/map/{task['index']:03d}/"
                            f"{task['task_id']}/0000/{'b' * 64}.parquet"
                        ),
                        "bytes": 10,
                        "records": task["expected_input_records"],
                        "sha256": "b" * 64,
                    }
                ]
                if task["sources"]
                else []
            ),
        }
        for task in build_plan["map_tasks"]
    ]


def artifact_manifests(build_plan, completions=None):
    completions = map_completions(build_plan) if completions is None else completions
    completion_digest = manifest.digest(completions)
    fragments = manifest.partition_fragments(
        completions, build_plan["configuration"]["partitions"]
    )
    return [
        {
            "schema": manifest.ARTIFACT_SCHEMA,
            "build_id": build_plan["build_id"],
            "family": "addresses",
            "partition": task["index"],
            "task_id": task["task_id"],
            "object_key": task["expected_artifact_key"],
            "map_completion_digest": completion_digest,
            "fragment_digest": manifest.digest(fragments[task["index"]]),
            "input_fragments": len(fragments[task["index"]]),
            "input_bytes": sum(item["bytes"] for item in fragments[task["index"]]),
            "input_records": sum(item["records"] for item in fragments[task["index"]]),
            "output_records": sum(item["records"] for item in fragments[task["index"]]),
            "format_version": "fixture-v1",
            "record_schema_version": build_plan["inventory"]["schema_version"],
            "verification": {"checksum": True, "record_count": True, "strict_reader": True},
            "bytes": task["index"] + 1,
            "sha256": f"{task['index'] + 1:064x}",
        }
        for task in build_plan["reduce_tasks"]
    ]


def test_plan_is_deterministic_across_inventory_order():
    reversed_inventory = inventory()
    reversed_inventory["objects"] = list(reversed(reversed_inventory["objects"]))
    assert plan(inventory()) == plan(reversed_inventory)


def test_plan_changes_when_source_identity_changes():
    changed = inventory()
    changed["objects"][0] = {**changed["objects"][0], "etag": "b2"}
    assert plan()["build_id"] != plan(changed)["build_id"]


def test_plan_rejects_inventory_mislabeled_as_another_release_or_family():
    with pytest.raises(ValueError, match="release/family"):
        manifest.build_plan(
            inventory(),
            release="2026-07-09.0",
            producer_commit="abc123",
            family="addresses",
            map_tasks=2,
            partitions=3,
        )
    mislabeled = inventory()
    mislabeled["family"] = "places"
    with pytest.raises(ValueError, match="release/family"):
        plan(mislabeled)
    wrong_uri = inventory()
    wrong_uri["objects"][0] = {
        **wrong_uri["objects"][0],
        "uri": wrong_uri["objects"][0]["uri"].replace("theme=addresses", "theme=places"),
    }
    with pytest.raises(ValueError, match="uri does not match"):
        plan(wrong_uri)


def test_plan_assigns_every_source_exactly_once():
    build_plan = plan()
    sources = [source for task in build_plan["map_tasks"] for source in task["sources"]]
    assert sorted(source["uri"] for source in sources) == [
        "s3://bucket/release/2026-07-02.3/theme=addresses/type=address/a.parquet",
        "s3://bucket/release/2026-07-02.3/theme=addresses/type=address/b.parquet",
    ]
    assert build_plan["inventory_totals"] == {
        "objects": 2,
        "bytes": 30,
        "records": 3,
        "row_groups": 3,
    }


def test_describe_task_rejects_boolean_index():
    with pytest.raises(ValueError, match="task index must be an integer"):
        manifest.describe_task(plan(), "map", True)


def test_validate_plan_rejects_tampering():
    build_plan = plan()
    build_plan["reduce_tasks"][0]["expected_artifact_key"] = "production/overwrite.bin"
    with pytest.raises(ValueError, match="deterministic contents"):
        manifest.validate_plan(build_plan)


def test_fan_in_is_order_independent_and_exact():
    build_plan = plan()
    artifacts = artifact_manifests(build_plan)
    completions = map_completions(build_plan)
    previous = "a" * 64
    first = manifest.build_catalog_candidate(
        build_plan, completions, artifacts, expected_previous_catalog_digest=previous
    )
    second = manifest.build_catalog_candidate(
        build_plan,
        list(reversed(completions)),
        list(reversed(artifacts)),
        expected_previous_catalog_digest=previous,
    )
    assert first == second
    assert first["totals"] == {
        "artifacts": 3,
        "bytes": 6,
        "source_records": 3,
        "selected_records": 3,
        "rejected_records": 0,
        "fragment_records": 3,
        "artifact_records": 3,
    }


def test_fan_in_rejects_missing_duplicate_and_wrong_object_key():
    build_plan = plan()
    completions = map_completions(build_plan)
    artifacts = artifact_manifests(build_plan)
    with pytest.raises(ValueError, match="missing partitions"):
        manifest.build_catalog_candidate(
            build_plan, completions, artifacts[:-1], expected_previous_catalog_digest="a" * 64
        )
    with pytest.raises(ValueError, match="duplicate partition"):
        manifest.build_catalog_candidate(
            build_plan,
            completions,
            artifacts + [artifacts[0]],
            expected_previous_catalog_digest="a" * 64,
        )
    artifacts[0] = {**artifacts[0], "object_key": "production/not-staged.bin"}
    with pytest.raises(ValueError, match="object_key mismatch"):
        manifest.build_catalog_candidate(
            build_plan, completions, artifacts, expected_previous_catalog_digest="a" * 64
        )


def test_fan_in_requires_promotion_compare_and_swap_digest():
    with pytest.raises(ValueError, match="expected_previous_catalog_digest"):
        manifest.build_catalog_candidate(
            plan(),
            map_completions(plan()),
            artifact_manifests(plan()),
            expected_previous_catalog_digest="latest",
        )


def test_map_completion_requires_every_planned_task_and_exact_source_digest():
    build_plan = plan()
    completions = map_completions(build_plan)
    with pytest.raises(ValueError, match="missing map tasks"):
        manifest.validate_map_completions(build_plan, completions[:-1])
    completions[0] = {**completions[0], "source_digest": "0" * 64}
    with pytest.raises(ValueError, match="source_digest mismatch"):
        manifest.validate_map_completions(build_plan, completions)


def test_nonempty_map_task_cannot_claim_complete_without_record_accounting():
    build_plan = plan()
    completions = map_completions(build_plan)
    target = next(index for index, task in enumerate(build_plan["map_tasks"]) if task["sources"])
    completions[target] = {
        **completions[target],
        "input_records": 0,
        "selected_records": 0,
        "output_records": 0,
        "fragments": [],
    }
    with pytest.raises(ValueError, match="do not match inventory"):
        manifest.validate_map_completions(build_plan, completions)


def test_all_rejected_build_is_never_promotion_eligible():
    build_plan = plan()
    completions = map_completions(build_plan)
    for index, completion in enumerate(completions):
        if completion["input_records"]:
            completions[index] = {
                **completion,
                "selected_records": 0,
                "rejected_records": completion["input_records"],
                "rejected_reasons": {"fixture_rejection": completion["input_records"]},
                "output_records": 0,
                "fragments": [],
            }
    candidate = manifest.build_catalog_candidate(
        build_plan,
        completions,
        artifact_manifests(build_plan, completions),
        expected_previous_catalog_digest="a" * 64,
    )
    assert candidate["totals"]["rejected_records"] == 3
    assert candidate["rejected_reasons"] == {"fixture_rejection": 3}
    assert candidate["totals"]["artifact_records"] == 0
    assert candidate["promotion_eligible"] is False


def test_production_inventory_requires_approved_overture_prefix():
    production = inventory()
    production["discovery"] = {
        "kind": "overture-s3-listing",
        "source": (
            "s3://overturemaps-us-west-2/release/2026-07-02.3/"
            "theme=addresses/type=address/"
        ),
    }
    production["objects"][0] = {
        **production["objects"][0],
        "uri": production["objects"][0]["uri"].replace("s3://bucket/", "s3://evil-bucket/"),
    }
    with pytest.raises(ValueError, match="outside the approved Overture prefix"):
        plan(production)


@pytest.mark.parametrize("field", ["bytes", "records", "row_groups"])
def test_inventory_numeric_fields_reject_booleans(field):
    invalid = inventory()
    invalid["objects"][0] = {**invalid["objects"][0], field: True}
    with pytest.raises(ValueError, match="must be an integer"):
        plan(invalid)


def test_manifest_numeric_fields_reject_booleans():
    build_plan = plan()
    completions = map_completions(build_plan)
    completions[0] = {**completions[0], "input_records": True}
    with pytest.raises(ValueError, match="must be an integer"):
        manifest.validate_map_completions(build_plan, completions)

    completions = map_completions(build_plan)
    artifacts = artifact_manifests(build_plan, completions)
    artifacts[0] = {**artifacts[0], "bytes": True}
    with pytest.raises(ValueError, match="must be an integer"):
        manifest.build_catalog_candidate(
            build_plan,
            completions,
            artifacts,
            expected_previous_catalog_digest="a" * 64,
        )


def test_reduce_artifact_binds_exact_map_completion_set():
    build_plan = plan()
    completions = map_completions(build_plan)
    artifacts = artifact_manifests(build_plan, completions)
    artifacts[0] = {**artifacts[0], "map_completion_digest": "0" * 64}
    with pytest.raises(ValueError, match="map_completion_digest mismatch"):
        manifest.build_catalog_candidate(
            build_plan,
            completions,
            artifacts,
            expected_previous_catalog_digest="a" * 64,
        )
    artifacts = artifact_manifests(build_plan, completions)
    artifacts[0] = {**artifacts[0], "fragment_digest": "0" * 64}
    with pytest.raises(ValueError, match="fragment_digest mismatch"):
        manifest.build_catalog_candidate(
            build_plan,
            completions,
            artifacts,
            expected_previous_catalog_digest="a" * 64,
        )


def test_completion_and_artifact_schemas_reject_unknown_fields():
    build_plan = plan()
    completions = map_completions(build_plan)
    completions[0] = {**completions[0], "finished_at": "nondeterministic"}
    with pytest.raises(ValueError, match="unknown=.*finished_at"):
        manifest.validate_map_completions(build_plan, completions)

    completions = map_completions(build_plan)
    artifacts = artifact_manifests(build_plan, completions)
    artifacts[0] = {**artifacts[0], "url": "unvalidated"}
    with pytest.raises(ValueError, match="unknown=.*url"):
        manifest.build_catalog_candidate(
            build_plan,
            completions,
            artifacts,
            expected_previous_catalog_digest="a" * 64,
        )


def test_cli_plan_and_describe_task(tmp_path):
    inventory_path = tmp_path / "inventory.json"
    plan_path = tmp_path / "plan.json"
    inventory_path.write_text(json.dumps(inventory()))
    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "plan",
            "--inventory",
            str(inventory_path),
            "--release",
            "2026-07-02.3",
            "--producer-commit",
            "abc123",
            "--family",
            "addresses",
            "--map-tasks",
            "2",
            "--partitions",
            "3",
            "--output",
            str(plan_path),
        ],
        check=True,
    )
    result = json.loads(plan_path.read_text())
    described = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "describe-task",
            "--plan",
            str(plan_path),
            "--kind",
            "map",
            "--index",
            "1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(described.stdout)["task"] == result["map_tasks"][1]
