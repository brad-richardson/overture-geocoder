"""Unit tests for the offline pieces of the /v2/forward accuracy benchmark."""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


# The benchmark only needs requests when executing a live run. Keep these unit
# tests hermetic even in a minimal test environment.
requests_stub = types.ModuleType("requests")
requests_stub.Session = lambda: None
requests_stub.RequestException = Exception
sys.modules.setdefault("requests", requests_stub)


SCRIPT = Path(__file__).parent.parent / "scripts" / "benchmark_v2_forward.py"
spec = importlib.util.spec_from_file_location("benchmark_v2_forward", SCRIPT)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def place_record(feature_id, name, country="MC", category="restaurant",
                 confidence=200, locality="Monaco", lon=7.42, lat=43.73):
    return {
        "feature_id": feature_id,
        "primary_name": name,
        "category": category,
        "locality": locality,
        "country": country,
        "confidence_rank": confidence,
        "longitude": lon,
        "latitude": lat,
    }


def address_record(feature_id, street="Main St", number="12", unit="",
                   postcode="98101", city="Seattle", country="us",
                   levels=("WA",), locator=(8, 108, 5)):
    return {
        "feature_id": feature_id,
        "longitude_e7": -1223300000,
        "latitude_e7": 476100000,
        "country": country,
        "display_country": country.upper(),
        "postal_city": city,
        "postcode": postcode,
        "street": street,
        "number": number,
        "unit": unit,
        "address_levels": list(levels),
        "source_object_index": locator[0],
        "source_row_group": locator[1],
        "source_row_index": locator[2],
    }


# ---------------------------------------------------------------------------
# Stratification primitives


def test_format_gers_id_hyphenates_binary_feature_ids():
    raw = bytes(range(16))
    assert bench.format_gers_id(raw) == "00010203-0405-0607-0809-0a0b0c0d0e0f"
    assert bench.format_gers_id("ABCD-EF") == "abcd-ef"
    with pytest.raises(ValueError):
        bench.format_gers_id(b"short")


def test_name_script_classification():
    assert bench.name_script("Cafe de Paris") == "latin"
    assert bench.name_script("Москва") == "non_latin"
    assert bench.name_script("東京タワー") == "non_latin"
    # Mixed with latin majority stays latin
    assert bench.name_script("Cafe 東") == "latin"
    assert bench.name_script("123 §") == "other"


def test_confidence_band_boundaries():
    assert bench.confidence_band(0) == "0-63"
    assert bench.confidence_band(63) == "0-63"
    assert bench.confidence_band(64) == "64-127"
    assert bench.confidence_band(255) == "192-255"
    assert bench.confidence_band(999) == "192-255"


def test_token_commonality_bucketing():
    names = ["Cafe Riviera", "Cafe Central", "Cafe Rex", "Zzyzx Emporium"]
    frequencies = bench.token_document_frequencies(names)
    assert frequencies["cafe"] == 3
    assert frequencies["zzyzx"] == 1
    assert bench.commonality_bucket("Zzyzx Emporium", frequencies) == "rare"
    assert bench.commonality_bucket("Cafe Rex", frequencies) == "mid"
    common = bench.token_document_frequencies(
        [f"Bar {index}" for index in range(30)])
    assert bench.commonality_bucket("Bar None", common) == "common"


def test_token_frequencies_count_each_document_once():
    frequencies = bench.token_document_frequencies(["Cafe Cafe Cafe"])
    assert frequencies == {"cafe": 1}


# ---------------------------------------------------------------------------
# Case building


def test_place_cases_are_deterministic_for_a_seed():
    records = [
        place_record(bytes([index] * 16), f"Cafe {index}",
                     category="cafe" if index % 2 else "bar",
                     confidence=40 + index * 20)
        for index in range(10)
    ]
    first = bench.build_place_cases(records, seed=7, per_stratum=2, max_cases=50)
    second = bench.build_place_cases(records, seed=7, per_stratum=2, max_cases=50)
    assert first == second
    assert first
    third = bench.build_place_cases(records, seed=8, per_stratum=2, max_cases=50)
    # A different seed may reshuffle bias points/query styles
    assert json.dumps(first) != json.dumps(third) or len(records) <= 2


def test_place_case_shape_and_bias_point_is_near_the_record():
    records = [place_record(bytes([1] * 16), "Cafe de Paris", lon=7.42, lat=43.73)]
    (case,) = bench.build_place_cases(records, seed=1, per_stratum=1, max_cases=10)
    assert case["kind"] == "place"
    assert case["expected_name"] == "Cafe de Paris"
    assert case["tolerance_km"] == bench.PLACE_TOLERANCE_KM
    assert case["expected_gers_id"] == "01010101-0101-0101-0101-010101010101"
    assert case["query"].startswith("Cafe de Paris")
    assert case["query_style"] in ("name", "name_locality")
    bias_lon, bias_lat = case["proximity"]
    assert abs(bias_lat - 43.73) <= 0.02 + 1e-9
    assert abs(bias_lon - 7.42) <= 0.02 + 1e-9
    assert case["strata"] == {
        "country": "MC", "category": "restaurant", "confidence": "192-255",
        "script": "latin", "commonality": "rare",
    }


