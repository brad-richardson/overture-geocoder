from __future__ import annotations

import copy
import hashlib
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import global_build_manifest as gbm
import finalize_rebuild
import global_v2_build_request as request_module
import global_v2_executor as executor
import global_v2_hosted as hosted
import r2_verified_store


RELEASE = "2026-06-17.0"
SLICE = "slice-2026-07-19.7"
COMMIT = "a" * 40


def build_request() -> dict:
    return request_module.build_request(
        overture_release=RELEASE,
        geocoder_build="2026-07-19.7",
        slice_version=SLICE,
        legacy_core_version="2026-07-18.0",
        legacy_core_overture_release=RELEASE,
        legacy_core_manifest_key="2026-07-18.0/release-manifest.json",
        legacy_core_manifest_sha256="1" * 64,
        addresses_inventory_sha256="2" * 64,
        addresses_schema_fingerprint_sha256="3" * 64,
        addresses_predecessor_family_manifest_sha256=None,
        addresses_lineage_generation=1,
        places_inventory_sha256="4" * 64,
        places_schema_fingerprint_sha256="5" * 64,
        places_predecessor_family_manifest_sha256=None,
        places_lineage_generation=1,
        producer_commit=COMMIT,
    )


def build_contract(**overrides) -> dict:
    request = build_request()
    values = {
        "prepared_at": "2026-07-19T20:00:00Z",
        "max_parallel": 4,
        "max_total_runner_minutes": 50_000,
        "max_estimated_cost_usd": 1_000,
        "runner_image_os": "ubuntu24",
        "runner_image_version": "20260713.1.0",
    }
    values.update(overrides)
    return executor.build_contract(request, "f" * 64, **values)


def runtime(version: str = "20260713.1.0") -> dict:
    return executor.build_runtime_fingerprint(
        runner_image_os="ubuntu24",
        runner_image_version=version,
        python_version=executor.PYTHON_VERSION,
        numpy_version=executor.NUMPY_VERSION,
        pyarrow_version=executor.PYARROW_VERSION,
        parquet_cpp_version=executor.PARQUET_CPP_VERSION,
    )


