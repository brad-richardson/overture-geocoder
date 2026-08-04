"""Contracts for the non-promoting GERS-to-QID Phase 0 tooling."""

import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import uuid

import pytest


ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "sidecar_phase0.py"
spec = importlib.util.spec_from_file_location("sidecar_phase0", SCRIPT)
sidecar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sidecar)


def gid(value):
    return str(uuid.UUID(int=value))


def place(identifier, name, *, lat=40.0, lon=-73.0, sources=None, country="US"):
    return {
        "gers_id": gid(identifier),
        "names": [name],
        "latitude": lat,
        "longitude": lon,
        "country": country,
        "sources": sources or [],
    }


def entity(qid, name, *, lat=40.0, lon=-73.0, external_ids=None, country="US"):
    return {
        "wikidata_qid": qid,
        "names": [name],
        "latitude": lat,
        "longitude": lon,
        "country": country,
        "external_ids": external_ids or {},
    }


def candidates(places, entities):
    return sidecar.build_candidates(
        place_rows=places,
        entity_rows=entities,
        release="2026-06-17.0",
        matcher_version="phase0-v1",
        max_distance_km=1.0,
        places_sha256="a" * 64,
        entities_sha256="b" * 64,
    )


def test_direct_source_identifier_is_the_only_automatic_acceptance():
    result = candidates(
        [place(1, "Example Museum", sources=[{
            "dataset": "Foursquare", "record_id": "venue-1"
        }])],
        [entity("Q42", "Different label", external_ids={
            "Foursquare": ["venue-1"]
        })],
    )
    row = result["candidates"][0]
    assert row["decision"] == "accepted"
    assert row["match_method"] == "direct_source_wikidata_id"
    assert row["automatic_acceptance"] is True
    assert row["eligible_for_prominence"] is False
    assert result["meta"]["construction_contract_movement"] is False


def test_conflicting_direct_identifiers_fail_into_review():
    result = candidates(
        [place(1, "Example", sources=[
            {"dataset": "Foursquare", "record_id": "venue-1"},
            {"dataset": "Other", "record_id": "other-1"},
        ])],
        [
            entity("Q1", "Example", external_ids={"Foursquare": ["venue-1"]}),
            entity("Q2", "Example", external_ids={"Other": ["other-1"]}),
        ],
    )
    assert len(result["candidates"]) == 2
    assert {row["decision"] for row in result["candidates"]} == {"needs_review"}
    assert not any(row["automatic_acceptance"] for row in result["candidates"])
    assert result["summary"]["direct_identifier_conflicts"] == 1


def test_one_source_identity_assigned_to_multiple_gers_is_not_autoaccepted():
    shared_source = [{"dataset": "Foursquare", "record_id": "venue-1"}]
    result = candidates(
        [place(1, "First", sources=shared_source), place(2, "Second", sources=shared_source)],
        [entity("Q1", "First", external_ids={"Foursquare": ["venue-1"]})],
    )
    assert len(result["candidates"]) == 2
    assert not any(row["automatic_acceptance"] for row in result["candidates"])
    assert all(row["decision"] == "needs_review" for row in result["candidates"])
    assert all(
        row["match_evidence"]["source_identities_with_multiple_gers"]
        for row in result["candidates"]
    )


def test_name_distance_candidate_requires_review_and_coordinates_only_gate():
    result = candidates(
        [place(1, "Café Étoile", lat=48.0, lon=2.0)],
        [
            entity("Q1", "Cafe Etoile", lat=48.001, lon=2.0),
            entity("Q2", "Cafe Etoile", lat=49.0, lon=2.0),
            entity("Q3", "Cafe Etoile", lat=48.001, lon=2.0, country="GB"),
        ],
    )
    assert len(result["candidates"]) == 1
    row = result["candidates"][0]
    assert row["wikidata_qid"] == "Q1"
    assert row["match_method"] == "reviewed_name_distance"
    assert row["decision"] == "needs_review"
    assert row["automatic_acceptance"] is False
    assert row["match_evidence"]["distance_km"] < 1.0


