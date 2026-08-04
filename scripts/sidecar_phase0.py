#!/usr/bin/env python3
"""Build and audit non-promoting GERS-to-QID sidecar evidence.

Phase 0 deliberately stops before the construction projection.  It creates a
deterministic candidate set, resolves an independently reviewed audit into a
durable ledger, and measures the exact binary broadcast representation that a
later projection join could consume.  Nothing in this script reads or writes a
construction namespace, release manifest, catalog, or Worker.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import sys
import unicodedata
import uuid
from typing import Any


CANDIDATE_SCHEMA = "gers-qid-sidecar-candidates-v1"
AUDIT_SCHEMA = "gers-qid-sidecar-audit-decisions-v1"
LEDGER_SCHEMA = "gers-qid-sidecar-ledger-v1"
AUDIT_REPORT_SCHEMA = "gers-qid-sidecar-audit-report-v1"
MEASUREMENT_SCHEMA = "gers-qid-sidecar-broadcast-measurement-v1"
REVIEW_QUEUE_SCHEMA = "gers-qid-sidecar-review-queue-v1"
BROADCAST_MAGIC = b"GQSC0001"
BROADCAST_HEADER = struct.Struct("<8sQ")
BROADCAST_RECORD = struct.Struct("<16sQ")
QID_RE = re.compile(r"^Q([1-9][0-9]*)$")
RELEASE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+$")
MATCHER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REVIEW_VERDICTS = {"same_entity", "different_entity", "uncertain"}


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path, what: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"{what} is not readable JSON: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be a JSON object: {path}")
    return value


def read_jsonl(path: Path, what: str) -> list[dict[str, Any]]:
    rows = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"line {line_number} is not an object")
                rows.append(value)
    except (OSError, ValueError) as error:
        raise ValueError(f"{what} is not valid JSONL: {path}: {error}") from error
    return rows


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value))


def canonical_gers_id(value: object) -> str:
    try:
        result = str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"invalid GERS ID: {value!r}") from error
    if str(value).lower() != result:
        raise ValueError(f"GERS ID is not canonical: {value!r}")
    return result


def qid_number(value: object) -> int:
    match = QID_RE.fullmatch(str(value))
    if match is None:
        raise ValueError(f"invalid Wikidata QID: {value!r}")
    number = int(match.group(1))
    if number > (1 << 64) - 1:
        raise ValueError(f"Wikidata QID exceeds the broadcast u64: {value!r}")
    return number


def normalize_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(character for character in text if not unicodedata.combining(character))
    return " ".join(
        "".join(character if character.isalnum() else " " for character in text).split()
    )


def _coordinate(row: dict[str, Any], what: str) -> tuple[float, float] | None:
    latitude = row.get("latitude")
    longitude = row.get("longitude")
    if latitude is None and longitude is None:
        return None
    if latitude is None or longitude is None:
        raise ValueError(f"{what} has only one coordinate")
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{what} has invalid coordinates") from error
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError(f"{what} coordinates are out of range")
    return latitude, longitude


def haversine_km(first: tuple[float, float], second: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, first)
    lat2, lon2 = map(math.radians, second)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(value)))


def _names(row: dict[str, Any], what: str) -> list[str]:
    raw = row.get("names")
    if raw is None and row.get("name") is not None:
        raw = [row["name"]]
    if not isinstance(raw, list):
        raise ValueError(f"{what} names must be a list")
    names = sorted({str(value).strip() for value in raw if str(value).strip()})
    if not names:
        raise ValueError(f"{what} has no name")
    return names


def _country(row: dict[str, Any]) -> str | None:
    value = row.get("country")
    if value in (None, ""):
        return None
    value = str(value).upper()
    if not re.fullmatch(r"[A-Z]{2}", value):
        raise ValueError(f"invalid country code: {value!r}")
    return value


def _candidate_id(gers_id: str, qid: str, method: str) -> str:
    payload = f"{gers_id}\0{qid}\0{method}".encode()
    return "gq-" + hashlib.sha256(payload).hexdigest()[:24]


def _validate_places(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    places = []
    seen = set()
    for index, raw in enumerate(rows):
        what = f"place row {index}"
        gers_id = canonical_gers_id(raw.get("gers_id"))
        if gers_id in seen:
            raise ValueError(f"duplicate place GERS ID: {gers_id}")
        seen.add(gers_id)
        sources = raw.get("sources") or []
        if not isinstance(sources, list):
            raise ValueError(f"{what} sources must be a list")
        normalized_sources = []
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError(f"{what} contains a non-object source")
            dataset = str(source.get("dataset") or "")
            record_id = str(source.get("record_id") or "")
            if not dataset or not record_id:
                raise ValueError(f"{what} source needs dataset and record_id")
            normalized_sources.append((dataset, record_id))
        places.append({
            "gers_id": gers_id,
            "names": _names(raw, what),
            "coordinate": _coordinate(raw, what),
            "country": _country(raw),
            "sources": sorted(set(normalized_sources)),
        })
    return sorted(places, key=lambda row: row["gers_id"])


def _validate_entities(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entities = []
    seen = set()
    for index, raw in enumerate(rows):
        what = f"entity row {index}"
        qid = str(raw.get("wikidata_qid") or "")
        qid_number(qid)
        if qid in seen:
            raise ValueError(f"duplicate Wikidata entity: {qid}")
        seen.add(qid)
        external_ids = raw.get("external_ids") or {}
        if not isinstance(external_ids, dict):
            raise ValueError(f"{what} external_ids must be an object")
        normalized_ids = {}
        for dataset, values in external_ids.items():
            if not isinstance(values, list):
                raise ValueError(f"{what} external_ids[{dataset!r}] must be a list")
            ids = sorted({str(value) for value in values if str(value)})
            if ids:
                normalized_ids[str(dataset)] = ids
        entities.append({
            "wikidata_qid": qid,
            "names": _names(raw, what),
            "coordinate": _coordinate(raw, what),
            "country": _country(raw),
            "external_ids": normalized_ids,
        })
    return sorted(entities, key=lambda row: qid_number(row["wikidata_qid"]))


def build_candidates(
    *,
    place_rows: list[dict[str, Any]],
    entity_rows: list[dict[str, Any]],
    release: str,
    matcher_version: str,
    max_distance_km: float,
    places_sha256: str,
    entities_sha256: str,
) -> dict[str, Any]:
    if not RELEASE_RE.fullmatch(release):
        raise ValueError(f"invalid Overture release: {release!r}")
    if not MATCHER_RE.fullmatch(matcher_version):
        raise ValueError(f"invalid matcher version: {matcher_version!r}")
    if not (0 < max_distance_km <= 100):
        raise ValueError("max distance must be greater than zero and at most 100 km")
    places = _validate_places(place_rows)
    entities = _validate_entities(entity_rows)
    entities_by_qid = {row["wikidata_qid"]: row for row in entities}
    direct_index: dict[tuple[str, str], set[str]] = defaultdict(set)
    source_owners: dict[tuple[str, str], set[str]] = defaultdict(set)
    name_index: dict[str, set[str]] = defaultdict(set)
    for place in places:
        for source in place["sources"]:
            source_owners[source].add(place["gers_id"])
    for entity in entities:
        qid = entity["wikidata_qid"]
        for dataset, values in entity["external_ids"].items():
            for record_id in values:
                direct_index[(dataset, record_id)].add(qid)
        for name in entity["names"]:
            normalized = normalize_name(name)
            if normalized:
                name_index[normalized].add(qid)

    candidates = []
    direct_conflicts = 0
    for place in places:
        direct_evidence: dict[str, list[dict[str, str]]] = defaultdict(list)
        for dataset, record_id in place["sources"]:
            for qid in sorted(direct_index.get((dataset, record_id), set()), key=qid_number):
                direct_evidence[qid].append({"dataset": dataset, "record_id": record_id})
        if direct_evidence:
            repeated_sources = sorted({
                (item["dataset"], item["record_id"])
                for values in direct_evidence.values()
                for item in values
                if len(source_owners[(item["dataset"], item["record_id"])]) != 1
            })
            conflict = len(direct_evidence) != 1 or bool(repeated_sources)
            direct_conflicts += int(conflict)
            for qid in sorted(direct_evidence, key=qid_number):
                entity = entities_by_qid[qid]
                distance = None
                if place["coordinate"] is not None and entity["coordinate"] is not None:
                    distance = round(haversine_km(place["coordinate"], entity["coordinate"]), 6)
                candidates.append({
                    "candidate_id": _candidate_id(
                        place["gers_id"], qid, "direct_source_wikidata_id"
                    ),
                    "gers_id": place["gers_id"],
                    "wikidata_qid": qid,
                    "decision": "needs_review" if conflict else "accepted",
                    "match_method": "direct_source_wikidata_id",
                    "match_evidence": {
                        "source_identifiers": direct_evidence[qid],
                        "place_names": place["names"],
                        "wikidata_names": entity["names"],
                        "observed_distance_km": distance,
                        "direct_identifier_conflict": conflict,
                        "source_identities_with_multiple_gers": [
                            {"dataset": dataset, "record_id": record_id}
                            for dataset, record_id in repeated_sources
                        ],
                    },
                    "first_overture_release": release,
                    "last_validated_overture_release": release,
                    "matcher_version": matcher_version,
                    "review_status": "unreviewed",
                    "automatic_acceptance": not conflict,
                    "eligible_for_prominence": False,
                })
            continue

        qids = set()
        place_normalized_names = {normalize_name(name) for name in place["names"]}
        place_normalized_names.discard("")
        for name in place_normalized_names:
            qids.update(name_index.get(name, set()))
        for qid in sorted(qids, key=qid_number):
            entity = entities_by_qid[qid]
            if place["coordinate"] is None or entity["coordinate"] is None:
                continue
            if (
                place["country"] is not None
                and entity["country"] is not None
                and place["country"] != entity["country"]
            ):
                continue
            distance = haversine_km(place["coordinate"], entity["coordinate"])
            if distance > max_distance_km:
                continue
            entity_names = {normalize_name(name) for name in entity["names"]}
            shared_names = sorted(place_normalized_names & entity_names)
            candidates.append({
                "candidate_id": _candidate_id(
                    place["gers_id"], qid, "reviewed_name_distance"
                ),
                "gers_id": place["gers_id"],
                "wikidata_qid": qid,
                "decision": "needs_review",
                "match_method": "reviewed_name_distance",
                "match_evidence": {
                    "shared_normalized_names": shared_names,
                    "place_names": place["names"],
                    "wikidata_names": entity["names"],
                    "distance_km": round(distance, 6),
                    "distance_gate_km": max_distance_km,
                    "country_gate": place["country"] or entity["country"],
                },
                "first_overture_release": release,
                "last_validated_overture_release": release,
                "matcher_version": matcher_version,
                "review_status": "unreviewed",
                "automatic_acceptance": False,
                "eligible_for_prominence": False,
            })

    candidates.sort(
        key=lambda row: (
            uuid.UUID(row["gers_id"]).bytes,
            qid_number(row["wikidata_qid"]),
            row["match_method"],
        )
    )
    if len({row["candidate_id"] for row in candidates}) != len(candidates):
        raise ValueError("candidate IDs are not unique")
    return {
        "schema": CANDIDATE_SCHEMA,
        "meta": {
            "overture_release": release,
            "matcher_version": matcher_version,
            "max_distance_km": max_distance_km,
            "places_sha256": places_sha256,
            "wikidata_entities_sha256": entities_sha256,
            "construction_contract_movement": False,
        },
        "summary": {
            "places": len(places),
            "wikidata_entities": len(entities),
            "candidates": len(candidates),
            "automatic_accepts": sum(
                row["automatic_acceptance"] for row in candidates
            ),
            "direct_identifier_conflicts": direct_conflicts,
            "review_required": sum(
                row["review_status"] == "unreviewed" for row in candidates
            ),
        },
        "candidates": candidates,
    }


def validate_audit(
    candidates: dict[str, Any],
    audit: dict[str, Any],
    *,
    candidate_sha256: str,
    minimum_reviews: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if candidates.get("schema") != CANDIDATE_SCHEMA:
        raise ValueError(f"candidate set is not {CANDIDATE_SCHEMA}")
    if audit.get("schema") != AUDIT_SCHEMA:
        raise ValueError(f"audit is not {AUDIT_SCHEMA}")
    if audit.get("candidate_set_sha256") != candidate_sha256:
        raise ValueError("audit is not bound to the supplied candidate set")
    rows = candidates.get("candidates")
    reviews = audit.get("reviews")
    if not isinstance(rows, list) or not isinstance(reviews, list):
        raise ValueError("candidate and review rows must be lists")
    by_id = {row.get("candidate_id"): row for row in rows}
    if None in by_id or len(by_id) != len(rows):
        raise ValueError("candidate IDs are missing or repeated")
    reviewed: dict[str, dict[str, Any]] = {}
    for review in reviews:
        if not isinstance(review, dict):
            raise ValueError("review rows must be objects")
        candidate_id = review.get("candidate_id")
        if candidate_id not in by_id:
            raise ValueError(f"audit names an unknown candidate: {candidate_id!r}")
        if candidate_id in reviewed:
            raise ValueError(f"audit repeats candidate: {candidate_id}")
        verdict = review.get("verdict")
        if verdict not in REVIEW_VERDICTS:
            raise ValueError(f"invalid review verdict for {candidate_id}: {verdict!r}")
        reviewer = str(review.get("reviewer") or "").strip()
        reviewed_at = str(review.get("reviewed_at") or "")
        evidence = review.get("evidence")
        if not reviewer or not reviewed_at or not isinstance(evidence, list) or not evidence:
            raise ValueError(
                f"review {candidate_id} needs reviewer, reviewed_at, and evidence"
            )
        try:
            parsed_reviewed_at = datetime.fromisoformat(
                reviewed_at.replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError(f"review {candidate_id} has an invalid timestamp") from error
        if parsed_reviewed_at.utcoffset() is None:
            raise ValueError(f"review {candidate_id} timestamp needs a timezone")
        reviewed[candidate_id] = review

    ledger_rows = []
    false_accepts = []
    reviewed_methods = set()
    verdict_counts = {verdict: 0 for verdict in sorted(REVIEW_VERDICTS)}
    for candidate in rows:
        result = dict(candidate)
        review = reviewed.get(candidate["candidate_id"])
        if review is None:
            ledger_rows.append(result)
            continue
        verdict = review["verdict"]
        verdict_counts[verdict] += 1
        reviewed_methods.add(candidate["match_method"])
        if candidate.get("automatic_acceptance") and verdict == "different_entity":
            false_accepts.append(candidate["candidate_id"])
        if verdict == "same_entity":
            decision = "accepted"
        elif verdict == "different_entity":
            decision = "rejected"
        else:
            decision = "needs_review"
        result.update({
            "decision": decision,
            "review_status": "independently_reviewed",
            "review": {
                "reviewer": review["reviewer"],
                "reviewed_at": review["reviewed_at"],
                "verdict": verdict,
                "evidence": review["evidence"],
                "notes": str(review.get("notes") or ""),
            },
            "eligible_for_prominence": decision == "accepted",
        })
        ledger_rows.append(result)

    automatic_methods = {
        row["match_method"] for row in rows if row.get("automatic_acceptance")
    }
    missing_automatic_methods = sorted(automatic_methods - reviewed_methods)
    ready = (
        len(reviewed) >= minimum_reviews
        and not false_accepts
        and not missing_automatic_methods
    )
    ledger = {
        "schema": LEDGER_SCHEMA,
        "meta": {
            "candidate_set_sha256": candidate_sha256,
            "overture_release": candidates.get("meta", {}).get("overture_release"),
            "matcher_version": candidates.get("meta", {}).get("matcher_version"),
            "construction_contract_movement": False,
        },
        "mappings": ledger_rows,
    }
    report = {
        "schema": AUDIT_REPORT_SCHEMA,
        "ready": ready,
        "gates": {
            "minimum_hand_checked_candidates": minimum_reviews,
            "hand_checked_candidates": len(reviewed),
            "maximum_false_accepts": 0,
            "false_accepts": len(false_accepts),
            "false_accept_candidate_ids": sorted(false_accepts),
            "automatic_methods_missing_review": missing_automatic_methods,
        },
        "verdict_counts": verdict_counts,
        "ledger_counts": {
            decision: sum(row["decision"] == decision for row in ledger_rows)
            for decision in ("accepted", "rejected", "needs_review")
        },
    }
    return ledger, report


def build_review_queue(
    candidates: dict[str, Any], *, candidate_sha256: str, limit: int,
    control_quota: int,
) -> dict[str, Any]:
    if candidates.get("schema") != CANDIDATE_SCHEMA:
        raise ValueError(f"candidate set is not {CANDIDATE_SCHEMA}")
    rows = candidates.get("candidates")
    if not isinstance(rows, list):
        raise ValueError("candidate rows must be a list")
    if limit < 0:
        raise ValueError("review queue limit cannot be negative")
    if control_quota < 0 or (limit and control_quota > limit):
        raise ValueError("control quota must be between zero and the queue limit")
    distance_gate = float(candidates.get("meta", {}).get("max_distance_km", 1.0))
    queue = []
    flag_counts: dict[str, int] = defaultdict(int)
    for candidate in rows:
        evidence = candidate.get("match_evidence") or {}
        place_names = evidence.get("place_names") or []
        wikidata_names = evidence.get("wikidata_names") or []
        shared_names = sorted((
            {normalize_name(value) for value in place_names}
            & {normalize_name(value) for value in wikidata_names}
        ) - {""})
        distance = evidence.get("observed_distance_km", evidence.get("distance_km"))
        flags = []
        if evidence.get("direct_identifier_conflict"):
            flags.append("direct_identifier_conflict")
        if evidence.get("source_identities_with_multiple_gers"):
            flags.append("source_identity_multiple_gers")
        if distance is None:
            flags.append("distance_missing")
        elif float(distance) > distance_gate:
            flags.append("distance_over_gate")
        if not shared_names:
            flags.append("no_normalized_name_overlap")
        if not flags:
            flags.append("clean_direct_control")
        for flag in flags:
            flag_counts[flag] += 1
        source_identifiers = evidence.get("source_identifiers") or []
        queue.append({
            "candidate_id": candidate["candidate_id"],
            "gers_id": candidate["gers_id"],
            "wikidata_qid": candidate["wikidata_qid"],
            "match_method": candidate["match_method"],
            "provisional_decision": candidate["decision"],
            "automatic_acceptance": candidate.get("automatic_acceptance", False),
            "risk_flags": flags,
            "place_names": place_names,
            "wikidata_names": wikidata_names,
            "shared_normalized_names": shared_names,
            "distance_km": distance,
            "source_identifiers": source_identifiers,
            "review_urls": [
                f"https://www.wikidata.org/wiki/{candidate['wikidata_qid']}",
                *[
                    f"https://foursquare.com/v/{item['record_id']}"
                    for item in source_identifiers
                    if item.get("dataset") == "Foursquare"
                ],
            ],
        })
    risk_order = {
        "direct_identifier_conflict": 0,
        "source_identity_multiple_gers": 1,
        "distance_over_gate": 2,
        "distance_missing": 3,
        "no_normalized_name_overlap": 4,
        "clean_direct_control": 5,
    }
    queue.sort(key=lambda row: (
        min(risk_order[flag] for flag in row["risk_flags"]),
        row["candidate_id"],
    ))
    if limit == 0:
        selected = queue
    else:
        controls = [
            row for row in queue if row["risk_flags"] == ["clean_direct_control"]
        ]
        risks = [
            row for row in queue if row["risk_flags"] != ["clean_direct_control"]
        ]
        selected = risks[: max(0, limit - control_quota)]
        selected.extend(controls[:control_quota])
        if len(selected) < limit:
            selected_ids = {row["candidate_id"] for row in selected}
            selected.extend(
                row for row in queue
                if row["candidate_id"] not in selected_ids
            )
            selected = selected[:limit]
    return {
        "schema": REVIEW_QUEUE_SCHEMA,
        "meta": {
            "candidate_set_sha256": candidate_sha256,
            "selection": "risk-first then candidate_id",
            "requested_limit": limit,
            "clean_direct_control_quota": control_quota,
            "construction_contract_movement": False,
        },
        "summary": {
            "available_candidates": len(queue),
            "selected_candidates": len(selected),
            "available_flag_counts": dict(sorted(flag_counts.items())),
            "selected_flag_counts": {
                flag: sum(flag in row["risk_flags"] for row in selected)
                for flag in sorted(flag_counts)
            },
        },
        "queue": selected,
    }


def measure_broadcast(ledger: dict[str, Any], output: Path) -> dict[str, Any]:
    if ledger.get("schema") != LEDGER_SCHEMA:
        raise ValueError(f"ledger is not {LEDGER_SCHEMA}")
    accepted = [
        row for row in ledger.get("mappings", [])
        if row.get("decision") == "accepted"
        and row.get("review_status") == "independently_reviewed"
        and row.get("eligible_for_prominence") is True
    ]
    pairs = sorted(
        (
            uuid.UUID(canonical_gers_id(row.get("gers_id"))).bytes,
            qid_number(row.get("wikidata_qid")),
        )
        for row in accepted
    )
    if len({gers for gers, _qid in pairs}) != len(pairs):
        raise ValueError("accepted ledger maps one GERS ID more than once")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as handle:
        handle.write(BROADCAST_HEADER.pack(BROADCAST_MAGIC, len(pairs)))
        for gers, qid in pairs:
            handle.write(BROADCAST_RECORD.pack(gers, qid))

    expected_bytes = BROADCAST_HEADER.size + len(pairs) * BROADCAST_RECORD.size
    if output.stat().st_size != expected_bytes:
        raise ValueError("broadcast byte count does not reconcile")
    try:
        import numpy as np
    except ImportError as error:  # pragma: no cover - dependency failure
        raise RuntimeError("numpy is required to measure the planned join") from error
    gers_array = np.frombuffer(b"".join(gers for gers, _qid in pairs), dtype="V16").copy()
    qid_array = np.asarray([qid for _gers, qid in pairs], dtype="<u8")
    resident_bytes = sys.getsizeof(gers_array) + sys.getsizeof(qid_array)
    return {
        "schema": MEASUREMENT_SCHEMA,
        "mapping_count": len(pairs),
        "broadcast": {
            "format": "GQSC0001 little-endian (16-byte GERS UUID, uint64 QID)",
            "bytes": expected_bytes,
            "sha256": sha256_file(output),
            "bytes_per_mapping_excluding_header": BROADCAST_RECORD.size,
        },
        "resident_join": {
            "implementation": "sorted NumPy V16 plus little-endian uint64 arrays",
            "gers_buffer_bytes": int(gers_array.nbytes),
            "qid_buffer_bytes": int(qid_array.nbytes),
            "array_resident_bytes_including_headers": resident_bytes,
            "bytes_per_mapping_excluding_array_headers": BROADCAST_RECORD.size,
        },
        "construction_contract_movement": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)

    generate = modes.add_parser("generate", help="build deterministic candidates")
    generate.add_argument("--places", type=Path, required=True)
    generate.add_argument("--wikidata-entities", type=Path, required=True)
    generate.add_argument("--overture-release", required=True)
    generate.add_argument("--matcher-version", required=True)
    generate.add_argument("--max-distance-km", type=float, default=1.0)
    generate.add_argument("--output", type=Path, required=True)

    audit = modes.add_parser("audit", help="validate independent review decisions")
    audit.add_argument("--candidates", type=Path, required=True)
    audit.add_argument("--reviews", type=Path, required=True)
    audit.add_argument("--minimum-reviews", type=int, default=200)
    audit.add_argument("--ledger-output", type=Path, required=True)
    audit.add_argument("--report-output", type=Path, required=True)
    audit.add_argument("--allow-incomplete", action="store_true")

    queue = modes.add_parser("queue", help="build a risk-first manual review queue")
    queue.add_argument("--candidates", type=Path, required=True)
    queue.add_argument("--limit", type=int, default=200)
    queue.add_argument("--control-quota", type=int, default=50)
    queue.add_argument("--output", type=Path, required=True)

    measure = modes.add_parser("measure", help="measure the accepted broadcast join")
    measure.add_argument("--ledger", type=Path, required=True)
    measure.add_argument("--broadcast-output", type=Path, required=True)
    measure.add_argument("--report-output", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.mode == "generate":
            result = build_candidates(
                place_rows=read_jsonl(args.places, "places"),
                entity_rows=read_jsonl(args.wikidata_entities, "Wikidata entities"),
                release=args.overture_release,
                matcher_version=args.matcher_version,
                max_distance_km=args.max_distance_km,
                places_sha256=sha256_file(args.places),
                entities_sha256=sha256_file(args.wikidata_entities),
            )
            write_json(args.output, result)
            print(json.dumps(result["summary"], sort_keys=True))
            return 0
        if args.mode == "audit":
            if args.minimum_reviews < 1:
                raise ValueError("minimum reviews must be positive")
            candidate_sha = sha256_file(args.candidates)
            ledger, report = validate_audit(
                read_json(args.candidates, "candidate set"),
                read_json(args.reviews, "audit decisions"),
                candidate_sha256=candidate_sha,
                minimum_reviews=args.minimum_reviews,
            )
            write_json(args.ledger_output, ledger)
            write_json(args.report_output, report)
            print(json.dumps(report, sort_keys=True))
            return 0 if report["ready"] or args.allow_incomplete else 1
        if args.mode == "queue":
            candidate_sha = sha256_file(args.candidates)
            result = build_review_queue(
                read_json(args.candidates, "candidate set"),
                candidate_sha256=candidate_sha,
                limit=args.limit,
                control_quota=args.control_quota,
            )
            write_json(args.output, result)
            print(json.dumps(result["summary"], sort_keys=True))
            return 0
        result = measure_broadcast(
            read_json(args.ledger, "sidecar ledger"), args.broadcast_output
        )
        write_json(args.report_output, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"sidecar Phase 0 failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
