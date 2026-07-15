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
FAMILIES = {"addresses", "places"}
FAMILY_PATHS = {
    "addresses": ("addresses", "address"),
    "places": ("places", "place"),
}


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
    candidate["candidate_digest"] = digest(candidate)
    return candidate


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
    else:
        result = build_catalog_candidate(
            read_json(args.plan),
            [read_json(path) for path in args.map_completion],
            [read_json(path) for path in args.manifest],
            expected_previous_catalog_digest=args.expected_previous_catalog_digest,
        )
        write_json(args.output, result)


if __name__ == "__main__":
    main()