def test_place_cases_skip_long_names_empty_names_and_duplicate_ids():
    records = [
        place_record(bytes([1] * 16), "One Two Three Four Five"),
        place_record(bytes([2] * 16), ""),
        place_record(bytes([3] * 16), "Keep Me"),
        place_record(bytes([3] * 16), "Keep Me"),  # duplicate gers
    ]
    cases = bench.build_place_cases(records, seed=1, per_stratum=5, max_cases=50)
    assert [case["expected_gers_id"] for case in cases] == [
        "03030303-0303-0303-0303-030303030303"
    ]


def test_place_query_never_exceeds_token_cap_even_with_locality():
    records = [place_record(bytes([9] * 16), "Alpha Beta Gamma Delta",
                            locality="Monte Carlo")]
    (case,) = bench.build_place_cases(records, seed=3, per_stratum=1, max_cases=10)
    assert case["query"] == "Alpha Beta Gamma Delta"
    assert case["query_style"] == "name"


def test_address_case_params_reconstruct_the_eight_field_key():
    record = address_record(bytes([4] * 16), unit="B",
                            levels=("Washington", "King County"))
    params = bench.address_case_params(record)
    assert params == {
        "country": "us",
        "admin_level_general": "Washington",
        "admin_level_specific": "King County",
        "postal_city": "Seattle",
        "postcode": "98101",
        "street": "Main St",
        "number": "12",
        "unit": "B",
    }
    # A single level fills both admin fields; empties are omitted (an omitted
    # parameter is the literal empty string on the worker side).
    sparse = bench.address_case_params(
        address_record(bytes([4] * 16), postcode="", city="", levels=("WA",)))
    assert sparse == {
        "country": "us", "admin_level_general": "WA",
        "admin_level_specific": "WA", "street": "Main St", "number": "12",
    }


def test_address_case_params_keep_empty_string_levels_for_key_parity():
    # The producer's levels() drops only NULLs and keys on first/last including
    # "": ["", "X"] must yield an empty (omitted) admin_level_general and
    # admin_level_specific "X", not general "X".
    params = bench.address_case_params(
        address_record(bytes([4] * 16), levels=("", "X")))
    assert "admin_level_general" not in params
    assert params["admin_level_specific"] == "X"
    trailing = bench.address_case_params(
        address_record(bytes([4] * 16), levels=("X", "")))
    assert trailing["admin_level_general"] == "X"
    assert "admin_level_specific" not in trailing
    # None entries are the producer's NULLs and are still dropped.
    nulls = bench.address_case_params(
        address_record(bytes([4] * 16), levels=(None, "X")))
    assert nulls["admin_level_general"] == "X"
    assert nulls["admin_level_specific"] == "X"


def test_address_cases_stratify_by_completeness_and_skip_unqueryable_rows():
    records = [
        address_record(bytes([1] * 16), locator=(8, 1, 1)),
        address_record(bytes([2] * 16), unit="7", locator=(8, 1, 2)),
        address_record(bytes([3] * 16), postcode="", locator=(8, 1, 3)),
        address_record(bytes([4] * 16), street="", locator=(8, 1, 4)),  # skipped
        address_record(bytes([5] * 16), number="", locator=(8, 1, 5)),  # skipped
    ]
    cases = bench.build_address_cases(records, seed=1, per_stratum=5, max_cases=50)
    assert len(cases) == 3
    completeness = sorted(case["strata"]["completeness"] for case in cases)
    assert completeness == ["number", "number+postcode", "number+unit+postcode"]
    assert all(case["strata"]["country"] == "us" for case in cases)
    (plain,) = [case for case in cases
                if case["strata"]["completeness"] == "number+postcode"]
    assert plain["expected_lat"] == pytest.approx(47.61)
    assert plain["expected_lon"] == pytest.approx(-122.33)


def test_address_cases_are_deterministic_and_keep_locator_identity():
    records = [
        address_record(bytes([1] * 16), locator=(8, 1, 1)),
        address_record(bytes([1] * 16), locator=(8, 1, 2)),  # same id, new row
    ]
    first = bench.build_address_cases(records, seed=5, per_stratum=5, max_cases=50)
    second = bench.build_address_cases(records, seed=5, per_stratum=5, max_cases=50)
    assert first == second
    assert len(first) == 2
    assert len({case["id"] for case in first}) == 2


