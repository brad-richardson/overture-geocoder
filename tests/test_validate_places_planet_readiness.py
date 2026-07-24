from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
SPEC_PATH = ROOT / "benchmarks/places-construction-v1-evidence-spec.json"
INVENTORY_PATH = ROOT / "benchmarks/places-construction-v1-data/inventory/places.json"
MODULE_SPEC = importlib.util.spec_from_file_location(
    "validate_places_planet_readiness_test",
    ROOT / "scripts/validate_places_planet_readiness.py",
)
assert MODULE_SPEC and MODULE_SPEC.loader
validator = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = validator
MODULE_SPEC.loader.exec_module(validator)


def complete_evidence():
    spec = json.loads(SPEC_PATH.read_text())
    inventory = json.loads(INVENTORY_PATH.read_text())
    universe = validator.candidate_universe(inventory)
    census = []
    for position, task_index in enumerate(universe):
        census.append(
            {
                "task_index": task_index,
                "metrics": {
                    "maximum_spatial_cell_term_rows": 1000 + position,
                    "term_rows_per_admitted_feature": 2 + position / 10,
                    "multilingual_cjk_features": 100 + position,
                    "maximum_uuid_multiplicity": 2 + position,
                    "duplicate_uuid_rows": 20 + position,
                    "maximum_token_rows": 500 + position,
                },
            }
        )
    census_map = {item["task_index"]: item for item in census}
    roles = validator.select_roles(inventory, census_map)
    binding = {
        "emitted_term_rows": 100,
        "semantic_sum_a": "a" * 64,
        "semantic_sum_b": "b" * 64,
    }
    task_runs = {}
    for task_index in roles.values():
        task = inventory["map_plan"]["tasks"][task_index]
        candidate = {
            "binding": binding,
            "output_sha256": "c" * 64,
            "resources": {
                "wall_seconds": 100,
                "peak_rss_bytes": 1_000_000_000,
                "peak_scratch_bytes": 1_000_000_000,
            },
        }
        task_runs[str(task_index)] = {
            "projection": {
                "identity": {"task_digest": task["task_digest"]},
                "output": {"bytes": 1000},
                "resources": {"remote_read_bytes": 1000},
            },
            "baseline": {**binding, "elapsed_seconds": 250},
            "candidates": [candidate, dict(candidate)],
        }
    return (
        spec,
        inventory,
        {
            "schema": "overture-places-construction-v1-scale-evidence-v1",
            "evidence_spec_sha256": validator.sha256_file(SPEC_PATH),
            "inventory_file_sha256": validator.sha256_file(INVENTORY_PATH),
            "inventory_sha256": inventory["inventory_sha256"],
            "schema_fingerprint_sha256": inventory["schema_contract"][
                "fingerprint_sha256"
            ],
            "candidate_universe": universe,
            "census": census,
            "roles": roles,
            "task_runs": task_runs,
            "rehearsal": {
                "logical_tasks": 7,
                "packs": 7,
                "parquet_row_groups": 14,
                "partitions": 8,
                "maximum_selective_amplification": 2.0,
                "exact_reconciliation": True,
                "overlap_reconciliation": True,
                "adaptive_subdivision": True,
                "multi_task_fan_in": True,
                "routed_verified": True,
                "head_verified": True,
                "worker_routed_query": True,
                "worker_head_query": True,
                "resume_before_projection": True,
                "interruption_phases": [
                    "local_write",
                    "immutable_publish",
                    "before_marker",
                ],
                "head_result_cap": 10,
                "maximum_worker_index_probes": 32,
            },
        },
    )


def test_readiness_accepts_only_complete_frozen_evidence(tmp_path):
    spec, _, evidence = complete_evidence()
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))
    report = validator.validate(
        SPEC_PATH, INVENTORY_PATH, path, runtime=spec["runtime"]
    )
    assert report["ready"] is True
    assert report["reasons"] == []


def test_readiness_fails_closed_on_missing_or_nondeterministic_evidence(tmp_path):
    spec, _, evidence = complete_evidence()
    role_task = next(iter(evidence["task_runs"].values()))
    role_task["candidates"][1]["output_sha256"] = "d" * 64
    del evidence["rehearsal"]["worker_head_query"]
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))
    report = validator.validate(
        SPEC_PATH, INVENTORY_PATH, path, runtime=spec["runtime"]
    )
    assert report["ready"] is False
    assert any("not deterministic" in reason for reason in report["reasons"])
    assert any("Worker head" in reason for reason in report["reasons"])


def test_readiness_does_not_gate_on_duplicate_uuid_coverage(tmp_path):
    # Real 2026-06-17.0 data has zero duplicate UUIDs. Evidence shaped like that
    # (maximum_uuid_multiplicity == 1, duplicate_uuid_rows == 0 everywhere) must
    # still validate ready -- the duplicate gate was unclosable on real data and
    # is now a synthetic-fixture gate, recorded here only as an observation.
    spec, inventory, evidence = complete_evidence()
    for item in evidence["census"]:
        item["metrics"]["maximum_uuid_multiplicity"] = 1
        item["metrics"]["duplicate_uuid_rows"] = 0
    # Zeroing the duplicate metrics changes the (metric-driven) role selection, so
    # recompute roles + task_runs to keep the rest of the evidence self-consistent;
    # the point under test is only that zero duplicate coverage does not block.
    census_map = {item["task_index"]: item for item in evidence["census"]}
    roles = validator.select_roles(inventory, census_map)
    evidence["roles"] = roles
    template = next(iter(evidence["task_runs"].values()))
    binding = template["baseline"]
    candidate = template["candidates"][0]
    evidence["task_runs"] = {}
    for task_index in roles.values():
        task = inventory["map_plan"]["tasks"][task_index]
        evidence["task_runs"][str(task_index)] = {
            "projection": {
                "identity": {"task_digest": task["task_digest"]},
                "output": {"bytes": 1000},
                "resources": {"remote_read_bytes": 1000},
            },
            "baseline": dict(binding),
            "candidates": [dict(candidate), dict(candidate)],
        }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))
    report = validator.validate(
        SPEC_PATH, INVENTORY_PATH, path, runtime=spec["runtime"]
    )
    assert report["ready"] is True, report["reasons"]
    assert not any("duplicate" in reason for reason in report["reasons"])
    assert report["observations"]["duplicate_uuid"] == {
        "maximum_uuid_multiplicity": 1,
        "duplicate_uuid_rows_total": 0,
    }


def test_readiness_ipc_cap_drift_guard(tmp_path, monkeypatch):
    # If the construction module's single IPC cap constant drifts from the frozen
    # evidence-spec value, readiness must fail closed.
    spec, _, evidence = complete_evidence()
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))
    construction = validator.load_construction_module()
    monkeypatch.setattr(construction, "MAX_IPC_BATCH_ROWS", 12345, raising=True)
    monkeypatch.setattr(
        validator, "load_construction_module", lambda: construction, raising=True
    )
    report = validator.validate(
        SPEC_PATH, INVENTORY_PATH, path, runtime=spec["runtime"]
    )
    assert report["ready"] is False
    assert any("IPC batch cap" in reason for reason in report["reasons"])
