#!/usr/bin/env python3
"""Audit whether frozen everyday-POI gold has a nearby named OSM feature.

This is an OSM-presence control, not another ranked search benchmark. It asks
Overpass for named nodes, ways, and relations around each independent authority
coordinate, then compares OSM name-family tags locally. Exact matches are
evidence that an OSM-backed search miss is an indexing/query-surface failure;
fuzzy matches remain review candidates rather than accepted truth.
"""

import argparse
import hashlib
import json
import math
import re
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests


CASES_SCHEMA = "benchmark-v2-forward-cases-v1"
RESULTS_SCHEMA = "everyday-poi-overpass-presence-v1"
DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"
DEFAULT_USER_AGENT = (
    "OvertureGeocoderBenchmark/1.0 "
    "(github.com/brad-richardson/overture-geocoder)"
)
NAME_KEY_PATTERN = (
    "^(name|official_name|alt_name|short_name|loc_name|brand|operator)(:.*)?$"
)
NAME_KEYS = {"name", "official_name", "alt_name", "short_name", "loc_name",
             "brand", "operator"}
DETAIL_TAGS = {
    "amenity", "healthcare", "public_transport", "railway", "shop",
    "station", "tourism", "train", "tram",
}


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_name(value):
    value = unicodedata.normalize("NFKD", value).casefold()
    return "".join(
        char for char in value
        if char.isalnum() and not unicodedata.combining(char)
    )


def split_name_values(value):
    if not isinstance(value, str):
        return []
    return [part.strip() for part in value.split(";") if part.strip()]


def name_values(tags):
    values = []
    for key, value in tags.items():
        base = key.split(":", 1)[0]
        if base in NAME_KEYS:
            values.extend((key, part) for part in split_name_values(value))
    return values


def haversine_m(lat1, lon1, lat2, lon2):
    radius = 6_371_000.0
    lat1, lat2 = math.radians(lat1), math.radians(lat2)
    dlat = lat2 - lat1
    dlon = math.radians(lon2 - lon1)
    value = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def element_point(element):
    if element.get("type") == "node":
        lat, lon = element.get("lat"), element.get("lon")
    else:
        center = element.get("center") or {}
        lat, lon = center.get("lat"), center.get("lon")
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


def candidate_from_element(element):
    point = element_point(element)
    tags = element.get("tags") or {}
    if point is None or not name_values(tags):
        return None
    retained = {
        key: value for key, value in tags.items()
        if key.split(":", 1)[0] in NAME_KEYS or key in DETAIL_TAGS
    }
    return {
        "osm_type": element.get("type"),
        "osm_id": element.get("id"),
        "lat": point[0],
        "lon": point[1],
        "tags": retained,
    }


def plausible_family(tags, family):
    amenity = tags.get("amenity")
    if family == "civic_transit":
        return bool(
            tags.get("public_transport")
            or tags.get("railway")
            or amenity == "bus_station"
        )
    if family == "food_drink":
        return amenity in {
            "bar", "cafe", "fast_food", "food_court", "ice_cream", "pub",
            "restaurant",
        } or tags.get("shop") in {"bakery", "coffee", "confectionery", "tea"}
    if family == "healthcare":
        return bool(tags.get("healthcare")) or amenity in {
            "clinic", "dentist", "doctors", "hospital", "pharmacy",
        }
    if family == "lodging":
        return tags.get("tourism") in {
            "apartment", "guest_house", "hostel", "hotel", "motel",
        }
    if family == "retail":
        return tags.get("shop") not in {None, "no", "vacant"}
    return False