def test_case_file_is_identical_for_a_fixed_seed_and_timestamp():
    records = [place_record(bytes([index] * 16), f"Cafe {index}")
               for index in range(6)]
    files = [
        bench.build_case_file(
            bench.build_place_cases(records, seed=11, per_stratum=2, max_cases=20),
            seed=11, sources=["slice/work"], timestamp="2026-07-28T00:00:00+00:00",
        )
        for _ in range(2)
    ]
    assert json.dumps(files[0], sort_keys=True) == json.dumps(files[1], sort_keys=True)
    assert files[0]["schema"] == bench.CASES_SCHEMA
    assert files[0]["meta"]["case_counts"] == {"place": len(files[0]["cases"])}


def test_max_cases_cap_keeps_every_stratum_represented():
    records = []
    for index in range(20):
        records.append(place_record(bytes([index + 1] * 16), f"Alpha {index}",
                                    category="bar"))
    for index in range(20):
        records.append(place_record(bytes([index + 100] * 16), f"Beta {index}",
                                    category="cafe"))
    cases = bench.build_place_cases(records, seed=2, per_stratum=10, max_cases=6)
    assert len(cases) == 6
    categories = {case["strata"]["category"] for case in cases}
    assert categories == {"bar", "cafe"}


# ---------------------------------------------------------------------------
# Scoring and aggregation


def feature(gers, lon, lat, name="Somewhere"):
    return {
        "type": "Feature",
        "id": gers,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"name": name},
    }


def test_score_case_ranks_by_exact_gers_id_for_self_recall():
    case = {"kind": "place", "expected_gers_id": "aa-bb",
            "expected_lat": 43.73, "expected_lon": 7.42}
    features = [feature("other", 7.0, 43.0), feature("AA-BB", 7.42, 43.73)]
    rank, matched, top1 = bench.score_case(case, features)
    assert rank == 2
    assert matched == pytest.approx(0.0, abs=1e-6)
    assert top1 == pytest.approx(bench.haversine_km(43.73, 7.42, 43.0, 7.0))


def test_score_case_misses_when_gers_absent():
    case = {"kind": "address", "expected_gers_id": "aa-bb",
            "expected_lat": 47.6, "expected_lon": -122.3}
    rank, matched, top1 = bench.score_case(case, [feature("cc-dd", -122.3, 47.6)])
    assert rank is None
    assert matched is None
    assert top1 == pytest.approx(0.0, abs=1e-6)


def test_score_case_name_and_distance_for_builtin_kinds():
    case = {"kind": "seam", "query": "Paris", "expected_name": "Paris",
            "alt_names": [], "expected_lat": 48.8566, "expected_lon": 2.3522,
            "tolerance_km": 50.0}
    features = [
        feature("poi", -95.55, 33.66, name="Paris"),  # Paris, Texas: too far
        feature("div", 2.3522, 48.8566, name="Paris, FR"),
    ]
    rank, matched, _ = bench.score_case(case, features)
    assert rank == 2
    assert matched == pytest.approx(0.0, abs=1e-6)


def test_score_case_accepts_alt_names_and_accents():
    case = {"kind": "multilingual", "query": "moscou", "expected_name": "Москва",
            "alt_names": ["Moscow"], "expected_lat": 55.7558,
            "expected_lon": 37.6173, "tolerance_km": 50.0}
    rank, _, _ = bench.score_case(
        case, [feature("div", 37.6173, 55.7558, name="Moscow, RU")])
    assert rank == 1


def test_curated_statue_case_accepts_the_official_nps_unit_name():
    payload = json.loads(
        (SCRIPT.parent.parent / "benchmarks/v2-forward-gold-cases-v1.json")
        .read_text()
    )
    case = next(
        case
        for case in payload["cases"]
        if case["id"] == "gold:name:statue-of-liberty"
    )
    observed = feature(
        "707ad7cf-7faf-495f-8b9e-6e0cd8807c25",
        -74.04000091552734,
        40.698612213134766,
        name="Statue Of Liberty National Monument",
    )

    rank, distance, _ = bench.score_case(
        case, [observed], provider="overture", semantic_scoring=True
    )

    assert rank == 1
    assert distance is not None and distance < case["tolerance_km"]
    assert case["sources"] == [
        "https://www.nps.gov/npnh/learn/news/fact-sheet-stli.htm"
    ]


def test_curated_big_ben_case_rejects_a_same_named_london_homonym():
    payload = json.loads(
        (SCRIPT.parent.parent / "benchmarks/v2-forward-gold-cases-v1.json")
        .read_text()
    )
    case = next(
        case
        for case in payload["cases"]
        if case["id"] == "gold:name_locality:big-ben"
    )
    homonym = feature(
        "50ba72f2-f781-4ab1-93e0-1adc4ac6699b",
        -0.11420848965644836,
        51.51083755493164,
        name="Big Ben",
    )
    landmark = feature(
        "99f74940-898a-49d5-9eca-18a8a2c47918",
        -0.12457490712404251,
        51.500701904296875,
        name="Big Ben",
    )

    rank, distance, _ = bench.score_case(
        case, [homonym, landmark], provider="overture", semantic_scoring=True
    )

    assert rank == 2
    assert distance is not None and distance < case["tolerance_km"]
    assert case["tolerance_km"] == 0.25