def artifact(key: str, body: bytes = b"artifact") -> dict:
    return {
        "object_key": key,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def test_contract_is_non_promoting_immutable_and_reproducible():
    request = build_request()
    contract = build_contract()

    assert executor.validate_contract(contract, request) == contract
    assert contract["scope"]["rebuild_legacy_core"] is False
    assert contract["scope"]["rebuild_divisions"] is False
    assert contract["scope"]["promote_catalog"] is False
    assert contract["namespace"]["forbidden_exact_keys"] == [
        "catalog.json",
        "v2/catalog.json",
    ]
    assert contract["namespace"]["forbidden_prefixes"] == ["2026-07-18.0/"]
    assert contract["runtime"]["image"] == {
        "label": executor.EXECUTOR_IMAGE,
        "os": "ubuntu24",
        "version": "20260713.1.0",
    }
    assert [item["name"] for item in contract["phases"]] == list(executor.PHASES)
    assert all(item["completion_marker_written_last"] for item in contract["phases"])
    assert contract["adapters"]["preview"]["shared_v2_namespace_writes"] is False


def test_contract_rejects_drift_and_cost_concurrency_escape():
    request = build_request()
    changed = copy.deepcopy(build_contract())
    changed["namespace"]["forbidden_exact_keys"].remove("v2/catalog.json")
    with pytest.raises(ValueError, match="differs from its deterministic"):
        executor.validate_contract(changed, request)

    with pytest.raises(ValueError, match="max_parallel"):
        build_contract(max_parallel=9)


def test_typed_confirmation_binds_request_and_complete_budget_contract():
    digest = "d" * 64
    limits = {
        "max_parallel": 4,
        "max_total_runner_minutes": 50_000,
        "max_estimated_cost_usd": 1_000,
        "prior_runner_minutes": 123,
    }
    assert executor.confirmation_phrase("execute", SLICE, digest, **limits) == (
        f"EXECUTE_GLOBAL_V2_FAMILIES_ONLY::{SLICE}::{digest}"
        "::MAX_PARALLEL=4::MAX_TOTAL_RUNNER_MINUTES=50000"
        "::MAX_ESTIMATED_COST_USD=1000::PRIOR_RUNNER_MINUTES=123"
    )
    assert executor.confirmation_phrase(
        "dry-run", SLICE, digest, **limits
    ).startswith("DRY_RUN_")
    with pytest.raises(ValueError):
        executor.confirmation_phrase("publish", SLICE, digest, **limits)
    with pytest.raises(ValueError, match="prior_runner_minutes"):
        executor.confirmation_phrase(
            "execute", SLICE, digest, **{**limits, "prior_runner_minutes": -1}
        )


def test_execute_rerun_requires_fresh_dispatch_with_explicit_prior_minutes():
    assert executor.validate_dispatch_attempt("execute", 1) == 1
    assert executor.validate_dispatch_attempt("dry-run", 2) == 2
    with pytest.raises(ValueError, match="fresh workflow_dispatch"):
        executor.validate_dispatch_attempt("execute", 2)


def test_read_only_core_inspection_freezes_manifest_identity(tmp_path, monkeypatch):
    manifest = {
        "families": {
            "forward": {"shard_count": 262},
            "reverse": {"shard_count": 253},
            "id": {"shard_count": 4096},
        }
    }
    path = tmp_path / "release-manifest.json"
    path.write_bytes(executor.canonical_json(manifest))
    monkeypatch.setattr(
        executor.v2_release_manifest,
        "_validate_legacy_release",
        lambda value, release: {"version": "2026-07-18.0"},
    )

    evidence = executor.inspect_legacy_core(
        path,
        expected_version="2026-07-18.0",
        expected_overture_release=RELEASE,
    )
    assert evidence["manifest_sha256"] == executor.sha256_file(path)
    assert evidence["manifest_key"] == "2026-07-18.0/release-manifest.json"
    assert evidence["read_only"] is True
    assert evidence["id_shards"] == 4096


def test_runtime_fingerprint_is_exact_and_stable():
    value = runtime()
    assert executor.validate_runtime(value) == value
    assert value["toolchain"]["parquet_format"] == "2.6"
    assert value["toolchain"]["parquet_cpp"] == executor.PARQUET_CPP_VERSION

    with pytest.raises(ValueError, match="runtime differs"):
        executor.build_runtime_fingerprint(
            runner_image_os="ubuntu24",
            runner_image_version="20260713.1.0",
            python_version=executor.PYTHON_VERSION,
            numpy_version=executor.NUMPY_VERSION,
            pyarrow_version="moving-latest",
            parquet_cpp_version=executor.PARQUET_CPP_VERSION,
        )


def test_runtime_records_staged_image_version_without_breaking_resume():
    contract = build_contract()
    later_image = executor.build_runtime_fingerprint(
        runner_image_os="ubuntu24",
        runner_image_version="20260720.1.0",
        python_version=executor.PYTHON_VERSION,
        numpy_version=executor.NUMPY_VERSION,
        pyarrow_version=executor.PYARROW_VERSION,
        parquet_cpp_version=executor.PARQUET_CPP_VERSION,
    )
    assert executor.validate_runtime_for_contract(later_image, contract) == later_image

    wrong_os = executor.build_runtime_fingerprint(
        runner_image_os="ubuntu26",
        runner_image_version="20260720.1.0",
        python_version=executor.PYTHON_VERSION,
        numpy_version=executor.NUMPY_VERSION,
        pyarrow_version=executor.PYARROW_VERSION,
        parquet_cpp_version=executor.PARQUET_CPP_VERSION,
    )
    with pytest.raises(ValueError, match="image OS"):
        executor.validate_runtime_for_contract(wrong_os, contract)

    root = contract["namespace"]["immutable_root"]
    task = executor.build_task_completion(
        contract,
        later_image,
        phase="map",
        family="places",
        task_id="places-map-000",
        index=0,
        producer_report=artifact(f"{root}/later/report.json"),
        artifacts=[artifact(f"{root}/later/fragment.bin")],
        counters={
            "input_records": 1,
            "retained_records": 1,
            "rejected_records": 0,
            "output_records": 1,
        },
    )
    completed = executor.build_phase_completion(
        contract,
        runtime(),
        phase="map",
        expected_tasks=[
            {"family": "places", "task_id": "places-map-000", "index": 0}
        ],
        task_completions=[task],
    )
    assert task["runtime"]["toolchain"]["image"]["version"] == "20260720.1.0"
    assert completed["runtime_fingerprint_sha256"] == runtime()["fingerprint_sha256"]


def task_completion(index: int, *, family: str = "places") -> dict:
    contract = build_contract()
    root = contract["namespace"]["immutable_root"]
    return executor.build_task_completion(
        contract,
        runtime(),
        phase="map",
        family=family,
        task_id=f"{family}-map-{index:03d}",
        index=index,
        producer_report=artifact(f"{root}/map/{family}/{index}/report.json", b"report"),
        artifacts=[artifact(f"{root}/map/{family}/{index}/fragment.bin")],
        counters={
            "input_records": 10,
            "retained_records": 8,
            "rejected_records": 2,
            "output_records": 8,
        },
    )


def test_phase_completion_requires_exact_unique_task_coverage():
    contract = build_contract()
    expected = [
        {"family": "places", "task_id": "places-map-000", "index": 0},
        {"family": "places", "task_id": "places-map-001", "index": 1},
    ]
    completed = executor.build_phase_completion(
        contract,
        runtime(),
        phase="map",
        expected_tasks=expected,
        task_completions=[task_completion(1), task_completion(0)],
    )
    assert completed["counters"] == {
        "input_records": 20,
        "retained_records": 16,
        "rejected_records": 4,
        "output_records": 16,
    }
    assert completed["artifact_totals"]["objects"] == 4

    with pytest.raises(ValueError, match="exactly cover"):
        executor.build_phase_completion(
            contract,
            runtime(),
            phase="map",
            expected_tasks=expected,
            task_completions=[task_completion(0)],
        )


def test_hosted_task_uploads_complete_set_then_marker_and_restores_exact_phase(tmp_path):
    class ListingStore(r2_verified_store.FilesystemStore):
        def __init__(self, root):
            super().__init__(root)
            self.uploaded = []

        def upload(self, source, key, sha256):
            super().upload(source, key, sha256)
            self.uploaded.append(key)

        def list_prefix(self, prefix):
            return sorted(
                path.relative_to(self.root).as_posix()
                for path in self.root.rglob("*")
                if path.is_file()
                and not path.name.endswith(".metadata.json")
                and path.relative_to(self.root).as_posix().startswith(prefix)
            )

    contract = build_contract()
    runtime_value = runtime()
    root = contract["namespace"]["immutable_root"]
    report = tmp_path / "report.json"
    fragment = tmp_path / "fragment.bin"
    report.write_bytes(b"report")
    fragment.write_bytes(b"fragment")
    store = ListingStore(tmp_path / "r2")
    result = hosted.publish_task(
        store,
        contract,
        runtime_value,
        phase="map",
        family="places",
        task_id="places-map-000",
        index=0,
        producer_report_path=report,
        producer_report_key=f"{root}/map/places/000/report.json",
        outputs=[
            {
                "path": str(fragment),
                "object_key": f"{root}/map/places/000/fragment.bin",
            }
        ],
        counters={
            "input_records": 10,
            "retained_records": 8,
            "rejected_records": 2,
            "output_records": 8,
        },
    )
    assert store.uploaded[-1] == result["marker_key"]
    restored = hosted.restore_exact_phase(
        store,
        contract,
        runtime_value,
        phase="map",
        expected_tasks=[
            {"family": "places", "task_id": "places-map-000", "index": 0}
        ],
        output_dir=tmp_path / "restored",
    )
    assert store.uploaded[-1] == restored["marker_key"]
    assert restored["completion"]["expected_tasks"][0]["index"] == 0

    extra = store.root / hosted.task_marker_key(contract, "map", "addresses", 0)
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b"stray")
    with pytest.raises(ValueError, match="completion set differs"):
        hosted.restore_exact_phase(
            store,
            contract,
            runtime_value,
            phase="map",
            expected_tasks=[
                {"family": "places", "task_id": "places-map-000", "index": 0}
            ],
            output_dir=tmp_path / "retry",
        )


def test_resume_admits_embedded_runtime_and_rejects_corrupt_artifact(tmp_path):
    contract = build_contract()
    first_runtime = runtime("20260713.1.0")
    later_runtime = runtime("20260720.1.0")
    root = contract["namespace"]["immutable_root"]
    report, fragment = tmp_path / "report", tmp_path / "fragment"
    report.write_bytes(b"report")
    fragment.write_bytes(b"fragment")
    store = r2_verified_store.FilesystemStore(tmp_path / "r2")
    hosted.publish_task(
        store, contract, first_runtime, phase="map", family="places",
        task_id="places-map-000", index=0, producer_report_path=report,
        producer_report_key=f"{root}/map/places/report.json",
        outputs=[{
            "path": str(fragment),
            "object_key": f"{root}/map/places/fragment.bin",
        }],
        counters={
            "input_records": 1, "retained_records": 1,
            "rejected_records": 0, "output_records": 1,
        },
    )
    admitted = hosted.admit_existing_task(
        store, contract, later_runtime, phase="map", family="places",
        task_id="places-map-000", index=0,
    )
    assert admitted["completed"] is True
    assert admitted["completion"]["runtime"] == first_runtime
    first_phase = hosted.restore_exact_phase(
        store, contract, first_runtime, phase="map",
        expected_tasks=[{
            "family": "places", "task_id": "places-map-000", "index": 0,
        }], output_dir=tmp_path / "first",
    )
    retried = hosted.restore_exact_phase(
        store, contract, later_runtime, phase="map",
        expected_tasks=[{
            "family": "places", "task_id": "places-map-000", "index": 0,
        }], output_dir=tmp_path / "retry",
    )
    assert retried["resumed"] is True
    assert retried["completion"] == first_phase["completion"]

    fragment_key = f"{root}/map/places/fragment.bin"
    stored_fragment = store._path(fragment_key)  # noqa: SLF001
    stored_fragment.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="artifact identity differs"):
        hosted.admit_existing_task(
            store, contract, later_runtime, phase="map", family="places",
            task_id="places-map-000", index=0,
        )


