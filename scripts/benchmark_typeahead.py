#!/usr/bin/env python3
"""
Type-ahead (autocomplete) benchmark: Overture geocoder vs Nominatim vs Photon.

For each target place, types progressively longer prefixes of the query and
records the rank of the target at each prefix length. The headline metric is
"keystrokes to rank 1": the shortest prefix at which the target is the top
result (and stays interpretable alongside keystrokes-to-top-3).

Photon is the natural comparator (it is built for type-ahead: edge n-grams +
QueryReranker). Nominatim is included for reference but is not an
autocomplete engine — expect it to only resolve near-complete names.

Also carries the ranking-regression cases from docs/ranking-research.md and
the 2026-06/07 ranking work (porter-stemmer removal, partial-match prefix
credit, alt-name rung primary-name exclusion, comma-qualified queries).

Caveats: run from a fixed location (CF headers give Overture a location
bias; Photon/Nominatim get no bias parameter), against public rate-limited
instances — latency numbers are indicative, rank quality is the signal.

Usage:
    python scripts/benchmark_typeahead.py                     # console table
    python scripts/benchmark_typeahead.py --output benchmarks/typeahead.json
    # Third-language exonym set (skip Nominatim: rate-limited, not autocomplete):
    python scripts/benchmark_typeahead.py --cases multilingual --skip nominatim \
        --output benchmarks/multilingual-typeahead.json
"""

import argparse
import json
import statistics
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field, asdict
from math import radians, sin, cos, sqrt, atan2
from typing import Optional

import requests

USER_AGENT = "OvertureGeocoderBenchmark/1.0 (github.com/overture-geocoder)"

# How deep in the result list we look for the target
TOP_K = 5
# Prefixes shorter than this are not typed (few UIs query at 1-2 chars)
MIN_PREFIX = 3
# A result must land within this distance of the expected point to count.
# City-scale: distinguishes London UK from London Ontario.
TOLERANCE_KM = 50.0


@dataclass
class Case:
    """A type-ahead scenario: the user wants `target`, typing `query`."""

    query: str  # full text the user would eventually type
    target: str  # expected primary name (normalized prefix match)
    lat: float
    lon: float
    note: str = ""
    # Some cases only make sense typed in full (regression checks)
    full_only: bool = False
    # Accept any place with the target name, regardless of location (for
    # cases where the regression is about the NAME winning, and location
    # bias legitimately picks the nearest same-named place)
    name_only: bool = False
    # Additional accepted primary-name variants (native / local-script names).
    # A result counts if its name matches `target` OR any of these, so a
    # coordinate-correct result whose primary name is the native form
    # (Tokyo -> 東京都, Germany -> Deutschland) is not scored as a name failure
    # solely because the benchmark query is an English exonym.
    alt_targets: tuple[str, ...] = ()


CASES = [
    # Global type-ahead: famous places a user expects after few keystrokes
    Case("london", "London", 51.5074, -0.1278),
    Case("paris", "Paris", 48.8566, 2.3522),
    Case("tokyo", "Tokyo", 35.6762, 139.6503, alt_targets=("東京都", "東京")),
    Case("berlin", "Berlin", 52.5200, 13.4050),
    Case("sao paulo", "São Paulo", -23.5505, -46.6333),
    Case("beijing", "Beijing", 39.9042, 116.4074, alt_targets=("北京市", "北京")),
    Case("mexico city", "Mexico City", 19.4326, -99.1332,
         alt_targets=("Ciudad de México", "CDMX")),
    Case("amsterdam", "Amsterdam", 52.3676, 4.9041),
    Case("new york", "New York", 40.7128, -74.0060),
    Case("san francisco", "San Francisco", 37.7749, -122.4194),
    # Famous-but-small (the HEAD wiki rung)
    Case("gettysburg", "Gettysburg", 39.8309, -77.2311, note="famous-small"),
    # Exonym via alternate names
    Case("moscow", "Москва", 55.7558, 37.6173, note="exonym"),
    # Regression: porter stemmer made "france" match San Francisco (P0)
    Case("france", "France", 46.6, 2.4, note="P0 stemmer regression"),
    # Regression: partial-match credit let Gelsenkirchen beat Deutschland
    Case("germany", "Germany", 51.1, 10.4, note="partial-credit regression",
         alt_targets=("Deutschland",)),
    # Regression: alt-name token-bag rung let NYC capture "york". Any exact
    # York may win (location bias picks the nearest one); NYC must not.
    Case("york", "York", 53.96, -1.08, note="alt-name rung regression",
         name_only=True),
    # Comma-qualified query must reach the exact rung
    Case("boston, ma", "Boston", 42.3601, -71.0589, note="comma query",
         full_only=True),
]


