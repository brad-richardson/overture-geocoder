"""Unit tests for the offline pieces of the latency/quality benchmark."""

import importlib.util
import sys
import types
from pathlib import Path


# The latency script only needs requests when it executes a live benchmark.
# Keep these unit tests hermetic even in a minimal test environment.
requests_stub = types.ModuleType("requests")
requests_stub.Session = lambda: None
requests_stub.RequestException = Exception
sys.modules.setdefault("requests", requests_stub)


SCRIPT = Path(__file__).parent.parent / "scripts" / "benchmark_latency.py"
spec = importlib.util.spec_from_file_location("benchmark_latency", SCRIPT)
benchmark_latency = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark_latency)


class FakeResponse:
    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response):
        self.responses = response if isinstance(response, list) else [response]
        self.calls = 0

    def get(self, url, timeout):
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class TimedFakeSession(FakeSession):
    def __init__(self, responses, clock):
        super().__init__(responses)
        self.clock = clock
        self.attempt_times = []

    def get(self, url, timeout):
        self.attempt_times.append(self.clock.now)
        return super().get(url, timeout)


def test_reverse_quality_accepts_context_suffix_and_accents():
    quality = benchmark_latency.evaluate_reverse_quality(
        {"gers_id": "reykjavik", "primary_name": "Reykjavík, IS", "subtype": "locality"},
        {"reverse_name": "Reykjavik"},
    )

    assert quality["passed"] is True
    assert quality["reason"] is None


def test_reverse_quality_accepts_native_script_variant():
    quality = benchmark_latency.evaluate_reverse_quality(
        {"gers_id": "ulaanbaatar", "primary_name": "Улаанбаатар, MN", "subtype": "locality"},
        {"reverse_name": "Ulaanbaatar", "reverse_alt_names": ["Улаанбаатар"]},
    )

    assert quality["passed"] is True
    assert quality["reason"] is None


def test_reverse_quality_rejects_county_with_matching_city_name():
    quality = benchmark_latency.evaluate_reverse_quality(
        {"gers_id": "new-york", "primary_name": "New York, NY", "subtype": "county"},
        {"reverse_name": "New York"},
    )

    assert quality["passed"] is False
    assert quality["reason"] == "accepted container missing"


def test_reverse_quality_rejects_remote_or_missing_response():
    wrong_city = benchmark_latency.evaluate_reverse_quality(
        {"gers_id": "boston", "primary_name": "Boston, MA", "subtype": "locality"},
        {"reverse_name": "Tbilisi"},
    )
    missing = benchmark_latency.evaluate_reverse_quality(None, {"reverse_name": "Tbilisi"})

    assert wrong_city["passed"] is False
    assert wrong_city["reason"] == "accepted container missing"
    assert missing["passed"] is False
    assert missing["reason"] == "missing or invalid JSON response"


def test_reverse_request_records_quality_and_summary():
    bench = benchmark_latency.Bench("https://example.test", interval=0, timeout=1)
    bench.session = FakeSession(
        FakeResponse(200, {"gers_id": "thimphu", "primary_name": "Thimphu, BT", "subtype": "locality"})
    )

    body = bench.request(
        "reverse",
        "cold",
        "bhutan",
        "/reverse?lat=27.4716&lon=89.6386",
        {"reverse_name": "Thimphu"},
    )

    assert body["primary_name"] == "Thimphu, BT"
    assert bench.samples[0]["reverse_quality"]["passed"] is True
    assert benchmark_latency.summarize_reverse_quality(bench.samples) == {
        "n": 1,
        "passed": 1,
        "failed": 0,
        "pass_rate": 1.0,
        "dimensions": {
            "container": {"n": 1, "passed": 1, "failed": 0},
            "country": {"n": 0, "passed": 0, "failed": 0},
            "region": {"n": 0, "passed": 0, "failed": 0},
            "hierarchy_coherence": {"n": 0, "passed": 0, "failed": 0},
            "reverse_id": {"n": 1, "passed": 1, "failed": 0},
        },
        "repeat_stability": {"n": 1, "stable": 1, "unstable": 0},
        "coverage": {"expected": 1, "observed": 1, "missing": []},
    }
    assert benchmark_latency.reverse_quality_failures(bench.samples) == []


def test_reverse_quality_accepts_specific_container_and_expected_region():
    quality = benchmark_latency.evaluate_reverse_quality(
        {
            "gers_id": "manhattan",
            "primary_name": "Manhattan, NY",
            "subtype": "locality",
            "hierarchy": [
                {"gers_id": "manhattan", "name": "Manhattan, NY", "subtype": "locality"},
                {"gers_id": "ny", "name": "New York, NY", "subtype": "region"},
                {"gers_id": "us", "name": "United States, US", "subtype": "country"},
            ],
        },
        {
            "reverse_name": "New York",
            "accepted_container_names": ["Manhattan"],
            "expected_country": "US",
            "expected_country_names": ["United States"],
            "expected_region_names": ["New York"],
        },
    )

    assert quality["passed"] is True
    assert quality["dimensions"] == {
        "container": True,
        "country": True,
        "region": True,
        "hierarchy_coherence": True,
        "reverse_id": True,
    }


