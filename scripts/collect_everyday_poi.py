#!/usr/bin/env python3
"""Collect frozen everyday-POI cases from retained official source bytes."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import io
import json
import math
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
MELBOURNE_PHYSICAL_RETAIL_DIVISIONS = {"39", "40", "41", "42"}
MELBOURNE_GENERIC_NAMES = {"vacant", "not stated", "unknown", "n/a"}
MELBOURNE_REVIEW_EXCLUSIONS = {
    "931e86d2d331e0e390fc00c3891813a75d7608b3":
        "storage_only_not_public_retail",
}
JAPAN_REVIEW_EXCLUSIONS = {
    "131041900546": "temporary_vaccination_site_not_enduring_everyday_poi",
}
MEXICO_CITY_BOUNDS = (-99.35, -98.94, 19.20, 19.60)
MEXICO_REVIEW_EXCLUSIONS = {
    "933195": "source_name_is_external_attraction_not_lodging_facility",
}
MEXICO_DENUE_MEMBER = "conjunto_de_datos/denue_inegi_09_.csv"


class JavaScriptDataParser:
    """Parse the data-only JavaScript subset emitted by Seoul's sheet preview.

    The endpoint does not emit JSON: object keys are bare identifiers and its
    arrays may have trailing commas.  Executing or evaluating a government
    response would make collection unsafe, so this deliberately small parser
    accepts only objects, arrays, JSON strings, finite numbers, and the three
    JSON literals.
    """

    _IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
    _NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")

    def __init__(self, value: str):
        self.value = value
        self.position = 0

    def parse(self) -> object:
        result = self._value()
        self._whitespace()
        if self.position != len(self.value):
            raise ValueError("unexpected content after JavaScript data value")
        return result

    def _whitespace(self) -> None:
        while self.position < len(self.value) and self.value[self.position].isspace():
            self.position += 1

    def _take(self, expected: str) -> None:
        self._whitespace()
        if not self.value.startswith(expected, self.position):
            raise ValueError(f"expected {expected!r} at byte {self.position}")
        self.position += len(expected)

    def _value(self) -> object:
        self._whitespace()
        if self.position >= len(self.value):
            raise ValueError("unexpected end of JavaScript data")
        character = self.value[self.position]
        if character == "{":
            return self._object()
        if character == "[":
            return self._array()
        if character == '"':
            try:
                result, end = json.JSONDecoder().raw_decode(
                    self.value, self.position
                )
            except json.JSONDecodeError as exception:
                raise ValueError("invalid JSON string in JavaScript data") from exception
            self.position = end
            return result
        for literal, result in (("true", True), ("false", False), ("null", None)):
            if self.value.startswith(literal, self.position):
                self.position += len(literal)
                return result
        match = self._NUMBER.match(self.value, self.position)
        if match:
            token = match.group()
            self.position = match.end()
            return float(token) if any(mark in token for mark in ".eE") else int(token)
        raise ValueError(f"unsupported JavaScript token at byte {self.position}")

    def _object(self) -> dict:
        self._take("{")
        result = {}
        self._whitespace()
        if self.position < len(self.value) and self.value[self.position] == "}":
            self.position += 1
            return result
        while True:
            self._whitespace()
            match = self._IDENTIFIER.match(self.value, self.position)
            if not match:
                raise ValueError(f"expected object key at byte {self.position}")
            key = match.group()
            if key in result:
                raise ValueError(f"duplicate JavaScript object key: {key}")
            self.position = match.end()
            self._take(":")
            result[key] = self._value()
            self._whitespace()
            if self.position >= len(self.value):
                raise ValueError("unterminated JavaScript object")
            character = self.value[self.position]
            self.position += 1
            if character == "}":
                return result
            if character != ",":
                raise ValueError(f"expected ',' or '}}' at byte {self.position - 1}")
            self._whitespace()
            if self.position < len(self.value) and self.value[self.position] == "}":
                self.position += 1
                return result

    def _array(self) -> list:
        self._take("[")
        result = []
        self._whitespace()
        if self.position < len(self.value) and self.value[self.position] == "]":
            self.position += 1
            return result
        while True:
            result.append(self._value())
            self._whitespace()
            if self.position >= len(self.value):
                raise ValueError("unterminated JavaScript array")
            character = self.value[self.position]
            self.position += 1
            if character == "]":
                return result
            if character != ",":
                raise ValueError(f"expected ',' or ']' at byte {self.position - 1}")
            self._whitespace()
            if self.position < len(self.value) and self.value[self.position] == "]":
                self.position += 1
                return result


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
    alt_names: list[str] | None = None,
    provenance_extra: dict | None = None,
) -> dict:
    case_provenance = provenance(source, record_id, selection_method)
    if provenance_extra:
        case_provenance.update(provenance_extra)
    result = {
        "comparison_providers": PROVIDERS,
        "expected_feature_type": "poi",
        "expected_lat": latitude,
        "expected_lon": longitude,
        "expected_name": name,
        "id": case_id,
        "kind": "place",
        "provenance": case_provenance,
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
    if alt_names:
        result["alt_names"] = alt_names
    return result


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


def contains_japanese_script(value: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff]", value))


def mexico_generic_name(value: str) -> bool:
    name = normalized_name(value)
    return (
        "sin nombre" in name
        or name.startswith("local de ")
        or name == "oficina administrativa"
    )


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


def collect_hong_kong(path: Path, source: dict, quota: int = 20) -> tuple[list, dict]:
    payload = json.loads(path.read_bytes())
    features = payload.get("features")
    if payload.get("type") != "FeatureCollection" or not isinstance(features, list):
        raise ValueError("Hong Kong snapshot is not a GeoJSON FeatureCollection")
    eligible = []
    filter_counts = Counter()
    for feature in features:
        properties = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates")
        if properties.get("OBJECTID") in (None, ""):
            filter_counts["missing_object_id"] += 1
        elif not str(properties.get("NAME_TC") or "").strip():
            filter_counts["missing_traditional_chinese_name"] += 1
        elif not str(properties.get("NAME_EN") or "").strip():
            filter_counts["missing_english_name"] += 1
        elif geometry.get("type") != "Point" or not valid_point(coordinates):
            filter_counts["invalid_point"] += 1
        else:
            eligible.append(feature)
    name_counts = Counter(
        normalized_name(item["properties"]["NAME_TC"]) for item in eligible
    )
    unambiguous = []
    review_exclusions = []
    for feature in eligible:
        properties = feature["properties"]
        if name_counts[normalized_name(properties["NAME_TC"])] > 1:
            review_exclusions.append(
                {
                    "reason": "duplicate_official_name",
                    "source_record_id": str(properties["OBJECTID"]),
                }
            )
        else:
            unambiguous.append(feature)
    unambiguous.sort(
        key=lambda item: selection_digest(
            source["id"], str(item["properties"]["OBJECTID"])
        )
    )
    if len(unambiguous) < quota:
        raise ValueError(
            f"Hong Kong has only {len(unambiguous)} eligible health facilities"
        )
    cases = []
    for rank, feature in enumerate(unambiguous[:quota], 1):
        properties = feature["properties"]
        longitude, latitude = feature["geometry"]["coordinates"][:2]
        record_id = str(properties["OBJECTID"])
        cases.append(
            case(
                case_id=f"everyday-hk-{record_id}",
                name=properties["NAME_TC"].strip(),
                alt_names=[properties["NAME_EN"].strip()],
                latitude=float(latitude),
                longitude=float(longitude),
                country="HK",
                macroregion="east_asia",
                family="healthcare",
                script="non_latin",
                source=source,
                record_id=record_id,
                selection_method=(
                    "named bilingual Hospital Authority point; exclude duplicate "
                    "Traditional Chinese names; rank by sha256(source_plan_id + NUL "
                    f"+ source_record_id); selection rank {rank}"
                ),
            )
        )
    return cases, {
        "eligible_after_duplicate_filter": len(unambiguous),
        "eligible_before_duplicate_filter": len(eligible),
        "input_features": len(features),
        "filter_counts": dict(sorted(filter_counts.items())),
        "review_exclusions": review_exclusions,
        "selected": len(cases),
    }


def collect_melbourne(path: Path, source: dict, quota: int = 25) -> tuple[list, dict]:
    records = json.loads(path.read_bytes())
    if not isinstance(records, list):
        raise ValueError("Melbourne snapshot is not a JSON array")
    record_ids = [str(item.get("recordid") or "") for item in records]
    if "" in record_ids or len(record_ids) != len(set(record_ids)):
        raise ValueError("Melbourne snapshot has missing or duplicate recordid values")
    eligible = []
    filter_counts = Counter()
    for record in records:
        fields = record.get("fields") or {}
        name = fields.get("trading_name")
        code = str(fields.get("industry_anzsic4_code") or "")
        coordinates = [fields.get("longitude"), fields.get("latitude")]
        if str(fields.get("census_year")) != "2024":
            filter_counts["outside_2024_census"] += 1
        elif code[:2] not in MELBOURNE_PHYSICAL_RETAIL_DIVISIONS:
            filter_counts["outside_physical_retail_divisions"] += 1
        elif not isinstance(name, str) or not name.strip():
            filter_counts["missing_name"] += 1
        elif normalized_name(name) in MELBOURNE_GENERIC_NAMES:
            filter_counts["generic_name"] += 1
        elif not valid_point(coordinates):
            filter_counts["invalid_point"] += 1
        else:
            eligible.append(record)
    name_counts = Counter(
        normalized_name(item["fields"]["trading_name"]) for item in eligible
    )
    unambiguous = []
    review_exclusions = []
    for record in eligible:
        if name_counts[normalized_name(record["fields"]["trading_name"])] > 1:
            review_exclusions.append(
                {
                    "reason": "duplicate_official_name",
                    "source_record_id": record["recordid"],
                }
            )
        else:
            unambiguous.append(record)
    unambiguous.sort(
        key=lambda item: selection_digest(source["id"], item["recordid"])
    )
    cases = []
    for review_position, record in enumerate(unambiguous, 1):
        record_id = record["recordid"]
        if record_id in MELBOURNE_REVIEW_EXCLUSIONS:
            review_exclusions.append(
                {
                    "reason": MELBOURNE_REVIEW_EXCLUSIONS[record_id],
                    "source_record_id": record_id,
                }
            )
            continue
        fields = record["fields"]
        selection_rank = len(cases) + 1
        cases.append(
            case(
                case_id=f"everyday-au-{record_id}",
                name=fields["trading_name"].strip(),
                latitude=float(fields["latitude"]),
                longitude=float(fields["longitude"]),
                country="AU",
                macroregion="oceania",
                family="retail",
                script="latin",
                source=source,
                record_id=record_id,
                selection_method=(
                    "2024 City of Melbourne establishment in physical-retail "
                    "ANZSIC divisions 39-42; exclude generic and duplicate official "
                    "names; review hash order and reject non-public retail records; "
                    "rank by sha256(source_plan_id + NUL + source_record_id); "
                    f"review position {review_position}; selection rank {selection_rank}"
                ),
                provenance_extra={
                    "business_address": fields.get("business_address"),
                    "census_year": fields["census_year"],
                    "industry_anzsic4_code": fields["industry_anzsic4_code"],
                    "industry_anzsic4_description": fields.get(
                        "industry_anzsic4_description"
                    ),
                    "property_id": fields.get("property_id"),
                },
            )
        )
        if len(cases) == quota:
            break
    if len(cases) < quota:
        raise ValueError(f"Melbourne has only {len(cases)} reviewed retail records")
    return cases, {
        "eligible_after_duplicate_filter": len(unambiguous),
        "eligible_before_duplicate_filter": len(eligible),
        "input_records": len(records),
        "filter_counts": dict(sorted(filter_counts.items())),
        "review_exclusions": review_exclusions,
        "selected": len(cases),
    }


def collect_bogota(path: Path, source: dict, quota: int = 20) -> tuple[list, dict]:
    payload = json.loads(path.read_bytes())
    features = payload.get("features")
    if payload.get("type") != "FeatureCollection" or not isinstance(features, list):
        raise ValueError("Bogota snapshot is not a GeoJSON FeatureCollection")
    source_ids = [
        str((item.get("properties") or {}).get("ID") or "")
        for item in features
    ]
    object_ids = [
        str((item.get("properties") or {}).get("OBJECTID") or "")
        for item in features
    ]
    if "" in source_ids or len(source_ids) != len(set(source_ids)):
        raise ValueError("Bogota snapshot has missing or duplicate ID values")
    if "" in object_ids or len(object_ids) != len(set(object_ids)):
        raise ValueError("Bogota snapshot has missing or duplicate OBJECTID values")
    eligible = []
    filter_counts = Counter()
    for feature in features:
        properties = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates")
        name = properties.get("NOMBRE_DEL")
        if properties.get("TIPO_DE_PR") != "Pública":
            filter_counts["not_public_provider"] += 1
        elif properties.get("CLASE_DE_P") != (
            "Instituciones Prestadoras de Servicios de Salud - IPS"
        ):
            filter_counts["not_healthcare_provider"] += 1
        elif not isinstance(name, str) or not name.strip():
            filter_counts["missing_name"] += 1
        elif geometry.get("type") != "Point" or not valid_point(coordinates):
            filter_counts["invalid_point"] += 1
        elif not (-74.3 <= float(coordinates[0]) <= -73.9):
            filter_counts["outside_bogota_bounds"] += 1
        elif not (4.3 <= float(coordinates[1]) <= 4.9):
            filter_counts["outside_bogota_bounds"] += 1
        else:
            eligible.append(feature)
    name_counts = Counter(
        normalized_name(item["properties"]["NOMBRE_DEL"]) for item in eligible
    )
    unambiguous = []
    review_exclusions = []
    for feature in eligible:
        properties = feature["properties"]
        if name_counts[normalized_name(properties["NOMBRE_DEL"])] > 1:
            review_exclusions.append(
                {
                    "reason": "duplicate_official_name",
                    "source_record_id": str(properties["ID"]),
                }
            )
        else:
            unambiguous.append(feature)
    unambiguous.sort(
        key=lambda item: selection_digest(
            source["id"], str(item["properties"]["ID"])
        )
    )
    if len(unambiguous) < quota:
        raise ValueError(f"Bogota has only {len(unambiguous)} eligible facilities")
    cases = []
    for rank, feature in enumerate(unambiguous[:quota], 1):
        properties = feature["properties"]
        longitude, latitude = feature["geometry"]["coordinates"][:2]
        record_id = str(properties["ID"])
        cases.append(
            case(
                case_id=f"everyday-co-{record_id}",
                name=properties["NOMBRE_DEL"].strip(),
                latitude=float(latitude),
                longitude=float(longitude),
                country="CO",
                macroregion="latin_america",
                family="healthcare",
                script="latin",
                source=source,
                record_id=record_id,
                selection_method=(
                    "named public IPS point in the Bogota district health network; "
                    "exclude duplicate official names; rank by "
                    "sha256(source_plan_id + NUL + source_record_id); "
                    f"selection rank {rank}"
                ),
                provenance_extra={
                    "address": properties.get("DIRECCION"),
                    "provider_name": properties.get("NOMBRE_DE_"),
                    "source_object_id": str(properties["OBJECTID"]),
                },
            )
        )
    return cases, {
        "eligible_after_duplicate_filter": len(unambiguous),
        "eligible_before_duplicate_filter": len(eligible),
        "input_features": len(features),
        "filter_counts": dict(sorted(filter_counts.items())),
        "review_exclusions": review_exclusions,
        "selected": len(cases),
    }


def collect_japan(path: Path, source: dict, quota: int = 30) -> tuple[list, dict]:
    with path.open(encoding="utf-16", newline="") as input_file:
        rows = list(csv.DictReader(input_file))
    record_ids = [str(row.get("ID") or "").strip() for row in rows]
    if "" in record_ids or len(record_ids) != len(set(record_ids)):
        raise ValueError("Japan snapshot has missing or duplicate ID values")
    eligible = []
    filter_counts = Counter()
    for row in rows:
        name = str(row.get("名称") or "").strip()
        coordinates = [row.get("経度"), row.get("緯度")]
        if str(row.get("地方公共団体名") or "").strip() != "新宿区":
            filter_counts["outside_shinjuku"] += 1
        elif not name:
            filter_counts["missing_name"] += 1
        elif not contains_japanese_script(name):
            filter_counts["name_has_no_japanese_script"] += 1
        elif not str(row.get("医療機関の種類") or "").strip():
            filter_counts["missing_facility_type"] += 1
        elif not valid_point(coordinates):
            filter_counts["invalid_point"] += 1
        elif not (139.67 <= float(coordinates[0]) <= 139.76):
            filter_counts["outside_shinjuku_bounds"] += 1
        elif not (35.67 <= float(coordinates[1]) <= 35.74):
            filter_counts["outside_shinjuku_bounds"] += 1
        else:
            eligible.append(row)
    name_counts = Counter(normalized_name(row["名称"]) for row in eligible)
    unambiguous = []
    review_exclusions = []
    for row in eligible:
        record_id = row["ID"].strip()
        if name_counts[normalized_name(row["名称"])] > 1:
            review_exclusions.append(
                {
                    "reason": "duplicate_official_name",
                    "source_record_id": record_id,
                }
            )
        else:
            unambiguous.append(row)
    unambiguous.sort(
        key=lambda row: selection_digest(source["id"], row["ID"].strip())
    )
    cases = []
    for review_position, row in enumerate(unambiguous, 1):
        record_id = row["ID"].strip()
        if record_id in JAPAN_REVIEW_EXCLUSIONS:
            review_exclusions.append(
                {
                    "reason": JAPAN_REVIEW_EXCLUSIONS[record_id],
                    "source_record_id": record_id,
                }
            )
            continue
        selection_rank = len(cases) + 1
        cases.append(
            case(
                case_id=f"everyday-jp-{record_id}",
                name=row["名称"].strip(),
                latitude=float(row["緯度"]),
                longitude=float(row["経度"]),
                country="JP",
                macroregion="east_asia",
                family="healthcare",
                script="non_latin",
                source=source,
                record_id=record_id,
                selection_method=(
                    "current named Shinjuku municipal-standard medical-facility "
                    "point with Japanese-script query surface; exclude duplicate "
                    "official names and temporary facilities; rank by "
                    "sha256(source_plan_id + NUL + source_record_id); "
                    f"review position {review_position}; selection rank {selection_rank}"
                ),
                provenance_extra={
                    "address": row.get("所在地_連結表記"),
                    "facility_type": row.get("医療機関の種類"),
                    "medical_subjects": row.get("診療科目"),
                    "municipality_code": row.get("全国地方公共団体コード"),
                },
            )
        )
        if len(cases) == quota:
            break
    if len(cases) < quota:
        raise ValueError(f"Japan has only {len(cases)} reviewed medical facilities")
    return cases, {
        "eligible_after_duplicate_filter": len(unambiguous),
        "eligible_before_duplicate_filter": len(eligible),
        "input_rows": len(rows),
        "filter_counts": dict(sorted(filter_counts.items())),
        "review_exclusions": review_exclusions,
        "selected": len(cases),
    }


def collect_mexico(
    path: Path,
    source: dict,
    lodging_quota: int = 20,
    retail_quota: int = 15,
) -> tuple[list, dict]:
    candidates = {"lodging": [], "retail": []}
    filter_counts = {"lodging": Counter(), "retail": Counter()}
    record_ids = set()
    clee_values = set()
    input_rows = 0
    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open(MEXICO_DENUE_MEMBER) as raw_input:
                input_file = io.TextIOWrapper(
                    raw_input, encoding="cp1252", newline=""
                )
                for row in csv.DictReader(input_file):
                    input_rows += 1
                    record_id = str(row.get("id") or "").strip()
                    clee = str(row.get("clee") or "").strip()
                    if not record_id or record_id in record_ids:
                        raise ValueError(
                            "Mexico snapshot has missing or duplicate DENUE IDs"
                        )
                    if not clee or clee in clee_values:
                        raise ValueError(
                            "Mexico snapshot has missing or duplicate CLEE values"
                        )
                    record_ids.add(record_id)
                    clee_values.add(clee)
                    activity_code = str(row.get("codigo_act") or "").strip()
                    if activity_code.startswith("721"):
                        family = "lodging"
                    elif activity_code.startswith("46"):
                        family = "retail"
                    else:
                        continue
                    name = str(row.get("nom_estab") or "").strip()
                    coordinates = [row.get("longitud"), row.get("latitud")]
                    if str(row.get("tipoUniEco") or "").strip() != "Fijo":
                        filter_counts[family]["not_fixed_establishment"] += 1
                    elif not name:
                        filter_counts[family]["missing_name"] += 1
                    elif mexico_generic_name(name):
                        filter_counts[family]["generic_activity_name"] += 1
                    elif not valid_point(coordinates):
                        filter_counts[family]["invalid_point"] += 1
                    elif not (
                        MEXICO_CITY_BOUNDS[0]
                        <= float(coordinates[0])
                        <= MEXICO_CITY_BOUNDS[1]
                    ):
                        filter_counts[family]["outside_mexico_city_bounds"] += 1
                    elif not (
                        MEXICO_CITY_BOUNDS[2]
                        <= float(coordinates[1])
                        <= MEXICO_CITY_BOUNDS[3]
                    ):
                        filter_counts[family]["outside_mexico_city_bounds"] += 1
                    else:
                        candidates[family].append(
                            {
                                "activity_code": activity_code,
                                "activity_name": row.get("nombre_act"),
                                "clee": clee,
                                "id": record_id,
                                "latitude": row.get("latitud"),
                                "longitude": row.get("longitud"),
                                "municipality": row.get("municipio"),
                                "name": name,
                                "settlement": row.get("nomb_asent"),
                                "source_added_date": row.get("fecha_alta"),
                            }
                        )
    except KeyError as exception:
        raise ValueError(
            f"Mexico snapshot is missing archive member {MEXICO_DENUE_MEMBER}"
        ) from exception

    selected_cases = []
    family_reports = {}
    for family, quota in (("lodging", lodging_quota), ("retail", retail_quota)):
        eligible = candidates[family]
        name_counts = Counter(normalized_name(row["name"]) for row in eligible)
        unambiguous = []
        review_exclusions = []
        for row in eligible:
            if name_counts[normalized_name(row["name"])] > 1:
                review_exclusions.append(
                    {
                        "reason": "duplicate_official_name",
                        "source_record_id": row["id"],
                    }
                )
            else:
                unambiguous.append(row)
        unambiguous.sort(
            key=lambda row: selection_digest(source["id"], row["id"])
        )
        family_cases = []
        for review_position, row in enumerate(unambiguous, 1):
            record_id = row["id"]
            if record_id in MEXICO_REVIEW_EXCLUSIONS:
                review_exclusions.append(
                    {
                        "reason": MEXICO_REVIEW_EXCLUSIONS[record_id],
                        "source_record_id": record_id,
                    }
                )
                continue
            selection_rank = len(family_cases) + 1
            family_cases.append(
                case(
                    case_id=f"everyday-mx-{record_id}",
                    name=row["name"],
                    latitude=float(row["latitude"]),
                    longitude=float(row["longitude"]),
                    country="MX",
                    macroregion="latin_america",
                    family=family,
                    script="latin",
                    source=source,
                    record_id=record_id,
                    selection_method=(
                        f"active fixed DENUE {family} establishment in frozen "
                        "Mexico City bounds; exclude generic and duplicate official "
                        "names; review hash order for public POI suitability; rank "
                        "by sha256(source_plan_id + NUL + source_record_id); "
                        f"review position {review_position}; selection rank "
                        f"{selection_rank}"
                    ),
                    provenance_extra={
                        "activity_code": row["activity_code"],
                        "activity_name": row["activity_name"],
                        "clee": row["clee"],
                        "municipality": row["municipality"],
                        "settlement": row["settlement"],
                        "source_added_date": row["source_added_date"],
                    },
                )
            )
            if len(family_cases) == quota:
                break
        if len(family_cases) < quota:
            raise ValueError(
                f"Mexico has only {len(family_cases)} reviewed {family} records"
            )
        selected_cases.extend(family_cases)
        family_reports[family] = {
            "eligible_after_duplicate_filter": len(unambiguous),
            "eligible_before_duplicate_filter": len(eligible),
            "filter_counts": dict(sorted(filter_counts[family].items())),
            "review_exclusions": review_exclusions,
            "selected": len(family_cases),
        }
    return selected_cases, {
        "families": family_reports,
        "input_rows": input_rows,
        "unique_clee_values": len(clee_values),
        "unique_denue_ids": len(record_ids),
        "selected": len(selected_cases),
    }


def parse_seoul_preview(path: Path) -> list[dict]:
    payload = JavaScriptDataParser(path.read_text()).parse()
    if not isinstance(payload, dict) or payload.get("result") != "ok":
        raise ValueError("Seoul preview did not return result=ok")
    page = payload.get("page")
    rows = payload.get("list")
    if not isinstance(page, dict) or not isinstance(rows, list):
        raise ValueError("Seoul preview is missing page/list data")
    if page.get("totalCount") != len(rows) or page.get("listCount") != len(rows):
        raise ValueError("Seoul preview count does not cover the complete dataset")
    if len(rows) > 1000:
        raise ValueError("Seoul preview exceeds its documented 1,000-row cap")
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("Seoul preview list contains a non-object row")
    return rows


def seoul_transformer(source: dict):
    expected = source.get("coordinate_transformation") or {}
    try:
        import pyproj
    except ImportError as exception:
        raise ValueError(
            "Seoul collection requires pyproj==3.7.2; install it in an ephemeral "
            "environment before reproducing this evidence"
        ) from exception
    if pyproj.__version__ != expected.get("pyproj_version"):
        raise ValueError(
            f"pyproj version differs: {pyproj.__version__} != "
            f"{expected.get('pyproj_version')}"
        )
    if pyproj.proj_version_str != expected.get("proj_version"):
        raise ValueError(
            f"PROJ version differs: {pyproj.proj_version_str} != "
            f"{expected.get('proj_version')}"
        )
    transformer = pyproj.Transformer.from_crs(
        expected.get("source_crs"),
        expected.get("target_crs"),
        always_xy=expected.get("always_xy") is True,
    )
    if transformer.description != expected.get("operation"):
        raise ValueError("selected EPSG coordinate operation differs from the manifest")
    if float(transformer.accuracy) != float(expected.get("accuracy_metres")):
        raise ValueError("selected EPSG coordinate-operation accuracy differs")
    return transformer


def collect_seoul(path: Path, source: dict, quota: int = 20) -> tuple[list, dict]:
    rows = parse_seoul_preview(path)
    eligible = []
    filter_counts = Counter()
    for row in rows:
        if row.get("TRDSTATEGBN") != "01":
            filter_counts["not_active_trade_state"] += 1
        elif row.get("DTLSTATENM") != "영업중":
            filter_counts["not_open_detailed_state"] += 1
        elif not str(row.get("BPLCNM") or "").strip():
            filter_counts["missing_name"] += 1
        elif not str(row.get("MGTNO") or "").strip():
            filter_counts["missing_licensing_management_number"] += 1
        elif not str(row.get("X") or "").strip() or not str(row.get("Y") or "").strip():
            filter_counts["missing_coordinates"] += 1
        else:
            try:
                x, y = float(row["X"]), float(row["Y"])
            except (TypeError, ValueError):
                filter_counts["invalid_coordinates"] += 1
                continue
            if not math.isfinite(x) or not math.isfinite(y):
                filter_counts["invalid_coordinates"] += 1
            else:
                eligible.append(row)
    name_counts = Counter(normalized_name(item["BPLCNM"]) for item in eligible)
    unambiguous = []
    review_exclusions = []
    for row in eligible:
        if name_counts[normalized_name(row["BPLCNM"])] > 1:
            review_exclusions.append(
                {
                    "reason": "duplicate_official_name",
                    "source_record_id": row["MGTNO"],
                }
            )
        else:
            unambiguous.append(row)
    unambiguous.sort(
        key=lambda item: selection_digest(source["id"], item["MGTNO"])
    )
    if len(unambiguous) < quota:
        raise ValueError(f"Seoul has only {len(unambiguous)} eligible hospitals")
    transformer = seoul_transformer(source)
    transformation = source["coordinate_transformation"]
    cases = []
    for rank, row in enumerate(unambiguous[:quota], 1):
        x, y = float(row["X"]), float(row["Y"])
        longitude, latitude = transformer.transform(x, y)
        if not valid_point([longitude, latitude]) or not (
            126.5 <= longitude <= 127.5 and 37.0 <= latitude <= 38.0
        ):
            raise ValueError(f"Seoul transformed point is implausible: {row['MGTNO']}")
        record_id = row["MGTNO"]
        cases.append(
            case(
                case_id=f"everyday-kr-{record_id}",
                name=row["BPLCNM"].strip(),
                latitude=latitude,
                longitude=longitude,
                country="KR",
                macroregion="east_asia",
                family="healthcare",
                script="non_latin",
                source=source,
                record_id=record_id,
                selection_method=(
                    "licensed hospital with trade state 01 and detailed state "
                    "영업중; exclude missing coordinates and duplicate official "
                    "names; rank by sha256(source_plan_id + NUL + "
                    f"source_record_id); selection rank {rank}"
                ),
                provenance_extra={
                    "source_coordinate": {
                        "x": row["X"],
                        "y": row["Y"],
                        "crs": transformation["source_crs"],
                    },
                    "coordinate_transformation": transformation,
                },
            )
        )
    return cases, {
        "eligible_after_duplicate_filter": len(unambiguous),
        "eligible_before_duplicate_filter": len(eligible),
        "input_rows": len(rows),
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
    parser.add_argument(
        "--hong-kong",
        type=Path,
        default=DEFAULT_SOURCE_DATA / "hk-hospitals.geojson",
    )
    parser.add_argument(
        "--seoul",
        type=Path,
        default=DEFAULT_SOURCE_DATA / "kr-seoul-hospitals.js",
    )
    parser.add_argument(
        "--melbourne",
        type=Path,
        default=DEFAULT_SOURCE_DATA / "au-melbourne-businesses-2024.json",
    )
    parser.add_argument(
        "--bogota",
        type=Path,
        default=DEFAULT_SOURCE_DATA / "co-bogota-health-network.geojson",
    )
    parser.add_argument(
        "--japan",
        type=Path,
        default=DEFAULT_SOURCE_DATA / "jp-shinjuku-medical.csv",
    )
    parser.add_argument(
        "--mexico",
        type=Path,
        default=DEFAULT_SOURCE_DATA / "mx-denue-09-2026.zip",
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
        hong_kong = source_by_id(manifest, "hk-ha-health-care-facilities")
        seoul = source_by_id(manifest, "kr-seoul-hospital-licenses")
        melbourne = source_by_id(manifest, "au-melbourne-clue-businesses")
        bogota = source_by_id(manifest, "co-bogota-public-health-network")
        japan = source_by_id(manifest, "jp-shinjuku-medical-facilities")
        mexico = source_by_id(manifest, "mx-inegi-denue")
        verify_snapshot(singapore, args.singapore)
        verify_snapshot(taiwan, args.taiwan)
        verify_snapshot(hong_kong, args.hong_kong)
        verify_snapshot(seoul, args.seoul)
        verify_snapshot(melbourne, args.melbourne)
        verify_snapshot(bogota, args.bogota)
        verify_snapshot(japan, args.japan)
        verify_snapshot(mexico, args.mexico)
        sg_cases, sg_report = collect_singapore(args.singapore, singapore)
        tw_cases, tw_report = collect_taiwan(args.taiwan, taiwan)
        hk_cases, hk_report = collect_hong_kong(args.hong_kong, hong_kong)
        kr_cases, kr_report = collect_seoul(args.seoul, seoul)
        co_cases, co_report = collect_bogota(args.bogota, bogota)
        au_cases, au_report = collect_melbourne(args.melbourne, melbourne)
        jp_cases, jp_report = collect_japan(args.japan, japan)
        mx_cases, mx_report = collect_mexico(args.mexico, mexico)
        payload = {
            "schema": SCHEMA,
            "collection_status": "complete; 200 of 200 frozen before provider requests",
            "cases": (
                sg_cases
                + tw_cases
                + hk_cases
                + kr_cases
                + co_cases
                + au_cases
                + jp_cases
                + mx_cases
            ),
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
                "hk-ha-health-care-facilities": hk_report,
                "kr-seoul-hospital-licenses": kr_report,
                "co-bogota-public-health-network": co_report,
                "au-melbourne-clue-businesses": au_report,
                "jp-shinjuku-medical-facilities": jp_report,
                "mx-inegi-denue": mx_report,
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
