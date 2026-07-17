#!/usr/bin/env python3
"""Deterministic stratified task selection for the address R2 map-reduce sweep.

The sweep exercises the fixed 127-task address row-group inventory
(``benchmarks/address-rowgroup-inventory-report.json``). Rather than hand-type
task indices, this script derives a twelve-task stratified sample from the
inventory using explicit, deterministic per-stratum rules and emits:

* the canonical selection file (``.github/address-sweep-selection.json``),
  committed to the repo and used as the workflow's default matrix, and
* the GitHub Actions matrix payload (``{"include": [...]}``) consumed by the
  rehearsal workflow via ``fromJSON``.

Every rule is a pure function of the checked-in inventory, so re-running
``generate`` on the same inventory reproduces byte-identical output. The
companion unit test pins the resulting indices against the checked-in
inventory.

Strata (see docs/plans/2026-07-17-address-stratified-sweep.md):

* continuity anchors   -- the two already-measured regression references:
                          the global max-compressed-bytes task (US) and the
                          largest-by-rows Mexico task.
* CJK Japan            -- the first Japan-dominant task (largest, mixed) plus a
                          low-bytes-per-row pure-Japan exemplar, contrasting the
                          ~36 B/row Latin tasks with CJK compression.
* CJK traditional      -- the first Taiwan-dominant task.
* Latin high-density   -- the first Brazil-dominant task.
* Latin Europe         -- the first Italy/France/Germany-dominant task each.
* sparse tail          -- the global minimum-rows task (small-task overhead).
* mixed/unknown        -- the global maximum mixed-country-share task.
* US mid-range         -- the first pure-US task other than the US anchor.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

SCHEMA = "overture-address-sweep-selection-v1"
INVENTORY_SCHEMA = "overture-address-rowgroup-inventory-v1"

# Countries whose "first dominant task" is taken verbatim as the stratum
# representative, in the deterministic matrix order used for tie-free output.
FIRST_DOMINANT_STRATA: tuple[tuple[str, str, str], ...] = (
    ("BR", "brazil-full", "latin-high-density"),
    ("FR", "france-full", "latin-europe"),
    ("IT", "italy-full", "latin-europe"),
    ("DE", "germany-full", "latin-europe"),
    ("TW", "taiwan-full", "cjk-traditional"),
    ("JP", "japan-full", "cjk-japan"),
)


class SelectionError(ValueError):
    """Raised when the inventory cannot yield a valid twelve-task selection."""


def load_inventory(path: Path) -> dict[str, Any]:
    report = json.loads(path.read_text())
    if report.get("schema") != INVENTORY_SCHEMA:
        raise SelectionError(
            f"unexpected inventory schema {report.get('schema')!r}; "
            f"expected {INVENTORY_SCHEMA!r}"
        )
    tasks = report.get("plan", {}).get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise SelectionError("inventory plan.tasks is missing or empty")
    return report


def dominant_country(task: dict[str, Any]) -> str | None:
    exact = task.get("exact_country_rows") or {}
    if not exact:
        return None
    # Deterministic argmax: highest rows, ties broken by country code.
    return max(exact.items(), key=lambda kv: (kv[1], kv[0]))[0]


def mixed_share(task: dict[str, Any]) -> float:
    rows = task["rows"]
    return task["mixed_or_unknown_country_rows"] / rows if rows else 0.0


def bytes_per_row(task: dict[str, Any]) -> float:
    rows = task["rows"]
    return task["selected_compressed_bytes"] / rows if rows else 0.0


def is_pure(task: dict[str, Any], country: str) -> bool:
    """True when every counted row of the task is exactly ``country``."""
    exact = task.get("exact_country_rows") or {}
    return (
        len(exact) == 1
        and country in exact
        and task["mixed_or_unknown_country_rows"] == 0
        and exact[country] == task["rows"]
    )


def _first_dominant(tasks: list[dict[str, Any]], country: str) -> dict[str, Any]:
    candidates = [t for t in tasks if dominant_country(t) == country]
    if not candidates:
        raise SelectionError(f"no task is dominated by {country}")
    return min(candidates, key=lambda t: t["index"])


def _entry(task: dict[str, Any], name: str, stratum: str, rationale: str) -> dict[str, Any]:
    return {
        "name": name,
        "task_index": task["index"],
        "stratum": stratum,
        "rationale": rationale,
        "expected_rows": task["rows"],
        "expected_selected_compressed_bytes": task["selected_compressed_bytes"],
    }


def select_tasks(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the twelve stratified selection entries, ordered by task_index."""
    tasks = report["plan"]["tasks"]

    entries: list[dict[str, Any]] = []

    # --- continuity anchors --------------------------------------------------
    us_anchor = max(
        tasks, key=lambda t: (t["selected_compressed_bytes"], -t["index"])
    )
    if dominant_country(us_anchor) != "US":
        raise SelectionError(
            "the global max-compressed-bytes task is no longer US-dominant; "
            "the anchor rule needs review"
        )
    entries.append(
        _entry(
            us_anchor,
            "us-full",
            "continuity-anchor",
            "global maximum selected_compressed_bytes; already-measured US "
            "regression reference",
        )
    )

    mx_tasks = [t for t in tasks if dominant_country(t) == "MX"]
    if not mx_tasks:
        raise SelectionError("no Mexico-dominant task available for the anchor")
    mx_anchor = max(mx_tasks, key=lambda t: (t["rows"], -t["index"]))
    entries.append(
        _entry(
            mx_anchor,
            "mexico-full",
            "continuity-anchor",
            "largest-by-rows Mexico-dominant task; already-measured MX "
            "regression reference",
        )
    )

    # --- sparse tail (global minimum rows) -----------------------------------
    sparse = min(tasks, key=lambda t: (t["rows"], t["index"]))
    entries.append(
        _entry(
            sparse,
            "sparse-tail",
            "sparse-tail",
            "global minimum-rows task; exercises small-task fixed overhead",
        )
    )

    # --- mixed / unknown country (global maximum mixed share) ----------------
    mixed = max(tasks, key=lambda t: (mixed_share(t), t["index"]))
    entries.append(
        _entry(
            mixed,
            "mixed-country",
            "mixed-unknown",
            "global maximum mixed/unknown-country row share; stresses the "
            "country partition rule",
        )
    )

    # --- US mid-range (first pure-US task other than the anchor) -------------
    us_pure = [
        t
        for t in tasks
        if is_pure(t, "US") and t["index"] != us_anchor["index"]
    ]
    if not us_pure:
        raise SelectionError("no pure-US task available for the US mid-range stratum")
    us_mid = min(us_pure, key=lambda t: t["index"])
    entries.append(
        _entry(
            us_mid,
            "us-mid",
            "us-mid-range",
            "first pure-US task other than the anchor; separates US-anchor "
            "effects from US-general behaviour",
        )
    )

    # --- first-dominant single-country strata --------------------------------
    for country, name, stratum in FIRST_DOMINANT_STRATA:
        task = _first_dominant(tasks, country)
        entries.append(
            _entry(
                task,
                name,
                stratum,
                f"first {country}-dominant task by index",
            )
        )

    # --- CJK Japan second exemplar (low bytes-per-row, pure Japan) -----------
    # Exclude the sparse-tail task (already selected) and the primary
    # japan-full task; among remaining pure-Japan tasks pick the second-lowest
    # bytes-per-row so the exemplar is a representative low-B/row CJK task
    # rather than the single most extreme outlier.
    japan_full_index = next(e["task_index"] for e in entries if e["name"] == "japan-full")
    taken = {e["task_index"] for e in entries}
    jp_pure = [
        t
        for t in tasks
        if is_pure(t, "JP")
        and t["index"] not in taken
        and t["index"] != japan_full_index
    ]
    if len(jp_pure) < 2:
        raise SelectionError("fewer than two pure-Japan tasks remain for the CJK pair")
    jp_pure_by_bpr = sorted(jp_pure, key=lambda t: (bytes_per_row(t), t["index"]))
    jp_second = jp_pure_by_bpr[1]
    entries.append(
        _entry(
            jp_second,
            "japan-pure",
            "cjk-japan",
            "pure-Japan task with the second-lowest bytes-per-row; low-B/row "
            "CJK exemplar contrasting Latin compression",
        )
    )

    _validate(entries, count=12)
    entries.sort(key=lambda e: e["task_index"])
    return entries


