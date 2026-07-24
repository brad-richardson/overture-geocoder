#!/usr/bin/env python3
"""Fail-closed scratch gate for the Places construction-v1 map stage.

The dense Japan map tasks (~13.6-13.8M emitted terms) historically breached the
8 GiB map-stage scratch cap by holding the hydrated IPC, the term IPC, the
DuckDB `terms` table, a second sorted `packed` DuckDB table, and all per-pack
files at once. After the staged-deletion + single on-disk sorted-copy hygiene
fix (the term IPC and hydrated stream are unlinked as soon as they are consumed,
the second full copy is a compact zstd parquet instead of a DuckDB table with
`terms` dropped the moment it exists, and each pack is unlinked right after it is
published), this tool runs the *real* ``places_construction_v1.map_task`` over a
dense projected task in a fresh, bounded process group, measures peak scratch
with the repo's ``run_bounded`` watchdog, and fails closed (nonzero exit) if peak
scratch exceeds the 8 GiB cap with the required 25% headroom.

No network access. Reads a local projected task parquet and the release
construction binaries; writes only local temporary artifacts under --scratch.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

# Mirrors the frozen evidence-spec acceptance_gates.resources values.
SCRATCH_HARD_CAP_BYTES = 8 * 1024**3
RSS_HARD_CAP_BYTES = 4 * 1024**3
RESOURCE_HEADROOM_MIN_FRACTION = 0.25
# A relaxed hard-kill so a genuine run completes and the true peak is observable;
# PASS/FAIL is still judged against the cap-with-headroom below, never this.
MEASURE_MAX_SCRATCH_BYTES = 16 * 1024**3


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


P = load("places_construction_v1_scratchgate", ROOT / "scripts/places_construction_v1.py")
A = P.A


def build_limits(module, *, wall_seconds: float, measure_scratch: int, memory_limit: str):
    return module.Limits(
        max_input_rows=1_000_000,
        max_pack_rows=500_000,
        parquet_row_group_rows=131_072,
        max_rss_bytes=RSS_HARD_CAP_BYTES,
        # The internal stage watchdogs must not kill before the parent observes
        # the true peak; judgement against the 8 GiB gate happens in the parent.
        max_scratch_bytes=measure_scratch,
        max_output_bytes=2 * 1024**3,
        wall_seconds=wall_seconds,
        duckdb_memory_limit=memory_limit,
        duckdb_threads=2,
        allow_unpinned_duckdb=True,
    )


def run_map_worker(args: argparse.Namespace) -> None:
    """Child entrypoint: run one real map_task and record its marker binding."""
    binaries = Path(args.binaries)
    store = A.LocalObjectStore(Path(args.scratch) / "store")
    limits = build_limits(
        P,
        wall_seconds=args.wall_seconds,
        measure_scratch=args.measure_max_scratch_bytes,
        memory_limit=args.memory_limit,
    )
    import hashlib

    task_id = args.task_id
    marker = P.map_task(
        input_path=Path(args.input),
        source_limits=Path(args.source_limits),
        store=store,
        scratch_root=Path(args.scratch) / "map",
        request_sha256=hashlib.sha256(task_id.encode()).hexdigest(),
        task_id=task_id,
        transform_binary=binaries / "places-transform-v1",
        proof_binary=binaries / "places-proof-directory",
        limits=limits,
    )
    Path(args.result).write_text(
        json.dumps(
            {
                "task_id": task_id,
                "binding": marker["binding"],
                "packs": len(marker["packs"]),
                "transform": {
                    "emitted_term_rows": marker["transform"]["emitted_term_rows"],
                    "admitted_features": marker["transform"].get("admitted_features"),
                },
                "construction_evidence": marker["construction_evidence"],
            },
            sort_keys=True,
        )
        + "\n"
    )


def measure(args: argparse.Namespace) -> int:
    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    result_path = scratch / "map-result.json"
    if result_path.exists():
        result_path.unlink()

    task_id = args.task_id or Path(args.input).stem.replace(".", "-")
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "map-worker",
        "--input",
        str(args.input),
        "--source-limits",
        str(args.source_limits),
        "--binaries",
        str(args.binaries),
        "--scratch",
        str(scratch),
        "--task-id",
        task_id,
        "--wall-seconds",
        str(args.wall_seconds),
        "--memory-limit",
        args.memory_limit,
        "--measure-max-scratch-bytes",
        str(args.measure_max_scratch_bytes),
        "--result",
        str(result_path),
    ]
    started = time.monotonic()
    breach: str | None = None
    evidence: dict[str, Any] | None = None
    try:
        evidence = A.run_bounded(
            command,
            scratch_roots=[scratch],
            limits=A.Limits(
                max_rss_bytes=args.measure_max_rss_bytes,
                max_scratch_bytes=args.measure_max_scratch_bytes,
                wall_seconds=args.wall_seconds + 60,
            ),
        )
    except RuntimeError as error:
        breach = str(error)

    gate_scratch = int(args.scratch_hard_cap_bytes * (1 - args.headroom))
    gate_rss = int(RSS_HARD_CAP_BYTES * (1 - args.headroom))
    result = json.loads(result_path.read_text()) if result_path.exists() else None

    report: dict[str, Any] = {
        "task_id": task_id,
        "input": str(args.input),
        "wall_seconds": round(time.monotonic() - started, 3),
        "scratch_hard_cap_bytes": args.scratch_hard_cap_bytes,
        "resource_headroom_min_fraction": args.headroom,
        "scratch_gate_bytes": gate_scratch,
        "rss_gate_bytes": gate_rss,
        "measured": evidence,
        "map_result": result,
    }

    reasons: list[str] = []
    if breach is not None:
        reasons.append(f"map worker killed at measurement hard cap: {breach}")
    if evidence is None:
        reasons.append("no measurement was recorded")
    else:
        peak_scratch = evidence["peak_scratch_bytes"]
        peak_rss = evidence["peak_rss_bytes"]
        report["peak_scratch_bytes"] = peak_scratch
        report["peak_rss_bytes"] = peak_rss
        report["scratch_headroom_bytes"] = gate_scratch - peak_scratch
        if peak_scratch > gate_scratch:
            reasons.append(
                f"peak scratch {peak_scratch} B exceeds the "
                f"{gate_scratch} B gate ({args.scratch_hard_cap_bytes} B cap, "
                f"{args.headroom:.0%} headroom)"
            )
        # RSS is measured and reported; on sandboxed macOS the child process-group
        # RSS can be under-sampled, so it is only gated when it exceeds the cap.
        if peak_rss > RSS_HARD_CAP_BYTES:
            reasons.append(f"peak RSS {peak_rss} B exceeds the {RSS_HARD_CAP_BYTES} B cap")
    if result is None:
        reasons.append("map task produced no marker binding")

    report["passed"] = not reasons
    report["reasons"] = reasons

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    sub = value.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Measure map-stage peak scratch and gate it.")
    run.add_argument("--input", required=True, help="Projected task parquet path.")
    run.add_argument("--source-limits", required=True)
    run.add_argument(
        "--binaries",
        default=str(ROOT / "crates/target/release"),
        help="Directory holding the release construction binaries.",
    )
    run.add_argument("--scratch", required=True)
    run.add_argument("--task-id", default="")
    run.add_argument("--wall-seconds", type=float, default=1200.0)
    run.add_argument("--memory-limit", default="3GB")
    run.add_argument("--scratch-hard-cap-bytes", type=int, default=SCRATCH_HARD_CAP_BYTES)
    run.add_argument("--headroom", type=float, default=RESOURCE_HEADROOM_MIN_FRACTION)
    run.add_argument(
        "--measure-max-scratch-bytes", type=int, default=MEASURE_MAX_SCRATCH_BYTES
    )
    run.add_argument("--measure-max-rss-bytes", type=int, default=12 * 1024**3)
    run.add_argument("--report", default=None)
    run.set_defaults(func=measure)

    worker = sub.add_parser("map-worker", help=argparse.SUPPRESS)
    worker.add_argument("--input", required=True)
    worker.add_argument("--source-limits", required=True)
    worker.add_argument("--binaries", required=True)
    worker.add_argument("--scratch", required=True)
    worker.add_argument("--task-id", required=True)
    worker.add_argument("--wall-seconds", type=float, required=True)
    worker.add_argument("--memory-limit", required=True)
    worker.add_argument("--measure-max-scratch-bytes", type=int, required=True)
    worker.add_argument("--result", required=True)
    worker.set_defaults(func=run_map_worker)
    return value


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.command == "map-worker":
        run_map_worker(arguments)
        return 0
    return arguments.func(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
