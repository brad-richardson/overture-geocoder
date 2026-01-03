#!/usr/bin/env python3
"""
Benchmark geocoders against test cases.

Compares Overture geocoder against Nominatim, Photon, and optionally Pelias.
Uses test cases from the geocoder-tester repo.

Usage:
    python scripts/benchmark_geocoders.py --limit 50 --warmup 2
    python scripts/benchmark_geocoders.py --limit 100 --output results.json
"""

import argparse
import csv
import json
import random
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from math import radians, sin, cos, sqrt, atan2
from pathlib import Path
from typing import Optional
import urllib.parse

import requests

# Test data paths
GEOCODER_TESTER_DIR = Path.home() / "dev" / "geocoder-tester" / "geocoder_tester" / "world"

# Global test cases (cities that should work everywhere)
GLOBAL_CITIES = [
    # Major world cities
    {"query": "London", "expected_name": "London", "expected_lat": 51.5074, "expected_lon": -0.1278, "country": "United Kingdom"},
    {"query": "Paris", "expected_name": "Paris", "expected_lat": 48.8566, "expected_lon": 2.3522, "country": "France"},
    {"query": "Tokyo", "expected_name": "Tokyo", "expected_lat": 35.6762, "expected_lon": 139.6503, "country": "Japan"},
    {"query": "Sydney", "expected_name": "Sydney", "expected_lat": -33.8688, "expected_lon": 151.2093, "country": "Australia"},
    {"query": "Berlin", "expected_name": "Berlin", "expected_lat": 52.5200, "expected_lon": 13.4050, "country": "Germany"},
    {"query": "Rome", "expected_name": "Rome", "expected_lat": 41.9028, "expected_lon": 12.4964, "country": "Italy"},
    {"query": "Madrid", "expected_name": "Madrid", "expected_lat": 40.4168, "expected_lon": -3.7038, "country": "Spain"},
    {"query": "Toronto", "expected_name": "Toronto", "expected_lat": 43.6532, "expected_lon": -79.3832, "country": "Canada"},
    {"query": "Mumbai", "expected_name": "Mumbai", "expected_lat": 19.0760, "expected_lon": 72.8777, "country": "India"},
    {"query": "Cairo", "expected_name": "Cairo", "expected_lat": 30.0444, "expected_lon": 31.2357, "country": "Egypt"},
    {"query": "Moscow", "expected_name": "Moscow", "expected_lat": 55.7558, "expected_lon": 37.6173, "country": "Russia"},
    {"query": "Beijing", "expected_name": "Beijing", "expected_lat": 39.9042, "expected_lon": 116.4074, "country": "China"},
    {"query": "São Paulo", "expected_name": "São Paulo", "expected_lat": -23.5505, "expected_lon": -46.6333, "country": "Brazil"},
    {"query": "Mexico City", "expected_name": "Mexico City", "expected_lat": 19.4326, "expected_lon": -99.1332, "country": "Mexico"},
    {"query": "Singapore", "expected_name": "Singapore", "expected_lat": 1.3521, "expected_lon": 103.8198, "country": "Singapore"},
]


@dataclass
class TestCase:
    query: str
    expected_name: str
    expected_lat: Optional[float] = None
    expected_lon: Optional[float] = None
    tolerance_m: float = 1000.0
    country: Optional[str] = None


@dataclass
class GeocoderResult:
    name: str
    lat: float
    lon: float
    latency_ms: float
    error: Optional[str] = None


