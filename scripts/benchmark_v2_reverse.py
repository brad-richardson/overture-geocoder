#!/usr/bin/env python3
"""Manual accuracy and latency benchmark for reverse geocoders.

The default remains Overture exact-GERS-ID self-recall. Nominatim and Photon
may be selected explicitly for an occasional provider-neutral comparison when
cases also carry semantic expectations. Provider IDs are never compared.

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
import unicodedata
from datetime import datetime, timezone
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


CASES_SCHEMA = "benchmark-v2-reverse-cases-v1"
RESULTS_SCHEMA = "benchmark-v2-reverse-results-v1"
FAMILY_TYPES = {"places": "poi", "addresses": "address"}
DEFAULT_RADIUS = {"places": 250, "addresses": 100}
DEFAULT_LIMIT = 5
PROVIDERS = ("overture", "nominatim", "photon")
PROVIDER_MIN_INTERVAL_S = {"overture": 0.0, "nominatim": 1.1, "photon": 1.0}
DEFAULT_NOMINATIM_URL = "https://nominatim.openstreetmap.org"
DEFAULT_PHOTON_URL = "https://photon.komoot.io"
USER_AGENT = (
    "OvertureGeocoderBenchmark/1.0 "
    "(github.com/brad-richardson/overture-geocoder)"
)
ADDRESS_COMPONENTS = {
    "number",
    "unit",
    "street",
    "postcode",
    "locality",
    "region",
    "country",
    "country_code",
}


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


def normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(value.split())


def haversine_m(
    latitude_a: float, longitude_a: float, latitude_b: float, longitude_b: float
) -> float:
    radius_m = 6_371_008.8
    delta_latitude = radians(latitude_b - latitude_a)
    delta_longitude = radians(longitude_b - longitude_a)
    latitude_a = radians(latitude_a)
    latitude_b = radians(latitude_b)
    value = (
        sin(delta_latitude / 2) ** 2
        + cos(latitude_a) * cos(latitude_b) * sin(delta_longitude / 2) ** 2
    )
    return radius_m * 2 * atan2(sqrt(value), sqrt(1 - value))


def _accepted_values(value: Any, label: str) -> list[str]:
    values = [value] if isinstance(value, str) else value
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(item, str) or not item.strip() for item in values)
    ):
        raise ValueError(f"{label} must be a string or non-empty string array")
    return values


def _semantic_expectation(case: dict[str, Any], index: int) -> dict[str, Any] | None:
    expected_name = case.get("expected_name")
    expected_address = case.get("expected_address")
    if expected_name is None and expected_address is None:
        return None
    if case["family"] == "places":
        names = _accepted_values(expected_name, f"case {index} expected_name")
        alt_names = case.get("alt_names", [])
        if not isinstance(alt_names, list) or any(
            not isinstance(item, str) or not item.strip() for item in alt_names
        ):
            raise ValueError(f"case {index} alt_names is invalid")
        expected = {"names": names + alt_names, "address": {}}
    else:
        if not isinstance(expected_address, dict) or not expected_address:
            raise ValueError(f"case {index} expected_address is invalid")
        unknown = set(expected_address) - ADDRESS_COMPONENTS
        if unknown:
            raise ValueError(
                f"case {index} expected_address has unknown components: "
                + ", ".join(sorted(unknown))
            )
        expected = {
            "names": [],
            "address": {
                key: _accepted_values(value, f"case {index} expected_address.{key}")
                for key, value in expected_address.items()
            },
        }
    expected_longitude = case.get("expected_longitude", case["longitude"])
    expected_latitude = case.get("expected_latitude", case["latitude"])
    tolerance_m = case.get("tolerance_m", case["radius_m"])
    if (
        isinstance(expected_longitude, bool)
        or not isinstance(expected_longitude, (int, float))
        or not -180 <= expected_longitude <= 180
        or isinstance(expected_latitude, bool)
        or not isinstance(expected_latitude, (int, float))
        or not -90 <= expected_latitude <= 90
        or isinstance(tolerance_m, bool)
        or not isinstance(tolerance_m, (int, float))
        or tolerance_m <= 0
    ):
        raise ValueError(f"case {index} semantic coordinates/tolerance are invalid")
    expected.update(
        {
            "longitude": float(expected_longitude),
            "latitude": float(expected_latitude),
            "tolerance_m": float(tolerance_m),
        }
    )
    return expected


def validate_cases(
    value: Any, *, require_exact_id: bool = True
) -> list[dict[str, Any]]:
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
            or (
                require_exact_id
                and (not isinstance(expected_id, str) or not expected_id)
            )
            or (
                expected_id is not None
                and (not isinstance(expected_id, str) or not expected_id)
            )
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
        normalized_case = {
            "id": case_id,
            "family": family,
            "longitude": float(longitude),
            "latitude": float(latitude),
            "radius_m": radius,
        }
        if expected_id is not None:
            normalized_case["expected_gers_id"] = expected_id.lower()
        semantic_case = {**case, **normalized_case}
        normalized_case["semantic_expectation"] = _semantic_expectation(
            semantic_case, index
        )
        if not require_exact_id and normalized_case["semantic_expectation"] is None:
            raise ValueError(f"case {index} lacks semantic gold")
        normalized.append(normalized_case)
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


def provider_request(
    provider: str,
    case: dict[str, Any],
    *,
    base_url: str,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Build one request without performing a status probe or network call."""
    if provider == "overture":
        path = request_path(case, limit=limit)
        return {
            "url": f"{base_url.rstrip('/')}{path}",
            "path": path,
            "params": None,
            "headers": {"User-Agent": USER_AGENT},
            "capability": {"type_equivalence": "exact"},
        }
    if provider == "nominatim":
        return {
            "url": f"{base_url.rstrip('/')}/reverse",
            "path": "/reverse",
            "params": {
                "lat": case["latitude"],
                "lon": case["longitude"],
                "format": "jsonv2",
                "addressdetails": 1,
                "layer": "poi" if case["family"] == "places" else "address",
            },
            "headers": {"User-Agent": USER_AGENT},
            "capability": {
                "type_equivalence": "layer-filtered",
                "result_cardinality": "single",
                "limitation": (
                    "Nominatim reverse returns exactly one result, so "
                    "quality@5 is identical to quality@1."
                ),
            },
        }
    if provider == "photon":
        params: dict[str, Any] = {
            "lat": case["latitude"],
            "lon": case["longitude"],
            "radius": case["radius_m"] / 1000,
            "limit": limit,
        }
        capability = {"type_equivalence": "generic-reverse"}
        if case["family"] == "addresses":
            params["layer"] = ["house", "street"]
            capability = {"type_equivalence": "house-or-street-layer"}
        else:
            capability["limitation"] = (
                "Photon has no portable generic POI layer equivalent; results "
                "are scored semantically from its unfiltered reverse response."
            )
        return {
            "url": f"{base_url.rstrip('/')}/reverse",
            "path": "/reverse",
            "params": params,
            "headers": {"User-Agent": USER_AGENT},
            "capability": capability,
        }
    raise ValueError(f"unsupported provider: {provider}")


