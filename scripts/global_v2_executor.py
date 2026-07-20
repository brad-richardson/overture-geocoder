#!/usr/bin/env python3
"""Fail-closed control plane for the global v2 families-only executor.

This module deliberately does not implement either family's data algorithms.
It binds a retained request to one immutable execution namespace, pins and
fingerprints the hosted toolchain, validates exact task/reducer coverage, and
builds the final non-promoting slice marker from a streaming remote inventory.

The data plane may upload immutable objects while a phase is running. A task,
phase, family, or slice is complete only when its validated completion marker
is written last. Nothing in this module can write ``catalog.json`` or
``v2/catalog.json`` and nothing can target the retained legacy-core prefix.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import global_build_manifest as family_manifests  # noqa: E402
import global_v2_build_request as build_requests  # noqa: E402
import global_v2_places_inventory as places_inventory  # noqa: E402
import inventory_address_rowgroups as address_inventory  # noqa: E402
import r2_verified_store  # noqa: E402
import v2_release_manifest  # noqa: E402


CONTRACT_SCHEMA = "overture-global-v2-execution-contract-v1"
RUNTIME_SCHEMA = "overture-global-v2-executor-runtime-v1"
TASK_COMPLETION_SCHEMA = "overture-global-v2-task-completion-v1"
PHASE_COMPLETION_SCHEMA = "overture-global-v2-phase-completion-v2"
REMOTE_LISTING_SCHEMA = "overture-global-v2-remote-listing-v1"
SLICE_MANIFEST_SCHEMA_VERSION = 1
STOP_EVIDENCE_SCHEMA = "overture-global-v2-stop-evidence-v1"

SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# The workflow pins the non-moving OS label. GitHub's exact ImageOS/ImageVersion
# is recorded in every runtime fingerprint, but ImageVersion is provenance, not
# a cross-job equality gate: GitHub can stage a new image between two matrix
# jobs in the same resumable run. The language/data toolchain remains exact.
EXECUTOR_IMAGE = "github-actions/ubuntu-24.04"
PYTHON_VERSION = "3.11.14"
NUMPY_VERSION = "2.3.5"
PYARROW_VERSION = "25.0.0"
PARQUET_CPP_VERSION = "25.0.0"
PARQUET_FORMAT_VERSION = "2.6"
CONSERVATIVE_RUNNER_USD_PER_MINUTE = "0.0200"

PHASES = (
    "inventory",
    "map",
    "aggregate-plan",
    "reduce",
    "head",
    "remote-verify",
    "finalize-slice",
    "worker-smoke",
)
PHASE_DEPENDENCIES = {
    "inventory": [],
    "map": ["inventory"],
    "aggregate-plan": ["map"],
    "reduce": ["aggregate-plan"],
    "head": ["reduce"],
    "remote-verify": ["reduce", "head"],
    "finalize-slice": ["remote-verify"],
    "worker-smoke": ["finalize-slice"],
}
PHASE_JOB_TIMEOUTS = {
    "inventory": 180,
    "map": 330,
    "aggregate-plan": 330,
    "reduce": 330,
    "head": 330,
    "remote-verify": 330,
    "finalize-slice": 90,
    "worker-smoke": 90,
}
PHASE_ESTIMATED_JOB_MINUTES = {
    "inventory": 120,
    "map": 90,
    "aggregate-plan": 180,
    "reduce": 180,
    "head": 240,
    "remote-verify": 240,
    "finalize-slice": 60,
    "worker-smoke": 60,
}

COUNTER_FIELDS = (
    "input_records",
    "retained_records",
    "rejected_records",
    "output_records",
)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(value))


def require_exact_fields(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ValueError(
            f"{label} fields differ: missing={sorted(expected - set(value))}, "
            f"unknown={sorted(set(value) - expected)}"
        )


def require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def require_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


def require_safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise ValueError(f"{label} must be a safe identifier")
    return value


def load_exact_request(path: Path, expected_sha256: str) -> tuple[dict[str, Any], str]:
    expected_sha256 = require_sha256(expected_sha256, "request_sha256")
    payload = path.read_bytes()
    actual_sha256 = sha256_bytes(payload)
    if actual_sha256 != expected_sha256:
        raise ValueError("retained build request SHA-256 differs")
    request = build_requests.validate_request(json.loads(payload))
    if payload != build_requests.canonical_json(request):
        raise ValueError("retained build request is not canonical producer output")
    return request, actual_sha256


def confirmation_phrase(
    mode: str,
    slice_version: str,
    request_sha256: str,
    *,
    max_parallel: int,
    max_total_runner_minutes: int,
    max_estimated_cost_usd: int,
    prior_runner_minutes: int,
) -> str:
    if mode not in {"dry-run", "execute"}:
        raise ValueError("mode must be dry-run or execute")
    max_parallel = require_int(max_parallel, "max_parallel", 1, 8)
    max_total_runner_minutes = require_int(
        max_total_runner_minutes, "max_total_runner_minutes", 1, 100_000
    )
    max_estimated_cost_usd = require_int(
        max_estimated_cost_usd, "max_estimated_cost_usd", 1, 10_000
    )
    prior_runner_minutes = require_int(
        prior_runner_minutes, "prior_runner_minutes", 0, 100_000
    )
    verb = "DRY_RUN" if mode == "dry-run" else "EXECUTE"
    return (
        f"{verb}_GLOBAL_V2_FAMILIES_ONLY::{slice_version}::{request_sha256}"
        f"::MAX_PARALLEL={max_parallel}"
        f"::MAX_TOTAL_RUNNER_MINUTES={max_total_runner_minutes}"
        f"::MAX_ESTIMATED_COST_USD={max_estimated_cost_usd}"
        f"::PRIOR_RUNNER_MINUTES={prior_runner_minutes}"
    )


def validate_dispatch_attempt(mode: str, run_attempt: int) -> int:
    if mode not in {"dry-run", "execute"}:
        raise ValueError("mode must be dry-run or execute")
    run_attempt = require_int(run_attempt, "run_attempt", 1, 2**31 - 1)
    if mode == "execute" and run_attempt != 1:
        raise ValueError(
            "execute mode cannot use GitHub Re-run jobs; resume with a fresh "
            "workflow_dispatch and explicit prior_runner_minutes"
        )
    return run_attempt


def _adapter_contract() -> dict[str, Any]:
    """Exact inter-agent CLI boundary; command names are part of the contract."""

    return {
        "addresses": {
            "inventory": "scripts/inventory_address_rowgroups.py",
            "map": "scripts/global_v2_address_map.py",
            "plan": "scripts/global_v2_address_plan.py",
            "reduce": "scripts/global_v2_address_reduce.py",
            "stable_outputs": [
                "families/addresses/partition-plan.json",
                "families/addresses/address-collection.json",
                "families/addresses/shards/*.aidx",
                "families/addresses/shards/*.adat",
            ],
        },
        "places": {
            "inventory": "scripts/global_v2_places_inventory.py",
            "map": "scripts/global_v2_places_map.py",
            "plan": "scripts/global_v2_places_plan.py",
            "reduce": "scripts/global_v2_places_reduce.py",
            "head": "scripts/global_v2_places_head.py",
            "stable_outputs": [
                "families/places/catalog.pcat",
                "families/places/head.phrp",
                "families/places/q-*.pcsh",
            ],
        },
        "preview": {
            "catalog_key_template": "smoketest-v2/<run-id>/catalog.json",
            "release_key_template": "smoketest-v2/<run-id>/release.json",
            "worker_environment": "preview",
            "shared_v2_namespace_writes": False,
        },
    }


def build_contract(
    request: dict[str, Any],
    request_sha256: str,
    *,
    prepared_at: str,
    max_parallel: int,
    max_total_runner_minutes: int,
    max_estimated_cost_usd: int,
    runner_image_os: str,
    runner_image_version: str,
    prior_runner_minutes: int = 0,
) -> dict[str, Any]:
    request = build_requests.validate_request(request)
    request_sha256 = require_sha256(request_sha256, "request_sha256")
    if not isinstance(prepared_at, str) or not prepared_at.strip():
        raise ValueError("prepared_at must be a non-empty retained-run timestamp")
    max_parallel = require_int(max_parallel, "max_parallel", 1, 8)
    max_total_runner_minutes = require_int(
        max_total_runner_minutes, "max_total_runner_minutes", 1, 100_000
    )
    max_estimated_cost_usd = require_int(
        max_estimated_cost_usd, "max_estimated_cost_usd", 1, 10_000
    )
    prior_runner_minutes = require_int(
        prior_runner_minutes, "prior_runner_minutes", 0, 100_000
    )
    if not isinstance(runner_image_os, str) or not runner_image_os.strip():
        raise ValueError("runner_image_os must be a non-empty ImageOS value")
    if not isinstance(runner_image_version, str) or not runner_image_version.strip():
        raise ValueError("runner_image_version must be a non-empty ImageVersion value")
    root = f"staging/global-v2/{request_sha256}"
    slice_version = request["slice_version"]
    legacy_version = request["legacy_core"]["version"]
    phases = []
    for phase in PHASES:
        phases.append(
            {
                "name": phase,
                "depends_on": PHASE_DEPENDENCIES[phase],
                "timeout_minutes": PHASE_JOB_TIMEOUTS[phase],
                "estimated_job_minutes": PHASE_ESTIMATED_JOB_MINUTES[phase],
                "completion_key": f"{root}/completed/phases/{phase}.json",
                "completion_marker_written_last": True,
                "resumable_from_r2": True,
            }
        )
    return {
        "schema": CONTRACT_SCHEMA,
        "request": {
            "sha256": request_sha256,
            "producer_commit": request["producer_commit"],
            "overture_release": request["overture_release"],
            "geocoder_build": request["geocoder_build"],
            "slice_version": slice_version,
            "prepared_at": prepared_at,
        },
        "scope": {
            "kind": "families-only",
            "families": ["addresses", "places"],
            "reused_legacy_core": {
                "version": legacy_version,
                "manifest_key": request["legacy_core"]["manifest_key"],
                "manifest_sha256": request["legacy_core"]["manifest_sha256"],
            },
            "rebuild_legacy_core": False,
            "rebuild_divisions": False,
            "promote_catalog": False,
        },
        "namespace": {
            "execution_root": root,
            "immutable_root": f"{root}/immutable",
            "task_completion_root": f"{root}/completed/tasks",
            "phase_completion_root": f"{root}/completed/phases",
            "slice_root": f"{slice_version}/",
            "slice_manifest_key": f"{slice_version}/slice-manifest.json",
            "forbidden_exact_keys": ["catalog.json", "v2/catalog.json"],
            "forbidden_prefixes": [f"{legacy_version}/"],
            "create_only": True,
        },
        "runtime": {
            "image": {
                "label": EXECUTOR_IMAGE,
                "os": runner_image_os,
                "version": runner_image_version,
            },
            "python": PYTHON_VERSION,
            "numpy": NUMPY_VERSION,
            "pyarrow": PYARROW_VERSION,
            "parquet_cpp": PARQUET_CPP_VERSION,
            "parquet_format": PARQUET_FORMAT_VERSION,
            "requirements": ".github/requirements-hosted-rowgroup.txt",
            "requirements_hashes_required": True,
            "r2_control_client": "host-runner-preinstalled-aws-cli-recorded-at-runtime",
            "fingerprint_key": f"{root}/immutable/runtime.json",
        },
        "limits": {
            "source_tasks_per_family": request["execution"]["source_task_limit"],
            "reduce_jobs_per_family": request["execution"]["reduce_job_limit"],
            "max_parallel": max_parallel,
            "max_total_runner_minutes": max_total_runner_minutes,
            "max_estimated_cost_usd": max_estimated_cost_usd,
            "prior_retained_runner_minutes": prior_runner_minutes,
            "conservative_runner_usd_per_minute": (
                CONSERVATIVE_RUNNER_USD_PER_MINUTE
            ),
            "matrix_job_limit": 256,
            "hosted_job_limit_minutes": 360,
            "stop_on_cost_or_coverage_gate": True,
        },
        "phases": phases,
        "adapters": _adapter_contract(),
    }


def validate_contract(value: Any, request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != CONTRACT_SCHEMA:
        raise ValueError(f"execution contract schema must be {CONTRACT_SCHEMA}")
    try:
        rebuilt = build_contract(
            request,
            value["request"]["sha256"],
            prepared_at=value["request"]["prepared_at"],
            max_parallel=value["limits"]["max_parallel"],
            max_total_runner_minutes=value["limits"]["max_total_runner_minutes"],
            max_estimated_cost_usd=value["limits"]["max_estimated_cost_usd"],
            runner_image_os=value["runtime"]["image"]["os"],
            runner_image_version=value["runtime"]["image"]["version"],
            prior_runner_minutes=value["limits"]["prior_retained_runner_minutes"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("execution contract contents are invalid") from exc
    if rebuilt != value:
        raise ValueError("execution contract differs from its deterministic contents")
    return value


def verify_git_lineage(
    repository: Path,
    *,
    producer_commit: str,
    main_commit: str,
    require_exact_checkout: bool,
) -> dict[str, str]:
    if not COMMIT_RE.fullmatch(producer_commit) or not COMMIT_RE.fullmatch(main_commit):
        raise ValueError("producer/main commit must be a full lowercase Git SHA")

    def git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository,
            text=True,
            capture_output=True,
            check=check,
        )

    head = git("rev-parse", "HEAD").stdout.strip()
    resolved_main = git("rev-parse", main_commit).stdout.strip()
    resolved_producer = git("rev-parse", producer_commit).stdout.strip()
    if resolved_main != main_commit or resolved_producer != producer_commit:
        raise ValueError("producer/main commit does not resolve exactly")
    if git("merge-base", "--is-ancestor", producer_commit, main_commit, check=False).returncode:
        raise ValueError("request producer commit is not merged into dispatched main")
    if require_exact_checkout and head != producer_commit:
        raise ValueError("executor checkout is not the exact request producer commit")
    if not require_exact_checkout and head != main_commit:
        raise ValueError("preflight checkout is not the dispatched main commit")
    return {"head": head, "main": main_commit, "producer": producer_commit}


def build_runtime_fingerprint(
    *,
    runner_image_os: str,
    runner_image_version: str,
    python_version: str,
    numpy_version: str,
    pyarrow_version: str,
    parquet_cpp_version: str,
) -> dict[str, Any]:
    if not isinstance(runner_image_os, str) or not runner_image_os.strip():
        raise ValueError("runtime ImageOS is required")
    if not isinstance(runner_image_version, str) or not runner_image_version.strip():
        raise ValueError("runtime ImageVersion is required")
    expected = {
        "image": {
            "label": EXECUTOR_IMAGE,
            "os": runner_image_os,
            "version": runner_image_version,
        },
        "python": PYTHON_VERSION,
        "numpy": NUMPY_VERSION,
        "pyarrow": PYARROW_VERSION,
        "parquet_cpp": PARQUET_CPP_VERSION,
        "parquet_format": PARQUET_FORMAT_VERSION,
        "requirements": ".github/requirements-hosted-rowgroup.txt",
        "requirements_hashes_required": True,
    }
    actual = {
        "image": {
            "label": EXECUTOR_IMAGE,
            "os": runner_image_os,
            "version": runner_image_version,
        },
        "python": python_version,
        "numpy": numpy_version,
        "pyarrow": pyarrow_version,
        "parquet_cpp": parquet_cpp_version,
        "parquet_format": PARQUET_FORMAT_VERSION,
        "requirements": ".github/requirements-hosted-rowgroup.txt",
        "requirements_hashes_required": True,
    }
    if actual != expected:
        raise ValueError(f"executor runtime differs: expected={expected}, actual={actual}")
    fingerprint = {"schema": RUNTIME_SCHEMA, "toolchain": actual}
    return {
        **fingerprint,
        "fingerprint_sha256": sha256_bytes(canonical_json(fingerprint)),
    }


def runtime_from_environment() -> dict[str, Any]:
    import numpy
    import pyarrow

    return build_runtime_fingerprint(
        runner_image_os=os.environ.get("ImageOS", ""),
        runner_image_version=os.environ.get("ImageVersion", ""),
        python_version=".".join(map(str, sys.version_info[:3])),
        numpy_version=numpy.__version__,
        pyarrow_version=pyarrow.__version__,
        parquet_cpp_version=pyarrow.cpp_version,
    )


def validate_runtime(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != RUNTIME_SCHEMA:
        raise ValueError(f"runtime schema must be {RUNTIME_SCHEMA}")
    toolchain = value.get("toolchain")
    if not isinstance(toolchain, dict):
        raise ValueError("runtime toolchain must be an object")
    image = toolchain.get("image")
    if not isinstance(image, dict):
        raise ValueError("runtime hosted image must be an object")
    rebuilt = build_runtime_fingerprint(
        runner_image_os=image.get("os"),
        runner_image_version=image.get("version"),
        python_version=toolchain.get("python"),
        numpy_version=toolchain.get("numpy"),
        pyarrow_version=toolchain.get("pyarrow"),
        parquet_cpp_version=toolchain.get("parquet_cpp"),
    )
    if rebuilt != value:
        raise ValueError("runtime fingerprint differs from its deterministic contents")
    return value


def validate_runtime_for_contract(
    value: Any, contract: dict[str, Any]
) -> dict[str, Any]:
    runtime = validate_runtime(value)
    expected = contract["runtime"]
    toolchain = runtime["toolchain"]
    expected_image = expected["image"]
    actual_image = toolchain["image"]
    if (
        actual_image["label"] != expected_image["label"]
        or actual_image["os"] != expected_image["os"]
    ):
        raise ValueError("runtime image OS differs from the retained contract")
    # ImageVersion remains in the signed runtime fingerprint as per-job
    # provenance, while pinned Python/NumPy/PyArrow/Parquet are deterministic.
    for field in ("python", "numpy", "pyarrow", "parquet_cpp", "parquet_format"):
        if toolchain[field] != expected[field]:
            raise ValueError(f"runtime {field} differs from the retained contract")
    return runtime


def artifact_identity(path: Path, object_key: str) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"artifact is missing or empty: {path}")
    return {
        "object_key": object_key,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def normalize_artifact(value: Any, contract: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("artifact identity must be an object")
    require_exact_fields(value, {"object_key", "bytes", "sha256"}, "artifact")
    key = value["object_key"]
    if not isinstance(key, str) or key.startswith("/") or "//" in key:
        raise ValueError("artifact object_key is not canonical")
    parts = key.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("artifact object_key escapes its namespace")
    namespace = contract["namespace"]
    if key in namespace["forbidden_exact_keys"] or any(
        key.startswith(prefix) for prefix in namespace["forbidden_prefixes"]
    ):
        raise ValueError(f"artifact targets a forbidden key: {key}")
    allowed = (
        key.startswith(namespace["immutable_root"] + "/")
        or key.startswith(namespace["task_completion_root"] + "/")
        or key.startswith(namespace["phase_completion_root"] + "/")
        or key.startswith(namespace["slice_root"] + "families/")
        or key == namespace["slice_root"] + "slice-manifest.json"
        or key.startswith("smoketest-v2/")
    )
    if not allowed:
        raise ValueError(f"artifact is outside executor namespaces: {key}")
    size = require_int(value["bytes"], f"artifact bytes for {key}", 1, 2**63 - 1)
    digest = require_sha256(value["sha256"], f"artifact sha256 for {key}")
    return {"object_key": key, "bytes": size, "sha256": digest}


def normalize_counters(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(COUNTER_FIELDS):
        raise ValueError(f"task counters must name exactly {list(COUNTER_FIELDS)}")
    normalized = {
        field: require_int(value[field], field, 0, 2**63 - 1)
        for field in COUNTER_FIELDS
    }
    if normalized["input_records"] != (
        normalized["retained_records"] + normalized["rejected_records"]
    ):
        raise ValueError("task retained/rejected accounting does not reconcile")
    if normalized["output_records"] > normalized["retained_records"]:
        raise ValueError("task output records exceed retained records")
    return normalized


def build_task_completion(
    contract: dict[str, Any],
    runtime: dict[str, Any],
    *,
    phase: str,
    family: str,
    task_id: str,
    index: int,
    producer_report: dict[str, Any],
    artifacts: Iterable[dict[str, Any]],
    counters: dict[str, int],
) -> dict[str, Any]:
    if phase not in {"map", "reduce", "head"}:
        raise ValueError("task completion phase must be map, reduce, or head")
    if family not in {"addresses", "places"}:
        raise ValueError("task completion family is invalid")
    task_id = require_safe_id(task_id, "task_id")
    index = require_int(index, "task index", 0, 255)
    runtime = validate_runtime_for_contract(runtime, contract)
    if not isinstance(producer_report, dict):
        raise ValueError("producer report identity must be an object")
    report = normalize_artifact(producer_report, contract)
    outputs = sorted(
        (normalize_artifact(item, contract) for item in artifacts),
        key=lambda item: item["object_key"],
    )
    if not outputs:
        raise ValueError("task completion requires at least one output artifact")
    keys = [item["object_key"] for item in outputs]
    if len(keys) != len(set(keys)) or report["object_key"] in set(keys):
        raise ValueError("task completion artifact keys are duplicated")
    counters = normalize_counters(counters)
    marker_without_digest = {
        "schema": TASK_COMPLETION_SCHEMA,
        "request_sha256": contract["request"]["sha256"],
        "runtime": runtime,
        "runtime_fingerprint_sha256": runtime["fingerprint_sha256"],
        "phase": phase,
        "family": family,
        "task_id": task_id,
        "index": index,
        "status": "complete",
        "producer_report": report,
        "artifacts": outputs,
        "counters": counters,
        "totals": {
            "artifacts": len(outputs),
            "bytes": sum(item["bytes"] for item in outputs),
        },
    }
    return {
        **marker_without_digest,
        "completion_sha256": sha256_bytes(canonical_json(marker_without_digest)),
    }


def validate_task_completion(
    value: Any, contract: dict[str, Any], runtime: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != TASK_COMPLETION_SCHEMA:
        raise ValueError(f"task completion schema must be {TASK_COMPLETION_SCHEMA}")
    validate_runtime_for_contract(runtime, contract)
    task_runtime = validate_runtime_for_contract(value.get("runtime"), contract)
    rebuilt = build_task_completion(
        contract,
        task_runtime,
        phase=value.get("phase"),
        family=value.get("family"),
        task_id=value.get("task_id"),
        index=value.get("index"),
        producer_report=value.get("producer_report"),
        artifacts=value.get("artifacts", []),
        counters=value.get("counters"),
    )
    if rebuilt != value:
        raise ValueError("task completion differs from its deterministic contents")
    return value


def normalize_expected_tasks(value: Any, phase: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("expected task set must be a non-empty array")
    normalized = []
    seen: set[tuple[str, str, int]] = set()
    family_indices: dict[str, set[int]] = {"addresses": set(), "places": set()}
    family_task_ids: dict[str, set[str]] = {"addresses": set(), "places": set()}
    family_counts = {"addresses": 0, "places": 0}
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("expected task must be an object")
        require_exact_fields(item, {"family", "task_id", "index"}, "expected task")
        family = item["family"]
        if family not in family_counts:
            raise ValueError("expected task family is invalid")
        task = {
            "family": family,
            "task_id": require_safe_id(item["task_id"], "expected task_id"),
            "index": require_int(item["index"], "expected task index", 0, 255),
        }
        identity = (task["family"], task["task_id"], task["index"])
        if (
            identity in seen
            or task["index"] in family_indices[family]
            or task["task_id"] in family_task_ids[family]
        ):
            raise ValueError("expected task set contains a duplicate")
        seen.add(identity)
        family_indices[family].add(task["index"])
        family_task_ids[family].add(task["task_id"])
        family_counts[family] += 1
        normalized.append(task)
    hard_limit = 128 if phase == "map" else 256
    if any(count > hard_limit for count in family_counts.values()):
        raise ValueError(f"{phase} task coverage exceeds its {hard_limit}-job limit")
    for family, indices in family_indices.items():
        if indices and indices != set(range(len(indices))):
            raise ValueError(
                f"{phase} {family} task indices must exactly cover 0..N-1"
            )
    return sorted(normalized, key=lambda item: (item["family"], item["index"], item["task_id"]))


def build_phase_completion(
    contract: dict[str, Any],
    runtime: dict[str, Any],
    *,
    phase: str,
    expected_tasks: list[dict[str, Any]],
    task_completions: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    if phase not in {"map", "reduce", "head"}:
        raise ValueError("matrix phase completion must be map, reduce, or head")
    runtime = validate_runtime_for_contract(runtime, contract)
    expected = normalize_expected_tasks(expected_tasks, phase)
    completions = [
        validate_task_completion(value, contract, runtime)
        for value in task_completions
    ]
    actual = sorted(
        (
            {
                "family": item["family"],
                "task_id": item["task_id"],
                "index": item["index"],
            }
            for item in completions
        ),
        key=lambda item: (item["family"], item["index"], item["task_id"]),
    )
    if len(actual) != len(completions) or actual != expected:
        raise ValueError("task completion set does not exactly cover the phase plan")
    all_artifacts = [
        artifact
        for completion in completions
        for artifact in [completion["producer_report"], *completion["artifacts"]]
    ]
    identities: dict[str, tuple[int, str]] = {}
    for artifact in all_artifacts:
        identity = (artifact["bytes"], artifact["sha256"])
        if artifact["object_key"] in identities:
            raise ValueError("phase task artifacts contain a duplicate object key")
        identities[artifact["object_key"]] = identity
    totals = {
        field: sum(item["counters"][field] for item in completions)
        for field in COUNTER_FIELDS
    }
    marker_without_digest = {
        "schema": PHASE_COMPLETION_SCHEMA,
        "request_sha256": contract["request"]["sha256"],
        "runtime": runtime,
        "runtime_fingerprint_sha256": runtime["fingerprint_sha256"],
        "phase": phase,
        "status": "complete",
        "expected_tasks": expected,
        "task_completion_sha256": [
            item["completion_sha256"]
            for item in sorted(
                completions, key=lambda item: (item["family"], item["index"], item["task_id"])
            )
        ],
        "counters": totals,
        "artifact_totals": {
            "objects": len(identities),
            "bytes": sum(size for size, _ in identities.values()),
        },
    }
    return {
        **marker_without_digest,
        "completion_sha256": sha256_bytes(canonical_json(marker_without_digest)),
    }


def build_control_phase_completion(
    contract: dict[str, Any],
    runtime: dict[str, Any],
    *,
    phase: str,
    artifacts: Iterable[dict[str, Any]],
    dependency_evidence: Iterable[dict[str, Any]],
    details: dict[str, Any],
) -> dict[str, Any]:
    if phase not in {
        "inventory",
        "aggregate-plan",
        "remote-verify",
        "finalize-slice",
        "worker-smoke",
    }:
        raise ValueError("control phase is invalid")
    runtime = validate_runtime_for_contract(runtime, contract)
    normalized_artifacts = sorted(
        (normalize_artifact(item, contract) for item in artifacts),
        key=lambda item: item["object_key"],
    )
    keys = [item["object_key"] for item in normalized_artifacts]
    if not normalized_artifacts or len(keys) != len(set(keys)):
        raise ValueError("control phase requires unique output artifacts")
    if not isinstance(details, dict):
        raise ValueError("control phase details must be an object")
    # Prove details are canonically JSON-serializable before placing them in a
    # completion marker that may be resumed across workflow runs.
    canonical_json(details)
    evidence = []
    seen: set[str] = set()
    for item in dependency_evidence:
        if not isinstance(item, dict):
            raise ValueError("dependency evidence must be an object")
        require_exact_fields(item, {"phase", "completion_sha256"}, "dependency evidence")
        dependency = item["phase"]
        if dependency not in PHASE_DEPENDENCIES[phase] or dependency in seen:
            raise ValueError("dependency evidence differs from the phase graph")
        seen.add(dependency)
        evidence.append(
            {
                "phase": dependency,
                "completion_sha256": require_sha256(
                    item["completion_sha256"], f"{dependency} completion_sha256"
                ),
            }
        )
    if seen != set(PHASE_DEPENDENCIES[phase]):
        raise ValueError("dependency evidence does not exactly cover phase prerequisites")
    marker_without_digest = {
        "schema": PHASE_COMPLETION_SCHEMA,
        "request_sha256": contract["request"]["sha256"],
        "runtime": runtime,
        "runtime_fingerprint_sha256": runtime["fingerprint_sha256"],
        "phase": phase,
        "status": "complete",
        "dependencies": sorted(evidence, key=lambda item: PHASES.index(item["phase"])),
        "artifacts": normalized_artifacts,
        "artifact_totals": {
            "objects": len(normalized_artifacts),
            "bytes": sum(item["bytes"] for item in normalized_artifacts),
        },
        "details": details,
    }
    return {
        **marker_without_digest,
        "completion_sha256": sha256_bytes(canonical_json(marker_without_digest)),
    }


def validate_address_inventory(value: Any, request: dict[str, Any]) -> dict[str, Any]:
    identity = address_inventory.validate_canonical_inventory(value)
    expected = request["families"]["addresses"]["source"]
    if identity["inventory_sha256"] != expected["inventory_sha256"]:
        raise ValueError("address inventory digest differs from retained request")
    if identity["schema_fingerprint_sha256"] != expected["schema_fingerprint_sha256"]:
        raise ValueError("address schema fingerprint differs from retained request")
    if value["release"] != request["overture_release"]:
        raise ValueError("address inventory release differs from retained request")
    return value


def validate_places_inventory(value: Any, request: dict[str, Any]) -> dict[str, Any]:
    value = places_inventory.validate_inventory(value)
    expected = request["families"]["places"]["source"]
    if value["inventory_sha256"] != expected["inventory_sha256"]:
        raise ValueError("Places inventory digest differs from retained request")
    if value["schema_contract"]["fingerprint_sha256"] != expected[
        "schema_fingerprint_sha256"
    ]:
        raise ValueError("Places schema fingerprint differs from retained request")
    if value["release"] != request["overture_release"]:
        raise ValueError("Places inventory release differs from retained request")
    return value


def validate_legacy_core(path: Path, request: dict[str, Any]) -> dict[str, Any]:
    expected = request["legacy_core"]
    actual_sha256 = sha256_file(path)
    if actual_sha256 != expected["manifest_sha256"]:
        raise ValueError("legacy core manifest SHA-256 differs from retained request")
    inspected = inspect_legacy_core(
        path,
        expected_version=expected["version"],
        expected_overture_release=request["overture_release"],
    )
    return {
        "version": inspected["version"],
        "overture_release": inspected["overture_release"],
        "manifest_key": expected["manifest_key"],
        "manifest_sha256": actual_sha256,
        "forward_shards": inspected["forward_shards"],
        "reverse_shards": inspected["reverse_shards"],
        "id_shards": inspected["id_shards"],
        "reused_not_rebuilt": True,
    }


def inspect_legacy_core(
    path: Path, *, expected_version: str, expected_overture_release: str
) -> dict[str, Any]:
    """Validate and identify an unpromoted core without trusting a request yet.

    This is the credentialed read-only bootstrap boundary: its canonical output
    supplies the manifest digest that an operator later freezes in the retained
    global-v2 request. It never has an object-store client and cannot publish.
    """

    if not v2_release_manifest.BUILD_RE.fullmatch(expected_version):
        raise ValueError("expected legacy core version must use YYYY-MM-DD.N")
    manifest_value = read_json(path)
    manifest = v2_release_manifest._validate_legacy_release(  # noqa: SLF001
        manifest_value, expected_overture_release
    )
    if manifest["version"] != expected_version:
        raise ValueError("legacy core version differs from read-only preflight input")
    families = manifest_value["families"]
    return {
        "schema": "overture-global-v2-legacy-core-preflight-v1",
        "version": expected_version,
        "overture_release": expected_overture_release,
        "manifest_key": f"{expected_version}/release-manifest.json",
        "manifest_sha256": sha256_file(path),
        "manifest_bytes": path.stat().st_size,
        "forward_shards": families["forward"]["shard_count"],
        "reverse_shards": families["reverse"]["shard_count"],
        "id_shards": families["id"]["shard_count"],
        "read_only": True,
    }


def phase_budget(
    contract: dict[str, Any], *, phase: str, jobs: int, consumed_runner_minutes: int
) -> dict[str, Any]:
    if phase not in PHASES:
        raise ValueError("unknown executor phase")
    jobs = require_int(jobs, "phase jobs", 1, 512)
    consumed = require_int(
        consumed_runner_minutes, "consumed runner minutes", 0, 100_000
    )
    if phase in {"map", "reduce"} and jobs > 256:
        raise ValueError("GitHub matrix job count cannot exceed 256")
    estimate = jobs * PHASE_ESTIMATED_JOB_MINUTES[phase]
    projected = consumed + estimate
    rate = float(CONSERVATIVE_RUNNER_USD_PER_MINUTE)
    projected_cost = round(projected * rate, 2)
    limits = contract["limits"]
    allowed = (
        projected <= limits["max_total_runner_minutes"]
        and projected_cost <= limits["max_estimated_cost_usd"]
    )
    result = {
        "phase": phase,
        "jobs": jobs,
        "consumed_runner_minutes": consumed,
        "estimated_phase_runner_minutes": estimate,
        "projected_total_runner_minutes": projected,
        "projected_cost_usd": projected_cost,
        "max_total_runner_minutes": limits["max_total_runner_minutes"],
        "max_estimated_cost_usd": limits["max_estimated_cost_usd"],
        "allowed": allowed,
    }
    if not allowed:
        raise ValueError(f"phase exceeds retained cost/runtime gate: {result}")
    return result


def execution_budget(
    contract: dict[str, Any], *, map_jobs: int, reduce_jobs: int,
    prior_runner_minutes: int | None = None,
) -> dict[str, Any]:
    """Gate the complete retained estimate before dynamic job expansion."""
    map_jobs = require_int(map_jobs, "total map jobs", 1, 256)
    reduce_jobs = require_int(reduce_jobs, "total reduce jobs", 1, 256)
    jobs_by_phase = {
        "inventory": 1,
        "map": map_jobs,
        "aggregate-plan": 1,
        "reduce": reduce_jobs,
        "head": 1,
        "remote-verify": 1,
        "finalize-slice": 1,
        "worker-smoke": 1,
    }
    retained_prior = contract["limits"]["prior_retained_runner_minutes"]
    if prior_runner_minutes is None:
        prior_runner_minutes = retained_prior
    prior_runner_minutes = require_int(
        prior_runner_minutes, "prior retained runner minutes", 0, 100_000
    )
    if prior_runner_minutes != retained_prior:
        raise ValueError("prior runner minutes differ from the retained contract")
    consumed = prior_runner_minutes
    phases = []
    for phase in PHASES:
        result = phase_budget(
            contract,
            phase=phase,
            jobs=jobs_by_phase[phase],
            consumed_runner_minutes=consumed,
        )
        phases.append(result)
        consumed = result["projected_total_runner_minutes"]
    return {
        "schema": "overture-global-v2-execution-budget-v1",
        "map_jobs": map_jobs,
        "reduce_jobs": reduce_jobs,
        "prior_runner_minutes": prior_runner_minutes,
        "max_parallel": contract["limits"]["max_parallel"],
        "estimated_total_runner_minutes": consumed,
        "estimated_total_cost_usd": round(
            consumed * float(CONSERVATIVE_RUNNER_USD_PER_MINUTE), 2
        ),
        "conservative_runner_usd_per_minute": CONSERVATIVE_RUNNER_USD_PER_MINUTE,
        "phases": phases,
        "allowed": True,
    }


def normalize_remote_listing(value: Any, slice_version: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != REMOTE_LISTING_SCHEMA:
        raise ValueError(f"remote listing schema must be {REMOTE_LISTING_SCHEMA}")
    if value.get("slice_version") != slice_version:
        raise ValueError("remote listing belongs to another slice")
    objects = value.get("objects")
    if not isinstance(objects, list):
        raise ValueError("remote listing objects must be an array")
    normalized = []
    seen: set[str] = set()
    prefix = f"{slice_version}/"
    for item in objects:
        if not isinstance(item, dict):
            raise ValueError("remote listing object is invalid")
        require_exact_fields(item, {"object_key", "bytes", "sha256"}, "remote object")
        key = item["object_key"]
        if not isinstance(key, str) or not key.startswith(prefix) or key in seen:
            raise ValueError("remote object key is outside or duplicated in the slice")
        seen.add(key)
        normalized.append(
            {
                "object_key": key,
                "bytes": require_int(item["bytes"], f"remote bytes for {key}", 1, 2**63 - 1),
                "sha256": require_sha256(item["sha256"], f"remote sha256 for {key}"),
            }
        )
    result = {
        "schema": REMOTE_LISTING_SCHEMA,
        "slice_version": slice_version,
        "objects": sorted(normalized, key=lambda item: item["object_key"]),
    }
    if result != value:
        raise ValueError("remote listing is not canonically ordered")
    return result


def _required_artifacts_present(request: dict[str, Any], family: str, keys: set[str]) -> None:
    patterns = request["families"][family]["required_artifacts"]
    for pattern in patterns:
        matches = [key for key in keys if fnmatch.fnmatchcase(key, pattern)]
        if not matches:
            raise ValueError(f"{family} required artifact pattern has no match: {pattern}")


def build_slice_manifest(
    contract: dict[str, Any],
    request: dict[str, Any],
    runtime: dict[str, Any],
    *,
    address_manifest: dict[str, Any],
    places_manifest: dict[str, Any],
    remote_listing: dict[str, Any],
    phase_evidence: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    request = build_requests.validate_request(request)
    contract = validate_contract(contract, request)
    runtime = validate_runtime_for_contract(runtime, contract)
    slice_version = request["slice_version"]
    listing = normalize_remote_listing(remote_listing, slice_version)
    if any(item["object_key"].endswith("/slice-manifest.json") for item in listing["objects"]):
        raise ValueError("slice completion marker already exists during candidate verification")
    remote = {
        item["object_key"]: (item["bytes"], item["sha256"])
        for item in listing["objects"]
    }
    summaries: dict[str, Any] = {}
    expected_remote: dict[str, tuple[int, str]] = {}
    for family, raw_manifest in (
        ("addresses", address_manifest),
        ("places", places_manifest),
    ):
        manifest = family_manifests.validate_family_manifest(raw_manifest)
        if manifest["family"] != family:
            raise ValueError(f"{family} manifest declares another family")
        if manifest["lineage"]["overture_release"] != request["overture_release"]:
            raise ValueError(f"{family} manifest Overture release differs")
        if manifest["lineage"]["producer_commit"] != request["producer_commit"]:
            raise ValueError(f"{family} manifest producer commit differs")
        if manifest["region"]["bbox"] != request["scope"]["coverage"]:
            raise ValueError(f"{family} manifest coverage is not global")
        artifact_keys = {item["object_key"] for item in manifest["artifacts"]}
        _required_artifacts_present(request, family, artifact_keys)
        for artifact in manifest["artifacts"]:
            full_key = f"{slice_version}/{artifact['object_key']}"
            expected_remote[full_key] = (artifact["bytes"], artifact["sha256"])
        manifest_key = f"{slice_version}/families/{family}/family-manifest.json"
        manifest_bytes = family_manifests.canonical_json(manifest)
        expected_remote[manifest_key] = (len(manifest_bytes), sha256_bytes(manifest_bytes))
        summaries[family] = {
            "manifest": f"./families/{family}/family-manifest.json",
            "manifest_digest": manifest["manifest_digest"],
            "region": manifest["region"],
            "artifact_count": len(manifest["artifacts"]),
            "total_bytes": sum(item["bytes"] for item in manifest["artifacts"]),
            "promotion_eligible": False,
            "objects": [
                {
                    "href": f"./{artifact['object_key']}",
                    "size_bytes": artifact["bytes"],
                    "sha256": artifact["sha256"],
                }
                for artifact in manifest["artifacts"]
            ],
        }
    if remote != expected_remote:
        missing = sorted(set(expected_remote) - set(remote))[:20]
        extra = sorted(set(remote) - set(expected_remote))[:20]
        mismatched = sorted(
            key for key in set(remote) & set(expected_remote) if remote[key] != expected_remote[key]
        )[:20]
        raise ValueError(
            "remote family slice differs from exact manifests: "
            f"missing={missing}, extra={extra}, mismatched={mismatched}"
        )
    evidence = []
    seen_phases: set[str] = set()
    for item in phase_evidence:
        if not isinstance(item, dict):
            raise ValueError("phase evidence must be an object")
        require_exact_fields(item, {"phase", "completion_sha256"}, "phase evidence")
        phase = item["phase"]
        if phase not in {"inventory", "map", "aggregate-plan", "reduce", "head"}:
            raise ValueError("slice phase evidence contains an unsupported phase")
        if phase in seen_phases:
            raise ValueError("slice phase evidence contains a duplicate")
        seen_phases.add(phase)
        evidence.append(
            {
                "phase": phase,
                "completion_sha256": require_sha256(
                    item["completion_sha256"], f"{phase} completion_sha256"
                ),
            }
        )
    required_evidence = {"inventory", "map", "aggregate-plan", "reduce", "head"}
    if seen_phases != required_evidence:
        raise ValueError("slice phase evidence does not cover every producer phase")
    verified_objects = [
        {
            "href": f"./{key.removeprefix(slice_version + '/')}",
            "size_bytes": identity[0],
            "sha256": identity[1],
        }
        for key, identity in sorted(expected_remote.items())
    ]
    return {
        "schema_version": SLICE_MANIFEST_SCHEMA_VERSION,
        "slice_version": slice_version,
        "overture_release": request["overture_release"],
        "generated_at": contract["request"]["prepared_at"],
        "is_slice": True,
        "promotion_eligible": False,
        "catalog_published": False,
        "legacy_core_rebuilt": False,
        "divisions_rebuilt": False,
        "request_sha256": contract["request"]["sha256"],
        "runtime": runtime,
        "runtime_fingerprint_sha256": runtime["fingerprint_sha256"],
        "phase_evidence": sorted(evidence, key=lambda item: PHASES.index(item["phase"])),
        "families": summaries,
        "verified_version_objects": verified_objects,
    }


def build_stop_evidence(
    contract: dict[str, Any],
    *,
    phase: str,
    run_id: str,
    run_attempt: str,
    result: str,
    completed: bool,
) -> dict[str, Any]:
    if phase not in {"preflight", *PHASES}:
        raise ValueError("stop evidence phase is invalid")
    if result not in {"success", "failure", "cancelled", "skipped"}:
        raise ValueError("stop evidence result is invalid")
    if type(completed) is not bool or completed != (result == "success"):
        raise ValueError("stop evidence completion/result does not reconcile")
    return {
        "schema": STOP_EVIDENCE_SCHEMA,
        "request_sha256": contract["request"]["sha256"],
        "slice_version": contract["request"]["slice_version"],
        "phase": phase,
        "run_id": require_safe_id(run_id, "run_id"),
        "run_attempt": require_safe_id(run_attempt, "run_attempt"),
        "result": result,
        "completed": completed,
        "slice_prefix_retained": True,
        "catalog_published": False,
        "legacy_core_rebuilt": False,
        "divisions_rebuilt": False,
    }


def _load_artifact_list(path: Path) -> list[dict[str, Any]]:
    value = read_json(path)
    if not isinstance(value, list):
        raise ValueError("artifact list must be a JSON array")
    return value


def _r2_store(bucket: str | None, endpoint_url: str | None):
    bucket = bucket or os.environ.get("R2_BUCKET") or "geocoder-shards"
    endpoint_url = endpoint_url or os.environ.get("R2_ENDPOINT")
    if endpoint_url is None:
        account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        if account:
            endpoint_url = f"https://{account}.r2.cloudflarestorage.com"
    if not endpoint_url:
        raise ValueError("R2 endpoint is required")
    for aws_name, r2_name in (
        ("AWS_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID"),
        ("AWS_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY"),
    ):
        if not os.environ.get(aws_name) and os.environ.get(r2_name):
            os.environ[aws_name] = os.environ[r2_name]
    return r2_verified_store.S3Store(bucket, endpoint_url)


def restore_known_key(
    store: r2_verified_store.ObjectStore,
    contract: dict[str, Any],
    *,
    object_key: str,
    destination: Path,
) -> dict[str, Any]:
    remote = store.head(object_key)
    if remote is None or remote.sha256 is None:
        raise ValueError(f"required immutable object/sha256 metadata is absent: {object_key}")
    identity = normalize_artifact(
        {
            "object_key": object_key,
            "bytes": remote.bytes,
            "sha256": remote.sha256,
        },
        contract,
    )
    status = r2_verified_store.verified_download(
        store,
        object_key,
        destination,
        expected_bytes=identity["bytes"],
        expected_sha256=identity["sha256"],
    )
    return {**identity, "status": status, "verified": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    preflight = commands.add_parser("preflight")
    preflight.add_argument("--request", type=Path, required=True)
    preflight.add_argument("--request-sha256", required=True)
    preflight.add_argument("--mode", choices=("dry-run", "execute"), required=True)
    preflight.add_argument("--confirmation", required=True)
    preflight.add_argument("--run-attempt", type=int, required=True)
    preflight.add_argument("--prepared-at", required=True)
    preflight.add_argument("--repository", type=Path, default=Path("."))
    preflight.add_argument("--main-commit", required=True)
    preflight.add_argument("--max-parallel", type=int, default=4)
    preflight.add_argument("--max-total-runner-minutes", type=int, default=50_000)
    preflight.add_argument("--max-estimated-cost-usd", type=int, default=1_000)
    preflight.add_argument("--runner-image-os", required=True)
    preflight.add_argument("--runner-image-version", required=True)
    preflight.add_argument("--prior-runner-minutes", type=int, required=True)
    preflight.add_argument("--output", type=Path, required=True)

    exact = commands.add_parser("assert-exact-checkout")
    exact.add_argument("--request", type=Path, required=True)
    exact.add_argument("--request-sha256", required=True)
    exact.add_argument("--repository", type=Path, default=Path("."))

    runtime = commands.add_parser("runtime")
    runtime.add_argument("--output", type=Path, required=True)

    validate_inventory_cli = commands.add_parser("validate-inventory")
    validate_inventory_cli.add_argument("--request", type=Path, required=True)
    validate_inventory_cli.add_argument("--request-sha256", required=True)
    validate_inventory_cli.add_argument("--family", choices=("addresses", "places"), required=True)
    validate_inventory_cli.add_argument("--inventory", type=Path, required=True)

    validate_core_cli = commands.add_parser("validate-legacy-core")
    validate_core_cli.add_argument("--request", type=Path, required=True)
    validate_core_cli.add_argument("--request-sha256", required=True)
    validate_core_cli.add_argument("--manifest", type=Path, required=True)
    validate_core_cli.add_argument("--output", type=Path, required=True)

    inspect_core_cli = commands.add_parser("inspect-legacy-core")
    inspect_core_cli.add_argument("--manifest", type=Path, required=True)
    inspect_core_cli.add_argument("--expected-version", required=True)
    inspect_core_cli.add_argument("--expected-overture-release", required=True)
    inspect_core_cli.add_argument("--output", type=Path, required=True)

    budget = commands.add_parser("budget")
    budget.add_argument("--contract", type=Path, required=True)
    budget.add_argument("--request", type=Path, required=True)
    budget.add_argument("--phase", choices=PHASES, required=True)
    budget.add_argument("--jobs", type=int, required=True)
    budget.add_argument("--consumed-runner-minutes", type=int, default=0)
    budget.add_argument("--output", type=Path, required=True)
    total_budget = commands.add_parser("execution-budget")
    total_budget.add_argument("--contract", type=Path, required=True)
    total_budget.add_argument("--request", type=Path, required=True)
    total_budget.add_argument("--map-jobs", type=int, required=True)
    total_budget.add_argument("--reduce-jobs", type=int, required=True)
    total_budget.add_argument("--prior-runner-minutes", type=int, default=0)
    total_budget.add_argument("--output", type=Path, required=True)

    task = commands.add_parser("task-completion")
    task.add_argument("--contract", type=Path, required=True)
    task.add_argument("--request", type=Path, required=True)
    task.add_argument("--runtime", type=Path, required=True)
    task.add_argument("--phase", choices=("map", "reduce", "head"), required=True)
    task.add_argument("--family", choices=("addresses", "places"), required=True)
    task.add_argument("--task-id", required=True)
    task.add_argument("--index", type=int, required=True)
    task.add_argument("--producer-report", type=Path, required=True)
    task.add_argument("--producer-report-key", required=True)
    task.add_argument("--artifacts", type=Path, required=True)
    task.add_argument("--counters", type=Path, required=True)
    task.add_argument("--output", type=Path, required=True)

    phase = commands.add_parser("phase-completion")
    phase.add_argument("--contract", type=Path, required=True)
    phase.add_argument("--request", type=Path, required=True)
    phase.add_argument("--runtime", type=Path, required=True)
    phase.add_argument("--phase", choices=("map", "reduce", "head"), required=True)
    phase.add_argument("--expected-tasks", type=Path, required=True)
    phase.add_argument("--task-completion", action="append", type=Path, default=[])
    phase.add_argument("--output", type=Path, required=True)

    control_phase = commands.add_parser("control-phase-completion")
    control_phase.add_argument("--contract", type=Path, required=True)
    control_phase.add_argument("--request", type=Path, required=True)
    control_phase.add_argument("--runtime", type=Path, required=True)
    control_phase.add_argument(
        "--phase",
        choices=(
            "inventory", "aggregate-plan", "remote-verify", "finalize-slice",
            "worker-smoke",
        ),
        required=True,
    )
    control_phase.add_argument("--artifacts", type=Path, required=True)
    control_phase.add_argument("--dependency-evidence", type=Path, required=True)
    control_phase.add_argument("--details", type=Path, required=True)
    control_phase.add_argument("--output", type=Path, required=True)

    slice_candidate = commands.add_parser("slice-candidate")
    slice_candidate.add_argument("--contract", type=Path, required=True)
    slice_candidate.add_argument("--request", type=Path, required=True)
    slice_candidate.add_argument("--runtime", type=Path, required=True)
    slice_candidate.add_argument("--addresses-manifest", type=Path, required=True)
    slice_candidate.add_argument("--places-manifest", type=Path, required=True)
    slice_candidate.add_argument("--remote-listing", type=Path, required=True)
    slice_candidate.add_argument("--phase-evidence", type=Path, required=True)
    slice_candidate.add_argument("--output", type=Path, required=True)

    stop = commands.add_parser("stop-evidence")
    stop.add_argument("--contract", type=Path, required=True)
    stop.add_argument("--request", type=Path, required=True)
    stop.add_argument("--phase", required=True)
    stop.add_argument("--run-id", required=True)
    stop.add_argument("--run-attempt", required=True)
    stop.add_argument("--result", required=True)
    stop.add_argument("--completed", action="store_true")
    stop.add_argument("--output", type=Path, required=True)

    publish_object = commands.add_parser("publish-immutable")
    publish_object.add_argument("--contract", type=Path, required=True)
    publish_object.add_argument("--request", type=Path, required=True)
    publish_object.add_argument("--source", type=Path, required=True)
    publish_object.add_argument("--object-key", required=True)
    publish_object.add_argument("--bucket")
    publish_object.add_argument("--endpoint-url")
    publish_object.add_argument("--report", type=Path, required=True)

    restore_object = commands.add_parser("restore-immutable")
    restore_object.add_argument("--contract", type=Path, required=True)
    restore_object.add_argument("--request", type=Path, required=True)
    restore_object.add_argument("--object-key", required=True)
    restore_object.add_argument("--bytes", type=int, required=True)
    restore_object.add_argument("--sha256", required=True)
    restore_object.add_argument("--destination", type=Path, required=True)
    restore_object.add_argument("--bucket")
    restore_object.add_argument("--endpoint-url")

    restore_known = commands.add_parser("restore-known-key")
    restore_known.add_argument("--contract", type=Path, required=True)
    restore_known.add_argument("--request", type=Path, required=True)
    restore_known.add_argument("--object-key", required=True)
    restore_known.add_argument("--destination", type=Path, required=True)
    restore_known.add_argument("--bucket")
    restore_known.add_argument("--endpoint-url")

    args = parser.parse_args()
    if args.command == "preflight":
        request, request_sha = load_exact_request(args.request, args.request_sha256)
        validate_dispatch_attempt(args.mode, args.run_attempt)
        expected_confirmation = confirmation_phrase(
            args.mode,
            request["slice_version"],
            request_sha,
            max_parallel=args.max_parallel,
            max_total_runner_minutes=args.max_total_runner_minutes,
            max_estimated_cost_usd=args.max_estimated_cost_usd,
            prior_runner_minutes=args.prior_runner_minutes,
        )
        if args.confirmation != expected_confirmation:
            raise ValueError(
                "typed confirmation differs; expected exactly " + expected_confirmation
            )
        verify_git_lineage(
            args.repository,
            producer_commit=request["producer_commit"],
            main_commit=args.main_commit,
            require_exact_checkout=False,
        )
        contract = build_contract(
            request,
            request_sha,
            prepared_at=args.prepared_at,
            max_parallel=args.max_parallel,
            max_total_runner_minutes=args.max_total_runner_minutes,
            max_estimated_cost_usd=args.max_estimated_cost_usd,
            runner_image_os=args.runner_image_os,
            runner_image_version=args.runner_image_version,
            prior_runner_minutes=args.prior_runner_minutes,
        )
        write_json(args.output, contract)
        print(json.dumps({"status": "ready", "request_sha256": request_sha}, sort_keys=True))
        return
    if args.command == "assert-exact-checkout":
        request, _ = load_exact_request(args.request, args.request_sha256)
        result = verify_git_lineage(
            args.repository,
            producer_commit=request["producer_commit"],
            main_commit=request["producer_commit"],
            require_exact_checkout=True,
        )
        print(json.dumps(result, sort_keys=True))
        return
    if args.command == "runtime":
        value = runtime_from_environment()
        write_json(args.output, value)
        print(json.dumps(value, sort_keys=True))
        return
    if args.command == "validate-inventory":
        request, _ = load_exact_request(args.request, args.request_sha256)
        value = read_json(args.inventory)
        if args.family == "addresses":
            validate_address_inventory(value, request)
        else:
            validate_places_inventory(value, request)
        print(json.dumps({"status": "valid", "family": args.family}, sort_keys=True))
        return
    if args.command == "validate-legacy-core":
        request, _ = load_exact_request(args.request, args.request_sha256)
        value = validate_legacy_core(args.manifest, request)
        write_json(args.output, value)
        print(json.dumps(value, sort_keys=True))
        return
    if args.command == "inspect-legacy-core":
        value = inspect_legacy_core(
            args.manifest,
            expected_version=args.expected_version,
            expected_overture_release=args.expected_overture_release,
        )
        write_json(args.output, value)
        print(json.dumps(value, sort_keys=True))
        return

    request = read_json(args.request)
    contract = validate_contract(read_json(args.contract), request)
    if args.command == "publish-immutable":
        identity = normalize_artifact(
            artifact_identity(args.source, args.object_key), contract
        )
        result = r2_verified_store.ensure_uploaded(
            _r2_store(args.bucket, args.endpoint_url),
            args.source,
            identity["object_key"],
        )
        args.report.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.report, result)
        marker = result
    elif args.command == "restore-known-key":
        marker = restore_known_key(
            _r2_store(args.bucket, args.endpoint_url),
            contract,
            object_key=args.object_key,
            destination=args.destination,
        )
    elif args.command == "restore-immutable":
        identity = normalize_artifact(
            {
                "object_key": args.object_key,
                "bytes": args.bytes,
                "sha256": args.sha256,
            },
            contract,
        )
        status = r2_verified_store.verified_download(
            _r2_store(args.bucket, args.endpoint_url),
            identity["object_key"],
            args.destination,
            expected_bytes=identity["bytes"],
            expected_sha256=identity["sha256"],
        )
        marker = {**identity, "status": status, "verified": True}
    elif args.command == "budget":
        result = phase_budget(
            contract,
            phase=args.phase,
            jobs=args.jobs,
            consumed_runner_minutes=args.consumed_runner_minutes,
        )
        write_json(args.output, result)
        marker = result
    elif args.command == "execution-budget":
        result = execution_budget(
            contract, map_jobs=args.map_jobs, reduce_jobs=args.reduce_jobs,
            prior_runner_minutes=args.prior_runner_minutes,
        )
        write_json(args.output, result)
        marker = result
    elif args.command == "task-completion":
        runtime_value = validate_runtime(read_json(args.runtime))
        marker = build_task_completion(
            contract,
            runtime_value,
            phase=args.phase,
            family=args.family,
            task_id=args.task_id,
            index=args.index,
            producer_report=artifact_identity(
                args.producer_report, args.producer_report_key
            ),
            artifacts=_load_artifact_list(args.artifacts),
            counters=read_json(args.counters),
        )
        write_json(args.output, marker)
    elif args.command == "phase-completion":
        runtime_value = validate_runtime(read_json(args.runtime))
        marker = build_phase_completion(
            contract,
            runtime_value,
            phase=args.phase,
            expected_tasks=read_json(args.expected_tasks),
            task_completions=[read_json(path) for path in args.task_completion],
        )
        write_json(args.output, marker)
    elif args.command == "control-phase-completion":
        runtime_value = validate_runtime(read_json(args.runtime))
        marker = build_control_phase_completion(
            contract,
            runtime_value,
            phase=args.phase,
            artifacts=_load_artifact_list(args.artifacts),
            dependency_evidence=read_json(args.dependency_evidence),
            details=read_json(args.details),
        )
        write_json(args.output, marker)
    elif args.command == "slice-candidate":
        runtime_value = validate_runtime(read_json(args.runtime))
        marker = build_slice_manifest(
            contract,
            request,
            runtime_value,
            address_manifest=read_json(args.addresses_manifest),
            places_manifest=read_json(args.places_manifest),
            remote_listing=read_json(args.remote_listing),
            phase_evidence=read_json(args.phase_evidence),
        )
        write_json(args.output, marker)
    else:
        marker = build_stop_evidence(
            contract,
            phase=args.phase,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            result=args.result,
            completed=args.completed,
        )
        write_json(args.output, marker)
    print(json.dumps(marker, sort_keys=True))


if __name__ == "__main__":
    main()
