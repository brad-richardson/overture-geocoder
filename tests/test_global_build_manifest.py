from __future__ import annotations

import hashlib
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


NE_BBOX = [-80.5, 38.0, -66.9, 47.5]


def region(name="US-Northeast", bbox=None, scope="row_group_approximate"):
    return {"name": name, "bbox": list(NE_BBOX if bbox is None else bbox), "bbox_scope": scope}


def address_lineage():
    return {
        "overture_release": "2026-06-17.0",
        "build_id": "a" * 64,
        "producer_commit": "abc123",
        "producer_script": "scripts/experiment_address_reduce.py",
        "producer_version": "2",
    }


def address_versions():
    return {
        "format": manifest.ADDRESS_FORMAT_VERSION,
        "tokenizer": None,
        "normalization": manifest.ADDRESS_NORMALIZATION_VERSION,
    }


def places_versions():
    return {
        "format": manifest.PLACES_FORMAT_VERSION,
        "tokenizer": manifest.PLACES_TOKENIZER_VERSION,
        "normalization": None,
    }


def manifest_artifacts():
    return [
        {"object_key": "addresses/0001/x.bin", "bytes": 20, "sha256": "2" * 64},
        {"object_key": "addresses/0000/y.bin", "bytes": 10, "sha256": "1" * 64},
    ]


def family_manifest(**overrides):
    kwargs = {
        "family": "addresses",
        "lineage": address_lineage(),
        "versions": address_versions(),
        "region": region(),
        "artifacts": manifest_artifacts(),
    }
    kwargs.update(overrides)
    family = kwargs.pop("family")
    return manifest.build_family_manifest(family, **kwargs)


def family_scope(**overrides):
    scope = {
        "region": region(),
        "tokenizer_version": None,
        "normalization_version": manifest.ADDRESS_NORMALIZATION_VERSION,
        "producer_script": "scripts/experiment_address_reduce.py",
        "producer_version": "2",
        "generated_at": None,
    }
    scope.update(overrides)
    return scope


def test_family_manifest_validates_and_self_digest_is_stable():
    built = family_manifest()
    assert built["schema"] == manifest.FAMILY_MANIFEST_SCHEMA
    assert manifest.validate_family_manifest(built) == built
    # The digest is over the manifest without the digest field, recomputable.
    without_digest = {key: value for key, value in built.items() if key != "manifest_digest"}
    assert built["manifest_digest"] == manifest.digest(without_digest)
    assert built["totals"] == {"artifacts": 2, "bytes": 30}


def test_family_manifest_is_deterministic_regardless_of_artifact_order():
    reversed_artifacts = list(reversed(manifest_artifacts()))
    first = family_manifest()
    second = family_manifest(artifacts=reversed_artifacts)
    assert manifest.canonical_json(first) == manifest.canonical_json(second)


def test_family_manifest_digest_changes_with_any_field():
    base = family_manifest()
    bigger = family_manifest(
        artifacts=[{**manifest_artifacts()[0], "bytes": 21}, manifest_artifacts()[1]]
    )
    assert base["manifest_digest"] != bigger["manifest_digest"]
    other_region = family_manifest(region=region(scope="exact"))
    assert base["manifest_digest"] != other_region["manifest_digest"]


def test_family_version_contract_is_family_aware():
    with pytest.raises(ValueError, match="places family requires a tokenizer"):
        family_manifest(family="places", versions={**places_versions(), "tokenizer": None})
    with pytest.raises(ValueError, match="addresses family requires a normalization"):
        family_manifest(versions={**address_versions(), "normalization": None})
    # Places is valid with a tokenizer and null normalization.
    built = family_manifest(family="places", versions=places_versions())
    assert manifest.validate_family_manifest(built)["versions"]["normalization"] is None


def test_family_manifest_bbox_scope_round_trips_both_modes():
    for scope in ("row_group_approximate", "exact"):
        built = family_manifest(region=region(scope=scope))
        validated = manifest.validate_family_manifest(built)
        assert validated["region"]["bbox_scope"] == scope
        assert validated["region"]["bbox"] == NE_BBOX
        assert validated["region"]["name"] == "US-Northeast"
    with pytest.raises(ValueError, match="bbox_scope must be one of"):
        family_manifest(region=region(scope="unbounded"))
    with pytest.raises(ValueError, match="xmin < xmax"):
        family_manifest(region=region(bbox=[10.0, 38.0, -66.9, 47.5]))


def test_validate_family_manifest_detects_tampering():
    built = family_manifest()
    tampered = {**built, "artifacts": [{**built["artifacts"][0], "bytes": 999}, built["artifacts"][1]]}
    with pytest.raises(ValueError, match="deterministic contents"):
        manifest.validate_family_manifest(tampered)
    bad_schema = {**built, "schema": "nope"}
    with pytest.raises(ValueError, match="family manifest schema"):
        manifest.validate_family_manifest(bad_schema)


def test_verify_against_listing_detects_every_discrepancy():
    built = family_manifest()
    good = {"addresses/0000/y.bin": (10, "1" * 64), "addresses/0001/x.bin": (20, "2" * 64)}
    assert manifest.verify_family_manifest_against_listing(built, good) == built

    missing = dict(good)
    del missing["addresses/0000/y.bin"]
    with pytest.raises(ValueError, match="missing artifacts"):
        manifest.verify_family_manifest_against_listing(built, missing)

    extra = {**good, "addresses/0002/z.bin": (5, "3" * 64)}
    with pytest.raises(ValueError, match="unexpected objects"):
        manifest.verify_family_manifest_against_listing(built, extra)

    wrong_size = {**good, "addresses/0000/y.bin": (11, "1" * 64)}
    with pytest.raises(ValueError, match="size mismatch"):
        manifest.verify_family_manifest_against_listing(built, wrong_size)

    wrong_hash = {**good, "addresses/0000/y.bin": (10, "9" * 64)}
    with pytest.raises(ValueError, match="sha256 mismatch"):
        manifest.verify_family_manifest_against_listing(built, wrong_hash)


