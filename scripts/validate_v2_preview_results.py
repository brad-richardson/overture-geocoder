#!/usr/bin/env python3
"""Fail closed on preview benchmark errors, regressions, or missing target gains."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_document(path: Path, what: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"{what} is not readable JSON: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be a JSON object")
    return value


def provider_rows(payload: dict[str, Any], provider: str = "overture") -> list[dict]:
    rows = [row for row in payload.get("results", []) if row.get("provider") == provider]
    if not rows:
        raise ValueError(f"preview contains no {provider} results")
    return rows


def overall(payload: dict[str, Any]) -> dict[str, Any]:
    value = (payload.get("summary") or {}).get("overall")
    if not isinstance(value, dict):
        raise ValueError("preview omits summary.overall")
    return value


def paired(payload: dict[str, Any]) -> dict[str, Any]:
    value = (
        (payload.get("paired_comparison") or {}).get("groups", {}).get("overall")
    )
    if not isinstance(value, dict):
        raise ValueError("preview omits the paired overall comparison")
    return value


def validate_one(
    *,
    label: str,
    preview: dict[str, Any],
    baseline: dict[str, Any],
    expected_build: str,
    required_at_10: set[str],
) -> dict[str, Any]:
    rows = provider_rows(preview)
    baseline_rows = provider_rows(baseline)
    preview_ids = {row.get("case_id") for row in rows}
    baseline_ids = {row.get("case_id") for row in baseline_rows}
    if preview_ids != baseline_ids:
        raise ValueError(f"{label} preview and baseline case sets differ")
    if preview.get("meta", {}).get("data_version") != expected_build:
        raise ValueError(f"{label} preview served an unexpected data version")
    failures = [
        row.get("case_id")
        for row in rows
        if row.get("error") is not None
        or row.get("status") != 200
        or row.get("capability") != "supported"
    ]
    if failures:
        raise ValueError(f"{label} preview has failed requests: {failures[:10]}")

    preview_overall = overall(preview)
    baseline_overall = overall(baseline)
    comparison = paired(preview)
    for metric in ("found_at_1", "found_at_10"):
        if comparison.get(metric, {}).get("flips_to_miss") != 0:
            raise ValueError(f"{label} has a paired {metric} regression")
        if preview_overall.get(metric) < baseline_overall.get(metric):
            raise ValueError(f"{label} aggregate {metric} regressed")

    by_id = {row["case_id"]: row for row in rows}
    missing_required = sorted(
        case_id
        for case_id in required_at_10
        if case_id not in by_id or not by_id[case_id].get("found_at_10")
    )
    if missing_required:
        raise ValueError(
            f"{label} required cases did not reach the top ten: {missing_required}"
        )
    return {
        "cases": len(rows),
        "baseline": {
            "found_at_1": baseline_overall["found_at_1"],
            "found_at_10": baseline_overall["found_at_10"],
            "recall_at_1": baseline_overall["recall_at_1"],
            "recall_at_10": baseline_overall["recall_at_10"],
        },
        "preview": {
            "found_at_1": preview_overall["found_at_1"],
            "found_at_10": preview_overall["found_at_10"],
            "recall_at_1": preview_overall["recall_at_1"],
            "recall_at_10": preview_overall["recall_at_10"],
        },
        "paired": {
            metric: comparison[metric] for metric in ("found_at_1", "found_at_10")
        },
        "required_at_10": sorted(required_at_10),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-build", required=True)
    parser.add_argument("--gold-preview", type=Path, required=True)
    parser.add_argument("--gold-baseline", type=Path, required=True)
    parser.add_argument("--everyday-preview", type=Path, required=True)
    parser.add_argument("--everyday-baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    report = {
        "schema": "v2-preview-acceptance-v1",
        "accepted": True,
        "expected_build": args.expected_build,
        "gold": validate_one(
            label="gold",
            preview=read_document(args.gold_preview, "gold preview"),
            baseline=read_document(args.gold_baseline, "gold baseline"),
            expected_build=args.expected_build,
            required_at_10={
                "gold:name:big-ben",
                "gold:name:empire-state-building",
            },
        ),
        "everyday_poi": validate_one(
            label="everyday POI",
            preview=read_document(args.everyday_preview, "everyday preview"),
            baseline=read_document(args.everyday_baseline, "everyday baseline"),
            expected_build=args.expected_build,
            required_at_10=set(),
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