def _coordinates(feature: dict[str, Any]) -> tuple[float, float] | None:
    geometry = feature.get("geometry")
    coordinates = (
        geometry.get("coordinates") if isinstance(geometry, dict) else None
    )
    if (
        isinstance(coordinates, list)
        and len(coordinates) >= 2
        and not isinstance(coordinates[0], bool)
        and isinstance(coordinates[0], (int, float))
        and not isinstance(coordinates[1], bool)
        and isinstance(coordinates[1], (int, float))
    ):
        return float(coordinates[0]), float(coordinates[1])
    return None


def _address_values(properties: dict[str, Any]) -> dict[str, list[str]]:
    address = properties.get("address")
    address = address if isinstance(address, dict) else properties

    def values(*keys: str) -> list[str]:
        return [
            str(address[key])
            for key in keys
            if isinstance(address.get(key), (str, int)) and str(address[key]).strip()
        ]

    levels = properties.get("address_levels")
    levels = levels if isinstance(levels, list) else []
    return {
        "number": values("number", "house_number", "housenumber"),
        "unit": values("unit"),
        "street": values("street", "road", "pedestrian"),
        "postcode": values("postcode"),
        "locality": values(
            "postal_city",
            "city",
            "town",
            "village",
            "municipality",
            "hamlet",
            "suburb",
            "district",
        )
        + [str(value) for value in levels[-1:] if value],
        "region": values("region", "state", "county", "state_district")
        + [str(value) for value in levels if value],
        "country": values("display_country", "country"),
        "country_code": values("country_code", "countrycode", "display_country"),
    }


