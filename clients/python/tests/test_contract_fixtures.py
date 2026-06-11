"""Contract tests against saved live responses from https://geocoder.bradr.dev.

The JSON files under tests/fixtures/live/ were fetched once from the
production worker (2026-06-11) and checked into the repo. These tests parse
them with the client's parsers, pinning the production contract without any
network access at test time.

To refresh the fixtures:
    curl -sS "https://geocoder.bradr.dev/search?q=boston&limit=5" \
        -o tests/fixtures/live/search_boston.json
    curl -sS "https://geocoder.bradr.dev/reverse?lat=42.36&lon=-71.06" \
        -o tests/fixtures/live/reverse_boston.json
    curl -sS "https://geocoder.bradr.dev/health" \
        -o tests/fixtures/live/health.json
"""

import json
from pathlib import Path

import pytest

from overture_geocoder import OvertureGeocoder

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "live"


def load_fixture(name: str):
    with open(FIXTURES_DIR / name) as f:
        return json.load(f)


@pytest.fixture
def client():
    geocoder = OvertureGeocoder()
    yield geocoder
    geocoder.close()


class TestSearchContract:
    """Pin the /search response contract."""

    def test_live_search_response_parses(self, client):
        data = load_fixture("search_boston.json")

        # Canonical envelope shape
        assert isinstance(data, dict)
        assert "results" in data

        results = client._parse_results(data, include_geocoder=True)

        assert len(results) >= 1
        boston = results[0]
        assert boston.gers_id
        assert "Boston" in boston.primary_name
        assert boston.type == "locality"
        assert isinstance(boston.lat, float)
        assert isinstance(boston.lon, float)
        # Boston is roughly at (42.36, -71.06)
        assert 42.0 < boston.lat < 43.0
        assert -72.0 < boston.lon < -70.0
        # bbox is [min_lon, min_lat, max_lon, max_lat]
        assert len(boston.boundingbox) == 4
        min_lon, min_lat, max_lon, max_lat = boston.boundingbox
        assert min_lon <= max_lon
        assert min_lat <= max_lat
        assert isinstance(boston.importance, float)
        assert boston.country == "US"
        assert boston.region == "US-MA"


class TestReverseContract:
    """Pin the /reverse response contract."""

    def test_live_reverse_response_parses(self, client):
        data = load_fixture("reverse_boston.json")

        # The worker returns a single object, not a list
        assert isinstance(data, dict)
        assert "gers_id" in data

        results = client._parse_reverse_results(data)

        assert len(results) == 1
        r = results[0]
        assert r.gers_id
        assert r.primary_name
        assert r.subtype
        assert isinstance(r.lat, float)
        assert isinstance(r.lon, float)
        assert len(r.boundingbox) == 4
        assert isinstance(r.distance_km, float)
        assert r.confidence in ("high", "medium", "low")
        assert r.hierarchy is not None
        assert len(r.hierarchy) >= 1
        for entry in r.hierarchy:
            assert entry.gers_id
            assert entry.subtype
            assert entry.name


class TestHealthContract:
    """Pin the /health response contract."""

    def test_live_health_response_shape(self):
        data = load_fixture("health.json")

        assert isinstance(data, dict)
        assert data["status"] in ("ok", "error")
        if data["status"] == "ok":
            assert "version" in data
