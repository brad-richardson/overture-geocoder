#!/usr/bin/env python3
"""Hosted-runner adapter for resumable global-v2 task and phase boundaries.

Family producers remain local-file programs. This adapter is the only boundary
that turns their outputs into immutable R2 state: it validates every key,
uploads and readback-verifies the complete output set, then writes the signed
task completion marker last. Phase fan-in lists the remote marker prefix and
requires the exact expected set before restoring and validating any marker.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import global_v2_executor as executor  # noqa: E402
import global_v2_address_plan as address_plan  # noqa: E402
import global_v2_places_plan as places_plan  # noqa: E402
import global_v2_build_request as build_request  # noqa: E402
import global_build_manifest as family_manifest  # noqa: E402
import global_v2_address_reduce as address_reduce  # noqa: E402
import global_v2_places_reduce as places_reduce  # noqa: E402
import global_v2_places_head as places_head  # noqa: E402
import v2_release_manifest  # noqa: E402
import r2_verified_store  # noqa: E402


@contextlib.contextmanager
def canonical_temporary(value: Any) -> Iterable[Path]:
    with tempfile.TemporaryDirectory(prefix="global-v2-marker-") as directory:
        path = Path(directory) / "marker.json"
        executor.write_json(path, value)
        yield path


def task_marker_key(contract: dict[str, Any], phase: str, family: str, index: int) -> str:
    if phase not in {"map", "reduce", "head"} or family not in {"addresses", "places"}:
        raise ValueError("invalid task marker phase/family")
    executor.require_int(index, "task marker index", 0, 255)
    return (
        f"{contract['namespace']['task_completion_root']}/"
        f"{phase}/{family}/{index:03d}.json"
    )


def phase_marker_key(contract: dict[str, Any], phase: str) -> str:
    if phase not in executor.PHASES:
        raise ValueError("invalid phase marker")
    return f"{contract['namespace']['phase_completion_root']}/{phase}.json"


def _specs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError("output specs must be a non-empty array")
    result = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("output spec must be an object")
        executor.require_exact_fields(raw, {"path", "object_key"}, "output spec")
        path = Path(raw["path"])
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"output spec path is absent or empty: {path}")
        result.append({"path": str(path), "object_key": raw["object_key"]})
    if len({item["object_key"] for item in result}) != len(result):
        raise ValueError("output specs contain duplicate object keys")
    return result


def _verify_remote_artifact(
    store: r2_verified_store.ObjectStore, identity: dict[str, Any]
) -> None:
    remote = store.head(identity["object_key"])
    if remote is None:
        raise ValueError(f"completed task artifact is absent: {identity['object_key']}")
    if remote.bytes != identity["bytes"] or remote.sha256 != identity["sha256"]:
        raise ValueError(
            f"completed task artifact identity differs: {identity['object_key']}"
        )


def admit_existing_task(
    store: r2_verified_store.ObjectStore,
    contract: dict[str, Any],
    runtime: dict[str, Any],
    *,
    phase: str,
    family: str,
    task_id: str,
    index: int,
    verify_artifacts: bool = True,
) -> dict[str, Any]:
    """Admit an exact completed task across attempts without rerunning it."""
    marker_key = task_marker_key(contract, phase, family, index)
    if store.head(marker_key) is None:
        return {"completed": False, "marker_key": marker_key}
    with tempfile.TemporaryDirectory(prefix="global-v2-admit-") as directory:
        marker_path = Path(directory) / "marker.json"
        executor.restore_known_key(
            store, contract, object_key=marker_key, destination=marker_path
        )
        completion = executor.validate_task_completion(
            executor.read_json(marker_path), contract, runtime
        )
    expected = {
        "phase": phase,
        "family": family,
        "task_id": task_id,
        "index": index,
    }
    if any(completion[field] != value for field, value in expected.items()):
        raise ValueError("existing task completion belongs to another planned task")
    if verify_artifacts:
        for identity in [completion["producer_report"], *completion["artifacts"]]:
            _verify_remote_artifact(store, identity)
    return {
        "completed": True,
        "marker_key": marker_key,
        "completion": completion,
        "embedded_runtime_admitted": True,
        "artifacts_verified": verify_artifacts,
        "verification_mode": (
            "marker-and-artifact-heads" if verify_artifacts else "marker-only"
        ),
    }


def publish_control_completion(
    store: r2_verified_store.ObjectStore,
    contract: dict[str, Any],
    runtime: dict[str, Any],
    *,
    phase: str,
    artifacts: list[dict[str, Any]],
    dependency_evidence: list[dict[str, Any]],
    details: dict[str, Any],
) -> tuple[str, dict[str, Any], bool]:
    """Create or admit an exact control marker using its embedded runtime."""
    for identity in artifacts:
        _verify_remote_artifact(store, identity)
    marker_key = phase_marker_key(contract, phase)
    marker_runtime = runtime
    existing_marker: dict[str, Any] | None = None
    if store.head(marker_key) is not None:
        with tempfile.TemporaryDirectory(prefix="global-v2-control-admit-") as directory:
            path = Path(directory) / "marker.json"
            executor.restore_known_key(
                store, contract, object_key=marker_key, destination=path
            )
            value = executor.read_json(path)
        if not isinstance(value, dict):
            raise ValueError("existing control completion is not an object")
        existing_marker = value
        marker_runtime = value.get("runtime")
    marker = executor.build_control_phase_completion(
        contract, marker_runtime, phase=phase, artifacts=artifacts,
        dependency_evidence=dependency_evidence, details=details,
    )
    if existing_marker is not None and existing_marker != marker:
        raise ValueError("existing control completion differs from deterministic inputs")
    with canonical_temporary(marker) as marker_path:
        result = r2_verified_store.ensure_uploaded(store, marker_path, marker_key)
    if not result.get("readback_verified"):
        raise ValueError(f"{phase} completion failed remote readback")
    return marker_key, marker, existing_marker is not None


def publish_task(
    store: r2_verified_store.ObjectStore,
    contract: dict[str, Any],
    runtime: dict[str, Any],
    *,
    phase: str,
    family: str,
    task_id: str,
    index: int,
    producer_report_path: Path,
    producer_report_key: str,
    outputs: Any,
    counters: dict[str, int],
) -> dict[str, Any]:
    admitted = admit_existing_task(
        store, contract, runtime, phase=phase, family=family,
        task_id=task_id, index=index,
    )
    if admitted["completed"]:
        return {**admitted, "marker_written_last": True, "resumed": True}
    specs = _specs(outputs)
    report_identity = executor.normalize_artifact(
        executor.artifact_identity(producer_report_path, producer_report_key), contract
    )
    identities = [
        executor.normalize_artifact(
            executor.artifact_identity(Path(item["path"]), item["object_key"]), contract
        )
        for item in specs
    ]
    marker = executor.build_task_completion(
        contract,
        runtime,
        phase=phase,
        family=family,
        task_id=task_id,
        index=index,
        producer_report=report_identity,
        artifacts=identities,
        counters=counters,
    )
    for path, identity in [
        (producer_report_path, report_identity),
        *[(Path(spec["path"]), identity) for spec, identity in zip(specs, identities)],
    ]:
        result = r2_verified_store.ensure_uploaded(store, path, identity["object_key"])
        if not result.get("readback_verified"):
            raise ValueError("immutable task output did not pass remote readback")
    marker_key = task_marker_key(contract, phase, family, index)
    with canonical_temporary(marker) as marker_path:
        result = r2_verified_store.ensure_uploaded(store, marker_path, marker_key)
    if not result.get("readback_verified"):
        raise ValueError("task completion marker did not pass remote readback")
    return {"marker_key": marker_key, "completion": marker, "marker_written_last": True}


def restore_exact_phase(
    store: r2_verified_store.ObjectStore,
    contract: dict[str, Any],
    runtime: dict[str, Any],
    *,
    phase: str,
    expected_tasks: list[dict[str, Any]],
    output_dir: Path,
) -> dict[str, Any]:
    expected = executor.normalize_expected_tasks(expected_tasks, phase)
    expected_keys = {
        task_marker_key(contract, phase, item["family"], item["index"])
        for item in expected
    }
    prefix = f"{contract['namespace']['task_completion_root']}/{phase}/"
    actual_keys = set(store.list_prefix(prefix))
    if actual_keys != expected_keys:
        raise ValueError(
            f"remote task completion set differs: missing={sorted(expected_keys-actual_keys)}, "
            f"extra={sorted(actual_keys-expected_keys)}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    completions = []
    for task in expected:
        admitted = admit_existing_task(
            store, contract, runtime, phase=phase, family=task["family"],
            task_id=task["task_id"], index=task["index"],
        )
        if not admitted["completed"]:
            raise ValueError("listed task completion disappeared during admission")
        completion = admitted["completion"]
        destination = output_dir / f"{task['family']}-{task['index']:03d}.json"
        executor.write_json(destination, completion)
        completions.append(completion)
    key = phase_marker_key(contract, phase)
    existing = store.head(key)
    marker_runtime = runtime
    if existing is not None:
        with tempfile.TemporaryDirectory(prefix="global-v2-phase-admit-") as directory:
            existing_path = Path(directory) / "phase.json"
            executor.restore_known_key(
                store, contract, object_key=key, destination=existing_path
            )
            existing_marker = executor.read_json(existing_path)
        if not isinstance(existing_marker, dict):
            raise ValueError("existing phase completion is not an object")
        marker_runtime = existing_marker.get("runtime")
    marker = executor.build_phase_completion(
        contract, marker_runtime, phase=phase, expected_tasks=expected,
        task_completions=completions,
    )
    if existing is not None and existing_marker != marker:
        raise ValueError("existing phase completion differs from exact restored task set")
    with canonical_temporary(marker) as marker_path:
        result = r2_verified_store.ensure_uploaded(store, marker_path, key)
    if not result.get("readback_verified"):
        raise ValueError("phase completion marker did not pass remote readback")
    return {
        "marker_key": key, "completion": marker, "marker_written_last": True,
        "resumed": existing is not None,
    }


def reducer_matrix(address_plan: Any, places_plan: Any) -> dict[str, Any]:
    if not isinstance(address_plan, dict) or not isinstance(places_plan, dict):
        raise ValueError("both stable family plans are required")
    address_jobs = address_plan.get("jobs")
    places_jobs = places_plan.get("reduce_jobs")
    if not isinstance(address_jobs, list) or not isinstance(places_jobs, list):
        raise ValueError("family plans omit reducer jobs")
    include = []
    for family, jobs in (("addresses", address_jobs), ("places", places_jobs)):
        indexes = []
        for job in jobs:
            index = job.get("index")
            executor.require_int(index, f"{family} reducer index", 0, 255)
            task_id = (
                job.get("id")
                if family == "addresses"
                else f"places-reduce-{index:03d}"
            )
            expected_id = (
                f"address-reduce-job-{index:03d}"
                if family == "addresses"
                else f"places-reduce-{index:03d}"
            )
            if task_id != expected_id:
                raise ValueError(f"{family} reducer task id differs from its exact plan")
            indexes.append(index)
            include.append(
                {
                    "family": family,
                    "index": index,
                    "task_id": task_id,
                }
            )
        if indexes != list(range(len(jobs))):
            raise ValueError(f"{family} reducer matrix must exactly cover 0..N-1")
    if not include:
        raise ValueError("reducer matrix is empty")
    if len(include) > 256:
        raise ValueError("combined reducer matrix exceeds GitHub's 256-job limit")
    return {"include": include}


def publish_inventory_phase(
    store: r2_verified_store.ObjectStore,
    contract: dict[str, Any],
    runtime: dict[str, Any],
    request: dict[str, Any],
    *,
    address_inventory_path: Path,
    places_inventory_path: Path,
) -> dict[str, Any]:
    address_inventory = executor.validate_address_inventory(
        executor.read_json(address_inventory_path), request
    )
    places_inventory = executor.validate_places_inventory(
        executor.read_json(places_inventory_path), request
    )
    paths = {
        "addresses": address_inventory_path,
        "places": places_inventory_path,
    }
    identities = []
    for family, path in paths.items():
        key = f"{contract['namespace']['immutable_root']}/inventory/{family}.json"
        identity = executor.normalize_artifact(executor.artifact_identity(path, key), contract)
        result = r2_verified_store.ensure_uploaded(store, path, key)
        if not result.get("readback_verified"):
            raise ValueError(f"{family} inventory failed remote readback")
        identities.append(identity)
    marker_key, marker, resumed = publish_control_completion(
        store, contract, runtime, phase="inventory", artifacts=identities,
        dependency_evidence=[], details={
            "addresses_map_tasks": len(address_inventory["plan"]["tasks"]),
            "places_map_tasks": len(places_inventory["map_plan"]["tasks"]),
            "source_inventories_match_request": True,
        },
    )
    return {
        "marker_key": marker_key,
        "completion": marker,
        "artifacts": identities,
        "marker_written_last": True,
        "resumed": resumed,
    }


def address_map_boundary(
    inventory: dict[str, Any], task: dict[str, Any], report_path: Path,
    output_root: Path, *, maximum_hash_bits: int, remote_object_prefix: str,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Validate an address map task and derive its complete immutable outputs."""
    completion, manifest_identity, fragments = address_plan._validate_map_task(  # noqa: SLF001
        report_path, output_root, inventory=inventory, task=task,
        maximum_hash_bits=maximum_hash_bits,
    )
    specs = [{
        "path": str(output_root / manifest_identity["relative_path"]),
        "object_key": f"{remote_object_prefix}/manifests/{task['index']:03d}.json",
    }]
    specs.extend({
        "path": str(fragment["source_path"]),
        "object_key": f"{remote_object_prefix}/objects/{fragment['object_key']}",
    } for fragment in fragments)
    accounting = completion["accounting"]
    return specs, {
        "input_records": accounting["input_rows"],
        "retained_records": accounting["retained_rows"],
        "rejected_records": accounting["rejected_rows"],
        "output_records": accounting["retained_rows"],
    }


