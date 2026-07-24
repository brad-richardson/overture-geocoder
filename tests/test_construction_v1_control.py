import argparse
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("construction_v1_control", ROOT / "scripts/construction_v1_control.py")
CONTROL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(CONTROL)


def arguments(**overrides):
    values = {
        "request_id": "request-20260722-a1",
        "build_id": "build-20260722-a1",
        "slice_id": "slice-20260722-a1",
        "staging_id": "staging-20260722-a1",
        "producer_commit": "1" * 40,
        "legacy_core_version": "legacy-core-20260722-a1",
        "legacy_core_manifest_sha256": "2" * 64,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_review_package_is_deterministic_genesis_and_admits_both_families():
    first, admitted = CONTROL.prepare(arguments())
    second, _ = CONTROL.prepare(arguments())
    assert admitted
    assert first["blockers"] == []
    assert CONTROL.canonical(first) == CONTROL.canonical(second)
    assert first["request_sha256"] == CONTROL.sha256_bytes(CONTROL.canonical(first["request"]))
    assert first["request"]["lineage"] == {"genesis": True, "generation": 1, "predecessor": None}
    assert first["readiness"]["addresses"]["ready"] is True
    assert first["readiness"]["places"]["ready"] is True
    assert len(first["map_matrices"]["addresses"]) == 127
    assert len(first["map_matrices"]["places"]) == 89
    assert first["cost"]["projected_runner_minutes_upper_bound"] <= first["cost"]["max_total_runner_minutes"]


def test_control_pins_match_the_real_committed_evidence_files():
    """Close the hole: pin the exact committed bytes/identity, not a synthetic
    fixture, so a stale spec/readiness/inventory pin (or a not-ready pinned
    readiness) fails closed in CI instead of silently admitting nothing."""
    for name, contract in CONTROL.FAMILIES.items():
        for field, pin in (
            ("inventory", "inventory_file_sha256"),
            ("spec", "spec_sha256"),
            ("readiness", "readiness_file_sha256"),
        ):
            actual = CONTROL.sha256_file(ROOT / contract[field])
            assert actual == contract[pin], (
                f"{name} {field} pin is stale: committed {actual} != pinned {contract[pin]}"
            )
        readiness = json.loads((ROOT / contract["readiness"]).read_text())
        assert readiness.get("ready") is True, f"{name} pinned readiness is not ready:true"
        identity = {
            **readiness.get("checks", {}).get("canonical_inventory_identity", {}),
            **readiness.get("checks", {}),
            **readiness,
        }
        assert identity.get("evidence_spec_sha256") == contract["spec_sha256"]
        assert identity.get("scale_evidence_sha256") == contract["scale_evidence_sha256"]
        assert identity.get("inventory_sha256") == contract["inventory_sha256"]


def test_request_and_confirmation_bind_every_operator_change():
    first, _ = CONTROL.prepare(arguments())
    changed, _ = CONTROL.prepare(arguments(build_id="build-20260722-b2"))
    assert first["request_sha256"] != changed["request_sha256"]
    assert first["typed_confirmation"] != changed["typed_confirmation"]
    assert first["request_sha256"] in first["typed_confirmation"]
    assert "MODE=execute" in first["typed_confirmation"]
    assert "MAX_PARALLEL=4" in first["typed_confirmation"]


def test_namespaces_are_immutable_and_production_is_explicitly_forbidden():
    report, _ = CONTROL.prepare(arguments())
    namespaces = report["request"]["namespaces"]
    for field in ("staging", "content", "markers", "slice", "preview"):
        assert namespaces[field].startswith(namespaces["immutable_root"] + "/")
    assert namespaces["forbidden"] == ["catalog.json", "v2/catalog.json", "v2/releases/"]
    assert report["request"]["publication"] == {"production_writes": False, "non_promoting_slice": True, "preview_only": True}


def test_missing_core_identity_emits_honest_non_admitted_review_package():
    report, admitted = CONTROL.prepare(arguments(legacy_core_version=None, legacy_core_manifest_sha256=None))
    assert not admitted
    assert report["request"] is None
    assert report["request_sha256"] is None
    assert report["typed_confirmation"] is None
    assert report["blockers"][0] == "exact legacy core version and release-manifest SHA-256 are required"