def test_resume_rejects_task_identity_mismatch(tmp_path):
    contract, runtime_value = build_contract(), runtime()
    root = contract["namespace"]["immutable_root"]
    report, fragment = tmp_path / "report", tmp_path / "fragment"
    report.write_bytes(b"report")
    fragment.write_bytes(b"fragment")
    store = r2_verified_store.FilesystemStore(tmp_path / "r2")
    hosted.publish_task(
        store, contract, runtime_value, phase="map", family="places",
        task_id="places-map-000", index=0, producer_report_path=report,
        producer_report_key=f"{root}/map/places/report.json",
        outputs=[{"path": str(fragment), "object_key": f"{root}/map/places/x"}],
        counters={
            "input_records": 1, "retained_records": 1,
            "rejected_records": 0, "output_records": 1,
        },
    )
    with pytest.raises(ValueError, match="another planned task"):
        hosted.admit_existing_task(
            store, contract, runtime_value, phase="map", family="places",
            task_id="places-map-wrong", index=0,
        )


def test_hosted_reducer_matrix_is_dynamic_contiguous_and_typed():
    assert hosted.reducer_matrix(
        {"jobs": [
            {"index": 0, "id": "address-reduce-job-000"},
            {"index": 1, "id": "address-reduce-job-001"},
        ]},
        {"reduce_jobs": [{"index": 0}]},
    ) == {
        "include": [
            {"family": "addresses", "index": 0, "task_id": "address-reduce-job-000"},
            {"family": "addresses", "index": 1, "task_id": "address-reduce-job-001"},
            {"family": "places", "index": 0, "task_id": "places-reduce-000"},
        ]
    }
    with pytest.raises(ValueError, match="0..N-1"):
        hosted.reducer_matrix(
            {"jobs": [{"index": 1, "id": "address-reduce-job-001"}]},
            {"reduce_jobs": [{"index": 0}]},
        )

    too_many_addresses = [
        {"index": index, "id": f"address-reduce-job-{index:03d}"}
        for index in range(256)
    ]
    with pytest.raises(ValueError, match="combined reducer matrix"):
        hosted.reducer_matrix(
            {"jobs": too_many_addresses}, {"reduce_jobs": [{"index": 0}]}
        )


def test_inventory_phase_uploads_both_inventories_then_signed_marker(tmp_path, monkeypatch):
    class Store(r2_verified_store.FilesystemStore):
        def __init__(self, root):
            super().__init__(root)
            self.keys = []

        def upload(self, source, key, sha256):
            super().upload(source, key, sha256)
            self.keys.append(key)

    address = {"plan": {"tasks": [{"index": 0}]}}
    places = {"map_plan": {"tasks": [{"index": 0}, {"index": 1}]}}
    address_path, places_path = tmp_path / "address.json", tmp_path / "places.json"
    address_path.write_bytes(executor.canonical_json(address))
    places_path.write_bytes(executor.canonical_json(places))
    monkeypatch.setattr(executor, "validate_address_inventory", lambda value, request: value)
    monkeypatch.setattr(executor, "validate_places_inventory", lambda value, request: value)
    contract = build_contract()
    store = Store(tmp_path / "store")
    result = hosted.publish_inventory_phase(
        store, contract, runtime(), build_request(),
        address_inventory_path=address_path, places_inventory_path=places_path,
    )
    assert store.keys == [
        f"{contract['namespace']['immutable_root']}/inventory/addresses.json",
        f"{contract['namespace']['immutable_root']}/inventory/places.json",
        f"{contract['namespace']['phase_completion_root']}/inventory.json",
    ]
    assert result["marker_written_last"] is True
    assert result["completion"]["details"]["addresses_map_tasks"] == 1
    assert result["completion"]["details"]["places_map_tasks"] == 2


def test_hosted_address_map_boundary_derives_outputs_from_validated_report(tmp_path, monkeypatch):
    manifest = tmp_path / "fragment-manifest.json"
    fragment = tmp_path / "fragment.bin"
    manifest.write_bytes(b"manifest")
    fragment.write_bytes(b"fragment")
    completion = {"accounting": {"input_rows": 10, "retained_rows": 8, "rejected_rows": 2}}
    monkeypatch.setattr(hosted.address_plan, "_validate_map_task", lambda *args, **kwargs: (
        completion, {"relative_path": manifest.name},
        [{"source_path": fragment, "object_key": "map/address-fragments/x.bin"}],
    ))
    specs, counters = hosted.address_map_boundary(
        {}, {"index": 3}, tmp_path / "report.json", tmp_path,
        maximum_hash_bits=12, remote_object_prefix="staging/global-v2/x/immutable/map/addresses",
    )
    assert [item["path"] for item in specs] == [str(manifest), str(fragment)]
    assert specs[1]["object_key"].endswith("/objects/map/address-fragments/x.bin")
    assert counters == {"input_records": 10, "retained_records": 8, "rejected_records": 2, "output_records": 8}


def test_hosted_places_map_boundary_uses_family_validator(tmp_path, monkeypatch):
    counts = tmp_path / "counts/x.gz"
    fragment = tmp_path / "fragments/x.parquet"
    counts.parent.mkdir()
    fragment.parent.mkdir()
    counts.write_bytes(b"counts")
    fragment.write_bytes(b"fragment")
    report = {
        "accounting": {"input_records": 5, "retained_records": 4, "rejected_records": 1},
        "counts": {"object_key": "counts/x.gz", "bytes": 6, "sha256": "a" * 64},
        "fragments": {"objects": [{"object_key": "fragments/x.parquet", "bytes": 8, "sha256": "b" * 64}]},
    }
    report_path = tmp_path / "report.json"
    report_path.write_text(executor.canonical_json(report).decode())
    observed = {}
    monkeypatch.setattr(hosted.places_plan, "_validate_map_report", lambda raw, **kwargs: observed.setdefault("raw", raw))
    specs, counters = hosted.places_map_boundary(
        {}, {}, {}, report_path, tmp_path,
        remote_object_prefix="staging/global-v2/x/immutable/map/places",
        scratch_dir=tmp_path / "scratch",
    )
    assert observed["raw"] == report
    assert [item["path"] for item in specs] == [str(counts), str(fragment)]
    assert counters["output_records"] == 4


