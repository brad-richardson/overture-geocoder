#!/usr/bin/env python3
"""Validate one semantic address reduce artifact and build Worker page objects."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from address_partition import address_key_hash  # noqa: E402
from build_address_collection import validate_plan  # noqa: E402
from experiment_address_compression import run as build_pages  # noqa: E402
from experiment_address_reduce import AddressReduceArtifact, sha256_file  # noqa: E402


REPORT_SCHEMA = "overture-address-serving-shard-v1"


def validate_reduce_partition(
    path: Path, leaf: dict[str, Any], *, overture_release: str
) -> dict[str, Any]:
    with AddressReduceArtifact(path) as artifact:
        source = artifact.header.get("source")
        if (
            not isinstance(source, dict)
            or source.get("release") != overture_release
            or source.get("family") != "addresses"
        ):
            raise ValueError("address reduce source differs from the partition plan")
        if artifact.header["records"] != leaf["rows"]:
            raise ValueError("address reduce rows differ from the partition plan")
        offset = 0
        rows = 0
        previous: tuple[str, ...] | None = None
        while offset < artifact.header["record_bytes"]:
            record, offset = artifact._record_at(offset)
            key = record["key"]
            if previous is not None and key < previous:
                raise ValueError("address reduce artifact is not sorted")
            if key[0] != leaf["country"]:
                raise ValueError("address reduce artifact crosses its country partition")
            hashed = address_key_hash(key[:8])
            if not leaf["hash_start"] <= hashed <= leaf["hash_end"]:
                raise ValueError("address reduce artifact crosses its hash partition")
            previous = key
            rows += 1
        if rows != leaf["rows"]:
            raise ValueError("address reduce partition scan does not reconcile")
    return {"rows": rows, "sorted": True, "partition_membership": True}


def build_shard(
    input_path: Path,
    output_dir: Path,
    plan: dict[str, Any],
    *,
    identifier: str,
    page_rows: int = 256,
    max_input_bytes: int = 1_000_000_000,
    max_output_bytes: int = 1_000_000_000,
    max_workspace_bytes: int = 6_000_000_000,
) -> dict[str, Any]:
    partitions = validate_plan(plan)
    matches = [item for item in partitions if item["id"] == identifier]
    if len(matches) != 1:
        raise ValueError("address partition ID is absent or duplicated in the plan")
    leaf = matches[0]
    if leaf["rows"] == 0:
        raise ValueError("empty address ranges do not have serving artifacts")
    verification = validate_reduce_partition(
        input_path, leaf, overture_release=plan["overture_release"]
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    index_path = output_dir / f"{identifier}.aidx"
    data_path = output_dir / f"{identifier}.adat"
    if index_path.exists() or data_path.exists():
        raise ValueError("address serving shard outputs are create-only")
    with tempfile.TemporaryDirectory(prefix=f".{identifier}-", dir=output_dir) as name:
        temporary = Path(name)
        compression = build_pages(
            input_path,
            temporary,
            page_rows=page_rows,
            planning_rows=plan["totals"]["retained_rows"],
            max_input_bytes=max_input_bytes,
            max_output_bytes=max_output_bytes,
            max_workspace_bytes=max_workspace_bytes,
            variant_names=["useful_gzip"],
        )
        os.link(temporary / "useful_gzip.idx", index_path)
        try:
            os.link(temporary / "useful_gzip.bin", data_path)
        except BaseException:
            index_path.unlink(missing_ok=True)
            raise
    report = {
        "schema": REPORT_SCHEMA,
        "overture_release": plan["overture_release"],
        "normalization_version": plan["normalization_version"],
        "partition": leaf,
        "source": {
            "path": str(input_path),
            "bytes": input_path.stat().st_size,
            "sha256": sha256_file(input_path),
        },
        "verification": verification,
        "page_format": {
            "format": 2,
            "variant": "useful_gzip",
            "page_rows": page_rows,
            "pages": compression["pages"],
            "candidate_groups_never_cross_pages": compression["oracle"][
                "candidate_groups_never_cross_pages"
            ],
            "indexed_candidate_sets_verified": compression["oracle"][
                "indexed_candidate_sets_verified"
            ],
        },
        "artifacts": {
            "index": {
                "path": str(index_path),
                "bytes": index_path.stat().st_size,
                "sha256": sha256_file(index_path),
            },
            "data": {
                "path": str(data_path),
                "bytes": data_path.stat().st_size,
                "sha256": sha256_file(data_path),
            },
        },
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--partition-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--page-rows", type=int, default=256)
    parser.add_argument("--max-input-bytes", type=int, default=1_000_000_000)
    parser.add_argument("--max-output-bytes", type=int, default=1_000_000_000)
    parser.add_argument("--max-workspace-bytes", type=int, default=6_000_000_000)
    args = parser.parse_args()
    report = build_shard(
        args.input,
        args.output_dir,
        json.loads(args.plan.read_text()),
        identifier=args.partition_id,
        page_rows=args.page_rows,
        max_input_bytes=args.max_input_bytes,
        max_output_bytes=args.max_output_bytes,
        max_workspace_bytes=args.max_workspace_bytes,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report["artifacts"], sort_keys=True))


if __name__ == "__main__":
    main()