def test_curated_machu_picchu_case_accepts_the_official_citadel_name():
    payload = json.loads(
        (SCRIPT.parent.parent / "benchmarks/v2-forward-gold-cases-v1.json")
        .read_text()
    )
    case = next(
        case
        for case in payload["cases"]
        if case["id"] == "gold:name:machu-picchu"
    )
    town_side_homonym = feature(
        "487cc093-6904-4102-b932-d31c32fcd471",
        -72.52550506591797,
        -13.15510082244873,
        name="Machu Picchu",
    )
    citadel = feature(
        "2fe76e46-e532-43ce-bfde-082d27ff1091",
        -72.54312896728516,
        -13.165722846984863,
        name="Ciudadela De Machu Picchu",
    )

    rank, distance, _ = bench.score_case(
        case,
        [town_side_homonym, citadel],
        provider="overture",
        semantic_scoring=True,
    )

    assert rank == 2
    assert distance is not None and distance < case["tolerance_km"]
    assert "Ciudadela de Machu Picchu" in case["alt_names"]


def test_aggregate_and_summary_math():
    rows = [
        {"kind": "place", "query_style": "name", "strata": {"country": "MC"},
         "error": None,
         "rank": 1, "found_at_1": True, "found_at_10": True,
         "top1_distance_km": 0.1, "ms": 100.0},
        {"kind": "place", "query_style": "name", "strata": {"country": "MC"},
         "error": None,
         "rank": 4, "found_at_1": False, "found_at_10": True,
         "top1_distance_km": 2.0, "ms": 200.0},
        {"kind": "place", "query_style": "name_locality",
         "strata": {"country": "FR"}, "error": None,
         "rank": None, "found_at_1": False, "found_at_10": False,
         "top1_distance_km": 500.0, "ms": 300.0},
        {"kind": "seam", "error": None, "rank": 1, "found_at_1": True,
         "found_at_10": True, "top1_distance_km": 0.5, "ms": 150.0},
        {"kind": "place", "strata": {"country": "FR"}, "error": "boom",
         "rank": None, "found_at_1": False, "found_at_10": False,
         "top1_distance_km": None, "ms": 50.0},
    ]
    summary = bench.summarize_results(rows)
    assert summary["overall"]["n"] == 5
    assert summary["overall"]["errors"] == 1
    assert summary["overall"]["recall_at_1"] == 0.4
    assert summary["overall"]["recall_at_5"] == 0.6
    assert summary["overall"]["recall_at_10"] == 0.6
    assert summary["self_recall"]["n"] == 4
    assert summary["self_recall"]["recall_at_10"] == 0.5
    assert summary["by_kind"]["seam"]["recall_at_1"] == 1.0
    assert summary["by_stratum"]["place:country=MC"]["recall_at_10"] == 1.0
    assert summary["by_query_style"]["place:name"]["recall_at_10"] == 1.0
    assert summary["by_query_style"]["place:name_locality"]["recall_at_10"] == 0.0
    assert summary["overall"]["p95_ms"] == 300.0
    # mrr counts misses as zero contribution
    assert summary["by_stratum"]["place:country=MC"]["mrr"] == round(
        (1 + 1 / 4) / 2, 3)
    assert bench.self_recall_at_10(summary) == 0.5


def test_aggregate_with_only_errors_counts_them_as_misses():
    stats = bench.summarize_results(
        [{"kind": "place", "error": "boom", "rank": None, "found_at_1": False,
          "found_at_10": False, "top1_distance_km": None, "ms": 10.0}])
    assert stats["overall"]["recall_at_10"] == 0.0
    assert stats["overall"]["p50_ms"] is None
    assert bench.self_recall_at_10(stats) == 0.0


def test_aggregate_excludes_explicitly_unscorable_cases():
    rows = [
        {"kind": "place", "capability": "unscorable", "error": None,
         "rank": None, "found_at_1": False, "found_at_10": False,
         "top1_distance_km": None, "ms": 0.0},
        {"kind": "place", "capability": "supported", "error": None,
         "rank": 1, "found_at_1": True, "found_at_10": True,
         "top1_distance_km": 0.0, "ms": 10.0},
    ]
    stats = bench.summarize_results(rows)["overall"]
    assert stats["requested"] == 2
    assert stats["unscorable"] == 1
    assert stats["n"] == 1
    assert stats["recall_at_1"] == 1.0


# ---------------------------------------------------------------------------
# Comparison / regression logic


