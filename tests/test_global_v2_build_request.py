from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "global_v2_build_request.py"
SPEC = importlib.util.spec_from_file_location("global_v2_build_request", SCRIPT)
assert SPEC and SPEC.loader
request = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(request)


def arguments(**overrides) -> dict:
    values = {
        "overture_release": "2026-06-17.0",
        "geocoder_build": "2026-07-19.7",
        "slice_version": "slice-2026-07-19.7",
        "legacy_core_version": "2026-07-18.0",
        "legacy_core_overture_release": "2026-06-17.0",
        "legacy_core_manifest_key": "2026-07-18.0/release-manifest.json",
        "legacy_core_manifest_sha256": "1" * 64,
        "addresses_inventory_sha256": "2" * 64,
        "addresses_schema_fingerprint_sha256": "3" * 64,
        "addresses_predecessor_family_manifest_sha256": None,
        "addresses_lineage_generation": 1,
        "places_inventory_sha256": "4" * 64,
        "places_schema_fingerprint_sha256": "5" * 64,
        "places_predecessor_family_manifest_sha256": None,
        "places_lineage_generation": 1,
        "producer_commit": "a" * 40,
    }
    values.update(overrides)
    return values


def build(**overrides) -> dict:
    return request.build_request(**arguments(**overrides))


def test_request_locks_complete_family_only_contract_without_dispatch():
    value = build()

    assert request.validate_request(value) == value
    assert value["legacy_core"] == {
        "version": "2026-07-18.0",
        "overture_release": "2026-06-17.0",
        "manifest_key": "2026-07-18.0/release-manifest.json",
        "manifest_sha256": "1" * 64,
    }
    assert value["slice_version"] == "slice-2026-07-19.7"
    assert value["scope"] == {
        "kind": "families-only",
        "coverage": [-180.0, -90.0, 180.0, 90.0],
        "artifact_prefix": "slice-2026-07-19.7/",
        "reuse_legacy_core": True,
        "rebuild_divisions": False,
        "publication": "separate-explicit-step",
    }

    addresses = value["families"]["addresses"]
    assert addresses["source"] == {
        "inventory_sha256": "2" * 64,
        "schema_fingerprint_sha256": "3" * 64,
    }
    assert addresses["predecessor_family_manifest_sha256"] is None
    assert addresses["partition"] == {
        "lineage_generation": 1,
        "scheme": "country-fnv1a-high-bits-v1",
        "maximum_hash_bits": 16,
        "split_row_cap": 1_000_000,
        "sticky_splits": True,
    }
    assert "families/addresses/partition-plan.json" in addresses["required_artifacts"]

    places = value["families"]["places"]
    assert places["source"] == {
        "inventory_sha256": "4" * 64,
        "schema_fingerprint_sha256": "5" * 64,
    }
    assert places["predecessor_family_manifest_sha256"] is None
    assert places["partition"] == {
        "lineage_generation": 1,
        "scheme": "world-quadkey-v1",
        "minimum_level": 6,
        "maximum_level": 12,
        "split_row_cap": 1_500_000,
        "sticky_splits": True,
    }
    assert places["global_head"] == {
        "format": "PHRP0001",
        "admission": "famous-unique-v1",
        "ranking": "quantized-confidence-stable-serving-order-v1",
        "minimum_candidates": 64,
        "famous_cap": 1024,
        "result_cap": 10,
        "prefix_policy": {
            "version": "normalized-token-prefix-lengths-v1",
            "lengths": [2, 3, 4, 5, 6, 7, 8],
        },
        "provenance": {
            "predecessor_family_manifest_sha256": None,
            "predecessor_family_manifest": {
                "object_key": None, "bytes": None, "sha256": None
            },
        },
    }
    assert "families/places/head.phrp" in places["required_artifacts"]

    assert value["execution"]["source_task_limit"] == 128
    assert value["execution"]["reduce_job_limit"] == 82
    assert value["execution"]["reduce_job_limit"] * 2 == 164
    assert value["execution"]["compute_tasks_are_not_shard_ids"] is True
    assert value["execution"]["state"] == "prepared-not-dispatched"


