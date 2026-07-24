"""End-to-end, no-network proof of the construction-v1 execute command sequence.

This composes the EXACT construction_v1_hosted.py subcommands the hosted
workflow runs in execute mode -- derive-contract -> admit-task -> run-map ->
plan-reduce -> run-reduce -> run-head (places) -> finalize -- against a tmpdir
store and a tmpdir "remote" instead of R2. If any phase produced or consumed a
file the previous phase never wrote, this fails locally, which is what prevents
a planet attempt from burning ~500 runner-minutes on a file-not-found.

It also proves the runner-minute ledger fails closed before the next phase when
prior + projected minutes exceed the confirmation cap.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


pytest.importorskip("pyarrow")

ROOT = Path(__file__).parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ADDR_SPIKE = _load("hosted_test_address_spike", "tests/test_address_construction_spike.py")
PLACES_TEST = _load("hosted_test_places", "tests/test_places_construction_v1.py")
HOSTED = _load("hosted_test_adapter", "scripts/construction_v1_hosted.py")


@pytest.fixture(scope="module")
def binaries() -> dict[str, Path]:
    subprocess.run(
        ["cargo", "build", "-p", "geocoder-construction", "--bins"],
        cwd=ROOT / "crates",
        check=True,
    )
    target = ROOT / "crates/target/debug"
    return {name: target / name for name in (
        "address-transform-v1", "address-proof-directory",
        "address-serving-encode-v1", "address-serving-verify-v1",
        "places-transform-v1", "places-proof-directory",
        "places-serving-encode-v1", "places-serving-verify-v1",
    )}


def _request(tmp_path: Path) -> Path:
    request = {
        "schema": "overture-construction-v1-request-v1",
        "release": "2026-06-17.0",
        "families": {"addresses": {}, "places": {}},
        "versions": {"duckdb": "1.5.1", "pyarrow": "25.0.0", "numpy": "2.3.5",
                     "python": "3.12.12", "rustc": "test"},
        "caps": {"max_remote_operations": 100000, "max_remote_write_bytes": 1_000_000_000_000},
        "namespaces": {
            "immutable_root": "construction-v1/deadbeef",
            "slice": "construction-v1/deadbeef/slice/slice-x/",
            "markers": "construction-v1/deadbeef/markers/",
        },
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request) + "\n")
    return path


def _run(*argv: str) -> None:
    assert HOSTED.main([str(a) for a in argv]) == 0


def _derive(tmp_path: Path) -> tuple[Path, Path]:
    contract = tmp_path / "contract.json"
    runtime = tmp_path / "runtime.json"
    _run("derive-contract", "--request", _request(tmp_path),
         "--output", contract, "--runtime", runtime,
         "--allow-unpinned-duckdb", "--map-input-rows-cap", "100")
    assert json.loads(runtime.read_text())["strict_versions"] is False
    return contract, runtime


def _admit_completed(store: Path, family: str, phase: str, **kw) -> bool:
    out = store.parent / f"admit-{family}-{phase}-{kw.get('task_id', kw.get('index', ''))}.json"
    argv = ["admit-task", "--store-root", store, "--family", family, "--phase", phase, "--output", out]
    for key, value in kw.items():
        argv += [f"--{key.replace('_', '-')}", value]
    _run(*argv)
    return json.loads(out.read_text())["completed"]


def test_execute_sequence_addresses_end_to_end_no_network(tmp_path, binaries):
    contract, _ = _derive(tmp_path)
    store = tmp_path / "store"
    markers_dir = tmp_path / "markers"
    markers_dir.mkdir()

    # One map task from a tiny projected fixture.
    rows = [
        {"id": str(uuid.UUID(int=2)), "street": "Main Street", "number": "10", "unit": "",
         "postcode": "02180", "postal_city": "Stoneham", "address_levels": ["MA", "Stoneham"],
         "country": "US", "point": [-71.0, 42.0], "source_object_index": 0,
         "source_row_group": 0, "source_row_index": 1},
        {"id": str(uuid.UUID(int=1)), "street": "Main Street", "number": "10", "unit": "",
         "postcode": "02180", "postal_city": "Stoneham", "address_levels": ["MA", "Stoneham"],
         "country": "US", "point": [-71.0, 42.0], "source_object_index": 0,
         "source_row_group": 0, "source_row_index": 0},
        {"id": str(uuid.UUID(int=3)), "street": "Main Street", "number": "99", "unit": "",
         "postcode": "02180", "postal_city": "Stoneham", "address_levels": ["MA", "Stoneham"],
         "country": "US", "point": [-71.0, 42.0], "source_object_index": 0,
         "source_row_group": 0, "source_row_index": 2},
    ]
    projected = tmp_path / "projected.parquet"
    ADDR_SPIKE.write_fixture(projected, rows)
    source_limits = tmp_path / "source-limits.json"
    source_limits.write_text(json.dumps({"objects": [{"records": 3, "row_groups": 1}]}) + "\n")

    assert _admit_completed(store, "addresses", "map", task_id="addresses-map-000") is False
    _run("run-map", "--contract", contract, "--store-root", store, "--family", "addresses",
         "--task-id", "addresses-map-000", "--input", projected, "--source-limits", source_limits,
         "--transform-binary", binaries["address-transform-v1"],
         "--proof-binary", binaries["address-proof-directory"],
         "--scratch-dir", tmp_path / "map-scratch",
         "--marker-out", markers_dir / "000.json")
    # Idempotent: a re-admit now reports completed and re-running the map admits
    # the existing marker.
    assert _admit_completed(store, "addresses", "map", task_id="addresses-map-000") is True

    # P1-4: a fresh RESUME dispatch has no local store, but the durable create-only
    # marker in the remote store lets admit-task skip a genuinely completed task.
    remote_markers = tmp_path / "remote-markers"
    marker_key = HOSTED.ADDRESS.marker_key("addresses-map-000")
    (remote_markers / marker_key).parent.mkdir(parents=True, exist_ok=True)
    (remote_markers / marker_key).write_bytes((store / marker_key).read_bytes())
    fresh_store = tmp_path / "fresh-store"  # empty: models a resume with no artifacts
    contract_path = contract
    out = tmp_path / "resume-admit-000.json"
    assert HOSTED.main(["admit-task", "--store-root", str(fresh_store),
                        "--family", "addresses", "--phase", "map",
                        "--task-id", "addresses-map-000", "--contract", str(contract_path),
                        "--remote-root", str(remote_markers), "--output", str(out)]) == 0
    assert json.loads(out.read_text())["completed"] is True  # completed subset skipped
    # An unpublished sibling task on the same resume is NOT skipped: it must run.
    out2 = tmp_path / "resume-admit-001.json"
    assert HOSTED.main(["admit-task", "--store-root", str(fresh_store),
                        "--family", "addresses", "--phase", "map",
                        "--task-id", "addresses-map-001", "--contract", str(contract_path),
                        "--remote-root", str(remote_markers), "--output", str(out2)]) == 0
    assert json.loads(out2.read_text())["completed"] is False

    plan = tmp_path / "plan.json"
    _run("plan-reduce", "--contract", contract, "--store-root", store, "--family", "addresses",
         "--markers-dir", markers_dir, "--row-cap", "2", "--output", plan,
         "--matrix-out", tmp_path / "reduce-matrix.json")
    partitions = json.loads(plan.read_text())["partitions"]
    assert len(partitions) == 2

    reductions_dir = tmp_path / "reductions"
    reductions_dir.mkdir()
    for index in range(len(partitions)):
        _run("run-reduce", "--contract", contract, "--store-root", store, "--family", "addresses",
             "--plan", plan, "--markers-dir", markers_dir, "--partition-index", index,
             "--proof-binary", binaries["address-proof-directory"],
             "--encoder-binary", binaries["address-serving-encode-v1"],
             "--verifier-binary", binaries["address-serving-verify-v1"],
             "--scratch-dir", tmp_path / f"reduce-scratch-{index}",
             "--output", reductions_dir / f"{index:04d}.json")
        assert _admit_completed(store, "addresses", "reduce", index=index) is True

    head = tmp_path / "head.json"
    _run("run-head", "--contract", contract, "--store-root", store, "--family", "addresses",
         "--markers-dir", markers_dir, "--output", head)
    assert json.loads(head.read_text())["head"] is None

    remote = tmp_path / "remote"
    final = tmp_path / "final.json"
    _run("finalize", "--contract", contract, "--store-root", store, "--family", "addresses",
         "--plan", plan, "--reductions-dir", reductions_dir, "--remote-root", remote,
         "--work-root", tmp_path / "final-work", "--output", final)
    result = json.loads(final.read_text())
    assert result["reconciles"] is True
    assert result["marker_written_last"] is True
    # The family manifest + slice manifest + one serving object per partition,
    # all present in the "remote" and byte-verified exactly once.
    assert result["objects"] == 2 + len(partitions)
    # The completion marker is outside the verified family prefix and written
    # last: publish_exact_set HEAD-verified it.
    marker_key = result["marker_key"]
    assert (remote / marker_key).is_file()


def _address_map_store(tmp_path: Path, contract: Path, binaries, tag: str) -> tuple[Path, Path]:
    """Build a store + markers dir with one address map task from a tiny fixture."""
    store = tmp_path / f"store-{tag}"
    markers_dir = tmp_path / f"markers-{tag}"
    markers_dir.mkdir()
    rows = [
        {"id": str(uuid.UUID(int=i)), "street": "Main Street", "number": str(10 + i),
         "unit": "", "postcode": "02180", "postal_city": "Stoneham",
         "address_levels": ["MA", "Stoneham"], "country": "US", "point": [-71.0, 42.0],
         "source_object_index": 0, "source_row_group": 0, "source_row_index": i}
        for i in range(6)
    ]
    projected = tmp_path / f"projected-{tag}.parquet"
    ADDR_SPIKE.write_fixture(projected, rows)
    source_limits = tmp_path / f"source-limits-{tag}.json"
    source_limits.write_text(json.dumps({"objects": [{"records": len(rows), "row_groups": 1}]}) + "\n")
    _run("run-map", "--contract", contract, "--store-root", store, "--family", "addresses",
         "--task-id", "addresses-map-000", "--input", projected, "--source-limits", source_limits,
         "--transform-binary", binaries["address-transform-v1"],
         "--proof-binary", binaries["address-proof-directory"],
         "--scratch-dir", tmp_path / f"map-scratch-{tag}",
         "--marker-out", markers_dir / "000.json")
    return store, markers_dir


def test_reduce_batching_matches_the_unbatched_plan(tmp_path, binaries):
    """P0-3: a fixture that yields MORE partitions than a tiny job cap forces
    multiple partitions per matrix JOB, and the batched serving outputs are
    byte-identical to running one job per partition."""
    contract, _ = _derive(tmp_path)

    def plan_and_partitions(store: Path, markers_dir: Path, tag: str, *, max_reduce_jobs=None):
        plan = tmp_path / f"plan-{tag}.json"
        matrix = tmp_path / f"matrix-{tag}.json"
        argv = ["plan-reduce", "--contract", contract, "--store-root", store,
                "--family", "addresses", "--markers-dir", markers_dir, "--row-cap", "2",
                "--output", plan, "--matrix-out", matrix]
        if max_reduce_jobs is not None:
            argv += ["--max-reduce-jobs", str(max_reduce_jobs)]
        _run(*argv)
        return json.loads(plan.read_text()), json.loads(matrix.read_text()), plan

    # Unbatched: default cap -> one job per partition (batch_size 1).
    store_u, markers_u = _address_map_store(tmp_path, contract, binaries, "unbatched")
    plan_u, _matrix_u, plan_u_path = plan_and_partitions(store_u, markers_u, "unbatched")
    partition_count = len(plan_u["partitions"])
    assert partition_count >= 3, "fixture must exceed the tiny job cap to force batching"
    assert plan_u["reduce_execution"]["batch_size"] == 1
    assert plan_u["reduce_execution"]["job_count"] == partition_count

    unbatched_dir = tmp_path / "reductions-unbatched"
    unbatched_dir.mkdir()
    for index in range(partition_count):
        _run("run-reduce", "--contract", contract, "--store-root", store_u,
             "--family", "addresses", "--plan", plan_u_path, "--markers-dir", markers_u,
             "--partition-index", index, "--proof-binary", binaries["address-proof-directory"],
             "--encoder-binary", binaries["address-serving-encode-v1"],
             "--verifier-binary", binaries["address-serving-verify-v1"],
             "--scratch-dir", tmp_path / f"reduce-u-{index}",
             "--output", unbatched_dir / f"{index:04d}.json")

    # Batched: a job cap of 2 forces batch_size = ceil(partitions/2) > 1.
    store_b, markers_b = _address_map_store(tmp_path, contract, binaries, "batched")
    plan_b, matrix_b, plan_b_path = plan_and_partitions(store_b, markers_b, "batched", max_reduce_jobs=2)
    # Same partitions; only the execution grouping changes.
    assert plan_b["partitions"] == plan_u["partitions"]
    assert plan_b["reduce_execution"]["batch_size"] > 1
    assert plan_b["reduce_execution"]["job_count"] <= 2
    assert len(matrix_b["include"]) == plan_b["reduce_execution"]["job_count"]
    assert matrix_b["include"][0]["partition_count"] >= 2

    batched_dir = tmp_path / "reductions-batched"
    batched_dir.mkdir()
    for batch in matrix_b["include"]:
        _run("run-reduce", "--contract", contract, "--store-root", store_b,
             "--family", "addresses", "--plan", plan_b_path, "--markers-dir", markers_b,
             "--batch-index", batch["batch_index"], "--proof-binary", binaries["address-proof-directory"],
             "--encoder-binary", binaries["address-serving-encode-v1"],
             "--verifier-binary", binaries["address-serving-verify-v1"],
             "--scratch-dir", tmp_path / f"reduce-b-{batch['batch_index']}",
             "--output-dir", batched_dir)

    # Every partition produced exactly one reduction in both modes, and the
    # per-partition serving artifacts are byte-identical.
    unbatched = sorted(p.name for p in unbatched_dir.glob("*.json"))
    batched = sorted(p.name for p in batched_dir.glob("*.json"))
    assert unbatched == batched == [f"{i:04d}.json" for i in range(partition_count)]
    for name in unbatched:
        u = json.loads((unbatched_dir / name).read_text())
        b = json.loads((batched_dir / name).read_text())
        # The content-addressed serving artifact and the partition binding are
        # identical; only per-run timing evidence may differ.
        assert u["artifact"] == b["artifact"], name
        assert u["partition"] == b["partition"], name
        assert u["verification"]["binding"] == b["verification"]["binding"], name


def test_execute_sequence_places_end_to_end_with_head_no_network(tmp_path, binaries):
    contract, _ = _derive(tmp_path)
    store = tmp_path / "store"
    markers_dir = tmp_path / "markers"
    markers_dir.mkdir()

    def one_map(task_id: str, seed: int, offset: int) -> None:
        rows = [
            {"id": str(uuid.UUID(int=seed * 1000 + i)),
             "primary_name": f"Common Place {offset + i}", "category": "library",
             "locality": "Town", "country": "XX", "confidence": 1.0 - (i % 8) / 20,
             "point": [0.0, 0.0], "source_row_index": i}
            for i in range(60)
        ]
        source = tmp_path / f"{task_id}.parquet"
        PLACES_TEST.write_fixture(source, rows, row_group_size=32)
        import pyarrow.parquet as pq

        (tmp_path / f"{task_id}-limits.json").write_text(json.dumps(
            {"objects": [{"records": len(rows), "row_groups": pq.ParquetFile(source).metadata.num_row_groups}]}))
        _run("run-map", "--contract", contract, "--store-root", store, "--family", "places",
             "--task-id", task_id, "--input", source,
             "--source-limits", tmp_path / f"{task_id}-limits.json",
             "--transform-binary", binaries["places-transform-v1"],
             "--proof-binary", binaries["places-proof-directory"],
             "--scratch-dir", tmp_path / f"{task_id}-scratch",
             "--marker-out", markers_dir / f"{task_id}.json")

    assert _admit_completed(store, "places", "map", task_id="places-map-000") is False
    one_map("places-map-000", 11, 0)
    one_map("places-map-001", 22, 500)
    assert _admit_completed(store, "places", "map", task_id="places-map-000") is True

    plan = tmp_path / "plan.json"
    _run("plan-reduce", "--contract", contract, "--store-root", store, "--family", "places",
         "--markers-dir", markers_dir, "--scratch-dir", tmp_path / "plan-scratch",
         "--output", plan, "--matrix-out", tmp_path / "reduce-matrix.json")
    partitions = json.loads(plan.read_text())["partitions"]
    assert len(partitions) >= 1

    reductions_dir = tmp_path / "reductions"
    reductions_dir.mkdir()
    for index in range(len(partitions)):
        _run("run-reduce", "--contract", contract, "--store-root", store, "--family", "places",
             "--plan", plan, "--markers-dir", markers_dir, "--partition-index", index,
             "--encoder-binary", binaries["places-serving-encode-v1"],
             "--verifier-binary", binaries["places-serving-verify-v1"],
             "--scratch-dir", tmp_path / f"reduce-scratch-{index}",
             "--output", reductions_dir / f"{index:04d}.json")

    head = tmp_path / "head.json"
    assert _admit_completed(store, "places", "head", index=0) is False
    _run("run-head", "--contract", contract, "--store-root", store, "--family", "places",
         "--markers-dir", markers_dir, "--encoder-binary", binaries["places-serving-encode-v1"],
         "--verifier-binary", binaries["places-serving-verify-v1"],
         "--scratch-dir", tmp_path / "head-scratch", "--shard-bits", "4", "--output", head)
    head_result = json.loads(head.read_text())
    assert head_result["shard_count"] == 16
    assert _admit_completed(store, "places", "head", index=0) is True

    remote = tmp_path / "remote"
    final = tmp_path / "final.json"
    _run("finalize", "--contract", contract, "--store-root", store, "--family", "places",
         "--plan", plan, "--reductions-dir", reductions_dir, "--head", head,
         "--remote-root", remote, "--work-root", tmp_path / "final-work", "--output", final)
    result = json.loads(final.read_text())
    assert result["reconciles"] is True
    assert result["marker_written_last"] is True
    assert (remote / result["marker_key"]).is_file()


def test_ledger_fails_closed_before_the_next_phase(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "schema": "construction-v1-run-ledger-v1",
        "max_total_runner_minutes": 100,
        "prior_runner_minutes": 40,
        "phases": [{"phase": "admission", "runner_minutes": 0}],
    }))
    # Append a map phase; consumed = 40 + 0 + 30 = 70, still under 100.
    _run("ledger-append", "--ledger", ledger, "--phase", "map", "--minutes", "30")
    # A next phase estimated at 25 keeps us at 95 <= 100: passes.
    _run("ledger-check", "--ledger", ledger, "--next-phase-minutes", "25")
    # A next phase estimated at 40 projects 110 > 100: must fail closed.
    with pytest.raises(SystemExit) as excinfo:
        HOSTED.main(["ledger-check", "--ledger", str(ledger), "--next-phase-minutes", "40"])
    assert "exceed cap" in str(excinfo.value)


def test_prior_runner_minutes_bind_into_the_typed_confirmation():
    control = _load("hosted_test_control", "scripts/construction_v1_control.py")
    import argparse

    def args(**overrides):
        base = dict(request_id="request-20260722-a1", build_id="build-20260722-a1",
                    slice_id="slice-20260722-a1", staging_id="staging-20260722-a1",
                    producer_commit="1" * 40, legacy_core_version="legacy-core-20260722-a1",
                    legacy_core_manifest_sha256="2" * 64)
        base.update(overrides)
        return argparse.Namespace(**base)

    base, _ = control.prepare(args())
    resumed, _ = control.prepare(args(prior_runner_minutes=5000))
    assert "PRIOR_RUNNER_MINUTES=0" in base["typed_confirmation"]
    assert "PRIOR_RUNNER_MINUTES=5000" in resumed["typed_confirmation"]
    assert base["request_sha256"] != resumed["request_sha256"]
    # admit-dispatch regeneration reproduces the resumed confirmation exactly.
    identity = resumed["request"]["identity"]
    core = resumed["request"]["legacy_core"]
    regen, _ = control.prepare(argparse.Namespace(
        request_id=identity["request_id"], build_id=identity["build_id"],
        slice_id=identity["slice_id"], staging_id=identity["staging_id"],
        producer_commit=resumed["request"]["producer_commit"],
        legacy_core_version=core["version"], legacy_core_manifest_sha256=core["manifest_sha256"],
        prior_runner_minutes=resumed["request"]["caps"]["prior_runner_minutes"]))
    assert regen["request"] == resumed["request"]
    assert regen["typed_confirmation"] == resumed["typed_confirmation"]
