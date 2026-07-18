#!/usr/bin/env python3
"""Drive the bbox-scoped ("region") address rehearsal over #106's scoped plan.

This is control-plane glue for the region mode of the address map/reduce
rehearsal workflow. It never reads Overture feature data, uploads to R2, or
touches a production catalog: it consumes the JSON that the run-time scoped
inventory (``inventory_address_rowgroups.py`` with ``--xmin/--ymin/--xmax/
--ymax``) and the per-task map/reduce/upload steps already produced, and it
emits deterministic control-plane artifacts.

Subcommands
-----------
``matrix``
    Turn a bbox-scoped inventory report into a GitHub Actions matrix over
    *every* scoped task and a scoped-plan evidence summary. Enforces a hard
    task cap: a scoped plan larger than the cap FAILS the run rather than
    silently sampling a subset.

``task-rows``
    Extract one task's measured input-row count from its verified-resume map
    report, tagged with its plan task index, for later exact reconciliation.

``reconcile``
    Sum the per-task measured rows and assert they equal the plan's
    ``bbox_scoped_rows`` exactly, over the complete set of plan task indices.
    A missing, duplicated, or extra task fails the reconciliation.

``manifest``
    Build the release-versioned #107 family manifest (family ``addresses``,
    ``bbox_scope: row_group_approximate``) over the uploaded reduce outputs and
    verify it against the isolated-prefix object listing with
    ``verify_family_manifest_against_listing``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import global_build_manifest as gbm  # noqa: E402

INVENTORY_SCHEMA = "overture-address-rowgroup-inventory-v1"
MAP_SCHEMA = "overture-address-verified-resume-map-v1"
STORE_SCHEMA = "overture-verified-shuffle-manifest-v1"
PLAN_SUMMARY_SCHEMA = "overture-address-region-rehearsal-plan-v1"
TASK_ROWS_SCHEMA = "overture-address-region-task-rows-v1"
RECONCILE_SCHEMA = "overture-address-region-reconcile-v1"

FAMILY = "addresses"
BBOX_SCOPE = "row_group_approximate"
PRODUCER_SCRIPT = "scripts/prepare_address_verified_resume.py"
PRODUCER_VERSION = "overture-address-verified-resume-reduce-v1"


class RegionRehearsalError(ValueError):
    """Raised when the scoped plan or its evidence cannot be trusted."""


def _load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text())


def load_scoped_report(path: Path) -> dict[str, Any]:
    """Load a bbox-scoped inventory report and validate it carries a region plan."""
    report = _load_json(path)
    if not isinstance(report, dict) or report.get("schema") != INVENTORY_SCHEMA:
        raise RegionRehearsalError(
            f"inventory schema must be {INVENTORY_SCHEMA!r}; got {report.get('schema')!r}"
            if isinstance(report, dict)
            else "inventory report must be a JSON object"
        )
    plan = report.get("plan")
    if not isinstance(plan, dict):
        raise RegionRehearsalError("inventory report is missing its plan")
    if plan.get("bbox_scope") != BBOX_SCOPE:
        raise RegionRehearsalError(
            "region mode requires a bbox-scoped plan "
            f"(bbox_scope={BBOX_SCOPE!r}); got {plan.get('bbox_scope')!r}. "
            "Generate the inventory with --xmin/--ymin/--xmax/--ymax."
        )
    for key in ("bbox", "bbox_row_groups", "bbox_scoped_rows", "tasks", "task_count"):
        if key not in plan:
            raise RegionRehearsalError(f"scoped plan is missing {key!r}")
    return report


def plan_task_indices(plan: dict[str, Any]) -> list[int]:
    return [task["index"] for task in plan["tasks"]]


def build_region_matrix(
    report: dict[str, Any], *, max_tasks: int, name_prefix: str = "region"
) -> dict[str, Any]:
    """Return the GitHub Actions matrix over every scoped task.

    Every scoped task is included; there is no sampling. A scoped plan with more
    than ``max_tasks`` tasks raises, so the workflow fails loudly instead of
    quietly dropping tasks.
    """
    if max_tasks <= 0:
        raise RegionRehearsalError("--max-tasks must be positive")
    plan = report["plan"]
    task_count = plan["task_count"]
    if task_count != len(plan["tasks"]):
        raise RegionRehearsalError("scoped plan task_count disagrees with tasks[]")
    if task_count == 0:
        raise RegionRehearsalError(
            "scoped plan is empty: the bbox selected no row groups"
        )
    if task_count > max_tasks:
        raise RegionRehearsalError(
            f"scoped plan has {task_count} tasks, exceeding the hard cap of "
            f"{max_tasks}; refusing to sample. Raise --max-tasks deliberately or "
            "shrink the region box."
        )
    include = [
        {"name": f"{name_prefix}-{position:03d}", "task_index": task["index"]}
        for position, task in enumerate(plan["tasks"])
    ]
    names = [entry["name"] for entry in include]
    indices = [entry["task_index"] for entry in include]
    if len(set(names)) != len(names) or len(set(indices)) != len(indices):
        raise RegionRehearsalError("scoped matrix produced duplicate names or indices")
    return {"include": include}


def scoped_plan_summary(
    report: dict[str, Any], *, region_name: str, max_tasks: int
) -> dict[str, Any]:
    plan = report["plan"]
    return {
        "schema": PLAN_SUMMARY_SCHEMA,
        "release": report.get("release"),
        "region": {
            "name": region_name,
            "bbox": plan["bbox"],
            "bbox_scope": plan["bbox_scope"],
        },
        "row_groups": plan["bbox_row_groups"],
        "task_count": plan["task_count"],
        "max_tasks": max_tasks,
        "bbox_scoped_rows": plan["bbox_scoped_rows"],
        "task_rows": plan["task_rows"],
        "task_selected_uncompressed_bytes": plan["task_selected_uncompressed_bytes"],
    }


def task_rows_entry(
    map_report: dict[str, Any], *, task_index: int, name: str
) -> dict[str, Any]:
    if not isinstance(map_report, dict) or map_report.get("schema") != MAP_SCHEMA:
        raise RegionRehearsalError(f"map report schema must be {MAP_SCHEMA!r}")
    fragments = map_report.get("map_fragments")
    if not isinstance(fragments, dict) or "input_rows" not in fragments:
        raise RegionRehearsalError("map report is missing map_fragments.input_rows")
    measured = fragments["input_rows"]
    if not isinstance(measured, int) or isinstance(measured, bool) or measured < 0:
        raise RegionRehearsalError("map_fragments.input_rows must be a non-negative int")
    if not isinstance(task_index, int) or task_index < 0:
        raise RegionRehearsalError("task_index must be a non-negative int")
    if not name:
        raise RegionRehearsalError("task name is required")
    return {
        "schema": TASK_ROWS_SCHEMA,
        "task_index": task_index,
        "name": name,
        "measured_rows": measured,
    }


def reconcile_rows(
    report: dict[str, Any], task_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Reconcile per-task measured rows against the plan's ``bbox_scoped_rows``.

    Exactness has two parts: the reconciled task indices must be *precisely* the
    plan's task indices (no missing, duplicate, or extra task), and the measured
    rows must sum to ``bbox_scoped_rows``. Either failure raises.
    """
    plan = report["plan"]
    expected_indices = set(plan_task_indices(plan))
    observed_indices: set[int] = set()
    measured_total = 0
    for entry in task_rows:
        if not isinstance(entry, dict) or entry.get("schema") != TASK_ROWS_SCHEMA:
            raise RegionRehearsalError(f"task-rows schema must be {TASK_ROWS_SCHEMA!r}")
        index = entry.get("task_index")
        measured = entry.get("measured_rows")
        if not isinstance(index, int) or isinstance(index, bool):
            raise RegionRehearsalError("task-rows entry has a non-integer task_index")
        if not isinstance(measured, int) or isinstance(measured, bool) or measured < 0:
            raise RegionRehearsalError("task-rows entry has an invalid measured_rows")
        if index in observed_indices:
            raise RegionRehearsalError(f"duplicate task_index in reconciliation: {index}")
        observed_indices.add(index)
        measured_total += measured
    missing = sorted(expected_indices - observed_indices)
    extra = sorted(observed_indices - expected_indices)
    if missing or extra:
        raise RegionRehearsalError(
            f"reconciliation task set differs from plan: missing={missing}, extra={extra}"
        )
    scoped_rows = plan["bbox_scoped_rows"]
    if measured_total != scoped_rows:
        raise RegionRehearsalError(
            f"measured rows {measured_total} do not equal plan bbox_scoped_rows "
            f"{scoped_rows}"
        )
    return {
        "schema": RECONCILE_SCHEMA,
        "release": report.get("release"),
        "task_count": plan["task_count"],
        "reconciled_tasks": len(task_rows),
        "bbox_scoped_rows": scoped_rows,
        "measured_rows": measured_total,
        "reconciled": True,
    }


