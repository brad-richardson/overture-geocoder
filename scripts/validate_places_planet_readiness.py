#!/usr/bin/env python3
"""Fail-closed validator for frozen Places construction-v1 planet evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
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


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_inventory_module():
    path = ROOT / "scripts/global_v2_places_inventory.py"
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

    return {
        "python": platform.python_version(),
        "duckdb": duckdb.__version__,
        "numpy": numpy.__version__,
        "pyarrow": pyarrow.__version__,
        "rustc": subprocess.check_output(["rustc", "--version"], text=True)
        .strip()
        .removeprefix("rustc "),
        "cargo": subprocess.check_output(["cargo", "--version"], text=True)
        .strip()
        .removeprefix("cargo "),
        "python_executable": ".venv/bin/python",
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
        spec.get("schema") == "overture-places-construction-v1-evidence-spec-v2",
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
    if isinstance(raw_census, list):
        for item in raw_census:
            if isinstance(item, dict) and type(item.get("task_index")) is int:
                census[item["task_index"]] = item
    require(sorted(census) == sorted(universe), "census task set is missing or extra")
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
        require(
            projection.get("identity", {}).get("task_digest") == task["task_digest"],
            f"task {task_index} projection identity differs",
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
            projection.get("output", {}).get(
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
    require(
        rehearsal.get("logical_tasks", 0) >= map_reduce["minimum_logical_tasks"],
        "logical task coverage is low",
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
        rehearsal.get("maximum_worker_index_probes", 10**9)
        <= gates["serving"]["maximum_index_probe_entries"],
        "Worker index probe cap differs",
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
