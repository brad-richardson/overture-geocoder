"""Offline contract tests for the point-family v2 reverse benchmark."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


requests_stub = types.ModuleType("requests")
requests_stub.Session = lambda: None
requests_stub.RequestException = Exception
sys.modules.setdefault("requests", requests_stub)

SCRIPT = Path(__file__).parent.parent / "scripts" / "benchmark_v2_reverse.py"
spec = importlib.util.spec_from_file_location("benchmark_v2_reverse", SCRIPT)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def case(family="places"):
    return {
        "id": f"{family}:one",
        "family": family,
        "expected_gers_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "longitude": 7.42 if family == "places" else -122.33,
        "latitude": 43.74 if family == "places" else 47.61,
        "radius_m": 250 if family == "places" else 100,
    }


def test_case_validation_and_paths_are_family_specific():
    value = {
        "schema": bench.CASES_SCHEMA,
        "cases": [
            case("places"),
            {**case("addresses"), "id": "addresses:one"},
        ],
    }
    places, addresses = bench.validate_cases(value)
    assert "types=poi" in bench.request_path(places)
    assert "radius=250" in bench.request_path(places)
    assert "types=address" in bench.request_path(addresses)
    assert "radius=100" in bench.request_path(addresses)

    with pytest.raises(ValueError):
        bench.validate_cases({"schema": "wrong", "cases": []})
    duplicate = {**value, "cases": [case(), case()]}
    with pytest.raises(ValueError):
        bench.validate_cases(duplicate)


def test_server_timing_parser_is_bounded_and_tolerant():
    assert bench.server_timing_ms("total;dur=12.5") == 12.5
    assert bench.server_timing_ms("cache;dur=2, total;dur=8") == 8
    assert bench.server_timing_ms("total;dur=bad") is None
    assert bench.server_timing_ms(None) is None


def test_http_errors_are_misses_and_exact_ids_rank():
    target = case()
    body = {
        "type": "FeatureCollection",
        "features": [
            {"id": "other"},
            {"id": target["expected_gers_id"].upper()},
        ],
    }
    scored = bench.score_response(target, 200, body, None)
    assert scored == {
        "valid_response": True,
        "rank": 2,
        "recall_at_1": False,
        "recall_at_5": True,
        "reciprocal_rank": 0.5,
        "features": 2,
    }
    missed = bench.score_response(target, 503, {"error": "unavailable"}, None)
    assert missed["valid_response"] is False
    assert missed["rank"] is None
    assert missed["recall_at_5"] is False
    assert missed["reciprocal_rank"] == 0.0


def test_summary_separates_families_and_phases():
    rows = [
        {
            "family": family,
            "phase": phase,
            "valid_response": valid,
            "recall_at_1": rank == 1,
            "recall_at_5": rank is not None and rank <= 5,
            "reciprocal_rank": 1 / rank if rank else 0.0,
            "client_ms": latency,
            "worker_ms": latency - 1,
        }
        for family, phase, valid, rank, latency in [
            ("places", "cold", True, 1, 100.0),
            ("places", "warm", True, 2, 20.0),
            ("addresses", "cold", False, None, 50.0),
            ("addresses", "warm", True, 1, 10.0),
        ]
    ]
    summary = bench.summarize(rows)
    assert summary["places"]["recall_at_5"] == 1.0
    assert summary["places"]["mrr"] == pytest.approx(0.75)
    assert summary["addresses"]["errors"] == 1
    assert summary["places/warm"]["client_ms"]["p50"] == 20.0
    assert "addresses: HTTP/shape errors" in bench.gate_failures(summary)
