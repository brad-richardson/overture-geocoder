"""Tests for the Overture Geocoder Python client."""

import json
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from overture_geocoder import (
    OvertureGeocoder,
    GeocoderResult,
    GeocoderError,
    GeocoderTimeoutError,
    GeocoderNetworkError,
    geocode,
)


class TestOvertureGeocoderInit:
    """Tests for OvertureGeocoder initialization."""

    def test_default_configuration(self):
        """Should use default configuration values."""
        client = OvertureGeocoder()
        assert client.get_base_url() == "https://geocoder.bradr.workers.dev"
        assert client.timeout == 30.0
        assert client.retries == 0
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

    def test_search_with_query_only(self, mock_search_results):
        """Should search with query only."""
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = mock_search_results

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response

        client = OvertureGeocoder(http_client=mock_client)
        results = client.search("123 Main St")

        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        assert "/search" in call_args[0][0]
        assert call_args[1]["params"]["q"] == "123 Main St"
        assert call_args[1]["params"]["format"] == "jsonv2"
        assert call_args[1]["params"]["limit"] == 10

        assert len(results) == 2
        assert results[0].gers_id == "abc-123"
        assert results[0].lat == 42.3601
        assert results[0].lon == -71.0589

    def test_search_with_limit_option(self, mock_search_results):
        """Should search with limit option."""
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = mock_search_results

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response

        client = OvertureGeocoder(http_client=mock_client)
        results = client.search("Boston", limit=5)

        call_args = mock_client.get.call_args
        params = call_args[1]["params"]
        assert params["limit"] == 5

        assert results[0].type == "locality"

    def test_search_clamps_limit(self):
        """Should clamp limit to 1-40 range."""
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = []

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response

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

    def test_search_with_custom_headers(self):
        """Should include custom headers."""
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = []

        with patch("httpx.Client") as mock_client_class:
            mock_instance = MagicMock()
            mock_instance.get.return_value = mock_response
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
    """Tests for searchGeoJSON functionality."""

    def test_returns_geojson_feature_collection(self, mock_geojson_response):
        """Should return GeoJSON FeatureCollection."""
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = mock_geojson_response

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response

        client = OvertureGeocoder(http_client=mock_client)
        result = client.search_geojson("123 Main St")

        call_args = mock_client.get.call_args
        assert call_args[1]["params"]["format"] == "geojson"

        assert result["type"] == "FeatureCollection"
        assert len(result["features"]) == 1
        assert result["features"][0]["geometry"]["type"] == "Point"


class TestErrorHandling:
    """Tests for error handling."""

    def test_raises_geocoder_error_on_4xx(self):
        """Should raise GeocoderError on 4xx response."""
        mock_response = MagicMock()
        mock_response.is_success = False
        mock_response.status_code = 400
        mock_response.reason_phrase = "Bad Request"

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response

        client = OvertureGeocoder(http_client=mock_client)

        with pytest.raises(GeocoderError) as exc_info:
            client.search("test")

        assert exc_info.value.status == 400

    def test_raises_geocoder_error_on_5xx(self):
        """Should raise GeocoderError on 5xx response without retries."""
        mock_response = MagicMock()
        mock_response.is_success = False
        mock_response.status_code = 500
        mock_response.reason_phrase = "Internal Server Error"

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response

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

    def test_retries_on_5xx_errors(self, mock_search_results):
        """Should retry on 5xx errors."""
        call_count = 0

        def mock_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                mock_response = MagicMock()
                mock_response.is_success = False
                mock_response.status_code = 500
                mock_response.reason_phrase = "Internal Server Error"
                return mock_response
            else:
                mock_response = MagicMock()
                mock_response.is_success = True
                mock_response.json.return_value = mock_search_results
                return mock_response

        mock_client = MagicMock()
        mock_client.get.side_effect = mock_get

        client = OvertureGeocoder(
            http_client=mock_client, retries=3, retry_delay=0.01
        )
        results = client.search("test")

        assert call_count == 3
        assert len(results) == 2

    def test_does_not_retry_on_4xx_errors(self):
        """Should not retry on 4xx errors."""
        mock_response = MagicMock()
        mock_response.is_success = False
        mock_response.status_code = 404
        mock_response.reason_phrase = "Not Found"

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response

        client = OvertureGeocoder(
            http_client=mock_client, retries=3, retry_delay=0.01
        )

        with pytest.raises(GeocoderError):
            client.search("test")

        assert mock_client.get.call_count == 1

    def test_retries_on_network_errors(self, mock_search_results):
        """Should retry on network errors."""
        call_count = 0

        def mock_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise httpx.RequestError("Network error")
            else:
                mock_response = MagicMock()
                mock_response.is_success = True
                mock_response.json.return_value = mock_search_results
                return mock_response

        mock_client = MagicMock()
        mock_client.get.side_effect = mock_get

        client = OvertureGeocoder(
            http_client=mock_client, retries=2, retry_delay=0.01
        )
        results = client.search("test")

        assert call_count == 2
        assert len(results) == 2

    def test_retries_on_timeout(self, mock_search_results):
        """Should retry on timeout."""
        call_count = 0

        def mock_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise httpx.TimeoutException("Timeout")
            else:
                mock_response = MagicMock()
                mock_response.is_success = True
                mock_response.json.return_value = mock_search_results
                return mock_response

        mock_client = MagicMock()
        mock_client.get.side_effect = mock_get

        client = OvertureGeocoder(
            http_client=mock_client, retries=2, retry_delay=0.01
        )
        results = client.search("test")

        assert call_count == 2
        assert len(results) == 2


class TestGeocoderResult:
    """Tests for GeocoderResult dataclass."""

    def test_result_fields(self):
        """Should have correct fields."""
        result = GeocoderResult(
            gers_id="abc-123",
            primary_name="123 Main St",
            lat=42.36,
            lon=-71.06,
            boundingbox=[42.35, 42.37, -71.07, -71.05],
            importance=0.85,
            type="address",
        )

        assert result.gers_id == "abc-123"
        assert result.primary_name == "123 Main St"
        assert result.lat == 42.36
        assert result.lon == -71.06
        assert result.importance == 0.85

    def test_get_geometry_without_geocoder(self):
        """Should raise error when getting geometry without geocoder."""
        result = GeocoderResult(
            gers_id="abc-123",
            primary_name="123 Main St",
            lat=42.36,
            lon=-71.06,
            boundingbox=[42.35, 42.37, -71.07, -71.05],
            importance=0.85,
        )

        with pytest.raises(ValueError, match="No geocoder instance"):
            result.get_geometry()


class TestConvenienceFunctions:
    """Tests for convenience functions."""

    def test_geocode_function(self, mock_search_results):
        """Should use default client for geocode."""
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = mock_search_results

        with patch("httpx.Client") as mock_client_class:
            mock_instance = MagicMock()
            mock_instance.get.return_value = mock_response
            mock_instance.__enter__ = MagicMock(return_value=mock_instance)
            mock_instance.__exit__ = MagicMock(return_value=False)
            mock_client_class.return_value = mock_instance

            results = geocode("123 Main St")

            assert len(results) == 2
