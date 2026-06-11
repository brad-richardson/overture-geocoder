"""Tests for the Overture Geocoder Python client."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from overture_geocoder import (
    OvertureGeocoder,
    GeocoderResult,
    IdLookupResult,
    GeocoderError,
    GeocoderTimeoutError,
    GeocoderNetworkError,
    geocode,
)


def make_response(json_data=None, status_code=200, headers=None):
    """Build a MagicMock standing in for an httpx.Response."""
    response = MagicMock()
    response.is_success = 200 <= status_code < 300
    response.status_code = status_code
    response.reason_phrase = ""
    response.headers = headers or {}
    response.json.return_value = json_data
    return response


class TestOvertureGeocoderInit:
    """Tests for OvertureGeocoder initialization."""

    def test_default_configuration(self):
        """Should use default configuration values."""
        client = OvertureGeocoder()
        assert client.get_base_url() == "https://geocoder.bradr.dev"
        assert client.timeout == 30.0
        assert client.retries == 2
        client.close()

    def test_custom_configuration(self):
        """Should accept custom configuration."""
        client = OvertureGeocoder(
            base_url="https://api.example.com/",
            timeout=10.0,
            retries=3,
            retry_delay=0.5,
        )
        assert client.get_base_url() == "https://api.example.com"
        assert client.timeout == 10.0
        assert client.retries == 3
        assert client.retry_delay == 0.5
        client.close()

    def test_strips_trailing_slash(self):
        """Should strip trailing slash from base URL."""
        client = OvertureGeocoder(base_url="https://api.example.com/")
        assert client.get_base_url() == "https://api.example.com"
        client.close()

    def test_context_manager(self):
        """Should work as context manager."""
        with OvertureGeocoder() as client:
            assert isinstance(client, OvertureGeocoder)


class TestSearch:
    """Tests for search functionality."""

    def test_search_with_query_only(self, mock_search_response):
        """Should search with query only and unwrap the results envelope."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(mock_search_response)

        client = OvertureGeocoder(http_client=mock_client)
        results = client.search("Boston")

        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        assert "/search" in call_args[0][0]
        params = call_args[1]["params"]
        assert params["q"] == "Boston"
        assert params["limit"] == 10
        # The bogus jsonv2 format param must not be sent
        assert "format" not in params

        assert len(results) == 2
        assert results[0].gers_id == "abc-123"
        assert results[0].primary_name == "Boston, MA"
        assert results[0].lat == 42.3601
        assert results[0].lon == -71.0589
        assert results[0].boundingbox == [-71.191, 42.227, -70.923, 42.397]
        assert results[0].type == "locality"
        assert results[0].country == "US"
        assert results[0].region == "US-MA"

    def test_search_with_limit_option(self, mock_search_response):
        """Should search with limit option."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(mock_search_response)

        client = OvertureGeocoder(http_client=mock_client)
        results = client.search("Boston", limit=5)

        params = mock_client.get.call_args[1]["params"]
        assert params["limit"] == 5

        assert results[0].type == "locality"

    def test_search_with_bias_and_autocomplete(self, mock_search_response):
        """Should pass autocomplete and lat/lon bias params."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(mock_search_response)

        client = OvertureGeocoder(http_client=mock_client)
        client.search("Bos", autocomplete=True, lat=42.36, lon=-71.06)

        params = mock_client.get.call_args[1]["params"]
        assert params["autocomplete"] == 1
        assert params["lat"] == 42.36
        assert params["lon"] == -71.06

    def test_search_clamps_limit(self):
        """Should clamp limit to 1-40 range."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response({"results": []})

        client = OvertureGeocoder(http_client=mock_client)

        # Test upper bound
        client.search("test", limit=100)
        assert mock_client.get.call_args[1]["params"]["limit"] == 40

        # Test lower bound
        client.search("test", limit=0)
        assert mock_client.get.call_args[1]["params"]["limit"] == 1

        # Test negative
        client.search("test", limit=-5)
        assert mock_client.get.call_args[1]["params"]["limit"] == 1

    def test_search_tolerates_bare_list(self, mock_search_response):
        """Should tolerate a bare list response (defensive)."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(
            mock_search_response["results"]
        )

        client = OvertureGeocoder(http_client=mock_client)
        results = client.search("Boston")

        assert len(results) == 2
        assert results[0].primary_name == "Boston, MA"

    def test_search_with_custom_headers(self):
        """Should include custom headers."""
        with patch("httpx.Client") as mock_client_class:
            mock_instance = MagicMock()
            mock_instance.get.return_value = make_response({"results": []})
            mock_client_class.return_value = mock_instance

            client = OvertureGeocoder(
                headers={"X-API-Key": "test-key", "X-Custom": "value"}
            )
            client.search("test")

            # Check headers were passed to httpx.Client
            call_kwargs = mock_client_class.call_args[1]
            assert "X-API-Key" in call_kwargs["headers"]
            assert call_kwargs["headers"]["X-API-Key"] == "test-key"
            client.close()