def audit_document(candidate_set, reviews):
    candidate_sha = hashlib.sha256(sidecar.canonical_json(candidate_set)).hexdigest()
    return candidate_sha, {
        "schema": sidecar.AUDIT_SCHEMA,
        "candidate_set_sha256": candidate_sha,
        "reviews": reviews,
    }


def review(candidate_id, verdict):
    return {
        "candidate_id": candidate_id,
        "reviewer": "independent-reviewer",
        "reviewed_at": "2026-08-03T00:00:00Z",
        "verdict": verdict,
        "evidence": ["https://www.wikidata.org/wiki/Q1"],
        "notes": "fixture review",
    }


def test_audit_counts_a_wrong_auto_accept_and_fails_closed():
    candidate_set = candidates(
        [place(1, "Wrong", sources=[{
            "dataset": "Foursquare", "record_id": "venue-1"
        }])],
        [entity("Q1", "Other", external_ids={"Foursquare": ["venue-1"]})],
    )
    row = candidate_set["candidates"][0]
    candidate_sha, audit = audit_document(
        candidate_set, [review(row["candidate_id"], "different_entity")]
    )
    ledger, report = sidecar.validate_audit(
        candidate_set,
        audit,
        candidate_sha256=candidate_sha,
        minimum_reviews=1,
    )
    assert report["ready"] is False
    assert report["gates"]["false_accepts"] == 1
    assert ledger["mappings"][0]["decision"] == "rejected"
    assert ledger["mappings"][0]["eligible_for_prominence"] is False


def test_reviewed_fuzzy_match_can_enter_ledger_but_unreviewed_cannot():
    candidate_set = candidates(
        [place(1, "Same", lat=10.0, lon=10.0), place(2, "Other")],
        [entity("Q1", "Same", lat=10.001, lon=10.0)],
    )
    row = candidate_set["candidates"][0]
    candidate_sha, audit = audit_document(
        candidate_set, [review(row["candidate_id"], "same_entity")]
    )
    ledger, report = sidecar.validate_audit(
        candidate_set,
        audit,
        candidate_sha256=candidate_sha,
        minimum_reviews=1,
    )
    assert report["ready"] is True
    assert ledger["mappings"][0]["decision"] == "accepted"
    assert ledger["mappings"][0]["review_status"] == "independently_reviewed"
    assert ledger["mappings"][0]["eligible_for_prominence"] is True


def test_audit_is_bound_to_exact_candidate_bytes():
    candidate_set = candidates([place(1, "Same")], [entity("Q1", "Same")])
    _candidate_sha, audit = audit_document(candidate_set, [])
    with pytest.raises(ValueError, match="not bound"):
        sidecar.validate_audit(
            candidate_set,
            audit,
            candidate_sha256="f" * 64,
            minimum_reviews=1,
        )


def test_review_queue_is_risk_first_and_bound_to_candidate_bytes():
    candidate_set = candidates(
        [
            place(1, "Clean", sources=[{
                "dataset": "Foursquare", "record_id": "clean"
            }]),
            place(2, "Far", lat=10, lon=10, sources=[{
                "dataset": "Foursquare", "record_id": "far"
            }]),
        ],
        [
            entity("Q1", "Clean", external_ids={"Foursquare": ["clean"]}),
            entity("Q2", "Different", lat=20, lon=20,
                   external_ids={"Foursquare": ["far"]}),
        ],
    )
    result = sidecar.build_review_queue(
        candidate_set, candidate_sha256="a" * 64, limit=1, control_quota=0
    )
    assert result["meta"]["candidate_set_sha256"] == "a" * 64
    assert result["summary"]["selected_candidates"] == 1
    assert result["queue"][0]["wikidata_qid"] == "Q2"
    assert "distance_over_gate" in result["queue"][0]["risk_flags"]
    assert "no_normalized_name_overlap" in result["queue"][0]["risk_flags"]


