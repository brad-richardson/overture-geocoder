#!/usr/bin/env python3
"""Fail-closed validator for frozen Places construction-v1 planet evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ROLES = (
    "representative",
    "near_cap",
    "densest_spatial",
    "token_fanout",
    "multilingual_cjk",
    "duplicate_heavy",
    "head_heavy",
)


def memory_limit_bytes(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([1-9][0-9]*)(MB|GB)", value)
    if match is None:
        return None
    multiplier = 1024**2 if match.group(2) == "MB" else 1024**3
    return int(match.group(1)) * multiplier


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def load_inventory_module():
    path = ROOT / "scripts/places_inventory_v1.py"
    spec = importlib.util.spec_from_file_location("places_readiness_inventory", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_construction_module():
    path = ROOT / "scripts/places_construction_v1.py"
    spec = importlib.util.spec_from_file_location("places_readiness_construction", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def actual_runtime() -> dict[str, str]:
    import duckdb
    import numpy
    import pyarrow
    import unicodedata2

    return {
        "python": platform.python_version(),
        "duckdb": duckdb.__version__,
        "numpy": numpy.__version__,
        "pyarrow": pyarrow.__version__,
        "unicodedata2": unicodedata2.unidata_version,
        "rustc": subprocess.check_output(["rustc", "--version"], text=True)
        .strip()
        .removeprefix("rustc "),
        "cargo": subprocess.check_output(["cargo", "--version"], text=True)
        .strip()
        .removeprefix("cargo "),
    }


def candidate_universe(inventory: dict[str, Any], maximum: int = 12) -> list[int]:
    tasks = inventory["map_plan"]["tasks"]
    ordered_rows = sorted(task["expected_input_records"] for task in tasks)
    selected: list[int] = []
    for fraction in (0, 0.10, 0.25, 0.50, 0.75, 0.90, 1.0):
        target = ordered_rows[round(fraction * (len(ordered_rows) - 1))]
        task = min(
            tasks,
            key=lambda item: (
                abs(item["expected_input_records"] - target),
                item["index"],
            ),
        )
        if task["index"] not in selected:
            selected.append(task["index"])
    for task in sorted(
        tasks, key=lambda item: (-item["selected_uncompressed_bytes"], item["index"])
    ):
        if task["index"] not in selected:
            selected.append(task["index"])
        if len(selected) == maximum:
            break
    if len(selected) != maximum:
        raise ValueError(
            "Places inventory cannot provide the frozen candidate universe"
        )
    return selected


def select_roles(
    inventory: dict[str, Any], census: dict[int, dict[str, Any]]
) -> dict[str, int]:
    universe = list(census)
    tasks = inventory["map_plan"]["tasks"]
    rows = sorted(tasks[index]["expected_input_records"] for index in universe)
    median = rows[round(0.5 * (len(rows) - 1))]
    selected: dict[str, int] = {}

    def choose(role: str, key) -> None:
        remaining = [index for index in universe if index not in selected.values()]
        if not remaining:
            raise ValueError("Places role selection exhausted its universe")
        selected[role] = min(remaining, key=key)

    choose(
        "representative",
        lambda index: (abs(tasks[index]["expected_input_records"] - median), index),
    )
    choose(
        "near_cap", lambda index: (-tasks[index]["selected_uncompressed_bytes"], index)
    )
    choose(
        "densest_spatial",
        lambda index: (
            -census[index]["metrics"]["maximum_spatial_cell_term_rows"],
            index,
        ),
    )
    choose(
        "token_fanout",
        lambda index: (
            -census[index]["metrics"]["term_rows_per_admitted_feature"],
            index,
        ),
    )
    choose(
        "multilingual_cjk",
        lambda index: (-census[index]["metrics"]["multilingual_cjk_features"], index),
    )
    choose(
        "duplicate_heavy",
        lambda index: (
            -census[index]["metrics"]["maximum_uuid_multiplicity"],
            -census[index]["metrics"]["duplicate_uuid_rows"],
            index,
        ),
    )
    choose(
        "head_heavy",
        lambda index: (-census[index]["metrics"]["maximum_token_rows"], index),
    )
    return selected


def validate(
    spec_path: Path,
    inventory_path: Path,
    evidence_path: Path,
    *,
    runtime: dict[str, str] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []

    def require(condition: bool, reason: str) -> None:
        if not condition:
            reasons.append(reason)

    spec = json.loads(spec_path.read_text())
    inventory = json.loads(inventory_path.read_text())
    evidence = json.loads(evidence_path.read_text())
    spec_sha256 = sha256_file(spec_path)
    inventory_file_sha256 = sha256_file(inventory_path)
    require(
        spec.get("schema") == "overture-places-construction-v1-evidence-spec-v4",
        "evidence spec schema differs",
    )
    try:
        load_inventory_module().validate_inventory(inventory)
    except (KeyError, TypeError, ValueError) as exc:
        reasons.append(f"inventory validation failed: {exc}")
    require(
        inventory.get("release") == spec.get("release"), "inventory release differs"
    )
    current_runtime = runtime or actual_runtime()
    require(current_runtime == spec.get("runtime"), "runtime differs from frozen spec")
    require(
        spec.get("tokenizer", {}).get("unicode_version") == "17.0.0",
        "tokenizer Unicode version differs from the frozen contract",
    )
    require(
        evidence.get("schema") == "overture-places-construction-v1-scale-evidence-v1",
        "scale evidence schema differs",
    )
    require(
        evidence.get("evidence_spec_sha256") == spec_sha256,
        "scale evidence spec hash differs",
    )
    require(
        evidence.get("inventory_file_sha256") == inventory_file_sha256,
        "scale inventory file hash differs",
    )
    require(
        evidence.get("inventory_sha256") == inventory.get("inventory_sha256"),
        "scale inventory content hash differs",
    )
    require(
        evidence.get("schema_fingerprint_sha256")
        == inventory.get("schema_contract", {}).get("fingerprint_sha256"),
        "scale schema fingerprint differs",
    )
    universe = candidate_universe(
        inventory, spec["candidate_universe"]["maximum_tasks"]
    )
    require(
        evidence.get("candidate_universe") == universe, "candidate universe differs"
    )
    raw_census = evidence.get("census")
    census: dict[int, dict[str, Any]] = {}
    census_items = 0
    if isinstance(raw_census, list):
        for item in raw_census:
            census_items += 1
            if isinstance(item, dict) and type(item.get("task_index")) is int:
                census[item["task_index"]] = item
    require(sorted(census) == sorted(universe), "census task set is missing or extra")
    require(census_items == len(census), "census contains malformed or duplicate tasks")
    transform_binary_sha256: str | None = None
    for task_index in universe:
        item = census.get(task_index, {})
        task = inventory["map_plan"]["tasks"][task_index]
        identity = item.get("identity", {})
        require(
            item.get("schema") == "overture-places-construction-v1-census-v1",
            f"census task {task_index} schema differs",
        )
        require(
            identity.get("evidence_spec_sha256") == spec_sha256,
            f"census task {task_index} evidence spec binding differs",
        )
        require(
            identity.get("inventory_file_sha256") == inventory_file_sha256
            and identity.get("inventory_sha256") == inventory.get("inventory_sha256"),
            f"census task {task_index} inventory binding differs",
        )
        require(
            identity.get("task_index") == task_index
            and identity.get("task_digest") == task["task_digest"]
            and identity.get("task_source_digest") == task["source_digest"]
            and identity.get("expected_input_records")
            == task["expected_input_records"],
            f"census task {task_index} task identity differs",
        )
        embedded_report = {
            key: value
            for key, value in item.items()
            if key not in ("task_index", "report_sha256")
        }
        embedded_sha256 = hashlib.sha256(
            (json.dumps(embedded_report, indent=2, sort_keys=True) + "\n").encode()
        ).hexdigest()
        require(
            item.get("report_sha256") == embedded_sha256,
            f"census task {task_index} report digest differs",
        )
        input_evidence = item.get("input", {})
        require(
            input_evidence.get("rows") == task["expected_input_records"]
            and is_sha256(input_evidence.get("sha256")),
            f"census task {task_index} projected input binding differs",
        )
        transform_binary = item.get("transform_binary", {})
        binary_sha256 = transform_binary.get("sha256")
        require(
            is_sha256(binary_sha256),
            f"census task {task_index} transform binary binding differs",
        )
        if transform_binary_sha256 is None:
            transform_binary_sha256 = binary_sha256
        require(
            binary_sha256 == transform_binary_sha256,
            f"census task {task_index} transform binary differs across tasks",
        )
        transform = item.get("transform", {})
        require(
            transform.get("schema") == "overture-places-rust-transform-report-v1"
            and transform.get("tokenizer_version") == spec["tokenizer"]["version"]
            and is_sha256(transform.get("semantic_sum_a"))
            and is_sha256(transform.get("semantic_sum_b")),
            f"census task {task_index} transform binding differs",
        )
        metrics = item.get("metrics", {})
        admitted = transform.get("admitted_features")
        emitted = transform.get("emitted_term_rows")
        require(
            type(admitted) is int
            and admitted > 0
            and type(emitted) is int
            and emitted > 0
            and metrics.get("term_rows_per_admitted_feature")
            == emitted / admitted
            and metrics.get("multilingual_cjk_features")
            == transform.get("multilingual_features", -1)
            + transform.get("cjk_features", -1),
            f"census task {task_index} metrics do not bind to its transform",
        )
        parameters = item.get("parameters", {})
        census_memory_bytes = memory_limit_bytes(
            parameters.get("duckdb_memory_limit")
        )
        memory_cap_bytes = memory_limit_bytes(
            spec["acceptance_gates"]["resources"]["duckdb_memory_limit"]
        )
        require(
            census_memory_bytes is not None
            and memory_cap_bytes is not None
            and census_memory_bytes <= memory_cap_bytes
            and parameters.get("duckdb_threads")
            == spec["acceptance_gates"]["resources"]["duckdb_threads"],
            f"census task {task_index} DuckDB parameters exceed the frozen caps",
        )
        census_resources = item.get("census_evidence", {})
        census_headroom = 1 - spec["acceptance_gates"]["resources"][
            "resource_headroom_min_fraction"
        ]
        require(
            type(census_resources.get("peak_rss_bytes")) is int
            and census_resources["peak_rss_bytes"]
            <= spec["acceptance_gates"]["resources"][
                "process_group_rss_hard_cap_bytes"
            ]
            * census_headroom,
            f"census task {task_index} RSS lacks headroom",
        )
        require(
            type(census_resources.get("peak_scratch_and_output_bytes")) is int
            and census_resources["peak_scratch_and_output_bytes"]
            <= spec["acceptance_gates"]["resources"]["scratch_hard_cap_bytes"]
            * census_headroom,
            f"census task {task_index} scratch lacks headroom",
        )
    expected_roles = (
        select_roles(inventory, census) if len(census) == len(universe) else {}
    )
    roles = evidence.get("roles")
    require(isinstance(roles, dict) and tuple(roles) == ROLES, "role set/order differs")
    require(roles == expected_roles, "role selection differs from frozen metrics")
    require(
        len(set(roles.values())) == len(ROLES) if isinstance(roles, dict) else False,
        "roles are not distinct",
    )
    if census:
        require(
            any(
                item["metrics"]["multilingual_cjk_features"] > 0
                for item in census.values()
            ),
            "multilingual/CJK coverage is absent",
        )
    # Duplicate-UUID coverage is deliberately NOT a planet gate. Real
    # 2026-06-17.0 census data carries zero duplicate UUIDs across all tasks
    # (`duplicate_uuid_rows: 0` everywhere), so a planet `require(... > 1)` could
    # never close on real data — it was an unclosable gate lying about being
    # closable. The duplicate-handling path is instead gated fail-closed against a
    # checked-in synthetic fixture in the test suite
    # (`tests/fixtures/places_duplicate_uuid.json`, exercised by
    # `tests/test_places_duplicate_uuid_gate.py`), which drives duplicate UUIDs
    # through map -> plan -> reduce and asserts multiplicity is preserved. Here we
    # only record the real-data observation informationally.
    duplicate_observation = {
        "maximum_uuid_multiplicity": max(
            (item["metrics"]["maximum_uuid_multiplicity"] for item in census.values()),
            default=0,
        ),
        "duplicate_uuid_rows_total": sum(
            item["metrics"]["duplicate_uuid_rows"] for item in census.values()
        ),
    }

    # IPC batch-row cap drift guard: the construction pipeline's single
    # MAX_IPC_BATCH_ROWS constant, the hydrate/ingest invariant derived from it,
    # and the frozen evidence-spec `maximum_ipc_batch_rows` must agree exactly, so
    # the spec, hydrate, ingest, and write_arrow_query call sites cannot drift.
    construction = load_construction_module()
    spec_ipc_cap = spec["acceptance_gates"]["resources"]["maximum_ipc_batch_rows"]
    require(
        construction.MAX_IPC_BATCH_ROWS == spec_ipc_cap,
        "construction IPC batch cap differs from the frozen evidence spec",
    )
    require(
        construction.HYDRATE_BATCH_ROWS * construction.MAX_TERMS_PER_FEATURE
        <= construction.MAX_IPC_BATCH_ROWS,
        "hydrate batch is not derived within the IPC cap",
    )
    require(
        construction.MAP_DUCKDB_MEMORY_LIMIT
        == spec["acceptance_gates"]["resources"].get("map_duckdb_memory_limit"),
        "Places map DuckDB memory limit differs from the frozen evidence spec",
    )
    require(
        construction.MAP_DUCKDB_TEMP_SHARE
        == spec["acceptance_gates"]["resources"].get("map_duckdb_temp_share"),
        "Places map DuckDB temp share differs from the frozen evidence spec",
    )

    entity_phrase = spec.get("acceptance_gates", {}).get("head", {}).get(
        "entity_phrase"
    )
    require(
        entity_phrase
        == {
            "admission": "prominence-primary-name-v1",
            "field": "primary_name",
            "minimum_normalized_words": 2,
            "maximum_normalized_words": 3,
            "maximum_keys_per_record": 1,
            "key_prefixes": ["e2:", "e3:"],
            "prominence_rank_must_be_positive": True,
            "head_only": True,
            "stored_primary_name_validation_required": True,
        },
        "entity-phrase contract differs",
    )
    require(
        list(construction.ENTITY_PHRASE_PREFIXES)
        == (entity_phrase or {}).get("key_prefixes"),
        "construction entity-phrase prefixes differ from the frozen evidence spec",
    )

    gates = spec["acceptance_gates"]
    task_runs = (
        evidence.get("task_runs") if isinstance(evidence.get("task_runs"), dict) else {}
    )
    require(
        sorted(map(int, task_runs)) == sorted(roles.values())
        if isinstance(roles, dict)
        else False,
        "required task run set differs",
    )
    for task_index in roles.values() if isinstance(roles, dict) else []:
        run = task_runs.get(str(task_index), {})
        projection = run.get("projection", {})
        task = inventory["map_plan"]["tasks"][task_index]
        projection_identity = projection.get("identity", {})
        require(
            projection.get("schema")
            == "overture-places-construction-v1-projection-report-v1"
            and projection_identity.get("evidence_spec_sha256") == spec_sha256
            and projection_identity.get("inventory_file_sha256")
            == inventory_file_sha256
            and projection_identity.get("inventory_sha256")
            == inventory.get("inventory_sha256")
            and projection_identity.get("task_index") == task_index
            and projection_identity.get("task_digest") == task["task_digest"]
            and projection_identity.get("task_source_digest")
            == task["source_digest"]
            and projection_identity.get("expected_input_records")
            == task["expected_input_records"],
            f"task {task_index} projection identity differs",
        )
        projected_output = projection.get("output", {})
        verified_input = projection.get("verified_input", {})
        require(
            all(
                projected_output.get(key) == verified_input.get(key)
                for key in ("bytes", "sha256", "records", "row_groups")
            )
            and is_sha256(verified_input.get("sha256"))
            and verified_input.get("records") == task["expected_input_records"],
            f"task {task_index} projected Parquet binding differs",
        )
        resources = projection.get("resources", {})
        input_gate = gates["input"]
        require(
            type(resources.get("remote_read_bytes")) is int
            and resources["remote_read_bytes"]
            <= input_gate["remote_read_bytes_per_task_hard_cap"],
            f"task {task_index} remote read evidence differs/over cap",
        )
        require(
            projected_output.get(
                "bytes", input_gate["projected_parquet_bytes_per_task_hard_cap"] + 1
            )
            <= input_gate["projected_parquet_bytes_per_task_hard_cap"],
            f"task {task_index} projection bytes exceed cap",
        )
        baseline = run.get("baseline", {})
        candidates = run.get("candidates")
        require(
            isinstance(candidates, list)
            and len(candidates)
            == gates["baseline_and_candidate"]["candidate_runs_per_required_input"],
            f"task {task_index} candidate run count differs",
        )
        if not isinstance(candidates, list) or len(candidates) != 2:
            continue
        baseline_binding = {
            key: baseline.get(key)
            for key in ("emitted_term_rows", "semantic_sum_a", "semantic_sum_b")
        }
        outputs = []
        worst_seconds = 0.0
        for candidate in candidates:
            binding = candidate.get("binding", {})
            require(
                binding == baseline_binding,
                f"task {task_index} baseline/candidate binding differs",
            )
            outputs.append(candidate.get("output_sha256"))
            resource = candidate.get("resources", {})
            worst_seconds = max(
                worst_seconds, float(resource.get("wall_seconds", float("inf")))
            )
            resource_gate = gates["resources"]
            headroom = 1 - resource_gate["resource_headroom_min_fraction"]
            require(
                resource.get(
                    "peak_rss_bytes",
                    resource_gate["process_group_rss_hard_cap_bytes"] + 1,
                )
                <= resource_gate["process_group_rss_hard_cap_bytes"] * headroom,
                f"task {task_index} RSS lacks headroom",
            )
            require(
                resource.get(
                    "peak_scratch_bytes", resource_gate["scratch_hard_cap_bytes"] + 1
                )
                <= resource_gate["scratch_hard_cap_bytes"] * headroom,
                f"task {task_index} scratch lacks headroom",
            )
            require(
                resource.get(
                    "wall_seconds", resource_gate["candidate_wall_hard_cap_seconds"] + 1
                )
                <= resource_gate["candidate_wall_hard_cap_seconds"] * headroom,
                f"task {task_index} wall time lacks headroom",
            )
        require(
            len(set(outputs)) == 1 and outputs[0],
            f"task {task_index} candidate outputs are not deterministic",
        )
        require(
            worst_seconds > 0
            and baseline.get("elapsed_seconds", 0) / worst_seconds
            >= gates["baseline_and_candidate"]["candidate_vs_baseline_speedup_min"],
            f"task {task_index} speedup gate failed",
        )

    rehearsal = evidence.get("rehearsal", {})
    map_reduce = gates["map_reduce"]
    coverage = gates["coverage"]
    role_tasks = list(roles.values()) if isinstance(roles, dict) else []
    require(
        rehearsal.get("logical_tasks") == len(role_tasks)
        and rehearsal.get("logical_tasks", 0) >= map_reduce["minimum_logical_tasks"],
        "logical task coverage differs",
    )
    require(
        rehearsal.get("observed", {}).get("mapped_tasks") == role_tasks,
        "rehearsal mapped task set/order differs from selected roles",
    )
    require(
        rehearsal.get("packs", 0) >= map_reduce["minimum_packs"], "pack coverage is low"
    )
    require(
        rehearsal.get("parquet_row_groups", 0)
        >= map_reduce["minimum_parquet_row_groups"],
        "row-group coverage is low",
    )
    require(
        rehearsal.get("partitions", 0) >= map_reduce["minimum_genesis_partitions"],
        "partition coverage is low",
    )
    require(
        rehearsal.get("maximum_selective_amplification", float("inf"))
        <= map_reduce["selective_read_amplification_max"],
        "selective amplification exceeds cap",
    )
    for field, reason in (
        ("exact_reconciliation", "exact reconciliation is absent"),
        ("overlap_reconciliation", "overlap reconciliation is absent"),
        ("adaptive_subdivision", "adaptive subdivision is absent"),
        ("multi_task_fan_in", "multi-task fan-in is absent"),
        ("routed_verified", "routed artifacts are unverified"),
        ("head_verified", "sharded head artifact is unverified"),
        ("head_sharded", "head is not hash-sharded"),
        ("worker_routed_query", "Worker routed query evidence is absent"),
        ("worker_head_query", "Worker head query evidence is absent"),
        # worker_head_query is produced by the ACTUAL geocoder-worker head-shard
        # decoder resolving real tokens from locally-built PLHD shard bytes. The
        # spec names this the worker_local_decoder_evidence class: no deployed
        # Worker or R2 fetch is required or implied. Require the class flag so the
        # evidence provenance is explicit and honest.
        ("worker_local_decoder_evidence", "Worker local-decoder evidence is absent"),
        (
            "worker_entity_phrase_decoder",
            "Worker entity-phrase decoder/validation evidence is absent",
        ),
        ("resume_before_projection", "resume-before-projection evidence is absent"),
    ):
        require(rehearsal.get(field) is True, reason)
    sharded_spec = (
        spec.get("acceptance_gates", {}).get("head", {}).get("sharded", {})
        if isinstance(spec.get("acceptance_gates"), dict)
        else {}
    )
    require(
        sharded_spec.get("manifest_schema") == "overture-places-global-head-sharded-v2",
        "sharded head manifest schema differs from spec",
    )
    require(
        sharded_spec.get("independent_reduce_side_binding_required") is True,
        "spec does not require the independent reduce-side head binding",
    )
    require(
        spec.get("acceptance_gates", {})
        .get("coverage", {})
        .get("worker_local_decoder_evidence_required")
        is True,
        "spec does not require worker local-decoder evidence",
    )
    require(
        rehearsal.get("interruption_phases") == coverage["interruption_phases"],
        "interruption phase set differs",
    )
    require(
        rehearsal.get("head_result_cap") == gates["head"]["result_cap_per_token"],
        "head result cap differs",
    )
    require(
        rehearsal.get("entity_phrase_admission")
        == (entity_phrase or {}).get("admission"),
        "head entity-phrase admission capability differs",
    )
    require(
        type(rehearsal.get("entity_phrase_head_index_entries")) is int
        and rehearsal["entity_phrase_head_index_entries"] > 0,
        "head entity-phrase index coverage is absent",
    )
    require(
        type(rehearsal.get("entity_phrase_head_records")) is int
        and rehearsal["entity_phrase_head_records"] > 0,
        "head entity-phrase record coverage is absent",
    )
    phrase_by_prefix = rehearsal.get("entity_phrase_head_by_prefix", {})
    for prefix in (entity_phrase or {}).get("key_prefixes", []):
        prefix_evidence = phrase_by_prefix.get(prefix, {})
        require(
            type(prefix_evidence.get("index_entries")) is int
            and prefix_evidence["index_entries"] > 0
            and type(prefix_evidence.get("records")) is int
            and prefix_evidence["records"] > 0,
            f"head entity-phrase coverage is absent for {prefix}",
        )
    require(
        rehearsal.get("routed_entity_phrase_index_entries") == 0
        and rehearsal.get("routed_entity_phrases_absent") is True,
        "entity-phrase keys entered routed serving artifacts",
    )
    require(
        rehearsal.get("maximum_worker_index_probes", 10**9)
        <= gates["serving"]["maximum_index_probe_entries"],
        "Worker index probe cap differs",
    )
    map_resources = rehearsal.get("map_stage_resources")
    require(
        isinstance(map_resources, list)
        and [item.get("task_index") for item in map_resources] == role_tasks,
        "map-stage resource task set/order differs from selected roles",
    )
    resource_gate = gates["resources"]
    headroom = 1 - resource_gate["resource_headroom_min_fraction"]
    require(
        type(rehearsal.get("head_output_bytes")) is int
        and rehearsal["head_output_bytes"]
        <= resource_gate["head_output_hard_cap_bytes"] * headroom,
        "head output lacks frozen resource headroom",
    )
    for item in map_resources if isinstance(map_resources, list) else []:
        task_index = item.get("task_index")
        for stage, scratch_field in (
            ("transform", "peak_scratch_bytes"),
            ("construction", "peak_scratch_and_output_bytes"),
        ):
            measured = item.get(stage, {})
            require(
                type(measured.get("peak_rss_bytes")) is int
                and measured["peak_rss_bytes"]
                <= resource_gate["process_group_rss_hard_cap_bytes"] * headroom,
                f"map task {task_index} {stage} RSS lacks headroom",
            )
            require(
                type(measured.get(scratch_field)) is int
                and measured[scratch_field]
                <= resource_gate["scratch_hard_cap_bytes"] * headroom,
                f"map task {task_index} {stage} scratch lacks headroom",
            )
            require(
                isinstance(measured.get("wall_seconds"), (int, float))
                and measured["wall_seconds"]
                <= resource_gate["candidate_wall_hard_cap_seconds"] * headroom,
                f"map task {task_index} {stage} wall time lacks headroom",
            )

    report = {
        "schema": "overture-places-construction-v1-readiness-v1",
        "ready": not reasons,
        "evidence_spec_sha256": spec_sha256,
        "inventory_sha256": inventory.get("inventory_sha256"),
        "scale_evidence_sha256": sha256_file(evidence_path),
        "reasons": reasons,
        "observations": {
            # Informational, never gated: duplicate coverage is enforced by the
            # synthetic fixture gate in the test suite, not by planet data.
            "duplicate_uuid": duplicate_observation,
        },
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = validate(args.spec, args.inventory, args.evidence)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))
    if not report["ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
