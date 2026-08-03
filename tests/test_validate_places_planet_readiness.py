from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
SPEC_PATH = ROOT / "benchmarks/places-construction-v1-evidence-spec-v4.json"
INVENTORY_PATH = ROOT / "benchmarks/places-construction-v1-data/inventory/places.json"
MODULE_SPEC = importlib.util.spec_from_file_location(
    "validate_places_planet_readiness_test",
    ROOT / "scripts/validate_places_planet_readiness.py",
)
assert MODULE_SPEC and MODULE_SPEC.loader
validator = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = validator
MODULE_SPEC.loader.exec_module(validator)


def bind_census_report(item):
    report = {
        key: value
        for key, value in item.items()
        if key not in ("task_index", "report_sha256")
    }
    item["report_sha256"] = validator.hashlib.sha256(
        (json.dumps(report, indent=2, sort_keys=True) + "\n").encode()
    ).hexdigest()


def complete_task_run(spec, inventory, task_index, binding):
    task = inventory["map_plan"]["tasks"][task_index]
    output = {
        "bytes": 1000,
        "sha256": "d" * 64,
        "records": task["expected_input_records"],
        "row_groups": task["row_groups"],
    }
    candidate = {
        "binding": binding,
        "output_sha256": "c" * 64,
        "resources": {
            "wall_seconds": 100,
            "peak_rss_bytes": 1_000_000_000,
            "peak_scratch_bytes": 1_000_000_000,
        },
    }
    return {
        "projection": {
            "schema": "overture-places-construction-v1-projection-report-v1",
            "identity": {
                "evidence_spec_sha256": validator.sha256_file(SPEC_PATH),
                "inventory_file_sha256": validator.sha256_file(INVENTORY_PATH),
                "inventory_sha256": inventory["inventory_sha256"],
                "task_index": task_index,
                "task_digest": task["task_digest"],
                "task_source_digest": task["source_digest"],
                "expected_input_records": task["expected_input_records"],
            },
            "output": output,
            "verified_input": dict(output),
            "resources": {"remote_read_bytes": 1000},
        },
        "baseline": {**binding, "elapsed_seconds": 250},
        "candidates": [candidate, dict(candidate)],
    }


def complete_evidence():
    spec = json.loads(SPEC_PATH.read_text())
    inventory = json.loads(INVENTORY_PATH.read_text())
    universe = validator.candidate_universe(inventory)
    census = []
    for position, task_index in enumerate(universe):
        task = inventory["map_plan"]["tasks"][task_index]
        admitted = 100
        emitted = 200 + position * 10
        item = {
            "task_index": task_index,
            "schema": "overture-places-construction-v1-census-v1",
            "identity": {
                "evidence_spec_sha256": validator.sha256_file(SPEC_PATH),
                "inventory_file_sha256": validator.sha256_file(INVENTORY_PATH),
                "inventory_sha256": inventory["inventory_sha256"],
                "task_index": task_index,
                "task_digest": task["task_digest"],
                "task_source_digest": task["source_digest"],
                "expected_input_records": task["expected_input_records"],
            },
            "transform_binary": {"path": "places-transform-v1", "sha256": "f" * 64},
            "parameters": {
                "duckdb_memory_limit": "2GB",
                "duckdb_threads": 2,
                "max_rss_bytes": 4_294_967_296,
                "max_scratch_bytes": 8_589_934_592,
                "wall_seconds": 600,
            },
            "census_evidence": {
                "peak_rss_bytes": 1_000_000_000,
                "peak_scratch_and_output_bytes": 1_000_000_000,
                "wall_seconds": 100,
            },
            "input": {
                "bytes": 1000,
                "rows": task["expected_input_records"],
                "sha256": "e" * 64,
            },
            "transform": {
                "schema": "overture-places-rust-transform-report-v1",
                "tokenizer_version": spec["tokenizer"]["version"],
                "admitted_features": admitted,
                "emitted_term_rows": emitted,
                "multilingual_features": 0,
                "cjk_features": 100 + position,
                "semantic_sum_a": "a" * 64,
                "semantic_sum_b": "b" * 64,
            },
            "metrics": {
                "maximum_spatial_cell_term_rows": 1000 + position,
                "term_rows_per_admitted_feature": emitted / admitted,
                "multilingual_cjk_features": 100 + position,
                "maximum_uuid_multiplicity": 2 + position,
                "duplicate_uuid_rows": 20 + position,
                "maximum_token_rows": 500 + position,
            },
        }
        bind_census_report(item)
        census.append(item)
    census_map = {item["task_index"]: item for item in census}
    roles = validator.select_roles(inventory, census_map)
    binding = {
        "emitted_term_rows": 100,
        "semantic_sum_a": "a" * 64,
        "semantic_sum_b": "b" * 64,
    }
    task_runs = {}
    for task_index in roles.values():
        task_runs[str(task_index)] = complete_task_run(
            spec, inventory, task_index, binding
        )
    role_tasks = list(roles.values())
    map_resources = [
        {
            "task_index": task_index,
            "transform": {
                "peak_rss_bytes": 1_000_000_000,
                "peak_scratch_bytes": 1_000_000_000,
                "wall_seconds": 100,
            },
            "construction": {
                "peak_rss_bytes": 1_000_000_000,
                "peak_scratch_and_output_bytes": 1_000_000_000,
                "wall_seconds": 100,
            },
        }
        for task_index in role_tasks
    ]
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
                "head_sharded": True,
                "worker_routed_query": True,
                "worker_head_query": True,
                "worker_local_decoder_evidence": True,
                "worker_entity_phrase_decoder": True,
                "resume_before_projection": True,
                "interruption_phases": [
                    "local_write",
                    "immutable_publish",
                    "before_marker",
                ],
                "head_result_cap": 10,
                "maximum_worker_index_probes": 32,
                "entity_phrase_admission": "prominence-primary-name-v1",
                "entity_phrase_head_index_entries": 100,
                "entity_phrase_head_records": 120,
                "entity_phrase_head_by_prefix": {
                    "e2:": {"index_entries": 50, "records": 60},
                    "e3:": {"index_entries": 50, "records": 60},
                },
                "routed_entity_phrase_index_entries": 0,
                "routed_entity_phrases_absent": True,
                "head_output_bytes": 1000,
                "map_stage_resources": map_resources,
                "observed": {"mapped_tasks": role_tasks},
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


