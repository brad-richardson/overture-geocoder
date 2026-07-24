#!/usr/bin/env python3
"""Fail-closed local validator for Address construction-v1 planet evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_inventory_module():
    path = ROOT / "scripts/inventory_address_rowgroups.py"
    spec = importlib.util.spec_from_file_location("address_inventory", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load inventory validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate(spec_path: Path, evidence_path: Path | None) -> dict[str, Any]:
    spec = json.loads(spec_path.read_text())
    blockers: list[str] = []
    checks: dict[str, Any] = {}
    inventory_path = ROOT / spec["inventory"]["path"]
    checks["evidence_spec_sha256"] = sha256_file(spec_path)
    checks["inventory_file_sha256"] = sha256_file(inventory_path)
    if checks["inventory_file_sha256"] != spec["inventory"]["file_sha256"]:
        blockers.append("pinned inventory file SHA-256 differs")
    inventory = json.loads(inventory_path.read_text())
    try:
        identity = load_inventory_module().validate_canonical_inventory(inventory)
    except (KeyError, TypeError, ValueError) as exc:
        blockers.append(f"canonical inventory binding is unavailable: {exc}")
        identity = None
    checks["canonical_inventory_identity"] = identity

    versions = {}
    for name in ("duckdb", "numpy", "pyarrow"):
        try:
            module = __import__(name)
            versions[name] = module.__version__
        except ImportError:
            versions[name] = None
        if versions[name] != spec["runtime"][name]:
            blockers.append(
                f"runtime {name} is {versions[name]!r}, expected {spec['runtime'][name]!r}"
            )
    versions["python"] = ".".join(str(item) for item in sys.version_info[:3])
    if versions["python"] != spec["runtime"]["python"]:
        blockers.append("Python runtime differs from frozen evidence spec")
    checks["runtime"] = versions

    if evidence_path is None or not evidence_path.is_file():
        blockers.append("scale evidence JSON is absent")
    else:
        evidence = json.loads(evidence_path.read_text())
        if evidence.get("evidence_spec_sha256") != checks["evidence_spec_sha256"]:
            blockers.append("scale evidence is not bound to this evidence spec")
        if evidence.get("inventory_sha256") != (
            identity.get("inventory_sha256") if identity else None
        ):
            blockers.append("scale evidence inventory identity differs")
        if evidence.get("all_gates_passed") is not True:
            blockers.append("one or more frozen scale gates did not pass")
        checks["scale_evidence_sha256"] = sha256_file(evidence_path)

    return {
        "schema": "overture-address-construction-v1-readiness-v1",
        "ready": not blockers,
        "blockers": blockers,
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--spec",
        type=Path,
        default=ROOT / "benchmarks/address-construction-v1-evidence-spec.json",
    )
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = validate(args.spec, args.evidence)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload)
    else:
        print(payload, end="")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
