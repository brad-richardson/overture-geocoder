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


def test_aggregate_and_summary_math():
    rows = [
        {"kind": "place", "strata": {"country": "MC"}, "error": None,
         "rank": 1, "found_at_1": True, "found_at_10": True,
         "top1_distance_km": 0.1, "ms": 100.0},
        {"kind": "place", "strata": {"country": "MC"}, "error": None,
         "rank": 4, "found_at_1": False, "found_at_10": True,
         "top1_distance_km": 2.0, "ms": 200.0},
        {"kind": "place", "strata": {"country": "FR"}, "error": None,
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
    assert summary["overall"]["recall_at_1"] == 0.5
    assert summary["overall"]["recall_at_10"] == 0.75
    assert summary["self_recall"]["n"] == 4
    assert summary["self_recall"]["recall_at_10"] == round(2 / 3, 3)
    assert summary["by_kind"]["seam"]["recall_at_1"] == 1.0
    assert summary["by_stratum"]["place:country=MC"]["recall_at_10"] == 1.0
    # mrr counts misses as zero contribution
    assert summary["by_stratum"]["place:country=MC"]["mrr"] == round(
        (1 + 1 / 4) / 2, 3)
    assert bench.self_recall_at_10(summary) == round(2 / 3, 3)


def test_aggregate_with_no_scored_rows_yields_none_metrics():
    stats = bench.summarize_results(
        [{"kind": "place", "error": "boom", "rank": None, "found_at_1": False,
          "found_at_10": False, "top1_distance_km": None, "ms": 10.0}])
    assert stats["overall"]["recall_at_10"] is None
    assert bench.self_recall_at_10(stats) is None


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

    def get(self, url, params, timeout):
        self.calls.append((url, dict(params)))
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def make_runner(responses):
    runner = bench.Runner("https://example.test", interval=0, timeout=1,
                          sleep_fn=lambda _s: None, monotonic_fn=lambda: 0.0)
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