def test_readiness_accepts_sub_gigabyte_census_memory_limit(tmp_path):
    spec, _, evidence = complete_evidence()
    evidence["census"][0]["parameters"]["duckdb_memory_limit"] = "512MB"
    bind_census_report(evidence["census"][0])
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))

    report = validator.validate(
        SPEC_PATH, INVENTORY_PATH, path, runtime=spec["runtime"]
    )

    assert report["ready"] is True, report["reasons"]


def test_memory_limit_parser_rejects_ambiguous_units():
    assert validator.memory_limit_bytes("512MB") == 512 * 1024**2
    assert validator.memory_limit_bytes("3GB") == 3 * 1024**3
    assert validator.memory_limit_bytes("0GB") is None
    assert validator.memory_limit_bytes("3GiB") is None


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


def test_readiness_fails_closed_on_missing_or_routed_entity_phrases(tmp_path):
    spec, _, evidence = complete_evidence()
    evidence["rehearsal"]["entity_phrase_head_records"] = 0
    evidence["rehearsal"]["routed_entity_phrase_index_entries"] = 1
    evidence["rehearsal"]["routed_entity_phrases_absent"] = False
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))

    report = validator.validate(
        SPEC_PATH, INVENTORY_PATH, path, runtime=spec["runtime"]
    )

    assert report["ready"] is False
    assert any("head entity-phrase record" in reason for reason in report["reasons"])
    assert any("entered routed" in reason for reason in report["reasons"])


def test_readiness_does_not_gate_on_duplicate_uuid_coverage(tmp_path):
    # Real 2026-06-17.0 data has zero duplicate UUIDs. Evidence shaped like that
    # (maximum_uuid_multiplicity == 1, duplicate_uuid_rows == 0 everywhere) must
    # still validate ready -- the duplicate gate was unclosable on real data and
    # is now a synthetic-fixture gate, recorded here only as an observation.
    spec, inventory, evidence = complete_evidence()
    for item in evidence["census"]:
        item["metrics"]["maximum_uuid_multiplicity"] = 1
        item["metrics"]["duplicate_uuid_rows"] = 0
        bind_census_report(item)
    # Zeroing the duplicate metrics changes the (metric-driven) role selection, so
    # recompute roles + task_runs to keep the rest of the evidence self-consistent;
    # the point under test is only that zero duplicate coverage does not block.
    census_map = {item["task_index"]: item for item in evidence["census"]}
    roles = validator.select_roles(inventory, census_map)
    evidence["roles"] = roles
    template = next(iter(evidence["task_runs"].values()))
    binding = {
        key: template["baseline"][key]
        for key in ("emitted_term_rows", "semantic_sum_a", "semantic_sum_b")
    }
    evidence["task_runs"] = {}
    for task_index in roles.values():
        evidence["task_runs"][str(task_index)] = complete_task_run(
            spec, inventory, task_index, binding
        )
    role_tasks = list(roles.values())
    evidence["rehearsal"]["observed"]["mapped_tasks"] = role_tasks
    evidence["rehearsal"]["map_stage_resources"] = [
        {**item, "task_index": task_index}
        for item, task_index in zip(
            evidence["rehearsal"]["map_stage_resources"], role_tasks, strict=True
        )
    ]
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


def test_readiness_rejects_stale_census_identity(tmp_path):
    spec, _, evidence = complete_evidence()
    evidence["census"][0]["identity"]["evidence_spec_sha256"] = "0" * 64
    bind_census_report(evidence["census"][0])
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))

    report = validator.validate(
        SPEC_PATH, INVENTORY_PATH, path, runtime=spec["runtime"]
    )

    assert report["ready"] is False
    assert any("census task" in reason and "spec binding" in reason for reason in report["reasons"])


def test_readiness_rejects_rehearsal_for_different_roles(tmp_path):
    spec, _, evidence = complete_evidence()
    evidence["rehearsal"]["observed"]["mapped_tasks"][-1] = 88
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))

    report = validator.validate(
        SPEC_PATH, INVENTORY_PATH, path, runtime=spec["runtime"]
    )

    assert report["ready"] is False
    assert any("mapped task set/order" in reason for reason in report["reasons"])


def test_readiness_rejects_map_stage_without_headroom(tmp_path):
    spec, _, evidence = complete_evidence()
    evidence["rehearsal"]["map_stage_resources"][0]["construction"][
        "peak_rss_bytes"
    ] = 3_866_140_672
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))

    report = validator.validate(
        SPEC_PATH, INVENTORY_PATH, path, runtime=spec["runtime"]
    )

    assert report["ready"] is False
    assert any("construction RSS lacks headroom" in reason for reason in report["reasons"])
