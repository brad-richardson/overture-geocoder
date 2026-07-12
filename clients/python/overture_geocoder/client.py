"""Overture Geocoder Python Client.

Forward and reverse geocoder using Overture Maps data.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional, TypeVar

import httpx

__all__ = [
    "OvertureGeocoder",
    "GeocoderResult",
    "ReverseGeocoderResult",
    "HierarchyEntry",
    "IdLookupResult",
    "BBox",
    "GeocoderError",
    "GeocoderTimeoutError",
    "GeocoderNetworkError",
    "geocode",
    "reverse_geocode",
]

# =============================================================================
# Constants
# =============================================================================

DEFAULT_BASE_URL = "https://geocoder.bradr.dev"
DEFAULT_TIMEOUT = 30.0
DEFAULT_RETRIES = 2
DEFAULT_RETRY_DELAY = 1.0
MAX_RETRY_AFTER_SECONDS = 30.0

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
    """A forward geocoding result.

    Attributes:
        gers_id: GERS ID of the matched division
        primary_name: Display name (the worker's ``name`` field)
        lat: Latitude of the representative point
        lon: Longitude of the representative point
        boundingbox: ``[min_lon, min_lat, max_lon, max_lat]`` (the worker's
            ``bbox`` field)
        importance: Relative importance score
        type: Division subtype (e.g. ``"locality"``)
        country: ISO country code (e.g. ``"US"``), when available
        region: ISO region code (e.g. ``"US-MA"``), when available
    """

    gers_id: str
    primary_name: str
    lat: float
    lon: float
    boundingbox: list[float]
    importance: float
    type: Optional[str] = None
    country: Optional[str] = None
    region: Optional[str] = None
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
    """A reverse geocoding result.

    Attributes:
        gers_id: GERS ID of the matched division
        primary_name: Display name of the division
        subtype: Division subtype (e.g. ``"county"``, ``"locality"``)
        lat: Latitude of the division's representative point
        lon: Longitude of the division's representative point
        boundingbox: ``[min_lon, min_lat, max_lon, max_lat]``
        distance_km: Distance from the query point in kilometers
        confidence: One of ``"high"``, ``"medium"``, or ``"low"``
        hierarchy: Administrative hierarchy from most to least specific
    """

    gers_id: str
    primary_name: str
    subtype: str
    lat: float
    lon: float
    boundingbox: list[float]
    distance_km: float
    confidence: str  # "high", "medium", or "low"
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
class BBox:
    """A bounding box in lon/lat coordinates."""

    xmin: float
    ymin: float
    xmax: float
    ymax: float


@dataclass
class IdLookupResult:
    """Result of a GERS ID lookup."""

    id: str
    bbox: BBox
    feature_type: Optional[str] = None
    theme: Optional[str] = None
    filename: Optional[str] = None
    last_seen_release: Optional[str] = None
    registry_member: Optional[bool] = None
    exists_in_current_release: Optional[bool] = None
    overture_path: Optional[str] = None


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
    """Forward and reverse geocoder using Overture Maps division data.

    Args:
        base_url: API base URL (default: 'https://geocoder.bradr.dev')
        timeout: Request timeout in seconds (default: 30.0)
        retries: Number of retry attempts for failed requests (default: 2)
        retry_delay: Base delay between retries in seconds (default: 1.0).
            Retries use exponential backoff with jitter.
        headers: Custom headers to include in all requests
        http_client: Custom httpx.Client instance

    Example:
        >>> client = OvertureGeocoder()
        >>> results = client.search("Boston, MA")
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
        autocomplete: Optional[bool] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
    ) -> list[GeocoderResult]:
        """Search for divisions matching the query.

        Args:
            query: Free-form search string
            limit: Maximum results (1-40, default: 10)
            autocomplete: Enable prefix matching for partial queries
            lat: Optional latitude to bias result ranking
            lon: Optional longitude to bias result ranking

        Returns:
            List of GeocoderResult objects
        """
        params: dict[str, Any] = {
            "q": query,
            "limit": min(max(1, limit), 40),
        }
        if autocomplete is not None:
            params["autocomplete"] = 1 if autocomplete else 0
        if lat is not None:
            params["lat"] = lat
        if lon is not None:
            params["lon"] = lon

        response = self._request_with_retry(f"{self.base_url}/search", params=params)
        data = response.json()

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
            GeoJSON FeatureCollection dict with Point features
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
    ) -> list[ReverseGeocoderResult]:
        """Reverse geocode coordinates to the containing division.

        The worker returns a single best match (with its administrative
        hierarchy); for API stability this is returned as a list with zero
        or one element.

        Args:
            lat: Latitude (-90 to 90)
            lon: Longitude (-180 to 180)

        Returns:
            List with one ReverseGeocoderResult, or an empty list if no
            division contains the point (HTTP 404 from the worker).
        """
        params: dict[str, Any] = {
            "lat": lat,
            "lon": lon,
        }

        try:
            response = self._request_with_retry(
                f"{self.base_url}/reverse", params=params
            )
        except GeocoderError as e:
            if e.status == 404:
                return []
            raise
        data = response.json()

        return self._parse_reverse_results(data)

    def reverse_geojson(
        self,
        lat: float,
        lon: float,
    ) -> dict[str, Any]:
        """Reverse geocode and return the raw GeoJSON response.

        Note: ``format=geojson`` on ``/reverse`` requires a current worker
        version; older deployments ignore the parameter and return the plain
        JSON object instead of GeoJSON. The response is returned as-is.

        Args:
            lat: Latitude (-90 to 90)
            lon: Longitude (-180 to 180)

        Returns:
            The JSON response dict (GeoJSON on current worker versions)
        """
        params: dict[str, Any] = {
            "lat": lat,
            "lon": lon,
            "format": "geojson",
        }

        response = self._request_with_retry(f"{self.base_url}/reverse", params=params)
        return response.json()

    def lookup_id(self, gers_id: str) -> Optional[IdLookupResult]:
        """Look up a GERS ID in the ID index.

        Args:
            gers_id: The GERS ID (UUID) to look up

        Returns:
            IdLookupResult with the ID and its bounding box, or None if the
            ID is unknown (HTTP 404).

        Raises:
            GeocoderError: With ``status=503`` if the ID index is unavailable,
                or for other HTTP errors.
        """
        try:
            response = self._request_with_retry(f"{self.base_url}/id/{gers_id}")
        except GeocoderError as e:
            if e.status == 404:
                return None
            raise

        data = response.json()
        bbox = data["bbox"]
        return IdLookupResult(
            id=data["id"],
            bbox=BBox(
                xmin=float(bbox["xmin"]),
                ymin=float(bbox["ymin"]),
                xmax=float(bbox["xmax"]),
                ymax=float(bbox["ymax"]),
            ),
            feature_type=data.get("feature_type"),
            theme=data.get("theme"),
            filename=data.get("filename"),
            last_seen_release=data.get("last_seen_release"),
            registry_member=data.get("registry_member"),
            exists_in_current_release=data.get("exists_in_current_release"),
            overture_path=data.get("overture_path"),
        )

    def health(self) -> dict[str, Any]:
        """Check service health.

        Returns the health JSON body, e.g. ``{"status": "ok", "version": ...}``.
        The worker responds with HTTP 503 and ``{"status": "error", ...}`` when
        unhealthy; that body is returned rather than raising, so callers can
        always inspect ``status``.

        Returns:
            Health status dict
        """
        try:
            response = self._http.get(f"{self.base_url}/health")
        except httpx.TimeoutException as e:
            raise GeocoderTimeoutError(
                f"Health check timed out after {self.timeout}s"
            ) from e
        except httpx.RequestError as e:
            raise GeocoderNetworkError(f"Network error: {e}", cause=e) from e

        try:
            return response.json()
        except ValueError as e:
            raise GeocoderError(
                f"Health check returned non-JSON response: "
                f"{response.status_code} {response.reason_phrase}",
                status=response.status_code,
                response=response,
            ) from e

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

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff delay with jitter for the given attempt."""
        delay = self.retry_delay * (2**attempt)
        return delay + random.uniform(0, delay * 0.25)

    def _retry_after_delay(self, response: httpx.Response, attempt: int) -> float:
        """Delay before retrying a 429, honoring Retry-After (capped)."""
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return min(float(retry_after), MAX_RETRY_AFTER_SECONDS)
            except ValueError:
                pass
        return self._backoff_delay(attempt)

    def _request_with_retry(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        attempt: int = 0,
    ) -> httpx.Response:
        """Make HTTP request with retry logic.

        - 429 responses are retried, honoring the Retry-After header
          (capped at 30 seconds).
        - 5xx responses, timeouts, and network errors are retried with
          exponential backoff plus jitter (retry_delay * 2^attempt).
        - Other 4xx responses are not retried.
        """
        try:
            response = self._http.get(url, params=params)

            if not response.is_success:
                # Rate limited: retry, honoring Retry-After
                if response.status_code == 429:
                    if attempt < self.retries:
                        time.sleep(self._retry_after_delay(response, attempt))
                        return self._request_with_retry(url, params, attempt + 1)
                    raise GeocoderError(
                        f"Rate limited after {attempt + 1} attempts: "
                        f"429 {response.reason_phrase}",
                        status=429,
                        response=response,
                    )

                # Don't retry other client errors (4xx)
                if 400 <= response.status_code < 500:
                    raise GeocoderError(
                        f"Request failed: {response.status_code} {response.reason_phrase}",
                        status=response.status_code,
                        response=response,
                    )

                # Retry server errors (5xx)
                if attempt < self.retries:
                    time.sleep(self._backoff_delay(attempt))
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
                time.sleep(self._backoff_delay(attempt))
                return self._request_with_retry(url, params, attempt + 1)
            raise GeocoderTimeoutError(
                f"Request timed out after {self.timeout}s ({attempt + 1} attempts)"
            ) from e
        except httpx.RequestError as e:
            if attempt < self.retries:
                time.sleep(self._backoff_delay(attempt))
                return self._request_with_retry(url, params, attempt + 1)
            raise GeocoderNetworkError(
                f"Network error after {attempt + 1} attempts: {e}", cause=e
            ) from e

    def _parse_results(
        self, data: Any, include_geocoder: bool = False
    ) -> list[GeocoderResult]:
        """Parse /search API response into GeocoderResult objects.

        The canonical worker response is ``{"results": [...]}``; a bare list
        is tolerated defensively. Each entry has ``name``, ``bbox``, ``type``,
        ``lat``, ``lon``, ``importance`` and optional ``country``/``region``.
        """
        if isinstance(data, dict):
            entries = data.get("results", [])
        elif isinstance(data, list):
            entries = data
        else:
            return []

        if not isinstance(entries, list):
            return []

        results = []
        for r in entries:
            result = GeocoderResult(
                gers_id=r["gers_id"],
                primary_name=r["name"],
                lat=float(r["lat"]),
                lon=float(r["lon"]),
                boundingbox=[float(b) for b in r["bbox"]],
                importance=float(r.get("importance", 0)),
                type=r.get("type"),
                country=r.get("country"),
                region=r.get("region"),
                _geocoder=self if include_geocoder else None,
            )
            results.append(result)

        return results

    def _parse_reverse_results(self, data: Any) -> list[ReverseGeocoderResult]:
        """Parse /reverse API response into ReverseGeocoderResult objects.

        The worker returns a single JSON object; it is wrapped in a list for
        API stability. A list is tolerated defensively.
        """
        if isinstance(data, dict):
            entries = [data]
        elif isinstance(data, list):
            entries = data
        else:
            return []

        results = []
        for r in entries:
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
        List of ReverseGeocoderResult objects (empty if no match)
    """
    with OvertureGeocoder() as client:
        return client.reverse(lat, lon, **kwargs)
