#!/usr/bin/env python3
"""Build and validate an undispatched global v2 family-build request.

This file captures the decisions that must not drift between the producer and
the Worker.  It is deliberately control-plane only: it does not read Overture,
write R2, rebuild a shard, or publish a catalog.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import address_partition  # noqa: E402
import build_places_region_shards as places  # noqa: E402
import global_build_manifest as manifests  # noqa: E402
import places_partition  # noqa: E402
import v2_release_manifest  # noqa: E402


SCHEMA = "overture-global-v2-build-request-v1"
RELEASE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
WORLD = (-180.0, -90.0, 180.0, 90.0)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def build_request(
    *, overture_release: str, geocoder_build: str, producer_commit: str
) -> dict[str, Any]:
    if not RELEASE_RE.fullmatch(overture_release):
        raise ValueError("overture_release must use YYYY-MM-DD.N")
    if not v2_release_manifest.BUILD_RE.fullmatch(geocoder_build):
        raise ValueError("geocoder_build must use YYYY-MM-DD.N")
    if not COMMIT_RE.fullmatch(producer_commit):
        raise ValueError("producer_commit must be a full lowercase Git commit SHA")

    return {
        "schema": SCHEMA,
        "overture_release": overture_release,
        "geocoder_build": geocoder_build,
        "producer_commit": producer_commit,
        "scope": {
            "kind": "families-only",
            "coverage": list(WORLD),
            "reuse_legacy_core": True,
            "rebuild_divisions": False,
            "publication": "separate-explicit-step",
        },
        "families": {
            "addresses": {
                "partition": {
                    "scheme": address_partition.PARTITION_SCHEME,
                    "maximum_hash_bits": address_partition.DEFAULT_MAXIMUM_HASH_BITS,
                    "split_row_cap": address_partition.DEFAULT_SHARD_ROW_CAP,
                    "sticky_splits": True,
                },
                "versions": {
                    "format": manifests.ADDRESS_FORMAT_VERSION,
                    "normalization": address_partition.NORMALIZATION_VERSION,
                },
                "operations": ["structured_forward"],
                "required_artifacts": [
                    "families/addresses/address-collection.json",
                    "families/addresses/shards/*.adat",
                    "families/addresses/shards/*.aidx",
                ],
            },
            "places": {
                "partition": {
                    "scheme": places_partition.PARTITION_SCHEME,
                    "minimum_level": places_partition.DEFAULT_MINIMUM_LEVEL,
                    "maximum_level": places_partition.DEFAULT_MAXIMUM_LEVEL,
                    "split_row_cap": places.DEFAULT_SHARD_ROW_CAP,
                    "sticky_splits": True,
                },
                "versions": {
                    "format": manifests.PLACES_FORMAT_VERSION,
                    "tokenizer": manifests.PLACES_TOKENIZER_VERSION,
                },
                "operations": ["forward"],
                "required_artifacts": [
                    "families/places/catalog.pcat",
                    "families/places/head.phrp",
                    "families/places/q-*.pcsh",
                ],
            },
        },
        "execution": {
            "state": "prepared-not-dispatched",
            "source_task_limit": 128,
            "reduce_job_limit": 256,
            "intermediate_store": "r2-unpublished-content-addressed",
            "compute_tasks_are_not_shard_ids": True,
            "required_phases": [
                "pin-and-inventory",
                "measure-stable-partition-counts",
                "plan-stable-serving-partitions",
                "map-content-addressed-fragments",
                "reduce-worker-readable-shards",
                "build-global-places-head",
                "verify-remote-object-set",
                "finalize-families-only-slice",
            ],
        },
    }


def validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError(f"build request schema must be {SCHEMA}")
    expected = build_request(
        overture_release=value.get("overture_release", ""),
        geocoder_build=value.get("geocoder_build", ""),
        producer_commit=value.get("producer_commit", ""),
    )
    if value != expected:
        raise ValueError("build request differs from the current producer contract")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build")
    build.add_argument("--overture-release", required=True)
    build.add_argument("--geocoder-build", required=True)
    build.add_argument("--producer-commit", required=True)
    build.add_argument("--output", type=Path, required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("--request", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "build":
        request = build_request(
            overture_release=args.overture_release,
            geocoder_build=args.geocoder_build,
            producer_commit=args.producer_commit,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(canonical_json(request))
        print(json.dumps(request, indent=2, sort_keys=True))
        return

    request = json.loads(args.request.read_text())
    validate_request(request)
    print(json.dumps({"valid": True, "schema": SCHEMA}, sort_keys=True))


if __name__ == "__main__":
    main()
