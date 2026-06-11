"""Overture Geocoder - Forward and reverse geocoder using Overture Maps data."""

from .client import (
    OvertureGeocoder,
    GeocoderResult,
    ReverseGeocoderResult,
    HierarchyEntry,
    IdLookupResult,
    BBox,
    GeocoderError,
    GeocoderTimeoutError,
    GeocoderNetworkError,
    geocode,
    reverse_geocode,
)

__version__ = "0.2.0"
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
