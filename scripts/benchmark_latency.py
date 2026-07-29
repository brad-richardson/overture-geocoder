#!/usr/bin/env python3
"""
Cold/warm latency and reverse-quality benchmark for the Overture geocoder worker.

Measures /search, /reverse, /v2/reverse point-family route classes, and
/id/:gers_id latency, separating "cold" (first request against a location
shard, likely an edge-cache miss) from "warm" (immediate repeats of the same
request).

Shard selection for /search and /reverse is coordinate-driven, so each target
uses a location in a different low-traffic country to maximize the chance the
first request is a genuine R2 fetch. Cold is best-effort: if another client
(or a recent run) warmed that PoP's cache within the shard TTL, "cold" samples
will look warm. Run from the same network for comparable results, and compare
percentiles, not single samples.

Every reverse target has explicit expectations for an accepted administrative
container and country. Quality is scored once per unique target; cold/warm
repeat stability is reported separately. Reverse-result IDs are also looked up
through /id to verify endpoint consistency. Because the API hierarchy does not
currently expose country/region codes as structured fields, coherence checks
only use explicit country entries, expected region names, and unambiguous
``CC-region`` name suffixes. They deliberately do not guess from bare two-letter
US-style region suffixes.

Paced to stay under the worker's 60 req/min per-IP rate limit.

Usage:
    python scripts/benchmark_latency.py --output baseline.json
    python scripts/benchmark_latency.py --assert-reverse-quality
    python scripts/benchmark_latency.py --output after.json --compare baseline.json
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
import urllib.parse
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

DEFAULT_BASE_URL = "https://geocoder.bradr.dev"

# Each target drives /search (q + lat/lon override) and /reverse (lat/lon).
# Accepted containers include legitimate municipality/district answers where
# the bbox-only dataset can identify a more specific administrative container.
# They do not bless known cross-country bbox overlap failures: Bhutan and
# Madagascar remain regression cases for routing/hierarchy coherence.
TARGETS = [
    {"name": "bhutan", "q": "Thimphu", "lat": 27.4716, "lon": 89.6386,
     "reverse_name": "Thimphu", "expected_country": "BT",
     "expected_country_names": ["Bhutan"]},
    {"name": "suriname", "q": "Paramaribo", "lat": 5.8520, "lon": -55.2038,
     "reverse_name": "Paramaribo", "accepted_container_names": ["Weg naar Zee"],
     "accepted_subtypes": ["locality", "county"], "expected_country": "SR",
     "expected_country_names": ["Suriname"]},
    {"name": "iceland", "q": "Reykjavik", "lat": 64.1466, "lon": -21.9426,
     "reverse_name": "Reykjavik",
     "accepted_container_names": ["Reykjavík", "Reykjavíkurborg"],
     "accepted_subtypes": ["locality", "county"], "expected_country": "IS",
     "expected_country_names": ["Iceland", "Ísland"]},
    {"name": "madagascar", "q": "Antananarivo", "lat": -18.8792, "lon": 47.5079,
     "reverse_name": "Antananarivo", "expected_country": "MG",
     "expected_country_names": ["Madagascar"]},
    {"name": "mongolia", "q": "Ulaanbaatar", "lat": 47.8864, "lon": 106.9057,
     "reverse_name": "Ulaanbaatar", "reverse_alt_names": ["Улаанбаатар"],
     "accepted_container_names": ["Bayangol"],
     "accepted_subtypes": ["locality", "county"], "expected_country": "MN",
     "expected_country_names": ["Mongolia", "Монгол Улс"]},
    {"name": "namibia", "q": "Windhoek", "lat": -22.5609, "lon": 17.0658,
     "reverse_name": "Windhoek", "expected_country": "NA",
     "expected_country_names": ["Namibia"]},
    {"name": "georgia", "q": "Tbilisi", "lat": 41.7151, "lon": 44.8271,
     "reverse_name": "Tbilisi", "reverse_alt_names": ["თბილისი"],
     "expected_country": "GE", "expected_country_names": ["Georgia", "საქართველო"],
     "expected_region_names": ["Tbilisi", "თბილისი"]},
    {"name": "us-east", "q": "New York", "lat": 40.7128, "lon": -74.0060,
     "reverse_name": "New York", "accepted_container_names": ["Manhattan"],
     "expected_country": "US", "expected_country_names": ["United States"],
     "expected_region_names": ["New York"]},
]

# Point-family v2 reverse routes have different serving artifacts and read
# shapes, so they remain separate latency classes. Correctness/recall is gated
# by benchmark_v2_reverse.py; this script supplies comparable deployed-edge
# cold/warm measurements for each class.
V2_REVERSE_CLASSES = (
    ("v2-reverse-poi", "poi"),
    ("v2-reverse-address", "address"),
)


def normalize_name(value: str) -> str:
    """Case- and accent-insensitive comparison form for reverse names."""
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in value if not unicodedata.combining(char))


def _name_matches(observed: object, accepted: list[str] | tuple[str, ...]) -> bool:
    return isinstance(observed, str) and any(
        normalize_name(observed).startswith(normalize_name(candidate))
        for candidate in accepted
    )


def _context_code(name: object, subtype: object) -> str | None:
    """Extract only country codes that are unambiguous in display names."""
    if not isinstance(name, str):
        return None
    suffix = name.rsplit(",", 1)[-1].strip().upper()
    region_code = re.fullmatch(r"([A-Z]{2})-[A-Z0-9-]+", suffix)
    if region_code:
        return region_code.group(1)
    if subtype == "country" and re.fullmatch(r"[A-Z]{2}", suffix):
        return suffix
    return None


def v2_reverse_path(target: dict, feature_type: str) -> str:
    """Build one point-family v2 reverse request without locale formatting."""
    return "/v2/reverse?" + urllib.parse.urlencode(
        {
            "lon": target["lon"],
            "lat": target["lat"],
            "types": feature_type,
        }
    )


def evaluate_reverse_quality(body: dict | None, target: dict) -> dict:
    """Evaluate one reverse response across independent quality dimensions.

    Display-name context is not a structured lineage contract. Checks therefore
    use target-specific accepted containers and only parse unambiguous codes.
    """
    expected_name = target.get("reverse_name")
    if not expected_name:
        return {"passed": None, "reason": "no expectation"}
    if not isinstance(body, dict):
        return {
            "passed": False,
            "expected_name": expected_name,
            "dimensions": {
                "container": False,
                "country": False,
                "region": None,
                "hierarchy_coherence": None,
                "reverse_id": False,
            },
            "reason": "missing or invalid JSON response",
        }

    observed_name = body.get("primary_name")
    observed_subtype = body.get("subtype")
    hierarchy = body.get("hierarchy")
    hierarchy = hierarchy if isinstance(hierarchy, list) else []
    observed_containers = [
        {"name": observed_name, "subtype": observed_subtype},
        *(entry for entry in hierarchy if isinstance(entry, dict)),
    ]

    accepted_names = [
        expected_name,
        *target.get("reverse_alt_names", ()),
        *target.get("accepted_container_names", ()),
    ]
    accepted_subtypes = target.get("accepted_subtypes", ["locality"])
    container_matches = any(
        _name_matches(entry.get("name"), accepted_names)
        and entry.get("subtype") in accepted_subtypes
        for entry in observed_containers
    )

    expected_country = target.get("expected_country")
    expected_country_names = target.get("expected_country_names", ())
    country_entries = [entry for entry in hierarchy if entry.get("subtype") == "country"]
    country_matches = None
    if expected_country or expected_country_names:
        country_matches = any(
            _name_matches(entry.get("name"), expected_country_names)
            or _context_code(entry.get("name"), "country") == expected_country
            for entry in country_entries
        )

    expected_regions = target.get("expected_region_names", ())
    region_matches = None
    if expected_regions:
        region_matches = any(
            entry.get("subtype") == "region"
            and _name_matches(entry.get("name"), expected_regions)
            for entry in hierarchy
        )

    observed_country_codes = {
        code
        for entry in observed_containers
        if (code := _context_code(entry.get("name"), entry.get("subtype")))
    }
    hierarchy_coherent = None
    if expected_country and observed_country_codes:
        hierarchy_coherent = observed_country_codes == {expected_country}

    reverse_id_present = isinstance(body.get("gers_id"), str) and bool(body["gers_id"])
    dimensions = {
        "container": container_matches,
        "country": country_matches,
        "region": region_matches,
        "hierarchy_coherence": hierarchy_coherent,
        "reverse_id": reverse_id_present,
    }
    checks = []
    if not container_matches:
        checks.append("accepted container missing")
    if country_matches is False:
        checks.append("country mismatch")
    if region_matches is False:
        checks.append("region mismatch")
    if hierarchy_coherent is False:
        checks.append("observable hierarchy country mismatch")
    if not reverse_id_present:
        checks.append("reverse GERS ID missing")
    passed = all(value is not False for value in dimensions.values())

    return {
        "passed": passed,
        "expected_name": expected_name,
        "accepted_subtypes": accepted_subtypes,
        "expected_country": expected_country,
        "observed_name": observed_name,
        "observed_subtype": observed_subtype,
        "result_signature": [
            body.get("gers_id"),
            observed_name,
            observed_subtype,
            [entry.get("gers_id") for entry in hierarchy],
        ],
        "dimensions": dimensions,
        "reason": "; ".join(checks) if checks else None,
    }


def evaluate_id_consistency(body: dict | None, expected_id: str) -> dict:
    """Verify that /id resolves the exact GERS ID returned by /reverse."""
    observed_id = body.get("id") if isinstance(body, dict) else None
    passed = observed_id == expected_id
    return {
        "passed": passed,
        "expected_id": expected_id,
        "observed_id": observed_id,
        "reason": None if passed else "ID lookup did not return the reverse GERS ID",
    }


def git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=Path(__file__).parent,
        ).stdout.strip()
    except OSError:
        return "unknown"


class Bench:
    def __init__(
        self,
        base_url: str,
        interval: float,
        timeout: float,
        sleep_fn=time.sleep,
        monotonic_fn=time.monotonic,
        rate_limit_retries: int = 1,
    ):
        self.base_url = base_url.rstrip("/")
        self.interval = interval
        self.timeout = timeout
        self.session = requests.Session()
        self.samples = []
        self._last_request = None
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self.rate_limit_retries = max(0, rate_limit_retries)

    def _pace(self):
        now = self._monotonic()
        if self._last_request is None:
            self._last_request = now
            return
        wait = self._last_request + self.interval - now
        if wait > 0:
            self._sleep(wait)
        self._last_request = self._monotonic()

    def request(
        self,
        endpoint: str,
        phase: str,
        target: str,
        path: str,
        reverse_target: dict | None = None,
        expected_id: str | None = None,
    ):
        """Make one paced request; returns parsed JSON body or None."""
        url = f"{self.base_url}{path}"
        start = time.perf_counter()
        status, body, error = None, None, None
        attempts = 0
        rate_limited = False
        for attempt in range(self.rate_limit_retries + 1):
            # Pace every actual HTTP attempt. A retry therefore becomes the
            # reference point for pacing the next benchmark request.
            self._pace()
            attempts += 1
            try:
                resp = self.session.get(url, timeout=self.timeout)
                status = resp.status_code
                if status == 429:
                    rate_limited = True
                    if attempt < self.rate_limit_retries:
                        try:
                            retry_after = float(resp.headers.get("Retry-After", 30))
                        except (TypeError, ValueError):
                            retry_after = 30.0
                        # Avoid a malformed header hanging a benchmark forever.
                        retry_after = max(0.0, min(retry_after, 60.0))
                        print(
                            f"    429 rate limited, retrying once after "
                            f"{retry_after:.0f}s",
                            file=sys.stderr,
                        )
                        self._sleep(retry_after)
                        continue
                    error = f"rate limited after {attempts} attempt(s)"
                    break
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                break
            except requests.RequestException as e:
                error = str(e)
                break
        ms = (time.perf_counter() - start) * 1000

        sample = {
            "endpoint": endpoint,
            "phase": phase,
            "target": target,
            "path": path,
            "status": status,
            "ms": round(ms, 1),
            "error": error,
            "attempts": attempts,
            "rate_limited": rate_limited,
        }
        if reverse_target is not None:
            sample["reverse_quality"] = evaluate_reverse_quality(body, reverse_target)
        if expected_id is not None:
            sample["id_consistency"] = evaluate_id_consistency(body, expected_id)
        self.samples.append(sample)
        ok = status is not None and 200 <= status < 400
        print(f"  {phase:>4} {endpoint:<8} {target:<12} {ms:7.0f}ms  {status or error}")
        return body if ok else None


def percentile(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    idx = min(int(len(s) * p), len(s) - 1)
    return s[idx]


def summarize(samples):
    """Group samples by (endpoint, phase) -> stats dict."""
    groups = {}
    for s in samples:
        if (s["error"] or s["status"] is None or s["status"] >= 400
                or s.get("rate_limited")):
            continue
        groups.setdefault((s["endpoint"], s["phase"]), []).append(s["ms"])
    out = {}
    for (endpoint, phase), vals in sorted(groups.items()):
        out[f"{endpoint}/{phase}"] = {
            "n": len(vals),
            "mean": round(statistics.mean(vals), 1),
            "p50": round(statistics.median(vals), 1),
            "p95": round(percentile(vals, 0.95), 1),
            "min": round(min(vals), 1),
            "max": round(max(vals), 1),
        }
    return out


def _aggregate_dimension(qualities, dimension):
    values = [
        quality.get("dimensions", {}).get(dimension)
        for quality in qualities
        if quality.get("dimensions", {}).get(dimension) is not None
    ]
    if any(value is False for value in values):
        return False
    if any(value is True for value in values):
        return True
    return None


def summarize_reverse_quality(samples, expected_targets=None):
    """Score targets once, failing a target when any cold/warm repeat fails."""
    by_target = {}
    for sample in samples:
        quality = sample.get("reverse_quality", {})
        if sample.get("endpoint") == "reverse" and quality.get("passed") is not None:
            by_target.setdefault(sample["target"], []).append(quality)
    scored = {
        target: all(quality["passed"] for quality in qualities)
        for target, qualities in by_target.items()
    }
    passed = sum(scored.values())
    dimension_summary = {}
    for dimension in (
        "container", "country", "region", "hierarchy_coherence", "reverse_id"
    ):
        values = [
            value
            for qualities in by_target.values()
            if (value := _aggregate_dimension(qualities, dimension)) is not None
        ]
        dimension_summary[dimension] = {
            "n": len(values),
            "passed": sum(value is True for value in values),
            "failed": sum(value is False for value in values),
        }
    stable = sum(
        len({json.dumps(quality.get("result_signature"), sort_keys=True)
             for quality in qualities}) <= 1
        for qualities in by_target.values()
    )
    expected = set(expected_targets or by_target)
    observed = set(by_target)
    return {
        "n": len(scored),
        "passed": passed,
        "failed": len(scored) - passed,
        "pass_rate": round(passed / len(scored), 3) if scored else None,
        "dimensions": dimension_summary,
        "repeat_stability": {
            "n": len(by_target),
            "stable": stable,
            "unstable": len(by_target) - stable,
        },
        "coverage": {
            "expected": len(expected),
            "observed": len(observed & expected),
            "missing": sorted(expected - observed),
        },
    }


def summarize_id_consistency(samples, expected_n=None):
    """Score each target/ID once, failing it when any lookup repeat fails."""
    by_id = {}
    for sample in samples:
        quality = sample.get("id_consistency", {})
        expected_id = quality.get("expected_id")
        if expected_id and quality.get("passed") is not None:
            key = (sample.get("target"), expected_id)
            by_id.setdefault(key, []).append(quality)
    scored = [all(quality["passed"] for quality in qualities)
              for qualities in by_id.values()]
    passed = sum(scored)
    stable = sum(
        len({quality.get("observed_id") for quality in qualities}) <= 1
        for qualities in by_id.values()
    )
    expected = len(by_id) if expected_n is None else expected_n
    return {
        "n": len(scored),
        "passed": passed,
        "failed": len(scored) - passed,
        "pass_rate": round(passed / len(scored), 3) if scored else None,
        "repeat_stability": {
            "n": len(by_id),
            "stable": stable,
            "unstable": len(by_id) - stable,
        },
        "coverage": {
            "expected": expected,
            "observed": len(by_id),
            "missing": max(0, expected - len(by_id)),
        },
    }


def print_reverse_quality_summary(summary):
    if not summary["n"]:
        print("\nReverse quality: no scored responses")
        return
    print(
        "\nReverse quality: "
        f"{summary['passed']}/{summary['n']} passed "
        f"({summary['pass_rate']:.0%}); {summary['failed']} failed; "
        f"repeats {summary['repeat_stability']['stable']}/"
        f"{summary['repeat_stability']['n']} stable"
    )


def print_id_consistency_summary(summary):
    if not summary["n"]:
        print("Reverse → ID consistency: no scored lookups")
        return
    print(
        "Reverse → ID consistency: "
        f"{summary['passed']}/{summary['n']} passed; "
        f"repeats {summary['repeat_stability']['stable']}/"
        f"{summary['repeat_stability']['n']} stable"
    )


def reverse_quality_failures(samples):
    """Return at most one scored reverse failure per unique target."""
    by_target = {}
    for sample in samples:
        if sample.get("endpoint") == "reverse" and sample.get(
            "reverse_quality", {}
        ).get("passed") is not None:
            by_target.setdefault(sample["target"], []).append(sample)
    return [
        next(sample for sample in target_samples
             if sample["reverse_quality"]["passed"] is False)
        for target_samples in by_target.values()
        if any(sample["reverse_quality"]["passed"] is False
               for sample in target_samples)
    ]


def id_consistency_failures(samples):
    """Return at most one failed lookup per unique reverse-derived ID."""
    by_id = {}
    for sample in samples:
        quality = sample.get("id_consistency", {})
        if quality.get("expected_id"):
            key = (sample.get("target"), quality["expected_id"])
            by_id.setdefault(key, []).append(sample)
    return [
        next(sample for sample in id_samples
             if sample["id_consistency"]["passed"] is False)
        for id_samples in by_id.values()
        if any(sample["id_consistency"]["passed"] is False
               for sample in id_samples)
    ]


def quality_coverage_failures(reverse_quality, id_consistency):
    """Describe missing target/ID coverage that must fail assertion mode."""
    failures = []
    if reverse_quality["coverage"]["missing"]:
        failures.append(
            "missing reverse targets: "
            + ", ".join(reverse_quality["coverage"]["missing"])
        )
    if id_consistency["coverage"]["missing"]:
        failures.append(
            "missing reverse-derived ID lookups: "
            f"{id_consistency['coverage']['observed']}/"
            f"{id_consistency['coverage']['expected']}"
        )
    return failures


def print_summary(summary, errors):
    print(f"\n{'group':<18}{'n':>4}{'mean':>9}{'p50':>9}{'p95':>9}{'min':>9}{'max':>9}")
    print("-" * 67)
    for group, st in summary.items():
        print(f"{group:<18}{st['n']:>4}{st['mean']:>9.0f}{st['p50']:>9.0f}"
              f"{st['p95']:>9.0f}{st['min']:>9.0f}{st['max']:>9.0f}")
    if errors:
        print(f"\n{errors} request(s) failed or returned >=400 (excluded from stats)")


def print_comparison(baseline: dict, current: dict):
    base_sum = baseline["summary"]
    cur_sum = current["summary"]
    baseline_id_source = baseline.get("meta", {}).get("id_source")
    current_id_source = current.get("meta", {}).get("id_source")
    comparable_id_source = (
        baseline_id_source is not None
        and baseline_id_source == current_id_source
    )
    print(f"\nComparison vs baseline ({baseline['meta']['timestamp']},"
          f" sha {baseline['meta']['git_sha']}):")
    print(f"{'group':<18}{'base p50':>10}{'now p50':>10}{'Δ':>8}"
          f"{'base p95':>10}{'now p95':>10}{'Δ':>8}")
    print("-" * 74)
    if not comparable_id_source and any(
        group.startswith("id/") for group in set(base_sum) | set(cur_sum)
    ):
        print(
            "ID latency comparison skipped: sample source is missing or differs "
            f"(baseline={baseline_id_source or 'unknown'}, "
            f"current={current_id_source or 'unknown'})."
        )
    for group in sorted(set(base_sum) | set(cur_sum)):
        if group.startswith("id/") and not comparable_id_source:
            continue
        b, c = base_sum.get(group), cur_sum.get(group)
        if not b or not c:
            print(f"{group:<18}{'(only in ' + ('baseline' if b else 'current') + ')':>56}")
            continue

        def delta(old, new):
            return f"{(new - old) / old * +100:+.0f}%" if old else "n/a"

        print(f"{group:<18}{b['p50']:>10.0f}{c['p50']:>10.0f}{delta(b['p50'], c['p50']):>8}"
              f"{b['p95']:>10.0f}{c['p95']:>10.0f}{delta(b['p95'], c['p95']):>8}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--warm-repeats", type=int, default=2,
                        help="warm requests per target per endpoint (default: 2)")
    parser.add_argument("--id-targets", type=int, default=6,
                        help="max reverse-result GERS IDs to verify and benchmark "
                             "via /id (default: 6)")
    parser.add_argument("--interval", type=float, default=1.2,
                        help="seconds between requests; keep >1.0 to stay under "
                             "the 60 req/min rate limit (default: 1.2)")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--assert-reverse-quality", action="store_true",
        help="exit non-zero on any reverse quality, ID consistency, or "
             "expected-coverage failure",
    )
    parser.add_argument("--output", type=str, help="write results JSON here")
    parser.add_argument("--compare", type=str, help="baseline JSON to diff against")
    args = parser.parse_args()

    bench = Bench(args.base_url, args.interval, args.timeout)

    n_targets = len(TARGETS)
    target_route_classes = 2 + len(V2_REVERSE_CLASSES)
    total = (
        1
        + n_targets * target_route_classes * (1 + args.warm_repeats)
        + args.id_targets * (1 + args.warm_repeats)
    )
    print(f"~{total} requests at {args.interval}s spacing "
          f"(~{total * args.interval / 60:.1f} min) against {args.base_url}\n")

    # Health first: warms TLS + HEAD-side catalog lookups, records data version.
    health = bench.request("health", "warm", "-", "/health") or {}
    data_version = health.get("version")

    # Cold pass: one search + one reverse per target, interleaved so
    # back-to-back requests never hit the same shard.
    reverse_ids = []
    for t in TARGETS:
        q = urllib.parse.quote(t["q"])
        bench.request(
            "search", "cold", t["name"],
            f"/search?q={q}&lat={t['lat']}&lon={t['lon']}")
        body = bench.request("reverse", "cold", t["name"],
                             f"/reverse?lat={t['lat']}&lon={t['lon']}", t)
        if isinstance(body, dict) and body.get("gers_id"):
            reverse_ids.append((t["name"], body["gers_id"]))
        for endpoint, feature_type in V2_REVERSE_CLASSES:
            bench.request(
                endpoint,
                "cold",
                t["name"],
                v2_reverse_path(t, feature_type),
            )

    # Warm passes: identical requests, immediately after.
    for _ in range(args.warm_repeats):
        for t in TARGETS:
            q = urllib.parse.quote(t["q"])
            bench.request("search", "warm", t["name"],
                          f"/search?q={q}&lat={t['lat']}&lon={t['lon']}")
            bench.request("reverse", "warm", t["name"],
                          f"/reverse?lat={t['lat']}&lon={t['lon']}", t)
            for endpoint, feature_type in V2_REVERSE_CLASSES:
                bench.request(
                    endpoint,
                    "warm",
                    t["name"],
                    v2_reverse_path(t, feature_type),
                )

    # /id lookups: cold then warm, verifying IDs harvested from reverse results.
    reverse_ids = reverse_ids[:args.id_targets]
    if reverse_ids:
        for target_name, gid in reverse_ids:
            bench.request("id", "cold", target_name, f"/id/{gid}", expected_id=gid)
        for _ in range(args.warm_repeats):
            for target_name, gid in reverse_ids:
                bench.request("id", "warm", target_name, f"/id/{gid}", expected_id=gid)
    else:
        print("  (no reverse gers_ids harvested; skipping /id)", file=sys.stderr)

    errors = sum(1 for s in bench.samples
                 if s["error"] or s["status"] is None or s["status"] >= 400)
    summary = summarize(bench.samples)
    reverse_quality = summarize_reverse_quality(
        bench.samples, expected_targets=[target["name"] for target in TARGETS]
    )
    expected_id_count = min(max(args.id_targets, 0), len(TARGETS))
    id_consistency = summarize_id_consistency(
        bench.samples, expected_n=expected_id_count
    )
    print_summary(summary, errors)
    print_reverse_quality_summary(reverse_quality)
    print_id_consistency_summary(id_consistency)

    result = {
        "meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "base_url": args.base_url,
            "git_sha": git_sha(),
            "data_version": data_version,
            "interval_s": args.interval,
            "warm_repeats": args.warm_repeats,
            "id_source": "reverse",
        },
        "summary": summary,
        "reverse_quality": reverse_quality,
        "id_consistency": id_consistency,
        "samples": bench.samples,
    }

    if args.compare:
        with open(args.compare) as f:
            print_comparison(json.load(f), result)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nResults written to {args.output}")

    failures = reverse_quality_failures(bench.samples)
    id_failures = id_consistency_failures(bench.samples)
    coverage_failures = quality_coverage_failures(reverse_quality, id_consistency)
    if args.assert_reverse_quality and (failures or id_failures or coverage_failures):
        print("\nReverse quality failures:", file=sys.stderr)
        for sample in failures:
            quality = sample["reverse_quality"]
            print(
                f"  {sample['target']}: {quality['reason']} "
                f"(got {quality.get('observed_name')!r}, "
                f"{quality.get('observed_subtype')!r})",
                file=sys.stderr,
            )
        for sample in id_failures:
            quality = sample["id_consistency"]
            print(
                f"  {sample['target']}: {quality['reason']} "
                f"(expected {quality['expected_id']!r}, "
                f"got {quality.get('observed_id')!r})",
                file=sys.stderr,
            )
        for failure in coverage_failures:
            print(f"  coverage: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
