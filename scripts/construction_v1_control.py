#!/usr/bin/env python3
"""Fail-closed admission contract for the Address + Places construction-v1 run.

This module deliberately contains no cloud client and cannot start a build.  It
creates the immutable review package consumed by the dormant hosted workflow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
RELEASE = "2026-06-17.0"

FAMILIES = {
    "addresses": {
        "inventory": "benchmarks/address-construction-v1-data/inventory/addresses.json",
        "inventory_file_sha256": "7b13abc149fee69d4931d04dd4f98ed65336e9685d8f3422c6598aa729f1db19",
        "inventory_sha256": "6a306fc9937dac82602dbc5233952c1f74fdb0f7467ad4cc38dcc559dfc9d34e",
        "schema_fingerprint_sha256": "05260dc6878478fe750a82ad3fb9ddd2fdffcda3f25c00f950acfccca132d7e0",
        "spec": "benchmarks/address-construction-v1-evidence-spec-v3.json",
        "spec_sha256": "130207f3debde346cc9c1178e5038e2257e883ccd46c5826d1a5ae22c2583af9",
        "readiness": "benchmarks/address-construction-v1-data/evidence/readiness-final-v3.json",
        "readiness_file_sha256": "f3a11863637151eaf255b79993737e3b595a3674f742315bd852691c360e118e",
        "scale_evidence_sha256": "dce535350bcb97b1871fa81e5a3e9c863b9b0ce8969175f743705237a9d980ea",
        "task_source": "readiness",
        "construction": "address-construction-v1",
    },
    "places": {
        "inventory": "benchmarks/places-construction-v1-data/inventory/places.json",
        "inventory_file_sha256": "0a5eaa1ce23a7c71ec4d6303059c0e5e829ba7402d3bac33802bbac16150c2eb",
        "inventory_sha256": "b1830aee50ea61395cda14f6b04888d846dcba12f24967c7ab52c64fe5944eff",
        "schema_fingerprint_sha256": "49453ed2b28a7940fe6664b13ec89631fbee2d98efdad0ff8ab1a26972212a5a",
        "spec": "benchmarks/places-construction-v1-evidence-spec.json",
        "spec_sha256": "0baee5f19bc3419995f504aec9bc6baf31d85b31d130a25fbf8114bbcd429dab",
        "readiness": "benchmarks/places-construction-v1-data/evidence/readiness-v1.json",
        "readiness_file_sha256": "8b6888e15c2471a36b0943c51100627ed091cab001dbde785c8e404c70440445",
        "scale_evidence_sha256": "9043241ac6ff332605ea11709269a9fe582612e65cfdc328a1b52b57d613f23f",
        "task_source": "inventory",
        "construction": "places-construction-v1",
    },
}

VERSIONS = {
    "python": "3.12.12",
    "duckdb": "1.5.1",
    "numpy": "2.3.5",
    "pyarrow": "25.0.0",
    "rustc": "1.97.1 (8bab26f4f 2026-07-14)",
    "cargo": "1.97.1 (c980f4866 2026-06-30)",
    "arrow_ipc": "construction-v1-arrow-ipc-v1",
    "shuffle_parquet": "construction-v1-shuffle-parquet-v1",
    "directory": "construction-v1-directory-v1",
    "proof": "construction-v1-proof-v1",
    "serving": "construction-v1-serving-v1",
}

CAPS = {
    "max_parallel": 4,
    "max_total_runner_minutes": 40_000,
    "prior_runner_minutes": 0,
    "max_cost_usd": "1200.00",
    "max_reducers_per_family": 128,
    "max_remote_operations": 100_000,
    "max_remote_write_bytes": 1_000_000_000_000,
    "max_cleanup_objects": 20_000,
    "max_cleanup_bytes": 250_000_000_000,
    "map_wall_minutes": {"addresses": 60, "places": 45},
    "reduce_wall_minutes": 90,
}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(relative: str) -> dict[str, Any]:
    return json.loads((ROOT / relative).read_text())


def family_status(name: str, contract: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    for field in ("inventory", "spec", "readiness"):
        actual = sha256_file(ROOT / contract[field])
        expected = contract[f"{field}_file_sha256"] if field in ("inventory", "readiness") else contract[f"{field}_sha256"]
        if actual != expected:
            errors.append(f"{name} {field} file SHA-256 differs: {actual}")
    inventory = read_json(contract["inventory"])
    readiness = read_json(contract["readiness"])
    readiness_identity = {**readiness.get("checks", {}), **readiness}
    readiness_identity = {
        **readiness.get("checks", {}).get("canonical_inventory_identity", {}),
        **readiness_identity,
    }
    if inventory.get("release") != RELEASE:
        errors.append(f"{name} inventory release differs")
    if inventory.get("inventory_sha256") != contract["inventory_sha256"]:
        errors.append(f"{name} inventory content identity differs")
    schema = inventory.get("schema_contract", {}).get("fingerprint_sha256")
    if schema != contract["schema_fingerprint_sha256"]:
        errors.append(f"{name} schema fingerprint differs")
    for field in ("inventory_sha256", "evidence_spec_sha256", "scale_evidence_sha256"):
        expected = contract["spec_sha256"] if field == "evidence_spec_sha256" else contract[field]
        if readiness_identity.get(field) != expected:
            errors.append(f"{name} readiness {field} differs")
    if readiness.get("ready") is not True:
        reasons = readiness.get("reasons") or readiness.get("blockers") or ["readiness is false"]
        errors.extend(f"{name} readiness: {reason}" for reason in reasons)
    return readiness, errors


def map_tasks(name: str, contract: dict[str, Any], readiness: dict[str, Any]) -> list[dict[str, Any]]:
    inventory = read_json(contract["inventory"])
    if contract["task_source"] == "readiness":
        tasks = readiness["checks"]["canonical_inventory_identity"]["tasks"]
    else:
        tasks = inventory["map_plan"]["tasks"]
    matrix = []
    for task in tasks:
        digest = task.get("task_digest_sha256", task.get("task_digest"))
        source_digest = task.get("source_digest_sha256", task.get("source_digest"))
        matrix.append({
            "family": name,
            "task_id": f"{name}-map-{task['index']:03d}",
            "task_index": task["index"],
            "task_digest": digest,
            "source_digest": source_digest,
            "expected_input_records": task.get("rows", task.get("expected_input_records")),
            "selected_uncompressed_bytes": task["selected_uncompressed_bytes"],
            "ranges": task["ranges"],
        })
    return matrix


def confirmation(request_sha256: str, caps: dict[str, Any]) -> str:
    return (
        f"EXECUTE_CONSTRUCTION_V1::{request_sha256}"
        f"::MODE=execute::MAX_PARALLEL={caps['max_parallel']}"
        f"::MAX_TOTAL_RUNNER_MINUTES={caps['max_total_runner_minutes']}"
        f"::PRIOR_RUNNER_MINUTES={caps['prior_runner_minutes']}"
        f"::MAX_COST_USD={caps['max_cost_usd']}"
    )


def prepare(values: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    blockers: list[str] = []
    ids = [values.request_id, values.build_id, values.slice_id, values.staging_id]
    if len(set(ids)) != 4 or any(not SAFE_ID.fullmatch(value) for value in ids):
        blockers.append("request/build/slice/staging IDs must be four distinct canonical fresh IDs")
    if not HEX40.fullmatch(values.producer_commit):
        blockers.append("producer commit must be an exact lowercase 40-hex commit")
    if not values.legacy_core_version or not values.legacy_core_manifest_sha256:
        blockers.append("exact legacy core version and release-manifest SHA-256 are required")
    elif not HEX64.fullmatch(values.legacy_core_manifest_sha256):
        blockers.append("legacy core release-manifest SHA-256 is not canonical")

    # Retained runner minutes from earlier attempts. A fresh resume dispatch
    # binds them into both the request and the typed confirmation so the honest
    # prior total cannot be silently reset to zero.
    prior_runner_minutes = int(getattr(values, "prior_runner_minutes", 0) or 0)
    if prior_runner_minutes < 0:
        blockers.append("prior runner minutes must be a non-negative integer")
    caps = {**CAPS, "prior_runner_minutes": prior_runner_minutes}

    readiness: dict[str, Any] = {}
    matrices: dict[str, list[dict[str, Any]]] = {}
    family_contracts: dict[str, Any] = {}
    for name, base in FAMILIES.items():
        status, errors = family_status(name, base)
        readiness[name] = {"ready": status.get("ready") is True and not errors, "file": base["readiness"], "file_sha256": base["readiness_file_sha256"]}
        blockers.extend(errors)
        matrices[name] = map_tasks(name, base, status)
        family_contracts[name] = {key: value for key, value in base.items() if key != "task_source"}

    request: dict[str, Any] | None = None
    request_sha: str | None = None
    typed: str | None = None
    if values.legacy_core_version and values.legacy_core_manifest_sha256 and HEX40.fullmatch(values.producer_commit):
        request = {
            "schema": "overture-construction-v1-request-v1",
            "mode": "execute",
            "identity": dict(zip(("request_id", "build_id", "slice_id", "staging_id"), ids)),
            "producer_commit": values.producer_commit,
            "release": RELEASE,
            "lineage": {"genesis": True, "generation": 1, "predecessor": None},
            "legacy_core": {
                "version": values.legacy_core_version,
                "release": RELEASE,
                "manifest_key": f"{values.legacy_core_version}/release-manifest.json",
                "manifest_sha256": values.legacy_core_manifest_sha256,
                "access": "read-only-head-and-range-get",
            },
            "families": family_contracts,
            "versions": {**VERSIONS, "cargo_lock_sha256": sha256_file(ROOT / "crates/Cargo.lock"), "address_source_sha256": sha256_file(ROOT / "scripts/address_construction_v1.py"), "places_source_sha256": sha256_file(ROOT / "scripts/places_construction_v1.py")},
            "caps": caps,
            "publication": {"production_writes": False, "non_promoting_slice": True, "preview_only": True},
        }
        # Avoid a self-referential fixed point: namespace binds the independently hashed request core.
        namespace_binding = sha256_bytes(canonical(request))
        root = f"construction-v1/{namespace_binding}"
        request["namespaces"] = {
            "binding_sha256": namespace_binding,
            "immutable_root": root,
            "staging": f"{root}/staging/{values.staging_id}/",
            "content": f"{root}/content/sha256/",
            "markers": f"{root}/markers/",
            "slice": f"{root}/slice/{values.slice_id}/",
            "preview": f"{root}/preview/{values.slice_id}/",
            "forbidden": ["catalog.json", "v2/catalog.json", "v2/releases/"],
        }
        request_sha = sha256_bytes(canonical(request))
        typed = confirmation(request_sha, caps)

    projected_minutes = 30 + len(matrices["addresses"]) * 60 + len(matrices["places"]) * 45 + 2 * CAPS["max_reducers_per_family"] * CAPS["reduce_wall_minutes"] + 300
    if prior_runner_minutes + projected_minutes > CAPS["max_total_runner_minutes"]:
        blockers.append("prior plus projected runner minutes exceed the admitted cap")
    report = {
        "schema": "overture-construction-v1-review-package-v1",
        "admitted": not blockers and request is not None and all(item["ready"] for item in readiness.values()),
        "blockers": blockers,
        "request_sha256": request_sha,
        "typed_confirmation": typed,
        "request": request,
        "readiness": readiness,
        "map_matrices": matrices,
        "reducer_matrices": {
            name: {"derivation": "adaptive-genesis-plan-v1", "replaceable": True, "maximum_entries": CAPS["max_reducers_per_family"], "admitted_marker_set": [task["task_id"] for task in matrix]}
            for name, matrix in matrices.items()
        },
        "cost": {"projected_runner_minutes_upper_bound": projected_minutes, "prior_runner_minutes": prior_runner_minutes, "max_total_runner_minutes": CAPS["max_total_runner_minutes"], "max_cost_usd": CAPS["max_cost_usd"]},
        "next_action": "Satisfy every blocker, rerun prepare, review the canonical request, then dispatch once with its exact hash and typed confirmation.",
    }
    return report, report["admitted"]


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    for name in ("request-id", "build-id", "slice-id", "staging-id", "producer-commit"):
        prep.add_argument(f"--{name}", required=True)
    prep.add_argument("--legacy-core-version")
    prep.add_argument("--legacy-core-manifest-sha256")
    prep.add_argument("--prior-runner-minutes", type=int, default=0)
    prep.add_argument("--output", type=Path, required=True)
    admit = sub.add_parser("admit-dispatch")
    admit.add_argument("--request", type=Path, required=True)
    admit.add_argument("--request-sha256", required=True)
    admit.add_argument("--confirmation", required=True)
    admit.add_argument("--run-attempt", type=int, required=True)
    admit.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if args.command == "admit-dispatch":
        request = json.loads(args.request.read_text())
        identity = request.get("identity", {})
        core = request.get("legacy_core", {})
        regenerated, admitted = prepare(argparse.Namespace(
            request_id=identity.get("request_id", ""), build_id=identity.get("build_id", ""),
            slice_id=identity.get("slice_id", ""), staging_id=identity.get("staging_id", ""),
            producer_commit=request.get("producer_commit", ""), legacy_core_version=core.get("version"),
            legacy_core_manifest_sha256=core.get("manifest_sha256"),
            prior_runner_minutes=request.get("caps", {}).get("prior_runner_minutes", 0),
        ))
        actual_sha = sha256_bytes(canonical(request))
        if args.run_attempt != 1:
            raise SystemExit("run_attempt must be exactly 1; create a fresh request for every retry")
        if request != regenerated["request"] or actual_sha != args.request_sha256:
            raise SystemExit("dispatch request differs from the canonical reviewed request")
        if args.confirmation != regenerated["typed_confirmation"]:
            raise SystemExit("typed confirmation differs")
        if not admitted:
            raise SystemExit("readiness or admission failed: " + "; ".join(regenerated["blockers"]))
        if args.github_output:
            compact = lambda family: {"include": [{"task_id": item["task_id"], "task_index": item["task_index"]} for item in regenerated["map_matrices"][family]]}
            with args.github_output.open("a") as output:
                output.write(f"address_matrix={json.dumps(compact('addresses'), separators=(',', ':'))}\n")
                output.write(f"places_matrix={json.dumps(compact('places'), separators=(',', ':'))}\n")
        print(json.dumps({"admitted": True, "request_sha256": actual_sha}, sort_keys=True))
        return 0
    report, admitted = prepare(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical(report))
    print(json.dumps({"admitted": admitted, "request_sha256": report["request_sha256"], "typed_confirmation": report["typed_confirmation"], "output": str(args.output)}, sort_keys=True))
    return 0 if admitted else 1


if __name__ == "__main__":
    sys.exit(main())