def test_request_pins_explicit_predecessors_and_head_provenance():
    value = build(
        addresses_lineage_generation=2,
        addresses_predecessor_family_manifest_sha256="b" * 64,
        addresses_predecessor_family_manifest_key="slice-2026-07-18.0/families/addresses/family-manifest.json",
        addresses_predecessor_family_manifest_bytes=123,
        places_predecessor_family_manifest_sha256="c" * 64,
        places_predecessor_family_manifest_key="slice-2026-07-18.0/families/places/family-manifest.json",
        places_predecessor_family_manifest_bytes=456,
        places_lineage_generation=2,
    )

    assert (
        value["families"]["addresses"]["predecessor_family_manifest_sha256"] == "b" * 64
    )
    assert value["families"]["places"]["predecessor_family_manifest_sha256"] == "c" * 64
    places_predecessor = {
        "object_key": "slice-2026-07-18.0/families/places/family-manifest.json",
        "bytes": 456,
        "sha256": "c" * 64,
    }
    assert value["families"]["places"]["predecessor_family_manifest"] == places_predecessor
    assert value["families"]["places"]["global_head"]["provenance"] == {
        "predecessor_family_manifest_sha256": "c" * 64,
        "predecessor_family_manifest": places_predecessor,
    }
    assert request.validate_request(value) == value


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["families"]["addresses"]["partition"].__setitem__(
            "scheme", "other-v1"
        ),
        lambda value: value["families"]["places"]["global_head"].__setitem__(
            "admission", "other-v1"
        ),
        lambda value: value["families"]["places"]["global_head"].__setitem__(
            "ranking", "other-v1"
        ),
        lambda value: value["families"]["places"]["global_head"][
            "prefix_policy"
        ].__setitem__("lengths", [2, 3]),
        lambda value: value["families"]["places"]["global_head"].__setitem__(
            "result_cap", 11
        ),
        lambda value: value["families"]["places"]["global_head"][
            "provenance"
        ].__setitem__("predecessor_family_manifest_sha256", "f" * 64),
        lambda value: value["scope"].__setitem__("publication", "automatic"),
        lambda value: value.__setitem__("unknown", True),
        lambda value: value["families"]["addresses"].pop("operations"),
    ],
)
def test_request_validation_detects_any_contract_drift(mutate):
    value = copy.deepcopy(build())
    mutate(value)

    with pytest.raises(ValueError, match="differs from the current producer contract"):
        request.validate_request(value)


@pytest.mark.parametrize(
    ("field", "bad", "message"),
    [
        ("overture_release", "latest", "overture_release"),
        ("geocoder_build", "next", "geocoder_build"),
        ("slice_version", "2026-07-19.7", "slice_version"),
        ("legacy_core_version", "current", "legacy_core_version"),
        (
            "legacy_core_overture_release",
            "2026-05-20.0",
            "must match overture_release",
        ),
        ("legacy_core_manifest_key", "/release.json", "canonical relative"),
        (
            "legacy_core_manifest_key",
            "2026-07-17.0/release-manifest.json",
            "must match legacy_core_version",
        ),
        ("legacy_core_manifest_sha256", "A" * 64, "lowercase SHA-256"),
        ("addresses_inventory_sha256", "short", "lowercase SHA-256"),
        ("addresses_schema_fingerprint_sha256", None, "lowercase SHA-256"),
        (
            "addresses_predecessor_family_manifest_sha256",
            "no",
            "lowercase SHA-256",
        ),
        ("places_inventory_sha256", "g" * 64, "lowercase SHA-256"),
        ("places_schema_fingerprint_sha256", "", "lowercase SHA-256"),
        (
            "places_predecessor_family_manifest_sha256",
            "D" * 64,
            "lowercase SHA-256",
        ),
        ("producer_commit", "deadbeef", "full lowercase Git commit SHA"),
    ],
)
def test_request_rejects_unpinned_or_mismatched_identities(field, bad, message):
    with pytest.raises(ValueError, match=message):
        build(**{field: bad})


