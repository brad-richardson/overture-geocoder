#!/usr/bin/env python3
"""Advanced usage examples for the Overture Geocoder client.

Demonstrates:
- Custom HTTP client configuration
- Retry configuration
- Error handling
- Custom headers and authentication
- Factory patterns

Run with: python examples/advanced_usage.py
"""

import os
import time
from typing import Optional

import httpx

from overture_geocoder import (
    OvertureGeocoder,
    GeocoderError,
    GeocoderTimeoutError,
    GeocoderNetworkError,
)


# =============================================================================
# Custom HTTP client with logging
# =============================================================================


def create_logging_client(timeout: float = 30.0) -> httpx.Client:
    """Create an httpx client with request/response logging."""

    def log_request(request: httpx.Request):
        print(f"[REQUEST] {request.method} {request.url}")

    def log_response(response: httpx.Response):
        request = response.request
        print(
            f"[RESPONSE] {request.method} {request.url} "
            f"-> {response.status_code} ({response.elapsed.total_seconds():.3f}s)"
        )

    return httpx.Client(
        timeout=timeout,
        event_hooks={
            "request": [log_request],
            "response": [log_response],
        },
    )


# =============================================================================
# Retry configuration example
# =============================================================================


def retry_example():
    """Demonstrate retry configuration."""
    print("\n=== Retry Configuration Example ===")

    client = OvertureGeocoder(
        base_url="http://localhost:8787",
        retries=3,  # Retry up to 3 times
        retry_delay=1.0,  # Wait 1 second between retries
        timeout=5.0,  # 5 second timeout per request
    )

    try:
        with client:
            results = client.search("test query")
            print(f"Success! Found {len(results)} results")
    except GeocoderTimeoutError:
        print("All retry attempts timed out")
    except GeocoderNetworkError as e:
        print(f"Network error after retries: {e}")
    except GeocoderError as e:
        print(f"API error ({e.status}): {e}")


# =============================================================================
# Error handling example
# =============================================================================


def error_handling_example():
    """Demonstrate comprehensive error handling."""
    print("\n=== Error Handling Example ===")

    with OvertureGeocoder(timeout=5.0, retries=1) as client:
        try:
            results = client.search("123 Main St")
            print(f"Found {len(results)} results")
        except GeocoderTimeoutError:
            print("Request timed out. The server may be slow or unavailable.")
            print("Try increasing the timeout or checking your connection.")
        except GeocoderNetworkError as e:
            print(f"Network error: {e}")
            if e.cause:
                print(f"Underlying cause: {e.cause}")
        except GeocoderError as e:
            print(f"API error: {e.status} - {e}")

            # Handle specific status codes
            if e.status == 400:
                print("Bad request - check your query parameters")
            elif e.status == 404:
                print("Resource not found")
            elif e.status == 429:
                print("Rate limited - slow down your requests")
            elif e.status == 500:
                print("Server error - try again later")


# =============================================================================
# Custom headers for authentication
# =============================================================================


def authentication_example():
    """Demonstrate custom headers for API authentication."""
    print("\n=== Custom Headers Example ===")

    api_token = os.environ.get("API_TOKEN", "your-api-key")

    with OvertureGeocoder(
        base_url="http://localhost:8787",
        headers={
            "Authorization": f"Bearer {api_token}",
            "X-Client-ID": "my-app",
            "X-Request-Source": "cli-example",
        },
    ) as client:
        try:
            results = client.search("Boston")
            print(f"Found {len(results)} results")
        except GeocoderError as e:
            print(f"Error: {e}")


# =============================================================================
# Custom HTTP client injection
# =============================================================================


def custom_client_example():
    """Demonstrate using a custom httpx client."""
    print("\n=== Custom HTTP Client Example ===")

    # Create a custom client with logging
    http_client = create_logging_client(timeout=10.0)

    try:
        with OvertureGeocoder(http_client=http_client) as client:
            results = client.search("Boston City Hall", limit=3)
            print(f"Found {len(results)} results")
            for r in results:
                print(f"  - {r.display_name}")
    finally:
        http_client.close()


# =============================================================================
# Factory functions for different environments
# =============================================================================


def create_production_client() -> OvertureGeocoder:
    """Create a client configured for production use."""
    return OvertureGeocoder(
        base_url=os.environ.get("GEOCODER_API_URL", "https://geocoder.example.com"),
        timeout=10.0,
        retries=2,
        retry_delay=0.5,
        headers={
            "Authorization": f"Bearer {os.environ.get('GEOCODER_API_KEY', '')}",
        },
    )


def create_development_client() -> OvertureGeocoder:
    """Create a client configured for development use."""
    return OvertureGeocoder(
        base_url="http://localhost:8787",
        timeout=30.0,  # Longer timeout for local dev
        retries=0,  # No retries in dev for faster feedback
        http_client=create_logging_client(),  # Log all requests
    )


def factory_example():
    """Demonstrate using factory functions."""
    print("\n=== Factory Example ===")

    env = os.environ.get("ENVIRONMENT", "development")

    if env == "production":
        client = create_production_client()
    else:
        client = create_development_client()

    print(f"Using client with base URL: {client.get_base_url()}")

    try:
        with client:
            results = client.search("Boston City Hall", limit=3)
            print(f"Found {len(results)} results")
            for r in results:
                print(f"  - {r.display_name}")
    except GeocoderError as e:
        print(f"Search failed: {e}")


# =============================================================================
# Batch geocoding with rate limiting
# =============================================================================


def batch_geocoding_example():
    """Demonstrate batch geocoding with rate limiting."""
    print("\n=== Batch Geocoding Example ===")

    addresses = [
        "123 Main St, Boston, MA",
        "456 Oak Ave, Cambridge, MA",
        "789 Elm St, Somerville, MA",
    ]

    results_map = {}
    rate_limit_delay = 0.1  # 100ms between requests

    with OvertureGeocoder(timeout=10.0, retries=2) as client:
        for i, address in enumerate(addresses):
            try:
                results = client.search(address, limit=1)
                if results:
                    results_map[address] = results[0]
                    print(f"[{i+1}/{len(addresses)}] {address}")
                    print(f"  -> {results[0].lat}, {results[0].lon}")
                else:
                    print(f"[{i+1}/{len(addresses)}] {address} - No results")

                # Rate limiting
                if i < len(addresses) - 1:
                    time.sleep(rate_limit_delay)

            except GeocoderError as e:
                print(f"[{i+1}/{len(addresses)}] {address} - Error: {e}")

    print(f"\nSuccessfully geocoded {len(results_map)}/{len(addresses)} addresses")


# =============================================================================
# Main
# =============================================================================


def main():
    print("=== Advanced Usage Examples ===")

    # Run examples
    custom_client_example()
    error_handling_example()
    # retry_example()  # Uncomment to test retry behavior
    # authentication_example()  # Uncomment to test with auth headers
    # factory_example()
    # batch_geocoding_example()


if __name__ == "__main__":
    main()
