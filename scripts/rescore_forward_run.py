#!/usr/bin/env python3
"""Re-score a frozen forward-benchmark run under a different name test.

Why this exists
---------------
The everyday-POI gold names are verbatim government-registry strings while
Overture carries signage/OSM names, and the benchmark's place matcher required
normalized string EQUALITY. `BEDOK RESERVOIR MRT STATION` therefore could not
match `Bedok Reservoir Station` even when returned at 10 m: a perfect retrieval
fix still scored a miss. Before spending more Worker work against that set we
need to know how much of the miss pile is a scorer artifact rather than a
retrieval failure.

The honest way to answer that is to score ONE run two ways -- same build, same
responses, no new requests -- and then hand-audit every case the relaxed test
newly accepts. That is what this does. It deliberately does not print a single
headline number without also emitting the flip list, because a metric that
moves for scoring reasons is worth nothing unless someone has looked at what
moved.

Requires a run recorded with candidate retention (`candidates` on each row).
Runs frozen before that landed cannot be re-scored and are rejected with a
clear message rather than silently scored as all-miss.

Usage:
    rescore_forward_run.py --run RESULTS.json --cases CASES.json [--cases ...]
                           [--mode containment] [--output REPORT.json]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchmark_v2_forward import (  # noqa: E402
    CONTAINMENT_NAME_MATCH_RATIONALE,
    FOUND_AT,
    NAME_MATCH_MODES,
    _name_matches,
    haversine_km,
)

RESCORE_SCHEMA = "benchmark-v2-forward-rescore-v1"


def load_cases(paths):
    cases = {}
    for path in paths:
        payload = json.loads(Path(path).read_text())
        for case in payload.get("cases", []):
            cases[case["id"]] = case
    return cases


def candidate_as_feature(candidate):
    """Rebuild the minimal feature shape the matcher reads."""
    return {
        "id": candidate.get("id"),
        "properties": {
            "name": candidate.get("name"),
            "feature_type": candidate.get("feature_type"),
        },
        "geometry": {
            "type": "Point",
            "coordinates": [candidate.get("lon"), candidate.get("lat")],
        },
    }


def rank_under(case, candidates, mode):
    """1-based rank of the first accepted candidate, or None."""
    expected = (case["expected_lat"], case["expected_lon"])
    tolerance = case.get("tolerance_km", 50.0)
    for index, candidate in enumerate(candidates):
        lat, lon = candidate.get("lat"), candidate.get("lon")
        if lat is None or lon is None:
            continue
        if haversine_km(*expected, lat, lon) > tolerance:
            continue
        if _name_matches(case, candidate_as_feature(candidate), mode):
            return index + 1
    return None


def rescore(run, cases, mode):
    rows = [row for row in run["results"] if row.get("provider") == "overture"]
    if not rows:
        raise SystemExit("run contains no overture rows")
    missing = [row["case_id"] for row in rows if "candidates" not in row]
    if missing:
        raise SystemExit(
            f"{len(missing)} of {len(rows)} rows have no retained candidates; "
            "this run predates candidate retention and cannot be re-scored "
            "offline. Re-run the benchmark to produce a re-scorable artifact."
        )
    unknown = [row["case_id"] for row in rows if row["case_id"] not in cases]
    if unknown:
        raise SystemExit(
            f"{len(unknown)} run rows have no matching case "
            f"(first: {unknown[0]}); wrong --cases file?"
        )

    baseline_at_1 = baseline_at_10 = 0
    rescored_at_1 = rescored_at_10 = 0
    flips, regressions = [], []
    for row in rows:
        case = cases[row["case_id"]]
        candidates = row["candidates"]
        old_rank = row.get("rank")
        new_rank = rank_under(case, candidates, mode)

        baseline_at_1 += bool(old_rank == 1)
        baseline_at_10 += bool(old_rank is not None and old_rank <= FOUND_AT)
        rescored_at_1 += bool(new_rank == 1)
        rescored_at_10 += bool(new_rank is not None and new_rank <= FOUND_AT)

        old_hit = old_rank is not None and old_rank <= FOUND_AT
        new_hit = new_rank is not None and new_rank <= FOUND_AT
        if new_hit and not old_hit:
            accepted = candidates[new_rank - 1]
            flips.append({
                "case_id": row["case_id"],
                "query": row.get("query"),
                "expected_name": case.get("expected_name"),
                "accepted_name": accepted.get("name"),
                "accepted_id": accepted.get("id"),
                "accepted_distance_km": accepted.get("distance_km"),
                "rank": new_rank,
                "strata": row.get("strata"),
                # Every entry here is a claim that needs a human to agree.
                "audit": "PENDING",
            })
        elif old_hit and not new_hit:
            # Must be empty: the relaxed test is a strict superset of exact.
            regressions.append(row["case_id"])

    if regressions:
        raise SystemExit(
            "relaxed scoring lost cases it should strictly dominate "
            f"({regressions[:5]}); the matcher is not a superset -- fix that "
            "before trusting any number here"
        )

    n = len(rows)
    return {
        "schema": RESCORE_SCHEMA,
        "mode": mode,
        "rationale": CONTAINMENT_NAME_MATCH_RATIONALE,
        "run_meta": run.get("meta", {}),
        "cases": n,
        "baseline": {
            "name_match": run.get("meta", {}).get("name_match", "exact"),
            "found_at_1": baseline_at_1,
            "found_at_10": baseline_at_10,
            "recall_at_1": round(baseline_at_1 / n, 3),
            "recall_at_10": round(baseline_at_10 / n, 3),
        },
        "rescored": {
            "name_match": mode,
            "found_at_1": rescored_at_1,
            "found_at_10": rescored_at_10,
            "recall_at_1": round(rescored_at_1 / n, 3),
            "recall_at_10": round(rescored_at_10 / n, 3),
        },
        "delta_at_10": rescored_at_10 - baseline_at_10,
        "flips_pending_audit": flips,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="results JSON to re-score")
    parser.add_argument("--cases", action="append", required=True,
                        help="cases JSON the run was executed from; repeatable")
    parser.add_argument("--mode", choices=NAME_MATCH_MODES,
                        default="containment")
    parser.add_argument("--output", help="write the full report JSON here")
    args = parser.parse_args(argv)

    run = json.loads(Path(args.run).read_text())
    report = rescore(run, load_cases(args.cases), args.mode)

    base, new = report["baseline"], report["rescored"]
    print(f"cases                {report['cases']}")
    print(f"baseline  ({base['name_match']:>11})  "
          f"@1 {base['found_at_1']:>3}  @10 {base['found_at_10']:>3}  "
          f"r@10 {base['recall_at_10']}")
    print(f"rescored  ({new['name_match']:>11})  "
          f"@1 {new['found_at_1']:>3}  @10 {new['found_at_10']:>3}  "
          f"r@10 {new['recall_at_10']}")
    print(f"delta @10            {report['delta_at_10']:+d}")
    print()
    if not report["flips_pending_audit"]:
        print("No cases flipped. The exact-name test was not costing this run "
              "anything, and the miss pile is a retrieval problem.")
    else:
        print(f"{len(report['flips_pending_audit'])} cases flipped and REQUIRE "
              "AUDIT before the rescored number may be quoted:")
        for flip in report["flips_pending_audit"]:
            print(f"  [{flip['case_id']}] rank {flip['rank']} "
                  f"{flip['accepted_distance_km']} km")
            print(f"      expected: {flip['expected_name']}")
            print(f"      accepted: {flip['accepted_name']}")

    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
        print(f"\nReport written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