def normalize_provider_response(
    provider: str, body: Any
) -> tuple[bool, list[dict[str, Any]]]:
    """Normalize result fields used by semantic scoring, never provider IDs."""
    if provider == "nominatim":
        raw_features = [body] if isinstance(body, dict) and "error" not in body else None
    else:
        raw_features = body.get("features") if isinstance(body, dict) else None
    if not isinstance(raw_features, list):
        return False, []

    normalized = []
    for feature in raw_features:
        if not isinstance(feature, dict):
            continue
        if provider == "nominatim":
            properties = feature
            try:
                coordinates = (float(feature["lon"]), float(feature["lat"]))
            except (KeyError, TypeError, ValueError):
                coordinates = None
            names = [feature.get("name"), feature.get("display_name", "").split(",")[0]]
            categories = [feature.get("category"), feature.get("type")]
        else:
            properties = feature.get("properties")
            properties = properties if isinstance(properties, dict) else {}
            coordinates = _coordinates(feature)
            names = [properties.get("name")]
            categories = [
                properties.get("category"),
                properties.get("osm_key"),
                properties.get("osm_value"),
            ]
        normalized.append(
            {
                "coordinates": coordinates,
                "names": [value for value in names if isinstance(value, str) and value],
                "categories": [
                    value
                    for value in categories
                    if isinstance(value, str) and value
                ],
                "address": _address_values(properties),
            }
        )
    return True, normalized