def make_summary(recall1, recall10):
    stats = {"n": 10, "errors": 0, "found_at_1": 0, "found_at_10": 0,
             "recall_at_1": recall1, "recall_at_10": recall10, "mrr": None,
             "median_top1_distance_km": None, "p50_ms": None}
    return {"overall": dict(stats), "self_recall": dict(stats),
            "by_kind": {"place": dict(stats)},
            "by_stratum": {"place:country=MC": dict(stats)}}


def test_compare_flags_regressions_beyond_threshold_only():
    baseline = make_summary(0.9, 0.95)
    unchanged = bench.compare_summaries(baseline, make_summary(0.88, 0.93), 0.05)
    assert unchanged == []
    regressed = bench.compare_summaries(baseline, make_summary(0.6, 0.95), 0.05)
    assert any("overall recall_at_1 regressed 0.900 -> 0.600" == entry
               for entry in regressed)
    assert any(entry.startswith("by_stratum:place:country=MC")
               for entry in regressed)
    assert not any("recall_at_10" in entry for entry in regressed)


def test_compare_ignores_groups_missing_from_either_side():
    baseline = make_summary(0.9, 0.9)
    current = make_summary(0.9, 0.9)
    del current["by_stratum"]["place:country=MC"]
    current["overall"]["recall_at_1"] = None
    assert bench.compare_summaries(baseline, current, 0.05) == []


# ---------------------------------------------------------------------------
# Request building and 503 handling (mocked network)


class FakeResponse:
    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls = []

    def get(self, url, params, timeout, headers=None):
        self.calls.append((url, dict(params), dict(headers or {})))
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def make_runner(responses, provider="overture"):
    runner = bench.Runner("https://example.test", interval=0, timeout=1,
                          sleep_fn=lambda _s: None, monotonic_fn=lambda: 0.0,
                          provider=provider)
    runner.session = FakeSession(responses)
    return runner


PLACE_CASE = {
    "id": "place:aa", "kind": "place", "query": "Cafe de Paris",
    "expected_gers_id": "aa-bb", "expected_lat": 43.73, "expected_lon": 7.42,
    "proximity": [7.421, 43.731], "strata": {"country": "MC"},
}


def test_case_request_shapes():
    url, params = bench.case_request(PLACE_CASE, "https://example.test/", 10)
    assert url == "https://example.test/v2/forward"
    assert params == {"q": "Cafe de Paris", "limit": "10",
                      "autocomplete": "false", "proximity": "7.421,43.731"}
    address_case = {"kind": "address",
                    "params": {"country": "us", "street": "Main St", "number": "12"}}
    _, params = bench.case_request(address_case, "https://example.test", 10)
    # Structured lookup rejects limit/autocomplete/proximity: params only.
    assert params == {"country": "us", "street": "Main St", "number": "12"}


def test_nominatim_request_shapes_free_text_bias_and_structured_address():
    url, params = bench.provider_case_request(
        "nominatim", PLACE_CASE, "https://nominatim.test/", 10)
    assert url == "https://nominatim.test/search"
    assert params["q"] == "Cafe de Paris"
    assert params["format"] == "jsonv2"
    assert params["addressdetails"] == "1"
    assert params["viewbox"] == "7.171,43.981,7.671,43.481"
    assert params["bounded"] == "0"

    address_case = {
        "kind": "address",
        "params": {
            "country": "us", "admin_level_general": "Washington",
            "admin_level_specific": "King County", "postal_city": "Seattle",
            "postcode": "98101", "street": "Main St", "number": "12",
            "unit": "B",
        },
    }
    url, params = bench.provider_case_request(
        "nominatim", address_case, "https://nominatim.test", 10)
    assert url == "https://nominatim.test/search"
    assert params == {
        "format": "jsonv2", "limit": "10", "addressdetails": "1",
        "street": "12 Main St", "city": "Seattle", "postalcode": "98101",
        "countrycodes": "us",
    }


def test_nominatim_omits_bias_box_at_antimeridian():
    case = dict(PLACE_CASE, proximity=[179.9, 0.0])
    _, params = bench.provider_case_request(
        "nominatim", case, "https://nominatim.test", 10)
    assert "viewbox" not in params
    assert "bounded" not in params


def test_nominatim_maps_conventional_address_locality_aliases():
    address_case = {
        "kind": "address",
        "params": {
            "country": "us", "state": "WA", "city": "Seattle",
            "postcode": "98101", "street": "Main St", "number": "12",
        },
    }
    url, params = bench.provider_case_request(
        "nominatim", address_case, "https://nominatim.test", 10)
    assert url == "https://nominatim.test/search"
    assert params == {
        "format": "jsonv2", "limit": "10", "addressdetails": "1",
        "street": "12 Main St", "city": "Seattle", "state": "WA",
        "postalcode": "98101", "countrycodes": "us",
    }


