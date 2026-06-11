#!/usr/bin/env python3
"""
Cold/warm latency benchmark for the Overture geocoder worker.

Measures /search, /reverse, and /id/:gers_id latency, separating "cold"
(first request against a location shard, likely an edge-cache miss) from
"warm" (immediate repeats of the same request).

Shard selection for /search and /reverse is location-driven, so each target
uses lat/lon overrides in a different low-traffic country to maximize the
chance the first request is a genuine R2 fetch. Cold is best-effort: if
another client (or a recent run) warmed that PoP's cache within the shard
TTL, "cold" samples will look warm. Run from the same network for
comparable results, and compare percentiles, not single samples.

Paced to stay under the worker's 60 req/min per-IP rate limit.

Usage:
    python scripts/benchmark_latency.py --output baseline.json
    python scripts/benchmark_latency.py --output after.json --compare baseline.json
"""

import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

DEFAULT_BASE_URL = "https://geocoder.bradr.dev"

# Each target drives /search (q + lat/lon override) and /reverse (lat/lon).
# Mostly low-traffic countries so the first request per run is likely an
# edge-cache miss; New York is a high-traffic control.
TARGETS = [
    {"name": "bhutan", "q": "Thimphu", "lat": 27.4716, "lon": 89.6386},
    {"name": "suriname", "q": "Paramaribo", "lat": 5.8520, "lon": -55.2038},
    {"name": "iceland", "q": "Reykjavik", "lat": 64.1466, "lon": -21.9426},
    {"name": "madagascar", "q": "Antananarivo", "lat": -18.8792, "lon": 47.5079},
    {"name": "mongolia", "q": "Ulaanbaatar", "lat": 47.8864, "lon": 106.9057},
    {"name": "namibia", "q": "Windhoek", "lat": -22.5609, "lon": 17.0658},
    {"name": "georgia", "q": "Tbilisi", "lat": 41.7151, "lon": 44.8271},
    {"name": "us-east", "q": "New York", "lat": 40.7128, "lon": -74.0060},
]


def git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=Path(__file__).parent,
        ).stdout.strip()
    except OSError:
        return "unknown"


class Bench:
    def __init__(self, base_url: str, interval: float, timeout: float):
        self.base_url = base_url.rstrip("/")
        self.interval = interval
        self.timeout = timeout
        self.session = requests.Session()
        self.samples = []
        self._last_request = 0.0

    def _pace(self):
        wait = self._last_request + self.interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def request(self, endpoint: str, phase: str, target: str, path: str):
        """Make one paced request; returns parsed JSON body or None."""
        self._pace()
        url = f"{self.base_url}{path}"
        start = time.perf_counter()
        status, body, error = None, None, None
        try:
            resp = self.session.get(url, timeout=self.timeout)
            ms = (time.perf_counter() - start) * 1000
            status = resp.status_code
            if status == 429:
                # Rate limited: back off and don't record the sample.
                retry_after = float(resp.headers.get("Retry-After", 30))
                print(f"    429 rate limited, sleeping {retry_after:.0f}s", file=sys.stderr)
                time.sleep(retry_after)
                return None
            try:
                body = resp.json()
            except ValueError:
                body = None
        except requests.RequestException as e:
            ms = (time.perf_counter() - start) * 1000
            error = str(e)

        self.samples.append({
            "endpoint": endpoint,
            "phase": phase,
            "target": target,
            "path": path,
            "status": status,
            "ms": round(ms, 1),
            "error": error,
        })
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
        if s["error"] or s["status"] is None or s["status"] >= 400:
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
    print(f"\nComparison vs baseline ({baseline['meta']['timestamp']},"
          f" sha {baseline['meta']['git_sha']}):")
    print(f"{'group':<18}{'base p50':>10}{'now p50':>10}{'Δ':>8}"
          f"{'base p95':>10}{'now p95':>10}{'Δ':>8}")
    print("-" * 74)
    for group in sorted(set(base_sum) | set(cur_sum)):
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
                        help="max GERS IDs to benchmark via /id (default: 6)")
    parser.add_argument("--interval", type=float, default=1.2,
                        help="seconds between requests; keep >1.0 to stay under "
                             "the 60 req/min rate limit (default: 1.2)")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--output", type=str, help="write results JSON here")
    parser.add_argument("--compare", type=str, help="baseline JSON to diff against")
    args = parser.parse_args()

    bench = Bench(args.base_url, args.interval, args.timeout)

    n_targets = len(TARGETS)
    total = 1 + n_targets * 2 * (1 + args.warm_repeats) + args.id_targets * (1 + args.warm_repeats)
    print(f"~{total} requests at {args.interval}s spacing "
          f"(~{total * args.interval / 60:.1f} min) against {args.base_url}\n")

    # Health first: warms TLS + HEAD-side catalog lookups, records data version.
    health = bench.request("health", "warm", "-", "/health") or {}
    data_version = health.get("version")

    # Cold pass: one search + one reverse per target, interleaved so
    # back-to-back requests never hit the same shard.
    gers_ids = []
    for t in TARGETS:
        q = urllib.parse.quote(t["q"])
        body = bench.request(
            "search", "cold", t["name"],
            f"/search?q={q}&lat={t['lat']}&lon={t['lon']}")
        for r in (body or {}).get("results", []):
            if r.get("gers_id"):
                gers_ids.append(r["gers_id"])
                break
        bench.request("reverse", "cold", t["name"],
                      f"/reverse?lat={t['lat']}&lon={t['lon']}")

    # Warm passes: identical requests, immediately after.
    for _ in range(args.warm_repeats):
        for t in TARGETS:
            q = urllib.parse.quote(t["q"])
            bench.request("search", "warm", t["name"],
                          f"/search?q={q}&lat={t['lat']}&lon={t['lon']}")
            bench.request("reverse", "warm", t["name"],
                          f"/reverse?lat={t['lat']}&lon={t['lon']}")

    # /id lookups: cold then warm, using IDs harvested from search results.
    gers_ids = gers_ids[:args.id_targets]
    if gers_ids:
        for gid in gers_ids:
            bench.request("id", "cold", gid[:8], f"/id/{gid}")
        for _ in range(args.warm_repeats):
            for gid in gers_ids:
                bench.request("id", "warm", gid[:8], f"/id/{gid}")
    else:
        print("  (no gers_ids harvested; skipping /id)", file=sys.stderr)

    errors = sum(1 for s in bench.samples
                 if s["error"] or s["status"] is None or s["status"] >= 400)
    summary = summarize(bench.samples)
    print_summary(summary, errors)

    result = {
        "meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "base_url": args.base_url,
            "git_sha": git_sha(),
            "data_version": data_version,
            "interval_s": args.interval,
            "warm_repeats": args.warm_repeats,
        },
        "summary": summary,
        "samples": bench.samples,
    }

    if args.compare:
        with open(args.compare) as f:
            print_comparison(json.load(f), result)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    main()
