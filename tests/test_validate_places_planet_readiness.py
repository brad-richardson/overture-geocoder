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
