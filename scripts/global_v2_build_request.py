#!/usr/bin/env python3
"""Build and validate an undispatched global v2 family-build request.

The request is the immutable hand-off between preparation and the future data
plane.  It pins every input and lineage decision that can affect stable shard
ownership or the global Places head.  This module remains control-plane only:
it does not read Overture, write R2, rebuild a shard, or publish a catalog.
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
import experiment_places_head_repack as places_head  # noqa: E402
import experiment_places_locality_head as locality_head  # noqa: E402
import global_build_manifest as manifests  # noqa: E402
import places_partition  # noqa: E402
import v2_release_manifest  # noqa: E402


SCHEMA = "overture-global-v2-build-request-v2"
RELEASE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SAFE_POLICY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
WORLD = (-180.0, -90.0, 180.0, 90.0)

# These describe behavior already implemented by the compact Places producer.
# They are deliberately not free-form request inputs: changing one requires a
# producer/reader change and, therefore, a new contract implementation.
HEAD_RANKING = "quantized-confidence-stable-serving-order-v1"
HEAD_PREFIX_POLICY = "normalized-token-prefix-lengths-v1"
DEFAULT_SOURCE_TASK_LIMIT = 128
DEFAULT_REDUCE_JOB_LIMIT = 256


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _require_release(value: Any, field: str) -> str:
    if not isinstance(value, str) or not RELEASE_RE.fullmatch(value):
        raise ValueError(f"{field} must use YYYY-MM-DD.N")
    return value


def _require_build(value: Any, field: str) -> str:
    if not isinstance(value, str) or not v2_release_manifest.BUILD_RE.fullmatch(value):
        raise ValueError(f"{field} must use YYYY-MM-DD.N")
    return value


def _require_slice(value: Any) -> str:
    if not isinstance(value, str) or not v2_release_manifest.SLICE_RE.fullmatch(value):
        raise ValueError("slice_version must use slice-YYYY-MM-DD.N")
    return value


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _require_optional_sha256(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _require_sha256(value, field)


def _require_safe_key(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is required")
    if value.startswith("/") or any(
        not part
        or part in {".", ".."}
        or not v2_release_manifest.KEY_COMPONENT_RE.fullmatch(part)
        for part in value.split("/")
    ):
        raise ValueError(f"{field} must be a canonical relative object key")
    return value


def _require_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def _require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def build_request(
    *,
    overture_release: str,
    geocoder_build: str,
    slice_version: str,
    legacy_core_version: str,
    legacy_core_overture_release: str,
    legacy_core_manifest_key: str,
    legacy_core_manifest_sha256: str,
    addresses_inventory_sha256: str,
    addresses_schema_fingerprint_sha256: str,
    addresses_predecessor_family_manifest_sha256: str | None,
    places_inventory_sha256: str,
    places_schema_fingerprint_sha256: str,
    places_predecessor_family_manifest_sha256: str | None,
    producer_commit: str,
    address_maximum_hash_bits: int = address_partition.DEFAULT_MAXIMUM_HASH_BITS,
    address_split_row_cap: int = address_partition.DEFAULT_SHARD_ROW_CAP,
    places_minimum_level: int = places_partition.DEFAULT_MINIMUM_LEVEL,
    places_maximum_level: int = places_partition.DEFAULT_MAXIMUM_LEVEL,
    places_split_row_cap: int = places.DEFAULT_SHARD_ROW_CAP,
    head_minimum_candidates: int = places.DEFAULT_HEAD_MINIMUM_CANDIDATES,
    head_famous_cap: int = places.DEFAULT_HEAD_FAMOUS_CAP,
    source_task_limit: int = DEFAULT_SOURCE_TASK_LIMIT,
    reduce_job_limit: int = DEFAULT_REDUCE_JOB_LIMIT,
) -> dict[str, Any]:
    overture_release = _require_release(overture_release, "overture_release")
    geocoder_build = _require_build(geocoder_build, "geocoder_build")
    slice_version = _require_slice(slice_version)
    legacy_core_version = _require_build(legacy_core_version, "legacy_core_version")
    legacy_core_overture_release = _require_release(
        legacy_core_overture_release, "legacy_core_overture_release"
    )
    if legacy_core_overture_release != overture_release:
        raise ValueError(
            "legacy_core_overture_release must match overture_release for v2 composition"
        )
    legacy_core_manifest_key = _require_safe_key(
        legacy_core_manifest_key, "legacy_core_manifest_key"
    )
    expected_legacy_key = f"{legacy_core_version}/release-manifest.json"
    if legacy_core_manifest_key != expected_legacy_key:
        raise ValueError("legacy_core_manifest_key must match legacy_core_version")
    legacy_core_manifest_sha256 = _require_sha256(
        legacy_core_manifest_sha256, "legacy_core_manifest_sha256"
    )

    addresses_inventory_sha256 = _require_sha256(
        addresses_inventory_sha256, "addresses_inventory_sha256"
    )
    addresses_schema_fingerprint_sha256 = _require_sha256(
        addresses_schema_fingerprint_sha256,
        "addresses_schema_fingerprint_sha256",
    )
    addresses_predecessor_family_manifest_sha256 = _require_optional_sha256(
        addresses_predecessor_family_manifest_sha256,
        "addresses_predecessor_family_manifest_sha256",
    )
    places_inventory_sha256 = _require_sha256(
        places_inventory_sha256, "places_inventory_sha256"
    )
    places_schema_fingerprint_sha256 = _require_sha256(
        places_schema_fingerprint_sha256, "places_schema_fingerprint_sha256"
    )
    places_predecessor_family_manifest_sha256 = _require_optional_sha256(
        places_predecessor_family_manifest_sha256,
        "places_predecessor_family_manifest_sha256",
    )
    if not isinstance(producer_commit, str) or not COMMIT_RE.fullmatch(producer_commit):
        raise ValueError("producer_commit must be a full lowercase Git commit SHA")

    address_maximum_hash_bits = _require_int(
        address_maximum_hash_bits,
        "address_maximum_hash_bits",
        minimum=1,
        maximum=address_partition.MAXIMUM_SUPPORTED_HASH_BITS,
    )
    address_split_row_cap = _require_int(
        address_split_row_cap,
        "address_split_row_cap",
        minimum=1,
        maximum=2**63 - 1,
    )
    places_minimum_level = _require_int(
        places_minimum_level,
        "places_minimum_level",
        minimum=1,
        maximum=places_partition.MAX_SUPPORTED_LEVEL,
    )
    places_maximum_level = _require_int(
        places_maximum_level,
        "places_maximum_level",
        minimum=1,
        maximum=places_partition.MAX_SUPPORTED_LEVEL,
    )
    places_partition.validate_levels(places_minimum_level, places_maximum_level)
    places_split_row_cap = _require_int(
        places_split_row_cap,
        "places_split_row_cap",
        minimum=1,
        maximum=2**63 - 1,
    )
    head_minimum_candidates = _require_int(
        head_minimum_candidates,
        "head_minimum_candidates",
        minimum=1,
        maximum=2**31 - 1,
    )
    head_famous_cap = _require_int(
        head_famous_cap,
        "head_famous_cap",
        minimum=0,
        maximum=2**31 - 1,
    )
    source_task_limit = _require_int(
        source_task_limit, "source_task_limit", minimum=1, maximum=128
    )
    reduce_job_limit = _require_int(
        reduce_job_limit, "reduce_job_limit", minimum=1, maximum=256
    )
    for policy, field in (
        (places_head.HEAD_ADMISSION_MARKER, "head admission"),
        (HEAD_RANKING, "head ranking"),
        (HEAD_PREFIX_POLICY, "head prefix policy"),
    ):
        if not SAFE_POLICY_RE.fullmatch(policy):
            raise RuntimeError(f"producer {field} is not a safe version marker")

    return {
        "schema": SCHEMA,
        "overture_release": overture_release,
        "geocoder_build": geocoder_build,
        "slice_version": slice_version,
        "producer_commit": producer_commit,
        "legacy_core": {
            "version": legacy_core_version,
            "overture_release": legacy_core_overture_release,
            "manifest_key": legacy_core_manifest_key,
            "manifest_sha256": legacy_core_manifest_sha256,
        },
        "scope": {
            "kind": "families-only",
            "coverage": list(WORLD),
            "artifact_prefix": f"{slice_version}/",
            "reuse_legacy_core": True,
            "rebuild_divisions": False,
            "publication": "separate-explicit-step",
        },
        "families": {
            "addresses": {
                "source": {
                    "inventory_sha256": addresses_inventory_sha256,
                    "schema_fingerprint_sha256": addresses_schema_fingerprint_sha256,
                },
                "predecessor_family_manifest_sha256": (
                    addresses_predecessor_family_manifest_sha256
                ),
                "partition": {
                    "scheme": address_partition.PARTITION_SCHEME,
                    "maximum_hash_bits": address_maximum_hash_bits,
                    "split_row_cap": address_split_row_cap,
                    "sticky_splits": True,
                },
                "versions": {
                    "format": manifests.ADDRESS_FORMAT_VERSION,
                    "normalization": address_partition.NORMALIZATION_VERSION,
                },
                "operations": ["structured_forward"],
                "required_artifacts": [
                    "families/addresses/address-collection.json",
                    "families/addresses/partition-plan.json",
                    "families/addresses/shards/*.adat",
                    "families/addresses/shards/*.aidx",
                ],
            },
            "places": {
                "source": {
                    "inventory_sha256": places_inventory_sha256,
                    "schema_fingerprint_sha256": places_schema_fingerprint_sha256,
                },
                "predecessor_family_manifest_sha256": (
                    places_predecessor_family_manifest_sha256
                ),
                "partition": {
                    "scheme": places_partition.PARTITION_SCHEME,
                    "minimum_level": places_minimum_level,
                    "maximum_level": places_maximum_level,
                    "split_row_cap": places_split_row_cap,
                    "sticky_splits": True,
                },
                "versions": {
                    "format": manifests.PLACES_FORMAT_VERSION,
                    "tokenizer": manifests.PLACES_TOKENIZER_VERSION,
                },
                "global_head": {
                    "format": places_head.MAGIC.decode(),
                    "admission": places_head.HEAD_ADMISSION_MARKER,
                    "ranking": HEAD_RANKING,
                    "minimum_candidates": head_minimum_candidates,
                    "famous_cap": head_famous_cap,
                    "result_cap": places_head.HEAD_LIMIT,
                    "prefix_policy": {
                        "version": HEAD_PREFIX_POLICY,
                        "lengths": list(locality_head.HEAD_PREFIX_LENGTHS),
                    },
                    "provenance": {
                        "predecessor_family_manifest_sha256": (
                            places_predecessor_family_manifest_sha256
                        )
                    },
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
            "source_task_limit": source_task_limit,
            "reduce_job_limit": reduce_job_limit,
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
    legacy = _require_object(value.get("legacy_core"), "legacy_core")
    families = _require_object(value.get("families"), "families")
    addresses = _require_object(families.get("addresses"), "families.addresses")
    address_source = _require_object(
        addresses.get("source"), "families.addresses.source"
    )
    address_partition_value = _require_object(
        addresses.get("partition"), "families.addresses.partition"
    )
    places_value = _require_object(families.get("places"), "families.places")
    places_source = _require_object(
        places_value.get("source"), "families.places.source"
    )
    places_partition_value = _require_object(
        places_value.get("partition"), "families.places.partition"
    )
    global_head = _require_object(
        places_value.get("global_head"), "families.places.global_head"
    )
    execution = _require_object(value.get("execution"), "execution")

    expected = build_request(
        overture_release=value.get("overture_release"),
        geocoder_build=value.get("geocoder_build"),
        slice_version=value.get("slice_version"),
        legacy_core_version=legacy.get("version"),
        legacy_core_overture_release=legacy.get("overture_release"),
        legacy_core_manifest_key=legacy.get("manifest_key"),
        legacy_core_manifest_sha256=legacy.get("manifest_sha256"),
        addresses_inventory_sha256=address_source.get("inventory_sha256"),
        addresses_schema_fingerprint_sha256=address_source.get(
            "schema_fingerprint_sha256"
        ),
        addresses_predecessor_family_manifest_sha256=addresses.get(
            "predecessor_family_manifest_sha256"
        ),
        places_inventory_sha256=places_source.get("inventory_sha256"),
        places_schema_fingerprint_sha256=places_source.get("schema_fingerprint_sha256"),
        places_predecessor_family_manifest_sha256=places_value.get(
            "predecessor_family_manifest_sha256"
        ),
        producer_commit=value.get("producer_commit"),
        address_maximum_hash_bits=address_partition_value.get("maximum_hash_bits"),
        address_split_row_cap=address_partition_value.get("split_row_cap"),
        places_minimum_level=places_partition_value.get("minimum_level"),
        places_maximum_level=places_partition_value.get("maximum_level"),
        places_split_row_cap=places_partition_value.get("split_row_cap"),
        head_minimum_candidates=global_head.get("minimum_candidates"),
        head_famous_cap=global_head.get("famous_cap"),
        source_task_limit=execution.get("source_task_limit"),
        reduce_job_limit=execution.get("reduce_job_limit"),
    )
    if value != expected:
        raise ValueError("build request differs from the current producer contract")
    return value


def _add_build_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--overture-release", required=True)
    parser.add_argument("--geocoder-build", required=True)
    parser.add_argument("--slice-version", required=True)
    parser.add_argument("--legacy-core-version", required=True)
    parser.add_argument("--legacy-core-overture-release", required=True)
    parser.add_argument("--legacy-core-manifest-key", required=True)
    parser.add_argument("--legacy-core-manifest-sha256", required=True)
    parser.add_argument("--addresses-inventory-sha256", required=True)
    parser.add_argument("--addresses-schema-fingerprint-sha256", required=True)
    parser.add_argument("--addresses-predecessor-family-manifest-sha256")
    parser.add_argument("--places-inventory-sha256", required=True)
    parser.add_argument("--places-schema-fingerprint-sha256", required=True)
    parser.add_argument("--places-predecessor-family-manifest-sha256")
    parser.add_argument("--producer-commit", required=True)
    parser.add_argument(
        "--address-maximum-hash-bits",
        type=int,
        default=address_partition.DEFAULT_MAXIMUM_HASH_BITS,
    )
    parser.add_argument(
        "--address-split-row-cap",
        type=int,
        default=address_partition.DEFAULT_SHARD_ROW_CAP,
    )
    parser.add_argument(
        "--places-minimum-level",
        type=int,
        default=places_partition.DEFAULT_MINIMUM_LEVEL,
    )
    parser.add_argument(
        "--places-maximum-level",
        type=int,
        default=places_partition.DEFAULT_MAXIMUM_LEVEL,
    )
    parser.add_argument(
        "--places-split-row-cap", type=int, default=places.DEFAULT_SHARD_ROW_CAP
    )
    parser.add_argument(
        "--head-minimum-candidates",
        type=int,
        default=places.DEFAULT_HEAD_MINIMUM_CANDIDATES,
    )
    parser.add_argument(
        "--head-famous-cap", type=int, default=places.DEFAULT_HEAD_FAMOUS_CAP
    )
    parser.add_argument(
        "--source-task-limit", type=int, default=DEFAULT_SOURCE_TASK_LIMIT
    )
    parser.add_argument(
        "--reduce-job-limit", type=int, default=DEFAULT_REDUCE_JOB_LIMIT
    )
    parser.add_argument("--output", type=Path, required=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build")
    _add_build_arguments(build)

    validate = commands.add_parser("validate")
    validate.add_argument("--request", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "build":
        values = vars(args).copy()
        values.pop("command")
        output = values.pop("output")
        request = build_request(**values)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(canonical_json(request))
        print(json.dumps(request, indent=2, sort_keys=True))
        return

    request = json.loads(args.request.read_text())
    validate_request(request)
    print(json.dumps({"valid": True, "schema": SCHEMA}, sort_keys=True))


if __name__ == "__main__":
    main()