def test_restore_map_planner_inputs_fetches_only_report_and_places_counts(tmp_path):
    class Store(r2_verified_store.FilesystemStore):
        def list_prefix(self, prefix):
            return sorted(
                path.relative_to(self.root).as_posix() for path in self.root.rglob("*")
                if path.is_file() and not path.name.endswith(".metadata.json")
                and path.relative_to(self.root).as_posix().startswith(prefix)
            )

    contract, runtime_value = build_contract(), runtime()
    prefix = f"{contract['namespace']['immutable_root']}/map/places"
    report, counts, fragment = (tmp_path / name for name in ("report", "counts", "fragment"))
    report.write_bytes(b"report")
    counts.write_bytes(b"counts")
    fragment.write_bytes(b"fragment")
    store = Store(tmp_path / "store")
    hosted.publish_task(
        store, contract, runtime_value, phase="map", family="places",
        task_id="places-map-000", index=0, producer_report_path=report,
        producer_report_key=f"{prefix}/reports/000.json",
        outputs=[
            {"path": str(counts), "object_key": f"{prefix}/objects/counts/x.gz"},
            {"path": str(fragment), "object_key": f"{prefix}/objects/fragments/x.parquet"},
        ],
        counters={"input_records": 1, "retained_records": 1, "rejected_records": 0, "output_records": 1},
    )
    restored = hosted.restore_map_planner_inputs(
        store, contract, runtime_value,
        expected_tasks=[{"family": "places", "task_id": "places-map-000", "index": 0}],
        output_root=tmp_path / "planner",
    )
    assert Path(restored["tasks"][0]["report"]).read_bytes() == b"report"
    assert Path(restored["tasks"][0]["planner_artifact"]).read_bytes() == b"counts"
    assert not (tmp_path / "planner/places/artifacts/fragments/x.parquet").exists()
    stray = store.root / f"{prefix}/objects/fragments/stray.parquet"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"stray")
    with pytest.raises(ValueError, match="map output set differs"):
        hosted.restore_map_planner_inputs(
            store, contract, runtime_value,
            expected_tasks=[{"family": "places", "task_id": "places-map-000", "index": 0}],
            output_root=tmp_path / "planner-retry",
        )


def test_aggregate_plan_boundary_requires_exact_predecessor_alias():
    request = build_request()
    hosted.require_build1_lineage(request)
    changed = copy.deepcopy(request)
    changed["families"]["places"]["predecessor_family_manifest_sha256"] = "9" * 64
    changed["families"]["places"]["partition"]["lineage_generation"] = 2
    changed["families"]["places"]["global_head"]["provenance"][
        "predecessor_family_manifest_sha256"
    ] = "9" * 64
    changed["families"]["places"]["predecessor_family_manifest"] = {
        "object_key": "slice-2026-07-18.0/families/places/family-manifest.json",
        "bytes": 42,
        "sha256": "9" * 64,
    }
    changed["families"]["places"]["global_head"]["provenance"][
        "predecessor_family_manifest"
    ] = changed["families"]["places"]["predecessor_family_manifest"]
    hosted.require_build1_lineage(changed)
    changed["families"]["places"]["predecessor_family_manifest"]["sha256"] = "8" * 64
    with pytest.raises(ValueError):
        hosted.require_build1_lineage(changed)


def test_publish_and_restore_exact_dynamic_reducer_plans(tmp_path):
    class Store(r2_verified_store.FilesystemStore):
        def list_prefix(self, prefix):
            return sorted(
                path.relative_to(self.root).as_posix() for path in self.root.rglob("*")
                if path.is_file() and not path.name.endswith(".metadata.json")
                and path.relative_to(self.root).as_posix().startswith(prefix)
            )

    contract, runtime_value = build_contract(), runtime()
    root = tmp_path / "plans"
    address = root / "addresses/families/addresses/reduce-plan.json"
    partition = root / "addresses/families/addresses/partition-plan.json"
    places = root / "places/plan.json"
    address.parent.mkdir(parents=True)
    places.parent.mkdir(parents=True)
    address.write_bytes(executor.canonical_json({"jobs": [
        {"index": 0, "id": "address-reduce-job-000"},
        {"index": 1, "id": "address-reduce-job-001"},
    ]}))
    partition.write_bytes(executor.canonical_json({"leaves": []}))
    places.write_bytes(executor.canonical_json({"reduce_jobs": [{"index": 0}]}))
    specs = [
        {"path": str(path), "relative": path.relative_to(root).as_posix()}
        for path in (address, partition, places)
    ]
    store = Store(tmp_path / "store")
    prefix = f"{contract['namespace']['immutable_root']}/aggregate-plan"
    published = hosted.publish_aggregate_plans(
        store, contract, runtime_value, specs=specs,
        map_completion={"completion_sha256": "8" * 64}, output_prefix=prefix,
    )
    restored = hosted.restore_reducer_plans(
        store, contract, runtime_value, output_root=tmp_path / "restored",
        output_prefix=prefix,
    )
    assert published["marker_written_last"] is True
    assert len(restored["matrix"]["include"]) == 3