def similarity(left, right):
    left, right = normalize_name(left), normalize_name(right)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def score_candidate(case, candidate, radius_m):
    distance = haversine_m(
        case["expected_lat"], case["expected_lon"],
        candidate["lat"], candidate["lon"],
    )
    if distance > radius_m:
        return None
    accepted = [case["expected_name"], *case.get("alt_names", [])]
    names = name_values(candidate["tags"])
    exact = []
    best = (0.0, None, None)
    accepted_norm = {normalize_name(value) for value in accepted if value}
    for key, observed in names:
        if normalize_name(observed) in accepted_norm:
            exact.append({"tag": key, "value": observed})
        for expected in accepted:
            score = similarity(expected, observed)
            if score > best[0]:
                best = (score, key, observed)
    return {
        **candidate,
        "distance_m": round(distance, 1),
        "exact_name_matches": exact,
        "best_name_similarity": round(best[0], 3),
        "best_name_tag": best[1],
        "best_name_value": best[2],
        "family_plausible": plausible_family(
            candidate["tags"], case["strata"]["poi_family"]),
    }


def build_query(cases, radius_m, timeout_s):
    clauses = [
        (
            f'nwr(around:{radius_m},{case["expected_lat"]},'
            f'{case["expected_lon"]})[~"{NAME_KEY_PATTERN}"~"."];'
        )
        for case in cases
    ]
    return (
        f"[out:json][timeout:{timeout_s}];\n(\n"
        + "\n".join(clauses)
        + "\n);\nout tags center qt;\n"
    )


def fetch_batch(session, endpoint, query, user_agent, timeout_s, retries=3):
    for attempt in range(retries):
        try:
            response = session.post(
                endpoint,
                data={"data": query},
                headers={"User-Agent": user_agent},
                timeout=timeout_s + 30,
            )
            if response.status_code in {429, 502, 503, 504} and attempt + 1 < retries:
                time.sleep(5 * (attempt + 1))
                continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
            if attempt + 1 == retries:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


def classify_case(case, candidates, radius_m, fuzzy_threshold):
    scored = [
        value for candidate in candidates
        if (value := score_candidate(case, candidate, radius_m)) is not None
    ]
    exact = sorted(
        (value for value in scored if value["exact_name_matches"]),
        key=lambda value: value["distance_m"],
    )
    fuzzy = sorted(
        (
            value for value in scored
            if not value["exact_name_matches"]
            and value["best_name_similarity"] >= fuzzy_threshold
        ),
        key=lambda value: (-value["best_name_similarity"], value["distance_m"]),
    )
    plausible = sorted(
        (
            value for value in scored
            if not value["exact_name_matches"] and value["family_plausible"]
        ),
        key=lambda value: (-value["best_name_similarity"], value["distance_m"]),
    )
    if exact:
        status = "exact_name_present"
    elif fuzzy:
        status = "fuzzy_name_candidate"
    elif plausible:
        status = "plausible_family_nearby_without_name_match"
    else:
        status = "no_named_family_candidate"
    return {
        "case_id": case["id"],
        "expected_name": case["expected_name"],
        "expected_lat": case["expected_lat"],
        "expected_lon": case["expected_lon"],
        "strata": case["strata"],
        "source_name": case["provenance"]["source_name"],
        "source_record_id": case["provenance"]["source_record_id"],
        "status": status,
        "named_candidates_within_radius": len(scored),
        "exact_matches": exact,
        "fuzzy_candidates": fuzzy[:5],
        "plausible_family_candidates": plausible[:5],
    }


def baseline_hits(path):
    if path is None:
        return {}, None
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    hits = {}
    for row in payload["results"]:
        hits.setdefault(row["case_id"], {})[row["provider"]] = row["found_at_10"]
    return hits, payload.get("meta", {}).get("data_version")


