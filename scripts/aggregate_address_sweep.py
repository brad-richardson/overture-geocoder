#!/usr/bin/env python3
"""Fan-in aggregation for the stratified address R2 map-reduce sweep.

Reads the committed selection document plus every per-task evidence file
downloaded from the matrix jobs, and emits one summary artifact (JSON +
markdown) covering, per task:

* structured-retention %          (map selected_rows / input_rows)
* fragment bytes                  (map fragment total)
* map / reduce wall seconds       (map + reduce resources.elapsed_seconds)
* peak RSS bytes                  (max of map / reduce peak_rss_bytes)
* retry read amplification        (resume-measurement)
* output bytes per retained row   (fragment bytes / selected_rows)
* local_oracle_match              (restored reduce byte-identity)

plus min / median / p95 / max across the completed tasks and a reconciliation
of projected rows against the inventory's per-task expectations.

Partial failure is expected and must not lose completed tasks: a task whose
evidence is missing or incomplete is reported with ``status != "complete"`` and
excluded from the distribution, while every task that finished is aggregated.
The distribution files are always written, even when nothing completed.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

SCHEMA = "overture-address-sweep-aggregate-v1"

# Numeric per-task metrics summarised into a min/median/p95/max distribution.
DISTRIBUTION_METRICS: tuple[tuple[str, str], ...] = (
    ("structured_retention_pct", "Retention %"),
    ("fragment_bytes", "Fragment bytes"),
    ("map_wall_seconds", "Map wall s"),
    ("reduce_wall_seconds", "Reduce wall s"),
    ("peak_rss_bytes", "Peak RSS bytes"),
    ("retry_read_amplification", "Retry amp"),
    ("output_bytes_per_retained_row", "B/retained row"),
)


def _find_one(evidence_dir: Path, filename: str) -> Path | None:
    """Locate a per-task evidence file regardless of download subdir layout."""
    matches = sorted(evidence_dir.rglob(filename))
    return matches[0] if matches else None


def _load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolated percentile over a sorted copy of ``values``."""
    if not values:
        raise ValueError("percentile of empty sequence")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _distribution(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": ordered[0],
        "median": _percentile(ordered, 50),
        "p95": _percentile(ordered, 95),
        "max": ordered[-1],
    }


def aggregate_task(
    name: str, expected: dict[str, Any], evidence_dir: Path
) -> dict[str, Any]:
    """Build one per-task record; tolerant of missing/partial evidence."""
    result: dict[str, Any] = {
        "name": name,
        "task_index": expected["task_index"],
        "stratum": expected.get("stratum"),
        "expected_rows": expected.get("expected_rows"),
        "expected_selected_compressed_bytes": expected.get(
            "expected_selected_compressed_bytes"
        ),
        "status": "complete",
        "missing": [],
    }

    map_report = _load_json(_find_one(evidence_dir, f"map-{name}.json"))
    measurement = _load_json(
        _find_one(evidence_dir, f"resume-measurement-{name}.json")
    )
    projection = _load_json(_find_one(evidence_dir, f"projection-{name}.json"))
    restored = _load_json(_find_one(evidence_dir, f"restored-reduce-{name}.json"))
    local_reduce = _load_json(_find_one(evidence_dir, f"local-reduce-{name}.json"))

    for label, report in (
        ("map", map_report),
        ("resume-measurement", measurement),
        ("projection", projection),
        ("restored-reduce", restored),
    ):
        if report is None:
            result["missing"].append(label)

    # --- retention + fragment bytes + map resources (map report) ------------
    if map_report is not None:
        fragments = map_report.get("map_fragments", {})
        input_rows = fragments.get("input_rows")
        selected_rows = fragments.get("selected_rows")
        fragment_bytes = fragments.get("bytes")
        result["input_rows"] = input_rows
        result["selected_rows"] = selected_rows
        result["fragment_bytes"] = fragment_bytes
        if input_rows:
            result["structured_retention_pct"] = 100.0 * selected_rows / input_rows
        if selected_rows:
            result["output_bytes_per_retained_row"] = fragment_bytes / selected_rows
        map_res = map_report.get("resources", {})
        result["map_wall_seconds"] = map_res.get("elapsed_seconds")
        map_rss = map_res.get("peak_rss_bytes")
    else:
        map_rss = None

    # --- reduce resources (prefer the resumed/restored reduce) --------------
    reduce_report = restored or local_reduce
    reduce_rss = None
    if reduce_report is not None:
        reduce_res = reduce_report.get("resources", {})
        result["reduce_wall_seconds"] = reduce_res.get("elapsed_seconds")
        reduce_rss = reduce_res.get("peak_rss_bytes")

    rss_values = [v for v in (map_rss, reduce_rss) if isinstance(v, (int, float))]
    if rss_values:
        result["peak_rss_bytes"] = max(rss_values)

    # --- retry amplification + oracle match (measurement / reduce) ----------
    if measurement is not None:
        result["retry_read_amplification"] = measurement.get(
            "retry_readback_amplification"
        )
        if "fragment_bytes" not in result or result.get("fragment_bytes") is None:
            result["fragment_bytes"] = measurement.get("fragment_bytes")

    oracle_match = None
    if restored is not None:
        oracle_match = restored.get("local_oracle_match")
    elif measurement is not None:
        oracle_match = measurement.get("local_oracle_match")
    result["local_oracle_match"] = oracle_match

    # --- reconciliation: projected rows vs inventory expectation ------------
    projected_rows = None
    if projection is not None:
        projected_rows = projection.get("selection", {}).get("rows")
    result["projected_rows"] = projected_rows
    expected_rows = expected.get("expected_rows")
    if projected_rows is not None and expected_rows is not None:
        result["rows_reconciled"] = projected_rows == expected_rows
        result["rows_delta"] = projected_rows - expected_rows
    else:
        result["rows_reconciled"] = None
        result["rows_delta"] = None

    # --- completeness verdict -----------------------------------------------
    required = (
        "structured_retention_pct",
        "fragment_bytes",
        "map_wall_seconds",
        "reduce_wall_seconds",
        "peak_rss_bytes",
        "retry_read_amplification",
        "output_bytes_per_retained_row",
    )
    if result["missing"] or any(result.get(k) is None for k in required):
        result["status"] = "incomplete"
    if oracle_match is False:
        result["status"] = "oracle-mismatch"

    return result


