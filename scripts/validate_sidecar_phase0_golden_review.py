#!/usr/bin/env python3
"""Validate sidecar Phase 0 golden-review verdicts and compute the gate.

The validator never decides a match and never marks anything eligible for
prominence.  It checks that a verdict file is bound by hash to the exact golden
review set and candidate set it was written against, reports coverage of the
frozen decision list, and computes the Phase 0 tripwire: a false accept is a
provisionally accepted candidate that the reviewer rejected.

It fails closed.  An incomplete review, an unbound verdict file, or any
integrity error is reported as "gate not met", never as passed.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


GOLDEN_SCHEMA = "gers-qid-sidecar-golden-review-set-v1"
VERDICT_SCHEMA = "gers-qid-sidecar-golden-verdicts-v1"
CANDIDATE_SCHEMA = "gers-qid-sidecar-candidates-v1"
REPORT_SCHEMA = "gers-qid-sidecar-golden-review-report-v1"
DECIDING_VERDICTS = ("accept", "reject")
VERDICT_VALUES = ("accept", "reject", "needs_more_evidence")

EXIT_GATE_MET = 0
EXIT_GATE_NOT_MET = 1
EXIT_INTEGRITY_FAILURE = 2


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


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.tmp")
    staged.write_bytes(canonical_json(value))
    staged.replace(path)


def _check_timestamp(value: str) -> str | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return "reviewed_at is not an ISO 8601 timestamp"
    if parsed.utcoffset() is None:
        return "reviewed_at needs a UTC offset"
    return None


def validate(
    *,
    golden: dict[str, Any],
    verdicts: dict[str, Any],
    golden_sha256: str,
    candidate_set_sha256: str | None,
    minimum_decisions: int | None,
) -> dict[str, Any]:
    errors: list[str] = []

    if golden.get("schema") != GOLDEN_SCHEMA:
        raise ValueError(f"golden review set is not {GOLDEN_SCHEMA}")
    decisions = golden.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("golden review set carries no decisions")
    by_decision = {row.get("decision_id"): row for row in decisions}
    if None in by_decision or len(by_decision) != len(decisions):
        raise ValueError("golden decision ids are missing or repeated")
    bindings = (golden.get("meta") or {}).get("input_bindings") or {}
    golden_candidate_sha = bindings.get("candidate_set_sha256")

    if verdicts.get("schema") != VERDICT_SCHEMA:
        raise ValueError(f"verdict file is not {VERDICT_SCHEMA}")
    verdict_meta = verdicts.get("meta") or {}
    rows = verdicts.get("verdicts")
    if not isinstance(rows, list):
        raise ValueError("verdict file must carry a verdicts list")

    if verdict_meta.get("golden_review_set_sha256") != golden_sha256:
        errors.append(
            "verdict file is bound to a different golden review set "
            f"({verdict_meta.get('golden_review_set_sha256')!r} != "
            f"{golden_sha256!r}); the review inputs changed under the verdicts"
        )
    if verdict_meta.get("candidate_set_sha256") != golden_candidate_sha:
        errors.append(
            "verdict file candidate_set_sha256 does not match the golden review set"
        )
    if candidate_set_sha256 is not None and golden_candidate_sha != candidate_set_sha256:
        errors.append(
            "golden review set is not bound to the supplied candidate set "
            f"({golden_candidate_sha!r} != {candidate_set_sha256!r})"
        )

    seen: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        label = f"verdict row {index}"
        if not isinstance(row, dict):
            errors.append(f"{label} is not an object")
            continue
        identifier = row.get("decision_id")
        if identifier not in by_decision:
            errors.append(f"{label} names an unknown decision: {identifier!r}")
            continue
        if identifier in seen:
            errors.append(f"{label} repeats decision {identifier}")
            continue
        verdict = row.get("verdict")
        if verdict not in VERDICT_VALUES:
            errors.append(f"{identifier} has an invalid verdict: {verdict!r}")
            continue
        if not str(row.get("reviewer") or "").strip():
            errors.append(f"{identifier} has no reviewer")
            continue
        timestamp_error = _check_timestamp(row.get("reviewed_at") or "")
        if timestamp_error is not None:
            errors.append(f"{identifier} {timestamp_error}")
            continue
        note = str(row.get("note") or "").strip()
        if verdict != "accept" and not note:
            errors.append(f"{identifier} verdict {verdict!r} requires a note")
            continue
        seen[identifier] = row

    required = (
        minimum_decisions
        if minimum_decisions is not None
        else int(
            (golden.get("summary") or {}).get(
                "minimum_hand_checked_candidates", len(decisions)
            )
        )
    )
    counts = {value: 0 for value in VERDICT_VALUES}
    for row in seen.values():
        counts[row["verdict"]] += 1
    decided = sum(counts[value] for value in DECIDING_VERDICTS)

    false_accepts = sorted(
        identifier
        for identifier, row in seen.items()
        if row["verdict"] == "reject"
        and by_decision[identifier]["provisional"]["decision"] == "accepted"
    )
    rejected_review_candidates = sorted(
        identifier
        for identifier, row in seen.items()
        if row["verdict"] == "reject"
        and by_decision[identifier]["provisional"]["decision"] != "accepted"
    )

    per_class: dict[str, dict[str, int]] = {}
    for decision in decisions:
        bucket = per_class.setdefault(
            decision["risk_class"],
            {"decisions": 0, "decided": 0, "needs_more_evidence": 0, "undecided": 0},
        )
        bucket["decisions"] += 1
        row = seen.get(decision["decision_id"])
        if row is None:
            bucket["undecided"] += 1
        elif row["verdict"] in DECIDING_VERDICTS:
            bucket["decided"] += 1
        else:
            bucket["needs_more_evidence"] += 1

    undecided = sorted(
        decision["decision_id"]
        for decision in decisions
        if seen.get(decision["decision_id"], {}).get("verdict")
        not in DECIDING_VERDICTS
    )

    coverage_met = decided >= required
    gate_met = bool(coverage_met and not false_accepts and not errors)
    blockers = []
    if errors:
        blockers.append("verdict-file integrity failed")
    if not coverage_met:
        blockers.append(
            f"only {decided} of {required} required decisions have an "
            "accept/reject verdict"
        )
    if false_accepts:
        blockers.append(f"{len(false_accepts)} false accepts")

    return {
        "schema": REPORT_SCHEMA,
        "gate_met": gate_met,
        "integrity": {
            "ok": not errors,
            "errors": errors,
            "golden_review_set_sha256": golden_sha256,
            "candidate_set_sha256": golden_candidate_sha,
        },
        "coverage": {
            "decisions": len(decisions),
            "minimum_required_decisions": required,
            "decided": decided,
            "verdict_counts": counts,
            "remaining_for_gate": max(0, required - decided),
            "undecided_decision_ids": undecided,
            "per_risk_class": dict(sorted(per_class.items())),
        },
        "phase0_gate": {
            "minimum_hand_checked_candidates": required,
            "hand_checked_candidates": decided,
            "maximum_false_accepts": 0,
            "false_accepts": len(false_accepts),
            "false_accept_decision_ids": false_accepts,
            "rejected_needs_review_decision_ids": rejected_review_candidates,
            "blockers": blockers,
        },
        "effect": {
            "eligible_for_prominence": False,
            "construction_contract_movement": False,
            "note": (
                "This report is evidence only. It never sets "
                "eligible_for_prominence and never authorizes a projection join, "
                "prominence change, or promotion."
            ),
        },
    }


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    """Compact console view; the full report keeps every undecided id."""
    coverage = report["coverage"]
    gate = report["phase0_gate"]
    return {
        "gate_met": report["gate_met"],
        "integrity_ok": report["integrity"]["ok"],
        "integrity_errors": report["integrity"]["errors"][:10],
        "decisions": coverage["decisions"],
        "decided": coverage["decided"],
        "remaining_for_gate": coverage["remaining_for_gate"],
        "verdict_counts": coverage["verdict_counts"],
        "false_accepts": gate["false_accepts"],
        "false_accept_decision_ids": gate["false_accept_decision_ids"],
        "blockers": gate["blockers"],
        "eligible_for_prominence": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden-set", type=Path, required=True)
    parser.add_argument("--verdicts", type=Path, required=True)
    parser.add_argument(
        "--candidates",
        type=Path,
        default=None,
        help="optional frozen candidate set; its hash must match the binding",
    )
    parser.add_argument(
        "--minimum-decisions",
        type=int,
        default=None,
        help="override the gate's minimum accept/reject count",
    )
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.minimum_decisions is not None and args.minimum_decisions < 1:
        parser.error("--minimum-decisions must be positive")
    try:
        candidate_sha = (
            sha256_file(args.candidates) if args.candidates is not None else None
        )
        if args.candidates is not None:
            candidates = read_json(args.candidates, "candidate set")
            if candidates.get("schema") != CANDIDATE_SCHEMA:
                raise ValueError(f"candidate set is not {CANDIDATE_SCHEMA}")
        report = validate(
            golden=read_json(args.golden_set, "golden review set"),
            verdicts=read_json(args.verdicts, "verdict file"),
            golden_sha256=sha256_file(args.golden_set),
            candidate_set_sha256=candidate_sha,
            minimum_decisions=args.minimum_decisions,
        )
    except (OSError, ValueError) as error:
        print(f"golden review validation failed: {error}", file=sys.stderr)
        return EXIT_INTEGRITY_FAILURE
    if args.output is not None:
        write_json(args.output, report)
    print(json.dumps(summarize(report), sort_keys=True))
    if not report["integrity"]["ok"]:
        return EXIT_INTEGRITY_FAILURE
    return EXIT_GATE_MET if report["gate_met"] else EXIT_GATE_NOT_MET


if __name__ == "__main__":
    raise SystemExit(main())
