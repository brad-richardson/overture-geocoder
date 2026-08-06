"""Contracts for the Phase 0 golden review instrument and its validator.

These tests protect two properties: published review evidence stays bound to
its inputs, and the validator fails closed.
"""

import hashlib
import importlib.util
import json
from pathlib import Path
import uuid

import pytest


ROOT = Path(__file__).parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _load("build_sidecar_phase0_golden_review")
validator = _load("validate_sidecar_phase0_golden_review")
sidecar = _load("sidecar_phase0")


BENCHMARKS = ROOT / "benchmarks"
GOLDEN_SET = BENCHMARKS / "2026-08-04-sidecar-phase0-golden-review-set-v1.json"
VERDICTS = BENCHMARKS / "2026-08-04-sidecar-phase0-golden-verdicts-v1.json"
SHEET = BENCHMARKS / "2026-08-04-sidecar-phase0-golden-review-sheet-v1.md"
CANDIDATES = BENCHMARKS / "2026-08-03-sidecar-phase0-candidates-v1.json"
QUEUE = BENCHMARKS / "2026-08-03-sidecar-phase0-review-queue-v1.json"
PLACES = BENCHMARKS / "sidecar-phase0-foursquare-places-v1.jsonl"
ENTITIES = BENCHMARKS / "sidecar-phase0-foursquare-wikidata-entities-v1.jsonl"
COLLECTION = BENCHMARKS / "2026-08-03-sidecar-phase0-foursquare-collection-v1.json"
SPEC = BENCHMARKS / "gers-qid-sidecar-phase0-spec-v1.json"


def gid(value):
    return str(uuid.UUID(int=value))