def score_semantic_response(
    case: dict[str, Any], status: int | None, body: Any, error: str | None, provider: str
) -> dict[str, Any]:
    expected = case.get("semantic_expectation")
    if expected is None:
        return {
            "scorable": False,
            "unscorable_reason": (
                "case lacks expected_name/alt_names or expected_address semantic gold"
            ),
            "valid_response": False,
            "rank": None,
            "quality_at_1": False,
            "quality_at_5": False,
            "reciprocal_rank": 0.0,
            "features": 0,
            "top_distance_m": None,
            "top_within_tolerance": False,
            "top_component_accuracy": None,
        }
    shape_valid, features = normalize_provider_response(provider, body)
    valid = (
        error is None
        and status is not None
        and 200 <= status < 300
        and shape_valid
    )
    if not valid:
        features = []
    expected_names = {normalize_text(value) for value in expected["names"]}
    expected_address = {
        key: {normalize_text(value) for value in values}
        for key, values in expected["address"].items()
    }
    candidate_scores = []
    for feature in features:
        coordinates = feature["coordinates"]
        distance_m = (
            haversine_m(
                expected["latitude"],
                expected["longitude"],
                coordinates[1],
                coordinates[0],
            )
            if coordinates is not None
            else None
        )
        within = distance_m is not None and distance_m <= expected["tolerance_m"]
        if case["family"] == "places":
            semantic_match = bool(
                expected_names
                & {normalize_text(value) for value in feature["names"]}
            )
            component_accuracy = None
        else:
            matches = {
                key: bool(
                    accepted
                    & {normalize_text(value) for value in feature["address"].get(key, [])}
                )
                for key, accepted in expected_address.items()
            }
            component_accuracy = (
                sum(matches.values()) / len(matches) if matches else None
            )
            semantic_match = bool(matches) and all(matches.values())
        candidate_scores.append(
            {
                "match": semantic_match and within,
                "distance_m": distance_m,
                "within": within,
                "component_accuracy": component_accuracy,
            }
        )
    rank = next(
        (index + 1 for index, value in enumerate(candidate_scores) if value["match"]),
        None,
    )
    top = candidate_scores[0] if candidate_scores else {}
    return {
        "scorable": True,
        "unscorable_reason": None,
        "valid_response": valid,
        "rank": rank,
        "quality_at_1": rank == 1,
        "quality_at_5": rank is not None and rank <= 5,
        "reciprocal_rank": 1.0 / rank if rank else 0.0,
        "features": len(features),
        "top_distance_m": top.get("distance_m"),
        "top_within_tolerance": top.get("within", False),
        "top_component_accuracy": top.get("component_accuracy"),
    }


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
    supported_rows = [row for row in rows if row.get("supported", True)]
    scorable_rows = [
        row for row in supported_rows if row.get("scorable", True)
    ]
    client_ms = [
        row["client_ms"]
        for row in rows
        if isinstance(row.get("client_ms"), (int, float))
    ]
    worker_ms = [
        row["worker_ms"] for row in rows if row.get("worker_ms") is not None
    ]
    valid = sum(row["valid_response"] for row in scorable_rows)
    result = {
        "n": total,
        "supported": len(supported_rows),
        "unsupported": total - len(supported_rows),
        "scorable": len(scorable_rows),
        "unscorable": len(supported_rows) - len(scorable_rows),
        "valid_responses": valid,
        "errors": len(scorable_rows) - valid,
        "client_ms": {
            "n": len(client_ms),
            "p50": statistics.median(client_ms) if client_ms else None,
            "p95": percentile(client_ms, 0.95),
            "min": min(client_ms) if client_ms else None,
            "max": max(client_ms) if client_ms else None,
        },
        "worker_ms": {
            "n": len(worker_ms),
            "p50": statistics.median(worker_ms) if worker_ms else None,
            "p95": percentile(worker_ms, 0.95),
        },
    }
    denominator = len(scorable_rows)
    if any("recall_at_1" in row for row in scorable_rows):
        result.update(
            {
                "recall_at_1": sum(
                    row.get("recall_at_1", False) for row in scorable_rows
                )
                / denominator,
                "recall_at_5": sum(
                    row.get("recall_at_5", False) for row in scorable_rows
                )
                / denominator,
                "mrr": sum(
                    row.get("reciprocal_rank", 0.0) for row in scorable_rows
                )
                / denominator,
            }
        )
    if any("quality_at_1" in row for row in scorable_rows):
        result.update(
            {
                "quality_at_1": sum(
                    row.get("quality_at_1", False) for row in scorable_rows
                )
                / denominator,
                "quality_at_5": sum(
                    row.get("quality_at_5", False) for row in scorable_rows
                )
                / denominator,
                "mrr": sum(
                    row.get("reciprocal_rank", 0.0) for row in scorable_rows
                )
                / denominator,
            }
        )
    return result


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {"all": rows}
    for row in rows:
        groups.setdefault(row["family"], []).append(row)
        groups.setdefault(f"{row['family']}/{row['phase']}", []).append(row)
        provider = row.get("provider", "overture")
        groups.setdefault(f"provider/{provider}", []).append(row)
        groups.setdefault(f"provider/{provider}/{row['family']}", []).append(row)
        groups.setdefault(
            f"provider/{provider}/{row['family']}/{row['phase']}", []
        ).append(row)
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
                "provider": "overture",
                "supported": True,
                "family": case["family"],
                "phase": phase,
                "repeat": repeat,
                "path": path,
                "capability": {"type_equivalence": "exact"},
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