def test_reverse_quality_catches_bhutan_cross_country_overlap():
    quality = benchmark_latency.evaluate_reverse_quality(
        {
            "gers_id": "tibet-county",
            "primary_name": "Shigatse, CN-XZ",
            "subtype": "county",
            "hierarchy": [
                {"gers_id": "tibet", "name": "Tibet, CN-XZ", "subtype": "region"},
                {"gers_id": "bhutan", "name": "Bhutan, BT", "subtype": "country"},
            ],
        },
        {
            "reverse_name": "Thimphu",
            "expected_country": "BT",
            "expected_country_names": ["Bhutan"],
        },
    )

    assert quality["passed"] is False
    assert quality["dimensions"]["country"] is True
    assert quality["dimensions"]["hierarchy_coherence"] is False
    assert "observable hierarchy country mismatch" in quality["reason"]


def test_reverse_quality_catches_madagascar_wrong_country_hierarchy():
    quality = benchmark_latency.evaluate_reverse_quality(
        {
            "gers_id": "arrondissement",
            "primary_name": "6e Arrondissement, MG",
            "subtype": "county",
            "hierarchy": [
                {"gers_id": "fiji-region", "name": "Eastern, FJ-E", "subtype": "region"},
                {"gers_id": "fiji", "name": "Viti, FJ", "subtype": "country"},
            ],
        },
        {
            "reverse_name": "Antananarivo",
            "expected_country": "MG",
            "expected_country_names": ["Madagascar"],
        },
    )

    assert quality["passed"] is False
    assert quality["dimensions"]["country"] is False
    assert quality["dimensions"]["hierarchy_coherence"] is False


def test_reverse_quality_scores_unique_target_and_stability_separately():
    stable_quality = {
        "passed": True,
        "dimensions": {"container": True, "country": None,
                       "region": None, "hierarchy_coherence": None,
                       "reverse_id": True},
        "result_signature": ["one", "Town", "locality", ["one"]],
    }
    changed_quality = {
        **stable_quality,
        "result_signature": ["two", "Other Town", "locality", ["two"]],
    }
    samples = [
        {"endpoint": "reverse", "target": "a", "reverse_quality": stable_quality},
        {"endpoint": "reverse", "target": "a", "reverse_quality": stable_quality},
        {"endpoint": "reverse", "target": "b", "reverse_quality": stable_quality},
        {"endpoint": "reverse", "target": "b", "reverse_quality": changed_quality},
    ]

    summary = benchmark_latency.summarize_reverse_quality(samples)

    assert summary["n"] == 2
    assert summary["passed"] == 2
    assert summary["repeat_stability"] == {"n": 2, "stable": 1, "unstable": 1}


def test_reverse_quality_any_warm_failure_fails_unique_target():
    passing = {
        "passed": True,
        "dimensions": {"container": True, "country": True, "region": None,
                       "hierarchy_coherence": True, "reverse_id": True},
        "result_signature": ["one", "Town", "locality", ["one"]],
    }
    failing = {
        **passing,
        "passed": False,
        "dimensions": {**passing["dimensions"], "country": False},
        "result_signature": ["two", "Wrong", "locality", ["two"]],
    }
    samples = [
        {"endpoint": "reverse", "phase": "cold", "target": "a",
         "reverse_quality": passing},
        {"endpoint": "reverse", "phase": "warm", "target": "a",
         "reverse_quality": failing},
    ]

    summary = benchmark_latency.summarize_reverse_quality(samples, ["a"])

    assert summary["n"] == 1
    assert summary["passed"] == 0
    assert summary["dimensions"]["country"] == {"n": 1, "passed": 0, "failed": 1}
    failures = benchmark_latency.reverse_quality_failures(samples)
    assert len(failures) == 1
    assert failures[0]["phase"] == "warm"


def test_reverse_id_consistency_scored_once_per_unique_id():
    assert benchmark_latency.evaluate_id_consistency({"id": "abc"}, "abc")["passed"] is True
    assert benchmark_latency.evaluate_id_consistency({"id": "other"}, "abc")["passed"] is False
    samples = [
        {"id_consistency": {"expected_id": "abc", "observed_id": "abc", "passed": True}},
        {"id_consistency": {"expected_id": "abc", "observed_id": "abc", "passed": True}},
    ]
    assert benchmark_latency.summarize_id_consistency(samples) == {
        "n": 1,
        "passed": 1,
        "failed": 0,
        "pass_rate": 1.0,
        "repeat_stability": {"n": 1, "stable": 1, "unstable": 0},
        "coverage": {"expected": 1, "observed": 1, "missing": 0},
    }