@dataclass
class BenchmarkResult:
    geocoder: str
    latencies_ms: list = field(default_factory=list)
    name_matches: list = field(default_factory=list)
    coord_distances_km: list = field(default_factory=list)
    within_tolerance: list = field(default_factory=list)
    errors: int = 0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points in km using Haversine formula."""
    R = 6371.0  # Earth's radius in km

    lat1_rad, lon1_rad = radians(lat1), radians(lon1)
    lat2_rad, lon2_rad = radians(lat2), radians(lon2)

    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad

    a = sin(dlat / 2) ** 2 + cos(lat1_rad) * cos(lat2_rad) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))

    return R * c


def load_usa_cities(csv_path: Path, limit: int = 50) -> list[TestCase]:
    """Load test cases from USA cities CSV."""
    test_cases = []

    with open(csv_path, 'r', encoding='utf-8') as f:
        # Semicolon-separated
        reader = csv.DictReader(f, delimiter=';')
        for row in reader:
            if len(test_cases) >= limit:
                break

            query = row.get('query', '').strip()
            if not query:
                continue

            # Parse expected coordinate: lat,lon,tolerance
            coord_str = row.get('expected_coordinate', '')
            lat, lon, tolerance = None, None, 1000.0
            if coord_str:
                parts = coord_str.split(',')
                if len(parts) >= 2:
                    try:
                        lat = float(parts[0])
                        lon = float(parts[1])
                        if len(parts) >= 3:
                            tolerance = float(parts[2])
                    except ValueError:
                        pass

            test_cases.append(TestCase(
                query=query,
                expected_name=row.get('expected_name', ''),
                expected_lat=lat,
                expected_lon=lon,
                tolerance_m=tolerance,
                country=row.get('country', ''),
            ))

    return test_cases


def load_test_cases(limit: int = 50, include_global: bool = True) -> list[TestCase]:
    """Load test cases from various sources."""
    test_cases = []

    # Add global cities first
    if include_global:
        for city in GLOBAL_CITIES[:15]:
            test_cases.append(TestCase(
                query=city["query"],
                expected_name=city["expected_name"],
                expected_lat=city["expected_lat"],
                expected_lon=city["expected_lon"],
                tolerance_m=5000.0,  # 5km tolerance for major cities
                country=city.get("country"),
            ))

    # Add USA cities
    usa_csv = GEOCODER_TESTER_DIR / "usa" / "test_cities.csv"
    if usa_csv.exists():
        remaining = limit - len(test_cases)
        if remaining > 0:
            usa_cases = load_usa_cities(usa_csv, remaining)
            test_cases.extend(usa_cases)

    return test_cases[:limit]


class Geocoder:
    """Base geocoder class."""

    name: str = "base"
    rate_limit_delay: float = 1.0  # seconds between requests

    def query(self, text: str) -> GeocoderResult:
        raise NotImplementedError

    def _rate_limit(self):
        """Add delay with jitter to respect rate limits."""
        delay = self.rate_limit_delay + random.uniform(0, 0.5)
        time.sleep(delay)


class OvertureGeocoder(Geocoder):
    """Overture geocoder (our implementation)."""

    name = "Overture"
    rate_limit_delay = 0.1  # Our own service, can be faster

    def __init__(self, base_url: str = "https://geocoder.bradr.dev"):
        self.base_url = base_url

    def query(self, text: str) -> GeocoderResult:
        url = f"{self.base_url}/search?q={urllib.parse.quote(text)}"
        start = time.perf_counter()

        try:
            resp = requests.get(url, timeout=10)
            latency_ms = (time.perf_counter() - start) * 1000

            if resp.status_code == 429:
                return GeocoderResult("", 0, 0, latency_ms, error="rate_limited")

            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", [])
            if not results:
                return GeocoderResult("", 0, 0, latency_ms, error="no_results")

            top = results[0]
            return GeocoderResult(
                name=top.get("name", ""),
                lat=top.get("lat", 0),
                lon=top.get("lon", 0),
                latency_ms=latency_ms,
            )
        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000
            return GeocoderResult("", 0, 0, latency_ms, error=str(e))


class NominatimGeocoder(Geocoder):
    """Nominatim geocoder (OpenStreetMap)."""

    name = "Nominatim"
    rate_limit_delay = 1.0  # Strict 1 req/sec policy

    def __init__(self):
        self.base_url = "https://nominatim.openstreetmap.org"

    def query(self, text: str) -> GeocoderResult:
        url = f"{self.base_url}/search"
        params = {
            "q": text,
            "format": "json",
            "limit": 5,
        }
        headers = {
            "User-Agent": "OvertureGeocoderBenchmark/1.0 (github.com/overture-geocoder)",
        }
        start = time.perf_counter()

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=10)
            latency_ms = (time.perf_counter() - start) * 1000

            if resp.status_code == 429:
                return GeocoderResult("", 0, 0, latency_ms, error="rate_limited")

            resp.raise_for_status()
            data = resp.json()

            if not data:
                return GeocoderResult("", 0, 0, latency_ms, error="no_results")

            top = data[0]
            return GeocoderResult(
                name=top.get("name", top.get("display_name", "").split(",")[0]),
                lat=float(top.get("lat", 0)),
                lon=float(top.get("lon", 0)),
                latency_ms=latency_ms,
            )
        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000
            return GeocoderResult("", 0, 0, latency_ms, error=str(e))


class PhotonGeocoder(Geocoder):
    """Photon geocoder (Komoot's OSM-based geocoder)."""

    name = "Photon"
    rate_limit_delay = 1.0  # Be respectful

    def __init__(self):
        self.base_url = "https://photon.komoot.io"

    def query(self, text: str) -> GeocoderResult:
        url = f"{self.base_url}/api/"
        params = {
            "q": text,
            "limit": 5,
        }
        headers = {
            "User-Agent": "OvertureGeocoderBenchmark/1.0 (github.com/overture-geocoder)",
        }
        start = time.perf_counter()

        try:
            resp = requests.get(url, params=params, headers=headers, timeout=10)
            latency_ms = (time.perf_counter() - start) * 1000

            if resp.status_code == 429:
                return GeocoderResult("", 0, 0, latency_ms, error="rate_limited")

            resp.raise_for_status()
            data = resp.json()

            features = data.get("features", [])
            if not features:
                return GeocoderResult("", 0, 0, latency_ms, error="no_results")

            top = features[0]
            props = top.get("properties", {})
            coords = top.get("geometry", {}).get("coordinates", [0, 0])

            return GeocoderResult(
                name=props.get("name", ""),
                lat=coords[1] if len(coords) > 1 else 0,
                lon=coords[0] if len(coords) > 0 else 0,
                latency_ms=latency_ms,
            )
        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000
            return GeocoderResult("", 0, 0, latency_ms, error=str(e))


class PeliasGeocoder(Geocoder):
    """Pelias geocoder (geocode.earth)."""

    name = "Pelias"
    rate_limit_delay = 1.0

    def __init__(self, api_key: str):
        self.base_url = "https://api.geocode.earth"
        self.api_key = api_key

    def query(self, text: str) -> GeocoderResult:
        url = f"{self.base_url}/v1/search"
        params = {
            "text": text,
            "size": 5,
            "api_key": self.api_key,
        }
        start = time.perf_counter()

        try:
            resp = requests.get(url, params=params, timeout=10)
            latency_ms = (time.perf_counter() - start) * 1000

            if resp.status_code == 429:
                return GeocoderResult("", 0, 0, latency_ms, error="rate_limited")

            resp.raise_for_status()
            data = resp.json()

            features = data.get("features", [])
            if not features:
                return GeocoderResult("", 0, 0, latency_ms, error="no_results")

            top = features[0]
            props = top.get("properties", {})
            coords = top.get("geometry", {}).get("coordinates", [0, 0])

            return GeocoderResult(
                name=props.get("name", props.get("label", "")),
                lat=coords[1] if len(coords) > 1 else 0,
                lon=coords[0] if len(coords) > 0 else 0,
                latency_ms=latency_ms,
            )
        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000
            return GeocoderResult("", 0, 0, latency_ms, error=str(e))


def name_matches(expected: str, actual: str) -> bool:
    """Check if the result name matches the expected name (fuzzy)."""
    if not expected or not actual:
        return False
    expected_lower = expected.lower().strip()
    actual_lower = actual.lower().strip()

    # Exact match
    if expected_lower == actual_lower:
        return True

    # One contains the other
    if expected_lower in actual_lower or actual_lower in expected_lower:
        return True

    return False


def run_benchmark(
    geocoders: list[Geocoder],
    test_cases: list[TestCase],
    warmup_runs: int = 2,
    verbose: bool = True,
) -> dict[str, BenchmarkResult]:
    """Run benchmark with warmup cycles, parallelized by query."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = {g.name: BenchmarkResult(geocoder=g.name) for g in geocoders}
    total_runs = warmup_runs + 1  # warmup + actual run

    def query_geocoder(geocoder: Geocoder, query: str) -> tuple[str, GeocoderResult]:
        """Query a single geocoder (for parallel execution)."""
        result = geocoder.query(query)
        return geocoder.name, result

    for run_idx in range(total_runs):
        is_warmup = run_idx < warmup_runs
        run_label = f"Warmup {run_idx + 1}" if is_warmup else "Benchmark"

        if verbose:
            print(f"\n{'=' * 50}")
            print(f"{run_label} run ({len(test_cases)} queries × {len(geocoders)} geocoders)")
            print('=' * 50)

        for i, test_case in enumerate(test_cases):
            if verbose:
                print(f"  [{i + 1}/{len(test_cases)}] {test_case.query[:30]}...", end=" ", flush=True)

            # Query all geocoders in parallel for this query
            query_results = {}
            with ThreadPoolExecutor(max_workers=len(geocoders)) as executor:
                futures = {
                    executor.submit(query_geocoder, g, test_case.query): g.name
                    for g in geocoders
                }
                for future in as_completed(futures):
                    geocoder_name, result = future.result()
                    query_results[geocoder_name] = result

            if verbose:
                latencies = [f"{query_results[g.name].latency_ms:.0f}ms" for g in geocoders]
                print(f"[{', '.join(latencies)}]")

            # Record metrics (only for non-warmup runs)
            if not is_warmup:
                for geocoder in geocoders:
                    result = query_results[geocoder.name]
                    bench = results[geocoder.name]

                    if result.error:
                        bench.errors += 1
                        continue

                    bench.latencies_ms.append(result.latency_ms)

                    # Name matching
                    matched = name_matches(test_case.expected_name, result.name)
                    bench.name_matches.append(matched)

                    # Coordinate distance
                    if test_case.expected_lat and test_case.expected_lon:
                        dist_km = haversine_km(
                            test_case.expected_lat, test_case.expected_lon,
                            result.lat, result.lon
                        )
                        bench.coord_distances_km.append(dist_km)
                        bench.within_tolerance.append(dist_km * 1000 <= test_case.tolerance_m)

            # Rate limit: wait between queries (respects slowest geocoder's limit)
            max_delay = max(g.rate_limit_delay for g in geocoders)
            time.sleep(max_delay + random.uniform(0, 0.3))

    return results


def print_results(results: dict[str, BenchmarkResult], num_queries: int, warmup_runs: int):
    """Print formatted benchmark results."""
    print("\n" + "=" * 70)
    print("Geocoder Benchmark Results")
    print("=" * 70)
    print(f"\nQueries: {num_queries} | Warmup runs: {warmup_runs}")

    # Header
    names = list(results.keys())
    header = "                  " + "  ".join(f"{n:>12}" for n in names)
    print(f"\n{header}")
    print("-" * len(header))

    # Latency stats
    def get_stat(bench: BenchmarkResult, stat_fn, default=0):
        if bench.latencies_ms:
            return stat_fn(bench.latencies_ms)
        return default

    avg_row = "Avg latency (ms)  " + "  ".join(
        f"{get_stat(results[n], statistics.mean):>12.0f}" for n in names
    )
    print(avg_row)

    p50_row = "P50 latency (ms)  " + "  ".join(
        f"{get_stat(results[n], statistics.median):>12.0f}" for n in names
    )
    print(p50_row)

    def p95(vals):
        if not vals:
            return 0
        sorted_vals = sorted(vals)
        idx = int(len(sorted_vals) * 0.95)
        return sorted_vals[min(idx, len(sorted_vals) - 1)]

    p95_row = "P95 latency (ms)  " + "  ".join(
        f"{get_stat(results[n], p95):>12.0f}" for n in names
    )
    print(p95_row)

    print("-" * len(header))

    # Quality stats
    def pct(vals):
        if not vals:
            return 0
        return sum(vals) / len(vals) * 100

    name_match_row = "Top-1 name match  " + "  ".join(
        f"{pct(results[n].name_matches):>11.0f}%" for n in names
    )
    print(name_match_row)

    def avg_dist(bench):
        if bench.coord_distances_km:
            return statistics.mean(bench.coord_distances_km)
        return 0

    dist_row = "Avg coord dist    " + "  ".join(
        f"{avg_dist(results[n]):>10.1f}km" for n in names
    )
    print(dist_row)

    tol_row = "Within tolerance  " + "  ".join(
        f"{pct(results[n].within_tolerance):>11.0f}%" for n in names
    )
    print(tol_row)

    # Errors
    err_row = "Errors            " + "  ".join(
        f"{results[n].errors:>12}" for n in names
    )
    print(err_row)

    print()


def main():
    parser = argparse.ArgumentParser(description="Benchmark geocoders against test cases")
    parser.add_argument("--limit", type=int, default=50,
                        help="Number of test cases to run (default: 50)")
    parser.add_argument("--warmup", type=int, default=2,
                        help="Number of warmup runs (default: 2)")
    parser.add_argument("--pelias-key", type=str,
                        help="Pelias (geocode.earth) API key (optional)")
    parser.add_argument("--output", type=str,
                        help="Output results to JSON file")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress progress output")
    parser.add_argument("--no-global", action="store_true",
                        help="Skip global city test cases")
    args = parser.parse_args()

    # Check for geocoder-tester directory
    if not GEOCODER_TESTER_DIR.exists():
        print(f"Warning: geocoder-tester not found at {GEOCODER_TESTER_DIR}")
        print("Clone it with: git clone https://github.com/geocoders/geocoder-tester ~/dev/geocoder-tester")

    # Load test cases
    print(f"Loading up to {args.limit} test cases...")
    test_cases = load_test_cases(args.limit, include_global=not args.no_global)
    print(f"Loaded {len(test_cases)} test cases")

    if not test_cases:
        print("Error: No test cases loaded")
        sys.exit(1)

    # Initialize geocoders
    geocoders = [
        OvertureGeocoder(),
        NominatimGeocoder(),
        PhotonGeocoder(),
    ]

    if args.pelias_key:
        geocoders.append(PeliasGeocoder(args.pelias_key))

    print(f"\nGeocoders: {', '.join(g.name for g in geocoders)}")
    print(f"Warmup runs: {args.warmup}")

    estimated_time = len(test_cases) * len(geocoders) * (args.warmup + 1) * 1.5
    print(f"Estimated time: ~{estimated_time / 60:.1f} minutes")

    # Run benchmark
    results = run_benchmark(
        geocoders,
        test_cases,
        warmup_runs=args.warmup,
        verbose=not args.quiet,
    )

    # Print results
    print_results(results, len(test_cases), args.warmup)

    # Save to JSON if requested
    if args.output:
        output_data = {
            "metadata": {
                "num_queries": len(test_cases),
                "warmup_runs": args.warmup,
                "geocoders": [g.name for g in geocoders],
            },
            "results": {name: asdict(bench) for name, bench in results.items()},
        }
        with open(args.output, 'w') as f:
            json.dump(output_data, f, indent=2)
        print(f"Results saved to {args.output}")


if __name__ == "__main__":
    main()