# Third-language exonym queries: the user types a place name in a language that
# is neither the place's local language nor English. Today `search_name` only
# carries primary + short + English common/official/alternate names, so these
# should mostly MISS on Overture while resolving on Photon (which ingests the
# full OSM `name:<lang>` set). This set quantifies that gap to decide whether
# language-specific shards / a names_i18n table are worth building
# (docs/plans/2026-07-02-future-work.md, section 2).
#
# alt_targets deliberately span the local/native name, the English exonym, and
# the query-language form so a coordinate-correct hit counts no matter which
# name form the engine surfaces; the 50 km tolerance still disambiguates
# same-named decoys (Pekin IL vs Beijing, Londres AR vs London GB).
MULTILINGUAL_CASES = [
    # French exonyms
    Case("moscou", "Москва", 55.7558, 37.6173, note="multilingual",
         alt_targets=("Moscow", "Moskau", "Moscou", "Moskva")),
    Case("londres", "London", 51.5074, -0.1278, note="multilingual",
         alt_targets=("Londres",)),
    Case("venise", "Venezia", 45.4372, 12.3346, note="multilingual",
         alt_targets=("Venice", "Venise", "Venedig")),
    # German exonyms
    Case("moskau", "Москва", 55.7558, 37.6173, note="multilingual",
         alt_targets=("Moscow", "Moskau", "Moscou", "Moskva")),
    Case("tokio", "東京都", 35.6762, 139.6503, note="multilingual",
         alt_targets=("Tokyo", "東京", "Tokio")),
    # Spanish exonyms (unaccented, as typed)
    Case("nueva york", "New York", 40.7128, -74.0060, note="multilingual",
         alt_targets=("Nueva York",)),
    Case("florencia", "Firenze", 43.7698, 11.2556, note="multilingual",
         alt_targets=("Florence", "Florencia", "Florenz")),
    Case("atenas", "Αθήνα", 37.9756, 23.7348, note="multilingual",
         alt_targets=("Athens", "Atenas", "Athina")),
    # Polish exonym
    Case("kolonia", "Köln", 50.9384, 6.9600, note="multilingual",
         alt_targets=("Cologne", "Kolonia", "Koln")),
    # Shared across several Romance/Slavic languages (es/it/pl "Prague")
    Case("praga", "Praha", 50.0875, 14.4213, note="multilingual",
         alt_targets=("Prague", "Praga")),
    # fr/es "Beijing" — unaccented, as typed
    Case("pekin", "北京市", 39.9042, 116.4074, note="multilingual",
         alt_targets=("Beijing", "北京", "Pekin", "Pekín", "Pékin")),
    # Non-Latin-script queries
    Case("モスクワ", "Москва", 55.7558, 37.6173, note="multilingual",
         alt_targets=("Moscow", "モスクワ", "Moskva")),  # Japanese katakana
    Case("варшава", "Warszawa", 52.2320, 21.0067, note="multilingual",
         alt_targets=("Warsaw", "Варшава", "Warschau")),  # Russian Cyrillic
]


CASE_SETS = {
    "standard": CASES,
    "multilingual": MULTILINGUAL_CASES,
    "all": CASES + MULTILINGUAL_CASES,
}


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return r * 2 * atan2(sqrt(a), sqrt(1 - a))


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower().strip())
    return "".join(c for c in s if not unicodedata.combining(c))