def test_id_consistency_any_warm_failure_fails_unique_target():
    samples = [
        {"target": "a", "phase": "cold", "id_consistency": {
            "expected_id": "abc", "observed_id": "abc", "passed": True}},
        {"target": "a", "phase": "warm", "id_consistency": {
            "expected_id": "abc", "observed_id": None, "passed": False}},
    ]

    summary = benchmark_latency.summarize_id_consistency(samples, expected_n=1)

    assert summary["passed"] == 0
    assert summary["failed"] == 1
    failures = benchmark_latency.id_consistency_failures(samples)
    assert len(failures) == 1
    assert failures[0]["phase"] == "warm"


def test_rate_limit_retries_same_request_and_records_recovery():
    sleeps = []
    bench = benchmark_latency.Bench(
        "https://example.test", interval=0, timeout=1, sleep_fn=sleeps.append
    )
    bench.session = FakeSession([
        FakeResponse(429, {}, {"Retry-After": "2"}),
        FakeResponse(200, {"gers_id": "abc", "primary_name": "Town", "subtype": "locality"}),
    ])

    body = bench.request("reverse", "cold", "a", "/reverse", {"reverse_name": "Town"})

    assert body["gers_id"] == "abc"
    assert sleeps == [2.0]
    assert bench.samples[0]["attempts"] == 2
    assert bench.samples[0]["rate_limited"] is True
    assert bench.samples[0]["error"] is None


def test_persistent_rate_limit_is_recorded_as_quality_failure_without_sleeping():
    sleeps = []
    bench = benchmark_latency.Bench(
        "https://example.test", interval=0, timeout=1,
        sleep_fn=sleeps.append, rate_limit_retries=1,
    )
    bench.session = FakeSession(FakeResponse(429, {}, {"Retry-After": "999"}))

    assert bench.request(
        "reverse", "cold", "a", "/reverse", {"reverse_name": "Town"}
    ) is None

    sample = bench.samples[0]
    assert sleeps == [60.0]
    assert sample["status"] == 429
    assert sample["error"] == "rate limited after 2 attempt(s)"
    assert sample["reverse_quality"]["passed"] is False


def test_next_request_is_paced_from_retried_http_attempt():
    clock = FakeClock()
    bench = benchmark_latency.Bench(
        "https://example.test", interval=1, timeout=1,
        sleep_fn=clock.sleep, monotonic_fn=clock.monotonic,
    )
    bench.session = TimedFakeSession([
        FakeResponse(429, {}, {"Retry-After": "2"}),
        FakeResponse(200, {"ok": True}),
        FakeResponse(200, {"ok": True}),
    ], clock)

    bench.request("health", "warm", "first", "/health")
    bench.request("health", "warm", "second", "/health")

    assert bench.session.attempt_times == [100.0, 102.0, 103.0]
    assert clock.sleeps == [2.0, 1.0]


def test_recovered_rate_limit_sample_is_excluded_from_latency_summary():
    samples = [
        {"endpoint": "reverse", "phase": "warm", "target": "a",
         "status": 200, "error": None, "ms": 999.0, "rate_limited": True},
        {"endpoint": "reverse", "phase": "warm", "target": "b",
         "status": 200, "error": None, "ms": 100.0, "rate_limited": False},
    ]

    summary = benchmark_latency.summarize(samples)

    assert summary["reverse/warm"]["n"] == 1
    assert summary["reverse/warm"]["p50"] == 100.0


def test_quality_summaries_report_degraded_expected_coverage():
    reverse = benchmark_latency.summarize_reverse_quality([], ["a", "b"])
    ids = benchmark_latency.summarize_id_consistency([], expected_n=2)

    assert reverse["coverage"] == {
        "expected": 2, "observed": 0, "missing": ["a", "b"]
    }
    assert ids["coverage"] == {"expected": 2, "observed": 0, "missing": 2}
    assert benchmark_latency.quality_coverage_failures(reverse, ids) == [
        "missing reverse targets: a, b",
        "missing reverse-derived ID lookups: 0/2",
    ]


def test_reverse_quality_requires_gers_id():
    quality = benchmark_latency.evaluate_reverse_quality(
        {"primary_name": "Town", "subtype": "locality"},
        {"reverse_name": "Town"},
    )

    assert quality["passed"] is False
    assert quality["dimensions"]["reverse_id"] is False
    assert "reverse GERS ID missing" in quality["reason"]


def test_comparison_skips_id_latency_when_sample_source_changed(capsys):
    stats = {"n": 1, "mean": 10, "p50": 10, "p95": 10, "min": 10, "max": 10}
    baseline = {
        "meta": {"timestamp": "before", "git_sha": "old"},
        "summary": {"id/warm": stats, "reverse/warm": stats},
    }
    current = {
        "meta": {"timestamp": "now", "git_sha": "new", "id_source": "reverse"},
        "summary": {"id/warm": stats, "reverse/warm": stats},
    }

    benchmark_latency.print_comparison(baseline, current)
    output = capsys.readouterr().out

    assert "ID latency comparison skipped" in output
    assert output.count("id/warm") == 0
    assert output.count("reverse/warm") == 1
