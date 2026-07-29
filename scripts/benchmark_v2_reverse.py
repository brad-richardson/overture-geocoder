#!/usr/bin/env python3
"""Accuracy and latency benchmark for point-family ``/v2/reverse``.

The case file contains record coordinates plus the exact expected GERS ID.
HTTP errors remain misses. Results report recall@1, recall@5, reciprocal rank,
and client/server latency separately for POIs and addresses and for the first
(``cold``) request versus immediate repeats (``warm``).

Usage:
    python scripts/benchmark_v2_reverse.py \
        --base-url http://127.0.0.1:8787 --cases reverse-cases.json \
        --warm-repeats 3 --assert-gates --output reverse-results.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


CASES_SCHEMA = "benchmark-v2-reverse-cases-v1"
RESULTS_SCHEMA = "benchmark-v2-reverse-results-v1"
FAMILY_TYPES = {"places": "poi", "addresses": "address"}
DEFAULT_RADIUS = {"places": 250, "addresses": 100}
DEFAULT_LIMIT = 5


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = min(int(len(ordered) * fraction), len(ordered) - 1)
    return ordered[position]


def server_timing_ms(value: str | None) -> float | None:
    if not value:
        return None
    for item in value.split(","):
        name, *parameters = item.strip().split(";")
        if name != "total":
            continue
        for parameter in parameters:
            key, separator, raw = parameter.partition("=")
            if key.strip() == "dur" and separator:
                try:
                    return float(raw)
                except ValueError:
                    return None
    return None


def validate_cases(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or value.get("schema") != CASES_SCHEMA:
        raise ValueError(f"case file schema must be {CASES_SCHEMA}")
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("case file must contain a non-empty cases array")
    normalized = []
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"case {index} is not an object")
        case_id = case.get("id")
        family = case.get("family")
        expected_id = case.get("expected_gers_id")
        longitude = case.get("longitude")
        latitude = case.get("latitude")
        if (
            not isinstance(case_id, str)
            or not case_id
            or case_id in seen
            or family not in FAMILY_TYPES
            or not isinstance(expected_id, str)
            or not expected_id
            or isinstance(longitude, bool)
            or not isinstance(longitude, (int, float))
            or not -180 <= longitude <= 180
            or isinstance(latitude, bool)
            or not isinstance(latitude, (int, float))
            or not -90 <= latitude <= 90
        ):
            raise ValueError(f"case {index} is invalid")
        radius = case.get("radius_m", DEFAULT_RADIUS[family])
        if isinstance(radius, bool) or not isinstance(radius, int) or radius < 1:
            raise ValueError(f"case {index} radius is invalid")
        normalized.append(
            {
                "id": case_id,
                "family": family,
                "expected_gers_id": expected_id.lower(),
                "longitude": float(longitude),
                "latitude": float(latitude),
                "radius_m": radius,
            }
        )
        seen.add(case_id)
    return normalized


def request_path(case: dict[str, Any], *, limit: int = DEFAULT_LIMIT) -> str:
    query = urlencode(
        {
            "lon": case["longitude"],
            "lat": case["latitude"],
            "types": FAMILY_TYPES[case["family"]],
            "radius": case["radius_m"],
            "limit": limit,
        }
    )
    return f"/v2/reverse?{query}"


def score_response(
    case: dict[str, Any], status: int | None, body: Any, error: str | None
) -> dict[str, Any]:
    features = body.get("features") if isinstance(body, dict) else None
    valid = (
        error is None
        and status is not None
        and 200 <= status < 300
        and body.get("type") == "FeatureCollection"
        and isinstance(features, list)
    )
    ids = (
        [
            feature.get("id", "").lower()
            for feature in features
            if isinstance(feature, dict) and isinstance(feature.get("id"), str)
        ]
        if valid
        else []
    )
    expected = case["expected_gers_id"].lower()
    rank = ids.index(expected) + 1 if expected in ids else None
    return {
        "valid_response": valid,
        "rank": rank,
        "recall_at_1": rank == 1,
        "recall_at_5": rank is not None and rank <= 5,
        "reciprocal_rank": 1.0 / rank if rank else 0.0,
        "features": len(features) if valid else 0,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    client_ms = [row["client_ms"] for row in rows]
    worker_ms = [
        row["worker_ms"] for row in rows if row.get("worker_ms") is not None
    ]
    valid = sum(row["valid_response"] for row in rows)
    return {
        "n": total,
        "valid_responses": valid,
        "errors": total - valid,
        "recall_at_1": sum(row["recall_at_1"] for row in rows) / total,
        "recall_at_5": sum(row["recall_at_5"] for row in rows) / total,
        "mrr": sum(row["reciprocal_rank"] for row in rows) / total,
        "client_ms": {
            "p50": statistics.median(client_ms),
            "p95": percentile(client_ms, 0.95),
            "min": min(client_ms),
            "max": max(client_ms),
        },
        "worker_ms": {
            "n": len(worker_ms),
            "p50": statistics.median(worker_ms) if worker_ms else None,
            "p95": percentile(worker_ms, 0.95),
        },
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {"all": rows}
    for row in rows:
        groups.setdefault(row["family"], []).append(row)
        groups.setdefault(f"{row['family']}/{row['phase']}", []).append(row)
    return {name: aggregate(values) for name, values in sorted(groups.items())}


def run(
    *,
    base_url: str,
    cases: list[dict[str, Any]],
    warm_repeats: int,
    interval_s: float,
    timeout_s: float,
    session: Any = None,
) -> list[dict[str, Any]]:
    client = session or requests.Session()
    rows = []
    base_url = base_url.rstrip("/")
    for repeat in range(warm_repeats + 1):
        phase = "cold" if repeat == 0 else "warm"
        for case in cases:
            if rows and interval_s:
                time.sleep(interval_s)
            path = request_path(case)
            started = time.perf_counter()
            status = None
            body = None
            error = None
            headers: dict[str, str] = {}
            try:
                response = client.get(f"{base_url}{path}", timeout=timeout_s)
                status = response.status_code
                headers = response.headers
                try:
                    body = response.json()
                except ValueError:
                    error = "response is not JSON"
            except requests.RequestException as exception:
                error = str(exception)
            client_ms = (time.perf_counter() - started) * 1000
            scored = score_response(case, status, body, error)
            row = {
                "case_id": case["id"],
                "family": case["family"],
                "phase": phase,
                "repeat": repeat,
                "path": path,
                "status": status,
                "error": error,
                "client_ms": client_ms,
                "worker_ms": server_timing_ms(headers.get("Server-Timing")),
                **scored,
            }
            rows.append(row)
            print(
                f"{phase:>4} {case['family']:<9} {case['id']:<28} "
                f"{client_ms:7.1f} ms rank={row['rank']} status={status}"
            )
    return rows


def gate_failures(summary: dict[str, Any]) -> list[str]:
    failures = []
    for family in FAMILY_TYPES:
        overall = summary.get(family)
        warm = summary.get(f"{family}/warm")
        if not overall or overall["errors"]:
            failures.append(f"{family}: HTTP/shape errors")
        if not overall or overall["recall_at_5"] < 0.99:
            failures.append(f"{family}: recall@5 below 0.99")
        if warm and warm["client_ms"]["p50"] > 250:
            failures.append(f"{family}: warm client p50 above 250 ms")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--warm-repeats", type=int, default=3)
    parser.add_argument("--interval", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--assert-gates", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.warm_repeats < 0 or args.interval < 0 or args.timeout <= 0:
        parser.error("warm-repeats/interval must be non-negative and timeout positive")

    try:
        cases = validate_cases(json.loads(args.cases.read_text()))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    rows = run(
        base_url=args.base_url,
        cases=cases,
        warm_repeats=args.warm_repeats,
        interval_s=args.interval,
        timeout_s=args.timeout,
    )
    summary = summarize(rows)
    result = {
        "schema": RESULTS_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_url": args.base_url,
        "warm_repeats": args.warm_repeats,
        "summary": summary,
        "rows": rows,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.output:
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    failures = gate_failures(summary)
    if args.assert_gates and failures:
        print("reverse benchmark gates failed: " + "; ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
