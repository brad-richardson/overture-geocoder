"""Tests for the everyday-POI tripwire provenance and coverage gates."""

import copy
import importlib.util
from pathlib import Path


ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "validate_everyday_poi_tripwire.py"
spec = importlib.util.spec_from_file_location("validate_everyday_poi_tripwire", SCRIPT)
tripwire = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tripwire)
SPEC = __import__("json").loads(
    (ROOT / "benchmarks/everyday-poi-tripwire-spec-v1.json").read_text()
)


def case(index=1, *, osm_derived=False):
    return {
        "id": f"everyday:{index}",
        "kind": "place",
        "query_style": "named_poi",
        "query": f"商店 {index} Taipei",
        "expected_name": f"商店 {index}",
        "expected_feature_type": "poi",
        "expected_lat": 25.03,
        "expected_lon": 121.56,
        "tolerance_km": 0.25,
        "strata": {
            "country": "TW",
            "density": "dense_urban",
            "macroregion": "east_asia",
            "poi_family": "retail",
            "script": "non_latin",
            "scope": "everyday",
        },
        "provenance": {
            "accessed_at": "2026-08-03",
            "osm_derived": osm_derived,
            "selection_method": "deterministic source-record sample",
            "source_kind": "government",
            "source_license": "Open Government Data License",
            "source_name": "City business registry",
            "source_record_id": str(index),
            "source_url": "https://example.gov/dataset",
        },
        "comparison_providers": (
            ["overture"]
            if osm_derived
            else ["overture", "nominatim", "photon"]
        ),
    }


def relaxed_spec():
    value = copy.deepcopy(SPEC)
    value["gates"] = {
        "minimum_cases": 1,
        "minimum_outside_europe_north_america": 1,
        "minimum_non_latin_script": 1,
        "minimum_macroregions": 1,
        "minimum_countries": 1,
        "maximum_cases_per_country": 10,
        "minimum_poi_families": 1,
        "minimum_cases_per_poi_family": 1,
        "minimum_government_or_open_primary_cases": 1,
    }
    return value


def test_valid_independent_case_passes_relaxed_gates():
    result = tripwire.validate_payload(
        {"schema": "benchmark-v2-forward-cases-v1", "cases": [case()]},
        relaxed_spec(),
    )
    assert result["ready"] is True
    assert result["errors"] == []
    assert result["dimensions"]["script"] == {"non_latin": 1}


def test_osm_derived_gold_must_exclude_osm_comparison_providers():
    value = case(osm_derived=True)
    value["comparison_providers"] = ["overture", "nominatim", "photon"]
    errors = tripwire.validate_case(value, relaxed_spec())
    assert any("OSM-derived" in message for message in errors)


def test_non_osm_gold_must_grade_all_comparison_providers():
    value = case()
    value["comparison_providers"] = ["overture"]
    errors = tripwire.validate_case(value, relaxed_spec())
    assert any("all providers otherwise" in message for message in errors)


def test_gold_cannot_embed_an_overture_gers_id():
    value = case()
    value["expected_gers_id"] = "00000000-0000-0000-0000-000000000001"
    errors = tripwire.validate_case(value, relaxed_spec())
    assert any("gold cannot come from Overture" in message for message in errors)


def test_frozen_spec_reports_incomplete_collection_without_weakening_gates():
    result = tripwire.validate_payload(
        {"schema": "benchmark-v2-forward-cases-v1", "cases": [case()]}, SPEC
    )
    assert result["ready"] is False
    assert "cases: 1 < required 200" in result["blockers"]
    assert any("countries" in blocker for blocker in result["blockers"])


def test_duplicate_case_ids_are_rejected():
    result = tripwire.validate_payload(
        {"schema": "benchmark-v2-forward-cases-v1", "cases": [case(), case()]},
        relaxed_spec(),
    )
    assert result["ready"] is False
    assert any("duplicate id" in message for message in result["errors"])


def test_source_plan_exactly_fills_the_frozen_tripwire_quotas():
    source_plan = __import__("json").loads(
        (ROOT / "benchmarks/everyday-poi-source-plan-v1.json").read_text()
    )
    assert source_plan["target_totals"]["cases"] == SPEC["gates"]["minimum_cases"]
    assert sum(source_plan["quota_by_country"].values()) == 200
    assert sum(source_plan["quota_by_family"].values()) == 200
    assert max(source_plan["quota_by_country"].values()) <= SPEC["gates"][
        "maximum_cases_per_country"
    ]
    assert len(source_plan["quota_by_country"]) >= SPEC["gates"]["minimum_countries"]
    assert len(source_plan["quota_by_family"]) >= SPEC["gates"][
        "minimum_poi_families"
    ]
    assert all(
        value >= SPEC["gates"]["minimum_cases_per_poi_family"]
        for value in source_plan["quota_by_family"].values()
    )
    assert sum(source["case_quota"] for source in source_plan["sources"]) == 200
    assert all(source["source_kind"] == "government" for source in source_plan["sources"])
    assert all(source["source_url"].startswith("https://") for source in source_plan["sources"])
    assert len({source["macroregion"] for source in source_plan["sources"]}) == 4
    assert sum(
        source["case_quota"]
        for source in source_plan["sources"]
        if source["script"] == "non_latin"
    ) == 100
