"""Offline contract tests for the point-family v2 reverse benchmark."""

from __future__ import annotations

import importlib.util
import json
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


def quality_case(family="places", suffix="one"):
    value = {**case(family), "id": f"{family}:{suffix}", "tolerance_m": 50}
    if family == "places":
        value.update({"expected_name": "Central Cafe", "alt_names": ["Cafe Central"]})
    else:
        value["expected_address"] = {
            "number": "12",
            "street": ["Main Street", "Main St"],
            "postcode": "98101",
            "locality": "Seattle",
            "country_code": "us",
        }
    return value


class FakeResponse:
    def __init__(self, body, status=200):
        self.status_code = status
        self.headers = {}
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


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


def test_provider_neutral_cases_do_not_require_overture_ids():
    provider_neutral = quality_case("places")
    provider_neutral.pop("expected_gers_id")
    value = {"schema": bench.CASES_SCHEMA, "cases": [provider_neutral]}

    normalized = bench.validate_cases(value, require_exact_id=False)
    assert "expected_gers_id" not in normalized[0]
    assert normalized[0]["semantic_expectation"]["names"] == [
        "Central Cafe",
        "Cafe Central",
    ]

    with pytest.raises(ValueError, match="case 0 is invalid"):
        bench.validate_cases(value)


def test_provider_neutral_cases_still_require_semantic_gold():
    provider_neutral = case("places")
    provider_neutral.pop("expected_gers_id")
    with pytest.raises(ValueError, match="lacks semantic gold"):
        bench.validate_cases(
            {"schema": bench.CASES_SCHEMA, "cases": [provider_neutral]},
            require_exact_id=False,
        )


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


def test_provider_request_builders_are_family_specific_and_identified():
    places = bench.validate_cases(
        {"schema": bench.CASES_SCHEMA, "cases": [quality_case("places")]}
    )[0]
    addresses = bench.validate_cases(
        {"schema": bench.CASES_SCHEMA, "cases": [quality_case("addresses")]}
    )[0]

    nominatim = bench.provider_request(
        "nominatim", places, base_url="https://nominatim.example/base/"
    )
    assert nominatim["url"] == "https://nominatim.example/base/reverse"
    assert nominatim["params"]["layer"] == "poi"
    assert "overture-geocoder" in nominatim["headers"]["User-Agent"]

    photon_places = bench.provider_request(
        "photon", places, base_url="https://photon.example/"
    )
    assert photon_places["url"] == "https://photon.example/reverse"
    assert photon_places["params"]["radius"] == pytest.approx(0.25)
    assert photon_places["capability"]["type_equivalence"] == "generic-reverse"
    assert "limitation" in photon_places["capability"]

    photon_addresses = bench.provider_request(
        "photon", addresses, base_url="https://unused.example"
    )
    assert photon_addresses["params"]["layer"] == ["house", "street"]


def test_semantic_scoring_uses_names_address_components_and_distance_not_ids():
    places = bench.validate_cases(
        {"schema": bench.CASES_SCHEMA, "cases": [quality_case("places")]}
    )[0]
    nominatim_body = {
        "place_id": 123,
        "osm_id": 999,
        "name": "Cafe Central",
        "lat": str(places["latitude"]),
        "lon": str(places["longitude"]),
        "category": "amenity",
        "type": "cafe",
        "address": {},
    }
    place_score = bench.score_semantic_response(
        places, 200, nominatim_body, None, "nominatim"
    )
    assert place_score["quality_at_1"] is True
    assert place_score["top_distance_m"] == pytest.approx(0)

    addresses = bench.validate_cases(
        {"schema": bench.CASES_SCHEMA, "cases": [quality_case("addresses")]}
    )[0]
    photon_body = {
        "type": "FeatureCollection",
        "features": [
            {
                "id": "provider-specific-and-ignored",
                "geometry": {
                    "type": "Point",
                    "coordinates": [
                        addresses["longitude"],
                        addresses["latitude"],
                    ],
                },
                "properties": {
                    "housenumber": "12",
                    "street": "Main St",
                    "postcode": "98101",
                    "city": "Seattle",
                    "countrycode": "US",
                },
            }
        ],
    }
    address_score = bench.score_semantic_response(
        addresses, 200, photon_body, None, "photon"
    )
    assert address_score["quality_at_1"] is True
    assert address_score["top_component_accuracy"] == 1.0


def test_null_geometry_is_a_miss_instead_of_aborting():
    assert bench._coordinates({"geometry": None}) is None


def test_external_legacy_cases_are_explicitly_unscorable_without_requests():
    legacy = bench.validate_cases(
        {"schema": bench.CASES_SCHEMA, "cases": [case()]}
    )
    session = FakeSession([])
    rows = bench.run_external_provider(
        provider="nominatim",
        base_url="https://overture.example",
        cases=legacy,
        interval_s=0,
        timeout_s=5,
        session=session,
    )
    assert session.calls == []
    assert rows[0]["scorable"] is False
    assert "semantic gold" in rows[0]["unscorable_reason"]
    summary = bench.summarize(rows)
    assert summary["provider/nominatim"]["unscorable"] == 1
    assert summary["provider/nominatim"]["errors"] == 0


def test_external_provider_runs_sequentially_at_provider_minimum(monkeypatch):
    cases = bench.validate_cases(
        {
            "schema": bench.CASES_SCHEMA,
            "cases": [
                quality_case("places", "one"),
                quality_case("places", "two"),
            ],
        }
    )
    response = {
        "type": "FeatureCollection",
        "features": [
            {
                "geometry": {
                    "type": "Point",
                    "coordinates": [cases[0]["longitude"], cases[0]["latitude"]],
                },
                "properties": {"name": "Central Cafe"},
            }
        ],
    }
    session = FakeSession([FakeResponse(response), FakeResponse(response)])
    sleeps = []
    monkeypatch.setattr(bench.time, "sleep", sleeps.append)

    rows = bench.run_external_provider(
        provider="photon",
        base_url="https://overture.example",
        cases=cases,
        interval_s=0,
        timeout_s=5,
        session=session,
    )

    assert len(session.calls) == 2
    assert sleeps == [bench.PROVIDER_MIN_INTERVAL_S["photon"]]
    assert all(row["provider"] == "photon" for row in rows)
    assert all(row["phase"] == "external" for row in rows)


def test_comparison_report_records_overridden_provider_urls(tmp_path, monkeypatch):
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps({
        "schema": bench.CASES_SCHEMA,
        "cases": [quality_case("places")],
    }))
    output_path = tmp_path / "results.json"
    monkeypatch.setattr(bench, "run_external_provider", lambda **_kwargs: [])
    assert bench.main([
        "--base-url", "https://overture.example",
        "--nominatim-url", "https://nominatim.example",
        "--cases", str(cases_path),
        "--provider", "overture",
        "--provider", "nominatim",
        "--output", str(output_path),
    ]) == 0
    payload = json.loads(output_path.read_text())
    assert payload["provider_urls"] == {
        "overture": "https://overture.example",
        "nominatim": "https://nominatim.example",
    }