def build_fixture(tmp_path, *, generated_at="2026-08-04T00:00:00Z"):
    """A tiny end-to-end instrument built from synthetic frozen inputs."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    places = [
        {
            "gers_id": gid(1),
            "names": ["Example Museum"],
            "latitude": 40.0,
            "longitude": -73.0,
            "country": "US",
            "sources": [{"dataset": "Foursquare", "record_id": "venue-a"}],
        },
        {
            "gers_id": gid(2),
            "names": ["Second Place"],
            "latitude": 41.0,
            "longitude": -73.0,
            "country": "US",
            "sources": [{"dataset": "Foursquare", "record_id": "venue-b"}],
        },
    ]
    entities = [
        {
            "wikidata_qid": "Q1",
            "names": ["Example Museum"],
            "latitude": 40.0,
            "longitude": -73.0,
            "coordinate_candidates": [{"latitude": 40.0, "longitude": -73.0}],
            "external_ids": {"Foursquare": ["venue-a"]},
        },
        {
            "wikidata_qid": "Q2",
            "names": ["Entirely Other Name"],
            "latitude": None,
            "longitude": None,
            "coordinate_candidates": [],
            "external_ids": {"Foursquare": ["venue-b"]},
        },
    ]
    places_path = tmp_path / "places.jsonl"
    entities_path = tmp_path / "entities.jsonl"
    places_path.write_bytes(b"".join(builder.canonical_json(row) for row in places))
    entities_path.write_bytes(
        b"".join(builder.canonical_json(row) for row in entities)
    )
    candidates = sidecar.build_candidates(
        place_rows=places,
        entity_rows=entities,
        release="2026-06-17.0",
        matcher_version="phase0-v1",
        max_distance_km=1.0,
        places_sha256=builder.sha256_file(places_path),
        entities_sha256=builder.sha256_file(entities_path),
    )
    candidates_path = tmp_path / "candidates.json"
    candidates_path.write_bytes(builder.canonical_json(candidates))
    queue = sidecar.build_review_queue(
        candidates,
        candidate_sha256=builder.sha256_file(candidates_path),
        limit=0,
        control_quota=0,
    )
    queue_path = tmp_path / "queue.json"
    queue_path.write_bytes(builder.canonical_json(queue))
    collection_path = tmp_path / "collection.json"
    collection_path.write_bytes(builder.canonical_json({
        "schema": "gers-qid-sidecar-foursquare-collection-v1",
        "meta": {
            "wikidata_property_label": "Foursquare City Guide venue ID",
            "wikidata_sparql_endpoint": "https://query.wikidata.org/sparql",
            "wikidata_query_sha256": "c" * 64,
            "wikidata_snapshot_sha256": "d" * 64,
        },
    }))
    spec_path = tmp_path / "spec.json"
    spec_path.write_bytes(builder.canonical_json({"schema": "spec"}))
    set_path = tmp_path / "golden.json"
    verdict_path = tmp_path / "verdicts.json"
    sheet_path = tmp_path / "sheet.md"
    code = builder.main([
        "--candidates", str(candidates_path),
        "--review-queue", str(queue_path),
        "--places", str(places_path),
        "--wikidata-entities", str(entities_path),
        "--collection-report", str(collection_path),
        "--phase0-spec", str(spec_path),
        "--output-set", str(set_path),
        "--output-verdicts", str(verdict_path),
        "--output-sheet", str(sheet_path),
        "--generated-at", generated_at,
    ])
    assert code == 0
    return {
        "candidates": candidates_path,
        "queue": queue_path,
        "set": set_path,
        "verdicts": verdict_path,
        "sheet": sheet_path,
    }


def write_verdicts(path, rows, *, meta_overrides=None):
    payload = json.loads(path.read_text())
    payload["verdicts"] = rows
    payload["meta"].update(meta_overrides or {})
    path.write_bytes(builder.canonical_json(payload))


def verdict(decision_id, value, note="checked"):
    return {
        "decision_id": decision_id,
        "verdict": value,
        "reviewer": "brad",
        "reviewed_at": "2026-08-04T12:00:00Z",
        "note": note,
    }


def run_validator(paths, **kwargs):
    argv = [
        "--golden-set", str(paths["set"]),
        "--verdicts", str(paths["verdicts"]),
        "--candidates", str(paths["candidates"]),
    ]
    output = kwargs.get("output")
    if output is not None:
        argv.extend(["--output", str(output)])
    return validator.main(argv)


def report_for(paths, tmp_path, name="report.json"):
    output = tmp_path / name
    code = run_validator(paths, output=output)
    return code, json.loads(output.read_text())


# --- the instrument decides nothing -----------------------------------------


def test_generated_set_is_deterministic_and_decides_nothing(tmp_path):
    first = build_fixture(tmp_path / "a")
    second = build_fixture(tmp_path / "b")
    assert first["set"].read_bytes() == second["set"].read_bytes()
    assert first["sheet"].read_bytes() == second["sheet"].read_bytes()
    golden = json.loads(first["set"].read_text())
    assert golden["schema"] == builder.GOLDEN_SCHEMA
    assert golden["meta"]["eligible_for_prominence"] is False
    assert golden["meta"]["construction_contract_movement"] is False
    for decision in golden["decisions"]:
        assert decision["provisional"]["eligible_for_prominence"] is False
        assert decision["provisional"]["review_status"] == "unreviewed"
        assert "verdict" not in decision
    assert json.loads(first["verdicts"].read_text())["verdicts"] == []


def test_decision_rows_carry_the_full_evidence_bundle(tmp_path):
    paths = build_fixture(tmp_path)
    golden = json.loads(paths["set"].read_text())
    by_id = {
        decision["overture"]["gers_id"]: decision for decision in golden["decisions"]
    }
    clean = by_id[gid(1)]
    assert clean["risk_class"] == "clean_direct_control"
    assert clean["provisional"]["decision"] == "accepted"
    assert clean["provisional"]["rule_id"] == "direct_source_wikidata_id.unambiguous"
    assert clean["provisional"]["rule_statement"]
    assert clean["overture"]["sources"] == [
        {"dataset": "Foursquare", "record_id": "venue-a"}
    ]
    assert clean["overture"]["categories"] is None
    assert clean["overture"]["categories_null_reason"]
    assert clean["wikidata"]["description"] is None
    assert clean["wikidata"]["aliases_null_reason"]
    assert clean["wikidata"]["p1968_claims"] == [{
        "property": "P1968",
        "property_label": "Foursquare City Guide venue ID",
        "value": "venue-a",
        "matches_overture_source_record": True,
    }]
    assert clean["wikidata"]["claim_provenance"]["snapshot_sha256"] == "d" * 64
    assert clean["comparison"]["distance_km"] == 0.0
    assert clean["comparison"]["distance_null_reason"] is None
    assert clean["review_urls"]


def test_missing_distance_states_an_explicit_null_reason(tmp_path):
    paths = build_fixture(tmp_path)
    golden = json.loads(paths["set"].read_text())
    missing = next(
        decision
        for decision in golden["decisions"]
        if decision["comparison"]["distance_km"] is None
    )
    assert missing["comparison"]["distance_null_reason"]["code"] == (
        "wikidata_coordinate_absent"
    )
    assert missing["comparison"]["distance_null_reason"]["explanation"]
    flags = {flag["flag"]: flag["explanation"] for flag in missing["risk_flags"]}
    assert "distance_missing" in flags
    assert all(explanation.strip() for explanation in flags.values())


def test_ambiguous_wikidata_coordinates_are_reported_as_ambiguous(tmp_path):
    paths = build_fixture(tmp_path)
    golden = json.loads(paths["set"].read_text())
    decision = golden["decisions"][0]
    place = {"latitude": 1.0, "longitude": 2.0}
    entity = {
        "coordinate_candidates": [
            {"latitude": 1.0, "longitude": 2.0},
            {"latitude": 3.0, "longitude": 4.0},
        ]
    }
    reason = builder._distance_null_reason(place, entity)
    assert reason["code"] == "wikidata_coordinate_ambiguous"
    assert "2 distinct" in reason["explanation"]
    assert decision["decision_id"].startswith("gqd-")


def test_review_order_matches_the_frozen_queue_exactly(tmp_path):
    paths = build_fixture(tmp_path)
    queue = json.loads(paths["queue"].read_text())
    golden = json.loads(paths["set"].read_text())
    assert [decision["candidate_id"] for decision in golden["decisions"]] == [
        row["candidate_id"] for row in queue["queue"]
    ]
    assert [decision["review_order"] for decision in golden["decisions"]] == list(
        range(1, len(queue["queue"]) + 1)
    )


def test_build_refuses_a_queue_bound_to_another_candidate_set(tmp_path):
    paths = build_fixture(tmp_path)
    queue = json.loads(paths["queue"].read_text())
    queue["meta"]["candidate_set_sha256"] = "0" * 64
    paths["queue"].write_bytes(builder.canonical_json(queue))
    code = builder.main([
        "--candidates", str(paths["candidates"]),
        "--review-queue", str(paths["queue"]),
        "--places", str(tmp_path / "places.jsonl"),
        "--wikidata-entities", str(tmp_path / "entities.jsonl"),
        "--collection-report", str(tmp_path / "collection.json"),
        "--phase0-spec", str(tmp_path / "spec.json"),
        "--output-set", str(tmp_path / "other.json"),
        "--output-verdicts", str(tmp_path / "other-verdicts.json"),
        "--output-sheet", str(tmp_path / "other.md"),
        "--generated-at", "2026-08-04T00:00:00Z",
    ])
    assert code == 2


def test_rebuild_refuses_to_discard_recorded_verdicts(tmp_path):
    paths = build_fixture(tmp_path)
    golden = json.loads(paths["set"].read_text())
    write_verdicts(
        paths["verdicts"],
        [verdict(golden["decisions"][0]["decision_id"], "accept")],
    )
    code = builder.main([
        "--candidates", str(paths["candidates"]),
        "--review-queue", str(paths["queue"]),
        "--places", str(tmp_path / "places.jsonl"),
        "--wikidata-entities", str(tmp_path / "entities.jsonl"),
        "--collection-report", str(tmp_path / "collection.json"),
        "--phase0-spec", str(tmp_path / "spec.json"),
        "--output-set", str(paths["set"]),
        "--output-verdicts", str(paths["verdicts"]),
        "--output-sheet", str(paths["sheet"]),
        "--generated-at", "2026-08-04T00:00:00Z",
    ])
    assert code == 2
    assert json.loads(paths["verdicts"].read_text())["verdicts"]


# --- the validator fails closed ---------------------------------------------


def test_empty_verdicts_are_gate_not_met_never_passed(tmp_path):
    paths = build_fixture(tmp_path)
    code, report = report_for(paths, tmp_path)
    assert code == validator.EXIT_GATE_NOT_MET
    assert report["gate_met"] is False
    assert report["integrity"]["ok"] is True
    assert report["coverage"]["decided"] == 0
    assert report["effect"]["eligible_for_prominence"] is False
    assert report["phase0_gate"]["blockers"]


def test_partial_review_is_resumable_and_reports_coverage(tmp_path):
    paths = build_fixture(tmp_path)
    golden = json.loads(paths["set"].read_text())
    first = golden["decisions"][0]["decision_id"]
    write_verdicts(paths["verdicts"], [verdict(first, "accept")])
    code, report = report_for(paths, tmp_path)
    assert code == validator.EXIT_GATE_NOT_MET
    assert report["coverage"]["decided"] == 1
    assert report["coverage"]["remaining_for_gate"] == (
        report["coverage"]["minimum_required_decisions"] - 1
    )
    assert first not in report["coverage"]["undecided_decision_ids"]
    assert sum(
        bucket["decided"]
        for bucket in report["coverage"]["per_risk_class"].values()
    ) == 1


def test_full_review_with_no_false_accepts_meets_the_gate(tmp_path):
    paths = build_fixture(tmp_path)
    golden = json.loads(paths["set"].read_text())
    rows = [
        verdict(decision["decision_id"], "accept")
        for decision in golden["decisions"]
    ]
    write_verdicts(paths["verdicts"], rows)
    output = tmp_path / "full.json"
    code = validator.main([
        "--golden-set", str(paths["set"]),
        "--verdicts", str(paths["verdicts"]),
        "--candidates", str(paths["candidates"]),
        "--minimum-decisions", str(len(rows)),
        "--output", str(output),
    ])
    report = json.loads(output.read_text())
    assert code == validator.EXIT_GATE_MET
    assert report["gate_met"] is True
    assert report["phase0_gate"]["false_accepts"] == 0
    assert report["effect"]["eligible_for_prominence"] is False


def test_a_rejected_provisional_accept_is_a_false_accept(tmp_path):
    paths = build_fixture(tmp_path)
    golden = json.loads(paths["set"].read_text())
    accepted = next(
        decision
        for decision in golden["decisions"]
        if decision["provisional"]["decision"] == "accepted"
    )
    rows = [
        verdict(
            decision["decision_id"],
            "reject" if decision is accepted else "accept",
            note="not the same venue",
        )
        for decision in golden["decisions"]
    ]
    write_verdicts(paths["verdicts"], rows)
    output = tmp_path / "false.json"
    code = validator.main([
        "--golden-set", str(paths["set"]),
        "--verdicts", str(paths["verdicts"]),
        "--minimum-decisions", str(len(rows)),
        "--output", str(output),
    ])
    report = json.loads(output.read_text())
    assert code == validator.EXIT_GATE_NOT_MET
    assert report["gate_met"] is False
    assert report["phase0_gate"]["false_accepts"] == 1
    assert report["phase0_gate"]["false_accept_decision_ids"] == [
        accepted["decision_id"]
    ]


def test_needs_more_evidence_does_not_count_toward_the_gate(tmp_path):
    paths = build_fixture(tmp_path)
    golden = json.loads(paths["set"].read_text())
    rows = [
        verdict(decision["decision_id"], "needs_more_evidence", note="open question")
        for decision in golden["decisions"]
    ]
    write_verdicts(paths["verdicts"], rows)
    output = tmp_path / "open.json"
    code = validator.main([
        "--golden-set", str(paths["set"]),
        "--verdicts", str(paths["verdicts"]),
        "--minimum-decisions", str(len(rows)),
        "--output", str(output),
    ])
    report = json.loads(output.read_text())
    assert code == validator.EXIT_GATE_NOT_MET
    assert report["gate_met"] is False
    assert report["coverage"]["decided"] == 0
    assert report["coverage"]["verdict_counts"]["needs_more_evidence"] == len(rows)


def test_changed_golden_set_invalidates_recorded_verdicts(tmp_path):
    paths = build_fixture(tmp_path)
    golden = json.loads(paths["set"].read_text())
    write_verdicts(
        paths["verdicts"],
        [
            verdict(decision["decision_id"], "accept")
            for decision in golden["decisions"]
        ],
    )
    golden["meta"]["generated_at"] = "2026-08-05T00:00:00Z"
    paths["set"].write_bytes(builder.canonical_json(golden))
    output = tmp_path / "drift.json"
    code = validator.main([
        "--golden-set", str(paths["set"]),
        "--verdicts", str(paths["verdicts"]),
        "--minimum-decisions", "1",
        "--output", str(output),
    ])
    report = json.loads(output.read_text())
    assert code == validator.EXIT_INTEGRITY_FAILURE
    assert report["gate_met"] is False
    assert any("golden review set" in error for error in report["integrity"]["errors"])


def test_candidate_set_binding_is_enforced(tmp_path):
    paths = build_fixture(tmp_path)
    other = tmp_path / "other-candidates.json"
    payload = json.loads(paths["candidates"].read_text())
    payload["meta"]["matcher_version"] = "phase0-v2"
    other.write_bytes(builder.canonical_json(payload))
    output = tmp_path / "binding.json"
    code = validator.main([
        "--golden-set", str(paths["set"]),
        "--verdicts", str(paths["verdicts"]),
        "--candidates", str(other),
        "--output", str(output),
    ])
    report = json.loads(output.read_text())
    assert code == validator.EXIT_INTEGRITY_FAILURE
    assert report["integrity"]["ok"] is False


@pytest.mark.parametrize(
    "row",
    [
        {"decision_id": "gqd-" + "0" * 24, "verdict": "accept",
         "reviewer": "brad", "reviewed_at": "2026-08-04T12:00:00Z", "note": ""},
        {"verdict": "accept", "reviewer": "brad",
         "reviewed_at": "2026-08-04T12:00:00Z", "note": ""},
    ],
)
def test_unknown_decision_ids_fail_integrity(tmp_path, row):
    paths = build_fixture(tmp_path)
    write_verdicts(paths["verdicts"], [row])
    code, report = report_for(paths, tmp_path)
    assert code == validator.EXIT_INTEGRITY_FAILURE
    assert report["integrity"]["ok"] is False
    assert report["gate_met"] is False


def test_malformed_verdict_rows_fail_integrity(tmp_path):
    paths = build_fixture(tmp_path)
    golden = json.loads(paths["set"].read_text())
    identifier = golden["decisions"][0]["decision_id"]
    cases = [
        verdict(identifier, "maybe"),
        {**verdict(identifier, "accept"), "reviewer": "  "},
        {**verdict(identifier, "accept"), "reviewed_at": "2026-08-04 12:00"},
        {**verdict(identifier, "accept"), "reviewed_at": "2026-08-04T12:00:00"},
        {**verdict(identifier, "reject"), "note": ""},
    ]
    for index, row in enumerate(cases):
        write_verdicts(paths["verdicts"], [row])
        code, report = report_for(paths, tmp_path, name=f"bad-{index}.json")
        assert code == validator.EXIT_INTEGRITY_FAILURE, row
        assert report["integrity"]["ok"] is False
        assert report["gate_met"] is False


def test_repeated_decision_ids_fail_integrity(tmp_path):
    paths = build_fixture(tmp_path)
    golden = json.loads(paths["set"].read_text())
    identifier = golden["decisions"][0]["decision_id"]
    write_verdicts(
        paths["verdicts"],
        [verdict(identifier, "accept"), verdict(identifier, "reject", note="no")],
    )
    code, report = report_for(paths, tmp_path)
    assert code == validator.EXIT_INTEGRITY_FAILURE
    assert report["gate_met"] is False


# --- the frozen 200-decision artifact ---------------------------------------


def test_published_golden_set_matches_the_frozen_inputs():
    golden = json.loads(GOLDEN_SET.read_text())
    queue = json.loads(QUEUE.read_text())
    bindings = golden["meta"]["input_bindings"]
    assert golden["summary"]["decisions"] == 200
    assert bindings["candidate_set_sha256"] == builder.sha256_file(CANDIDATES)
    assert bindings["review_queue_sha256"] == builder.sha256_file(QUEUE)
    assert bindings["places_sha256"] == builder.sha256_file(PLACES)
    assert bindings["wikidata_entities_sha256"] == builder.sha256_file(ENTITIES)
    assert bindings["collection_report_sha256"] == builder.sha256_file(COLLECTION)
    assert bindings["phase0_spec_sha256"] == builder.sha256_file(SPEC)
    assert [decision["candidate_id"] for decision in golden["decisions"]] == [
        row["candidate_id"] for row in queue["queue"]
    ]
    assert golden["summary"]["risk_class_counts"] == {
        "clean_direct_control": 50,
        "direct_identifier_conflict": 2,
        "distance_missing": 21,
        "distance_over_gate": 5,
        "no_normalized_name_overlap": 122,
    }
    assert golden["summary"]["risk_flag_counts"]["no_normalized_name_overlap"] == 134


def test_published_partial_verdict_file_is_bound_and_fails_closed(tmp_path):
    verdicts = json.loads(VERDICTS.read_text())
    assert len(verdicts["verdicts"]) == 58
    assert verdicts["meta"]["golden_review_set_sha256"] == builder.sha256_file(
        GOLDEN_SET
    )
    assert verdicts["meta"]["expected_decisions"] == 200
    output = tmp_path / "published.json"
    code = validator.main([
        "--golden-set", str(GOLDEN_SET),
        "--verdicts", str(VERDICTS),
        "--candidates", str(CANDIDATES),
        "--output", str(output),
    ])
    report = json.loads(output.read_text())
    assert code == validator.EXIT_GATE_NOT_MET
    assert report["integrity"]["ok"] is True
    assert report["gate_met"] is False
    assert report["coverage"]["decided"] == 57
    assert report["coverage"]["remaining_for_gate"] == 143
    assert report["coverage"]["verdict_counts"] == {
        "accept": 56,
        "needs_more_evidence": 1,
        "reject": 1,
    }


def test_published_sheet_orders_risk_first_and_controls_last():
    text = SHEET.read_text(encoding="utf-8")
    positions = [
        text.index(f"## {builder.RISK_CLASS_TITLES[risk_class]} (")
        for risk_class in builder.RISK_ORDER
        if f"## {builder.RISK_CLASS_TITLES[risk_class]} (" in text
    ]
    assert positions == sorted(positions)
    assert text.rstrip().endswith("4bcda17c937ca59309fdac92")
    assert "eligible_for_prominence" not in text
    assert text.count("### ") == 200


def test_no_artifact_claims_the_gate_is_met():
    for path in (GOLDEN_SET, VERDICTS):
        payload = json.loads(path.read_text())
        text = json.dumps(payload)
        assert '"gate_met": true' not in text
        assert '"eligible_for_prominence":true' not in json.dumps(
            payload, separators=(",", ":")
        )
    assert hashlib.sha256(GOLDEN_SET.read_bytes()).hexdigest() == builder.sha256_file(
        GOLDEN_SET
    )
