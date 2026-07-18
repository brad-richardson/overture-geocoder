#!/usr/bin/env python3
"""Plan and verify deterministic, resumable global data builds.

This is control-plane code only. It does not download Overture data, upload to
R2, or promote a production catalog.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


PLAN_SCHEMA = "overture-global-build-plan-v1"
SOURCE_INVENTORY_SCHEMA = "overture-global-source-inventory-v1"
MAP_COMPLETION_SCHEMA = "overture-global-map-completion-v1"
ARTIFACT_SCHEMA = "overture-global-artifact-v1"
CATALOG_SCHEMA = "overture-global-catalog-candidate-v1"
FAMILY_MANIFEST_SCHEMA = "overture-global-family-manifest-v1"
FAMILIES = {"addresses", "places"}
FAMILY_PATHS = {
    "addresses": ("addresses", "address"),
    "places": ("places", "place"),
}

# A family manifest scopes its build to a rough source bounding box. The box
# bounds the source scan, not a coverage promise: an out-of-box query is
# classified out-of-coverage, never not-found. Two scope modes exist:
#   row_group_approximate  addresses (PR #106) — pruning is row-group granular,
#                          so the served scope is a superset of the box.
#   exact                  places — experiment_places_partition_extract.py
#                          applies an exact bbox predicate at extract time.
BBOX_SCOPES = {"row_group_approximate", "exact"}

# Format/tokenizer/normalization identities referenced from the family
# producers (not invented here). The fan-in derives a manifest's `format` from
# the reduce artifacts themselves; these document the producer contract and
# back CLI defaults.
#   places  format    : scripts/experiment_places_compact_shard.py MAGIC
#                       b"PCSH0001" (directory schema_version 1).
#           tokenizer : scripts/experiment_places_compact_index.py
#                       TOKENIZER_VERSION = "nfkd-latin-fold-cjk-bigram-v2".
#   address format    : scripts/experiment_address_reduce.py FORMAT_VERSION = 2.
#           normalize : that module's normalize() — NFC, Unicode-whitespace
#                       collapse, ASCII-only lowercasing (the first format).
PLACES_FORMAT_VERSION = "PCSH0001"
PLACES_TOKENIZER_VERSION = "nfkd-latin-fold-cjk-bigram-v2"
ADDRESS_FORMAT_VERSION = "address-reduce-2"
ADDRESS_NORMALIZATION_VERSION = "nfc-uniws-collapse-ascii-lower-1"


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def require_hex_digest(value: str, field: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


def require_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def require_exact_fields(value: dict[str, Any], expected: set[str], kind: str) -> None:
    if set(value) != expected:
        raise ValueError(
            f"{kind} fields differ: missing={sorted(expected - set(value))}, "
            f"unknown={sorted(set(value) - expected)}"
        )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value))


def normalize_inventory(
    raw: Any, *, release: str, family: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(raw, dict) or raw.get("schema") != SOURCE_INVENTORY_SCHEMA:
        raise ValueError(f"inventory schema must be {SOURCE_INVENTORY_SCHEMA}")
    require_exact_fields(
        raw,
        {"schema", "release", "family", "theme", "type", "schema_version", "discovery", "objects"},
        "source inventory",
    )
    if raw["release"] != release or raw["family"] != family:
        raise ValueError("inventory release/family does not match requested build")
    expected_theme, expected_type = FAMILY_PATHS[family]
    if (raw["theme"], raw["type"]) != (expected_theme, expected_type):
        raise ValueError("inventory theme/type does not match family")
    if not isinstance(raw["schema_version"], str) or not raw["schema_version"]:
        raise ValueError("inventory schema_version is required")
    discovery = raw["discovery"]
    if not isinstance(discovery, dict):
        raise ValueError("inventory discovery must be an object")
    require_exact_fields(discovery, {"kind", "source"}, "inventory discovery")
    if discovery["kind"] not in {"overture-s3-listing", "test-fixture"}:
        raise ValueError("inventory discovery kind is not supported")
    if not isinstance(discovery["source"], str) or not discovery["source"]:
        raise ValueError("inventory discovery source is required")
    approved_prefix = (
        f"s3://overturemaps-us-west-2/release/{release}/"
        f"theme={expected_theme}/type={expected_type}/"
    )
    if discovery["kind"] == "overture-s3-listing" and discovery["source"] != approved_prefix:
        raise ValueError("production inventory discovery source is not the approved Overture prefix")
    objects = raw["objects"]
    if not isinstance(objects, list) or not objects:
        raise ValueError("inventory objects must be a non-empty JSON array")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    path_marker = f"/release/{release}/theme={expected_theme}/type={expected_type}/"
    for entry in objects:
        if not isinstance(entry, dict):
            raise ValueError("every inventory entry must be an object")
        unknown = set(entry) - {"uri", "etag", "bytes", "records", "row_groups", "sha256"}
        if unknown:
            raise ValueError(f"unknown inventory fields: {sorted(unknown)}")
        uri = entry.get("uri")
        etag = entry.get("etag")
        byte_count = entry.get("bytes")
        records = entry.get("records")
        row_groups = entry.get("row_groups")
        if not isinstance(uri, str) or not uri.startswith("s3://"):
            raise ValueError("inventory uri must be an s3:// URI")
        if path_marker not in uri:
            raise ValueError(f"inventory uri does not match release/family: {uri}")
        if discovery["kind"] == "overture-s3-listing" and not uri.startswith(approved_prefix):
            raise ValueError(f"production inventory uri is outside the approved Overture prefix: {uri}")
        if uri in seen:
            raise ValueError(f"duplicate inventory uri: {uri}")
        if not isinstance(etag, str) or not etag:
            raise ValueError(f"inventory etag is required for {uri}")
        require_int(byte_count, f"inventory bytes for {uri}", minimum=1)
        require_int(records, f"inventory records for {uri}", minimum=1)
        require_int(row_groups, f"inventory row_groups for {uri}", minimum=1)
        item = {
            "uri": uri,
            "etag": etag,
            "bytes": byte_count,
            "records": records,
            "row_groups": row_groups,
        }
        if "sha256" in entry:
            require_hex_digest(entry["sha256"], f"sha256 for {uri}")
            item["sha256"] = entry["sha256"]
        normalized.append(item)
        seen.add(uri)
    metadata = {key: raw[key] for key in ("schema", "release", "family", "theme", "type", "schema_version", "discovery")}
    return metadata, sorted(normalized, key=lambda item: item["uri"])


def stable_bucket(value: Any, count: int) -> int:
    return int(digest(value)[:16], 16) % count


def build_plan(
    inventory: Any,
    *,
    release: str,
    producer_commit: str,
    family: str,
    map_tasks: int,
    partitions: int,
) -> dict[str, Any]:
    if family not in FAMILIES:
        raise ValueError(f"family must be one of {sorted(FAMILIES)}")
    if not isinstance(release, str) or not release or not isinstance(producer_commit, str) or not producer_commit:
        raise ValueError("release and producer_commit are required")
    require_int(map_tasks, "map_tasks", minimum=1)
    require_int(partitions, "partitions", minimum=1)
    if map_tasks > 128:
        raise ValueError("map_tasks must be between 1 and 128")
    if partitions > 256:
        raise ValueError("partitions must be between 1 and 256")
    inventory_metadata, sources = normalize_inventory(inventory, release=release, family=family)
    configuration = {
        "release": release,
        "producer_commit": producer_commit,
        "family": family,
        "map_tasks": map_tasks,
        "partitions": partitions,
    }
    normalized_inventory = {**inventory_metadata, "objects": sources}
    inventory_digest = digest(normalized_inventory)
    build_id = digest({"configuration": configuration, "inventory_digest": inventory_digest})

    assignments: list[list[dict[str, Any]]] = [[] for _ in range(map_tasks)]
    for source in sources:
        assignments[stable_bucket(source, map_tasks)].append(source)

    map_plan = []
    for index, assigned_sources in enumerate(assignments):
        task_id = digest(
            {"build_id": build_id, "kind": "map", "index": index, "sources": assigned_sources}
        )
        map_plan.append(
            {
                "index": index,
                "task_id": task_id,
                "source_digest": digest(assigned_sources),
                "expected_input_records": sum(source["records"] for source in assigned_sources),
                "sources": assigned_sources,
                "expected_done_key": f"staging/{build_id}/map/{index:03d}/{task_id}.json",
            }
        )

    reduce_plan = []
    map_task_ids = [task["task_id"] for task in map_plan]
    for index in range(partitions):
        task_id = digest(
            {
                "build_id": build_id,
                "kind": "reduce",
                "index": index,
                "map_task_ids": map_task_ids,
            }
        )
        reduce_plan.append(
            {
                "index": index,
                "task_id": task_id,
                "expected_artifact_key": (
                    f"staging/{build_id}/{family}/{index:04d}/{task_id}.bin"
                ),
                "expected_manifest_key": (
                    f"staging/{build_id}/{family}/{index:04d}/{task_id}.json"
                ),
            }
        )

    return {
        "schema": PLAN_SCHEMA,
        "build_id": build_id,
        "configuration": configuration,
        "inventory_digest": inventory_digest,
        "inventory": inventory_metadata,
        "inventory_totals": {
            "objects": len(sources),
            "bytes": sum(source["bytes"] for source in sources),
            "records": sum(source["records"] for source in sources),
            "row_groups": sum(source["row_groups"] for source in sources),
        },
        "map_tasks": map_plan,
        "reduce_tasks": reduce_plan,
    }


def validate_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        raise ValueError(f"plan schema must be {PLAN_SCHEMA}")
    require_hex_digest(plan.get("build_id", ""), "build_id")
    configuration = plan.get("configuration")
    rebuilt_inventory = {
        **plan.get("inventory", {}),
        "objects": [
            source for task in plan.get("map_tasks", []) for source in task.get("sources", [])
        ],
    }
    rebuilt = build_plan(
        rebuilt_inventory,
        release=configuration["release"],
        producer_commit=configuration["producer_commit"],
        family=configuration["family"],
        map_tasks=configuration["map_tasks"],
        partitions=configuration["partitions"],
    )
    if plan != rebuilt:
        raise ValueError("plan does not match its deterministic contents")
    return plan


def describe_task(plan: Any, kind: str, index: int) -> dict[str, Any]:
    validated = validate_plan(plan)
    require_int(index, "task index")
    key = f"{kind}_tasks"
    if kind not in {"map", "reduce"}:
        raise ValueError("kind must be map or reduce")
    tasks = validated[key]
    if not 0 <= index < len(tasks):
        raise ValueError(f"{kind} task index is out of range")
    return {
        "schema": PLAN_SCHEMA,
        "build_id": validated["build_id"],
        "family": validated["configuration"]["family"],
        "kind": kind,
        "task": tasks[index],
    }


def validate_map_completions(plan: Any, completions: list[Any]) -> list[dict[str, Any]]:
    validated = validate_plan(plan)
    expected = {task["index"]: task for task in validated["map_tasks"]}
    observed: dict[int, dict[str, Any]] = {}
    partition_count = validated["configuration"]["partitions"]
    for completion in completions:
        if not isinstance(completion, dict) or completion.get("schema") != MAP_COMPLETION_SCHEMA:
            raise ValueError(f"map completion schema must be {MAP_COMPLETION_SCHEMA}")
        require_exact_fields(
            completion,
            {
                "schema",
                "build_id",
                "index",
                "task_id",
                "source_digest",
                "status",
                "input_records",
                "selected_records",
                "rejected_records",
                "rejected_reasons",
                "output_records",
                "fragments",
            },
            "map completion",
        )
        if completion.get("build_id") != validated["build_id"]:
            raise ValueError("map completion belongs to a different build")
        index = completion.get("index")
        require_int(index, "map task index")
        if index not in expected:
            raise ValueError(f"unexpected map task: {index}")
        if index in observed:
            raise ValueError(f"duplicate map task: {index}")
        task = expected[index]
        if completion.get("task_id") != task["task_id"]:
            raise ValueError(f"map task_id mismatch for task {index}")
        if completion.get("source_digest") != task["source_digest"]:
            raise ValueError(f"source_digest mismatch for map task {index}")
        if completion.get("status") != "complete":
            raise ValueError(f"map task {index} is not complete")
        count_fields = ("input_records", "selected_records", "rejected_records", "output_records")
        for field in count_fields:
            require_int(completion.get(field), f"{field} for map task {index}")
        if completion["input_records"] != task["expected_input_records"]:
            raise ValueError(f"map input records do not match inventory for task {index}")
        if completion["input_records"] != completion["selected_records"] + completion["rejected_records"]:
            raise ValueError(f"map input records do not reconcile for task {index}")
        if completion["selected_records"] != completion["output_records"]:
            raise ValueError(f"map selected/output records do not reconcile for task {index}")
        rejected_reasons = completion.get("rejected_reasons")
        if not isinstance(rejected_reasons, dict) or any(
            not isinstance(reason, str) or not reason for reason in rejected_reasons
        ):
            raise ValueError(f"rejected_reasons must be an object for map task {index}")
        for reason, count in rejected_reasons.items():
            require_int(count, f"rejection count {reason} for map task {index}", minimum=1)
        if sum(rejected_reasons.values()) != completion["rejected_records"]:
            raise ValueError(f"rejection reasons do not reconcile for map task {index}")
        fragments = completion.get("fragments")
        if not isinstance(fragments, list):
            raise ValueError(f"fragments must be an array for map task {index}")
        seen_partitions: set[int] = set()
        prefix = f"staging/{validated['build_id']}/map/{index:03d}/{task['task_id']}/"
        for fragment in fragments:
            partition = fragment.get("partition") if isinstance(fragment, dict) else None
            if isinstance(fragment, dict):
                require_exact_fields(
                    fragment,
                    {"partition", "object_key", "bytes", "records", "sha256"},
                    "map fragment",
                )
            require_int(partition, f"fragment partition for map task {index}")
            if partition >= partition_count:
                raise ValueError(f"invalid fragment partition for map task {index}")
            if partition in seen_partitions:
                raise ValueError(f"duplicate fragment partition {partition} for map task {index}")
            require_hex_digest(fragment.get("sha256", ""), f"fragment sha256 for map task {index}")
            expected_key = f"{prefix}{partition:04d}/{fragment['sha256']}.parquet"
            if fragment.get("object_key") != expected_key:
                raise ValueError(f"fragment object_key mismatch for map task {index}")
            require_int(fragment.get("bytes"), f"fragment bytes for map task {index}", minimum=1)
            require_int(
                fragment.get("records"), f"fragment records for map task {index}", minimum=1
            )
            seen_partitions.add(partition)
        if not task["sources"] and fragments:
            raise ValueError(f"empty map task {index} cannot publish fragments")
        if sum(fragment["records"] for fragment in fragments) != completion["output_records"]:
            raise ValueError(f"fragment records do not reconcile for map task {index}")
        observed[index] = completion
    missing = sorted(set(expected) - set(observed))
    if missing:
        raise ValueError(f"missing map tasks: {missing}")
    return [observed[index] for index in sorted(observed)]


def partition_fragments(
    completions: list[dict[str, Any]], partition_count: int
) -> dict[int, list[dict[str, Any]]]:
    result = {index: [] for index in range(partition_count)}
    for completion in completions:
        for fragment in completion["fragments"]:
            result[fragment["partition"]].append(
                {
                    "map_index": completion["index"],
                    "map_task_id": completion["task_id"],
                    "object_key": fragment["object_key"],
                    "bytes": fragment["bytes"],
                    "records": fragment["records"],
                    "sha256": fragment["sha256"],
                }
            )
    return result


def build_catalog_candidate(
    plan: Any,
    map_completions: list[Any],
    manifests: list[Any],
    *,
    expected_previous_catalog_digest: str,
    family_scope: Any | None = None,
) -> dict[str, Any]:
    validated = validate_plan(plan)
    require_hex_digest(expected_previous_catalog_digest, "expected_previous_catalog_digest")
    verified_completions = validate_map_completions(validated, map_completions)
    completion_digest = digest(verified_completions)
    fragments_by_partition = partition_fragments(
        verified_completions, validated["configuration"]["partitions"]
    )
    expected = {task["index"]: task for task in validated["reduce_tasks"]}
    observed: dict[int, dict[str, Any]] = {}
    for manifest in manifests:
        if not isinstance(manifest, dict) or manifest.get("schema") != ARTIFACT_SCHEMA:
            raise ValueError(f"artifact schema must be {ARTIFACT_SCHEMA}")
        require_exact_fields(
            manifest,
            {
                "schema",
                "build_id",
                "family",
                "partition",
                "task_id",
                "object_key",
                "bytes",
                "sha256",
                "map_completion_digest",
                "fragment_digest",
                "input_fragments",
                "input_bytes",
                "input_records",
                "output_records",
                "format_version",
                "record_schema_version",
                "verification",
            },
            "artifact manifest",
        )
        if manifest.get("build_id") != validated["build_id"]:
            raise ValueError("artifact belongs to a different build")
        if manifest.get("family") != validated["configuration"]["family"]:
            raise ValueError("artifact belongs to a different family")
        index = manifest.get("partition")
        require_int(index, "artifact partition")
        if index not in expected:
            raise ValueError(f"unexpected partition: {index}")
        if index in observed:
            raise ValueError(f"duplicate partition: {index}")
        if manifest.get("task_id") != expected[index]["task_id"]:
            raise ValueError(f"task_id mismatch for partition {index}")
        if manifest.get("object_key") != expected[index]["expected_artifact_key"]:
            raise ValueError(f"object_key mismatch for partition {index}")
        if manifest.get("map_completion_digest") != completion_digest:
            raise ValueError(f"map_completion_digest mismatch for partition {index}")
        expected_fragments = fragments_by_partition[index]
        if manifest.get("fragment_digest") != digest(expected_fragments):
            raise ValueError(f"fragment_digest mismatch for partition {index}")
        expected_fragment_count = len(expected_fragments)
        expected_input_bytes = sum(fragment["bytes"] for fragment in expected_fragments)
        expected_input_records = sum(fragment["records"] for fragment in expected_fragments)
        require_int(manifest.get("input_fragments"), f"input_fragments for partition {index}")
        require_int(manifest.get("input_bytes"), f"input_bytes for partition {index}")
        require_int(manifest.get("input_records"), f"input_records for partition {index}")
        require_int(manifest.get("output_records"), f"output_records for partition {index}")
        if manifest.get("input_fragments") != expected_fragment_count:
            raise ValueError(f"input fragment count mismatch for partition {index}")
        if manifest.get("input_bytes") != expected_input_bytes:
            raise ValueError(f"input byte count mismatch for partition {index}")
        if manifest.get("input_records") != expected_input_records:
            raise ValueError(f"input record count mismatch for partition {index}")
        if manifest.get("output_records") != expected_input_records:
            raise ValueError(f"output record count mismatch for partition {index}")
        if not isinstance(manifest.get("format_version"), str) or not manifest["format_version"]:
            raise ValueError(f"format_version is required for partition {index}")
        if manifest.get("record_schema_version") != validated["inventory"]["schema_version"]:
            raise ValueError(f"record_schema_version mismatch for partition {index}")
        verification = manifest.get("verification")
        if not isinstance(verification, dict):
            raise ValueError(f"verification is required for partition {index}")
        require_exact_fields(
            verification, {"checksum", "record_count", "strict_reader"}, "artifact verification"
        )
        if not all(value is True for value in verification.values()):
            raise ValueError(f"artifact verification failed for partition {index}")
        require_int(manifest.get("bytes"), f"artifact bytes for partition {index}", minimum=1)
        require_hex_digest(manifest.get("sha256", ""), f"artifact sha256 for partition {index}")
        observed[index] = manifest
    missing = sorted(set(expected) - set(observed))
    if missing:
        raise ValueError(f"missing partitions: {missing}")

    artifacts = [observed[index] for index in sorted(observed)]
    rejected_reasons: dict[str, int] = {}
    for completion in verified_completions:
        for reason, count in completion["rejected_reasons"].items():
            rejected_reasons[reason] = rejected_reasons.get(reason, 0) + count
    candidate = {
        "schema": CATALOG_SCHEMA,
        "build_id": validated["build_id"],
        "release": validated["configuration"]["release"],
        "producer_commit": validated["configuration"]["producer_commit"],
        "family": validated["configuration"]["family"],
        "inventory_digest": validated["inventory_digest"],
        "map_completion_digest": completion_digest,
        "expected_previous_catalog_digest": expected_previous_catalog_digest,
        "production_source_inventory": (
            validated["inventory"]["discovery"]["kind"] == "overture-s3-listing"
        ),
        "promotion_eligible": False,
        "artifacts": artifacts,
        "rejected_reasons": dict(sorted(rejected_reasons.items())),
        "totals": {
            "artifacts": len(artifacts),
            "bytes": sum(artifact["bytes"] for artifact in artifacts),
            "source_records": sum(item["input_records"] for item in verified_completions),
            "selected_records": sum(item["selected_records"] for item in verified_completions),
            "rejected_records": sum(item["rejected_records"] for item in verified_completions),
            "fragment_records": sum(item["output_records"] for item in verified_completions),
            "artifact_records": sum(artifact["output_records"] for artifact in artifacts),
        },
    }
    if family_scope is not None:
        family_manifest = build_family_manifest_from_candidate(validated, artifacts, family_scope)
        candidate["family_manifest"] = {
            "key": family_manifest_key(validated["build_id"], candidate["family"]),
            "manifest_digest": family_manifest["manifest_digest"],
        }
    candidate["candidate_digest"] = digest(candidate)
    return candidate


# ---------------------------------------------------------------------------
# Release-versioned family manifests
#
# A family manifest is the immutable, release-versioned record for one
# experimental family (addresses or places) in a regional build. It carries the
# published object identities (key + byte size + SHA-256), lineage (source
# Overture release, producer script + version, build run id), the
# format/tokenizer/normalization contract versions, and the region scope (bbox
# floats + scope mode + human-readable name). A canonical-JSON self-digest,
# computed with this module's own ``digest`` (sorted-key, compact-separator JSON
# + SHA-256, matching the catalog candidate it is referenced from), lets a later
# finalizer bind the manifest to the catalog candidate and verify it against a
# local directory of artifacts or a remote object listing. Note this shares the
# algorithm — but not the byte encoding — of id_index_protocol's addressing:
# that module dumps with ensure_ascii=False, so digests of non-ASCII region
# names diverge. Cross-check family manifests only through this module.
# ---------------------------------------------------------------------------


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is required")
    return value


def _require_number(value: Any, field: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{field} must be a number")
    return float(value)


def normalize_region(region: Any) -> dict[str, Any]:
    if not isinstance(region, dict):
        raise ValueError("region must be an object")
    require_exact_fields(region, {"name", "bbox", "bbox_scope"}, "region scope")
    name = _require_str(region["name"], "region name")
    bbox_scope = region["bbox_scope"]
    if bbox_scope not in BBOX_SCOPES:
        raise ValueError(f"region bbox_scope must be one of {sorted(BBOX_SCOPES)}")
    bbox = region["bbox"]
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ValueError("region bbox must be [xmin, ymin, xmax, ymax]")
    xmin = _require_number(bbox[0], "bbox xmin")
    ymin = _require_number(bbox[1], "bbox ymin")
    xmax = _require_number(bbox[2], "bbox xmax")
    ymax = _require_number(bbox[3], "bbox ymax")
    if not -180.0 <= xmin < xmax <= 180.0:
        raise ValueError("bbox longitude bounds must satisfy -180 <= xmin < xmax <= 180")
    if not -90.0 <= ymin < ymax <= 90.0:
        raise ValueError("bbox latitude bounds must satisfy -90 <= ymin < ymax <= 90")
    return {"name": name, "bbox": [xmin, ymin, xmax, ymax], "bbox_scope": bbox_scope}


def normalize_lineage(lineage: Any) -> dict[str, Any]:
    if not isinstance(lineage, dict):
        raise ValueError("lineage must be an object")
    require_exact_fields(
        lineage,
        {"overture_release", "build_id", "producer_commit", "producer_script", "producer_version"},
        "lineage",
    )
    require_hex_digest(lineage.get("build_id", ""), "lineage build_id")
    return {
        "overture_release": _require_str(lineage["overture_release"], "overture_release"),
        "build_id": lineage["build_id"],
        "producer_commit": _require_str(lineage["producer_commit"], "producer_commit"),
        "producer_script": _require_str(lineage["producer_script"], "producer_script"),
        "producer_version": _require_str(lineage["producer_version"], "producer_version"),
    }


def normalize_manifest_versions(family: str, versions: Any) -> dict[str, Any]:
    if not isinstance(versions, dict):
        raise ValueError("versions must be an object")
    require_exact_fields(versions, {"format", "tokenizer", "normalization"}, "versions")
    fmt = _require_str(versions["format"], "format version")
    tokenizer = versions["tokenizer"]
    normalization = versions["normalization"]
    for key, value in (("tokenizer", tokenizer), ("normalization", normalization)):
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"{key} version must be a non-empty string or null")
    if family == "places" and not tokenizer:
        raise ValueError("places family requires a tokenizer version")
    if family == "addresses" and not normalization:
        raise ValueError("addresses family requires a normalization version")
    return {"format": fmt, "tokenizer": tokenizer, "normalization": normalization}


def normalize_manifest_artifacts(artifacts: Any) -> list[dict[str, Any]]:
    if not isinstance(artifacts, list):
        raise ValueError("family manifest artifacts must be a JSON array")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in artifacts:
        if not isinstance(entry, dict):
            raise ValueError("every family artifact must be an object")
        require_exact_fields(entry, {"object_key", "bytes", "sha256"}, "family artifact")
        key = _require_str(entry["object_key"], "artifact object_key")
        if key in seen:
            raise ValueError(f"duplicate artifact object_key: {key}")
        require_int(entry["bytes"], f"artifact bytes for {key}", minimum=1)
        require_hex_digest(entry.get("sha256", ""), f"artifact sha256 for {key}")
        normalized.append({"object_key": key, "bytes": entry["bytes"], "sha256": entry["sha256"]})
        seen.add(key)
    if not normalized:
        raise ValueError("family manifest requires at least one artifact")
    return sorted(normalized, key=lambda item: item["object_key"])


def build_family_manifest(
    family: str,
    *,
    lineage: Any,
    versions: Any,
    region: Any,
    artifacts: Any,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if family not in FAMILIES:
        raise ValueError(f"family must be one of {sorted(FAMILIES)}")
    normalized_lineage = normalize_lineage(lineage)
    normalized_versions = normalize_manifest_versions(family, versions)
    normalized_region = normalize_region(region)
    normalized_artifacts = normalize_manifest_artifacts(artifacts)
    if generated_at is not None and (not isinstance(generated_at, str) or not generated_at):
        raise ValueError("generated_at must be a non-empty string or null")
    manifest = {
        "schema": FAMILY_MANIFEST_SCHEMA,
        "family": family,
        "generated_at": generated_at,
        "lineage": normalized_lineage,
        "versions": normalized_versions,
        "region": normalized_region,
        "artifacts": normalized_artifacts,
        "totals": {
            "artifacts": len(normalized_artifacts),
            "bytes": sum(item["bytes"] for item in normalized_artifacts),
        },
    }
    manifest["manifest_digest"] = digest(manifest)
    return manifest


def validate_family_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("schema") != FAMILY_MANIFEST_SCHEMA:
        raise ValueError(f"family manifest schema must be {FAMILY_MANIFEST_SCHEMA}")
    require_exact_fields(
        manifest,
        {
            "schema",
            "family",
            "generated_at",
            "lineage",
            "versions",
            "region",
            "artifacts",
            "totals",
            "manifest_digest",
        },
        "family manifest",
    )
    require_hex_digest(manifest.get("manifest_digest", ""), "manifest_digest")
    rebuilt = build_family_manifest(
        manifest["family"],
        lineage=manifest["lineage"],
        versions=manifest["versions"],
        region=manifest["region"],
        artifacts=manifest["artifacts"],
        generated_at=manifest["generated_at"],
    )
    if rebuilt != manifest:
        raise ValueError("family manifest does not match its deterministic contents")
    return manifest


def family_manifest_key(build_id: str, family: str) -> str:
    return f"staging/{build_id}/{family}/family-manifest.json"


def _require_uniform_format_version(artifacts: list[dict[str, Any]]) -> str:
    observed: set[Any] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or "format_version" not in artifact:
            raise ValueError("reduce artifact is missing format_version")
        observed.add(artifact["format_version"])
    if len(observed) != 1:
        raise ValueError("reduce artifacts do not share a single format_version")
    return observed.pop()


def build_family_manifest_from_candidate(
    plan: Any, artifacts: list[dict[str, Any]], family_scope: Any
) -> dict[str, Any]:
    """Assemble a family manifest from a validated plan and its reduce artifacts.

    Lineage (release / build id / producer commit) and the object identities
    come from the build itself; the region scope, tokenizer/normalization
    versions, and producer script identity come from ``family_scope``. The
    manifest ``format`` is the reduce artifacts' own uniform ``format_version``,
    so a manifest can never claim a format its artifacts do not carry.
    """
    validated = validate_plan(plan)
    if not isinstance(family_scope, dict):
        raise ValueError("family_scope must be an object")
    require_exact_fields(
        family_scope,
        {
            "region",
            "tokenizer_version",
            "normalization_version",
            "producer_script",
            "producer_version",
            "generated_at",
        },
        "family scope",
    )
    family = validated["configuration"]["family"]
    lineage = {
        "overture_release": validated["configuration"]["release"],
        "build_id": validated["build_id"],
        "producer_commit": validated["configuration"]["producer_commit"],
        "producer_script": family_scope["producer_script"],
        "producer_version": family_scope["producer_version"],
    }
    versions = {
        "format": _require_uniform_format_version(artifacts),
        "tokenizer": family_scope["tokenizer_version"],
        "normalization": family_scope["normalization_version"],
    }
    manifest_artifacts = [
        {"object_key": artifact["object_key"], "bytes": artifact["bytes"], "sha256": artifact["sha256"]}
        for artifact in artifacts
    ]
    return build_family_manifest(
        family,
        lineage=lineage,
        versions=versions,
        region=family_scope["region"],
        artifacts=manifest_artifacts,
        generated_at=family_scope["generated_at"],
    )


def _verify_object_set(
    artifacts: list[dict[str, Any]], observed: dict[str, tuple[int, str]]
) -> dict[str, Any]:
    expected = {artifact["object_key"]: (artifact["bytes"], artifact["sha256"]) for artifact in artifacts}
    problems: list[str] = []
    missing = sorted(set(expected) - set(observed))
    if missing:
        problems.append(f"missing artifacts: {missing}")
    unexpected = sorted(set(observed) - set(expected))
    if unexpected:
        problems.append(f"unexpected objects: {unexpected}")
    size_mismatch = sorted(
        key for key in expected if key in observed and observed[key][0] != expected[key][0]
    )
    if size_mismatch:
        problems.append(f"size mismatch: {size_mismatch}")
    sha_mismatch = sorted(
        key for key in expected if key in observed and observed[key][1] != expected[key][1]
    )
    if sha_mismatch:
        problems.append(f"sha256 mismatch: {sha_mismatch}")
    if problems:
        raise ValueError("; ".join(problems))
    return {"objects": len(expected), "bytes": sum(size for size, _ in expected.values())}


def verify_family_manifest_against_listing(
    manifest: Any, listing: dict[str, Any]
) -> dict[str, Any]:
    """Verify a manifest against a remote listing (a pure key -> (size, sha) map).

    No network code: the caller supplies whatever object-store listing it has
    already fetched. Returns the verified manifest; raises on any missing,
    unexpected, size, or hash discrepancy.
    """
    validated = validate_family_manifest(manifest)
    if not isinstance(listing, dict):
        raise ValueError("listing must be a mapping of object_key to (bytes, sha256)")
    observed: dict[str, tuple[int, str]] = {}
    for key, value in listing.items():
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ValueError(f"listing entry for {key} must be (bytes, sha256)")
        observed[key] = (value[0], value[1])
    _verify_object_set(validated["artifacts"], observed)
    return validated


def verify_family_manifest_against_directory(manifest: Any, directory: Path) -> dict[str, Any]:
    """Verify a manifest against a local directory, recomputing sizes and hashes.

    Every file under ``directory`` is hashed and keyed by its POSIX-relative
    path, then compared to the manifest's object set. Raises on any missing,
    unexpected, size, or hash discrepancy.
    """
    validated = validate_family_manifest(manifest)
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"artifact directory does not exist: {root}")
    observed: dict[str, tuple[int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            observed[path.relative_to(root).as_posix()] = (
                len(data),
                hashlib.sha256(data).hexdigest(),
            )
    _verify_object_set(validated["artifacts"], observed)
    return validated


# ---------------------------------------------------------------------------
# Standalone family-manifest CLI
#
# The fan-in path derives a family manifest from a full map/reduce plan and its
# reduce artifacts. A direct regional producer (e.g. the Places region build
# workflow) has no map/reduce plan: it splits one bbox extraction into shards
# and publishes them under an isolated prefix. These helpers let such a producer
# assemble and verify the same `overture-global-family-manifest-v1` object from
# the published object identities directly, reusing `build_family_manifest` and
# `verify_family_manifest_against_listing` unchanged.
# ---------------------------------------------------------------------------


DEFAULT_FORMAT_VERSION = {
    "places": PLACES_FORMAT_VERSION,
    "addresses": ADDRESS_FORMAT_VERSION,
}
DEFAULT_TOKENIZER_VERSION = {"places": PLACES_TOKENIZER_VERSION, "addresses": None}
DEFAULT_NORMALIZATION_VERSION = {
    "places": None,
    "addresses": ADDRESS_NORMALIZATION_VERSION,
}


def artifact_from_spec(spec: str) -> dict[str, Any]:
    """Turn an ``object_key=local_path`` spec into an artifact identity.

    The object key is the immutable published key (verified later against a
    remote listing); the byte size and SHA-256 are recomputed from the local
    file so the manifest can never claim an identity the bytes do not carry.
    """
    key, separator, path = spec.partition("=")
    if not separator or not key or not path:
        raise ValueError(f"artifact spec must be object_key=local_path: {spec!r}")
    data = Path(path).read_bytes()
    if not data:
        raise ValueError(f"artifact file is empty: {path}")
    return {
        "object_key": key,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def derive_direct_build_id(
    *, family: str, release: str, producer_commit: str, region: dict[str, Any],
    artifacts: list[dict[str, Any]],
) -> str:
    """A deterministic lineage build id for a build with no map/reduce plan."""
    return digest(
        {
            "kind": "regional-direct-build",
            "family": family,
            "overture_release": release,
            "producer_commit": producer_commit,
            "region": region,
            "artifact_object_keys": sorted(item["object_key"] for item in artifacts),
        }
    )


def load_listing(path: Path) -> dict[str, Any]:
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise ValueError("listing must be a JSON object of object_key -> [bytes, sha256]")
    return raw


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--inventory", type=Path, required=True)
    plan_parser.add_argument("--release", required=True)
    plan_parser.add_argument("--producer-commit", required=True)
    plan_parser.add_argument("--family", choices=sorted(FAMILIES), required=True)
    plan_parser.add_argument("--map-tasks", type=int, required=True)
    plan_parser.add_argument("--partitions", type=int, required=True)
    plan_parser.add_argument("--output", type=Path, required=True)

    describe_parser = commands.add_parser("describe-task")
    describe_parser.add_argument("--plan", type=Path, required=True)
    describe_parser.add_argument("--kind", choices=["map", "reduce"], required=True)
    describe_parser.add_argument("--index", type=int, required=True)

    fan_in_parser = commands.add_parser("fan-in")
    fan_in_parser.add_argument("--plan", type=Path, required=True)
    fan_in_parser.add_argument("--map-completion", type=Path, action="append", required=True)
    fan_in_parser.add_argument("--manifest", type=Path, action="append", required=True)
    fan_in_parser.add_argument("--expected-previous-catalog-digest", required=True)
    fan_in_parser.add_argument("--output", type=Path, required=True)
    # Optional family-manifest scope. When --region-name/--bbox/--bbox-scope are
    # supplied the fan-in also emits a release-versioned family manifest and the
    # candidate references it by key + digest.
    fan_in_parser.add_argument("--region-name")
    fan_in_parser.add_argument(
        "--bbox", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX")
    )
    fan_in_parser.add_argument("--bbox-scope", choices=sorted(BBOX_SCOPES))
    fan_in_parser.add_argument("--tokenizer-version")
    fan_in_parser.add_argument("--normalization-version")
    fan_in_parser.add_argument("--producer-script")
    fan_in_parser.add_argument("--producer-version")
    fan_in_parser.add_argument("--generated-at")
    fan_in_parser.add_argument("--family-manifest-output", type=Path)

    family_parser = commands.add_parser("family-manifest")
    family_parser.add_argument("--family", choices=sorted(FAMILIES), required=True)
    family_parser.add_argument("--overture-release", required=True)
    family_parser.add_argument("--producer-commit", required=True)
    family_parser.add_argument("--producer-script", required=True)
    family_parser.add_argument("--producer-version", required=True)
    family_parser.add_argument("--region-name", required=True)
    family_parser.add_argument(
        "--bbox", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
        required=True,
    )
    family_parser.add_argument("--bbox-scope", choices=sorted(BBOX_SCOPES), required=True)
    family_parser.add_argument("--format-version")
    family_parser.add_argument("--tokenizer-version")
    family_parser.add_argument("--normalization-version")
    family_parser.add_argument("--build-id")
    family_parser.add_argument("--generated-at")
    family_parser.add_argument(
        "--artifact", action="append", required=True, metavar="OBJECT_KEY=LOCAL_PATH"
    )
    family_parser.add_argument("--output", type=Path, required=True)

    verify_parser = commands.add_parser("verify-family-manifest")
    verify_parser.add_argument("--manifest", type=Path, required=True)
    verify_group = verify_parser.add_mutually_exclusive_group(required=True)
    verify_group.add_argument("--listing", type=Path)
    verify_group.add_argument("--against-directory", type=Path)
    verify_parser.add_argument("--output", type=Path)

    args = parser.parse_args()
    if args.command == "plan":
        result = build_plan(
            read_json(args.inventory),
            release=args.release,
            producer_commit=args.producer_commit,
            family=args.family,
            map_tasks=args.map_tasks,
            partitions=args.partitions,
        )
        write_json(args.output, result)
    elif args.command == "describe-task":
        print(canonical_json(describe_task(read_json(args.plan), args.kind, args.index)).decode(), end="")
    elif args.command == "family-manifest":
        artifacts = [artifact_from_spec(spec) for spec in args.artifact]
        region = {
            "name": args.region_name,
            "bbox": list(args.bbox),
            "bbox_scope": args.bbox_scope,
        }
        build_id = args.build_id or derive_direct_build_id(
            family=args.family,
            release=args.overture_release,
            producer_commit=args.producer_commit,
            region=region,
            artifacts=artifacts,
        )
        require_hex_digest(build_id, "build_id")
        versions = {
            "format": args.format_version or DEFAULT_FORMAT_VERSION[args.family],
            "tokenizer": (
                args.tokenizer_version
                if args.tokenizer_version is not None
                else DEFAULT_TOKENIZER_VERSION[args.family]
            ),
            "normalization": (
                args.normalization_version
                if args.normalization_version is not None
                else DEFAULT_NORMALIZATION_VERSION[args.family]
            ),
        }
        manifest = build_family_manifest(
            args.family,
            lineage={
                "overture_release": args.overture_release,
                "build_id": build_id,
                "producer_commit": args.producer_commit,
                "producer_script": args.producer_script,
                "producer_version": args.producer_version,
            },
            versions=versions,
            region=region,
            artifacts=artifacts,
            generated_at=args.generated_at,
        )
        write_json(args.output, manifest)
    elif args.command == "verify-family-manifest":
        manifest = read_json(args.manifest)
        if args.against_directory is not None:
            verified = verify_family_manifest_against_directory(
                manifest, args.against_directory
            )
        else:
            verified = verify_family_manifest_against_listing(
                manifest, load_listing(args.listing)
            )
        summary = {
            "schema": "overture-family-manifest-verification-v1",
            "family": verified["family"],
            "manifest_digest": verified["manifest_digest"],
            "region": verified["region"],
            "verified_objects": verified["totals"]["artifacts"],
            "verified_bytes": verified["totals"]["bytes"],
        }
        if args.output is not None:
            write_json(args.output, summary)
        print(canonical_json(summary).decode(), end="")
    else:
        plan = read_json(args.plan)
        completions = [read_json(path) for path in args.map_completion]
        manifests = [read_json(path) for path in args.manifest]
        family_scope = None
        scope_flags = (args.region_name, args.bbox, args.bbox_scope)
        if any(flag is not None for flag in scope_flags):
            if not all(flag is not None for flag in scope_flags):
                parser.error("--region-name, --bbox, and --bbox-scope must be given together")
            if not args.producer_script or not args.producer_version:
                parser.error("--producer-script and --producer-version are required with a region scope")
            family_scope = {
                "region": {
                    "name": args.region_name,
                    "bbox": list(args.bbox),
                    "bbox_scope": args.bbox_scope,
                },
                "tokenizer_version": args.tokenizer_version,
                "normalization_version": args.normalization_version,
                "producer_script": args.producer_script,
                "producer_version": args.producer_version,
                "generated_at": args.generated_at,
            }
        result = build_catalog_candidate(
            plan,
            completions,
            manifests,
            expected_previous_catalog_digest=args.expected_previous_catalog_digest,
            family_scope=family_scope,
        )
        write_json(args.output, result)
        if family_scope is not None and args.family_manifest_output is not None:
            family_manifest = build_family_manifest_from_candidate(
                plan, result["artifacts"], family_scope
            )
            write_json(args.family_manifest_output, family_manifest)


if __name__ == "__main__":
    main()