def is_target(case: Case, name: str, lat: float, lon: float) -> bool:
    """Result counts when its name matches any accepted variant (the target or
    an alt_target, prefix match in either direction for partial typing) and it
    is geographically the right place."""
    n = normalize(name.split(",")[0])
    accepted = (normalize(t.split(",")[0]) for t in (case.target, *case.alt_targets))
    if not any(n.startswith(t) or t.startswith(n) for t in accepted):
        return False
    if case.name_only:
        return True
    return haversine_km(case.lat, case.lon, lat, lon) <= TOLERANCE_KM


@dataclass
class Engine:
    name: str
    rate_limit_delay: float

    def top_k(self, text: str) -> tuple[list[tuple[str, float, float]], float, Optional[str]]:
        """Return ([(name, lat, lon)] up to TOP_K, latency_ms, error)."""
        raise NotImplementedError


class Overture(Engine):
    def __init__(self, base_url: str = "https://geocoder.bradr.dev"):
        super().__init__("Overture", 0.15)
        self.base_url = base_url

    def top_k(self, text: str):
        url = f"{self.base_url}/search?q={urllib.parse.quote(text)}&limit={TOP_K}"
        start = time.perf_counter()
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": USER_AGENT})
            ms = (time.perf_counter() - start) * 1000
            resp.raise_for_status()
            rows = resp.json().get("results", [])
            return ([(r.get("name", ""), r.get("lat", 0), r.get("lon", 0)) for r in rows], ms, None)
        except Exception as e:
            return ([], (time.perf_counter() - start) * 1000, str(e))


class Photon(Engine):
    def __init__(self):
        super().__init__("Photon", 1.0)

    def top_k(self, text: str):
        start = time.perf_counter()
        try:
            resp = requests.get(
                "https://photon.komoot.io/api/",
                params={"q": text, "limit": TOP_K},
                timeout=10,
                headers={"User-Agent": USER_AGENT},
            )
            ms = (time.perf_counter() - start) * 1000
            resp.raise_for_status()
            out = []
            for f in resp.json().get("features", []):
                props = f.get("properties", {})
                c = f.get("geometry", {}).get("coordinates", [0, 0])
                out.append((props.get("name", ""), c[1], c[0]))
            return (out, ms, None)
        except Exception as e:
            return ([], (time.perf_counter() - start) * 1000, str(e))


class Nominatim(Engine):
    def __init__(self):
        super().__init__("Nominatim", 1.1)  # strict 1 req/s policy

    def top_k(self, text: str):
        start = time.perf_counter()
        try:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": text, "format": "json", "limit": TOP_K},
                timeout=10,
                headers={"User-Agent": USER_AGENT},
            )
            ms = (time.perf_counter() - start) * 1000
            resp.raise_for_status()
            out = []
            for r in resp.json():
                name = r.get("name") or r.get("display_name", "").split(",")[0]
                out.append((name, float(r.get("lat", 0)), float(r.get("lon", 0))))
            return (out, ms, None)
        except Exception as e:
            return ([], (time.perf_counter() - start) * 1000, str(e))


def prefixes_for(case: Case) -> list[str]:
    if case.full_only:
        return [case.query]
    return [case.query[:i] for i in range(MIN_PREFIX, len(case.query) + 1)]


@dataclass
class CaseResult:
    query: str
    target: str
    note: str
    # prefix length -> rank of target (1-based) or None
    ranks: dict = field(default_factory=dict)
    keystrokes_to_top1: Optional[int] = None
    keystrokes_to_top3: Optional[int] = None
    full_query_rank: Optional[int] = None
    latencies_ms: list = field(default_factory=list)
    errors: int = 0