def test_photon_request_shapes_free_text_bias_and_structured_address():
    url, params = bench.provider_case_request(
        "photon", PLACE_CASE, "https://photon.test/", 10)
    assert url == "https://photon.test/api/"
    assert params == {
        "q": "Cafe de Paris", "limit": "10",
        "lon": "7.421", "lat": "43.731",
    }
    address_case = {
        "kind": "address",
        "params": {
            "country": "us", "admin_level_general": "Washington",
            "admin_level_specific": "King County", "postal_city": "Seattle",
            "postcode": "98101", "street": "Main St", "number": "12",
            "unit": "B",
        },
    }
    url, params = bench.provider_case_request(
        "photon", address_case, "https://photon.test", 10)
    assert url == "https://photon.test/structured"
    assert params == {
        "limit": "10", "city": "Seattle", "postcode": "98101",
        "housenumber": "12",
        "street": "Main St", "countrycode": "US",
    }


def test_photon_maps_conventional_address_locality_aliases():
    address_case = {
        "kind": "address",
        "params": {
            "country": "us", "region": "WA", "city": "Seattle",
            "postcode": "98101", "street": "Main St", "number": "12",
        },
    }
    url, params = bench.provider_case_request(
        "photon", address_case, "https://photon.test", 10)
    assert url == "https://photon.test/structured"
    assert params == {
        "limit": "10", "city": "Seattle", "state": "WA",
        "postcode": "98101", "housenumber": "12",
        "street": "Main St", "countrycode": "US",
    }


def test_provider_response_normalization_is_provider_neutral():
    nominatim = bench.normalize_provider_response("nominatim", [{
        "place_id": 99, "name": "Cafe de Paris", "lat": "43.73", "lon": "7.42",
        "address": {
            "road": "Main St", "house_number": "12", "city": "Monaco",
            "postcode": "98000", "country_code": "mc",
        },
    }])
    assert nominatim == [{
        "type": "Feature", "id": "99",
        "geometry": {"type": "Point", "coordinates": [7.42, 43.73]},
        "properties": {
            "name": "Cafe de Paris",
            "address": {
                "country_code": "mc", "locality": "Monaco",
                "postcode": "98000", "street": "Main St", "number": "12",
            },
        },
    }]
    photon = bench.normalize_provider_response("photon", {
        "features": [{
            "geometry": {"coordinates": [7.42, 43.73]},
            "properties": {
                "name": "Cafe de Paris", "osm_id": 7, "countrycode": "MC",
                "street": "Main St", "housenumber": "12",
            },
        }],
    })
    assert photon[0]["properties"]["address"]["number"] == "12"
    assert photon[0]["geometry"]["coordinates"] == [7.42, 43.73]


def test_external_scoring_uses_semantics_not_provider_ids():
    case = dict(PLACE_CASE, expected_name="Cafe de Paris", tolerance_km=1.0)
    features = [
        feature("aa-bb", 50.0, 50.0, name="Unrelated"),
        feature("provider-specific-id", 7.42, 43.73, name="Cafe de Paris"),
    ]
    rank, matched, _ = bench.score_case(case, features, provider="nominatim")
    assert rank == 2
    assert matched == pytest.approx(0.0, abs=1e-6)


def test_semantic_place_scoring_does_not_accept_name_prefixes():
    case = dict(PLACE_CASE, expected_name="Starbucks Reserve", tolerance_km=1.0)
    result = feature("provider-id", 7.42, 43.73, name="Starbucks")
    rank, _, _ = bench.score_case(case, [result], provider="nominatim")
    assert rank is None


def test_overture_comparison_scoring_uses_the_same_semantic_gold():
    case = dict(PLACE_CASE, expected_name="Cafe de Paris", tolerance_km=1.0)
    features = [
        feature(case["expected_gers_id"], 50.0, 50.0, name="Unrelated"),
        feature("different-overture-id", 7.42, 43.73, name="Cafe de Paris"),
    ]
    exact_rank, _, _ = bench.score_case(case, features, provider="overture")
    semantic_rank, _, _ = bench.score_case(
        case, features, provider="overture", semantic_scoring=True)
    assert exact_rank == 1
    assert semantic_rank == 2


def test_external_address_scoring_requires_number_street_and_distance():
    case = {
        "kind": "address", "params": {"number": "12", "street": "Main St"},
        "expected_gers_id": "overture-only", "expected_lat": 47.61,
        "expected_lon": -122.33, "tolerance_km": 1.0,
    }
    wrong = feature("overture-only", -122.33, 47.61)
    wrong["properties"]["address"] = {"number": "13", "street": "Main St"}
    right = feature("external", -122.33, 47.61)
    right["properties"]["address"] = {"number": "12", "street": "Main Street"}
    rank, _, _ = bench.score_case(case, [wrong, right], provider="photon")
    assert rank == 2