def test_verify_against_directory_recomputes_size_and_hash(tmp_path):
    payloads = {"addresses/0000/y.bin": b"y" * 10, "addresses/0001/x.bin": b"x" * 20}
    artifacts = [
        {
            "object_key": key,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        for key, data in payloads.items()
    ]
    built = family_manifest(artifacts=artifacts)
    for key, data in payloads.items():
        path = tmp_path / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assert manifest.verify_family_manifest_against_directory(built, tmp_path) == built

    # Corrupt one byte -> hash mismatch.
    (tmp_path / "addresses/0000/y.bin").write_bytes(b"z" * 10)
    with pytest.raises(ValueError, match="sha256 mismatch"):
        manifest.verify_family_manifest_against_directory(built, tmp_path)

    # Truncate -> size (and hash) mismatch.
    (tmp_path / "addresses/0000/y.bin").write_bytes(b"y" * 9)
    with pytest.raises(ValueError, match="size mismatch"):
        manifest.verify_family_manifest_against_directory(built, tmp_path)

    # Remove -> missing.
    (tmp_path / "addresses/0000/y.bin").unlink()
    with pytest.raises(ValueError, match="missing artifacts"):
        manifest.verify_family_manifest_against_directory(built, tmp_path)

    # Extra file -> unexpected.
    (tmp_path / "addresses/0000/y.bin").write_bytes(b"y" * 10)
    (tmp_path / "addresses/0002/z.bin").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "addresses/0002/z.bin").write_bytes(b"z")
    with pytest.raises(ValueError, match="unexpected objects"):
        manifest.verify_family_manifest_against_directory(built, tmp_path)


def test_fan_in_references_family_manifest_by_key_and_digest():
    build_plan = plan()
    completions = map_completions(build_plan)
    artifacts = artifact_manifests(build_plan, completions)
    previous = "a" * 64

    without_scope = manifest.build_catalog_candidate(
        build_plan, completions, artifacts, expected_previous_catalog_digest=previous
    )
    assert "family_manifest" not in without_scope

    scope = family_scope()
    candidate = manifest.build_catalog_candidate(
        build_plan,
        completions,
        artifacts,
        expected_previous_catalog_digest=previous,
        family_scope=scope,
    )
    assert candidate["promotion_eligible"] is False
    reference = candidate["family_manifest"]
    expected_manifest = manifest.build_family_manifest_from_candidate(
        build_plan, candidate["artifacts"], scope
    )
    assert reference["key"] == manifest.family_manifest_key(build_plan["build_id"], "addresses")
    assert reference["manifest_digest"] == expected_manifest["manifest_digest"]
    # The manifest's format is the reduce artifacts' own format_version.
    assert expected_manifest["versions"]["format"] == "fixture-v1"
    # Fan-in stays order-independent with a scope attached.
    reordered = manifest.build_catalog_candidate(
        build_plan,
        list(reversed(completions)),
        list(reversed(artifacts)),
        expected_previous_catalog_digest=previous,
        family_scope=scope,
    )
    assert candidate == reordered


def test_family_manifest_rejects_nonuniform_artifact_format():
    build_plan = plan()
    completions = map_completions(build_plan)
    artifacts = artifact_manifests(build_plan, completions)
    artifacts[0] = {**artifacts[0], "format_version": "other-v9"}
    with pytest.raises(ValueError, match="single format_version"):
        manifest.build_family_manifest_from_candidate(build_plan, artifacts, family_scope())


def test_cli_fan_in_emits_family_manifest(tmp_path):
    build_plan = plan()
    completions = map_completions(build_plan)
    artifacts = artifact_manifests(build_plan, completions)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(build_plan))
    completion_paths = []
    for index, completion in enumerate(completions):
        path = tmp_path / f"completion-{index}.json"
        path.write_text(json.dumps(completion))
        completion_paths.append(path)
    manifest_paths = []
    for index, artifact in enumerate(artifacts):
        path = tmp_path / f"artifact-{index}.json"
        path.write_text(json.dumps(artifact))
        manifest_paths.append(path)
    candidate_path = tmp_path / "candidate.json"
    family_manifest_path = tmp_path / "family-manifest.json"
    command = [
        sys.executable,
        str(SCRIPT),
        "fan-in",
        "--plan",
        str(plan_path),
        "--expected-previous-catalog-digest",
        "a" * 64,
        "--output",
        str(candidate_path),
        "--region-name",
        "US-Northeast",
        "--bbox",
        "-80.5",
        "38.0",
        "-66.9",
        "47.5",
        "--bbox-scope",
        "row_group_approximate",
        "--normalization-version",
        manifest.ADDRESS_NORMALIZATION_VERSION,
        "--producer-script",
        "scripts/experiment_address_reduce.py",
        "--producer-version",
        "2",
        "--family-manifest-output",
        str(family_manifest_path),
    ]
    for path in completion_paths:
        command += ["--map-completion", str(path)]
    for path in manifest_paths:
        command += ["--manifest", str(path)]
    subprocess.run(command, check=True)
    candidate = json.loads(candidate_path.read_text())
    emitted = json.loads(family_manifest_path.read_text())
    assert manifest.validate_family_manifest(emitted) == emitted
    assert candidate["family_manifest"]["manifest_digest"] == emitted["manifest_digest"]
    assert candidate["family_manifest"]["key"] == manifest.family_manifest_key(
        build_plan["build_id"], "addresses"
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
