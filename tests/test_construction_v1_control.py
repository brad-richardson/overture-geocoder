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
    assert len(first["map_matrices"]["addresses"]) == 126
    assert len(first["map_matrices"]["places"]) == 88
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
        # Readiness names the ATTESTED inventory and keeps naming it after the
        # build release moves on; `inventory_sha256` tracks the live one.
        assert identity.get("inventory_sha256") == contract["attested_inventory_sha256"]


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



def test_the_envelope_admits_a_release_that_is_smaller_than_the_attested_one():
    """The whole point of the envelope: build 2026-07-22.0 on evidence measured
    against 2026-06-17.0, because the producer and schema did not move and the
    data got smaller."""
    report, admitted = CONTROL.prepare(arguments())
    assert admitted, report["blockers"]
    assert report["request"]["release"] == CONTROL.DEFAULT_RELEASE
    assert report["request"]["attestation"]["attested_release"] == CONTROL.ATTESTED_RELEASE
    assert CONTROL.DEFAULT_RELEASE != CONTROL.ATTESTED_RELEASE, (
        "this test is vacuous unless the build release has actually moved"
    )
    for name, contract in CONTROL.FAMILIES.items():
        inventory = CONTROL.read_json(contract["inventory"])
        live = CONTROL.live_scale(contract, inventory)
        for field, attested in contract["attested_scale"].items():
            assert live[field] <= attested, f"{name} {field} is outside the envelope"


def test_the_envelope_is_one_directional_and_a_bigger_release_fails_closed():
    """Evidence that a producer survived N records says nothing about N+1. A
    release that grows past any attested dimension must demand fresh evidence."""
    for name, contract in CONTROL.FAMILIES.items():
        inventory = CONTROL.read_json(contract["inventory"])
        assert CONTROL.scale_envelope_errors(name, contract, inventory) == []
        for field in contract["attested_scale"]:
            section, key = (
                CONTROL.SCALE_PATHS[field]
                if field in CONTROL.SCALE_PATHS
                else (contract["task_path"][0], "task_count")
            )
            grown = json.loads(json.dumps(inventory))
            grown[section][key] = contract["attested_scale"][field] + 1
            errors = CONTROL.scale_envelope_errors(name, contract, grown)
            assert errors, f"{name} {field} grew past the attested scale and still passed"
            assert "regenerate the evidence" in errors[0]


def test_a_changed_schema_fingerprint_fails_closed_however_small_the_release():
    """The envelope is a scale allowance, never a schema allowance: the evidence
    describes a producer contract, and a changed contract voids it outright."""
    for name, contract in CONTROL.FAMILIES.items():
        moved = {**contract, "schema_fingerprint_sha256": "0" * 64}
        _, errors = CONTROL.family_status(name, moved, CONTROL.DEFAULT_RELEASE)
        assert any("schema fingerprint differs" in error for error in errors)


def test_address_map_tasks_come_from_the_live_inventory_not_the_readiness_doc():
    """The readiness document pins the ATTESTED release's row-group ranges and
    ETags forever. Sourcing the matrix from it after the release moves would map
    one release's byte offsets over another's objects."""
    contract = CONTROL.FAMILIES["addresses"]
    inventory = CONTROL.read_json(contract["inventory"])
    readiness = CONTROL.read_json(contract["readiness"])
    frozen = readiness["checks"]["canonical_inventory_identity"]["tasks"]
    matrix = CONTROL.map_tasks("addresses", contract, readiness)
    assert len(matrix) == inventory["plan"]["task_count"]
    assert len(matrix) != len(frozen), "expected the two task sets to have diverged"
    assert [task["task_digest"] for task in matrix] == [
        task["task_digest_sha256"] for task in inventory["plan"]["tasks"]
    ]


