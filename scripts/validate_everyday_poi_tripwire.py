#!/usr/bin/env python3
"""Validate coverage and provenance for the everyday-POI fast-loop gold set."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC = ROOT / "benchmarks/everyday-poi-tripwire-spec-v1.json"
REPORT_SCHEMA = "everyday-poi-tripwire-readiness-v1"
REQUIRED_STRATA = {
    "country",
    "density",
    "macroregion",
    "poi_family",
    "script",
    "scope",
}
REQUIRED_PROVENANCE = {
    "accessed_at",
    "osm_derived",
    "selection_method",
    "source_kind",
    "source_license",
    "source_name",
    "source_record_id",
    "source_url",
}
OUTSIDE_TARGET = {"europe", "north_america"}


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode()


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _https_url(value: object) -> bool:
    if not _nonempty_string(value):
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _valid_lat_lon(case: dict) -> bool:
    try:
        latitude = float(case["expected_lat"])
        longitude = float(case["expected_lon"])
    except (KeyError, TypeError, ValueError):
        return False
    return -90 <= latitude <= 90 and -180 <= longitude <= 180


def validate_case(case: object, spec: dict) -> list[str]:
    if not isinstance(case, dict):
        return ["case is not an object"]
    case_id = str(case.get("id") or "<missing-id>")
    errors = []

    def error(message: str) -> None:
        errors.append(f"{case_id}: {message}")

    if not _nonempty_string(case.get("id")):
        error("id is required")
    if case.get("kind") != "place":
        error("kind must be place")
    if case.get("query_style") not in {"named_poi", "brand_branch"}:
        error("query_style must be named_poi or brand_branch")
    for field in ("query", "expected_name"):
        if not _nonempty_string(case.get(field)):
            error(f"{field} is required")
    if case.get("expected_feature_type") != "poi":
        error("expected_feature_type must be poi")
    if case.get("expected_gers_id") not in (None, ""):
        error("expected_gers_id must be absent; gold cannot come from Overture")
    if not _valid_lat_lon(case):
        error("expected coordinates are missing or out of range")
    try:
        tolerance = float(case.get("tolerance_km"))
        if not 0 < tolerance <= 5:
            raise ValueError
    except (TypeError, ValueError):
        error("tolerance_km must be greater than 0 and at most 5")

    strata = case.get("strata")
    if not isinstance(strata, dict):
        error("strata must be an object")
        strata = {}
    missing_strata = sorted(REQUIRED_STRATA - set(strata))
    if missing_strata:
        error("missing strata: " + ", ".join(missing_strata))
    if strata.get("scope") != "everyday":
        error("strata.scope must be everyday")
    allowed = spec["allowed"]
    for field, allowed_key in (
        ("poi_family", "poi_families"),
        ("script", "scripts"),
        ("density", "densities"),
    ):
        if strata.get(field) not in allowed[allowed_key]:
            error(f"strata.{field} is not allowed by the spec")
    for field in ("country", "macroregion"):
        if not _nonempty_string(strata.get(field)):
            error(f"strata.{field} is required")

    provenance = case.get("provenance")
    if not isinstance(provenance, dict):
        error("provenance must be an object")
        provenance = {}
    missing_provenance = sorted(REQUIRED_PROVENANCE - set(provenance))
    if missing_provenance:
        error("missing provenance: " + ", ".join(missing_provenance))
    for field in REQUIRED_PROVENANCE - {"osm_derived", "source_url"}:
        if not _nonempty_string(provenance.get(field)):
            error(f"provenance.{field} is required")
    if not _https_url(provenance.get("source_url")):
        error("provenance.source_url must be an https URL")
    if provenance.get("source_kind") not in allowed["source_kinds"]:
        error("provenance.source_kind is not allowed by the spec")
    if not isinstance(provenance.get("osm_derived"), bool):
        error("provenance.osm_derived must be boolean")

    providers = case.get("comparison_providers")
    if not isinstance(providers, list) or not providers:
        error("comparison_providers must be a non-empty list")
        providers = []
    elif len(set(providers)) != len(providers):
        error("comparison_providers contains duplicates")
    if any(provider not in allowed["comparison_providers"] for provider in providers):
        error("comparison_providers contains an unknown provider")
    expected_providers = (
        ["overture"]
        if provenance.get("osm_derived") is True
        else allowed["comparison_providers"]
    )
    if set(providers) != set(expected_providers):
        error(
            "comparison_providers must be Overture-only for OSM-derived gold "
            "and all providers otherwise"
        )
    return errors


def dimension_counts(cases: list[dict]) -> dict:
    counts = {
        "country": Counter(),
        "macroregion": Counter(),
        "poi_family": Counter(),
        "script": Counter(),
        "source_kind": Counter(),
    }
    osm_derived = 0
    for case in cases:
        strata = case.get("strata") if isinstance(case.get("strata"), dict) else {}
        provenance = (
            case.get("provenance")
            if isinstance(case.get("provenance"), dict)
            else {}
        )
        for dimension in ("country", "macroregion", "poi_family", "script"):
            counts[dimension][strata.get(dimension) or "<missing>"] += 1
        counts["source_kind"][provenance.get("source_kind") or "<missing>"] += 1
        osm_derived += provenance.get("osm_derived") is True
    return {
        **{key: dict(sorted(value.items())) for key, value in counts.items()},
        "osm_derived": osm_derived,
    }


def coverage_blockers(cases: list[dict], spec: dict) -> list[str]:
    gates = spec["gates"]
    counts = dimension_counts(cases)
    blockers = []

    def minimum(name: str, actual: int, required: int) -> None:
        if actual < required:
            blockers.append(f"{name}: {actual} < required {required}")

    minimum("cases", len(cases), gates["minimum_cases"])
    outside = sum(
        count for region, count in counts["macroregion"].items()
        if region not in OUTSIDE_TARGET
    )
    minimum(
        "outside Europe/North America cases",
        outside,
        gates["minimum_outside_europe_north_america"],
    )
    minimum(
        "non-Latin cases",
        counts["script"].get("non_latin", 0),
        gates["minimum_non_latin_script"],
    )
    minimum(
        "macroregions", len(counts["macroregion"]), gates["minimum_macroregions"]
    )
    minimum("countries", len(counts["country"]), gates["minimum_countries"])
    if counts["country"]:
        country, maximum = max(counts["country"].items(), key=lambda item: item[1])
        if maximum > gates["maximum_cases_per_country"]:
            blockers.append(
                f"country concentration {country}: {maximum} > allowed "
                f"{gates['maximum_cases_per_country']}"
            )
    minimum(
        "POI families", len(counts["poi_family"]), gates["minimum_poi_families"]
    )
    thin_families = {
        family: count
        for family, count in counts["poi_family"].items()
        if count < gates["minimum_cases_per_poi_family"]
    }
    if thin_families:
        blockers.append(
            "POI family counts below minimum: "
            + ", ".join(f"{key}={value}" for key, value in thin_families.items())
        )
    primary = sum(
        counts["source_kind"].get(kind, 0)
        for kind in ("government", "open_primary")
    )
    minimum(
        "government/open-primary cases",
        primary,
        gates["minimum_government_or_open_primary_cases"],
    )
    return blockers


def validate_payload(payload: object, spec: dict) -> dict:
    errors = []
    if not isinstance(payload, dict):
        return {"ready": False, "errors": ["case file is not an object"], "blockers": []}
    if payload.get("schema") != spec["case_schema"]:
        errors.append(f"case file schema must be {spec['case_schema']}")
    cases = payload.get("cases")
    if not isinstance(cases, list):
        return {"ready": False, "errors": errors + ["cases must be a list"], "blockers": []}
    seen = set()
    for case in cases:
        errors.extend(validate_case(case, spec))
        if isinstance(case, dict) and _nonempty_string(case.get("id")):
            if case["id"] in seen:
                errors.append(f"{case['id']}: duplicate id")
            seen.add(case["id"])
    blockers = coverage_blockers(
        [case for case in cases if isinstance(case, dict)], spec
    )
    return {
        "ready": not errors and not blockers,
        "errors": errors,
        "blockers": blockers,
        "case_count": len(cases),
        "dimensions": dimension_counts(
            [case for case in cases if isinstance(case, dict)]
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="write coverage evidence but exit zero while the collection is incomplete",
    )
    args = parser.parse_args(argv)
    try:
        spec_bytes = args.spec.read_bytes()
        case_bytes = args.cases.read_bytes()
        spec = json.loads(spec_bytes)
        payload = json.loads(case_bytes)
        if spec.get("schema") != "everyday-poi-tripwire-spec-v1":
            raise ValueError("unsupported tripwire spec schema")
        result = validate_payload(payload, spec)
    except (OSError, json.JSONDecodeError, ValueError) as exception:
        print(f"tripwire validation failed: {exception}", file=sys.stderr)
        return 2
    report = {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "spec": str(args.spec),
        "spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
        "cases": str(args.cases),
        "cases_sha256": hashlib.sha256(case_bytes).hexdigest(),
        **result,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical_json(report))
    for message in result["errors"]:
        print(f"error: {message}", file=sys.stderr)
    for message in result["blockers"]:
        print(f"blocker: {message}", file=sys.stderr)
    print(
        f"everyday POI tripwire: {result.get('case_count', 0)} cases, "
        f"ready={result['ready']}"
    )
    return 0 if result["ready"] or args.allow_incomplete else 1


if __name__ == "__main__":
    raise SystemExit(main())