@pytest.mark.parametrize(
    ("field", "bad", "message"),
    [
        ("address_maximum_hash_bits", 0, "address_maximum_hash_bits"),
        ("addresses_lineage_generation", True, "addresses_lineage_generation"),
        ("addresses_lineage_generation", 0, "addresses_lineage_generation"),
        ("address_maximum_hash_bits", 25, "address_maximum_hash_bits"),
        ("address_split_row_cap", True, "address_split_row_cap"),
        ("places_minimum_level", 0, "places_minimum_level"),
        ("places_lineage_generation", 0, "places_lineage_generation"),
        ("places_maximum_level", 16, "places_maximum_level"),
        ("places_split_row_cap", 0, "places_split_row_cap"),
        ("head_minimum_candidates", 0, "head_minimum_candidates"),
        ("head_famous_cap", -1, "head_famous_cap"),
        ("source_task_limit", 129, "source_task_limit"),
        ("reduce_job_limit", 257, "reduce_job_limit"),
    ],
)
def test_request_rejects_partition_head_and_workflow_limits(field, bad, message):
    with pytest.raises(ValueError, match=message):
        build(**{field: bad})


def test_request_rejects_inverted_places_levels():
    with pytest.raises(ValueError, match="partition levels"):
        build(places_minimum_level=12, places_maximum_level=6)


@pytest.mark.parametrize("family", ["addresses", "places"])
def test_lineage_generation_one_rejects_a_predecessor(family):
    prefix = f"{family}_predecessor_family_manifest"
    with pytest.raises(ValueError, match=f"{family} lineage generation 1"):
        build(
            **{
                f"{prefix}_key": (
                    f"slice-2026-07-18.0/families/{family}/family-manifest.json"
                ),
                f"{prefix}_bytes": 123,
                f"{prefix}_sha256": "b" * 64,
            }
        )


@pytest.mark.parametrize("family", ["addresses", "places"])
def test_later_lineage_generation_requires_an_exact_predecessor(family):
    with pytest.raises(ValueError, match=f"{family} lineage generation 2"):
        build(**{f"{family}_lineage_generation": 2})


def test_validation_rejects_wrong_schema_and_malformed_nested_shape():
    wrong_schema = copy.deepcopy(build())
    wrong_schema["schema"] = "old"
    with pytest.raises(ValueError, match="build request schema"):
        request.validate_request(wrong_schema)

    malformed = copy.deepcopy(build())
    malformed["families"]["places"]["source"] = []
    with pytest.raises(ValueError, match="families.places.source must be an object"):
        request.validate_request(malformed)


def test_cli_output_is_reproducible_explicitly_null_and_validates(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    command = [
        sys.executable,
        str(SCRIPT),
        "build",
        "--overture-release",
        "2026-06-17.0",
        "--geocoder-build",
        "2026-07-19.7",
        "--slice-version",
        "slice-2026-07-19.7",
        "--legacy-core-version",
        "2026-07-18.0",
        "--legacy-core-overture-release",
        "2026-06-17.0",
        "--legacy-core-manifest-key",
        "2026-07-18.0/release-manifest.json",
        "--legacy-core-manifest-sha256",
        "1" * 64,
        "--addresses-inventory-sha256",
        "2" * 64,
        "--addresses-schema-fingerprint-sha256",
        "3" * 64,
        "--addresses-lineage-generation",
        "1",
        "--places-inventory-sha256",
        "4" * 64,
        "--places-schema-fingerprint-sha256",
        "5" * 64,
        "--places-lineage-generation",
        "1",
        "--producer-commit",
        "b" * 40,
    ]
    subprocess.run([*command, "--output", str(first)], check=True, capture_output=True)
    subprocess.run([*command, "--output", str(second)], check=True, capture_output=True)

    assert first.read_bytes() == second.read_bytes()
    value = json.loads(first.read_text())
    assert value == request.validate_request(value)
    assert value["families"]["addresses"]["predecessor_family_manifest_sha256"] is None
    assert value["families"]["places"]["predecessor_family_manifest_sha256"] is None
    subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--request", str(first)],
        check=True,
        capture_output=True,
    )