def test_overture_semantic_address_scoring_accepts_flat_v2_properties():
    case = {
        "kind": "address", "params": {"number": "12", "street": "Main St"},
        "expected_gers_id": "different-id", "expected_lat": 47.61,
        "expected_lon": -122.33, "tolerance_km": 1.0,
    }
    result = feature("overture-id", -122.33, 47.61)
    result["properties"].update({"number": "12", "street": "Main Street"})
    rank, _, _ = bench.score_case(
        case, [result], provider="overture", semantic_scoring=True)
    assert rank == 1


def test_runner_scores_a_hit_and_captures_data_version():
    body = {"type": "FeatureCollection",
            "features": [feature("AA-BB", 7.42, 43.73, name="Cafe de Paris")]}
    runner = make_runner(
        FakeResponse(200, body, {"X-Geocoder-Build": "2026-07-27.1"}))
    result = runner.execute(PLACE_CASE)
    assert result["rank"] == 1
    assert result["found_at_1"] is True
    assert result["found_at_10"] is True
    assert result["error"] is None
    assert runner.data_version == "2026-07-27.1"
    assert runner.session.calls[0][2]["User-Agent"] == bench.USER_AGENT


def test_external_runner_normalizes_and_scores_a_hit():
    body = [{
        "place_id": 5, "name": "Cafe de Paris", "lat": "43.73", "lon": "7.42",
        "address": {"city": "Monaco"},
    }]
    runner = make_runner(FakeResponse(200, body), provider="nominatim")
    case = dict(PLACE_CASE, expected_name="Cafe de Paris", tolerance_km=1.0)
    result = runner.execute(case)
    assert result["provider"] == "nominatim"
    assert result["capability"] == "supported"
    assert result["rank"] == 1


def test_external_old_place_case_is_unscorable_without_http_request():
    runner = make_runner(FakeResponse(200, []), provider="nominatim")
    result = runner.execute(PLACE_CASE)
    assert result["capability"] == "unscorable"
    assert "regenerate" in result["capability_reason"]
    assert runner.session.calls == []


def test_external_tester_case_without_name_is_explicitly_unscorable():
    case = {
        "id": "tester:empty-name", "kind": "tester", "query": "12 Main St",
        "expected_name": "", "expected_lat": 47.61, "expected_lon": -122.33,
    }
    runner = make_runner(FakeResponse(200, []), provider="nominatim")
    result = runner.execute(case)
    assert result["capability"] == "unscorable"
    assert runner.session.calls == []


def test_success_with_wrong_response_shape_is_an_error():
    runner = make_runner(FakeResponse(200, {"unexpected": []}), provider="photon")
    case = dict(PLACE_CASE, expected_name="Cafe de Paris", tolerance_km=1.0)
    result = runner.execute(case)
    assert result["error"] == "invalid response shape"
    assert result["found_at_10"] is False


def test_runner_aborts_on_release_unavailable():
    runner = make_runner(FakeResponse(
        503, {"error": "release_unavailable",
              "message": "no v2 geocoder release is currently available"}))
    with pytest.raises(bench.ReleaseUnavailableError):
        runner.execute(PLACE_CASE)
    # The aborted request is not recorded as a scored result.
    assert runner.results == []


def test_other_503s_do_not_abort_but_record_an_error():
    runner = make_runner(FakeResponse(
        503, {"error": "capability_unavailable", "message": "Places data"}))
    result = runner.execute(PLACE_CASE)
    assert result["error"] == "http 503"
    assert result["found_at_10"] is False


def test_runner_retries_a_429_once_and_recovers():
    body = {"features": [feature("AA-BB", 7.42, 43.73)]}
    sleeps = []
    runner = bench.Runner("https://example.test", interval=0, timeout=1,
                          sleep_fn=sleeps.append, monotonic_fn=lambda: 0.0)
    runner.session = FakeSession([
        FakeResponse(429, {}, {"Retry-After": "2"}),
        FakeResponse(200, body),
    ])
    result = runner.execute(PLACE_CASE)
    assert result["rank"] == 1
    assert 2.0 in sleeps
    assert len(runner.session.calls) == 2


def test_runner_paces_requests_by_interval():
    clock = {"now": 100.0}
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    runner = bench.Runner("https://example.test", interval=1.5, timeout=1,
                          sleep_fn=fake_sleep,
                          monotonic_fn=lambda: clock["now"])
    runner.session = FakeSession(FakeResponse(200, {"features": []}))
    runner.execute(PLACE_CASE)
    runner.execute(PLACE_CASE)
    assert sleeps == [1.5]


def test_overture_allows_zero_interval_for_local_runs():
    assert bench.PROVIDER_MIN_INTERVALS["overture"] == 0.0