def test_predecessor_restore_uses_request_and_manifest_bound_identities(tmp_path, monkeypatch):
    request = build_request()
    manifest_value = {
        "family": "places",
        "lineage": {"overture_release": "2026-05-20.0"},
        "region": {
            "name": "global", "bbox": [-180.0, -90.0, 180.0, 90.0],
            "bbox_scope": "exact",
        },
        "versions": {
            "format": request["families"]["places"]["versions"]["format"],
            "tokenizer": "prior-tokenizer-version",
            "normalization": None,
        },
        "artifacts": [{
            "object_key": "families/places/catalog.pcat", "bytes": 7,
            "sha256": hashlib.sha256(b"catalog").hexdigest(),
        }],
    }
    manifest_bytes = executor.canonical_json(manifest_value)
    identity = {
        "object_key": "slice-2026-07-18.0/families/places/family-manifest.json",
        "bytes": len(manifest_bytes), "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    request["families"]["places"]["predecessor_family_manifest"] = identity
    request["families"]["places"]["partition"]["lineage_generation"] = 2
    request["families"]["places"]["predecessor_family_manifest_sha256"] = identity["sha256"]
    request["families"]["places"]["global_head"]["provenance"]["predecessor_family_manifest"] = identity
    request["families"]["places"]["global_head"]["provenance"]["predecessor_family_manifest_sha256"] = identity["sha256"]
    store = r2_verified_store.FilesystemStore(tmp_path / "store")
    manifest_path = tmp_path / "manifest.json"
    catalog_path = tmp_path / "catalog.pcat"
    manifest_path.write_bytes(manifest_bytes)
    catalog_path.write_bytes(b"catalog")
    store.upload(manifest_path, identity["object_key"], identity["sha256"])
    store.upload(catalog_path, "slice-2026-07-18.0/families/places/catalog.pcat", hashlib.sha256(b"catalog").hexdigest())
    monkeypatch.setattr(hosted.family_manifest, "validate_family_manifest", lambda value: value)
    manifests = hosted.restore_predecessor_manifests(store, request, tmp_path / "manifests")
    artifacts = hosted.restore_predecessor_plan_artifacts(store, request, manifests, tmp_path / "artifacts")
    assert artifacts["addresses"] is None
    assert artifacts["places"].read_bytes() == b"catalog"


def test_hosted_address_reduce_derives_serving_outputs_and_uses_safe_fetch(tmp_path, monkeypatch):
    contract, runtime_value, request = build_contract(), runtime(), build_request()
    reduce_plan = tmp_path / "address-reduce.json"
    partition_plan = tmp_path / "address-partition.json"
    places_plan = tmp_path / "places-plan.json"
    reduce_plan.write_bytes(executor.canonical_json({"jobs": [{"index": 0, "id": "address-reduce-job-000"}]}))
    partition_plan.write_bytes(b"{}\n")
    places_plan.write_bytes(b"{}\n")
    observed = {}

    def run_job(*args, **kwargs):
        observed["fetch"] = kwargs["fragment_fetch_command"]
        root = kwargs["output_root"]
        for relative, body in (
            ("families/addresses/shards/a.aidx", b"index"),
            ("families/addresses/shards/a.adat", b"data"),
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        report = {
            "artifacts": [{
                "index": {"relative_path": "families/addresses/shards/a.aidx"},
                "data": {"relative_path": "families/addresses/shards/a.adat"},
            }],
            "accounting": {"expected_rows": 5, "output_rows": 5},
        }
        path = root / "families/addresses/reduce-completions/address-reduce-job-000.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(executor.canonical_json(report))
        return report

    monkeypatch.setattr(hosted.address_reduce, "run_job", run_job)
    monkeypatch.setenv("R2_ENDPOINT", "https://example.invalid")
    report, specs, counters, task_id = hosted.run_reduce_task(
        contract, runtime_value, family="addresses", index=0, matrix_jobs=2,
        consumed_runner_minutes=0, request=request,
        address_partition_path=partition_plan, address_plan_path=reduce_plan,
        places_plan_path=places_plan, work_root=tmp_path / "work",
    )
    assert report.is_file() and task_id == "address-reduce-job-000"
    assert {item["object_key"] for item in specs} == {
        f"{SLICE}/families/addresses/shards/a.aidx",
        f"{SLICE}/families/addresses/shards/a.adat",
    }
    assert counters["output_records"] == 5
    assert observed["fetch"][1].endswith("r2_fragment_fetch.py")
    assert observed["fetch"].count("{output}") == 1


def test_hosted_head_requires_reduce_completion_and_derives_head(tmp_path, monkeypatch):
    contract, runtime_value, request = build_contract(), runtime(), build_request()
    plan = tmp_path / "places-plan.json"
    plan.write_bytes(b"{}\n")
    unsigned = {
        "schema": executor.PHASE_COMPLETION_SCHEMA,
        "request_sha256": contract["request"]["sha256"],
        "phase": "reduce",
    }
    reduce_completion = {
        **unsigned, "completion_sha256": executor.sha256_bytes(executor.canonical_json(unsigned))
    }

    def build_head(*args, **kwargs):
        kwargs["output"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output"].write_bytes(b"head")
        return {"accounting": {"retained_records": 9}}

    monkeypatch.setattr(hosted.places_head, "build_global_head", build_head)
    monkeypatch.setattr(hosted.places_head, "validate_head_report", lambda value, *args: value)
    monkeypatch.setenv("R2_ENDPOINT", "https://example.invalid")
    report, specs, counters, task_id = hosted.run_head_task(
        contract, runtime_value, request=request, places_plan_path=plan,
        reduce_completion=reduce_completion, work_root=tmp_path / "head",
        consumed_runner_minutes=0,
    )
    assert report.is_file() and task_id == "places-head-000"
    assert specs[0]["object_key"] == f"{SLICE}/families/places/head.phrp"
    assert counters["retained_records"] == 9


def test_hosted_places_reduce_validates_report_and_streams_plan_fragments(tmp_path, monkeypatch):
    contract, runtime_value, request = build_contract(), runtime(), build_request()
    places_plan = tmp_path / "places-plan.json"
    places_plan.write_bytes(executor.canonical_json({"reduce_jobs": [{"index": 0}]}))
    dummy = tmp_path / "dummy.json"
    dummy.write_bytes(b"{}\n")
    observed = {}

    def execute(plan, **kwargs):
        observed["fetch"] = kwargs["fragment_fetch_command"]
        output = kwargs["output_dir"] / "q-0.pcsh"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"shard")
        return {
            "accounting": {"input_fragment_records": 7, "output_records": 7},
            "shards": [{"object": "q-0.pcsh"}],
        }

    monkeypatch.setattr(hosted.places_reduce, "execute_reduce_job", execute)
    monkeypatch.setattr(hosted.places_reduce, "validate_reduce_report", lambda value, plan: value)
    monkeypatch.setenv("R2_ENDPOINT", "https://example.invalid")
    report, specs, counters, task_id = hosted.run_reduce_task(
        contract, runtime_value, family="places", index=0, matrix_jobs=1,
        consumed_runner_minutes=0, request=request,
        address_partition_path=dummy, address_plan_path=dummy,
        places_plan_path=places_plan, work_root=tmp_path / "work-places",
    )
    assert report.is_file() and task_id == "places-reduce-000"
    assert specs[0]["object_key"] == f"{SLICE}/families/places/q-0.pcsh"
    assert counters["input_records"] == 7
    assert observed["fetch"][1].endswith("r2_fragment_fetch.py")


def test_finalization_fanin_restores_only_exact_reports_and_heads_serving_set(tmp_path):
    class Store(r2_verified_store.FilesystemStore):
        def list_prefix(self, prefix):
            return sorted(
                path.relative_to(self.root).as_posix() for path in self.root.rglob("*")
                if path.is_file() and not path.name.endswith(".metadata.json")
                and path.relative_to(self.root).as_posix().startswith(prefix)
            )

    contract, runtime_value = build_contract(), runtime()
    store = Store(tmp_path / "store")
    for phase, name, serving_key in (
        ("reduce", "reduce", f"{SLICE}/families/places/q-0.pcsh"),
        ("head", "head", f"{SLICE}/families/places/head.phrp"),
    ):
        report, serving = tmp_path / f"{name}-report", tmp_path / name
        report.write_bytes(name.encode())
        serving.write_bytes((name + "-serving").encode())
        hosted.publish_task(
            store, contract, runtime_value, phase=phase, family="places",
            task_id=f"places-{name}-000", index=0,
            producer_report_path=report,
            producer_report_key=f"{contract['namespace']['immutable_root']}/{phase}/places/report.json",
            outputs=[{"path": str(serving), "object_key": serving_key}],
            counters={"input_records": 1, "retained_records": 1, "rejected_records": 0, "output_records": 1},
        )
    result = hosted.restore_finalization_reports(
        store, contract, runtime_value,
        expected_reduce_tasks=[{"family": "places", "task_id": "places-reduce-000", "index": 0}],
        expected_head_tasks=[{"family": "places", "task_id": "places-head-000", "index": 0}],
        output_root=tmp_path / "final-inputs",
    )
    assert len(result["reports"]) == 2
    assert not (tmp_path / "final-inputs/families/places/q-0.pcsh").exists()


def test_preview_publish_is_isolated_create_only_and_catalog_last(tmp_path, monkeypatch):
    class Store(r2_verified_store.FilesystemStore):
        def __init__(self, root):
            super().__init__(root)
            self.keys = []

        def upload(self, source, key, sha256):
            super().upload(source, key, sha256)
            self.keys.append(key)

    paths = {}
    for name in ("legacy", "addresses", "places", "slice"):
        path = tmp_path / f"{name}.json"
        path.write_bytes(executor.canonical_json({"name": name}))
        paths[name] = path
    monkeypatch.setattr(hosted.v2_release_manifest, "build_release_manifest", lambda **kwargs: {"release": True})
    monkeypatch.setattr(hosted.v2_release_manifest, "build_catalog", lambda **kwargs: {"catalog": True})
    store = Store(tmp_path / "store")
    value = hosted.publish_preview_catalog(
        store, build_contract(), build_request(), legacy_manifest_path=paths["legacy"],
        address_manifest_path=paths["addresses"], places_manifest_path=paths["places"],
        slice_manifest_path=paths["slice"], run_id="run-123-1",
        generated_at="2026-07-19T00:00:00Z", output_root=tmp_path / "preview",
    )
    assert store.keys == [
        "smoketest-v2/run-123-1/release.json",
        "smoketest-v2/run-123-1/catalog.json",
    ]
    assert value["catalog_written_last"] is True
    assert value["production_catalog_writes"] is False
    assert value["cleanup"]["object_keys"] == [
        "smoketest-v2/run-123-1/catalog.json",
        "smoketest-v2/run-123-1/release.json",
    ]


def test_worker_smoke_requires_query_and_exact_cleanup_then_marks_last(tmp_path):
    class Store(r2_verified_store.FilesystemStore):
        def __init__(self, root):
            super().__init__(root)
            self.keys = []

        def upload(self, source, key, sha256):
            super().upload(source, key, sha256)
            self.keys.append(key)

    contract = build_contract()
    request = build_request()
    runtime_value = runtime()
    run_id = "12345-2"
    preview = {
        "catalog_key": f"smoketest-v2/{run_id}/catalog.json",
        "release_key": f"smoketest-v2/{run_id}/release.json",
        "production_catalog_writes": False,
        "cleanup": {
            "worker_name": f"geocoder-global-v2-{run_id}",
            "object_keys": [
                f"smoketest-v2/{run_id}/catalog.json",
                f"smoketest-v2/{run_id}/release.json",
            ],
            "verify_empty_prefix": f"smoketest-v2/{run_id}/",
        },
    }
    health = {
        "status": "ok",
        "geocoder_build": request["geocoder_build"],
        "overture_release": request["overture_release"],
        "catalog_key": preview["catalog_key"],
        "candidate_isolated": True,
    }
    places_query = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "id": "place",
            "properties": {"feature_type": "poi"},
        }],
        "data_version": {
            "geocoder_build": request["geocoder_build"],
            "overture_release": request["overture_release"],
        },
        "metadata": {"mode": "text"},
    }
    address_query = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature", "id": "address",
            "properties": {"feature_type": "address"},
        }],
        "data_version": places_query["data_version"],
        "metadata": {"mode": "structured_address"},
    }
    smoke_requests = {
        "places": {
            "query": {"q": "coffee", "types": ["poi"], "limit": 5},
            "expected_id": "place",
        },
        "addresses": {
            "query": {
                "country": "US", "admin_level_general": "Region",
                "admin_level_specific": "County", "postal_city": "Town",
                "postcode": "12345", "street": "Main", "number": "1",
                "unit": "",
            },
            "expected_id": "address",
        },
    }
    cleanup = {
        "worker_name": preview["cleanup"]["worker_name"],
        "worker_deleted": True,
        "deleted_object_keys": list(reversed(preview["cleanup"]["object_keys"])),
        "remaining_object_keys": [],
        "verified_empty_prefix": preview["cleanup"]["verify_empty_prefix"],
        "production_catalog_writes": False,
    }
    store = Store(tmp_path / "store")
    value = hosted.publish_worker_smoke_evidence(
        store, contract, runtime_value, request, preview=preview, health=health,
        places_query=places_query, address_query=address_query, cleanup=cleanup,
        smoke_requests=smoke_requests,
        slice_completion={"finalize_slice": {"completion_sha256": "7" * 64}},
    )
    assert store.keys == [
        f"{contract['namespace']['immutable_root']}/worker-smoke/report.json",
        f"{contract['namespace']['phase_completion_root']}/worker-smoke.json",
    ]
    assert value["marker_written_last"] is True
    assert value["production_catalog_writes"] is False

    bad_cleanup = {**cleanup, "remaining_object_keys": [preview["catalog_key"]]}
    with pytest.raises(ValueError, match="cleanup evidence"):
        hosted.publish_worker_smoke_evidence(
            Store(tmp_path / "bad-store"), contract, runtime_value, request, preview=preview,
            health=health, places_query=places_query, address_query=address_query,
            smoke_requests=smoke_requests,
            cleanup=bad_cleanup,
            slice_completion={"finalize_slice": {"completion_sha256": "7" * 64}},
        )