def summarize(results, hits):
    statuses = {}
    families = {}
    for result in results:
        statuses[result["status"]] = statuses.get(result["status"], 0) + 1
        family = result["strata"]["poi_family"]
        family_summary = families.setdefault(family, {"n": 0})
        family_summary["n"] += 1
        family_summary[result["status"]] = family_summary.get(result["status"], 0) + 1
    providers = {}
    for provider in sorted({key for values in hits.values() for key in values}):
        provider_summary = {
            "hit_and_exact_osm_name_present": 0,
            "hit_without_exact_osm_name_match": 0,
            "miss_and_exact_osm_name_present": 0,
            "miss_and_fuzzy_osm_name_candidate": 0,
            "miss_without_osm_name_match": 0,
        }
        for result in results:
            hit = hits.get(result["case_id"], {}).get(provider)
            if hit is None:
                continue
            exact = result["status"] == "exact_name_present"
            fuzzy = result["status"] == "fuzzy_name_candidate"
            if hit and exact:
                key = "hit_and_exact_osm_name_present"
            elif hit:
                key = "hit_without_exact_osm_name_match"
            elif not hit and exact:
                key = "miss_and_exact_osm_name_present"
            elif not hit and fuzzy:
                key = "miss_and_fuzzy_osm_name_candidate"
            else:
                key = "miss_without_osm_name_match"
            provider_summary[key] += 1
        providers[provider] = provider_summary
    return {
        "cases": len(results),
        "by_status": dict(sorted(statuses.items())),
        "by_family": dict(sorted(families.items())),
        "provider_cross_tab": providers,
    }


def run(args):
    cases_path = Path(args.cases)
    with open(cases_path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema") != CASES_SCHEMA:
        raise ValueError(f"{cases_path} is not a {CASES_SCHEMA} file")
    cases = payload["cases"]
    hits, baseline_version = baseline_hits(Path(args.baseline) if args.baseline else None)
    session = requests.Session()
    results = []
    osm_timestamps = []
    query_hashes = []
    query_count = 0
    for start in range(0, len(cases), args.batch_size):
        batch = cases[start:start + args.batch_size]
        query = build_query(batch, args.radius_m, args.query_timeout)
        query_hashes.append(hashlib.sha256(query.encode()).hexdigest())
        response = fetch_batch(
            session, args.endpoint, query, args.user_agent, args.query_timeout)
        query_count += 1
        timestamp = (response.get("osm3s") or {}).get("timestamp_osm_base")
        if timestamp:
            osm_timestamps.append(timestamp)
        candidates = [
            candidate for element in response.get("elements", [])
            if (candidate := candidate_from_element(element)) is not None
        ]
        results.extend(
            classify_case(case, candidates, args.radius_m, args.fuzzy_threshold)
            for case in batch
        )
        print(
            f"batch {start // args.batch_size + 1}: {len(batch)} cases, "
            f"{len(candidates)} named OSM elements"
        )
        if start + args.batch_size < len(cases):
            time.sleep(args.interval)
    output = {
        "schema": RESULTS_SCHEMA,
        "meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "endpoint": args.endpoint,
            "radius_m": args.radius_m,
            "batch_size": args.batch_size,
            "query_count": query_count,
            "query_timeout_s": args.query_timeout,
            "query_sha256": query_hashes,
            "osm_base_timestamps": sorted(set(osm_timestamps)),
            "cases": str(cases_path),
            "cases_sha256": sha256_file(cases_path),
            "baseline": args.baseline,
            "baseline_sha256": sha256_file(args.baseline) if args.baseline else None,
            "baseline_overture_data_version": baseline_version,
            "fuzzy_threshold": args.fuzzy_threshold,
            "license": "OpenStreetMap data © OpenStreetMap contributors, ODbL 1.0",
            "claim": (
                "presence control only; exact nearby OSM names are accepted, "
                "fuzzy candidates require review"
            ),
        },
        "summary": summarize(results, hits),
        "results": results,
    }
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    print(json.dumps(output["summary"], indent=1))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--output", required=True)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--radius-m", type=float, default=500.0)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--query-timeout", type=int, default=60)
    parser.add_argument("--fuzzy-threshold", type=float, default=0.86)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    args = parser.parse_args(argv)
    if args.radius_m <= 0 or args.batch_size <= 0 or args.interval < 0:
        parser.error("radius and batch size must be positive; interval cannot be negative")
    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
