#!/usr/bin/env python3
"""Collect the frozen Singapore/Taiwan everyday-POI benchmark batch."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "benchmarks/everyday-poi-source-snapshots-v1.json"
DEFAULT_SOURCE_DATA = ROOT / "benchmarks/everyday-poi-source-data-v1"
SCHEMA = "benchmark-v2-forward-cases-v1"
PROVIDERS = ["overture", "nominatim", "photon"]
TAINAN_CORE_DISTRICTS = {"中西區", "東區", "北區", "南區", "安平區"}


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def selection_digest(source_plan_id: str, source_record_id: str) -> str:
    return hashlib.sha256(
        f"{source_plan_id}\0{source_record_id}".encode()
    ).hexdigest()


def natural_exit_key(value: object) -> tuple:
    parts = re.split(r"(\d+)", str(value or "").casefold())
    return tuple(int(part) if part.isdigit() else part for part in parts)


def valid_point(coordinates: object) -> bool:
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return False
    try:
        longitude, latitude = float(coordinates[0]), float(coordinates[1])
    except (TypeError, ValueError):
        return False
    return -180 <= longitude <= 180 and -90 <= latitude <= 90


def source_by_id(manifest: dict, source_id: str) -> dict:
    matches = [item for item in manifest["sources"] if item.get("id") == source_id]
    if len(matches) != 1:
        raise ValueError(f"manifest must contain one {source_id} source")
    return matches[0]


def verify_snapshot(source: dict, path: Path) -> None:
    actual = sha256_file(path)
    if actual != source.get("snapshot_sha256"):
        raise ValueError(
            f"{source['id']} snapshot SHA-256 differs: {actual} != "
            f"{source.get('snapshot_sha256')}"
        )


def provenance(source: dict, record_id: str, selection_method: str) -> dict:
    return {
        "accessed_at": source["accessed_at"],
        "osm_derived": False,
        "selection_method": selection_method,
        "source_kind": "government",
        "source_license": source["source_license"],
        "source_name": source["source_name"],
        "source_record_id": record_id,
        "source_snapshot_sha256": source["snapshot_sha256"],
        "source_url": source["source_url"],
    }


def case(
    *,
    case_id: str,
    name: str,
    latitude: float,
    longitude: float,
    country: str,
    macroregion: str,
    family: str,
    script: str,
    source: dict,
    record_id: str,
    selection_method: str,
) -> dict:
    return {
        "comparison_providers": PROVIDERS,
        "expected_feature_type": "poi",
        "expected_lat": latitude,
        "expected_lon": longitude,
        "expected_name": name,
        "id": case_id,
        "kind": "place",
        "provenance": provenance(source, record_id, selection_method),
        "query": name,
        "query_style": "named_poi",
        "selection_review": {
            "decision": "accepted",
            "reason": (
                "current official source record has a stable identifier, a specific "
                "name, and a non-duplicated point in the frozen dense-city filter"
            ),
        },
        "strata": {
            "country": country,
            "density": "dense_urban",
            "macroregion": macroregion,
            "poi_family": family,
            "scope": "everyday",
            "script": script,
        },
        "tolerance_km": 1.0,
    }


def collect_singapore(path: Path, source: dict, quota: int = 20) -> tuple[list, dict]:
    payload = json.loads(path.read_bytes())
    features = payload.get("features")
    if payload.get("type") != "FeatureCollection" or not isinstance(features, list):
        raise ValueError("Singapore snapshot is not a GeoJSON FeatureCollection")
    stations: dict[str, list[dict]] = defaultdict(list)
    filter_counts = Counter()
    for feature in features:
        properties = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        station = properties.get("STATION_NA")
        record_id = properties.get("OBJECTID")
        if not isinstance(station, str) or not station.strip():
            filter_counts["missing_station_name"] += 1
        elif record_id in (None, ""):
            filter_counts["missing_object_id"] += 1
        elif not str(properties.get("EXIT_CODE") or "").strip():
            filter_counts["missing_exit_code"] += 1
        elif geometry.get("type") != "Point" or not valid_point(
            geometry.get("coordinates")
        ):
            filter_counts["invalid_point"] += 1
        else:
            stations[station.strip()].append(feature)
    collapsed = []
    for station_name, exits in stations.items():
        exits.sort(
            key=lambda item: (
                natural_exit_key((item.get("properties") or {}).get("EXIT_CODE")),
                str((item.get("properties") or {}).get("OBJECTID")),
            )
        )
        collapsed.append((station_name, exits[0]))
        filter_counts["collapsed_additional_station_exit"] += len(exits) - 1
    collapsed.sort(
        key=lambda item: selection_digest(
            source["id"], str(item[1]["properties"]["OBJECTID"])
        )
    )
    if len(collapsed) < quota:
        raise ValueError(f"Singapore has only {len(collapsed)} eligible stations")
    cases = []
    for rank, (name, feature) in enumerate(collapsed[:quota], 1):
        properties = feature["properties"]
        longitude, latitude = feature["geometry"]["coordinates"][:2]
        record_id = str(properties["OBJECTID"])
        cases.append(
            case(
                case_id=f"everyday-sg-{record_id}",
                name=name,
                latitude=float(latitude),
                longitude=float(longitude),
                country="SG",
                macroregion="southeast_asia",
                family="civic_transit",
                script="latin",
                source=source,
                record_id=record_id,
                selection_method=(
                    "collapse by STATION_NA; choose natural-lowest EXIT_CODE then "
                    "OBJECTID; rank unique stations by sha256(source_plan_id + NUL "
                    f"+ source_record_id); selection rank {rank}"
                ),
            )
        )
    return cases, {
        "eligible_unique_stations": len(collapsed),
        "input_features": len(features),
        "filter_counts": dict(sorted(filter_counts.items())),
        "review_exclusions": [],
        "selected": len(cases),
    }


def normalized_name(value: str) -> str:
    return " ".join(value.casefold().split())


def collect_taiwan(path: Path, source: dict, quota: int = 30) -> tuple[list, dict]:
    with zipfile.ZipFile(path) as archive:
        restaurants = json.loads(archive.read("RestaurantList.json"))["Restaurants"]
    eligible = []
    filter_counts = Counter()
    for restaurant in restaurants:
        address = restaurant.get("PostalAddress") or {}
        name = restaurant.get("RestaurantName")
        coordinates = [restaurant.get("PositionLon"), restaurant.get("PositionLat")]
        if address.get("City") != "臺南市":
            filter_counts["outside_tainan"] += 1
        elif address.get("Town") not in TAINAN_CORE_DISTRICTS:
            filter_counts["outside_tainan_core_districts"] += 1
        elif restaurant.get("ServiceStatus") != 1:
            filter_counts["not_active"] += 1
        elif not isinstance(name, str) or not name.strip():
            filter_counts["missing_name"] += 1
        elif not restaurant.get("RestaurantID"):
            filter_counts["missing_restaurant_id"] += 1
        elif not valid_point(coordinates):
            filter_counts["invalid_point"] += 1
        else:
            eligible.append(restaurant)
    name_counts = Counter(normalized_name(item["RestaurantName"]) for item in eligible)
    unambiguous = []
    review_exclusions = []
    for restaurant in eligible:
        if name_counts[normalized_name(restaurant["RestaurantName"])] > 1:
            review_exclusions.append(
                {
                    "reason": "duplicate_official_name",
                    "source_record_id": restaurant["RestaurantID"],
                }
            )
        else:
            unambiguous.append(restaurant)
    unambiguous.sort(
        key=lambda item: selection_digest(source["id"], item["RestaurantID"])
    )
    if len(unambiguous) < quota:
        raise ValueError(f"Taiwan has only {len(unambiguous)} eligible restaurants")
    cases = []
    for rank, restaurant in enumerate(unambiguous[:quota], 1):
        record_id = restaurant["RestaurantID"]
        cases.append(
            case(
                case_id=f"everyday-tw-{record_id}",
                name=restaurant["RestaurantName"].strip(),
                latitude=float(restaurant["PositionLat"]),
                longitude=float(restaurant["PositionLon"]),
                country="TW",
                macroregion="east_asia",
                family="food_drink",
                script="non_latin",
                source=source,
                record_id=record_id,
                selection_method=(
                    "active named point in Tainan core districts; exclude duplicate "
                    "official names; rank by sha256(source_plan_id + NUL + "
                    f"source_record_id); selection rank {rank}"
                ),
            )
        )
    return cases, {
        "eligible_after_duplicate_filter": len(unambiguous),
        "eligible_before_duplicate_filter": len(eligible),
        "input_restaurants": len(restaurants),
        "filter_counts": dict(sorted(filter_counts.items())),
        "review_exclusions": review_exclusions,
        "selected": len(cases),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--singapore",
        type=Path,
        default=DEFAULT_SOURCE_DATA / "sg-mrt-exits.geojson",
    )
    parser.add_argument(
        "--taiwan",
        type=Path,
        default=DEFAULT_SOURCE_DATA / "tw-restaurants.zip",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest_bytes = args.manifest.read_bytes()
        manifest = json.loads(manifest_bytes)
        if manifest.get("schema") != "everyday-poi-source-snapshots-v1":
            raise ValueError("unsupported source snapshot manifest schema")
        singapore = source_by_id(manifest, "sg-lta-mrt-exits")
        taiwan = source_by_id(manifest, "tw-tourism-restaurants")
        verify_snapshot(singapore, args.singapore)
        verify_snapshot(taiwan, args.taiwan)
        sg_cases, sg_report = collect_singapore(args.singapore, singapore)
        tw_cases, tw_report = collect_taiwan(args.taiwan, taiwan)
        payload = {
            "schema": SCHEMA,
            "collection_status": "partial; 50 of 200 frozen before provider requests",
            "cases": sg_cases + tw_cases,
        }
        output_bytes = canonical_json(payload)
        report = {
            "schema": "everyday-poi-selection-report-v1",
            "case_output_sha256": hashlib.sha256(output_bytes).hexdigest(),
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "provider_requests_made_during_selection": 0,
            "sources": {
                "sg-lta-mrt-exits": sg_report,
                "tw-tourism-restaurants": tw_report,
            },
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(output_bytes)
        args.report.write_bytes(canonical_json(report))
    except (KeyError, OSError, ValueError, zipfile.BadZipFile) as exception:
        print(f"everyday POI collection failed: {exception}", file=sys.stderr)
        return 2
    print(f"collected {len(payload['cases'])} frozen cases; provider requests=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