def test_streaming_family_finalizers_publish_manifests_last(tmp_path, monkeypatch):
    class Store(r2_verified_store.FilesystemStore):
        def list_prefix(self, prefix):
            return sorted(
                path.relative_to(self.root).as_posix() for path in self.root.rglob("*")
                if path.is_file() and not path.name.endswith(".metadata.json")
                and path.relative_to(self.root).as_posix().startswith(prefix)
            )

    request, contract = build_request(), build_contract()
    store = Store(tmp_path / "store")
    serving = {}
    for relative, body in (
        ("families/addresses/shards/a.aidx", b"idx"),
        ("families/addresses/shards/a.adat", b"dat"),
        ("families/places/q-0.pcsh", b"pcsh"),
        ("families/places/head.phrp", b"head"),
    ):
        source = tmp_path / relative.replace("/", "-")
        source.write_bytes(body)
        key = f"{SLICE}/{relative}"
        digest = hashlib.sha256(body).hexdigest()
        store.upload(source, key, digest)
        serving[key] = [len(body), digest]
    address_report = tmp_path / "address-report.json"
    places_report = tmp_path / "places-report.json"
    head_report = tmp_path / "head-report.json"
    for path in (address_report, places_report, head_report):
        path.write_bytes(b"{}\n")
    inputs = {
        "reports": [
            {"phase": "reduce", "family": "addresses", "index": 0, "path": str(address_report)},
            {"phase": "reduce", "family": "places", "index": 0, "path": str(places_report)},
            {"phase": "head", "family": "places", "index": 0, "path": str(head_report)},
        ],
        "serving": serving,
    }
    partition, reduce_plan, places_plan_path = (
        tmp_path / "partition.json", tmp_path / "reduce.json", tmp_path / "places-plan.json"
    )
    for path in (partition, reduce_plan, places_plan_path):
        path.write_bytes(b"{}\n")

    def address_finalize(*args, **kwargs):
        root = kwargs["output_root"] / "families/addresses"
        root.mkdir(parents=True, exist_ok=True)
        (root / "address-collection.json").write_bytes(b"collection")
        report = {
            "partition_plan_sha256": "a" * 64,
            "artifact_materialization": {"exact_content_identity_verified": True},
        }
        (root / "reduce-completion.json").write_bytes(executor.canonical_json(report))
        return report

    monkeypatch.setattr(hosted.address_reduce, "finalize", address_finalize)
    monkeypatch.setattr(hosted.family_manifest, "build_family_manifest", lambda *args, artifacts, **kwargs: {"artifacts": artifacts})

    def places_finalize(*args, **kwargs):
        kwargs["output_dir"].mkdir(parents=True, exist_ok=True)
        (kwargs["output_dir"] / "catalog.pcat").write_bytes(b"catalog")
        artifacts = [
            {"object_key": key.split(f"{SLICE}/", 1)[1], "bytes": size, "sha256": digest}
            for key, (size, digest) in serving.items() if "/families/places/" in key
        ]
        artifacts.append({"object_key": "families/places/catalog.pcat", "bytes": 7, "sha256": hashlib.sha256(b"catalog").hexdigest()})
        return {"artifact_materialization": {}}, {"artifacts": artifacts}

    monkeypatch.setattr(hosted.places_plan, "finalize_places_family", places_finalize)
    monkeypatch.setattr(hosted.places_plan, "validate_places_final_report", lambda value, *args: value)
    monkeypatch.setenv("R2_ENDPOINT", "https://example.invalid")
    result = hosted.finalize_publish_families(
        store, contract, request, address_partition_path=partition,
        address_reduce_path=reduce_plan, places_plan_path=places_plan_path,
        finalization_inputs=inputs, work_root=tmp_path / "finalize",
    )
    assert result["addresses"]["manifest_written_last"] is True
    assert result["places"]["manifest_written_last"] is True

    with pytest.raises(ValueError, match="0..N-1"):
        executor.normalize_expected_tasks(
            [
                {"family": "places", "task_id": "places-map-000", "index": 0},
                {"family": "places", "task_id": "places-map-002", "index": 2},
            ],
            "map",
        )


