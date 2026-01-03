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
    "ReverseGeocoderResult",
    "GeocoderError",
    "GeocoderTimeoutError",
    "GeocoderNetworkError",
    "geocode",
    "reverse_geocode",
]

# =============================================================================
# Constants
# =============================================================================

DEFAULT_BASE_URL = "https://geocoder.bradr.workers.dev"
DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 0
DEFAULT_RETRY_DELAY = 1.0

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
class GeocoderResult:
    """A geocoding result."""

    gers_id: str
    primary_name: str
    lat: float
    lon: float
    boundingbox: list[float]
    importance: float
    type: Optional[str] = None
    _geocoder: Optional["OvertureGeocoder"] = field(default=None, repr=False)

    def get_geometry(self) -> Optional[dict[str, Any]]:
        """Fetch geometry for this result."""
        if self._geocoder is None:
            raise ValueError("No geocoder instance - use OvertureGeocoder.search()")
        return self._geocoder.get_geometry(self.gers_id)


@dataclass
class HierarchyEntry:
    """A division in the administrative hierarchy."""

    gers_id: str
    subtype: str
    name: str


@dataclass
class ReverseGeocoderResult:
    """A reverse geocoding result."""

    gers_id: str
    primary_name: str
    subtype: str
    lat: float
    lon: float
    boundingbox: list[float]
    distance_km: float
    confidence: str  # "exact", "bbox", or "approximate"
    hierarchy: Optional[list[HierarchyEntry]] = None
    _geocoder: Optional["OvertureGeocoder"] = field(default=None, repr=False)

    def get_geometry(self) -> Optional[dict[str, Any]]:
        """Fetch geometry for this result."""
        if self._geocoder is None:
            raise ValueError("No geocoder instance - use OvertureGeocoder.reverse()")
        return self._geocoder.get_geometry(self.gers_id)

    def verify_contains_point(self, lat: float, lon: float) -> bool:
        """Fetch polygon from Overture S3 and verify point-in-polygon.

        Uses the client-side geometry fetching to download the division's
        polygon and check if the given point is inside it.

        Args:
            lat: Latitude to check
            lon: Longitude to check

        Returns:
            True if point is inside the polygon, False otherwise
        """
        if self._geocoder is None:
            raise ValueError("No geocoder instance")

        feature = self._geocoder.get_geometry(self.gers_id)
        if not feature:
            return False

        try:
            from shapely.geometry import Point, shape
        except ImportError:
            raise ImportError(
                "shapely required for point-in-polygon check. "
                "Install with: pip install shapely"
            )

        polygon = shape(feature["geometry"])
        point = Point(lon, lat)
        return polygon.contains(point)


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
        base_url: API base URL (default: 'https://geocoder.bradr.workers.dev')
        timeout: Request timeout in seconds (default: 30.0)
        retries: Number of retry attempts for failed requests (default: 0)
        retry_delay: Delay between retries in seconds (default: 1.0)
        headers: Custom headers to include in all requests
        http_client: Custom httpx.Client instance

    Example:
        >>> client = OvertureGeocoder(base_url="https://api.example.com")
        >>> results = client.search("123 Main St, Boston, MA")
        >>> for r in results:
        ...     print(f"{r.primary_name}: ({r.lat}, {r.lon})")
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        headers: Optional[dict[str, str]] = None,
        http_client: Optional[httpx.Client] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self.headers = headers or {}

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
        format: str = "jsonv2",
    ) -> list[GeocoderResult]:
        """Search for divisions matching the query.

        Args:
            query: Free-form search string
            limit: Maximum results (1-40, default: 10)
            format: Response format ('json', 'jsonv2', 'geojson')

        Returns:
            List of GeocoderResult objects
        """
        params: dict[str, Any] = {
            "q": query,
            "format": format,
            "limit": min(max(1, limit), 40),
        }

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
    ) -> dict[str, Any]:
        """Search and return results as GeoJSON FeatureCollection.

        Args:
            query: Free-form search string
            limit: Maximum results (1-40, default: 10)

        Returns:
            GeoJSON FeatureCollection dict
        """
        params: dict[str, Any] = {
            "q": query,
            "format": "geojson",
            "limit": min(max(1, limit), 40),
        }

        response = self._request_with_retry(f"{self.base_url}/search", params=params)
        return response.json()

    def reverse(
        self,
        lat: float,
        lon: float,
        *,
        format: str = "jsonv2",
    ) -> list[ReverseGeocoderResult]:
        """Reverse geocode coordinates to divisions.

        Returns divisions (localities, neighborhoods, counties, etc.) that
        contain the given coordinate. Results are sorted by specificity
        (smallest/most specific first).

        Args:
            lat: Latitude (-90 to 90)
            lon: Longitude (-180 to 180)
            format: Response format ('jsonv2', 'geojson')

        Returns:
            List of ReverseGeocoderResult objects, most specific first
        """
        params: dict[str, Any] = {
            "lat": lat,
            "lon": lon,
            "format": format,
        }

        response = self._request_with_retry(f"{self.base_url}/reverse", params=params)
        data = response.json()

        if format == "geojson":
            return data  # type: ignore

        return self._parse_reverse_results(data)

    def reverse_geojson(
        self,
        lat: float,
        lon: float,
    ) -> dict[str, Any]:
        """Reverse geocode and return results as GeoJSON FeatureCollection.

        Args:
            lat: Latitude (-90 to 90)
            lon: Longitude (-180 to 180)

        Returns:
            GeoJSON FeatureCollection dict
        """
        params: dict[str, Any] = {
            "lat": lat,
            "lon": lon,
            "format": "geojson",
        }

        response = self._request_with_retry(f"{self.base_url}/reverse", params=params)
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
            result = GeocoderResult(
                gers_id=r["gers_id"],
                primary_name=r["primary_name"],
                lat=r["lat"],  # Server now returns numbers
                lon=r["lon"],  # Server now returns numbers
                boundingbox=r["boundingbox"],  # Server now returns numbers
                importance=r.get("importance", 0),
                type=r.get("type"),
                _geocoder=self if include_geocoder else None,
            )
            results.append(result)

        return results

    def _parse_reverse_results(
        self, data: list[dict[str, Any]]
    ) -> list[ReverseGeocoderResult]:
        """Parse reverse geocoding API response into ReverseGeocoderResult objects."""
        if not isinstance(data, list):
            return []

        results = []
        for r in data:
            hierarchy = None
            if "hierarchy" in r and r["hierarchy"]:
                hierarchy = [
                    HierarchyEntry(
                        gers_id=h.get("gers_id", ""),
                        subtype=h.get("subtype", ""),
                        name=h.get("name", ""),
                    )
                    for h in r["hierarchy"]
                ]

            result = ReverseGeocoderResult(
                gers_id=r["gers_id"],
                primary_name=r["primary_name"],
                subtype=r["subtype"],
                lat=float(r["lat"]),
                lon=float(r["lon"]),
                boundingbox=[float(b) for b in r["boundingbox"]],
                distance_km=float(r["distance_km"]),
                confidence=r["confidence"],
                hierarchy=hierarchy,
                _geocoder=self,
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


def reverse_geocode(lat: float, lon: float, **kwargs: Any) -> list[ReverseGeocoderResult]:
    """Quick reverse geocode function using default settings.

    Args:
        lat: Latitude (-90 to 90)
        lon: Longitude (-180 to 180)
        **kwargs: Additional arguments passed to reverse()

    Returns:
        List of ReverseGeocoderResult objects
    """
    with OvertureGeocoder() as client:
        return client.reverse(lat, lon, **kwargs)
