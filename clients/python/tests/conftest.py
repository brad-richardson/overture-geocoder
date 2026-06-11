"""Pytest fixtures for Overture Geocoder tests.

Mock payloads mirror the ACTUAL worker response shapes
(crates/geocoder-worker/src/handlers.rs, crates/geocoder-core/src/types.rs):

- /search returns ``{"results": [{gers_id, name, type, lat, lon,
  bbox: [min_lon, min_lat, max_lon, max_lat], importance, country?, region?}]}``
- /reverse returns a SINGLE JSON object (not a list)
- /id/{gers_id} returns ``{"id": ..., "bbox": {xmin, ymin, xmax, ymax}}``
- /health returns ``{"status": "ok"|"error", "version"?, "error"?}``
"""

import pytest


# Canonical /search response shape: dict with a "results" array.
MOCK_SEARCH_RESPONSE = {
    "results": [
        {
            "gers_id": "abc-123",
            "name": "Boston, MA",
            "type": "locality",
            "lat": 42.3601,
            "lon": -71.0589,
            "bbox": [-71.191, 42.227, -70.923, 42.397],
            "importance": 0.85,
            "country": "US",
            "region": "US-MA",
        },
        {
            "gers_id": "def-456",
            "name": "Cambridge, MA",
            "type": "locality",
            "lat": 42.3736,
            "lon": -71.1097,
            "bbox": [-71.161, 42.352, -71.064, 42.404],
            "importance": 0.75,
            "country": "US",
            "region": "US-MA",
        },
    ]
}

# Canonical /reverse response shape: a single object, NOT a list.
MOCK_REVERSE_RESPONSE = {
    "gers_id": "ghi-789",
    "primary_name": "Suffolk County, MA",
    "subtype": "county",
    "lat": 42.3544455,
    "lon": -70.9788771,
    "boundingbox": [-71.1912442, 42.2279149, -70.8955683, 42.4501176],
    "distance_km": 6.694295,
    "confidence": "medium",
    "hierarchy": [
        {"gers_id": "ghi-789", "subtype": "county", "name": "Suffolk County, MA"},
        {"gers_id": "jkl-012", "subtype": "region", "name": "Massachusetts, MA"},
        {"gers_id": "mno-345", "subtype": "country", "name": "United States, US"},
    ],
}

# /id/{gers_id} response shape.
MOCK_ID_LOOKUP_RESPONSE = {
    "id": "abc-123",
    "bbox": {
        "xmin": -71.191,
        "ymin": 42.227,
        "xmax": -70.923,
        "ymax": 42.397,
    },
}

# /health response shape.
MOCK_HEALTH_RESPONSE = {"status": "ok", "version": "2026-05-25.0"}

MOCK_GEOJSON_RESPONSE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "id": "abc-123",
            "properties": {
                "gers_id": "abc-123",
                "name": "Boston, MA",
                "type": "locality",
                "importance": 0.85,
                "country": "US",
                "region": "US-MA",
            },
            "bbox": [-71.191, 42.227, -70.923, 42.397],
            "geometry": {
                "type": "Point",
                "coordinates": [-71.0589, 42.3601],
            },
        },
    ],
}


@pytest.fixture
def mock_search_response():
    """Return mock /search response (canonical dict shape)."""
    return MOCK_SEARCH_RESPONSE


@pytest.fixture
def mock_reverse_response():
    """Return mock /reverse response (single object)."""
    return MOCK_REVERSE_RESPONSE


@pytest.fixture
def mock_id_lookup_response():
    """Return mock /id/{gers_id} response."""
    return MOCK_ID_LOOKUP_RESPONSE


@pytest.fixture
def mock_health_response():
    """Return mock /health response."""
    return MOCK_HEALTH_RESPONSE


@pytest.fixture
def mock_geojson_response():
    """Return mock GeoJSON response."""
    return MOCK_GEOJSON_RESPONSE