def _validate(entries: list[dict[str, Any]], *, count: int | None = None) -> None:
    if not entries:
        raise SelectionError("selection is empty")
    if count is not None and len(entries) != count:
        raise SelectionError(f"expected {count} selected tasks, got {len(entries)}")
    names = [e["name"] for e in entries]
    if len(set(names)) != len(names):
        raise SelectionError(f"duplicate matrix names: {sorted(names)}")
    indices = [e["task_index"] for e in entries]
    if len(set(indices)) != len(indices):
        raise SelectionError(f"duplicate task indices: {sorted(indices)}")
    for name in names:
        # Names become artifact-name suffixes and shell paths; keep them safe.
        if not name or not all(c.islower() or c.isdigit() or c == "-" for c in name):
            raise SelectionError(f"unsafe matrix name: {name!r}")


def build_selection_document(report: dict[str, Any]) -> dict[str, Any]:
    entries = select_tasks(report)
    # Provenance hash over the canonical selection input (plan.tasks) rather
    # than the raw inventory-file bytes: this stays stable under re-serialization
    # (whitespace, key order, line endings) so `check` only fails when the data
    # that actually drives selection changes.
    canonical_tasks = json.dumps(
        report["plan"]["tasks"], sort_keys=True, separators=(",", ":")
    )
    inventory_tasks_sha256 = hashlib.sha256(
        canonical_tasks.encode("utf-8")
    ).hexdigest()
    return {
        "schema": SCHEMA,
        "release": report.get("release"),
        "generated_from": "benchmarks/address-rowgroup-inventory-report.json",
        "inventory_tasks_sha256": inventory_tasks_sha256,
        "task_count": len(entries),
        "tasks": entries,
    }


