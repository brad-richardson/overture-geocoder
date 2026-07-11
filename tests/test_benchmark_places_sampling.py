"""Hermetic tests for the offline Places sampling benchmark."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parent.parent / "scripts" / "benchmark_places_sampling.py"
spec = importlib.util.spec_from_file_location("benchmark_places_sampling", SCRIPT)
benchmark = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = benchmark
spec.loader.exec_module(benchmark)


def row(place_id, name, confidence, brand="", wikidata="", category="", lat=0, lon=0):
    return {
        "gers_id": place_id,
        "primary_name": name,
        "confidence": confidence,
        "brand_name": brand,
        "brand_wikidata": wikidata,
        "category_primary": category,
        "lat": lat,
        "lon": lon,
        "region": "US-CA",
    }


def make_places(rows):
    return [benchmark.place_from_row(value, index) for index, value in enumerate(rows, 1)]


def make_cases(tmp_path, cases):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": cases}))
    return benchmark.load_cases(path)


def test_prominence_score_matches_prototype_weights():
    assert benchmark.prominence_score(row("x", "Airport", 0.95, "Brand", "Q1", "airport")) == 1.0
    assert benchmark.prominence_score(row("x", "Cafe", 0.80, "Brand", "", "restaurant")) == pytest.approx(0.62)
    assert benchmark.prominence_score(row("x", "Unknown", "", "", "", "")) == 0.25


def test_older_raw_export_schema_aliases_are_preserved():
    place = benchmark.place_from_row({
        "gers_id": "old-schema",
        "name": "Asian Art Museum",
        "category": "museum",
        "city": "San Francisco",
        "address": "200 Larkin St",
        "confidence": 0.8,
    }, 1)
    assert place.primary_name == "Asian Art Museum"
    assert place.category_primary == "museum"
    assert place.locality == "San Francisco"
    assert place.prominence == pytest.approx(0.5)
    assert "200 larkin st" in place.search_text


def test_nested_samples_and_prominence_improve_famous_retention(tmp_path):
    places = make_places([
        row("generic-1", "Generic One", 0.99),
        row("generic-2", "Generic Two", 0.98),
        row("landmark", "Great Airport", 0.80, category="airport", lat=37.0, lon=-122.0),
        row("generic-3", "Generic Three", 0.70),
    ])
    cases = make_cases(tmp_path, [{
        "id": "airport", "query": "Great Airport", "routing_class": "famous_unique",
        "expected_ids": ["landmark"],
    }])
    report = benchmark.run_benchmark(
        places, cases, [2, 3], ["confidence", "experimental-prominence"]
    )

    confidence, prominence = report["strategies"]
    assert [sample["actual_size"] for sample in confidence["samples"]] == [2, 3]
    assert [sample["incremental_rows"] for sample in confidence["samples"]] == [2, 1]
    assert confidence["samples"][0]["cases"][0]["covered"] is False
    assert confidence["samples"][1]["cases"][0]["covered"] is True
    assert prominence["samples"][0]["cases"][0]["covered"] is True
    assert prominence["routing"][0]["recommendation"] == "global_head_candidate"


def test_ubiquitous_brand_reports_retention_but_routes_regionally(tmp_path):
    places = make_places([
        row("s1", "Starbucks", 0.99, brand="Starbucks"),
        row("other", "Other", 0.98),
        row("s2", "Starbucks Coffee", 0.70, brand="Starbucks"),
    ])
    cases = make_cases(tmp_path, [{
        "id": "starbucks", "query": "Starbucks", "routing_class": "ubiquitous_brand",
        "target_brand": "Starbucks",
    }])
    strategy = benchmark.run_benchmark(places, cases, [1, 3], ["confidence"])["strategies"][0]
    first, full = strategy["samples"]
    assert first["cases"][0]["eligible_count"] == 2
    assert first["cases"][0]["retained_count"] == 1
    assert full["cases"][0]["retained_count"] == 2
    assert strategy["routing"][0]["recommendation"] == "regional_places_shards"


def test_name_and_coordinate_rule_disambiguates_duplicates(tmp_path):
    places = make_places([
        row("near", "Landmark", 0.8, lat=37.0, lon=-122.0),
        row("far", "Landmark", 0.9, lat=40.0, lon=-120.0),
    ])
    case = make_cases(tmp_path, [{
        "id": "landmark", "query": "Landmark", "routing_class": "famous_unique",
        "target_name": "Landmark", "target_lat": 37.0, "target_lon": -122.0,
        "tolerance_km": 2,
    }])[0]
    assert [place.place_id for place in places if benchmark.matches_case(place, case)] == ["near"]


def test_cli_writes_json_and_markdown_from_csv(tmp_path):
    csv_path = tmp_path / "places.csv"
    csv_path.write_text(
        "gers_id,primary_name,confidence,brand_name,lat,lon\n"
        "famous,Famous Place,0.9,,37,-122\n"
        "chain,Chain Shop,0.8,Chain Shop,38,-121\n"
    )
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps({"cases": [{
        "id": "famous", "query": "Famous Place", "routing_class": "famous_unique",
        "expected_ids": ["famous"],
    }]}))
    json_out = tmp_path / "report.json"
    markdown_out = tmp_path / "report.md"
    result = benchmark.main([
        str(csv_path), "--cases", str(cases_path), "--sizes", "1,2",
        "--json-out", str(json_out), "--markdown-out", str(markdown_out),
    ])
    assert result == 0
    machine = json.loads(json_out.read_text())
    assert machine["schema_version"] == 1
    assert machine["source_count"] == 2
    assert "Places sampling benchmark" in markdown_out.read_text()


def test_file_benchmark_keeps_only_largest_requested_sample(tmp_path, monkeypatch):
    csv_path = tmp_path / "places.csv"
    csv_path.write_text(
        "gers_id,primary_name,confidence\n"
        "one,One,0.9\n"
        "two,Two,0.8\n"
        "three,Three,0.7\n"
    )
    cases = make_cases(tmp_path, [{
        "id": "one", "query": "One", "routing_class": "famous_unique",
        "expected_ids": ["one"],
    }])
    monkeypatch.setattr(
        benchmark,
        "load_places",
        lambda _path: pytest.fail("streaming file path must not call load_places"),
    )

    report = benchmark.run_benchmark_file(
        csv_path, cases, [1, 2], ["confidence", "experimental-prominence"]
    )

    assert report["source_count"] == 3
    assert all(
        strategy["samples"][-1]["actual_size"] == 2
        for strategy in report["strategies"]
    )


def test_unlabelled_case_is_explicit_not_scored_as_failure(tmp_path):
    places = make_places([row("one", "Existing", 0.9)])
    cases = make_cases(tmp_path, [{
        "id": "missing", "query": "Missing", "routing_class": "famous_unique",
        "target_name": "Missing",
    }])
    summary = benchmark.run_benchmark(places, cases, [1], ["confidence"])["strategies"][0]["samples"][0]["summary"]
    assert summary["labelled_case_count"] == 0
    assert summary["coverage_rate"] is None
    assert summary["unlabelled_cases"] == ["missing"]


def test_confidence_strategy_is_not_reranked_by_rejected_prominence(tmp_path):
    places = make_places([
        row("target", "Same", 0.95),
        row("branded", "Same", 0.60, brand="Brand", wikidata="Q1", category="airport"),
    ])
    case = make_cases(tmp_path, [{
        "id": "same", "query": "Same", "routing_class": "famous_unique",
        "expected_ids": ["target"],
    }])[0]
    result = benchmark.evaluate_case(
        places, case, {"target"}, top_k=1, strategy="confidence"
    )
    assert result["first_target_rank"] == 1


def test_source_coverage_is_bounded_and_source_stratified():
    places = [
        benchmark.place_from_row({
            **row("one", "One", 0.95, category="museum"),
            "country": "US",
            "root_sources": [{
                "property": "", "dataset": "alpha", "license": "x",
                "record_id": "a1", "update_time": "2026-01-02", "confidence": 0.8,
            }],
            "root_source_count": 1,
            "overture_release": "2026-06-17.0",
        }, 1),
        benchmark.place_from_row({
            **row("two", "Two", 0.65, category="hotel"),
            "country": "US",
            "root_sources": [
                {"property": "", "dataset": "beta"},
                {"property": "", "dataset": "gamma"},
            ],
            "root_source_count": 2,
            "overture_release": "2026-06-17.0",
        }, 2),
    ]
    profile = benchmark.coverage_profile(places)
    assert profile["key_limit_per_dimension"] == benchmark.PROFILE_KEY_LIMIT
    assert profile["root_cardinality"]["one_root_rows"] == 1
    assert profile["root_cardinality"]["multiple_root_rows"] == 1
    assert profile["dimensions"]["source_feature_confidence"]["counts"]["alpha|0.9-1.0"] == 1
    assert profile["dimensions"]["source_root_confidence"]["counts"]["alpha|0.75-0.9"] == 1
    assert profile["dimensions"]["root_source_update_years"]["counts"]["2026"] == 1
    assert profile["dimensions"]["root_source_licenses"]["counts"]["x"] == 1
    assert profile["dimensions"]["root_source_record_id_presence"]["counts"]["present"] == 1