def test_cli_defaults_to_overture_only_and_preserves_legacy_summary(
        tmp_path, monkeypatch):
    case = dict(PLACE_CASE, expected_name="Cafe de Paris", tolerance_km=1.0)
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps({
        "schema": bench.CASES_SCHEMA, "meta": {}, "cases": [case],
    }), encoding="utf-8")
    output_path = tmp_path / "results.json"
    session = FakeSession(FakeResponse(
        200,
        {"features": [feature("AA-BB", 7.42, 43.73, name="Cafe de Paris")]},
        {"X-Geocoder-Build": "test-build"},
    ))
    monkeypatch.setattr(bench.requests, "Session", lambda: session)
    assert bench.main([
        "run", "--cases", str(cases_path), "--skip-builtin",
        "--geocoder-tester", str(tmp_path / "missing"),
        "--output", str(output_path),
    ]) == 0
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert list(payload["provider_summaries"]) == ["overture"]
    assert payload["summary"] == payload["provider_summaries"]["overture"]
    assert payload["meta"]["primary_provider"] == "overture"
    assert payload["meta"]["providers"]["overture"]["data_version"] == "test-build"
    assert {row["provider"] for row in payload["results"]} == {"overture"}


@pytest.mark.parametrize(
    "exact_only_option", [("--compare", "baseline.json"), ("--assert-recall", "0.9")])
def test_cli_rejects_exact_only_gates_in_provider_comparison(
        tmp_path, exact_only_option):
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps({
        "schema": bench.CASES_SCHEMA,
        "meta": {},
        "cases": [dict(
            PLACE_CASE, expected_name="Cafe de Paris", tolerance_km=1.0)],
    }), encoding="utf-8")
    assert bench.main([
        "run", "--cases", str(cases_path), "--skip-builtin",
        "--geocoder-tester", str(tmp_path / "missing"),
        "--provider", "overture", "--provider", "nominatim",
        *exact_only_option,
    ]) == 2


# ---------------------------------------------------------------------------
# geocoder-tester loading


def test_geocoder_tester_loader_degrades_gracefully_when_absent(tmp_path):
    assert bench.load_geocoder_tester_cases(tmp_path / "missing", 10, 1) == []


def test_geocoder_tester_loader_parses_semicolon_csv(tmp_path):
    csv_path = tmp_path / "world" / "test_cities.csv"
    csv_path.parent.mkdir()
    csv_path.write_text(
        "query;expected_name;expected_coordinate\n"
        "Paris;Paris;48.8566,2.3522,1000\n"
        "Nowhere;;bad,coords\n"
        ";Empty;1.0,2.0\n",
        encoding="utf-8",
    )
    cases = bench.load_geocoder_tester_cases(tmp_path / "world", 10, 1)
    assert len(cases) == 1
    assert cases[0]["kind"] == "tester"
    assert cases[0]["query"] == "Paris"
    assert cases[0]["expected_lat"] == pytest.approx(48.8566)
    limited = bench.load_geocoder_tester_cases(tmp_path / "world", 0, 1)
    assert limited == []


# ---------------------------------------------------------------------------
# Synthetic-artifact loader test (skipped when pyarrow is unavailable)


def test_sample_loader_reads_synthetic_positions_pack(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    pack_dir = tmp_path / "families" / "places" / "positions"
    pack_dir.mkdir(parents=True)
    table = pa.table({
        "feature_id": pa.array([bytes([7] * 16)], pa.binary()),
        "partition_cell": ["cb7e"],
        "longitude": [7.42],
        "latitude": [43.73],
        "primary_name": ["Cafe de Paris"],
        "brand_name": [""],
        "category": ["cafe"],
        "locality": ["Monaco"],
        "region": [""],
        "country": ["MC"],
        "confidence_rank": pa.array([200], pa.uint8()),
        "source_object_index": pa.array([1], pa.uint32()),
        "source_row_group": pa.array([2], pa.uint32()),
        "source_row_index": pa.array([3], pa.uint64()),
    })
    pq.write_table(table, pack_dir / "pack.parquet")
    # A non-positions parquet with a different schema must be ignored.
    other_dir = tmp_path / "families" / "places" / "positions-other"
    other_dir.mkdir()
    pq.write_table(pa.table({"unrelated": [1]}), other_dir / "x.parquet")

    records, packs = bench.load_place_records(tmp_path)
    assert packs == 1
    assert len(records) == 1
    cases = bench.build_place_cases(records, seed=1, per_stratum=1, max_cases=10)
    assert cases[0]["expected_gers_id"] == "07070707-0707-0707-0707-070707070707"

    assert bench.load_address_records(tmp_path) == ([], 0)


def test_r2_sampling_is_an_explicit_stub():
    with pytest.raises(NotImplementedError):
        bench.load_r2_records("staging/global-v2/whatever")