def run_engine(engine: Engine, cases: list[Case], quiet: bool) -> dict:
    results = []
    for case in cases:
        cr = CaseResult(case.query, case.target, case.note)
        for prefix in prefixes_for(case):
            rows, ms, err = engine.top_k(prefix)
            cr.latencies_ms.append(ms)
            if err:
                cr.errors += 1
                cr.ranks[len(prefix)] = None
            else:
                rank = next(
                    (i + 1 for i, (n, lat, lon) in enumerate(rows) if is_target(case, n, lat, lon)),
                    None,
                )
                cr.ranks[len(prefix)] = rank
                if rank == 1 and cr.keystrokes_to_top1 is None:
                    cr.keystrokes_to_top1 = len(prefix)
                if rank is not None and rank <= 3 and cr.keystrokes_to_top3 is None:
                    cr.keystrokes_to_top3 = len(prefix)
            time.sleep(engine.rate_limit_delay)
        cr.full_query_rank = cr.ranks.get(len(case.query))
        results.append(cr)
        if not quiet:
            k1 = cr.keystrokes_to_top1 or "-"
            print(f"  {engine.name:10s} {case.query!r:18s} top1@{k1} chars, "
                  f"full-query rank {cr.full_query_rank}")
    return {
        "engine": engine.name,
        "cases": [asdict(r) for r in results],
    }


def summarize(engine_results: dict) -> dict:
    cases = engine_results["cases"]
    typed = [c for c in cases if len(c["ranks"]) > 1]
    k1 = [c["keystrokes_to_top1"] for c in typed]
    lat = [ms for c in cases for ms in c["latencies_ms"]]
    full_top1 = sum(1 for c in cases if c["full_query_rank"] == 1)
    return {
        "cases": len(cases),
        "full_query_top1": full_top1,
        "typeahead_cases": len(typed),
        "resolved_before_full_query": sum(
            1 for c, k in zip(typed, k1) if k is not None and k < len(c["query"])
        ),
        "median_keystrokes_to_top1": (
            statistics.median([k for k in k1 if k is not None]) if any(k is not None for k in k1) else None
        ),
        "unresolved_top1": sum(1 for k in k1 if k is None),
        "latency_p50_ms": round(statistics.median(lat), 1) if lat else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--output", type=str, help="Write raw JSON results here")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--overture-url", default="https://geocoder.bradr.dev")
    parser.add_argument("--skip", default="", help="Comma-separated engines to skip")
    parser.add_argument(
        "--cases", choices=sorted(CASE_SETS), default="standard",
        help="Which case set to run: standard (default), multilingual "
             "(third-language exonyms), or all",
    )
    args = parser.parse_args()

    cases = CASE_SETS[args.cases]

    skip = {s.strip().lower() for s in args.skip.split(",") if s.strip()}
    engines = [e for e in (Overture(args.overture_url), Photon(), Nominatim())
               if e.name.lower() not in skip]

    all_results = {}
    for engine in engines:
        n_queries = sum(len(prefixes_for(c)) for c in cases)
        print(f"\n=== {engine.name} ({n_queries} queries, "
              f"~{n_queries * engine.rate_limit_delay:.0f}s) ===")
        all_results[engine.name] = run_engine(engine, cases, args.quiet)

    print("\n=== Summary ===")
    summaries = {name: summarize(r) for name, r in all_results.items()}
    header = f"{'':22s}" + "".join(f"{name:>12s}" for name in summaries)
    print(header)
    for key in ["full_query_top1", "resolved_before_full_query",
                "median_keystrokes_to_top1", "unresolved_top1", "latency_p50_ms"]:
        row = f"{key:22s}"
        for s in summaries.values():
            row += f"{str(s[key]):>12s}"
        print(row)

    if args.output:
        payload = {
            "metadata": {
                "top_k": TOP_K,
                "min_prefix": MIN_PREFIX,
                "tolerance_km": TOLERANCE_KM,
                "engines": list(all_results),
                "case_set": args.cases,
                "n_cases": len(cases),
            },
            "summaries": summaries,
            "results": all_results,
        }
        with open(args.output, "w") as f:
            json.dump(payload, f, indent=1)
        print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