class TestSearchGeoJSON:
    """Tests for search_geojson functionality."""

    def test_returns_geojson_feature_collection(self, mock_geojson_response):
        """Should return GeoJSON FeatureCollection."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(mock_geojson_response)

        client = OvertureGeocoder(http_client=mock_client)
        result = client.search_geojson("Boston")

        params = mock_client.get.call_args[1]["params"]
        assert params["format"] == "geojson"

        assert result["type"] == "FeatureCollection"
        assert len(result["features"]) == 1
        assert result["features"][0]["geometry"]["type"] == "Point"


class TestReverse:
    """Tests for reverse geocoding."""

    def test_reverse_wraps_single_object(self, mock_reverse_response):
        """Should wrap the single-object worker response in a list."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(mock_reverse_response)

        client = OvertureGeocoder(http_client=mock_client)
        results = client.reverse(42.36, -71.06)

        call_args = mock_client.get.call_args
        assert "/reverse" in call_args[0][0]
        params = call_args[1]["params"]
        assert params["lat"] == 42.36
        assert params["lon"] == -71.06
        assert "format" not in params

        assert len(results) == 1
        r = results[0]
        assert r.gers_id == "ghi-789"
        assert r.primary_name == "Suffolk County, MA"
        assert r.subtype == "county"
        assert r.confidence == "medium"
        assert r.confidence in ("high", "medium", "low")
        assert r.distance_km == pytest.approx(6.694295)
        assert r.boundingbox == [-71.1912442, 42.2279149, -70.8955683, 42.4501176]
        assert r.hierarchy is not None
        assert len(r.hierarchy) == 3
        assert r.hierarchy[0].subtype == "county"
        assert r.hierarchy[2].name == "United States, US"

    def test_reverse_404_returns_empty_list(self):
        """Should return [] when the worker responds 404 (no result)."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(
            None, status_code=404
        )

        client = OvertureGeocoder(http_client=mock_client)
        results = client.reverse(0.0, 0.0)

        assert results == []

    def test_reverse_other_errors_raise(self):
        """Should still raise on non-404 client errors."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(None, status_code=400)

        client = OvertureGeocoder(http_client=mock_client)

        with pytest.raises(GeocoderError) as exc_info:
            client.reverse(999.0, 999.0)
        assert exc_info.value.status == 400

    def test_reverse_geojson_passes_format(self, mock_reverse_response):
        """reverse_geojson should request format=geojson and return JSON as-is."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(mock_reverse_response)

        client = OvertureGeocoder(http_client=mock_client)
        result = client.reverse_geojson(42.36, -71.06)

        params = mock_client.get.call_args[1]["params"]
        assert params["format"] == "geojson"
        # Returned as-is (older workers ignore format=geojson)
        assert result == mock_reverse_response


class TestLookupId:
    """Tests for GERS ID lookup."""

    def test_lookup_id_success(self, mock_id_lookup_response):
        """Should return IdLookupResult with bbox."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(mock_id_lookup_response)

        client = OvertureGeocoder(http_client=mock_client)
        result = client.lookup_id("abc-123")

        call_args = mock_client.get.call_args
        assert "/id/abc-123" in call_args[0][0]

        assert isinstance(result, IdLookupResult)
        assert result.id == "abc-123"
        assert result.bbox.xmin == -71.191
        assert result.bbox.ymin == 42.227
        assert result.bbox.xmax == -70.923
        assert result.bbox.ymax == 42.397

    def test_lookup_id_404_returns_none(self):
        """Should return None for unknown IDs."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(None, status_code=404)

        client = OvertureGeocoder(http_client=mock_client)
        assert client.lookup_id("does-not-exist") is None

    def test_lookup_id_503_raises(self):
        """Should raise GeocoderError with status 503 when index unavailable."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(None, status_code=503)

        client = OvertureGeocoder(http_client=mock_client, retries=0)

        with pytest.raises(GeocoderError) as exc_info:
            client.lookup_id("abc-123")
        assert exc_info.value.status == 503


