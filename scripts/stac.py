#!/usr/bin/env python3
"""
STAC client for Overture Maps release discovery.

Usage:
    python scripts/stac.py              # Get latest release
    python scripts/stac.py --releases   # List all releases
"""

import argparse
import json
import sys
import time
from urllib.request import urlopen

STAC_ROOT = "https://stac.overturemaps.org/catalog.json"

FETCH_TIMEOUT_SECONDS = 30
FETCH_RETRIES = 3


def get_catalog(url: str) -> dict:
    """Fetch and parse a STAC catalog (with timeout and transient-error retry)."""
    last_exc = None
    for attempt in range(FETCH_RETRIES):
        try:
            with urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except Exception as exc:
            last_exc = exc
            if attempt < FETCH_RETRIES - 1:
                wait = 5 * (2 ** attempt)
                print(
                    f"STAC fetch failed (attempt {attempt + 1}/{FETCH_RETRIES}): "
                    f"{exc}; retrying in {wait}s...",
                    file=sys.stderr,
                )
                time.sleep(wait)
    raise RuntimeError(f"Failed to fetch STAC catalog {url}: {last_exc}") from last_exc


def get_latest_release() -> str:
    """Get the latest Overture release version."""
    catalog = get_catalog(STAC_ROOT)

    for link in catalog.get("links", []):
        if link.get("latest") is True:
            # Extract version from href like "./2025-12-17.0/catalog.json"
            href = link.get("href", "")
            version = href.split("/")[1] if "/" in href else None
            if version:
                return version

    raise ValueError("Could not find latest release in STAC catalog")


def list_releases() -> list[str]:
    """List all available Overture releases."""
    catalog = get_catalog(STAC_ROOT)
    releases = []

    for link in catalog.get("links", []):
        if link.get("rel") == "child":
            href = link.get("href", "")
            # Extract version from href like "./2025-12-17.0/catalog.json"
            if "/" in href:
                version = href.split("/")[1]
                if version and version[0].isdigit():
                    releases.append(version)

    return sorted(releases, reverse=True)


def get_s3_path(theme: str, type_name: str, release: str = None) -> str:
    """Get S3 path for a specific theme/type."""
    if release is None:
        release = get_latest_release()

    return f"s3://overturemaps-us-west-2/release/{release}/theme={theme}/type={type_name}/*"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Overture Maps STAC client")
    parser.add_argument("--releases", action="store_true", help="List all releases")
    parser.add_argument("--path", nargs=2, metavar=("THEME", "TYPE"),
                       help="Get S3 path for theme/type")
    args = parser.parse_args()

    if args.releases:
        releases = list_releases()
        print("Available Overture releases:")
        for i, r in enumerate(releases):
            latest = " (latest)" if i == 0 else ""
            print(f"  {r}{latest}")
    elif args.path:
        theme, type_name = args.path
        print(get_s3_path(theme, type_name))
    else:
        release = get_latest_release()
        print(f"Latest Overture release: {release}")
