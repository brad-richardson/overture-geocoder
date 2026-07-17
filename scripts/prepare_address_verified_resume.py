#!/usr/bin/env python3
"""Map and deterministically reduce real address fragments around verified R2 restore."""

from __future__ import annotations

import argparse
import json
import resource
import shutil
import sys
import time
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from experiment_address_reduce import (  # noqa: E402
    AddressReduceArtifact,
    build_artifact,
    build_fragments,
    sha256_file,
)


MAP_SCHEMA = "overture-address-verified-resume-map-v1"
REDUCE_SCHEMA = "overture-address-verified-resume-reduce-v1"
STORE_SCHEMA = "overture-verified-shuffle-manifest-v1"


def peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value * 1024 if value < 10_000_000 else value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def map_real_fragments(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    input_bytes = args.input.stat().st_size
    disk_before = shutil.disk_usage(args.work_dir)
    if disk_before.free < args.max_workspace_bytes:
        raise ValueError("free disk is below configured workspace reservation")
    report = build_fragments(
        args.input,
        args.fragment_dir,
        fragment_rows=args.fragment_rows,
        max_rows=args.max_rows,
        max_workspace_bytes=args.max_workspace_bytes,
        input_bytes=input_bytes,
    )
    fragment_bytes = sum(item["bytes"] for item in report["fragments"])
    payload = {
        "schema": MAP_SCHEMA,
        "input": {
            "path": str(args.input),
            "bytes": input_bytes,
            "sha256": sha256_file(args.input),
        },
        "map_fragments": {**report, "bytes": fragment_bytes},
        "resources": {
            "elapsed_seconds": time.monotonic() - started,
            "peak_rss_bytes": peak_rss_bytes(),
            "disk_free_before": disk_before.free,
            "disk_free_after": shutil.disk_usage(args.work_dir).free,
        },
        "limits": {
            "max_rows": args.max_rows,
            "fragment_rows": args.fragment_rows,
            "max_workspace_bytes": args.max_workspace_bytes,
        },
    }
    write_json(args.json_out, payload)
    return payload


def restored_paths(
    map_report: dict[str, Any], restore_report_path: Path | None
) -> list[dict[str, Any]]:
    expected = map_report["map_fragments"]["fragments"]
    if restore_report_path is None:
        return expected
    restored = json.loads(restore_report_path.read_text())
    if restored.get("schema") != STORE_SCHEMA:
        raise ValueError("unsupported verified-store restore report")
    by_name = {Path(item["path"]).name: item for item in restored["artifacts"]}
    if len(by_name) != len(restored["artifacts"]):
        raise ValueError("restored fragment basenames are not unique")
    result = []
    for fragment in expected:
        name = Path(fragment["path"]).name
        item = by_name.get(name)
        if item is None:
            raise ValueError(f"restored manifest omits map fragment: {name}")
        if not item.get("verified") or (
            item.get("bytes"), item.get("sha256")
        ) != (fragment["bytes"], fragment["sha256"]):
            raise ValueError(f"restored fragment identity differs: {name}")
        result.append({**fragment, "path": item["path"]})
    if len(result) != len(by_name):
        raise ValueError("restore report contains an unexpected fragment")
    return result


def reduce_fragments(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    map_report = json.loads(args.map_report.read_text())
    if map_report.get("schema") != MAP_SCHEMA:
        raise ValueError("unsupported address map report")
    fragments = restored_paths(map_report, args.restore_report)
    disk_before = shutil.disk_usage(args.work_dir)
    if disk_before.free < args.max_workspace_bytes:
        raise ValueError("free disk is below configured workspace reservation")
    reduce_report = build_artifact(
        fragments,
        args.output,
        source=map_report["map_fragments"]["source"],
        sparse_stride=args.sparse_stride,
        max_artifact_bytes=args.max_artifact_bytes,
        max_workspace_bytes=args.max_workspace_bytes,
        input_bytes=0,
    )
    with AddressReduceArtifact(args.output) as artifact:
        verification = artifact.verify(reduce_report["verification_groups"])
    payload = {
        "schema": REDUCE_SCHEMA,
        "source_inventory_sha256": map_report["map_fragments"]["source"][
            "source_inventory_sha256"
        ],
        "fragment_count": len(fragments),
        "fragment_bytes": sum(item["bytes"] for item in fragments),
        "restored": args.restore_report is not None,
        "reduce": {
            **reduce_report,
            "path": str(args.output),
            "bytes": args.output.stat().st_size,
            "sha256": sha256_file(args.output),
            "verification": verification,
        },
        "resources": {
            "elapsed_seconds": time.monotonic() - started,
            "peak_rss_bytes": peak_rss_bytes(),
            "disk_free_before": disk_before.free,
            "disk_free_after": shutil.disk_usage(args.work_dir).free,
        },
        "limits": {
            "sparse_stride": args.sparse_stride,
            "max_artifact_bytes": args.max_artifact_bytes,
            "max_workspace_bytes": args.max_workspace_bytes,
        },
    }
    if args.expected_report is not None:
        expected = json.loads(args.expected_report.read_text())
        if expected.get("schema") != REDUCE_SCHEMA:
            raise ValueError("unsupported expected address reduce report")
        identity = (payload["reduce"]["bytes"], payload["reduce"]["sha256"])
        expected_identity = (
            expected["reduce"]["bytes"],
            expected["reduce"]["sha256"],
        )
        if identity != expected_identity:
            raise ValueError("restored reduce artifact differs from the local oracle")
        payload["local_oracle_match"] = True
    write_json(args.json_out, payload)
    return payload


def positive(values: list[int]) -> None:
    if min(values) <= 0:
        raise SystemExit("all limits must be positive")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    map_parser = subparsers.add_parser("map")
    map_parser.add_argument("--input", type=Path, required=True)
    map_parser.add_argument("--fragment-dir", type=Path, required=True)
    map_parser.add_argument("--json-out", type=Path, required=True)
    map_parser.add_argument("--work-dir", type=Path, default=Path("/tmp"))
    map_parser.add_argument("--max-rows", type=int, default=4_000_000)
    map_parser.add_argument("--fragment-rows", type=int, default=128_000)
    map_parser.add_argument("--max-workspace-bytes", type=int, default=12_000_000_000)

    reduce_parser = subparsers.add_parser("reduce")
    reduce_parser.add_argument("--map-report", type=Path, required=True)
    reduce_parser.add_argument("--restore-report", type=Path)
    reduce_parser.add_argument("--expected-report", type=Path)
    reduce_parser.add_argument("--output", type=Path, required=True)
    reduce_parser.add_argument("--json-out", type=Path, required=True)
    reduce_parser.add_argument("--work-dir", type=Path, default=Path("/tmp"))
    reduce_parser.add_argument("--sparse-stride", type=int, default=256)
    reduce_parser.add_argument("--max-artifact-bytes", type=int, default=1_000_000_000)
    reduce_parser.add_argument("--max-workspace-bytes", type=int, default=12_000_000_000)

    args = parser.parse_args()
    if args.command == "map":
        positive([args.max_rows, args.fragment_rows, args.max_workspace_bytes])
        payload = map_real_fragments(args)
    else:
        positive(
            [
                args.sparse_stride,
                args.max_artifact_bytes,
                args.max_workspace_bytes,
            ]
        )
        payload = reduce_fragments(args)
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