class TestHealth:
    """Tests for the health endpoint."""

    def test_health_ok(self, mock_health_response):
        """Should return health dict."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(mock_health_response)

        client = OvertureGeocoder(http_client=mock_client)
        result = client.health()

        call_args = mock_client.get.call_args
        assert "/health" in call_args[0][0]
        assert result["status"] == "ok"
        assert result["version"] == "2026-05-25.0"

    def test_health_error_returns_body(self):
        """Should return the error body on 503 rather than raising."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(
            {"status": "error", "error": "catalog unavailable"}, status_code=503
        )

        client = OvertureGeocoder(http_client=mock_client)
        result = client.health()

        assert result["status"] == "error"
        assert result["error"] == "catalog unavailable"


class TestErrorHandling:
    """Tests for error handling."""

    def test_raises_geocoder_error_on_4xx(self):
        """Should raise GeocoderError on 4xx response."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(None, status_code=400)

        client = OvertureGeocoder(http_client=mock_client)

        with pytest.raises(GeocoderError) as exc_info:
            client.search("test")

        assert exc_info.value.status == 400

    def test_raises_geocoder_error_on_5xx(self):
        """Should raise GeocoderError on 5xx response without retries."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(None, status_code=500)

        client = OvertureGeocoder(http_client=mock_client, retries=0)

        with pytest.raises(GeocoderError):
            client.search("test")

    def test_raises_timeout_error(self):
        """Should raise GeocoderTimeoutError on timeout."""
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.TimeoutException("Timeout")

        client = OvertureGeocoder(http_client=mock_client, retries=0)

        with pytest.raises(GeocoderTimeoutError):
            client.search("test")

    def test_raises_network_error(self):
        """Should raise GeocoderNetworkError on network failure."""
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.RequestError("Network failure")

        client = OvertureGeocoder(http_client=mock_client, retries=0)

        with pytest.raises(GeocoderNetworkError):
            client.search("test")