def run_external_provider(
    *,
    provider: str,
    base_url: str,
    cases: list[dict[str, Any]],
    interval_s: float,
    timeout_s: float,
    session: Any = None,
) -> list[dict[str, Any]]:
    """Run one sequential external pass; legacy ID-only cases make no request."""
    client = session or requests.Session()
    rows = []
    effective_interval_s = max(
        interval_s, PROVIDER_MIN_INTERVAL_S[provider]
    )
    requested = False
    for case in cases:
        request = provider_request(provider, case, base_url=base_url)
        expected = case.get("semantic_expectation")
        if expected is None:
            scored = score_semantic_response(case, None, None, None, provider)
            rows.append(
                {
                    "case_id": case["id"],
                    "provider": provider,
                    "supported": True,
                    "family": case["family"],
                    "phase": "external",
                    "repeat": 0,
                    "path": request["path"],
                    "capability": request["capability"],
                    "status": None,
                    "error": None,
                    "client_ms": None,
                    "worker_ms": None,
                    **scored,
                }
            )
            continue
        if requested and effective_interval_s:
            time.sleep(effective_interval_s)
        started = time.perf_counter()
        status = None
        body = None
        error = None
        headers: dict[str, str] = {}
        try:
            kwargs: dict[str, Any] = {
                "timeout": timeout_s,
                "headers": request["headers"],
            }
            if request["params"] is not None:
                kwargs["params"] = request["params"]
            response = client.get(request["url"], **kwargs)
            requested = True
            status = response.status_code
            headers = response.headers
            try:
                body = response.json()
            except ValueError:
                error = "response is not JSON"
        except requests.RequestException as exception:
            requested = True
            error = str(exception)
        client_ms = (time.perf_counter() - started) * 1000
        scored = score_semantic_response(case, status, body, error, provider)
        row = {
            "case_id": case["id"],
            "provider": provider,
            "supported": True,
            "family": case["family"],
            "phase": "external",
            "repeat": 0,
            "path": request["path"],
            "capability": request["capability"],
            "status": status,
            "error": error,
            "client_ms": client_ms,
            "worker_ms": server_timing_ms(headers.get("Server-Timing")),
            **scored,
        }
        rows.append(row)
        print(
            f"{provider:>10} {case['family']:<9} {case['id']:<28} "
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
    parser.add_argument("--nominatim-url", default=DEFAULT_NOMINATIM_URL)
    parser.add_argument("--photon-url", default=DEFAULT_PHOTON_URL)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument(
        "--provider",
        action="append",
        choices=PROVIDERS,
        help=(
            "Provider to run (repeatable). Defaults to Overture exact-ID "
            "self-recall. Selecting Nominatim or Photon switches all selected "
            "providers to one provider-neutral semantic pass."
        ),
    )
    parser.add_argument("--warm-repeats", type=int, default=3)
    parser.add_argument("--interval", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--assert-gates", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.warm_repeats < 0 or args.interval < 0 or args.timeout <= 0:
        parser.error("warm-repeats/interval must be non-negative and timeout positive")
    providers = list(dict.fromkeys(args.provider or ["overture"]))
    comparison_mode = any(provider != "overture" for provider in providers)
    provider_urls = {
        "overture": args.base_url,
        "nominatim": args.nominatim_url,
        "photon": args.photon_url,
    }
    if comparison_mode and args.assert_gates:
        parser.error("--assert-gates applies only to Overture exact-ID self-recall")

    try:
        cases = validate_cases(
            json.loads(args.cases.read_text()),
            require_exact_id=not comparison_mode,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    if comparison_mode:
        rows = []
        for provider in providers:
            rows.extend(
                run_external_provider(
                    provider=provider,
                    base_url=provider_urls[provider],
                    cases=cases,
                    interval_s=args.interval,
                    timeout_s=args.timeout,
                )
            )
    else:
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
        "providers": providers,
        "provider_urls": {
            provider: provider_urls[provider] for provider in providers
        },
        "benchmark_mode": (
            "provider_neutral_semantic" if comparison_mode else "exact_id_self_recall"
        ),
        "warm_repeats": args.warm_repeats,
        "summary": summary,
        "rows": rows,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.output:
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    failures = gate_failures(summary) if not comparison_mode else []
    if args.assert_gates and failures:
        print("reverse benchmark gates failed: " + "; ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