def synth_build_id(
    *, release: str, region_name: str, bbox: list[float], run_id: str
) -> str:
    """A deterministic 64-hex build id for a rehearsal run's family manifest."""
    return gbm.digest(
        {
            "rehearsal": "address-region",
            "release": release,
            "region": region_name,
            "bbox": list(bbox),
            "run_id": run_id,
        }
    )


def _artifacts_from_upload_reports(
    reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for report in reports:
        if not isinstance(report, dict) or report.get("schema") != STORE_SCHEMA:
            raise RegionRehearsalError(
                f"upload report schema must be {STORE_SCHEMA!r}"
            )
        for artifact in report.get("artifacts", []):
            key = artifact.get("key")
            if not isinstance(key, str) or not key:
                raise RegionRehearsalError("upload artifact is missing its key")
            if key in seen:
                raise RegionRehearsalError(f"duplicate reduce-output key: {key}")
            seen.add(key)
            artifacts.append(
                {
                    "object_key": key,
                    "bytes": artifact["bytes"],
                    "sha256": artifact["sha256"],
                }
            )
    if not artifacts:
        raise RegionRehearsalError("no reduce outputs found in the upload reports")
    return artifacts


def build_and_verify_manifest(
    *,
    release: str,
    region_name: str,
    bbox: list[float],
    build_id: str,
    producer_commit: str,
    upload_reports: list[dict[str, Any]],
    listing: dict[str, Any],
    generated_at: str | None,
) -> dict[str, Any]:
    """Assemble and verify the region's addresses family manifest.

    The manifest artifacts are the uploaded reduce outputs; the listing is what
    the isolated R2 prefix actually holds. ``verify_family_manifest_against_
    listing`` fails on any missing, unexpected, size, or hash discrepancy.
    """
    artifacts = _artifacts_from_upload_reports(upload_reports)
    lineage = {
        "overture_release": release,
        "build_id": build_id,
        "producer_commit": producer_commit,
        "producer_script": PRODUCER_SCRIPT,
        "producer_version": PRODUCER_VERSION,
    }
    versions = {
        "format": gbm.ADDRESS_FORMAT_VERSION,
        "tokenizer": None,
        "normalization": gbm.ADDRESS_NORMALIZATION_VERSION,
    }
    region = {
        "name": region_name,
        "bbox": list(bbox),
        "bbox_scope": BBOX_SCOPE,
    }
    manifest = gbm.build_family_manifest(
        FAMILY,
        lineage=lineage,
        versions=versions,
        region=region,
        artifacts=artifacts,
        generated_at=generated_at,
    )
    observed = _normalize_listing(listing, artifacts)
    gbm.verify_family_manifest_against_listing(manifest, observed)
    return manifest


def _normalize_listing(
    listing: dict[str, Any], artifacts: list[dict[str, Any]]
) -> dict[str, tuple[int, str]]:
    """Join an object-key -> size listing with the reduce outputs' hashes.

    R2's object listing carries key and size but not a SHA-256, so the hash for
    each observed key is taken from the readback-verified upload report. A key
    present in the listing with no matching upload artifact is surfaced with an
    empty hash, which ``verify_family_manifest_against_listing`` rejects as an
    unexpected object rather than silently passing.
    """
    if not isinstance(listing, dict):
        raise RegionRehearsalError("listing must be an object of key -> size")
    sha_by_key = {artifact["object_key"]: artifact["sha256"] for artifact in artifacts}
    observed: dict[str, tuple[int, str]] = {}
    for key, value in listing.items():
        if isinstance(value, (list, tuple)) and len(value) == 2:
            size, sha = int(value[0]), str(value[1])
        else:
            size = int(value)
            sha = sha_by_key.get(key, "")
        observed[key] = (size, sha)
    return observed


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _write(path: Path | None, text: str) -> None:
    if path is None or str(path) == "-":
        sys.stdout.write(text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _cmd_matrix(args: argparse.Namespace) -> int:
    report = load_scoped_report(args.scoped_report)
    matrix = build_region_matrix(report, max_tasks=args.max_tasks)
    summary = scoped_plan_summary(
        report, region_name=args.region_name, max_tasks=args.max_tasks
    )
    _write(args.matrix_out, json.dumps(matrix, separators=(",", ":"), sort_keys=True) + "\n")
    if args.summary_out is not None:
        _write(args.summary_out, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 0


def _cmd_task_rows(args: argparse.Namespace) -> int:
    entry = task_rows_entry(
        _load_json(args.map_report), task_index=args.task_index, name=args.name
    )
    _write(args.out, json.dumps(entry, indent=2, sort_keys=True) + "\n")
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    report = load_scoped_report(args.scoped_report)
    paths = list(args.task_rows or [])
    if args.task_rows_dir is not None:
        paths.extend(sorted(Path(args.task_rows_dir).rglob("*.json")))
    if not paths:
        raise RegionRehearsalError("reconcile requires at least one task-rows file")
    entries = [_load_json(path) for path in paths]
    result = reconcile_rows(report, entries)
    _write(args.out, json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


def _cmd_manifest(args: argparse.Namespace) -> int:
    reports: list[dict[str, Any]] = [
        _load_json(path) for path in (args.upload_report or [])
    ]
    if args.upload_report_dir is not None:
        reports.extend(
            _load_json(path) for path in sorted(Path(args.upload_report_dir).rglob("*.json"))
        )
    if not reports:
        raise RegionRehearsalError("manifest requires at least one upload report")
    bbox = list(args.bbox)
    build_id = args.build_id or synth_build_id(
        release=args.release,
        region_name=args.region_name,
        bbox=bbox,
        run_id=args.run_id or "",
    )
    listing = _load_json(args.listing)
    manifest = build_and_verify_manifest(
        release=args.release,
        region_name=args.region_name,
        bbox=bbox,
        build_id=build_id,
        producer_commit=args.producer_commit,
        upload_reports=reports,
        listing=listing,
        generated_at=args.generated_at,
    )
    _write(args.out, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    sys.stderr.write(f"manifest_digest={manifest['manifest_digest']}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    matrix = sub.add_parser("matrix", help="emit the region matrix and plan summary")
    matrix.add_argument("--scoped-report", type=Path, required=True)
    matrix.add_argument("--region-name", required=True)
    matrix.add_argument("--max-tasks", type=int, required=True)
    matrix.add_argument("--matrix-out", type=Path, default=None)
    matrix.add_argument("--summary-out", type=Path, default=None)
    matrix.set_defaults(func=_cmd_matrix)

    task_rows = sub.add_parser("task-rows", help="extract one task's measured rows")
    task_rows.add_argument("--map-report", type=Path, required=True)
    task_rows.add_argument("--task-index", type=int, required=True)
    task_rows.add_argument("--name", required=True)
    task_rows.add_argument("--out", type=Path, default=None)
    task_rows.set_defaults(func=_cmd_task_rows)

    reconcile = sub.add_parser("reconcile", help="reconcile measured rows vs the plan")
    reconcile.add_argument("--scoped-report", type=Path, required=True)
    reconcile.add_argument("--task-rows", type=Path, action="append")
    reconcile.add_argument("--task-rows-dir", type=Path)
    reconcile.add_argument("--out", type=Path, default=None)
    reconcile.set_defaults(func=_cmd_reconcile)

    manifest = sub.add_parser("manifest", help="build and verify the family manifest")
    manifest.add_argument("--release", required=True)
    manifest.add_argument("--region-name", required=True)
    manifest.add_argument(
        "--bbox", nargs=4, type=float, required=True, metavar=("XMIN", "YMIN", "XMAX", "YMAX")
    )
    manifest.add_argument("--producer-commit", required=True)
    manifest.add_argument("--build-id")
    manifest.add_argument("--run-id")
    manifest.add_argument("--upload-report", type=Path, action="append")
    manifest.add_argument("--upload-report-dir", type=Path)
    manifest.add_argument("--listing", type=Path, required=True)
    manifest.add_argument("--generated-at")
    manifest.add_argument("--out", type=Path, default=None)
    manifest.set_defaults(func=_cmd_manifest)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RegionRehearsalError as error:
        sys.stderr.write(f"error: {error}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