class TestRetryBehavior:
    """Tests for retry behavior."""

    def test_retries_on_5xx_errors(self, mock_search_response):
        """Should retry on 5xx errors with exponential backoff."""
        responses = [
            make_response(None, status_code=500),
            make_response(None, status_code=500),
            make_response(mock_search_response),
        ]
        mock_client = MagicMock()
        mock_client.get.side_effect = responses

        client = OvertureGeocoder(
            http_client=mock_client, retries=3, retry_delay=0.01
        )
        results = client.search("test")

        assert mock_client.get.call_count == 3
        assert len(results) == 2

    def test_retries_429_honoring_retry_after(self, mock_search_response):
        """Should retry 429s, sleeping per the Retry-After header (capped)."""
        responses = [
            make_response(None, status_code=429, headers={"Retry-After": "60"}),
            make_response(mock_search_response),
        ]
        mock_client = MagicMock()
        mock_client.get.side_effect = responses

        client = OvertureGeocoder(
            http_client=mock_client, retries=2, retry_delay=0.01
        )

        with patch("overture_geocoder.client.time.sleep") as mock_sleep:
            results = client.search("test")

        assert mock_client.get.call_count == 2
        assert len(results) == 2
        # Retry-After of 60s must be honored but capped at 30s
        mock_sleep.assert_called_once_with(30.0)

    def test_429_raises_when_retries_exhausted(self):
        """Should raise GeocoderError(429) once retries are exhausted."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(
            None, status_code=429, headers={"Retry-After": "1"}
        )

        client = OvertureGeocoder(
            http_client=mock_client, retries=1, retry_delay=0.01
        )

        with patch("overture_geocoder.client.time.sleep"):
            with pytest.raises(GeocoderError) as exc_info:
                client.search("test")

        assert exc_info.value.status == 429
        assert mock_client.get.call_count == 2

    def test_does_not_retry_on_4xx_errors(self):
        """Should not retry on non-429 4xx errors."""
        mock_client = MagicMock()
        mock_client.get.return_value = make_response(None, status_code=404)

        client = OvertureGeocoder(
            http_client=mock_client, retries=3, retry_delay=0.01
        )

        with pytest.raises(GeocoderError):
            client.search("test")

        assert mock_client.get.call_count == 1

    def test_retries_on_network_errors(self, mock_search_response):
        """Should retry on network errors."""
        mock_client = MagicMock()
        mock_client.get.side_effect = [
            httpx.RequestError("Network error"),
            make_response(mock_search_response),
        ]

        client = OvertureGeocoder(
            http_client=mock_client, retries=2, retry_delay=0.01
        )
        results = client.search("test")

        assert mock_client.get.call_count == 2
        assert len(results) == 2

    def test_retries_on_timeout(self, mock_search_response):
        """Should retry on timeout."""
        mock_client = MagicMock()
        mock_client.get.side_effect = [
            httpx.TimeoutException("Timeout"),
            make_response(mock_search_response),
        ]

        client = OvertureGeocoder(
            http_client=mock_client, retries=2, retry_delay=0.01
        )
        results = client.search("test")

        assert mock_client.get.call_count == 2
        assert len(results) == 2

    def test_backoff_is_exponential(self, mock_search_response):
        """Backoff delays should grow as retry_delay * 2^attempt (plus jitter)."""
        mock_client = MagicMock()
        mock_client.get.side_effect = [
            make_response(None, status_code=500),
            make_response(None, status_code=500),
            make_response(mock_search_response),
        ]

        client = OvertureGeocoder(
            http_client=mock_client, retries=2, retry_delay=1.0
        )

        with patch("overture_geocoder.client.time.sleep") as mock_sleep:
            client.search("test")

        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert len(delays) == 2
        # attempt 0: 1.0 <= d < 1.25; attempt 1: 2.0 <= d < 2.5
        assert 1.0 <= delays[0] <= 1.25
        assert 2.0 <= delays[1] <= 2.5


class TestGeocoderResult:
    """Tests for GeocoderResult dataclass."""

    def test_result_fields(self):
        """Should have correct fields."""
        result = GeocoderResult(
            gers_id="abc-123",
            primary_name="Boston, MA",
            lat=42.36,
            lon=-71.06,
            boundingbox=[-71.07, 42.35, -71.05, 42.37],
            importance=0.85,
            type="locality",
            country="US",
            region="US-MA",
        )

        assert result.gers_id == "abc-123"
        assert result.primary_name == "Boston, MA"
        assert result.lat == 42.36
        assert result.lon == -71.06
        assert result.importance == 0.85
        assert result.country == "US"
        assert result.region == "US-MA"

    def test_optional_fields_default_none(self):
        """country/region should default to None."""
        result = GeocoderResult(
            gers_id="abc-123",
            primary_name="Boston, MA",
            lat=42.36,
            lon=-71.06,
            boundingbox=[-71.07, 42.35, -71.05, 42.37],
            importance=0.85,
        )
        assert result.country is None
        assert result.region is None

    def test_get_geometry_without_geocoder(self):
        """Should raise error when getting geometry without geocoder."""
        result = GeocoderResult(
            gers_id="abc-123",
            primary_name="Boston, MA",
            lat=42.36,
            lon=-71.06,
            boundingbox=[-71.07, 42.35, -71.05, 42.37],
            importance=0.85,
        )

        with pytest.raises(ValueError, match="No geocoder instance"):
            result.get_geometry()


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    def test_geocode_function(self, mock_search_response):
        """Should use default client for geocode."""
        with patch("httpx.Client") as mock_client_class:
            mock_instance = MagicMock()
            mock_instance.get.return_value = make_response(mock_search_response)
            mock_client_class.return_value = mock_instance

            results = geocode("Boston")

            assert len(results) == 2
