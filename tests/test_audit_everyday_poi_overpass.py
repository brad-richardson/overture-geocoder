import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "audit_everyday_poi_overpass.py"
sys.modules.setdefault(
    "requests", SimpleNamespace(Session=lambda: None, RequestException=Exception)
)
spec = importlib.util.spec_from_file_location("audit_everyday_poi_overpass", SCRIPT)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def case(name="Central Clinic", family="healthcare"):
    return {
        "id": "case-1",
        "expected_name": name,
        "expected_lat": 1.0,
        "expected_lon": 2.0,
        "strata": {"poi_family": family},
        "provenance": {"source_name": "Authority", "source_record_id": "7"},
    }


def candidate(name="Central Clinic", **tags):
    return {
        "osm_type": "node",
        "osm_id": 10,
        "lat": 1.0001,
        "lon": 2.0001,
        "tags": {"name": name, **tags},
    }


def test_query_batches_named_nodes_ways_and_relations():
    query = audit.build_query([case()], 500, 60)
    assert query.startswith("[out:json][timeout:60]")
    assert "nwr(around:500,1.0,2.0)" in query
    assert audit.NAME_KEY_PATTERN in query
    assert query.endswith("out tags center qt;\n")


def test_candidate_reads_node_and_way_centers_and_retains_poi_tags():
    node = audit.candidate_from_element({
        "type": "node", "id": 1, "lat": 1, "lon": 2,
        "tags": {"name": "Clinic", "amenity": "clinic", "phone": "secret"},
    })
    way = audit.candidate_from_element({
        "type": "way", "id": 2, "center": {"lat": 3, "lon": 4},
        "tags": {"name:ja": "医院", "healthcare": "clinic"},
    })
    assert node["tags"] == {"name": "Clinic", "amenity": "clinic"}
    assert (way["lat"], way["lon"]) == (3.0, 4.0)
    assert way["tags"]["name:ja"] == "医院"


def test_exact_name_accepts_punctuation_case_diacritics_and_alt_names():
    value = audit.classify_case(
        {**case("Hotel del Angel", "lodging"), "alt_names": ["旅館"]},
        [candidate("HOTEL DEL ÁNGEL", tourism="hotel")],
        500,
        0.86,
    )
    assert value["status"] == "exact_name_present"
    assert value["exact_matches"][0]["exact_name_matches"]


def test_fuzzy_and_plausible_candidates_are_not_exact_truth():
    fuzzy = audit.classify_case(
        case("Central Clinic"),
        [candidate("Central Medical Clinic", amenity="clinic")],
        500,
        0.75,
    )
    plausible = audit.classify_case(
        case("Central Clinic"),
        [candidate("Different Health", amenity="clinic")],
        500,
        0.95,
    )
    assert fuzzy["status"] == "fuzzy_name_candidate"
    assert plausible["status"] == "plausible_family_nearby_without_name_match"


def test_out_of_radius_candidates_do_not_count():
    far = candidate()
    far.update({"lat": 2.0, "lon": 3.0})
    value = audit.classify_case(case(), [far], 500, 0.86)
    assert value["status"] == "no_named_family_candidate"
    assert value["named_candidates_within_radius"] == 0


def test_summary_cross_tabs_ranked_provider_misses_against_osm_presence():
    results = [
        {"case_id": "a", "status": "exact_name_present",
         "strata": {"poi_family": "retail"}},
        {"case_id": "b", "status": "fuzzy_name_candidate",
         "strata": {"poi_family": "retail"}},
    ]
    summary = audit.summarize(results, {
        "a": {"nominatim": False},
        "b": {"nominatim": False},
    })
    assert summary["by_status"] == {
        "exact_name_present": 1,
        "fuzzy_name_candidate": 1,
    }
    assert summary["provider_cross_tab"]["nominatim"] == {
        "hit_and_exact_osm_name_present": 0,
        "hit_without_exact_osm_name_match": 0,
        "miss_and_exact_osm_name_present": 1,
        "miss_and_fuzzy_osm_name_candidate": 1,
        "miss_without_osm_name_match": 0,
    }


def test_committed_inputs_remain_compatible():
    cases = json.loads(
        (ROOT / "benchmarks/everyday-poi-tripwire-cases-v1.json").read_text()
    )
    baseline = json.loads(
        (ROOT / "benchmarks/2026-08-03-everyday-poi-external-baseline-v1.json")
        .read_text()
    )
    assert cases["schema"] == audit.CASES_SCHEMA
    assert len(cases["cases"]) == 200
    assert len(baseline["results"]) == 600
    assert all(audit.name_values({"name": value["expected_name"]})
               for value in cases["cases"])