def aggregate(selection: dict[str, Any], evidence_dir: Path) -> dict[str, Any]:
    tasks = selection.get("tasks", [])
    per_task = [aggregate_task(t["name"], t, evidence_dir) for t in tasks]

    complete = [t for t in per_task if t["status"] == "complete"]
    distributions: dict[str, dict[str, float] | None] = {}
    for key, _ in DISTRIBUTION_METRICS:
        values = [
            float(t[key])
            for t in complete
            if isinstance(t.get(key), (int, float))
        ]
        distributions[key] = _distribution(values)

    oracle_matches = [t for t in per_task if t.get("local_oracle_match") is True]
    oracle_mismatches = [t for t in per_task if t.get("local_oracle_match") is False]
    reconciled = [t for t in per_task if t.get("rows_reconciled") is True]
    reconcile_failures = [t for t in per_task if t.get("rows_reconciled") is False]

    return {
        "schema": SCHEMA,
        "release": selection.get("release"),
        "task_count": len(per_task),
        "completed_count": len(complete),
        "incomplete_count": len(per_task) - len(complete),
        "all_local_oracle_match": (
            len(oracle_matches) == len(per_task) and len(per_task) > 0
        ),
        "oracle_mismatch_tasks": [t["name"] for t in oracle_mismatches],
        "rows_reconciled_count": len(reconciled),
        "rows_reconcile_failure_tasks": [t["name"] for t in reconcile_failures],
        "incomplete_tasks": [
            {"name": t["name"], "status": t["status"], "missing": t["missing"]}
            for t in per_task
            if t["status"] != "complete"
        ],
        "distributions": distributions,
        "tasks": per_task,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "NO"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def render_markdown(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Stratified address sweep aggregate")
    lines.append("")
    lines.append(f"- Release: `{summary.get('release')}`")
    lines.append(
        f"- Tasks completed: **{summary['completed_count']}/{summary['task_count']}**"
    )
    lines.append(
        f"- Byte-identical local oracle across all tasks: "
        f"**{_fmt(summary['all_local_oracle_match'])}**"
    )
    if summary["oracle_mismatch_tasks"]:
        lines.append(
            f"- Oracle MISMATCH tasks: {', '.join(summary['oracle_mismatch_tasks'])}"
        )
    lines.append(
        f"- Rows reconciled vs inventory: "
        f"**{summary['rows_reconciled_count']}/{summary['task_count']}**"
    )
    if summary["rows_reconcile_failure_tasks"]:
        lines.append(
            f"- Row reconciliation FAILURES: "
            f"{', '.join(summary['rows_reconcile_failure_tasks'])}"
        )
    if summary["incomplete_tasks"]:
        lines.append("- Incomplete tasks:")
        for item in summary["incomplete_tasks"]:
            miss = ", ".join(item["missing"]) or "resource fields"
            lines.append(f"  - `{item['name']}` ({item['status']}): missing {miss}")
    lines.append("")

    lines.append("## Per-task metrics")
    lines.append("")
    header = (
        "| task | idx | stratum | status | retention % | frag bytes | "
        "map s | reduce s | peak RSS | retry amp | B/row | oracle |"
    )
    lines.append(header)
    lines.append("|" + "---|" * 12)
    for t in summary["tasks"]:
        lines.append(
            "| {name} | {idx} | {stratum} | {status} | {ret} | {frag} | "
            "{maps} | {reds} | {rss} | {amp} | {bpr} | {oracle} |".format(
                name=t["name"],
                idx=t["task_index"],
                stratum=t.get("stratum") or "",
                status=t["status"],
                ret=_fmt(t.get("structured_retention_pct")),
                frag=_fmt(t.get("fragment_bytes")),
                maps=_fmt(t.get("map_wall_seconds")),
                reds=_fmt(t.get("reduce_wall_seconds")),
                rss=_fmt(t.get("peak_rss_bytes")),
                amp=_fmt(t.get("retry_read_amplification")),
                bpr=_fmt(t.get("output_bytes_per_retained_row")),
                oracle=_fmt(t.get("local_oracle_match")),
            )
        )
    lines.append("")

    lines.append("## Distribution across completed tasks")
    lines.append("")
    lines.append("| metric | min | median | p95 | max | n |")
    lines.append("|---|---|---|---|---|---|")
    for key, label in DISTRIBUTION_METRICS:
        dist = summary["distributions"].get(key)
        if dist is None:
            lines.append(f"| {label} | n/a | n/a | n/a | n/a | 0 |")
        else:
            lines.append(
                f"| {label} | {_fmt(dist['min'])} | {_fmt(dist['median'])} | "
                f"{_fmt(dist['p95'])} | {_fmt(dist['max'])} | {dist['count']} |"
            )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    args = parser.parse_args(argv)

    selection = json.loads(args.selection.read_text())
    summary = aggregate(selection, args.evidence_dir)

    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.write_text(render_markdown(summary) + "\n")

    print(
        f"aggregated {summary['completed_count']}/{summary['task_count']} tasks; "
        f"all_local_oracle_match={summary['all_local_oracle_match']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
