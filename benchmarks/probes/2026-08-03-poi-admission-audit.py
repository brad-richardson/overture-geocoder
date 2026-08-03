#!/usr/bin/env python3
"""Classify curated POI misses across source, routing, and producer stages.

The benchmark says whether a query missed.  This probe asks the next question:
where did it miss?  For each landmark-admission or expanded everyday-POI case
it combines three bounded observations against the exact live source release:

* an Overture Parquet read clipped to the case's gold-radius bounding box;
* the deployed context-free query; and
* the same expected name through explicit-proximity forward lookup.

The source read is evidence about coverage and construction eligibility.  The
explicit-proximity control separates a global-head/locality-routing miss from a
record that cannot be retrieved even inside its one routed shard.  No query
scans unbounded source rows, and the script never mutates remote state.

Run:
  uv run --with 'duckdb==1.5.1' python3 \
    benchmarks/probes/2026-08-03-poi-admission-audit.py --output /tmp/audit.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
from pathlib import Path
import re
import time
import unicodedata
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import duckdb


ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = ROOT / "benchmarks/v2-forward-gold-cases-v1.json"
SOURCE_RELEASE = "2026-06-17.0"
SOURCE_URI = (
    "s3://overturemaps-us-west-2/release/"
    f"{SOURCE_RELEASE}/theme=places/type=place/*"
)
BASE_URL = "https://geocoder.bradr.dev"
USER_AGENT = "overture-geocoder-poi-admission-audit/1"

LANDMARK_CASE_IDS = {
    "gold:name:empire-state-building",
    "gold:name:big-ben",
    "gold:name:brandenburg-gate",
}
EVERYDAY_QUERY_STYLES = {"named_poi", "brand_branch"}


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TYPE_PRIOR = _load_module(ROOT / "scripts/places_type_prior_v1.py", "places_type_prior_audit")


def selected_cases() -> list[dict]:
    cases = json.loads(CASES_PATH.read_text())["cases"]
    selected = [
        case
        for case in cases
        if case["id"] in LANDMARK_CASE_IDS
        or case.get("query_style") in EVERYDAY_QUERY_STYLES
    ]
    expected = len(LANDMARK_CASE_IDS) + 20
    if len(selected) != expected:
        raise ValueError(f"expected {expected} audit cases, found {len(selected)}")
    return selected


def request_json(path: str, params: dict[str, object]) -> dict:
    url = f"{BASE_URL}{path}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in value if not unicodedata.combining(char))


def normalized_words(value: str) -> list[str]:
    return re.findall(r"\w+", normalize_name(value))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    value = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _feature_point(feature: dict) -> tuple[float, float] | None:
    geometry = feature.get("geometry")
    coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else []
    if not coordinates or len(coordinates) < 2:
        return None
    return float(coordinates[1]), float(coordinates[0])


def score(case: dict, body: dict) -> dict:
    features = body.get("features") if isinstance(body, dict) else []
    features = features if isinstance(features, list) else []
    accepted = {
        normalize_name(value)
        for value in [case.get("expected_name"), *case.get("alt_names", [])]
        if value
    }
    rank = distance = None
    expected = float(case["expected_lat"]), float(case["expected_lon"])
    for index, feature in enumerate(features):
        properties = feature.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        observed = properties.get("name")
        point = _feature_point(feature)
        if not isinstance(observed, str) or point is None:
            continue
        candidate = normalize_name(observed.split(",")[0])
        candidate_distance = haversine_km(*expected, *point)
        if candidate in accepted and candidate_distance <= case.get("tolerance_km", 50.0):
            rank = index + 1
            distance = candidate_distance
            break
    top1_point = _feature_point(features[0]) if features else None
    top1_distance = haversine_km(*expected, *top1_point) if top1_point else None
    return {
        "rank": rank,
        "result_count": len(features),
        "matched_distance_km": None if distance is None else round(distance, 3),
        "top1_distance_km": (
            None if top1_distance is None else round(top1_distance, 3)
        ),
        "metadata": body.get("metadata", {}),
        "top_names": [
            feature.get("properties", {}).get("name") for feature in features[:3]
        ],
    }


def live_controls(case: dict, interval_s: float) -> dict:
    common = {"types": "poi", "limit": 10}
    controls = {}
    requests = (
        ("original", {**common, "q": case["query"]}),
        (
            "expected_name_with_proximity",
            {
                **common,
                "q": case["expected_name"],
                "proximity": f'{case["expected_lon"]},{case["expected_lat"]}',
            },
        ),
        (
            "original_with_proximity",
            {
                **common,
                "q": case["query"],
                "proximity": f'{case["expected_lon"]},{case["expected_lat"]}',
            },
        ),
    )
    for index, (name, params) in enumerate(requests):
        if index:
            time.sleep(interval_s)
        controls[name] = score(case, request_json("/v2/forward", params))
    controls["accepted_names_with_proximity"] = []
    accepted_names = [case["expected_name"], *case.get("alt_names", [])]
    for query in dict.fromkeys(accepted_names):
        time.sleep(interval_s)
        result = score(
            case,
            request_json(
                "/v2/forward",
                {
                    **common,
                    "q": query,
                    "proximity": f'{case["expected_lon"]},{case["expected_lat"]}',
                },
            ),
        )
        controls["accepted_names_with_proximity"].append(
            {"query": query, **result}
        )
    return controls


def source_rows(con: duckdb.DuckDBPyConnection, case: dict) -> list[dict]:
    # One degree of longitude narrows with latitude.  The padding is twice the
    # scoring radius (minimum 1 km), so a source hit at the gold tolerance
    # cannot sit outside the read box due to the degree conversion.
    radius_km = max(1.0, float(case.get("tolerance_km", 1.0))) * 2.0
    lat = float(case["expected_lat"])
    lon = float(case["expected_lon"])
    lat_pad = radius_km / 110.574
    lon_pad = radius_km / max(1.0, 111.320 * math.cos(math.radians(lat)))
    result = con.execute(
        f"""
        SELECT
            id::VARCHAR AS id,
            names.primary AS primary_name,
            brand.names.primary AS brand_name,
            categories.primary AS category,
            categories.alternate AS alternate_categories,
            basic_category,
            operating_status,
            confidence,
            bbox.xmin AS longitude,
            bbox.ymin AS latitude
        FROM read_parquet('{SOURCE_URI}', hive_partitioning=true)
        WHERE bbox.xmin BETWEEN ? AND ?
          AND bbox.ymin BETWEEN ? AND ?
          AND names.primary IS NOT NULL
        """,
        [lon - lon_pad, lon + lon_pad, lat - lat_pad, lat + lat_pad],
    )
    columns = [item[0] for item in result.description]
    rows = [dict(zip(columns, values, strict=True)) for values in result.fetchall()]
    for row in rows:
        row["distance_km"] = round(
            haversine_km(
                lat, lon, float(row["latitude"]), float(row["longitude"])
            ),
            3,
        )
        row["prominence_rank"] = TYPE_PRIOR.prominence_rank(
            row["category"], row["basic_category"], None, row["alternate_categories"]
        )
    return rows


def source_matches(case: dict, rows: list[dict]) -> list[dict]:
    accepted = [case["expected_name"], *case.get("alt_names", [])]
    accepted = {normalize_name(value.split(",")[0]) for value in accepted}
    tolerance = float(case.get("tolerance_km", 1.0))
    matches = []
    for row in rows:
        observed = {
            normalize_name(str(value or "").split(",")[0])
            for value in (row["primary_name"], row["brand_name"])
            if value
        }
        if accepted & observed and row["distance_km"] <= tolerance:
            matches.append(row)
    return sorted(matches, key=lambda row: (row["distance_km"], row["id"]))


def classify(case: dict, matches: list[dict], controls: dict) -> str:
    if controls["original"]["rank"] is not None:
        return "retrieved"
    if not matches:
        return "source_coverage_or_name_contract"
    if controls["original_with_proximity"]["rank"] is not None:
        return "global_head_or_locality_routing"
    if any(
        item["rank"] is not None
        for item in controls["accepted_names_with_proximity"]
    ):
        return "query_context_token_or_locality_routing"
    return "routed_forward_admission_or_tokenization"


def audit_case(con: duckdb.DuckDBPyConnection, case: dict, interval_s: float) -> dict:
    rows = source_rows(con, case)
    matches = source_matches(case, rows)
    controls = live_controls(case, interval_s)
    query_words = normalized_words(case["query"])
    phrase_candidates = [
        row["id"]
        for row in matches
        if row["prominence_rank"] > 0
        and 2 <= len(query_words) <= 3
        and normalized_words(row["primary_name"]) == query_words
    ]
    return {
        "case_id": case["id"],
        "query": case["query"],
        "query_style": case["query_style"],
        "strata": case.get("strata", {}),
        "source_bbox_rows": len(rows),
        "source_matches": matches,
        "entity_phrase_candidate_ids": phrase_candidates,
        "live_controls": controls,
        "classification": classify(case, matches, controls),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--only-case", action="append", default=[])
    args = parser.parse_args()

    cases = selected_cases()
    if args.only_case:
        wanted = set(args.only_case)
        cases = [case for case in cases if case["id"] in wanted]
        missing = wanted - {case["id"] for case in cases}
        if missing:
            raise ValueError(f"unknown or unselected case ids: {sorted(missing)}")

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2'")
    audited = []
    for index, case in enumerate(cases, 1):
        print(f'[{index}/{len(cases)}] {case["id"]}', flush=True)
        audited.append(audit_case(con, case, args.interval))

    counts: dict[str, int] = {}
    for item in audited:
        key = item["classification"]
        counts[key] = counts.get(key, 0) + 1
    evidence = {
        "schema": "overture-poi-admission-audit-v1",
        "source_release": SOURCE_RELEASE,
        "endpoint": BASE_URL,
        "case_count": len(audited),
        "classification_counts": dict(sorted(counts.items())),
        "cases": audited,
    }
    Path(args.output).write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps(evidence["classification_counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