def test_the_frozen_specs_still_point_at_the_inventory_they_attest():
    """The evidence specs are sha256-pinned AND name their inventory by path and
    release. Repointing that path at a newer release would leave a frozen
    artifact describing a file that no longer holds what it attests, and would
    strand the readiness validators, which require inventory.release ==
    spec.release. So the live inventory gets its own path and the attested one
    stays exactly where the spec left it."""
    for name, contract in CONTROL.FAMILIES.items():
        spec = CONTROL.read_json(contract["spec"])
        attested = CONTROL.read_json(contract["attested_inventory"])
        assert spec["inventory"]["path"] == contract["attested_inventory"], (
            f"{name} spec names {spec['inventory']['path']}, but the attested "
            f"inventory is {contract['attested_inventory']}"
        )
        assert attested["release"] == spec["release"] == CONTROL.ATTESTED_RELEASE
        assert attested["inventory_sha256"] == contract["attested_inventory_sha256"]
        assert contract["inventory"] != contract["attested_inventory"]
        assert CONTROL.read_json(contract["inventory"])["release"] == CONTROL.DEFAULT_RELEASE


def test_the_attested_scale_pins_describe_the_attested_inventory():
    """`attested_scale` is hand-typed, and a typo raising an anchor would widen
    the envelope silently -- nothing else in the gate would notice. The attested
    inventory is committed precisely so the pin can be reconciled against the
    artifact it claims to summarise."""
    for name, contract in CONTROL.FAMILIES.items():
        attested = CONTROL.read_json(contract["attested_inventory"])
        measured = CONTROL.live_scale(contract, attested)
        for field, pinned in contract["attested_scale"].items():
            assert measured[field] == pinned, (
                f"{name} attested_scale[{field}] is {pinned:,}, but the attested "
                f"inventory reports {measured[field]:,}"
            )


def test_per_task_dimensions_are_bound_by_the_specs_declared_caps():
    """Totals are not enough: a release can shrink overall while pushing one task
    past a cap the evidence never covered. The bound is the spec's own declared
    cap, read from the artifact the gate already verifies byte for byte."""
    for name, contract in CONTROL.FAMILIES.items():
        inventory = CONTROL.read_json(contract["inventory"])
        spec = CONTROL.read_json(contract["spec"])
        assert CONTROL.scale_envelope_errors(name, contract, inventory) == []
        section, key = contract["task_path"]
        for field, path in contract["task_caps"].items():
            cap = spec
            for step in path:
                cap = cap[step]
            assert isinstance(cap, int)
            burst = json.loads(json.dumps(inventory))
            first = burst[section][key][0]
            mutated = [
                candidate for candidate in CONTROL.TASK_SCALE_FIELDS[field]
                if candidate in first
            ]
            assert mutated, f"{name} tasks carry no field for {field}"
            for candidate in mutated:
                first[candidate] = cap + 1
            errors = CONTROL.scale_envelope_errors(name, contract, burst)
            assert any(field in error for error in errors), (
                f"{name} task 0 blew past {'.'.join(path)} and was still admitted"
            )


def test_the_addresses_anchor_reconciles_with_the_frozen_readiness_task_list():
    """A second, independent path to the same numbers: the readiness document
    carries the attested task list, so its totals must agree with the pins. This
    would survive even the attested inventory going missing."""
    contract = CONTROL.FAMILIES["addresses"]
    readiness = CONTROL.read_json(contract["readiness"])
    tasks = readiness["checks"]["canonical_inventory_identity"]["tasks"]
    anchor = contract["attested_scale"]
    assert len(tasks) == anchor["map_tasks"]
    assert sum(task["rows"] for task in tasks) == anchor["records"]
    assert sum(
        task["selected_uncompressed_bytes"] for task in tasks
    ) == anchor["selected_uncompressed_bytes"]


def test_the_request_survives_a_json_round_trip_unchanged():
    """`admit-dispatch` compares a `json.loads`'d dispatch request against a
    freshly derived one, so any value whose Python type does not survive JSON
    makes EVERY dispatch fail with 'dispatch request differs from the canonical
    reviewed request' -- and the message names nothing, so it reads like a
    tampered request rather than a tuple. Caught in a real dry-run by
    `task_caps`, whose paths were tuples and came back as lists."""
    report, admitted = CONTROL.prepare(arguments())
    assert admitted
    request = report["request"]
    assert json.loads(json.dumps(request)) == request
    # And the identity the gate actually enforces reproduces from the bytes.
    assert CONTROL.sha256_bytes(
        CONTROL.canonical(json.loads(json.dumps(request)))
    ) == report["request_sha256"]
