#!/usr/bin/env python3
"""Construction-v1 native hosted adapter.

This is the missing piece between ``construction_v1_control.py`` (fail-closed
admission and the typed confirmation) and the vectorized Address/Places data
planes (``address_construction_v1.py`` / ``places_construction_v1.py``). It:

* derives a deterministic contract + runtime from the reviewed request, so no
  hosted job depends on a file that was never produced;
* wraps each phase (admit / map / plan / reduce / head / finalize) as a CLI
  subcommand driving the real data plane against a single content-addressed
  store directory that the workflow carries between jobs as an artifact;
* publishes only the final slice create-only through the backend-neutral
  ``construction_v1_remote`` primitives (create-only, per-upload HEAD, marker
  written last, one exact-prefix listing + one streaming read per object); and
* keeps an honest runner-minute ledger and fails closed *before* provisioning
  the next phase when prior + projected minutes exceed the confirmation cap.

Every subcommand runs with no network. The end-to-end execute sequence is
proven against a tmpdir store in tests/test_construction_v1_hosted.py, which is
what stops a planet attempt from burning runner-minutes on a file-not-found.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import json
import platform
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ADDRESS = _load("construction_v1_hosted_address", "scripts/address_construction_v1.py")
PLACES = _load("construction_v1_hosted_places", "scripts/places_construction_v1.py")
REMOTE = _load("construction_v1_hosted_remote", "scripts/construction_v1_remote.py")
CONTROL = _load("construction_v1_hosted_control", "scripts/construction_v1_control.py")

FAMILIES = ("addresses", "places")

# Conservative bounded hosted limits. They stay well under a 330-minute job and
# are overridable per run through the contract so a rehearsal can shrink them.
HOSTED_LIMITS: dict[str, dict[str, Any]] = {
    "addresses": {
        "max_input_rows": 4_000_000,
        "max_pack_rows": 1_000_000,
        "parquet_row_group_rows": 65_536,
        "max_rss_bytes": 12 * 1024**3,
        "max_scratch_bytes": 24 * 1024**3,
        "max_output_bytes": 8 * 1024**3,
        "max_serving_bytes": 2 * 1024**3,
        "wall_seconds": 18_000,
        "duckdb_memory_limit": "8GB",
        "duckdb_threads": 4,
        "allow_unpinned_duckdb": False,
    },
    "places": {
        "max_input_rows": 4_000_000,
        "max_pack_rows": 1_500_000,
        "parquet_row_group_rows": 65_536,
        "max_rss_bytes": 12 * 1024**3,
        "max_scratch_bytes": 24 * 1024**3,
        "max_output_bytes": 8 * 1024**3,
        "wall_seconds": 18_000,
        "allow_unpinned_duckdb": False,
        "partition_term_rows": 1_000_000,
        "partition_estimated_bytes": 512 * 1024**2,
        "partition_distinct_tokens": 200_000,
    },
}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text())


def _family_module(family: str):
    return ADDRESS if family == "addresses" else PLACES


def _limits_for(contract: dict[str, Any], family: str):
    module = _family_module(family)
    values = dict(contract["limits"][family])
    fields = {field.name for field in dataclasses.fields(module.Limits)}
    filtered = {key: value for key, value in values.items() if key in fields}
    return module.Limits(**filtered)


def _store(store_root: str):
    return ADDRESS.LocalObjectStore(Path(store_root))


def _reduce_marker_key(family: str, index: int) -> str:
    return f"reduce/{family}/tasks/{index:04d}/complete.json"


def _head_marker_key() -> str:
    return "head/places/complete.json"


# --------------------------------------------------------------------------- #
# derive-contract
# --------------------------------------------------------------------------- #
def cmd_derive_contract(args: argparse.Namespace) -> int:
    request = read_json(args.request)
    request_sha256 = hashlib.sha256(CONTROL.canonical(request)).hexdigest()
    limits = {family: dict(HOSTED_LIMITS[family]) for family in FAMILIES}
    if args.allow_unpinned_duckdb:
        for family in FAMILIES:
            limits[family]["allow_unpinned_duckdb"] = True
    if args.map_input_rows_cap is not None:
        for family in FAMILIES:
            limits[family]["max_input_rows"] = args.map_input_rows_cap
    contract = {
        "schema": "construction-v1-contract-v1",
        "request_sha256": request_sha256,
        "release": request.get("release"),
        "families": request.get("families", {}),
        "namespaces": request.get("namespaces", {}),
        "caps": request.get("caps", {}),
        "limits": limits,
    }
    write_json(args.output, contract)

    pinned = request.get("versions", {})
    runtime = {
        "schema": "construction-v1-runtime-v1",
        "request_sha256": request_sha256,
        "python": platform.python_version(),
        "pinned": {key: pinned.get(key) for key in ("python", "duckdb", "pyarrow", "numpy", "rustc")},
        "strict_versions": bool(args.strict_versions),
    }
    if args.strict_versions:
        try:
            import duckdb  # noqa: WPS433

            if duckdb.__version__ != pinned.get("duckdb"):
                raise SystemExit(
                    f"runner duckdb {duckdb.__version__} != pinned {pinned.get('duckdb')}"
                )
            runtime["duckdb"] = duckdb.__version__
        except ImportError as error:
            raise SystemExit("duckdb is required in strict-version mode") from error
    write_json(args.runtime, runtime)
    print(json.dumps({"request_sha256": request_sha256, "contract": str(args.output)}, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# admit-task
# --------------------------------------------------------------------------- #
def cmd_admit_task(args: argparse.Namespace) -> int:
    store = _store(args.store_root)
    if args.phase == "map":
        key = _family_module(args.family).marker_key(args.task_id)
    elif args.phase == "reduce":
        key = _reduce_marker_key(args.family, args.index)
    elif args.phase == "head":
        key = _head_marker_key()
    else:
        raise SystemExit(f"unknown phase {args.phase}")
    completed = store.read_json(key) is not None
    result = {"phase": args.phase, "family": args.family, "marker_key": key, "completed": completed}
    if args.output:
        write_json(args.output, result)
    print(json.dumps(result, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# run-map
# --------------------------------------------------------------------------- #
def cmd_run_map(args: argparse.Namespace) -> int:
    contract = read_json(args.contract)
    store = _store(args.store_root)
    limits = _limits_for(contract, args.family)
    request_sha256 = contract["request_sha256"]
    common = dict(
        input_path=Path(args.input),
        source_limits=Path(args.source_limits),
        store=store,
        scratch_root=Path(args.scratch_dir),
        request_sha256=request_sha256,
        task_id=args.task_id,
        transform_binary=Path(args.transform_binary),
        limits=limits,
    )
    if args.family == "addresses":
        marker = ADDRESS.map_task(directory_binary=Path(args.proof_binary), **common)
    else:
        marker = PLACES.map_task(proof_binary=Path(args.proof_binary), **common)
    write_json(args.marker_out, marker)
    summary = {
        "task_id": args.task_id,
        "family": args.family,
        "admitted_existing": marker.get("admitted_existing"),
        "records": marker["binding"]["records"],
        "packs": len(marker["packs"]),
    }
    if args.output:
        write_json(args.output, summary)
    print(json.dumps(summary, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# plan-reduce
# --------------------------------------------------------------------------- #
def _load_markers(markers_dir: str) -> list[dict[str, Any]]:
    paths = sorted(Path(markers_dir).glob("*.json"))
    if not paths:
        raise SystemExit(f"no map markers found under {markers_dir}")
    return [json.loads(path.read_text()) for path in paths]


def cmd_plan_reduce(args: argparse.Namespace) -> int:
    contract = read_json(args.contract)
    store = _store(args.store_root)
    limits = _limits_for(contract, args.family)
    markers = _load_markers(args.markers_dir)
    if args.family == "addresses":
        row_cap = args.row_cap or limits.max_pack_rows
        plan = ADDRESS.genesis_plan(markers, row_cap=row_cap)
    else:
        plan = PLACES.adaptive_genesis_plan(
            markers, store=store, scratch_root=Path(args.scratch_dir), limits=limits
        )
    write_json(args.output, plan)
    matrix = {"include": [{"partition_index": index} for index in range(len(plan["partitions"]))]}
    if args.matrix_out:
        write_json(args.matrix_out, matrix)
    summary = {"family": args.family, "partitions": len(plan["partitions"]), "binding": plan["binding"]}
    print(json.dumps(summary, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# run-reduce
# --------------------------------------------------------------------------- #
def cmd_run_reduce(args: argparse.Namespace) -> int:
    contract = read_json(args.contract)
    store = _store(args.store_root)
    limits = _limits_for(contract, args.family)
    plan = read_json(args.plan)
    markers = _load_markers(args.markers_dir)
    partition = plan["partitions"][args.partition_index]
    if args.family == "addresses":
        reduction = ADDRESS.reduce_partition(
            partition=partition,
            markers=markers,
            store=store,
            scratch_root=Path(args.scratch_dir),
            directory_binary=Path(args.proof_binary),
            encoder_binary=Path(args.encoder_binary),
            verifier_binary=Path(args.verifier_binary),
            limits=limits,
        )
    else:
        reduction = PLACES.reduce_partition(
            partition=partition,
            plan=plan,
            markers=markers,
            store=store,
            scratch_root=Path(args.scratch_dir),
            encoder_binary=Path(args.encoder_binary),
            verifier_binary=Path(args.verifier_binary),
            limits=limits,
        )
    write_json(args.output, reduction)
    # A per-partition completion marker so admit-task can skip a completed
    # reducer on a fresh resume dispatch.
    store.write_marker_last(
        _reduce_marker_key(args.family, args.partition_index),
        {"partition_index": args.partition_index, "artifact": reduction.get("artifact")},
    )
    print(json.dumps({"family": args.family, "partition_index": args.partition_index,
                      "artifact": reduction.get("artifact")}, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# run-head (Places only)
# --------------------------------------------------------------------------- #
def cmd_run_head(args: argparse.Namespace) -> int:
    contract = read_json(args.contract)
    store = _store(args.store_root)
    if args.family != "places":
        write_json(args.output, {"family": args.family, "head": None, "note": "no global head phase"})
        print(json.dumps({"family": args.family, "head": None}, sort_keys=True))
        return 0
    limits = _limits_for(contract, "places")
    markers = _load_markers(args.markers_dir)
    result = PLACES.build_sharded_global_head_from_markers(
        markers=markers,
        store=store,
        scratch_root=Path(args.scratch_dir),
        encoder_binary=Path(args.encoder_binary),
        verifier_binary=Path(args.verifier_binary),
        limits=limits,
        shard_bits=args.shard_bits,
    )
    write_json(args.output, result)
    store.write_marker_last(_head_marker_key(), {"shard_count": result["shard_count"],
                                                 "total_records": result["total_records"]})
    print(json.dumps({"family": "places", "shard_count": result["shard_count"],
                      "populated_shards": result["populated_shards"]}, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# finalize
# --------------------------------------------------------------------------- #
def _artifact_keys(family: str, reductions: list[dict[str, Any]], head: dict[str, Any] | None) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for reduction in reductions:
        artifact = reduction.get("artifact")
        if artifact:
            objects.append(artifact)
    if head:
        objects.extend(head.get("shard_objects", []))
    return objects


def cmd_finalize(args: argparse.Namespace) -> int:
    contract = read_json(args.contract)
    store = _store(args.store_root)
    plan = read_json(args.plan)
    reductions = [read_json(path) for path in sorted(Path(args.reductions_dir).glob("*.json"))]
    if not reductions:
        raise SystemExit("finalize requires at least one reduction")
    head = read_json(args.head) if args.head and Path(args.head).exists() else None

    if args.family == "addresses":
        reconciliation = ADDRESS.validate_complete_reduction(plan, reductions)
    else:
        binding = ADDRESS.combine_bindings([item["binding"] for item in reductions])
        if binding != plan["binding"]:
            raise SystemExit("places reduction binding differs from the genesis plan")
        reconciliation = {"partitions": len(reductions), "binding": binding, "reconciles": True}

    request_sha256 = contract["request_sha256"]
    slice_root = contract["namespaces"]["slice"].rstrip("/")
    artifacts = _artifact_keys(args.family, reductions, head)
    if len({item["key"] for item in artifacts}) != len(artifacts):
        raise SystemExit("duplicate serving artifact key")

    family_manifest = {
        "schema": "construction-v1-family-manifest-v1",
        "family": args.family,
        "request_sha256": request_sha256,
        "reconciles": reconciliation["reconciles"],
        "binding": reconciliation["binding"],
        "partitions": reconciliation["partitions"],
        "artifacts": sorted(artifacts, key=lambda item: item["key"]),
        "head": {"shard_count": head["shard_count"], "total_records": head["total_records"]} if head else None,
    }
    family_manifest_path = Path(args.work_root) / "family-manifest.json"
    write_json(family_manifest_path, family_manifest)

    slice_manifest = {
        "schema": "construction-v1-slice-manifest-v1",
        "request_sha256": request_sha256,
        "family": args.family,
        "family_manifest_sha256": hashlib.sha256(family_manifest_path.read_bytes()).hexdigest(),
        "object_count": len(artifacts),
        "non_promoting": True,
    }
    slice_manifest_path = Path(args.work_root) / "slice-manifest.json"
    write_json(slice_manifest_path, slice_manifest)

    # Create-only publication of the exact final set, marker last.
    budget = REMOTE.Budget(
        max_operations=int(contract["caps"].get("max_remote_operations", 100_000)),
        max_write_bytes=int(contract["caps"].get("max_remote_write_bytes", 1_000_000_000_000)),
        max_read_bytes=int(contract["caps"].get("max_remote_write_bytes", 1_000_000_000_000)),
    )
    remote = REMOTE.FilesystemRemote(Path(args.remote_root), budget)
    exact_set: list[tuple[str, Path]] = [
        (f"{slice_root}/families/{args.family}/family-manifest.json", family_manifest_path),
        (f"{slice_root}/families/{args.family}/slice-manifest.json", slice_manifest_path),
    ]
    for artifact in artifacts:
        exact_set.append((f"{slice_root}/families/{args.family}/objects/{Path(artifact['key']).name}",
                          store.path(artifact["key"])))
    marker_key = f"{contract['namespaces']['markers'].rstrip('/')}/finalize/{args.family}.json"
    marker = REMOTE.publish_exact_set(
        remote,
        artifacts=exact_set,
        marker_key=marker_key,
        request_sha256=request_sha256,
        fail_after_upload=args.fail_after_upload,
    )
    expected = [{"key": key, "sha256": REMOTE.file_identity(path)["sha256"],
                 "bytes": REMOTE.file_identity(path)["bytes"]} for key, path in exact_set]
    verification = REMOTE.verify_whole_slice_once(
        remote, prefix=f"{slice_root}/families/{args.family}/", expected=expected
    )
    result = {
        "family": args.family,
        "reconciles": reconciliation["reconciles"],
        "objects": verification["objects"],
        "bytes": verification["bytes"],
        "marker_key": marker_key,
        "marker_written_last": True,
        "verification": verification,
    }
    write_json(args.output, result)
    print(json.dumps({"family": args.family, "objects": verification["objects"],
                      "reconciles": reconciliation["reconciles"], "marker_written_last": True}, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #
def cmd_ledger_append(args: argparse.Namespace) -> int:
    ledger = read_json(args.ledger)
    ledger.setdefault("phases", []).append({"phase": args.phase, "runner_minutes": args.minutes})
    write_json(args.ledger, ledger)
    total = int(ledger.get("prior_runner_minutes", 0)) + sum(int(item["runner_minutes"]) for item in ledger["phases"])
    print(json.dumps({"phase": args.phase, "minutes": args.minutes, "consumed_runner_minutes": total}, sort_keys=True))
    return 0


def cmd_ledger_check(args: argparse.Namespace) -> int:
    ledger = read_json(args.ledger)
    prior = int(ledger.get("prior_runner_minutes", 0))
    spent = sum(int(item["runner_minutes"]) for item in ledger.get("phases", []))
    cap = int(ledger["max_total_runner_minutes"])
    projected = prior + spent + int(args.next_phase_minutes)
    result = {
        "prior_runner_minutes": prior,
        "spent_runner_minutes": spent,
        "next_phase_minutes": int(args.next_phase_minutes),
        "projected_runner_minutes": projected,
        "max_total_runner_minutes": cap,
        "within_cap": projected <= cap,
    }
    if args.output:
        write_json(args.output, result)
    print(json.dumps(result, sort_keys=True))
    if projected > cap:
        raise SystemExit(
            f"projected {projected} runner minutes exceed cap {cap} "
            f"(prior={prior} spent={spent} next={args.next_phase_minutes}); failing closed before the next phase"
        )
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    derive = sub.add_parser("derive-contract")
    derive.add_argument("--request", type=Path, required=True)
    derive.add_argument("--output", type=Path, required=True)
    derive.add_argument("--runtime", type=Path, required=True)
    derive.add_argument("--allow-unpinned-duckdb", action="store_true")
    derive.add_argument("--strict-versions", action="store_true")
    derive.add_argument("--map-input-rows-cap", type=int, default=None)
    derive.set_defaults(func=cmd_derive_contract)

    admit = sub.add_parser("admit-task")
    admit.add_argument("--store-root", required=True)
    admit.add_argument("--family", choices=FAMILIES, required=True)
    admit.add_argument("--phase", choices=["map", "reduce", "head"], required=True)
    admit.add_argument("--task-id", default="")
    admit.add_argument("--index", type=int, default=0)
    admit.add_argument("--output", type=Path)
    admit.set_defaults(func=cmd_admit_task)

    run_map = sub.add_parser("run-map")
    run_map.add_argument("--contract", type=Path, required=True)
    run_map.add_argument("--store-root", required=True)
    run_map.add_argument("--family", choices=FAMILIES, required=True)
    run_map.add_argument("--task-id", required=True)
    run_map.add_argument("--input", required=True)
    run_map.add_argument("--source-limits", required=True)
    run_map.add_argument("--transform-binary", required=True)
    run_map.add_argument("--proof-binary", required=True)
    run_map.add_argument("--scratch-dir", required=True)
    run_map.add_argument("--marker-out", type=Path, required=True)
    run_map.add_argument("--output", type=Path)
    run_map.set_defaults(func=cmd_run_map)

    plan = sub.add_parser("plan-reduce")
    plan.add_argument("--contract", type=Path, required=True)
    plan.add_argument("--store-root", required=True)
    plan.add_argument("--family", choices=FAMILIES, required=True)
    plan.add_argument("--markers-dir", required=True)
    plan.add_argument("--scratch-dir", default="/tmp/construction-v1-plan-scratch")
    plan.add_argument("--row-cap", type=int, default=None)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--matrix-out", type=Path)
    plan.set_defaults(func=cmd_plan_reduce)

    reduce = sub.add_parser("run-reduce")
    reduce.add_argument("--contract", type=Path, required=True)
    reduce.add_argument("--store-root", required=True)
    reduce.add_argument("--family", choices=FAMILIES, required=True)
    reduce.add_argument("--plan", type=Path, required=True)
    reduce.add_argument("--markers-dir", required=True)
    reduce.add_argument("--partition-index", type=int, required=True)
    reduce.add_argument("--proof-binary", default="")
    reduce.add_argument("--encoder-binary", required=True)
    reduce.add_argument("--verifier-binary", required=True)
    reduce.add_argument("--scratch-dir", required=True)
    reduce.add_argument("--output", type=Path, required=True)
    reduce.set_defaults(func=cmd_run_reduce)

    head = sub.add_parser("run-head")
    head.add_argument("--contract", type=Path, required=True)
    head.add_argument("--store-root", required=True)
    head.add_argument("--family", choices=FAMILIES, required=True)
    head.add_argument("--markers-dir", required=True)
    head.add_argument("--encoder-binary", default="")
    head.add_argument("--verifier-binary", default="")
    head.add_argument("--scratch-dir", default="/tmp/construction-v1-head-scratch")
    head.add_argument("--shard-bits", type=int, default=4)
    head.add_argument("--output", type=Path, required=True)
    head.set_defaults(func=cmd_run_head)

    final = sub.add_parser("finalize")
    final.add_argument("--contract", type=Path, required=True)
    final.add_argument("--store-root", required=True)
    final.add_argument("--family", choices=FAMILIES, required=True)
    final.add_argument("--plan", type=Path, required=True)
    final.add_argument("--reductions-dir", required=True)
    final.add_argument("--head", type=Path, default=None)
    final.add_argument("--remote-root", required=True)
    final.add_argument("--work-root", required=True)
    final.add_argument("--fail-after-upload", type=int, default=None)
    final.add_argument("--output", type=Path, required=True)
    final.set_defaults(func=cmd_finalize)

    append = sub.add_parser("ledger-append")
    append.add_argument("--ledger", type=Path, required=True)
    append.add_argument("--phase", required=True)
    append.add_argument("--minutes", type=int, required=True)
    append.set_defaults(func=cmd_ledger_append)

    check = sub.add_parser("ledger-check")
    check.add_argument("--ledger", type=Path, required=True)
    check.add_argument("--next-phase-minutes", type=int, default=0)
    check.add_argument("--output", type=Path)
    check.set_defaults(func=cmd_ledger_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
