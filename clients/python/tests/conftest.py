"""Pytest fixtures for Overture Geocoder tests."""

import pytest
import httpx


# Mock response data
MOCK_SEARCH_RESULTS = [
    {
        "gers_id": "abc-123",
        "primary_name": "123 Main St, Boston, MA 02101",
        "lat": "42.3601000",
        "lon": "-71.0589000",
        "boundingbox": ["42.3600000", "42.3602000", "-71.0590000", "-71.0588000"],
        "importance": 0.85,
        "type": "address",
    },
    {
        "gers_id": "def-456",
        "primary_name": "456 Oak Ave, Boston, MA 02102",
        "lat": "42.3611000",
        "lon": "-71.0599000",
        "boundingbox": ["42.3610000", "42.3612000", "-71.0600000", "-71.0598000"],
        "importance": 0.75,
        "type": "address",
    },
]

MOCK_SEARCH_RESULTS_WITH_ADDRESS = [
    {
        **MOCK_SEARCH_RESULTS[0],
        "address": {
            "house_number": "123",
            "road": "Main St",
            "city": "Boston",
            "state": "MA",
            "postcode": "02101",
            "country": "United States",
            "country_code": "us",
        },
    },
]

MOCK_GEOJSON_RESPONSE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "id": "abc-123",
            "properties": {
                "gers_id": "abc-123",
                "primary_name": "123 Main St, Boston, MA 02101",
                "importance": 0.85,
            },
            "bbox": [42.36, 42.3602, -71.059, -71.0588],
            "geometry": {
                "type": "Point",
                "coordinates": [-71.0589, 42.3601],
            },
        },
    ],
}


@pytest.fixture
def mock_search_results():
    """Return mock search results."""
    return MOCK_SEARCH_RESULTS


@pytest.fixture
def mock_search_results_with_address():
    """Return mock search results with address details."""
    return MOCK_SEARCH_RESULTS_WITH_ADDRESS


@pytest.fixture
def mock_geojson_response():
    """Return mock GeoJSON response."""
    return MOCK_GEOJSON_RESPONSE