def matrix_from_selection(selection: dict[str, Any]) -> dict[str, Any]:
    """Strip a selection document down to the GitHub Actions matrix payload."""
    tasks = selection.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise SelectionError("selection document has no tasks")
    include = []
    for task in tasks:
        name = task["name"]
        index = task["task_index"]
        if (
            not isinstance(name, str)
            or isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
        ):
            raise SelectionError(f"invalid selection entry: {task!r}")
        include.append({"name": name, "task_index": index})
    _validate(include, count=12)
    return {"include": include}


def validate_override(raw: str) -> dict[str, Any]:
    """Parse and validate a workflow_dispatch matrix override as pure data.

    Accepts either a bare ``[...]`` include list or a ``{"include": [...]}``
    object of ``{"name": str, "task_index": int}`` entries. Never evaluated as
    shell.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SelectionError(f"override is not valid JSON: {exc}") from exc
    if isinstance(parsed, dict):
        include = parsed.get("include")
    elif isinstance(parsed, list):
        include = parsed
    else:
        raise SelectionError("override must be a JSON array or {'include': [...]}")
    if not isinstance(include, list) or not include:
        raise SelectionError("override include list is empty")
    normalized = []
    for entry in include:
        if not isinstance(entry, dict):
            raise SelectionError(f"override entry is not an object: {entry!r}")
        extra = set(entry) - {"name", "task_index"}
        if extra:
            raise SelectionError(f"override entry has unexpected keys: {sorted(extra)}")
        name = entry.get("name")
        index = entry.get("task_index")
        if not isinstance(name, str) or not name:
            raise SelectionError(f"override entry has an invalid name: {entry!r}")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise SelectionError(f"override entry has an invalid task_index: {entry!r}")
        normalized.append({"name": name, "task_index": index})
    _validate(normalized)
    return {"include": normalized}


def _cmd_generate(args: argparse.Namespace) -> int:
    report = load_inventory(args.inventory_report)
    document = build_selection_document(report)
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.output is None or str(args.output) == "-":
        sys.stdout.write(text)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    report = load_inventory(args.inventory_report)
    expected = build_selection_document(report)
    actual = json.loads(args.selection.read_text())
    if actual != expected:
        sys.stderr.write(
            "committed selection is stale; regenerate with "
            "`python scripts/select_address_sweep_tasks.py generate`\n"
        )
        return 1
    return 0


def _cmd_matrix(args: argparse.Namespace) -> int:
    if args.override_json is not None and args.override_json.strip():
        matrix = validate_override(args.override_json)
    else:
        selection = json.loads(args.selection.read_text())
        matrix = matrix_from_selection(selection)
    # Compact, single-line JSON for GitHub Actions step outputs.
    text = json.dumps(matrix, separators=(",", ":"), sort_keys=True)
    if args.output is None or str(args.output) == "-":
        sys.stdout.write(text + "\n")
    else:
        args.output.write_text(text + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="compute and emit the selection document")
    gen.add_argument("--inventory-report", type=Path, required=True)
    gen.add_argument(
        "--output",
        type=Path,
        default=None,
        help="output path (default: stdout); use the committed selection path",
    )
    gen.set_defaults(func=_cmd_generate)

    chk = sub.add_parser(
        "check", help="verify a committed selection matches the current inventory"
    )
    chk.add_argument("--inventory-report", type=Path, required=True)
    chk.add_argument("--selection", type=Path, required=True)
    chk.set_defaults(func=_cmd_check)

    mat = sub.add_parser(
        "matrix", help="emit the GitHub Actions matrix payload for the workflow"
    )
    mat.add_argument("--selection", type=Path, required=True)
    mat.add_argument(
        "--override-json",
        type=str,
        default=None,
        help="optional workflow_dispatch matrix override, validated as data",
    )
    mat.add_argument("--output", type=Path, default=None)
    mat.set_defaults(func=_cmd_matrix)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SelectionError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
