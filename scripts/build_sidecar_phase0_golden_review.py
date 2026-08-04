#!/usr/bin/env python3
"""Build the sidecar Phase 0 golden review set for independent hand review.

This is a review *instrument*, not a review result.  It joins the frozen
200-decision risk-first queue to every piece of evidence a human needs to decide
a GERS-to-QID match without opening another file, emits an empty verdict file
bound to the exact inputs by hash, and renders the same decisions as a readable
markdown sheet.

It makes no decision.  Every row stays provisional, ``eligible_for_prominence``
is never set anywhere, and nothing here reads or writes a construction
namespace, release manifest, catalog, or Worker.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata
from typing import Any


GOLDEN_SCHEMA = "gers-qid-sidecar-golden-review-set-v1"
VERDICT_SCHEMA = "gers-qid-sidecar-golden-verdicts-v1"
CANDIDATE_SCHEMA = "gers-qid-sidecar-candidates-v1"
REVIEW_QUEUE_SCHEMA = "gers-qid-sidecar-review-queue-v1"
VERDICT_VALUES = ("accept", "reject", "needs_more_evidence")

RISK_ORDER = (
    "direct_identifier_conflict",
    "source_identity_multiple_gers",
    "distance_over_gate",
    "distance_missing",
    "no_normalized_name_overlap",
    "clean_direct_control",
)

RISK_CLASS_TITLES = {
    "direct_identifier_conflict": "Direct identifier conflict",
    "source_identity_multiple_gers": "Source identity owned by multiple GERS IDs",
    "distance_over_gate": "Observed distance beyond the match-radius gate",
    "distance_missing": "No computable distance",
    "no_normalized_name_overlap": "No normalized label overlap",
    "clean_direct_control": "Clean direct control",
}

RISK_CLASS_GUIDANCE = {
    "direct_identifier_conflict": (
        "Two or more Wikidata entities claim the same Overture place through the "
        "same direct Foursquare identifier. At most one can be the same entity, "
        "and possibly neither is. Decide each candidate row on its own: accept "
        "only the row whose Wikidata entity is the venue Overture names, and "
        "reject the others."
    ),
    "source_identity_multiple_gers": (
        "The Foursquare record backing this match is attached to more than one "
        "GERS ID, so the identifier does not uniquely name one Overture place. "
        "Check which GERS place is the real venue before accepting."
    ),
    "distance_over_gate": (
        "The direct identifier matched but the Overture and Wikidata coordinates "
        "disagree by more than the 1 km gate. Distance never accepts a match "
        "under the Phase 0 contract; treat the gap as a prompt to confirm the "
        "two records describe the same venue rather than a venue and its "
        "operator, a chain, or a relocated site."
    ),
    "distance_missing": (
        "No distance could be computed, so the only evidence is the identifier "
        "and the names. Read the null reason: either Wikidata published no P625 "
        "coordinate, or it published several and the collector deliberately "
        "refused to pick one."
    ),
    "no_normalized_name_overlap": (
        "The Overture names and the Wikidata label share no normalized token "
        "string, so the acceptance rests entirely on the direct identifier. "
        "Expect legitimate cases (different language, official versus common "
        "name, renamed venue) and illegitimate ones (recycled Foursquare venue "
        "id, entity describing the operator rather than the place)."
    ),
    "clean_direct_control": (
        "Unambiguous direct identifier, coordinates inside the gate, and at "
        "least one shared normalized name. These are controls: they should be "
        "accepts. A reject here is a false accept and fails the Phase 0 gate."
    ),
}

RULES = {
    "direct_source_wikidata_id.unambiguous": (
        "Exactly one Wikidata QID claims this Overture place's Foursquare source "
        "record through property P1968, and that source record belongs to exactly "
        "one GERS ID. A direct external identifier is the only automatic "
        "acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; "
        "names and coordinates did not contribute to this provisional decision."
    ),
    "direct_source_wikidata_id.conflict": (
        "This Overture place's Foursquare source identity resolves to more than "
        "one Wikidata QID, or the source record is claimed by more than one GERS "
        "ID. The direct identifier is therefore ambiguous, automatic acceptance "
        "is withheld by contract, and the candidate is provisionally "
        "needs_review."
    ),
    "reviewed_name_distance.always_review": (
        "The candidate was produced by normalized-name equality inside the "
        "coordinate gate. Fuzzy candidates are never automatically accepted "
        "under the Phase 0 contract."
    ),
}

EVIDENCE_GAPS = [
    {
        "field": "overture_categories",
        "state": "absent_from_frozen_input",
        "reason": (
            "scripts/collect_sidecar_phase0_foursquare.py selected only names, "
            "bbox coordinates, country, and bridge source ids from the public "
            "Places rows, so benchmarks/sidecar-phase0-foursquare-places-v1.jsonl "
            "carries no category. Re-collecting would change the frozen input "
            "hashes that the candidate set and this review set are bound to."
        ),
        "workaround": (
            "Use the Overture Explorer or the release parquet for the GERS ID if "
            "a category is decisive; record what you found in the verdict note."
        ),
    },
    {
        "field": "wikidata_description",
        "state": "absent_from_frozen_input",
        "reason": (
            "The frozen SPARQL snapshot selected ?item, ?foursquare, ?coord and "
            "?itemLabel only, so no schema:description was retrieved."
        ),
        "workaround": "Open the wikidata.org URL in review_urls.",
    },
    {
        "field": "wikidata_aliases",
        "state": "absent_from_frozen_input",
        "reason": (
            "The frozen SPARQL snapshot requested no skos:altLabel values. The "
            "names list below is the set of distinct itemLabel values observed "
            "for the QID in the snapshot, not the Wikidata alias set."
        ),
        "workaround": "Open the wikidata.org URL in review_urls.",
    },
    {
        "field": "wikidata_statement_id",
        "state": "absent_from_frozen_input",
        "reason": (
            "The snapshot used the truthy wdt:P1968 predicate, which returns "
            "claim values without statement GUIDs, ranks, or qualifiers."
        ),
        "workaround": (
            "The claim value (the Foursquare venue id) plus the snapshot and "
            "query hashes below identify the claim; open the QID to see rank and "
            "references."
        ),
    },
]


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
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"line {number} is not an object")
                rows.append(value)
    except (OSError, ValueError) as error:
        raise ValueError(f"{what} is not valid JSONL: {path}: {error}") from error
    return rows


def write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.tmp")
    staged.write_bytes(payload)
    staged.replace(path)


def normalize_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(character for character in text if not unicodedata.combining(character))
    return " ".join(
        "".join(character if character.isalnum() else " " for character in text).split()
    )


def decision_id(candidate_id: str) -> str:
    if not re.fullmatch(r"gq-[0-9a-f]{24}", str(candidate_id)):
        raise ValueError(f"unexpected candidate id shape: {candidate_id!r}")
    return "gqd-" + str(candidate_id)[3:]


def primary_risk_class(flags: list[str]) -> str:
    known = [flag for flag in RISK_ORDER if flag in flags]
    if not known:
        raise ValueError(f"no known risk flag in {flags!r}")
    return known[0]


def _rule_for(candidate: dict[str, Any]) -> str:
    method = candidate["match_method"]
    if method == "direct_source_wikidata_id":
        evidence = candidate.get("match_evidence") or {}
        conflict = bool(evidence.get("direct_identifier_conflict")) or bool(
            evidence.get("source_identities_with_multiple_gers")
        )
        return (
            "direct_source_wikidata_id.conflict"
            if conflict
            else "direct_source_wikidata_id.unambiguous"
        )
    if method == "reviewed_name_distance":
        return "reviewed_name_distance.always_review"
    raise ValueError(f"unknown match method: {method!r}")


def _distance_null_reason(
    place: dict[str, Any], entity: dict[str, Any]
) -> dict[str, str]:
    candidates = entity.get("coordinate_candidates") or []
    if place.get("latitude") is None or place.get("longitude") is None:
        return {
            "code": "overture_coordinate_absent",
            "explanation": (
                "The frozen Overture place row carries no coordinate, so no "
                "distance can be computed."
            ),
        }
    if len(candidates) > 1:
        return {
            "code": "wikidata_coordinate_ambiguous",
            "explanation": (
                f"The frozen Wikidata snapshot returned {len(candidates)} distinct "
                "P625 coordinates for this QID. The collector deliberately refuses "
                "to select one, so no distance is computed. Every candidate point "
                "is listed under wikidata.coordinate_candidates."
            ),
        }
    return {
        "code": "wikidata_coordinate_absent",
        "explanation": (
            "The frozen Wikidata snapshot returned no P625 coordinate for this "
            "QID, so no distance can be computed."
        ),
    }


def _flag_reasons(
    row: dict[str, Any],
    place: dict[str, Any],
    entity: dict[str, Any],
    *,
    distance_gate_km: float,
    distance_null: dict[str, str] | None,
) -> list[dict[str, str]]:
    reasons = []
    for flag in RISK_ORDER:
        if flag not in row["risk_flags"]:
            continue
        if flag == "direct_identifier_conflict":
            explanation = (
                "This GERS place resolves to more than one Wikidata QID through "
                "the same direct Foursquare identifier, so at most one candidate "
                "row for this place can be the same entity."
            )
        elif flag == "source_identity_multiple_gers":
            explanation = (
                "The Foursquare record id backing this match is attached to more "
                "than one GERS ID, so the identifier does not uniquely name one "
                "Overture place."
            )
        elif flag == "distance_over_gate":
            explanation = (
                f"The Overture and Wikidata coordinates are {row['distance_km']} km "
                f"apart, beyond the {distance_gate_km:.3f} km match-radius gate. "
                "Distance never accepts a match under the Phase 0 contract; it is "
                "a prompt to confirm both records describe the same venue."
            )
        elif flag == "distance_missing":
            explanation = (
                "No distance could be computed for this pair. "
                + (distance_null or {}).get("explanation", "")
            ).strip()
        elif flag == "no_normalized_name_overlap":
            explanation = (
                "The Overture names and the Wikidata label share no normalized "
                "token string, so this acceptance rests entirely on the direct "
                "identifier."
            )
        else:
            explanation = (
                "Unambiguous direct identifier, a distance inside the gate, and at "
                "least one shared normalized name. Included as a control on the "
                "automatic rule; a reject here is a false accept."
            )
        reasons.append({"flag": flag, "explanation": explanation})
    return reasons


def build_golden_set(
    *,
    candidates: dict[str, Any],
    review_queue: dict[str, Any],
    place_rows: list[dict[str, Any]],
    entity_rows: list[dict[str, Any]],
    collection: dict[str, Any],
    candidate_set_sha256: str,
    review_queue_sha256: str,
    places_sha256: str,
    entities_sha256: str,
    collection_sha256: str,
    spec_sha256: str,
    generated_at: str,
) -> dict[str, Any]:
    if candidates.get("schema") != CANDIDATE_SCHEMA:
        raise ValueError(f"candidate set is not {CANDIDATE_SCHEMA}")
    if review_queue.get("schema") != REVIEW_QUEUE_SCHEMA:
        raise ValueError(f"review queue is not {REVIEW_QUEUE_SCHEMA}")
    queue_binding = (review_queue.get("meta") or {}).get("candidate_set_sha256")
    if queue_binding != candidate_set_sha256:
        raise ValueError(
            "review queue is bound to a different candidate set: "
            f"{queue_binding!r} != {candidate_set_sha256!r}"
        )
    meta = candidates.get("meta") or {}
    if meta.get("places_sha256") != places_sha256:
        raise ValueError("candidate set is bound to a different places file")
    if meta.get("wikidata_entities_sha256") != entities_sha256:
        raise ValueError("candidate set is bound to a different entities file")

    by_candidate = {row["candidate_id"]: row for row in candidates["candidates"]}
    places = {row["gers_id"]: row for row in place_rows}
    entities = {row["wikidata_qid"]: row for row in entity_rows}
    distance_gate_km = float(meta.get("max_distance_km", 1.0))
    collection_meta = collection.get("meta") or {}

    decisions = []
    seen_ids = set()
    for order, row in enumerate(review_queue.get("queue") or [], 1):
        candidate = by_candidate.get(row["candidate_id"])
        if candidate is None:
            raise ValueError(f"queue names an unknown candidate: {row['candidate_id']}")
        place = places.get(row["gers_id"])
        entity = entities.get(row["wikidata_qid"])
        if place is None or entity is None:
            raise ValueError(
                f"frozen inputs do not cover candidate {row['candidate_id']}"
            )
        identifier = decision_id(row["candidate_id"])
        if identifier in seen_ids:
            raise ValueError(f"duplicate decision id: {identifier}")
        seen_ids.add(identifier)

        distance_km = row.get("distance_km")
        distance_null = (
            None if distance_km is not None else _distance_null_reason(place, entity)
        )
        place_normalized = sorted(
            {normalize_name(name) for name in place["names"]} - {""}
        )
        entity_normalized = sorted(
            {normalize_name(name) for name in entity["names"]} - {""}
        )
        rule_id = _rule_for(candidate)
        claims = [
            {
                "property": "P1968",
                "property_label": collection_meta.get(
                    "wikidata_property_label", "Foursquare City Guide venue ID"
                ),
                "value": record_id,
                "matches_overture_source_record": any(
                    source.get("record_id") == record_id
                    for source in row.get("source_identifiers") or []
                ),
            }
            for record_id in entity.get("external_ids", {}).get("Foursquare", [])
        ]
        decisions.append({
            "decision_id": identifier,
            "review_order": order,
            "candidate_id": row["candidate_id"],
            "risk_class": primary_risk_class(row["risk_flags"]),
            "risk_flags": _flag_reasons(
                row,
                place,
                entity,
                distance_gate_km=distance_gate_km,
                distance_null=distance_null,
            ),
            "provisional": {
                "decision": candidate["decision"],
                "automatic_acceptance": bool(candidate.get("automatic_acceptance")),
                "match_method": candidate["match_method"],
                "rule_id": rule_id,
                "rule_statement": RULES[rule_id],
                "matcher_version": candidate["matcher_version"],
                "review_status": candidate["review_status"],
                "eligible_for_prominence": False,
            },
            "overture": {
                "gers_id": row["gers_id"],
                "names": place["names"],
                "normalized_names": place_normalized,
                "country": place.get("country"),
                "coordinate": (
                    None
                    if place.get("latitude") is None
                    else {
                        "latitude": place["latitude"],
                        "longitude": place["longitude"],
                    }
                ),
                "coordinate_semantics": (
                    "Overture bbox minimum corner (ymin, xmin) of the place "
                    "geometry, as extracted by the frozen collector."
                ),
                "categories": None,
                "categories_null_reason": EVIDENCE_GAPS[0]["reason"],
                "release": candidate["first_overture_release"],
                "sources": [
                    {"dataset": source["dataset"], "record_id": source["record_id"]}
                    for source in place.get("sources") or []
                ],
            },
            "wikidata": {
                "wikidata_qid": row["wikidata_qid"],
                "labels": entity["names"],
                "normalized_labels": entity_normalized,
                "description": None,
                "description_null_reason": EVIDENCE_GAPS[1]["reason"],
                "aliases": None,
                "aliases_null_reason": EVIDENCE_GAPS[2]["reason"],
                "coordinate": (
                    None
                    if entity.get("latitude") is None
                    else {
                        "latitude": entity["latitude"],
                        "longitude": entity["longitude"],
                    }
                ),
                "coordinate_candidates": entity.get("coordinate_candidates") or [],
                "p1968_claims": claims,
                "claim_provenance": {
                    "sparql_endpoint": collection_meta.get("wikidata_sparql_endpoint"),
                    "query_sha256": collection_meta.get("wikidata_query_sha256"),
                    "snapshot_sha256": collection_meta.get("wikidata_snapshot_sha256"),
                    "predicate": "wdt:P1968 (truthy claim value; no statement GUID)",
                },
            },
            "comparison": {
                "distance_km": distance_km,
                "distance_null_reason": distance_null,
                "distance_gate_km": distance_gate_km,
                "distance_over_gate": (
                    None if distance_km is None else float(distance_km) > distance_gate_km
                ),
                "shared_normalized_names": row.get("shared_normalized_names") or [],
                "has_normalized_name_overlap": bool(row.get("shared_normalized_names")),
                "matched_source_identifiers": row.get("source_identifiers") or [],
            },
            "review_guidance": RISK_CLASS_GUIDANCE[
                primary_risk_class(row["risk_flags"])
            ],
            "review_urls": row.get("review_urls") or [],
        })

    if not decisions:
        raise ValueError("the review queue selected no decisions")

    class_counts: dict[str, int] = {}
    flag_counts: dict[str, int] = {}
    for decision in decisions:
        class_counts[decision["risk_class"]] = (
            class_counts.get(decision["risk_class"], 0) + 1
        )
        for flag in decision["risk_flags"]:
            flag_counts[flag["flag"]] = flag_counts.get(flag["flag"], 0) + 1

    return {
        "schema": GOLDEN_SCHEMA,
        "meta": {
            "generated_at": generated_at,
            "generator": "scripts/build_sidecar_phase0_golden_review.py",
            "purpose": (
                "Review instrument for the Phase 0 GERS-to-QID hand audit. It "
                "records provisional decisions and evidence only; no verdict, no "
                "gate result, and no prominence effect."
            ),
            "overture_release": meta.get("overture_release"),
            "matcher_version": meta.get("matcher_version"),
            "distance_gate_km": distance_gate_km,
            "input_bindings": {
                "candidate_set_sha256": candidate_set_sha256,
                "review_queue_sha256": review_queue_sha256,
                "places_sha256": places_sha256,
                "wikidata_entities_sha256": entities_sha256,
                "collection_report_sha256": collection_sha256,
                "phase0_spec_sha256": spec_sha256,
            },
            "verdict_values": list(VERDICT_VALUES),
            "review_order": (
                "Exactly the frozen risk-first order of "
                "benchmarks/2026-08-03-sidecar-phase0-review-queue-v1.json: "
                "highest risk class first, then candidate_id."
            ),
            "known_evidence_gaps": EVIDENCE_GAPS,
            "eligible_for_prominence": False,
            "construction_contract_movement": False,
        },
        "summary": {
            "decisions": len(decisions),
            "minimum_hand_checked_candidates": 200,
            "provisionally_accepted": sum(
                decision["provisional"]["decision"] == "accepted"
                for decision in decisions
            ),
            "provisionally_needs_review": sum(
                decision["provisional"]["decision"] == "needs_review"
                for decision in decisions
            ),
            "risk_class_counts": dict(sorted(class_counts.items())),
            "risk_flag_counts": dict(sorted(flag_counts.items())),
        },
        "decisions": decisions,
    }


def build_verdict_template(
    golden: dict[str, Any], *, golden_sha256: str
) -> dict[str, Any]:
    bindings = golden["meta"]["input_bindings"]
    return {
        "schema": VERDICT_SCHEMA,
        "meta": {
            "golden_review_set": (
                "benchmarks/2026-08-04-sidecar-phase0-golden-review-set-v1.json"
            ),
            "golden_review_set_sha256": golden_sha256,
            "candidate_set_sha256": bindings["candidate_set_sha256"],
            "expected_decisions": golden["summary"]["decisions"],
            "verdict_values": list(VERDICT_VALUES),
            "instructions": (
                "Append one object to \"verdicts\" per decision you check, in any "
                "order; partial review is expected and resumable. Required keys: "
                "decision_id, verdict, reviewer, reviewed_at (ISO 8601 with a UTC "
                "offset), note. A note is mandatory for reject and "
                "needs_more_evidence. Do not edit meta: the validator recomputes "
                "the golden-set hash and refuses any verdict file bound to "
                "different inputs."
            ),
            "gate": (
                "Phase 0 passes only when at least expected_decisions accept/reject "
                "verdicts exist and zero provisionally accepted decisions were "
                "rejected. The validator fails closed; nothing here marks the gate "
                "as met."
            ),
            "construction_contract_movement": False,
        },
        "verdicts": [],
    }


def render_sheet(golden: dict[str, Any]) -> str:
    meta = golden["meta"]
    bindings = meta["input_bindings"]
    lines = [
        "# Sidecar Phase 0 golden review sheet",
        "",
        f"Generated at: {meta['generated_at']}",
        "",
        "This sheet is a reading surface for "
        "`benchmarks/2026-08-04-sidecar-phase0-golden-review-set-v1.json`. It "
        "records provisional decisions only. Nothing here is a verdict, a gate "
        "result, or a prominence change.",
        "",
        f"- Decisions: {golden['summary']['decisions']} "
        f"(provisionally accepted {golden['summary']['provisionally_accepted']}, "
        f"provisionally needs_review "
        f"{golden['summary']['provisionally_needs_review']})",
        f"- Overture release: {meta['overture_release']}",
        f"- Matcher version: {meta['matcher_version']}",
        f"- Candidate set sha256: `{bindings['candidate_set_sha256']}`",
        f"- Review queue sha256: `{bindings['review_queue_sha256']}`",
        "- Record verdicts in "
        "`benchmarks/2026-08-04-sidecar-phase0-golden-verdicts-v1.json`, then run "
        "`scripts/validate_sidecar_phase0_golden_review.py`.",
        "",
        "## Evidence known to be missing from the frozen inputs",
        "",
    ]
    for gap in meta["known_evidence_gaps"]:
        lines.append(f"- **{gap['field']}** ({gap['state']}): {gap['reason']} "
                     f"_Workaround:_ {gap['workaround']}")
    lines.extend(["", "## Risk class index", ""])
    lines.append("| order | risk class | decisions |")
    lines.append("| ---: | --- | ---: |")
    for index, risk_class in enumerate(RISK_ORDER, 1):
        count = golden["summary"]["risk_class_counts"].get(risk_class, 0)
        if count:
            lines.append(
                f"| {index} | {RISK_CLASS_TITLES[risk_class]} | {count} |"
            )
    for risk_class in RISK_ORDER:
        group = [
            decision
            for decision in golden["decisions"]
            if decision["risk_class"] == risk_class
        ]
        if not group:
            continue
        lines.extend([
            "",
            f"## {RISK_CLASS_TITLES[risk_class]} ({len(group)})",
            "",
            RISK_CLASS_GUIDANCE[risk_class],
            "",
        ])
        for decision in group:
            overture = decision["overture"]
            entity = decision["wikidata"]
            comparison = decision["comparison"]
            coordinate = overture["coordinate"]
            place_point = (
                "absent"
                if coordinate is None
                else f"{coordinate['latitude']:.6f}, {coordinate['longitude']:.6f}"
            )
            entity_coordinate = entity["coordinate"]
            if entity_coordinate is not None:
                entity_point = (
                    f"{entity_coordinate['latitude']:.6f}, "
                    f"{entity_coordinate['longitude']:.6f}"
                )
            elif entity["coordinate_candidates"]:
                entity_point = " | ".join(
                    f"{point['latitude']:.6f}, {point['longitude']:.6f}"
                    for point in entity["coordinate_candidates"]
                ) + " (ambiguous, none selected)"
            else:
                entity_point = "absent"
            if comparison["distance_km"] is None:
                distance = (
                    "null — " + comparison["distance_null_reason"]["code"]
                )
            else:
                distance = f"{comparison['distance_km']} km"
            lines.extend([
                f"### {decision['review_order']:03d}. `{decision['decision_id']}`",
                "",
                f"- Provisional decision: **{decision['provisional']['decision']}**"
                f" (automatic_acceptance="
                f"{str(decision['provisional']['automatic_acceptance']).lower()},"
                f" rule `{decision['provisional']['rule_id']}`)",
                f"- Rule: {decision['provisional']['rule_statement']}",
                f"- Overture `{overture['gers_id']}` — names: "
                + "; ".join(overture["names"])
                + f" — country: {overture['country'] or 'null'} — point: {place_point}"
                + " — categories: null (not in frozen input)",
                "- Overture sources: "
                + "; ".join(
                    f"{source['dataset']}:{source['record_id']}"
                    for source in overture["sources"]
                ),
                f"- Wikidata `{entity['wikidata_qid']}` — labels: "
                + "; ".join(entity["labels"])
                + f" — point: {entity_point}"
                + " — description/aliases: null (not in frozen input)",
                "- P1968 claims: "
                + "; ".join(
                    f"{claim['value']}"
                    + (" (matches Overture source)" if claim["matches_overture_source_record"] else " (does not match this Overture source)")
                    for claim in entity["p1968_claims"]
                ),
                f"- Distance: {distance} (gate "
                f"{comparison['distance_gate_km']:.3f} km)",
                "- Shared normalized names: "
                + (
                    "; ".join(comparison["shared_normalized_names"])
                    if comparison["shared_normalized_names"]
                    else "none"
                ),
                "- Risk flags:",
            ])
            for flag in decision["risk_flags"]:
                lines.append(f"  - `{flag['flag']}` — {flag['explanation']}")
            lines.append("- Links: " + " ".join(decision["review_urls"]))
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--review-queue", type=Path, required=True)
    parser.add_argument("--places", type=Path, required=True)
    parser.add_argument("--wikidata-entities", type=Path, required=True)
    parser.add_argument("--collection-report", type=Path, required=True)
    parser.add_argument("--phase0-spec", type=Path, required=True)
    parser.add_argument("--output-set", type=Path, required=True)
    parser.add_argument("--output-verdicts", type=Path, required=True)
    parser.add_argument("--output-sheet", type=Path, required=True)
    parser.add_argument(
        "--generated-at",
        default=None,
        help="explicit ISO 8601 UTC stamp; defaults to the current time",
    )
    parser.add_argument(
        "--overwrite-verdicts",
        action="store_true",
        help="replace an existing verdict file (this discards recorded verdicts)",
    )
    args = parser.parse_args(argv)
    try:
        generated_at = args.generated_at or datetime.now(timezone.utc).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        golden = build_golden_set(
            candidates=read_json(args.candidates, "candidate set"),
            review_queue=read_json(args.review_queue, "review queue"),
            place_rows=read_jsonl(args.places, "places"),
            entity_rows=read_jsonl(args.wikidata_entities, "Wikidata entities"),
            collection=read_json(args.collection_report, "collection report"),
            candidate_set_sha256=sha256_file(args.candidates),
            review_queue_sha256=sha256_file(args.review_queue),
            places_sha256=sha256_file(args.places),
            entities_sha256=sha256_file(args.wikidata_entities),
            collection_sha256=sha256_file(args.collection_report),
            spec_sha256=sha256_file(args.phase0_spec),
            generated_at=generated_at,
        )
        write_bytes(args.output_set, canonical_json(golden))
        golden_sha = sha256_file(args.output_set)
        if args.output_verdicts.exists() and not args.overwrite_verdicts:
            existing = read_json(args.output_verdicts, "verdict file")
            recorded = existing.get("verdicts")
            if recorded:
                raise ValueError(
                    f"{args.output_verdicts} already records "
                    f"{len(recorded)} verdicts; refusing to overwrite without "
                    "--overwrite-verdicts"
                )
        write_bytes(
            args.output_verdicts,
            canonical_json(build_verdict_template(golden, golden_sha256=golden_sha)),
        )
        write_bytes(args.output_sheet, render_sheet(golden).encode("utf-8"))
    except (OSError, ValueError) as error:
        print(f"golden review set build failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(golden["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
