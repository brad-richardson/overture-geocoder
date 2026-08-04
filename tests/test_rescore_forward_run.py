"""Tests for the offline re-scorer.

The re-scorer exists to answer one question honestly: how much of the miss pile
is a scorer artifact rather than a retrieval failure. Its value depends
entirely on it being unable to manufacture a number nobody looked at, so most
of what is asserted here is the refusals.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

spec = importlib.util.spec_from_file_location(
    "rescore_forward_run", ROOT / "scripts" / "rescore_forward_run.py"
)
rescore_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rescore_module)


def _case(case_id, name, lat=1.3369, lon=103.9320):
    return {
        "id": case_id, "kind": "place", "query": name, "expected_name": name,
        "expected_lat": lat, "expected_lon": lon, "tolerance_km": 1.0,
    }


def _row(case_id, rank, candidates):
    return {
        "provider": "overture", "case_id": case_id, "query": case_id,
        "rank": rank, "candidates": candidates,
    }


def _candidate(name, lat=1.3369, lon=103.9320):
    return {"id": f"id-{name}", "name": name, "lat": lat, "lon": lon,
            "distance_km": 0.0}


def test_registry_name_flips_and_is_emitted_for_audit():
    cases = {"c1": _case("c1", "BEDOK RESERVOIR MRT STATION")}
    run = {"meta": {"name_match": "exact"},
           "results": [_row("c1", None, [_candidate("Bedok Reservoir Station")])]}
    report = rescore_module.rescore(run, cases, "containment")
    assert report["delta_at_10"] == 1
    assert len(report["flips_pending_audit"]) == 1
    flip = report["flips_pending_audit"][0]
    assert flip["accepted_name"] == "Bedok Reservoir Station"
    # A flip is a claim, not a result, until a human agrees with it.
    assert flip["audit"] == "PENDING"


def test_generic_short_name_does_not_flip():
    cases = {"c2": _case("c2", "HOTEL CENTRAL", lat=19.43, lon=-99.13)}
    run = {"meta": {}, "results": [
        _row("c2", None, [_candidate("Hotel", lat=19.43, lon=-99.13)])]}
    report = rescore_module.rescore(run, cases, "containment")
    assert report["delta_at_10"] == 0
    assert report["flips_pending_audit"] == []


def test_distant_candidate_does_not_flip_however_well_named():
    cases = {"c3": _case("c3", "BEDOK RESERVOIR MRT STATION")}
    run = {"meta": {}, "results": [_row("c3", None, [
        _candidate("Bedok Reservoir Station", lat=14.6, lon=121.0)])]}
    report = rescore_module.rescore(run, cases, "containment")
    assert report["delta_at_10"] == 0


def test_runs_without_retained_candidates_are_refused():
    # The failure mode this prevents: scoring absent candidates as misses and
    # reporting a confident zero.
    cases = {"c4": _case("c4", "ANYTHING")}
    run = {"meta": {}, "results": [
        {"provider": "overture", "case_id": "c4", "rank": None}]}
    with pytest.raises(SystemExit, match="predates candidate retention"):
        rescore_module.rescore(run, cases, "containment")


def test_mismatched_cases_file_is_refused():
    run = {"meta": {}, "results": [_row("c5", None, [])]}
    with pytest.raises(SystemExit, match="no matching case"):
        rescore_module.rescore(run, {"other": _case("other", "X")},
                               "containment")


def test_exact_mode_reproduces_the_recorded_baseline():
    cases = {"c6": _case("c6", "EXACT MATCH PLACE")}
    run = {"meta": {}, "results": [
        _row("c6", 1, [_candidate("EXACT MATCH PLACE")])]}
    report = rescore_module.rescore(run, cases, "exact")
    assert report["delta_at_10"] == 0
    assert report["baseline"]["found_at_10"] == report["rescored"]["found_at_10"]


def test_a_mode_that_loses_cases_is_refused():
    # Guards the superset invariant: any relaxed mode must strictly dominate
    # exact. If it ever does not, the number is meaningless and this must fail
    # rather than quietly report a negative delta.
    cases = {"c7": _case("c7", "SOMEWHERE")}
    # rank recorded as a hit, but no candidate can reproduce it.
    run = {"meta": {}, "results": [_row("c7", 1, [_candidate("Unrelated Name")])]}
    with pytest.raises(SystemExit, match="strictly dominate"):
        rescore_module.rescore(run, cases, "containment")