def places_map_boundary(
    request: dict[str, Any], inventory: dict[str, Any], task: dict[str, Any],
    report_path: Path, output_root: Path, *, remote_object_prefix: str,
    scratch_dir: Path,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Validate a Places map task and derive count plus fragment outputs."""
    report = executor.read_json(report_path)
    counts = report.get("counts") if isinstance(report, dict) else None
    fragments = report.get("fragments") if isinstance(report, dict) else None
    if not isinstance(counts, dict) or not isinstance(fragments, dict):
        raise ValueError("Places map report omits count/fragment outputs")
    objects = fragments.get("objects")
    if not isinstance(objects, list):
        raise ValueError("Places map report fragment objects must be an array")
    listing_items = [counts, *objects]
    listing = {
        item["object_key"]: (item["bytes"], item["sha256"])
        for item in listing_items
    }
    if len(listing) != len(listing_items):
        raise ValueError("Places map report contains duplicate output keys")
    store = places_plan._CountStore(scratch_dir)  # noqa: SLF001
    try:
        places_plan._validate_map_report(  # noqa: SLF001
            report, request=request, inventory=inventory, task=task,
            artifact_root=output_root, artifact_listing=listing, count_store=store,
        )
    finally:
        store.close()
    specs = [{
        "path": str(output_root / item["object_key"]),
        "object_key": f"{remote_object_prefix}/objects/{item['object_key']}",
    } for item in listing_items]
    accounting = report["accounting"]
    return specs, {
        "input_records": accounting["input_records"],
        "retained_records": accounting["retained_records"],
        "rejected_records": accounting["rejected_records"],
        "output_records": accounting["retained_records"],
    }


def restore_map_planner_inputs(
    store: r2_verified_store.ObjectStore, contract: dict[str, Any],
    runtime: dict[str, Any], *, expected_tasks: list[dict[str, Any]], output_root: Path,
) -> dict[str, Any]:
    """Restore exact map reports and only the small artifacts planners scan."""
    phase = restore_exact_phase(
        store, contract, runtime, phase="map", expected_tasks=expected_tasks,
        output_dir=output_root / "markers",
    )
    markers = [
        executor.validate_task_completion(executor.read_json(path), contract, runtime)
        for path in sorted((output_root / "markers").glob("*.json"))
    ]
    for family in ("addresses", "places"):
        family_markers = [item for item in markers if item["family"] == family]
        if not family_markers:
            continue
        expected_keys = {
            identity["object_key"]
            for marker in family_markers
            for identity in [marker["producer_report"], *marker["artifacts"]]
        }
        prefix = f"{contract['namespace']['immutable_root']}/map/{family}/"
        actual_keys = set(store.list_prefix(prefix))
        if actual_keys != expected_keys:
            raise ValueError(
                f"remote {family} map output set differs: "
                f"missing={sorted(expected_keys-actual_keys)}, extra={sorted(actual_keys-expected_keys)}"
            )
    restored = []
    for marker in markers:
        family, index = marker["family"], marker["index"]
        report = output_root / family / "reports" / f"{index:03d}.json"
        executor.restore_known_key(
            store, contract, object_key=marker["producer_report"]["object_key"],
            destination=report,
        )
        artifacts = marker["artifacts"]
        if family == "addresses":
            selected = [item for item in artifacts if "/manifests/" in item["object_key"]]
            if len(selected) != 1 or not any(
                "/objects/map/address-fragments/" in item["object_key"] for item in artifacts
            ):
                raise ValueError("address map marker output set is incomplete")
            destination = output_root / family / f"task-{index:03d}" / "fragment-manifest.json"
        else:
            selected = [item for item in artifacts if "/objects/counts/" in item["object_key"]]
            if len(selected) != 1 or not any(
                "/objects/fragments/" in item["object_key"] for item in artifacts
            ):
                raise ValueError("Places map marker output set is incomplete")
            logical = selected[0]["object_key"].split("/objects/", 1)[1]
            destination = output_root / family / "artifacts" / logical
        identity = selected[0]
        r2_verified_store.verified_download(
            store, identity["object_key"], destination,
            expected_bytes=identity["bytes"], expected_sha256=identity["sha256"],
        )
        restored.append({"family": family, "index": index, "report": str(report), "planner_artifact": str(destination)})
    return {"phase": phase, "tasks": restored}


def require_build1_lineage(request: dict[str, Any]) -> None:
    """Fail closed until a later request binds predecessor keys as well as SHAs."""
    request = build_request.validate_request(request)
    predecessors = {
        family: request["families"][family]["predecessor_family_manifest_sha256"]
        for family in ("addresses", "places")
    }
    triples = {
        family: request["families"][family]["predecessor_family_manifest"]
        for family in ("addresses", "places")
    }
    for family, digest in predecessors.items():
        if triples[family]["sha256"] != digest:
            raise ValueError(f"{family} predecessor digest aliases differ")


def restore_predecessor_manifests(
    store: r2_verified_store.ObjectStore, request: dict[str, Any], output_root: Path
) -> dict[str, Path | None]:
    request = build_request.validate_request(request)
    result: dict[str, Path | None] = {}
    for family in ("addresses", "places"):
        identity = request["families"][family]["predecessor_family_manifest"]
        if identity["object_key"] is None:
            result[family] = None
            continue
        destination = output_root / family / "family-manifest.json"
        r2_verified_store.verified_download(
            store, identity["object_key"], destination,
            expected_bytes=identity["bytes"], expected_sha256=identity["sha256"],
        )
        manifest = family_manifest.validate_family_manifest(executor.read_json(destination))
        source_version = identity["object_key"].split("/", 1)[0]

        def build_order(value: str) -> tuple[str, int]:
            raw = value.removeprefix("slice-")
            date, number = raw.rsplit(".", 1)
            return date, int(number)

        if (
            manifest["family"] != family
            or manifest["region"] != {
                "name": "global",
                "bbox": [-180.0, -90.0, 180.0, 90.0],
                "bbox_scope": "exact",
            }
            or build_order(source_version) >= build_order(request["geocoder_build"])
        ):
            raise ValueError(
                f"{family} predecessor family/global/build ordering differs"
            )
        # Sticky partition lineage intentionally crosses monthly Overture
        # releases and may cross safe tokenizer/format or row-cap rotations.
        # The exact request-bound key/bytes/SHA and canonical family manifest
        # are the identity boundary; the restored address partition plan or
        # Places catalog validates scheme, depth growth, and sticky splits in
        # its family-specific planner downstream.
        result[family] = destination
    return result


def restore_predecessor_plan_artifacts(
    store: r2_verified_store.ObjectStore, request: dict[str, Any],
    manifests: dict[str, Path | None], output_root: Path,
) -> dict[str, Path | None]:
    wanted = {
        "addresses": "families/addresses/partition-plan.json",
        "places": "families/places/catalog.pcat",
    }
    restored: dict[str, Path | None] = {}
    for family, manifest_path in manifests.items():
        if manifest_path is None:
            restored[family] = None
            continue
        manifest = family_manifest.validate_family_manifest(executor.read_json(manifest_path))
        matches = [item for item in manifest["artifacts"] if item["object_key"] == wanted[family]]
        if len(matches) != 1:
            raise ValueError(f"{family} predecessor omits exact planning artifact")
        source_version = request["families"][family]["predecessor_family_manifest"]["object_key"].split("/", 1)[0]
        item = matches[0]
        destination = output_root / family / Path(item["object_key"]).name
        r2_verified_store.verified_download(
            store, f"{source_version}/{item['object_key']}", destination,
            expected_bytes=item["bytes"], expected_sha256=item["sha256"],
        )
        restored[family] = destination
    return restored


def build_aggregate_plans(
    request: dict[str, Any], address_inventory: dict[str, Any],
    places_inventory: dict[str, Any], *, restored_root: Path, output_root: Path,
    build_number: int, address_fragment_fetch_command: list[str],
    predecessor_manifests: dict[str, Path | None] | None = None,
    predecessor_planning_artifacts: dict[str, Path | None] | None = None,
) -> tuple[Path, Path, list[dict[str, str]]]:
    """Build both stable plans from selective map restoration, without fragments."""
    require_build1_lineage(request)
    predecessor_manifests = predecessor_manifests or {"addresses": None, "places": None}
    predecessor_planning_artifacts = predecessor_planning_artifacts or {"addresses": None, "places": None}
    address_fragment_fetch_command = address_plan.parse_fetch_command(
        json.dumps(address_fragment_fetch_command)
    )
    address_tasks = address_inventory["plan"]["tasks"]
    address_inputs = [
        (
            restored_root / "addresses/reports" / f"{task['index']:03d}.json",
            restored_root / "addresses" / f"task-{task['index']:03d}",
        )
        for task in address_tasks
    ]
    address_root = output_root / "addresses"
    address_plan.build_fanin_plan(
        address_inventory, address_inputs, address_root,
        build_number=build_number,
        lineage_generation=request["families"]["addresses"]["partition"][
            "lineage_generation"
        ],
        predecessor_family_manifest=request["families"]["addresses"][
            "predecessor_family_manifest"
        ],
        previous_plan=(None if predecessor_planning_artifacts["addresses"] is None else executor.read_json(predecessor_planning_artifacts["addresses"])),
        expected_previous_sha256=(None if predecessor_planning_artifacts["addresses"] is None else executor.sha256_file(predecessor_planning_artifacts["addresses"])),
        maximum_hash_bits=request["families"]["addresses"]["partition"]["maximum_hash_bits"],
        row_cap=request["families"]["addresses"]["partition"]["split_row_cap"],
        max_reduce_jobs=request["execution"]["reduce_job_limit"],
        fragment_fetch_command=address_fragment_fetch_command,
        stage_local_fragments=False,
    )
    places_reports = restored_root / "places/reports"
    places_artifacts = restored_root / "places/artifacts"
    listing_objects = []
    for path in sorted(places_reports.glob("*.json")):
        report = executor.read_json(path)
        for item in [report["counts"], *report["fragments"]["objects"]]:
            listing_objects.append({
                "object_key": item["object_key"], "bytes": item["bytes"],
                "sha256": item["sha256"],
            })
    places_value = places_plan.build_places_plan(
        request, places_inventory,
        [executor.read_json(path) for path in sorted(places_reports.glob("*.json"))],
        artifact_root=places_artifacts, scratch_dir=output_root / "places-scratch",
        artifact_listing={"schema": places_plan.ARTIFACT_LISTING_SCHEMA, "objects": listing_objects},
        predecessor_family_manifest=(None if predecessor_manifests["places"] is None else executor.read_json(predecessor_manifests["places"])),
        predecessor_catalog=predecessor_planning_artifacts["places"],
    )
    places_path = output_root / "places/plan.json"
    executor.write_json(places_path, places_value)
    address_reduce_path = address_root / "families/addresses/reduce-plan.json"
    specs = [
        {"path": str(path), "relative": path.relative_to(output_root).as_posix()}
        for path in sorted(output_root.rglob("*"))
        if path.is_file()
        and not any(part in {"places-scratch", "predecessors", "predecessor-artifacts"} for part in path.parts)
    ]
    return address_reduce_path, places_path, specs


def publish_aggregate_plans(
    store: r2_verified_store.ObjectStore, contract: dict[str, Any],
    runtime: dict[str, Any], *, specs: list[dict[str, str]],
    map_completion: dict[str, Any], output_prefix: str,
) -> dict[str, Any]:
    identities = []
    for spec in specs:
        path = Path(spec["path"])
        key = f"{output_prefix}/{spec['relative']}"
        identity = executor.normalize_artifact(executor.artifact_identity(path, key), contract)
        result = r2_verified_store.ensure_uploaded(store, path, key)
        if not result.get("readback_verified"):
            raise ValueError("aggregate plan artifact failed remote readback")
        identities.append(identity)
    key, marker, resumed = publish_control_completion(
        store, contract, runtime, phase="aggregate-plan", artifacts=identities,
        dependency_evidence=[{
            "phase": "map", "completion_sha256": map_completion["completion_sha256"],
        }],
        details={"families": ["addresses", "places"], "predecessor_lineage_bound": True},
    )
    return {
        "marker_key": key, "completion": marker, "marker_written_last": True,
        "resumed": resumed,
    }


def restore_reducer_plans(
    store: r2_verified_store.ObjectStore, contract: dict[str, Any],
    runtime: dict[str, Any], *, output_root: Path, output_prefix: str,
) -> dict[str, Any]:
    executor.validate_runtime_for_contract(runtime, contract)
    marker_path = output_root / "aggregate-plan-completion.json"
    executor.restore_known_key(
        store, contract, object_key=phase_marker_key(contract, "aggregate-plan"),
        destination=marker_path,
    )
    marker = executor.read_json(marker_path)
    unsigned = {key: value for key, value in marker.items() if key != "completion_sha256"}
    if (
        marker.get("schema") != executor.PHASE_COMPLETION_SCHEMA
        or marker.get("phase") != "aggregate-plan"
        or marker.get("request_sha256") != contract["request"]["sha256"]
        or marker.get("completion_sha256") != executor.sha256_bytes(executor.canonical_json(unsigned))
        or not isinstance(marker.get("artifacts"), list)
    ):
        raise ValueError("aggregate-plan completion marker is invalid")
    expected = {item["object_key"] for item in marker["artifacts"]}
    actual = set(store.list_prefix(output_prefix + "/"))
    if actual != expected:
        raise ValueError("remote aggregate-plan artifact set differs")
    selected = {}
    suffixes = {
        "addresses": "/addresses/families/addresses/reduce-plan.json",
        "address_partition": "/addresses/families/addresses/partition-plan.json",
        "places": "/places/plan.json",
    }
    for family, suffix in suffixes.items():
        matches = [item for item in marker["artifacts"] if item["object_key"].endswith(suffix)]
        if len(matches) != 1:
            raise ValueError(f"aggregate plan omits exact {family} reducer plan")
        item = matches[0]
        destination = output_root / f"{family}-plan.json"
        r2_verified_store.verified_download(
            store, item["object_key"], destination,
            expected_bytes=item["bytes"], expected_sha256=item["sha256"],
        )
        selected[family] = destination
    matrix = reducer_matrix(
        executor.read_json(selected["addresses"]), executor.read_json(selected["places"])
    )
    return {
        "aggregate_completion_sha256": marker["completion_sha256"],
        "address_plan": str(selected["addresses"]),
        "address_partition_plan": str(selected["address_partition"]),
        "places_plan": str(selected["places"]),
        "matrix": matrix,
    }


def fragment_fetch_command(prefix: str) -> list[str]:
    endpoint = executor.os.environ.get("R2_ENDPOINT")
    if not endpoint:
        raise ValueError("R2_ENDPOINT is required for hosted fragment fetch")
    return [
        sys.executable, str(SCRIPT_DIR / "r2_fragment_fetch.py"),
        "--bucket", executor.os.environ.get("R2_BUCKET", "geocoder-shards"),
        "--prefix", prefix, "--object-key", "{object_key}",
        "--output", "{output}", "--endpoint-url", endpoint,
    ]


def run_reduce_task(
    contract: dict[str, Any], runtime: dict[str, Any], *, family: str, index: int,
    matrix_jobs: int, consumed_runner_minutes: int, request: dict[str, Any],
    address_partition_path: Path, address_plan_path: Path, places_plan_path: Path,
    work_root: Path,
) -> tuple[Path, list[dict[str, str]], dict[str, int], str]:
    executor.phase_budget(
        contract, phase="reduce", jobs=matrix_jobs,
        consumed_runner_minutes=consumed_runner_minutes,
    )
    executor.validate_runtime_for_contract(runtime, contract)
    fetch_prefix = f"{contract['namespace']['immutable_root']}/map/{family}/objects"
    fetch = fragment_fetch_command(fetch_prefix)
    output_root, scratch = work_root / "output", work_root / "scratch"
    if family == "addresses":
        reduce_value = executor.read_json(address_plan_path)
        if not 0 <= index < len(reduce_value["jobs"]):
            raise ValueError("address reducer index outside exact matrix")
        job = reduce_value["jobs"][index]
        report = address_reduce.run_job(
            address_partition_path, address_plan_path, job_id=job["id"],
            input_root=work_root / "unused-inputs", output_root=output_root,
            fragment_fetch_command=fetch,
        )
        report_path = output_root / f"families/addresses/reduce-completions/{job['id']}.json"
        specs = []
        for item in report["artifacts"]:
            for kind in ("index", "data"):
                relative = item[kind]["relative_path"]
                specs.append({"path": str(output_root / relative), "object_key": f"{request['slice_version']}/{relative}"})
        accounting = report["accounting"]
        counters = {
            "input_records": accounting["expected_rows"], "retained_records": accounting["output_rows"],
            "rejected_records": 0, "output_records": accounting["output_rows"],
        }
        task_id = job["id"]
    else:
        plan_value = executor.read_json(places_plan_path)
        report = places_reduce.execute_reduce_job(
            plan_value, job_index=index, artifact_root=work_root / "unused-inputs",
            scratch_dir=scratch, output_dir=output_root,
            fragment_fetch_command=fetch,
        )
        report = places_reduce.validate_reduce_report(report, plan_value)
        report_path = work_root / "places-reduce-report.json"
        executor.write_json(report_path, report)
        specs = [{
            "path": str(output_root / item["object"]),
            "object_key": f"{request['slice_version']}/families/places/{item['object']}",
        } for item in report["shards"]]
        candidate = report["head_candidates"]
        specs.append({
            "path": str(output_root / candidate["object_key"]),
            "object_key": (
                f"{contract['namespace']['immutable_root']}/reduce/places/"
                f"{candidate['object_key']}"
            ),
        })
        accounting = report["accounting"]
        counters = {
            "input_records": accounting["input_fragment_records"],
            "retained_records": accounting["output_records"], "rejected_records": 0,
            "output_records": accounting["output_records"],
        }
        task_id = f"places-reduce-{index:03d}"
    return report_path, specs, counters, task_id


def validate_phase_marker(value: Any, contract: dict[str, Any], phase: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("phase completion marker must be an object")
    unsigned = {key: item for key, item in value.items() if key != "completion_sha256"}
    if (
        value.get("schema") != executor.PHASE_COMPLETION_SCHEMA
        or value.get("phase") != phase
        or value.get("request_sha256") != contract["request"]["sha256"]
        or value.get("completion_sha256") != executor.sha256_bytes(executor.canonical_json(unsigned))
    ):
        raise ValueError(f"{phase} completion marker is invalid")
    return value


def run_head_task(
    store: r2_verified_store.ObjectStore,
    contract: dict[str, Any], runtime: dict[str, Any], *, request: dict[str, Any],
    places_plan_path: Path, reduce_completion: dict[str, Any], work_root: Path,
    reduce_marker_dir: Path, consumed_runner_minutes: int,
) -> tuple[Path, list[dict[str, str]], dict[str, int], str]:
    executor.phase_budget(
        contract, phase="head", jobs=1,
        consumed_runner_minutes=consumed_runner_minutes,
    )
    executor.validate_runtime_for_contract(runtime, contract)
    validate_phase_marker(reduce_completion, contract, "reduce")
    plan_value = executor.read_json(places_plan_path)
    candidate_prefix = f"{contract['namespace']['immutable_root']}/reduce/places"
    reduce_reports = []
    places_markers = [
        executor.validate_task_completion(executor.read_json(path), contract, runtime)
        for path in sorted(reduce_marker_dir.glob("places-*.json"))
    ]
    if [marker["index"] for marker in places_markers] != list(
        range(len(plan_value["reduce_jobs"]))
    ):
        raise ValueError("Places head requires every reducer checkpoint")
    for marker in places_markers:
        report_path = work_root / "reduce-reports" / f"{marker['index']:03d}.json"
        identity = marker["producer_report"]
        r2_verified_store.verified_download(
            store, identity["object_key"], report_path,
            expected_bytes=identity["bytes"], expected_sha256=identity["sha256"],
        )
        report = executor.read_json(report_path)
        expected_candidate_key = (
            f"{candidate_prefix}/{report['head_candidates']['object_key']}"
        )
        matches = [
            item for item in marker["artifacts"]
            if item["object_key"] == expected_candidate_key
        ]
        if len(matches) != 1:
            raise ValueError("Places reducer checkpoint omits exact head candidates")
        reduce_reports.append(report)
    output = work_root / "output/head.phrp"
    report = places_head.build_global_head(
        request, plan_value, reduce_reports=reduce_reports,
        artifact_root=work_root / "reduce-inputs",
        scratch_dir=work_root / "scratch", output=output,
        fragment_fetch_command=fragment_fetch_command(candidate_prefix),
    )
    report = places_head.validate_head_report(report, request, plan_value)
    report_path = work_root / "head-report.json"
    executor.write_json(report_path, report)
    retained = report["accounting"]["retained_records"]
    return report_path, [{
        "path": str(output),
        "object_key": f"{request['slice_version']}/families/places/head.phrp",
    }], {
        "input_records": retained, "retained_records": retained,
        "rejected_records": 0, "output_records": retained,
    }, "places-head-000"


def restore_finalization_reports(
    store: r2_verified_store.ObjectStore, contract: dict[str, Any],
    runtime: dict[str, Any], *, expected_reduce_tasks: list[dict[str, Any]],
    expected_head_tasks: list[dict[str, Any]], output_root: Path,
) -> dict[str, Any]:
    """Exact fan-in of reduce/head markers and reports without serving readback."""
    phases = {}
    markers = []
    for phase, expected in (
        ("reduce", expected_reduce_tasks), ("head", expected_head_tasks)
    ):
        directory = output_root / phase / "markers"
        phases[phase] = restore_exact_phase(
            store, contract, runtime, phase=phase, expected_tasks=expected,
            output_dir=directory,
        )
        markers.extend(
            executor.validate_task_completion(executor.read_json(path), contract, runtime)
            for path in sorted(directory.glob("*.json"))
        )
    expected_serving: dict[str, tuple[int, str]] = {}
    restored_reports = []
    for marker in markers:
        for artifact in marker["artifacts"]:
            key = artifact["object_key"]
            identity = (artifact["bytes"], artifact["sha256"])
            if key in expected_serving:
                raise ValueError("reduce/head serving output key is duplicated")
            expected_serving[key] = identity
            if store.head(key) != r2_verified_store.ObjectInfo(*identity):
                raise ValueError(f"remote serving output identity differs: {key}")
        destination = (
            output_root / marker["phase"] / "reports"
            / f"{marker['family']}-{marker['index']:03d}.json"
        )
        identity = marker["producer_report"]
        r2_verified_store.verified_download(
            store, identity["object_key"], destination,
            expected_bytes=identity["bytes"], expected_sha256=identity["sha256"],
        )
        restored_reports.append({
            "phase": marker["phase"], "family": marker["family"],
            "index": marker["index"], "path": str(destination),
        })
    slice_prefix = contract["namespace"]["slice_root"]
    actual = set(store.list_prefix(slice_prefix))
    if actual != set(expected_serving):
        # A retry may begin after either or both family manifests were
        # published but before the slice marker or Worker smoke completed.
        # Accept only exact manifest-bound partial/finalized states; arbitrary
        # extra slice objects still fail closed.
        expected_finalized: dict[str, tuple[int, str]] = dict(expected_serving)
        for family in ("addresses", "places"):
            manifest_key = f"{slice_prefix}families/{family}/family-manifest.json"
            if manifest_key not in actual:
                continue
            manifest_path = output_root / "existing-manifests" / f"{family}.json"
            executor.restore_known_key(
                store, contract, object_key=manifest_key, destination=manifest_path
            )
            manifest = family_manifest.validate_family_manifest(
                executor.read_json(manifest_path)
            )
            if manifest["family"] != family:
                raise ValueError("existing family manifest declares another family")
            manifest_bytes = executor.canonical_json(manifest)
            expected_finalized[manifest_key] = (
                len(manifest_bytes), executor.sha256_bytes(manifest_bytes)
            )
            for artifact in manifest["artifacts"]:
                expected_finalized[f"{slice_prefix}{artifact['object_key']}"] = (
                    artifact["bytes"], artifact["sha256"]
                )
        if actual != set(expected_finalized):
            raise ValueError("remote partial/finalized family object set differs from manifests")
        for key, identity in expected_finalized.items():
            if store.head(key) != r2_verified_store.ObjectInfo(*identity):
                raise ValueError(f"remote finalized object identity differs: {key}")
    return {"phases": phases, "reports": restored_reports, "serving": expected_serving}


def _publish_family_manifest_last(
    store: r2_verified_store.ObjectStore, contract: dict[str, Any],
    request: dict[str, Any], *, family: str, report_path: Path,
    local_artifacts: list[tuple[Path, str]], manifest: dict[str, Any],
    output_root: Path,
) -> dict[str, Any]:
    report_key = f"{contract['namespace']['immutable_root']}/finalize/{family}/report.json"
    r2_verified_store.ensure_uploaded(store, report_path, report_key)
    for path, relative in local_artifacts:
        r2_verified_store.ensure_uploaded(
            store, path, f"{request['slice_version']}/{relative}"
        )
    manifest_path = output_root / family / "family-manifest.json"
    executor.write_json(manifest_path, manifest)
    manifest_key = f"{request['slice_version']}/families/{family}/family-manifest.json"
    r2_verified_store.ensure_uploaded(store, manifest_path, manifest_key)
    expected = {
        f"{request['slice_version']}/{item['object_key']}" for item in manifest["artifacts"]
    } | {manifest_key}
    actual = set(store.list_prefix(f"{request['slice_version']}/families/{family}/"))
    if actual != expected:
        raise ValueError(f"remote {family} finalized object set differs")
    return {
        "family": family, "manifest": str(manifest_path),
        "manifest_key": manifest_key, "manifest_sha256": executor.sha256_file(manifest_path),
        "manifest_written_last": True,
    }


def finalize_publish_families(
    store: r2_verified_store.ObjectStore, contract: dict[str, Any],
    request: dict[str, Any], *, address_partition_path: Path,
    address_reduce_path: Path, places_plan_path: Path,
    finalization_inputs: dict[str, Any], work_root: Path,
) -> dict[str, Any]:
    reports = finalization_inputs["reports"]
    address_reports = [Path(item["path"]) for item in reports if item["phase"] == "reduce" and item["family"] == "addresses"]
    places_reports = [executor.read_json(Path(item["path"])) for item in reports if item["phase"] == "reduce" and item["family"] == "places"]
    head_matches = [executor.read_json(Path(item["path"])) for item in reports if item["phase"] == "head" and item["family"] == "places"]
    if len(head_matches) != 1:
        raise ValueError("finalization requires exactly one Places head report")
    fetch = fragment_fetch_command(request["slice_version"])
    address_root = work_root / "addresses"
    address_completion = address_reduce.finalize(
        address_partition_path, address_reduce_path, address_reports,
        output_root=address_root, artifact_fetch_command=fetch,
    )
    if not address_completion["artifact_materialization"]["exact_content_identity_verified"]:
        raise ValueError("address finalization omitted exact remote parser verification")
    partition_relative = "families/addresses/partition-plan.json"
    partition_copy = address_root / partition_relative
    partition_copy.parent.mkdir(parents=True, exist_ok=True)
    partition_copy.write_bytes(address_partition_path.read_bytes())
    serving = finalization_inputs["serving"]
    address_artifacts = [
        {"object_key": key.split(f"{request['slice_version']}/", 1)[1], "bytes": size, "sha256": digest}
        for key, (size, digest) in serving.items() if "/families/addresses/shards/" in key
    ]
    for path, relative in (
        (partition_copy, partition_relative),
        (address_root / "families/addresses/address-collection.json", "families/addresses/address-collection.json"),
    ):
        address_artifacts.append({
            "object_key": relative, "bytes": path.stat().st_size,
            "sha256": executor.sha256_file(path),
        })
    addresses = request["families"]["addresses"]
    address_manifest = family_manifest.build_family_manifest(
        "addresses",
        lineage={
            "overture_release": request["overture_release"],
            "build_id": address_completion["partition_plan_sha256"],
            "producer_commit": request["producer_commit"],
            "producer_script": "scripts/global_v2_address_reduce.py",
            "producer_version": "1",
        },
        versions={
            "format": addresses["versions"]["format"], "tokenizer": None,
            "normalization": addresses["versions"]["normalization"],
        },
        region={"name": "global", "bbox": [-180.0, -90.0, 180.0, 90.0], "bbox_scope": "exact"},
        artifacts=address_artifacts, generated_at=None,
    )
    address_report_path = address_root / "families/addresses/reduce-completion.json"
    address_result = _publish_family_manifest_last(
        store, contract, request, family="addresses", report_path=address_report_path,
        local_artifacts=[
            (partition_copy, partition_relative),
            (address_root / "families/addresses/address-collection.json", "families/addresses/address-collection.json"),
        ], manifest=address_manifest, output_root=work_root,
    )
    places_root = work_root / "places"
    places_report, places_manifest = places_plan.finalize_places_family(
        request, executor.read_json(places_plan_path), places_reports, head_matches[0],
        output_dir=places_root, scratch_dir=work_root / "places-scratch",
        fragment_fetch_command=fetch,
    )
    places_report = places_plan.validate_places_final_report(
        places_report, request, executor.read_json(places_plan_path)
    )
    places_report_path = work_root / "places-final-report.json"
    executor.write_json(places_report_path, places_report)
    places_result = _publish_family_manifest_last(
        store, contract, request, family="places", report_path=places_report_path,
        local_artifacts=[(places_root / "catalog.pcat", "families/places/catalog.pcat")],
        manifest=places_manifest, output_root=work_root,
    )
    return {"addresses": address_result, "places": places_result}


def publish_preview_catalog(
    store: r2_verified_store.ObjectStore, contract: dict[str, Any],
    request: dict[str, Any], *, legacy_manifest_path: Path,
    address_manifest_path: Path, places_manifest_path: Path,
    slice_manifest_path: Path, run_id: str, generated_at: str,
    output_root: Path,
) -> dict[str, Any]:
    catalog_key = f"smoketest-v2/{run_id}/catalog.json"
    release_key = v2_release_manifest.release_manifest_key_for_catalog(
        catalog_key, request["geocoder_build"]
    )
    if release_key != f"smoketest-v2/{run_id}/release.json":
        raise ValueError("preview release is not the catalog's isolated sibling")
    legacy = executor.read_json(legacy_manifest_path)
    family_paths = {"addresses": address_manifest_path, "places": places_manifest_path}
    families = {
        family: (executor.read_json(path), executor.sha256_file(path))
        for family, path in family_paths.items()
    }
    slice_manifest = executor.read_json(slice_manifest_path)
    source = (slice_manifest, executor.sha256_file(slice_manifest_path))
    sources = {family: source for family in families}
    release = v2_release_manifest.build_release_manifest(
        geocoder_build=request["geocoder_build"],
        overture_release=request["overture_release"], legacy_release=legacy,
        legacy_manifest_sha256=executor.sha256_file(legacy_manifest_path),
        family_manifests=families, family_source_manifests=sources,
        family_operations={
            family: request["families"][family]["operations"] for family in families
        },
        family_entrypoints={
            "addresses": {"structured_forward": "families/addresses/address-collection.json"},
            "places": {"forward": "families/places/catalog.pcat"},
        },
        generated_at=generated_at,
    )
    output_root.mkdir(parents=True, exist_ok=True)
    release_path = output_root / "release.json"
    executor.write_json(release_path, release)
    catalog = v2_release_manifest.build_catalog(
        release_manifest=release,
        release_manifest_sha256=executor.sha256_file(release_path),
        legacy_release=legacy,
        legacy_manifest_sha256=executor.sha256_file(legacy_manifest_path),
        family_manifests=families, family_source_manifests=sources,
        initialize=True, generated_at=generated_at, catalog_key=catalog_key,
    )
    catalog_path = output_root / "catalog.json"
    executor.write_json(catalog_path, catalog)
    uploaded = []
    for path, key in ((release_path, release_key), (catalog_path, catalog_key)):
        executor.normalize_artifact(executor.artifact_identity(path, key), contract)
        result = r2_verified_store.ensure_uploaded(store, path, key)
        if not result.get("readback_verified"):
            raise ValueError("preview object failed remote readback")
        uploaded.append(key)
    return {
        "catalog_key": catalog_key, "release_key": release_key,
        "uploaded_in_order": uploaded, "catalog_written_last": True,
        "production_catalog_writes": False,
        "cleanup": {
            "worker_name": f"geocoder-global-v2-{run_id}",
            "object_keys": [catalog_key, release_key],
            "verify_empty_prefix": f"smoketest-v2/{run_id}/",
        },
    }


def publish_worker_smoke_evidence(
    store: r2_verified_store.ObjectStore,
    contract: dict[str, Any],
    runtime: dict[str, Any],
    request: dict[str, Any],
    *,
    preview: dict[str, Any],
    health: dict[str, Any],
    places_query: dict[str, Any],
    address_query: dict[str, Any],
    smoke_requests: dict[str, Any],
    cleanup: dict[str, Any],
    slice_completion: dict[str, Any],
) -> dict[str, Any]:
    """Validate isolated Worker smoke plus cleanup, then retain signed evidence."""
    expected_cleanup = preview.get("cleanup")
    if (
        preview.get("production_catalog_writes") is not False
        or not isinstance(expected_cleanup, dict)
        or health.get("status") != "ok"
        or health.get("geocoder_build") != request["geocoder_build"]
        or health.get("overture_release") != request["overture_release"]
        or health.get("catalog_key") != preview.get("catalog_key")
        or health.get("candidate_isolated") is not True
    ):
        raise ValueError("preview publication or Worker health evidence is invalid")
    if not isinstance(smoke_requests, dict) or set(smoke_requests) != {
        "places", "addresses"
    }:
        raise ValueError("smoke requests must bind exactly both family queries")
    query_summaries = {}
    for family, query in (("places", places_query), ("addresses", address_query)):
        submitted = smoke_requests[family]
        if not isinstance(submitted, dict) or set(submitted) != {
            "query", "expected_id"
        }:
            raise ValueError(f"{family} smoke request is not exact")
        expected_id = submitted["expected_id"]
        request_query = submitted["query"]
        if (
            not isinstance(expected_id, str)
            or not expected_id
            or (
                family == "places"
                and (
                    not isinstance(request_query, dict)
                    or set(request_query) != {"q", "types", "limit"}
                    or not isinstance(request_query["q"], str)
                    or not request_query["q"]
                    or request_query["types"] != ["poi"]
                    or request_query["limit"] != 5
                )
            )
            or (
                family == "addresses"
                and (
                    not isinstance(request_query, dict)
                    or set(request_query) != {
                        "country", "admin_level_general", "admin_level_specific",
                        "postal_city", "postcode", "street", "number", "unit",
                    }
                    or any(not isinstance(value, str) for value in request_query.values())
                )
            )
        ):
            raise ValueError(f"{family} smoke request binding is invalid")
        data_version = query.get("data_version")
        features = query.get("features")
        expected_type = "poi" if family == "places" else "address"
        expected_mode = "text" if family == "places" else "structured_address"
        if (
            query.get("type") != "FeatureCollection"
            or not isinstance(features, list)
            or not features
            or any(
                not isinstance(item, dict)
                or item.get("properties", {}).get("feature_type") != expected_type
                for item in features
            )
            or not isinstance(data_version, dict)
            or data_version.get("geocoder_build") != request["geocoder_build"]
            or data_version.get("overture_release") != request["overture_release"]
            or query.get("metadata", {}).get("mode") != expected_mode
            or not any(
                isinstance(item, dict) and item.get("id") == expected_id
                for item in features
            )
        ):
            raise ValueError(
                f"preview Worker did not return the requested {family} family build"
            )
        query_summaries[family] = {
            "feature_count": len(features),
            "data_version": data_version,
            "metadata": query.get("metadata"),
            "submitted_query": request_query,
            "expected_id": expected_id,
        }
    expected_keys = set(expected_cleanup.get("object_keys", []))
    deleted_keys = cleanup.get("deleted_object_keys")
    if (
        cleanup.get("worker_name") != expected_cleanup.get("worker_name")
        or cleanup.get("worker_deleted") is not True
        or not isinstance(deleted_keys, list)
        or set(deleted_keys) != expected_keys
        or len(deleted_keys) != len(expected_keys)
        or cleanup.get("remaining_object_keys") != []
        or cleanup.get("verified_empty_prefix")
        != expected_cleanup.get("verify_empty_prefix")
        or cleanup.get("production_catalog_writes") is not False
    ):
        raise ValueError("isolated preview cleanup evidence is incomplete")
    finalize_slice = slice_completion.get("finalize_slice")
    if not isinstance(finalize_slice, dict):
        raise ValueError("slice completion omits finalize-slice phase evidence")
    completion_sha = finalize_slice.get("completion_sha256")
    executor.require_sha256(completion_sha, "slice completion sha256")
    report = {
        "schema": "overture-global-v2-worker-smoke-v1",
        "slice_version": request["slice_version"],
        "geocoder_build": request["geocoder_build"],
        "worker_name": expected_cleanup["worker_name"],
        "catalog_key": preview["catalog_key"],
        "health": health,
        "queries": query_summaries,
        "cleanup": cleanup,
        "catalog_published": False,
        "production_catalog_writes": False,
    }
    report_key = f"{contract['namespace']['immutable_root']}/worker-smoke/report.json"
    with canonical_temporary(report) as report_path:
        artifact = executor.normalize_artifact(
            executor.artifact_identity(report_path, report_key), contract
        )
        result = r2_verified_store.ensure_uploaded(store, report_path, report_key)
    if not result.get("readback_verified"):
        raise ValueError("worker smoke report failed remote readback")
    marker_key, completion, resumed = publish_control_completion(
        store, contract, runtime, phase="worker-smoke", artifacts=[artifact],
        dependency_evidence=[
            {"phase": "finalize-slice", "completion_sha256": completion_sha}
        ],
        details={
            "worker_deleted": True,
            "preview_prefix_empty": True,
            "production_catalog_writes": False,
            "places_feature_count": query_summaries["places"]["feature_count"],
            "address_feature_count": query_summaries["addresses"]["feature_count"],
        },
    )
    return {
        "report_key": report_key,
        "marker_key": marker_key,
        "completion": completion,
        "marker_written_last": True,
        "cleanup_verified": True,
        "production_catalog_writes": False,
        "resumed": resumed,
    }


def _store() -> r2_verified_store.S3Store:
    return executor._r2_store(None, None)  # noqa: SLF001


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish-task")
    admit = commands.add_parser("admit-task")
    restore = commands.add_parser("complete-phase")
    matrix = commands.add_parser("reducer-matrix")
    inventory = commands.add_parser("publish-inventory")
    publish_map = commands.add_parser("publish-map")
    restore_map = commands.add_parser("restore-map-planner-inputs")
    aggregate = commands.add_parser("build-publish-plans")
    restore_plans = commands.add_parser("restore-reducer-plans")
    run_reduce = commands.add_parser("run-publish-reduce")
    run_head = commands.add_parser("run-publish-head")
    final_inputs = commands.add_parser("restore-finalization-reports")
    preview = commands.add_parser("publish-preview")
    smoke = commands.add_parser("publish-worker-smoke-evidence")
    finalize_families = commands.add_parser("finalize-publish-families")
    for command in (publish, admit, restore):
        command.add_argument("--contract", type=Path, required=True)
        command.add_argument("--runtime", type=Path, required=True)
    admit.add_argument("--phase", choices=("map", "reduce", "head"), required=True)
    admit.add_argument("--family", choices=("addresses", "places"), required=True)
    admit.add_argument("--task-id", required=True)
    admit.add_argument("--index", type=int, required=True)
    admit.add_argument("--marker-only", action="store_true")
    admit.add_argument("--output", type=Path, required=True)
    publish.add_argument("--phase", choices=("map", "reduce", "head"), required=True)
    publish.add_argument("--family", choices=("addresses", "places"), required=True)
    publish.add_argument("--task-id", required=True)
    publish.add_argument("--index", type=int, required=True)
    publish.add_argument("--producer-report", type=Path, required=True)
    publish.add_argument("--producer-report-key", required=True)
    publish.add_argument("--outputs", type=Path, required=True)
    publish.add_argument("--counters", type=Path, required=True)
    publish.add_argument("--output", type=Path, required=True)
    restore.add_argument("--phase", choices=("map", "reduce", "head"), required=True)
    restore.add_argument("--expected-tasks", type=Path, required=True)
    restore.add_argument("--output-dir", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    matrix.add_argument("--address-plan", type=Path, required=True)
    matrix.add_argument("--places-plan", type=Path, required=True)
    matrix.add_argument("--output", type=Path, required=True)
    for argument in ("contract", "runtime", "request"):
        inventory.add_argument(f"--{argument}", type=Path, required=True)
    inventory.add_argument("--addresses", type=Path, required=True)
    inventory.add_argument("--places", type=Path, required=True)
    inventory.add_argument("--output", type=Path, required=True)
    for command in (publish_map, restore_map):
        command.add_argument("--contract", type=Path, required=True)
        command.add_argument("--runtime", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
    publish_map.add_argument("--request", type=Path, required=True)
    publish_map.add_argument("--inventory", type=Path, required=True)
    publish_map.add_argument("--family", choices=("addresses", "places"), required=True)
    publish_map.add_argument("--task-index", type=int, required=True)
    publish_map.add_argument("--producer-report", type=Path, required=True)
    publish_map.add_argument("--producer-output-root", type=Path, required=True)
    publish_map.add_argument("--scratch-dir", type=Path, required=True)
    restore_map.add_argument("--expected-tasks", type=Path, required=True)
    restore_map.add_argument("--output-root", type=Path, required=True)
    for command in (aggregate, restore_plans):
        command.add_argument("--contract", type=Path, required=True)
        command.add_argument("--runtime", type=Path, required=True)
        command.add_argument("--output", type=Path, required=True)
    aggregate.add_argument("--request", type=Path, required=True)
    aggregate.add_argument("--address-inventory", type=Path, required=True)
    aggregate.add_argument("--places-inventory", type=Path, required=True)
    aggregate.add_argument("--restored-map-root", type=Path, required=True)
    aggregate.add_argument("--plan-output-root", type=Path, required=True)
    aggregate.add_argument("--map-completion", type=Path, required=True)
    aggregate.add_argument("--build-number", type=int, required=True)
    aggregate.add_argument("--address-fragment-fetch-command-json", required=True)
    restore_plans.add_argument("--output-root", type=Path, required=True)
    for command in (run_reduce, run_head):
        command.add_argument("--contract", type=Path, required=True)
        command.add_argument("--runtime", type=Path, required=True)
        command.add_argument("--request", type=Path, required=True)
        command.add_argument("--places-plan", type=Path, required=True)
        command.add_argument("--work-root", type=Path, required=True)
        command.add_argument("--consumed-runner-minutes", type=int, default=0)
        command.add_argument("--output", type=Path, required=True)
    run_reduce.add_argument("--family", choices=("addresses", "places"), required=True)
    run_reduce.add_argument("--index", type=int, required=True)
    run_reduce.add_argument("--matrix-jobs", type=int, required=True)
    run_reduce.add_argument("--address-partition-plan", type=Path, required=True)
    run_reduce.add_argument("--address-reduce-plan", type=Path, required=True)
    run_head.add_argument("--reduce-completion", type=Path, required=True)
    run_head.add_argument("--reduce-marker-dir", type=Path, required=True)
    final_inputs.add_argument("--contract", type=Path, required=True)
    final_inputs.add_argument("--runtime", type=Path, required=True)
    final_inputs.add_argument("--expected-reduce-tasks", type=Path, required=True)
    final_inputs.add_argument("--expected-head-tasks", type=Path, required=True)
    final_inputs.add_argument("--output-root", type=Path, required=True)
    final_inputs.add_argument("--output", type=Path, required=True)
    preview.add_argument("--contract", type=Path, required=True)
    preview.add_argument("--request", type=Path, required=True)
    preview.add_argument("--legacy-manifest", type=Path, required=True)
    preview.add_argument("--address-manifest", type=Path, required=True)
    preview.add_argument("--places-manifest", type=Path, required=True)
    preview.add_argument("--slice-manifest", type=Path, required=True)
    preview.add_argument("--run-id", required=True)
    preview.add_argument("--generated-at", required=True)
    preview.add_argument("--output-root", type=Path, required=True)
    preview.add_argument("--output", type=Path, required=True)
    for argument in (
        "contract", "runtime", "request", "preview", "health", "places-query",
        "address-query",
        "smoke-requests",
        "cleanup", "slice-completion", "output",
    ):
        smoke.add_argument(f"--{argument}", type=Path, required=True)
    finalize_families.add_argument("--contract", type=Path, required=True)
    finalize_families.add_argument("--request", type=Path, required=True)
    finalize_families.add_argument("--address-partition-plan", type=Path, required=True)
    finalize_families.add_argument("--address-reduce-plan", type=Path, required=True)
    finalize_families.add_argument("--places-plan", type=Path, required=True)
    finalize_families.add_argument("--finalization-inputs", type=Path, required=True)
    finalize_families.add_argument("--work-root", type=Path, required=True)
    finalize_families.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "reducer-matrix":
        value = reducer_matrix(executor.read_json(args.address_plan), executor.read_json(args.places_plan))
    elif args.command == "publish-inventory":
        value = publish_inventory_phase(
            _store(), executor.read_json(args.contract), executor.read_json(args.runtime),
            executor.read_json(args.request), address_inventory_path=args.addresses,
            places_inventory_path=args.places,
        )
    elif args.command == "publish-map":
        contract, runtime = executor.read_json(args.contract), executor.read_json(args.runtime)
        request, inventory = executor.read_json(args.request), executor.read_json(args.inventory)
        tasks = inventory["plan" if args.family == "addresses" else "map_plan"]["tasks"]
        if not 0 <= args.task_index < len(tasks) or tasks[args.task_index]["index"] != args.task_index:
            raise ValueError("map task index differs from contiguous inventory matrix")
        task = tasks[args.task_index]
        prefix = f"{contract['namespace']['immutable_root']}/map/{args.family}"
        if args.family == "addresses":
            specs, counters = address_map_boundary(
                inventory, task, args.producer_report, args.producer_output_root,
                maximum_hash_bits=request["families"]["addresses"]["partition"]["maximum_hash_bits"],
                remote_object_prefix=prefix,
            )
        else:
            specs, counters = places_map_boundary(
                request, inventory, task, args.producer_report, args.producer_output_root,
                remote_object_prefix=prefix, scratch_dir=args.scratch_dir,
            )
        value = publish_task(
            _store(), contract, runtime, phase="map", family=args.family,
            task_id=f"{args.family}-map-{args.task_index:03d}", index=args.task_index,
            producer_report_path=args.producer_report,
            producer_report_key=f"{prefix}/reports/{args.task_index:03d}.json",
            outputs=specs, counters=counters,
        )
    elif args.command == "restore-map-planner-inputs":
        value = restore_map_planner_inputs(
            _store(), executor.read_json(args.contract), executor.read_json(args.runtime),
            expected_tasks=executor.read_json(args.expected_tasks), output_root=args.output_root,
        )
    elif args.command == "build-publish-plans":
        contract, runtime = executor.read_json(args.contract), executor.read_json(args.runtime)
        request_value = executor.read_json(args.request)
        store = _store()
        predecessor_manifests = restore_predecessor_manifests(
            store, request_value, args.plan_output_root / "predecessors"
        )
        predecessor_artifacts = restore_predecessor_plan_artifacts(
            store, request_value, predecessor_manifests,
            args.plan_output_root / "predecessor-artifacts",
        )
        address_path, places_path, specs = build_aggregate_plans(
            request_value, executor.read_json(args.address_inventory),
            executor.read_json(args.places_inventory), restored_root=args.restored_map_root,
            output_root=args.plan_output_root, build_number=args.build_number,
            address_fragment_fetch_command=json.loads(args.address_fragment_fetch_command_json),
            predecessor_manifests=predecessor_manifests,
            predecessor_planning_artifacts=predecessor_artifacts,
        )
        value = publish_aggregate_plans(
            store, contract, runtime, specs=specs,
            map_completion=executor.read_json(args.map_completion),
            output_prefix=f"{contract['namespace']['immutable_root']}/aggregate-plan",
        )
        value["matrix"] = reducer_matrix(executor.read_json(address_path), executor.read_json(places_path))
    elif args.command == "restore-reducer-plans":
        contract, runtime = executor.read_json(args.contract), executor.read_json(args.runtime)
        value = restore_reducer_plans(
            _store(), contract, runtime, output_root=args.output_root,
            output_prefix=f"{contract['namespace']['immutable_root']}/aggregate-plan",
        )
    elif args.command == "run-publish-reduce":
        contract, runtime, request_value = (
            executor.read_json(args.contract), executor.read_json(args.runtime),
            executor.read_json(args.request),
        )
        report, specs, counters, task_id = run_reduce_task(
            contract, runtime, family=args.family, index=args.index,
            matrix_jobs=args.matrix_jobs,
            consumed_runner_minutes=args.consumed_runner_minutes,
            request=request_value, address_partition_path=args.address_partition_plan,
            address_plan_path=args.address_reduce_plan,
            places_plan_path=args.places_plan, work_root=args.work_root,
        )
        value = publish_task(
            _store(), contract, runtime, phase="reduce", family=args.family,
            task_id=task_id, index=args.index, producer_report_path=report,
            producer_report_key=(
                f"{contract['namespace']['immutable_root']}/reduce/{args.family}/reports/{args.index:03d}.json"
            ), outputs=specs, counters=counters,
        )
    elif args.command == "run-publish-head":
        contract, runtime, request_value = (
            executor.read_json(args.contract), executor.read_json(args.runtime),
            executor.read_json(args.request),
        )
        store = _store()
        report, specs, counters, task_id = run_head_task(
            store, contract, runtime, request=request_value,
            places_plan_path=args.places_plan,
            reduce_completion=executor.read_json(args.reduce_completion),
            work_root=args.work_root, reduce_marker_dir=args.reduce_marker_dir,
            consumed_runner_minutes=args.consumed_runner_minutes,
        )
        value = publish_task(
            store, contract, runtime, phase="head", family="places",
            task_id=task_id, index=0, producer_report_path=report,
            producer_report_key=f"{contract['namespace']['immutable_root']}/head/places/report.json",
            outputs=specs, counters=counters,
        )
    elif args.command == "restore-finalization-reports":
        value = restore_finalization_reports(
            _store(), executor.read_json(args.contract), executor.read_json(args.runtime),
            expected_reduce_tasks=executor.read_json(args.expected_reduce_tasks),
            expected_head_tasks=executor.read_json(args.expected_head_tasks),
            output_root=args.output_root,
        )
    elif args.command == "publish-preview":
        value = publish_preview_catalog(
            _store(), executor.read_json(args.contract), executor.read_json(args.request),
            legacy_manifest_path=args.legacy_manifest,
            address_manifest_path=args.address_manifest,
            places_manifest_path=args.places_manifest,
            slice_manifest_path=args.slice_manifest, run_id=args.run_id,
            generated_at=args.generated_at, output_root=args.output_root,
        )
    elif args.command == "publish-worker-smoke-evidence":
        value = publish_worker_smoke_evidence(
            _store(), executor.read_json(args.contract), executor.read_json(args.runtime),
            executor.read_json(args.request), preview=executor.read_json(args.preview),
            health=executor.read_json(args.health),
            places_query=executor.read_json(args.places_query),
            address_query=executor.read_json(args.address_query),
            smoke_requests=executor.read_json(args.smoke_requests),
            cleanup=executor.read_json(args.cleanup),
            slice_completion=executor.read_json(args.slice_completion),
        )
    elif args.command == "finalize-publish-families":
        value = finalize_publish_families(
            _store(), executor.read_json(args.contract), executor.read_json(args.request),
            address_partition_path=args.address_partition_plan,
            address_reduce_path=args.address_reduce_plan,
            places_plan_path=args.places_plan,
            finalization_inputs=executor.read_json(args.finalization_inputs),
            work_root=args.work_root,
        )
    else:
        contract = executor.read_json(args.contract)
        runtime = executor.read_json(args.runtime)
        if args.command == "admit-task":
            value = admit_existing_task(
                _store(), contract, runtime, phase=args.phase, family=args.family,
                task_id=args.task_id, index=args.index,
                verify_artifacts=not args.marker_only,
            )
        elif args.command == "publish-task":
            value = publish_task(
                _store(), contract, runtime, phase=args.phase, family=args.family,
                task_id=args.task_id, index=args.index,
                producer_report_path=args.producer_report,
                producer_report_key=args.producer_report_key,
                outputs=executor.read_json(args.outputs), counters=executor.read_json(args.counters),
            )
        else:
            value = restore_exact_phase(
                _store(), contract, runtime, phase=args.phase,
                expected_tasks=executor.read_json(args.expected_tasks), output_dir=args.output_dir,
            )
    executor.write_json(args.output, value)
    print(json.dumps(value, sort_keys=True))


if __name__ == "__main__":
    main()
