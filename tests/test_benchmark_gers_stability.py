"""Contract tests for the cross-release Places GERS stability probe."""

import importlib.util
import hashlib
import json
from pathlib import Path
import uuid

import pytest


SCRIPT = Path(__file__).parent.parent / "scripts" / "benchmark_gers_stability.py"
spec = importlib.util.spec_from_file_location("benchmark_gers_stability", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def gid(value):
    return str(uuid.UUID(int=value))


def row(dataset, record_id, identifier):
    return {"dataset": dataset, "record_id": record_id, "id": identifier}


def test_bridge_glob_validates_untrusted_path_components():
    assert probe.bridge_glob("2026-06-17.0", "meta").endswith(
        "/2026-06-17.0/dataset=meta/theme=places/type=place/*"
    )
    with pytest.raises(ValueError):
        probe.bridge_glob("latest", "meta")
    with pytest.raises(ValueError):
        probe.bridge_glob("2026-06-17.0", "../meta")


def test_memory_limit_contract_rejects_sql_fragments():
    assert probe.MEMORY_LIMIT_RE.fullmatch("8GB")
    assert not probe.MEMORY_LIMIT_RE.fullmatch("8gb")
    assert not probe.MEMORY_LIMIT_RE.fullmatch("8GB'; DROP TABLE old_mapping; --")


def test_stability_uses_dataset_and_record_id_as_the_source_identity():
    old = [
        row("meta", "same", gid(1)),
        row("Microsoft", "same", gid(2)),
    ]
    new = [
        row("meta", "same", gid(1)),
        row("Microsoft", "same", gid(3)),
    ]
    report = probe.summarize_bridge_rows(old, new)
    assert report["overall"]["comparable_shared_source_keys"] == 2
    assert report["overall"]["stable_gers_ids"] == 1
    assert report["overall"]["reassigned_gers_ids"] == 1
    assert report["by_dataset"]["meta"]["stable_gers_rate"] == 1.0
    assert report["by_dataset"]["Microsoft"]["stable_gers_rate"] == 0.0


def test_ambiguous_bridge_keys_are_reported_and_excluded_from_rate():
    old = [
        row("meta", "stable", gid(1)),
        row("meta", "ambiguous-old", gid(2)),
        row("meta", "ambiguous-old", gid(3)),
        row("meta", "removed", gid(4)),
        row("meta", "bad-id", "not-a-uuid"),
    ]
    new = [
        row("meta", "stable", gid(1)),
        row("meta", "ambiguous-old", gid(2)),
        row("meta", "added", gid(5)),
    ]
    result = probe.summarize_bridge_rows(old, new)["overall"]
    assert result == {
        "old_source_keys": 3,
        "new_source_keys": 3,
        "shared_source_keys": 2,
        "old_only_source_keys": 1,
        "new_only_source_keys": 1,
        "ambiguous_shared_source_keys": 1,
        "comparable_shared_source_keys": 1,
        "stable_gers_ids": 1,
        "reassigned_gers_ids": 0,
        "shared_source_key_rate_from_old": pytest.approx(2 / 3),
        "stable_gers_rate": 1.0,
    }


def test_reassigned_examples_are_deterministic_and_canonical():
    old = [row("meta", "z", gid(1)), row("meta", "a", gid(2))]
    new = [row("meta", "z", gid(3)), row("meta", "a", gid(4))]
    examples = probe.summarize_bridge_rows(old, new)["reassigned_examples"]
    assert [item["record_id"] for item in examples] == ["a", "z"]
    assert examples[0] == {
        "dataset": "meta",
        "record_id": "a",
        "old_gers_id": gid(2),
        "new_gers_id": gid(4),
    }


def test_probe_sql_binds_all_selected_dataset_paths_and_ambiguity_counts():
    sql = probe.build_probe_sql(
        "2026-06-17.0", "2026-07-22.0", ["meta", "Microsoft"]
    )
    assert "dataset=meta/theme=places/type=place/*" in sql
    assert "dataset=Microsoft/theme=places/type=place/*" in sql
    assert "count(DISTINCT id) AS id_count" in sql
    assert "try_cast(id AS UUID) IS NOT NULL" in sql


def test_sidecar_phase0_contract_uses_durable_gers_mapping_and_no_output_change():
    contract = json.loads(
        (
            Path(__file__).parent.parent
            / "benchmarks/gers-qid-sidecar-phase0-spec-v1.json"
        ).read_text()
    )
    assert contract["identity_model"]["durable_mapping_key"] == "gers_id"
    assert "membership/delta" in contract["identity_model"]["release_handling"]
    assert contract["phase0_gates"] == {
        "minimum_hand_checked_candidates": 200,
        "maximum_false_accepts": 0,
        "automatic_acceptance_requires_direct_external_id": True,
        "fuzzy_candidates_require_review": True,
        "coordinates_may_only_gate_match_distance": True,
        "unreviewed_matches_may_not_change_prominence": True,
        "construction_contract_movement": False,
    }
    assert "v5" in contract["phase1_stop_line"]


def test_committed_full_bridge_evidence_is_current_and_reconciles():
    evidence = json.loads(
        (
            Path(__file__).parent.parent
            / "benchmarks/2026-08-03-gers-place-stability-v1.json"
        ).read_text()
    )
    meta = evidence["meta"]
    assert meta["old_release"] == "2026-06-17.0"
    assert meta["new_release"] == "2026-07-22.0"
    assert meta["datasets"] == list(probe.DEFAULT_DATASETS)
    sql = probe.build_probe_sql(
        meta["old_release"], meta["new_release"], meta["datasets"]
    )
    assert meta["probe_sql_sha256"] == hashlib.sha256(sql.encode()).hexdigest()

    overall = evidence["measurement"]["overall"]
    by_dataset = evidence["measurement"]["by_dataset"]
    additive_fields = (
        "old_source_keys",
        "new_source_keys",
        "shared_source_keys",
        "old_only_source_keys",
        "new_only_source_keys",
        "ambiguous_shared_source_keys",
        "comparable_shared_source_keys",
        "stable_gers_ids",
        "reassigned_gers_ids",
    )
    for field in additive_fields:
        assert overall[field] == sum(value[field] for value in by_dataset.values())
    assert overall["stable_gers_ids"] == 76_440_029
    assert overall["reassigned_gers_ids"] == 4_784_834
    assert overall["stable_gers_rate"] == pytest.approx(0.94109151)
    assert overall["ambiguous_shared_source_keys"] == 0