def test_broadcast_is_sorted_compact_and_contains_only_reviewed_accepts(tmp_path):
    rows = []
    for identifier, qid, decision, reviewed, eligible in (
        (2, "Q20", "accepted", "independently_reviewed", True),
        (1, "Q10", "accepted", "independently_reviewed", True),
        (3, "Q30", "needs_review", "unreviewed", False),
    ):
        rows.append({
            "gers_id": gid(identifier),
            "wikidata_qid": qid,
            "decision": decision,
            "review_status": reviewed,
            "eligible_for_prominence": eligible,
        })
    ledger = {"schema": sidecar.LEDGER_SCHEMA, "meta": {}, "mappings": rows}
    output = tmp_path / "sidecar.bin"
    report = sidecar.measure_broadcast(ledger, output)
    assert report["mapping_count"] == 2
    assert report["broadcast"]["bytes"] == 16 + 2 * 24
    assert report["resident_join"]["gers_buffer_bytes"] == 32
    assert report["resident_join"]["qid_buffer_bytes"] == 16
    payload = output.read_bytes()
    magic, count = sidecar.BROADCAST_HEADER.unpack_from(payload)
    assert (magic, count) == (sidecar.BROADCAST_MAGIC, 2)
    first_gers, first_qid = struct.unpack_from("<16sQ", payload, 16)
    assert first_gers == uuid.UUID(gid(1)).bytes
    assert first_qid == 10


def test_phase0_spec_and_tool_share_the_stop_line():
    contract = json.loads(
        (ROOT / "benchmarks" / "gers-qid-sidecar-phase0-spec-v1.json").read_text()
    )
    assert contract["phase0_gates"]["construction_contract_movement"] is False
    source = SCRIPT.read_text()
    assert "construction_contract_movement" in source
    assert "eligible_for_prominence" in source


def test_committed_direct_candidate_evidence_reconciles():
    collection_path = (
        ROOT / "benchmarks" / "2026-08-03-sidecar-phase0-foursquare-collection-v1.json"
    )
    places_path = ROOT / "benchmarks" / "sidecar-phase0-foursquare-places-v1.jsonl"
    entities_path = (
        ROOT / "benchmarks" / "sidecar-phase0-foursquare-wikidata-entities-v1.jsonl"
    )
    candidates_path = (
        ROOT / "benchmarks" / "2026-08-03-sidecar-phase0-candidates-v1.json"
    )
    queue_path = (
        ROOT / "benchmarks" / "2026-08-03-sidecar-phase0-review-queue-v1.json"
    )
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    collection = json.loads(collection_path.read_text())
    candidate_set = json.loads(candidates_path.read_text())
    queue = json.loads(queue_path.read_text())

    assert collection["outputs"] == {
        "places_sha256": digest(places_path),
        "entities_sha256": digest(entities_path),
    }
    assert collection["counts"]["joined_gers_ids"] == 343
    assert collection["counts"]["joined_wikidata_qids"] == 344
    assert collection["meta"]["construction_contract_movement"] is False
    assert candidate_set["meta"]["places_sha256"] == digest(places_path)
    assert candidate_set["meta"]["wikidata_entities_sha256"] == digest(entities_path)
    assert candidate_set["summary"] == {
        "places": 343,
        "wikidata_entities": 344,
        "candidates": 344,
        "automatic_accepts": 342,
        "direct_identifier_conflicts": 1,
        "review_required": 344,
    }
    assert queue["meta"]["candidate_set_sha256"] == digest(candidates_path)
    assert queue["summary"]["selected_candidates"] == 200
    assert queue["summary"]["selected_flag_counts"]["clean_direct_control"] == 50
    assert queue["summary"]["selected_flag_counts"]["direct_identifier_conflict"] == 2

    place_rows = [json.loads(line) for line in places_path.read_text().splitlines()]
    entity_rows = [json.loads(line) for line in entities_path.read_text().splitlines()]
    rebuilt = sidecar.build_candidates(
        place_rows=place_rows,
        entity_rows=entity_rows,
        release="2026-06-17.0",
        matcher_version="direct-foursquare-p1968-v1",
        max_distance_km=1.0,
        places_sha256=digest(places_path),
        entities_sha256=digest(entities_path),
    )
    assert sidecar.canonical_json(rebuilt) == candidates_path.read_bytes()
    rebuilt_queue = sidecar.build_review_queue(
        rebuilt,
        candidate_sha256=digest(candidates_path),
        limit=200,
        control_quota=50,
    )
    assert sidecar.canonical_json(rebuilt_queue) == queue_path.read_bytes()
