"""Overture Geocoder Python Client.

Forward geocoder using Overture Maps data with Nominatim-compatible API.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TypeVar

import httpx

__all__ = [
    "OvertureGeocoder",
    "GeocoderResult",
    "GeocoderError",
    "GeocoderTimeoutError",
    "GeocoderNetworkError",
    "geocode",
    "lookup",
]

# =============================================================================
# Constants
# =============================================================================

DEFAULT_BASE_URL = "https://overture-geocoder.bradr.workers.dev"
DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 0
DEFAULT_RETRY_DELAY = 1.0
DEFAULT_OVERTURE_RELEASE = "2025-12-17.0"

T = TypeVar("T")

# =============================================================================
# Errors
# =============================================================================


class GeocoderError(Exception):
    """Base error for geocoder operations."""

    def __init__(
        self,
        message: str,
        status: Optional[int] = None,
        response: Optional[httpx.Response] = None,
    ):
        super().__init__(message)
        self.status = status
        self.response = response


class GeocoderTimeoutError(GeocoderError):
    """Raised when a request times out."""

    def __init__(self, message: str = "Request timed out"):
        super().__init__(message)


class GeocoderNetworkError(GeocoderError):
    """Raised when a network error occurs."""

    def __init__(self, message: str, cause: Optional[Exception] = None):
        super().__init__(message)
        self.cause = cause


# =============================================================================
# Result Types
# =============================================================================


@dataclass
class AddressDetails:
    """Parsed address components."""

    house_number: Optional[str] = None
    road: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postcode: Optional[str] = None
    country: Optional[str] = None
    country_code: Optional[str] = None


@dataclass
class GeocoderResult:
    """A geocoding result."""

    gers_id: str
    display_name: str
    lat: float
    lon: float
    boundingbox: list[float]
    importance: float
    type: Optional[str] = None
    address: Optional[AddressDetails] = None
    _geocoder: Optional["OvertureGeocoder"] = field(default=None, repr=False)

    def get_geometry(self) -> Optional[dict[str, Any]]:
        """Fetch geometry for this result."""
        if self._geocoder is None:
            raise ValueError("No geocoder instance - use OvertureGeocoder.search()")
        return self._geocoder.get_geometry(self.gers_id)


@dataclass
class GeoJSONFeature:
    """GeoJSON Feature representation."""

    type: str
    id: str
    properties: dict[str, Any]
    geometry: dict[str, Any]
    bbox: Optional[list[float]] = None


@dataclass
class GeoJSONFeatureCollection:
    """GeoJSON FeatureCollection representation."""

    type: str
    features: list[GeoJSONFeature]


# =============================================================================
# Client
# =============================================================================


class OvertureGeocoder:
    """Forward geocoder using Overture Maps address data.

    Args:
        base_url: API base URL (default: 'https://overture-geocoder.bradr.workers.dev')
        timeout: Request timeout in seconds (default: 30.0)
        retries: Number of retry attempts for failed requests (default: 0)
        retry_delay: Delay between retries in seconds (default: 1.0)
        headers: Custom headers to include in all requests
        http_client: Custom httpx.Client instance
        overture_release: Overture Maps release version for geometry fetching

    Example:
        >>> client = OvertureGeocoder(base_url="https://api.example.com")
        >>> results = client.search("123 Main St, Boston, MA")
        >>> for r in results:
        ...     print(f"{r.display_name}: ({r.lat}, {r.lon})")
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        headers: Optional[dict[str, str]] = None,
        http_client: Optional[httpx.Client] = None,
        overture_release: str = DEFAULT_OVERTURE_RELEASE,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self.headers = headers or {}
        self.overture_release = overture_release

        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            timeout=timeout,
            headers={"Accept": "application/json", **self.headers},
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        countrycodes: Optional[str] = None,
        viewbox: Optional[tuple[float, float, float, float]] = None,
        bounded: bool = False,
        addressdetails: bool = False,
        format: str = "jsonv2",
    ) -> list[GeocoderResult]:
        """Search for addresses matching the query.

        Args:
            query: Free-form search string
            limit: Maximum results (1-40, default: 10)
            countrycodes: Comma-separated ISO 3166-1 alpha-2 country codes
            viewbox: Bounding box (lon1, lat1, lon2, lat2)
            bounded: Restrict results to viewbox
            addressdetails: Include address breakdown
            format: Response format ('json', 'jsonv2', 'geojson')

        Returns:
            List of GeocoderResult objects
        """
        params: dict[str, Any] = {
            "q": query,
            "format": format,
            "limit": min(max(1, limit), 40),
        }

        if countrycodes:
            params["countrycodes"] = countrycodes
        if viewbox:
            params["viewbox"] = ",".join(map(str, viewbox))
        if bounded:
            params["bounded"] = "1"
        if addressdetails:
            params["addressdetails"] = "1"

        response = self._request_with_retry(f"{self.base_url}/search", params=params)
        data = response.json()

        if format == "geojson":
            return data  # type: ignore

        return self._parse_results(data, include_geocoder=True)

    def search_geojson(
        self,
        query: str,
        *,
        limit: int = 10,
        countrycodes: Optional[str] = None,
        viewbox: Optional[tuple[float, float, float, float]] = None,
        bounded: bool = False,
        addressdetails: bool = False,
    ) -> dict[str, Any]:
        """Search and return results as GeoJSON FeatureCollection.

        Args:
            query: Free-form search string
            limit: Maximum results (1-40, default: 10)
            countrycodes: Comma-separated ISO country codes
            viewbox: Bounding box (lon1, lat1, lon2, lat2)
            bounded: Restrict results to viewbox
            addressdetails: Include address breakdown

        Returns:
            GeoJSON FeatureCollection dict
        """
        params: dict[str, Any] = {
            "q": query,
            "format": "geojson",
            "limit": min(max(1, limit), 40),
        }

        if countrycodes:
            params["countrycodes"] = countrycodes
        if viewbox:
            params["viewbox"] = ",".join(map(str, viewbox))
        if bounded:
            params["bounded"] = "1"
        if addressdetails:
            params["addressdetails"] = "1"

        response = self._request_with_retry(f"{self.base_url}/search", params=params)
        return response.json()

    def lookup(
        self,
        gers_ids: str | list[str],
        *,
        format: str = "jsonv2",
    ) -> list[GeocoderResult]:
        """Lookup features by their GERS IDs.

        Args:
            gers_ids: Single GERS ID or list of IDs
            format: Response format ('json', 'jsonv2', 'geojson')

        Returns:
            List of GeocoderResult objects
        """
        ids = [gers_ids] if isinstance(gers_ids, str) else gers_ids
        if not ids:
            return []

        params = {
            "gers_ids": ",".join(ids),
            "format": format,
        }

        response = self._request_with_retry(f"{self.base_url}/lookup", params=params)
        data = response.json()

        if format == "geojson":
            return data  # type: ignore

        return self._parse_results(data, include_geocoder=True)

    def lookup_geojson(self, gers_ids: str | list[str]) -> dict[str, Any]:
        """Lookup features and return as GeoJSON FeatureCollection.

        Args:
            gers_ids: Single GERS ID or list of IDs

        Returns:
            GeoJSON FeatureCollection dict
        """
        ids = [gers_ids] if isinstance(gers_ids, str) else gers_ids
        if not ids:
            return {"type": "FeatureCollection", "features": []}

        params = {
            "gers_ids": ",".join(ids),
            "format": "geojson",
        }

        response = self._request_with_retry(f"{self.base_url}/lookup", params=params)
        return response.json()

    def get_geometry(self, gers_id: str) -> Optional[dict[str, Any]]:
        """Fetch full geometry from Overture S3 via the overturemaps-py library.

        Uses the GERS registry for efficient lookup - only downloads the specific
        parquet file containing the requested feature.

        Note: Requires `overturemaps` and `shapely` packages:
            pip install overture-geocoder[geometry]

        Args:
            gers_id: The GERS ID to look up

        Returns:
            GeoJSON Feature dict or None if not found
        """
        try:
            import overturemaps
        except ImportError:
            raise ImportError(
                "overturemaps required for geometry fetching. "
                "Install with: pip install overture-geocoder[geometry]"
            )

        # Use the GERS registry lookup (handles STAC/binary search internally)
        reader = overturemaps.record_batch_reader_from_gers(gers_id)
        if reader is None:
            return None

        table = reader.read_all()
        if len(table) == 0:
            return None

        # Convert to GeoJSON Feature
        import json

        try:
            import shapely
            from shapely import from_wkb, to_geojson
        except ImportError:
            raise ImportError(
                "shapely required for geometry conversion. "
                "Install with: pip install overture-geocoder[geometry]"
            )

        row = table.to_pydict()
        geometry_wkb = row["geometry"][0]

        # Convert WKB to GeoJSON
        geom = from_wkb(geometry_wkb)

        # Build properties from all columns except geometry
        properties = {}
        for k, v in row.items():
            if k != "geometry" and v:
                val = v[0]
                # Handle pyarrow types
                if hasattr(val, "as_py"):
                    val = val.as_py()
                properties[k] = val

        return {
            "type": "Feature",
            "id": gers_id,
            "geometry": json.loads(to_geojson(geom)),
            "properties": properties,
        }

    def get_base_url(self) -> str:
        """Get the base URL configured for this client."""
        return self.base_url

    def get_overture_release(self) -> str:
        """Get the Overture release version configured for this client."""
        return self.overture_release

    def close(self) -> None:
        """Close HTTP client and release resources."""
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> "OvertureGeocoder":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # =========================================================================
    # Private methods
    # =========================================================================

    def _request_with_retry(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        attempt: int = 0,
    ) -> httpx.Response:
        """Make HTTP request with retry logic."""
        try:
            response = self._http.get(url, params=params)

            if not response.is_success:
                # Don't retry client errors (4xx)
                if 400 <= response.status_code < 500:
                    raise GeocoderError(
                        f"Request failed: {response.status_code} {response.reason_phrase}",
                        status=response.status_code,
                        response=response,
                    )

                # Retry server errors (5xx)
                if attempt < self.retries:
                    time.sleep(self.retry_delay)
                    return self._request_with_retry(url, params, attempt + 1)

                raise GeocoderError(
                    f"Request failed after {attempt + 1} attempts: "
                    f"{response.status_code} {response.reason_phrase}",
                    status=response.status_code,
                    response=response,
                )

            return response

        except GeocoderError:
            raise
        except httpx.TimeoutException as e:
            if attempt < self.retries:
                time.sleep(self.retry_delay)
                return self._request_with_retry(url, params, attempt + 1)
            raise GeocoderTimeoutError(
                f"Request timed out after {self.timeout}s ({attempt + 1} attempts)"
            ) from e
        except httpx.RequestError as e:
            if attempt < self.retries:
                time.sleep(self.retry_delay)
                return self._request_with_retry(url, params, attempt + 1)
            raise GeocoderNetworkError(
                f"Network error after {attempt + 1} attempts: {e}", cause=e
            ) from e

    def _parse_results(
        self, data: list[dict[str, Any]], include_geocoder: bool = False
    ) -> list[GeocoderResult]:
        """Parse API response into GeocoderResult objects."""
        if not isinstance(data, list):
            return []

        results = []
        for r in data:
            address = None
            if "address" in r and r["address"]:
                addr = r["address"]
                address = AddressDetails(
                    house_number=addr.get("house_number"),
                    road=addr.get("road"),
                    city=addr.get("city"),
                    state=addr.get("state"),
                    postcode=addr.get("postcode"),
                    country=addr.get("country"),
                    country_code=addr.get("country_code"),
                )

            result = GeocoderResult(
                gers_id=r["gers_id"],
                display_name=r["display_name"],
                lat=float(r["lat"]),
                lon=float(r["lon"]),
                boundingbox=[float(b) for b in r["boundingbox"]],
                importance=r.get("importance", 0),
                type=r.get("type"),
                address=address,
                _geocoder=self if include_geocoder else None,
            )
            results.append(result)

        return results


# =============================================================================
# Convenience functions
# =============================================================================


def geocode(query: str, **kwargs: Any) -> list[GeocoderResult]:
    """Quick geocode function using default settings.

    Args:
        query: Search query string
        **kwargs: Additional arguments passed to search()

    Returns:
        List of GeocoderResult objects
    """
    with OvertureGeocoder() as client:
        return client.search(query, **kwargs)


def lookup(gers_ids: str | list[str], **kwargs: Any) -> list[GeocoderResult]:
    """Quick lookup function using default settings.

    Args:
        gers_ids: Single GERS ID or list of IDs
        **kwargs: Additional arguments passed to lookup()

    Returns:
        List of GeocoderResult objects
    """
    with OvertureGeocoder() as client:
        return client.lookup(gers_ids, **kwargs)