def test_task_completion_rejects_unreconciled_counts_and_forbidden_keys():
    contract = build_contract()
    with pytest.raises(ValueError, match="does not reconcile"):
        executor.build_task_completion(
            contract,
            runtime(),
            phase="map",
            family="places",
            task_id="places-map-000",
            index=0,
            producer_report=artifact(
                f"{contract['namespace']['immutable_root']}/report.json"
            ),
            artifacts=[artifact(f"{contract['namespace']['immutable_root']}/x")],
            counters={
                "input_records": 10,
                "retained_records": 9,
                "rejected_records": 2,
                "output_records": 9,
            },
        )

    with pytest.raises(ValueError, match="forbidden"):
        executor.normalize_artifact(artifact("v2/catalog.json"), contract)
    with pytest.raises(ValueError, match="forbidden"):
        executor.normalize_artifact(
            artifact("2026-07-18.0/release-manifest.json"), contract
        )


def test_budget_gate_uses_actual_matrix_cardinality_and_stops_overrun():
    contract = build_contract()
    allowed = executor.phase_budget(
        contract, phase="map", jobs=128, consumed_runner_minutes=0
    )
    assert allowed["allowed"] is True
    assert allowed["estimated_phase_runner_minutes"] == 11_520

    constrained = build_contract(
        max_total_runner_minutes=10_000, max_estimated_cost_usd=1_000
    )
    with pytest.raises(ValueError, match="cost/runtime gate"):
        executor.phase_budget(
            constrained, phase="map", jobs=128, consumed_runner_minutes=0
        )


def test_full_execution_budget_is_cumulative_and_gates_every_phase():
    contract = build_contract()
    value = executor.execution_budget(contract, map_jobs=216, reduce_jobs=12)
    assert value["allowed"] is True
    assert value["phases"][1]["consumed_runner_minutes"] == 120
    assert value["phases"][3]["consumed_runner_minutes"] == 120 + 216 * 90 + 180
    assert value["estimated_total_runner_minutes"] == sum(
        phase["estimated_phase_runner_minutes"] for phase in value["phases"]
    )
    assert value["estimated_total_cost_usd"] == round(
        value["estimated_total_runner_minutes"] * 0.02, 2
    )

    constrained = build_contract(
        max_total_runner_minutes=value["estimated_total_runner_minutes"] - 1,
        max_estimated_cost_usd=10_000,
    )
    with pytest.raises(ValueError, match="cost/runtime gate"):
        executor.execution_budget(constrained, map_jobs=216, reduce_jobs=12)


def _family_manifest(family: str, artifacts: list[dict]) -> dict:
    versions = (
        {
            "format": gbm.ADDRESS_FORMAT_VERSION,
            "tokenizer": None,
            "normalization": gbm.ADDRESS_NORMALIZATION_VERSION,
        }
        if family == "addresses"
        else {
            "format": gbm.PLACES_FORMAT_VERSION,
            "tokenizer": gbm.PLACES_TOKENIZER_VERSION,
            "normalization": None,
        }
    )
    return gbm.build_family_manifest(
        family,
        lineage={
            "overture_release": RELEASE,
            "build_id": ("a" if family == "addresses" else "b") * 64,
            "producer_commit": COMMIT,
            "producer_script": f"scripts/global_v2_{family}_plan.py",
            "producer_version": "1",
        },
        versions=versions,
        region={
            "name": "world",
            "bbox": [-180.0, -90.0, 180.0, 90.0],
            "bbox_scope": "exact",
        },
        artifacts=artifacts,
        generated_at=None,
    )


