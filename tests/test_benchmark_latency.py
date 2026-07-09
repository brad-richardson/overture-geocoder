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
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.headers = {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, url, timeout):
        return self.response


def test_reverse_quality_accepts_context_suffix_and_accents():
    quality = benchmark_latency.evaluate_reverse_quality(
        {"primary_name": "Reykjavík, IS", "subtype": "locality"},
        {"reverse_name": "Reykjavik"},
    )

    assert quality["passed"] is True
    assert quality["reason"] is None


def test_reverse_quality_rejects_county_with_matching_city_name():
    quality = benchmark_latency.evaluate_reverse_quality(
        {"primary_name": "New York, NY", "subtype": "county"},
        {"reverse_name": "New York"},
    )

    assert quality["passed"] is False
    assert quality["reason"] == "expected subtype locality"


def test_reverse_quality_rejects_remote_or_missing_response():
    wrong_city = benchmark_latency.evaluate_reverse_quality(
        {"primary_name": "Boston, MA", "subtype": "locality"},
        {"reverse_name": "Tbilisi"},
    )
    missing = benchmark_latency.evaluate_reverse_quality(None, {"reverse_name": "Tbilisi"})

    assert wrong_city["passed"] is False
    assert wrong_city["reason"] == "locality name mismatch"
    assert missing["passed"] is False
    assert missing["reason"] == "missing or invalid JSON response"


def test_reverse_request_records_quality_and_summary():
    bench = benchmark_latency.Bench("https://example.test", interval=0, timeout=1)
    bench.session = FakeSession(
        FakeResponse(200, {"primary_name": "Thimphu, BT", "subtype": "locality"})
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
    }
    assert benchmark_latency.reverse_quality_failures(bench.samples) == []
