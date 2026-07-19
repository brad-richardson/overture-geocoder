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


def build() -> dict:
    return request.build_request(
        overture_release="2026-06-17.0",
        geocoder_build="2026-07-19.7",
        producer_commit="a" * 40,
    )


def test_request_locks_family_only_global_contracts_without_dispatch():
    value = build()

    assert request.validate_request(value) == value
    assert value["scope"] == {
        "kind": "families-only",
        "coverage": [-180.0, -90.0, 180.0, 90.0],
        "reuse_legacy_core": True,
        "rebuild_divisions": False,
        "publication": "separate-explicit-step",
    }
    assert value["families"]["places"]["partition"]["scheme"] == (
        "world-quadkey-v1"
    )
    assert value["families"]["addresses"]["partition"]["scheme"] == (
        "country-fnv1a-high-bits-v1"
    )
    assert "families/places/head.phrp" in value["families"]["places"][
        "required_artifacts"
    ]
    assert value["execution"]["compute_tasks_are_not_shard_ids"] is True
    assert value["execution"]["state"] == "prepared-not-dispatched"


def test_request_validation_detects_contract_drift():
    value = copy.deepcopy(build())
    value["families"]["addresses"]["partition"]["split_row_cap"] += 1
    with pytest.raises(ValueError, match="differs from the current producer contract"):
        request.validate_request(value)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("overture_release", "latest", "overture_release"),
        ("geocoder_build", "next", "geocoder_build"),
        ("producer_commit", "deadbeef", "full lowercase Git commit SHA"),
    ],
)
def test_request_rejects_unpinned_identities(field, value, message):
    arguments = {
        "overture_release": "2026-06-17.0",
        "geocoder_build": "2026-07-19.7",
        "producer_commit": "a" * 40,
    }
    arguments[field] = value
    with pytest.raises(ValueError, match=message):
        request.build_request(**arguments)


def test_cli_output_is_reproducible_and_validates(tmp_path):
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
        "--producer-commit",
        "b" * 40,
    ]
    subprocess.run([*command, "--output", str(first)], check=True, capture_output=True)
    subprocess.run([*command, "--output", str(second)], check=True, capture_output=True)
    assert first.read_bytes() == second.read_bytes()
    assert json.loads(first.read_text()) == request.validate_request(
        json.loads(first.read_text())
    )
    subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--request", str(first)],
        check=True,
        capture_output=True,
    )