def family_slice_inputs():
    address_artifacts = [
        artifact("families/addresses/address-collection.json", b"collection"),
        artifact("families/addresses/partition-plan.json", b"plan"),
        artifact("families/addresses/shards/a.aidx", b"index"),
        artifact("families/addresses/shards/a.adat", b"data"),
    ]
    places_artifacts = [
        artifact("families/places/catalog.pcat", b"catalog"),
        artifact("families/places/head.phrp", b"head"),
        artifact("families/places/q-0.pcsh", b"shard"),
    ]
    address_manifest = _family_manifest("addresses", address_artifacts)
    places_manifest = _family_manifest("places", places_artifacts)
    objects = []
    for manifest in (address_manifest, places_manifest):
        for item in manifest["artifacts"]:
            objects.append({**item, "object_key": f"{SLICE}/{item['object_key']}"})
        manifest_key = f"{SLICE}/families/{manifest['family']}/family-manifest.json"
        manifest_bytes = gbm.canonical_json(manifest)
        objects.append(
            {
                "object_key": manifest_key,
                "bytes": len(manifest_bytes),
                "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            }
        )
    listing = {
        "schema": executor.REMOTE_LISTING_SCHEMA,
        "slice_version": SLICE,
        "objects": sorted(objects, key=lambda item: item["object_key"]),
    }
    evidence = [
        {"phase": phase, "completion_sha256": str(index + 1) * 64}
        for index, phase in enumerate(
            ("inventory", "map", "aggregate-plan", "reduce", "head")
        )
    ]
    return address_manifest, places_manifest, listing, evidence


def test_slice_candidate_exactly_verifies_families_hashes_totals_and_head():
    address_manifest, places_manifest, listing, evidence = family_slice_inputs()
    candidate = executor.build_slice_manifest(
        build_contract(),
        build_request(),
        runtime(),
        address_manifest=address_manifest,
        places_manifest=places_manifest,
        remote_listing=listing,
        phase_evidence=evidence,
    )

    assert candidate["promotion_eligible"] is False
    assert candidate["catalog_published"] is False
    assert candidate["legacy_core_rebuilt"] is False
    assert candidate["divisions_rebuilt"] is False
    assert set(candidate["families"]) == {"addresses", "places"}
    assert candidate["families"]["places"]["artifact_count"] == 3
    assert len(candidate["verified_version_objects"]) == 9


def test_slice_candidate_rejects_extra_remote_object_and_missing_places_head():
    address_manifest, places_manifest, listing, evidence = family_slice_inputs()
    extra = copy.deepcopy(listing)
    extra["objects"].append(
        artifact(f"{SLICE}/families/places/stray.bin", b"stray")
    )
    extra["objects"].sort(key=lambda item: item["object_key"])
    with pytest.raises(ValueError, match="remote family slice differs"):
        executor.build_slice_manifest(
            build_contract(),
            build_request(),
            runtime(),
            address_manifest=address_manifest,
            places_manifest=places_manifest,
            remote_listing=extra,
            phase_evidence=evidence,
        )

    without_head = copy.deepcopy(places_manifest)
    without_head["artifacts"] = [
        item for item in without_head["artifacts"] if not item["object_key"].endswith("head.phrp")
    ]
    # Rebuild so failure is specifically the retained request's required pattern.
    without_head = _family_manifest("places", without_head["artifacts"])
    with pytest.raises(ValueError, match="head.phrp"):
        executor.build_slice_manifest(
            build_contract(),
            build_request(),
            runtime(),
            address_manifest=address_manifest,
            places_manifest=without_head,
            remote_listing=listing,
            phase_evidence=evidence,
        )


def test_stop_evidence_never_claims_catalog_or_core_mutation():
    value = executor.build_stop_evidence(
        build_contract(),
        phase="map",
        run_id="12345",
        run_attempt="2",
        result="failure",
        completed=False,
    )
    assert value["slice_prefix_retained"] is True
    assert value["catalog_published"] is False
    assert value["legacy_core_rebuilt"] is False
    assert value["divisions_rebuilt"] is False


def test_slice_finalizer_stream_verifies_once_and_publishes_marker_last(tmp_path):
    address_manifest, places_manifest, listing, evidence = family_slice_inputs()
    request = build_request()
    contract = build_contract()
    runtime_value = runtime()
    candidate = executor.build_slice_manifest(
        contract,
        request,
        runtime_value,
        address_manifest=address_manifest,
        places_manifest=places_manifest,
        remote_listing=listing,
        phase_evidence=evidence,
    )
    paths = {}
    for name, value in (
        ("request", request),
        ("contract", contract),
        ("runtime", runtime_value),
        ("addresses", address_manifest),
        ("places", places_manifest),
        ("evidence", evidence),
        ("candidate", candidate),
    ):
        path = tmp_path / f"{name}.json"
        path.write_bytes(executor.canonical_json(value))
        paths[name] = path

    class IdentityClient:
        def __init__(self):
            self.identities = {
                item["object_key"]: (item["bytes"], item["sha256"])
                for item in listing["objects"]
            }
            self.data = {}
            self.identity_reads = []
            self.puts = []

        def list_prefix(self, prefix):
            return sorted(key for key in self.identities if key.startswith(prefix))

        def object_identity(self, key):
            self.identity_reads.append(key)
            return self.identities[key]

        def put_immutable(self, key, data):
            identity = (len(data), hashlib.sha256(data).hexdigest())
            if key in self.identities:
                assert self.identities[key] == identity
                return
            self.puts.append(key)
            self.identities[key] = identity
            self.data[key] = data

        def get_object(self, key):
            return self.data[key]

    client = IdentityClient()
    report = finalize_rebuild.publish_global_v2_slice(
        client,
        request_path=paths["request"],
        contract_path=paths["contract"],
        runtime_path=paths["runtime"],
        addresses_manifest_path=paths["addresses"],
        places_manifest_path=paths["places"],
        phase_evidence_path=paths["evidence"],
        candidate_path=paths["candidate"],
        log=lambda *_args: None,
    )
    marker_key = f"{SLICE}/slice-manifest.json"
    remote_report_key = f"{contract['namespace']['immutable_root']}/remote-verify/report.json"
    remote_marker_key = (
        f"{contract['namespace']['phase_completion_root']}/remote-verify.json"
    )
    finalize_marker_key = (
        f"{contract['namespace']['phase_completion_root']}/finalize-slice.json"
    )
    assert client.puts == [
        remote_report_key, remote_marker_key, marker_key, finalize_marker_key,
    ]
    assert report["catalog_published"] is False
    assert report["verified_family_objects"] == len(listing["objects"])
    for item in listing["objects"]:
        assert client.identity_reads.count(item["object_key"]) == 1

    reads_before_retry = list(client.identity_reads)
    paths["runtime"].write_bytes(executor.canonical_json(runtime("20260720.1.0")))
    retry = finalize_rebuild.publish_global_v2_slice(
        client,
        request_path=paths["request"],
        contract_path=paths["contract"],
        runtime_path=paths["runtime"],
        addresses_manifest_path=paths["addresses"],
        places_manifest_path=paths["places"],
        phase_evidence_path=paths["evidence"],
        candidate_path=paths["candidate"],
        log=lambda *_args: None,
    )
    assert retry == report
    assert client.puts == [
        remote_report_key, remote_marker_key, marker_key, finalize_marker_key,
    ]
    assert client.identity_reads[len(reads_before_retry) :] == [
        remote_report_key,
        remote_marker_key,
        marker_key,
        finalize_marker_key,
    ]
