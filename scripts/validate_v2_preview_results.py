#!/usr/bin/env python3
"""Fail closed on preview benchmark errors, regressions, or missing target gains.

Every gate condition below is unchanged. What is new is that a rejection is
CLASSIFIED and written down instead of escaping as a bare traceback.

The v4 session ran six preview attempts that failed for three materially
different reasons and surfaced identically as "preview failed":

* attempts 1-2 -- setup/environment (an unpinned build tool, a missing Python
  dependency). Fix the workflow, then retry.
* attempts 3-4 -- a real Worker quality regression. Correctly caught. Retrying
  would have been wrong.
* attempts 5-6 -- transient infrastructure (all 55 gold requests 404'd; then one
  timeout and two HTTP 500s). Retrying was exactly right.

Telling those apart took an operator reading logs each time. This script now
emits `failure_class` -- `setup`, `operational-transient`, or
`quality-regression` -- so the retry decision is mechanical.

Classification never relaxes a gate. A rejection is still a rejection and the
exit code is still 1; the class only says which of the three things went wrong.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


# The environment or the binding is wrong: the preview measured a different
# thing than the baseline, or an input is unreadable. Fix the setup, then retry.
SETUP = "setup"
# The endpoint misbehaved during the window. The measurement is void, not
# negative. Retry unchanged.
OPERATIONAL_TRANSIENT = "operational-transient"
# The candidate is worse. Do NOT retry -- a retry is how a real regression gets
# rerolled into an accept.
QUALITY_REGRESSION = "quality-regression"

RETRY_ADVICE = {
    SETUP: "Fix the workflow or inputs; a retry as-is will fail the same way.",
    OPERATIONAL_TRANSIENT: (
        "Retry the same inputs unchanged. If it recurs, treat the endpoint as "
        "unhealthy rather than raising the retry budget."
    ),
    QUALITY_REGRESSION: (
        "DO NOT retry. The candidate lost cases the baseline held; this is the "
        "gate working."
    ),
}


class Rejection(ValueError):
    """A classified refusal to accept a preview."""

    def __init__(
        self,
        failure_class: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.detail = detail or {}


def read_document(path: Path, what: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise Rejection(
            SETUP, f"{what} is not readable JSON: {path}"
        ) from error
    if not isinstance(value, dict):
        raise Rejection(SETUP, f"{what} must be a JSON object")
    return value


def provider_rows(payload: dict[str, Any], provider: str = "overture") -> list[dict]:
    rows = [row for row in payload.get("results", []) if row.get("provider") == provider]
    if not rows:
        raise Rejection(SETUP, f"preview contains no {provider} results")
    return rows


def overall(payload: dict[str, Any]) -> dict[str, Any]:
    value = (payload.get("summary") or {}).get("overall")
    if not isinstance(value, dict):
        raise Rejection(SETUP, "preview omits summary.overall")
    return value


def paired(payload: dict[str, Any]) -> dict[str, Any]:
    value = (
        (payload.get("paired_comparison") or {}).get("groups", {}).get("overall")
    )
    if not isinstance(value, dict):
        raise Rejection(SETUP, "preview omits the paired overall comparison")
    return value


def retry_telemetry(payload: dict[str, Any]) -> dict[str, Any]:
    """What the harness had to retry to produce this measurement.

    Reported whether or not the run passed. Bounded transient retries make a
    flaky window survivable; they must never make a flaky window invisible.
    """
    meta = payload.get("meta") or {}
    return {
        "transient_retries_allowed": meta.get("transient_retries_allowed"),
        "transient_retries_used": meta.get("transient_retries_used"),
        "cases_with_transient_retry": meta.get("cases_with_transient_retry"),
    }


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
        raise Rejection(
            SETUP,
            f"{label} preview and baseline case sets differ",
            {
                "only_in_preview": sorted(preview_ids - baseline_ids)[:10],
                "only_in_baseline": sorted(baseline_ids - preview_ids)[:10],
            },
        )
    if preview.get("meta", {}).get("data_version") != expected_build:
        raise Rejection(
            SETUP,
            f"{label} preview served an unexpected data version",
            {
                "expected": expected_build,
                "served": preview.get("meta", {}).get("data_version"),
            },
        )
    failed_rows = [
        row
        for row in rows
        if row.get("error") is not None
        or row.get("status") != 200
        or row.get("capability") != "supported"
    ]
    if failed_rows:
        statuses: dict[str, int] = {}
        for row in failed_rows:
            key = str(row.get("status"))
            statuses[key] = statuses.get(key, 0) + 1
        raise Rejection(
            OPERATIONAL_TRANSIENT,
            f"{label} preview has failed requests: "
            f"{[row.get('case_id') for row in failed_rows][:10]}",
            {
                "failed": len(failed_rows),
                "of": len(rows),
                "status_histogram": statuses,
                # Every request failing is still the transient class -- attempt
                # 5 was 55 of 55 -- but it points at the deploy, not at a blip.
                "all_requests_failed": len(failed_rows) == len(rows),
                "retries": retry_telemetry(preview),
            },
        )

    preview_overall = overall(preview)
    baseline_overall = overall(baseline)
    comparison = paired(preview)
    for metric in ("found_at_1", "found_at_10"):
        if comparison.get(metric, {}).get("flips_to_miss") != 0:
            raise Rejection(
                QUALITY_REGRESSION,
                f"{label} has a paired {metric} regression",
                {"metric": metric, "paired": comparison.get(metric)},
            )
        if preview_overall.get(metric) < baseline_overall.get(metric):
            raise Rejection(
                QUALITY_REGRESSION,
                f"{label} aggregate {metric} regressed",
                {
                    "metric": metric,
                    "baseline": baseline_overall.get(metric),
                    "preview": preview_overall.get(metric),
                },
            )

    by_id = {row["case_id"]: row for row in rows}
    missing_required = sorted(
        case_id
        for case_id in required_at_10
        if case_id not in by_id or not by_id[case_id].get("found_at_10")
    )
    if missing_required:
        raise Rejection(
            QUALITY_REGRESSION,
            f"{label} required cases did not reach the top ten: {missing_required}",
            {"missing_required": missing_required},
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
        "retries": retry_telemetry(preview),
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    return {
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-build", required=True)
    parser.add_argument("--gold-preview", type=Path, required=True)
    parser.add_argument("--gold-baseline", type=Path, required=True)
    parser.add_argument("--everyday-preview", type=Path, required=True)
    parser.add_argument("--everyday-baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        report = build_report(args)
    except Rejection as rejection:
        # A rejection is now WRITTEN DOWN. Previously it escaped as a traceback
        # and no acceptance document existed, so the run summary had nothing to
        # report and the class had to be reconstructed from logs by hand.
        report = {
            "schema": "v2-preview-acceptance-v1",
            "accepted": False,
            "expected_build": args.expected_build,
            "failure_class": rejection.failure_class,
            "reason": str(rejection),
            "detail": rejection.detail,
            "retry_advice": RETRY_ADVICE[rejection.failure_class],
        }
        write_report(args.output, report)
        print(f"::error::preview rejected [{rejection.failure_class}]: {rejection}")
        print(RETRY_ADVICE[rejection.failure_class])
        return 1

    write_report(args.output, report)
    print(json.dumps(report, sort_keys=True))
    return 0


def write_report(output: Path, report: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    raise SystemExit(main())
