from __future__ import annotations

import importlib.util
import copy
import dataclasses
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


pa = pytest.importorskip("pyarrow")

ROOT = Path(__file__).parents[1]
SPIKE_TEST_SPEC = importlib.util.spec_from_file_location(
    "address_spike_test_helpers", ROOT / "tests/test_address_construction_spike.py"
)
assert SPIKE_TEST_SPEC and SPIKE_TEST_SPEC.loader
SPIKE_TEST = importlib.util.module_from_spec(SPIKE_TEST_SPEC)
SPIKE_TEST_SPEC.loader.exec_module(SPIKE_TEST)
CONSTRUCTION_SPEC = importlib.util.spec_from_file_location(
    "address_construction_v1", ROOT / "scripts/address_construction_v1.py"
)
assert CONSTRUCTION_SPEC and CONSTRUCTION_SPEC.loader
CONSTRUCTION = importlib.util.module_from_spec(CONSTRUCTION_SPEC)
sys.modules[CONSTRUCTION_SPEC.name] = CONSTRUCTION
CONSTRUCTION_SPEC.loader.exec_module(CONSTRUCTION)
STAGING_SPEC = importlib.util.spec_from_file_location(
    "address_construction_staging_v1", ROOT / "scripts/construction_staging_v1.py"
)
assert STAGING_SPEC and STAGING_SPEC.loader
STAGING = importlib.util.module_from_spec(STAGING_SPEC)
sys.modules[STAGING_SPEC.name] = STAGING
STAGING_SPEC.loader.exec_module(STAGING)


@pytest.fixture(scope="module")
def binaries():
    subprocess.run(
        ["cargo", "build", "-p", "geocoder-construction", "--bins"],
        cwd=ROOT / "crates",
        check=True,
    )
    target = ROOT / "crates/target/debug"
    return {
        "transform": target / "address-transform-v1",
        "directory": target / "address-proof-directory",
        "encoder": target / "address-serving-encode-v1",
        "verifier": target / "address-serving-verify-v1",
    }


def base_row(feature_id: int, *, number: str, source_row_index: int) -> dict:
    return {
        "id": str(uuid.UUID(int=feature_id)),
        "street": "Main Street",
        "number": number,
        "unit": "",
        "postcode": "02180",
        "postal_city": "Stoneham",
        "address_levels": ["MA", "Stoneham"],
        "country": "US",
        "point": [-71.0, 42.0],
        "source_object_index": 0,
        "source_row_group": 0,
        "source_row_index": source_row_index,
    }


def limits() -> CONSTRUCTION.Limits:
    return CONSTRUCTION.Limits(
        max_input_rows=100,
        max_pack_rows=10,
        parquet_row_group_rows=2_048,
        max_rss_bytes=1_024**3,
        max_scratch_bytes=512 * 1024**2,
        max_output_bytes=128 * 1024**2,
        max_serving_bytes=16 * 1024**2,
        wall_seconds=60,
        duckdb_memory_limit="256MB",
        duckdb_threads=2,
        allow_unpinned_duckdb=True,
    )


def test_planet_runtime_gate_is_fail_closed():
    with pytest.raises(RuntimeError, match="DuckDB 1.5.1 is required"):
        CONSTRUCTION.require_duckdb_runtime(
            SimpleNamespace(__version__="1.4.4"), CONSTRUCTION.Limits()
        )


def test_local_object_write_interruption_never_publishes_partial_identity(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.bin"
    source.write_bytes(b"address-construction" * 1024)
    store = CONSTRUCTION.LocalObjectStore(tmp_path / "objects")
    original = CONSTRUCTION.shutil.copyfileobj

    def interrupted(input_file, output_file, length):
        output_file.write(input_file.read(37))
        raise RuntimeError("injected local write interruption")

    monkeypatch.setattr(CONSTRUCTION.shutil, "copyfileobj", interrupted)
    with pytest.raises(RuntimeError, match="local write interruption"):
        store.put_content(source, "map/address/packs", ".bin")
    digest = CONSTRUCTION.sha256_file(source)
    assert not store.path(f"map/address/packs/sha256/{digest}.bin").exists()

    monkeypatch.setattr(CONSTRUCTION.shutil, "copyfileobj", original)
    identity = store.put_content(source, "map/address/packs", ".bin")
    assert identity["sha256"] == digest
    assert store.path(identity["key"]).read_bytes() == source.read_bytes()


def test_local_vertical_slice_marker_resume_overlap_reduce_and_query(tmp_path, binaries):
    rows = [
        base_row(2, number="10", source_row_index=1),
        base_row(1, number="10", source_row_index=0),
        base_row(3, number="99", source_row_index=2),
    ]
    projected = tmp_path / "projected.parquet"
    SPIKE_TEST.write_fixture(projected, rows)
    source_limits = tmp_path / "source-limits.json"
    source_limits.write_text(
        json.dumps({"objects": [{"records": 3, "row_groups": 1}]}) + "\n"
    )
    store = CONSTRUCTION.LocalObjectStore(tmp_path / "objects")
    request = "a" * 64
    with pytest.raises(RuntimeError, match="injected interruption"):
        CONSTRUCTION.map_task(
            input_path=projected,
            source_limits=source_limits,
            store=store,
            scratch_root=tmp_path / "scratch",
            request_sha256=request,
            task_id="fixture",
            transform_binary=binaries["transform"],
            directory_binary=binaries["directory"],
            limits=limits(),
            failpoint="after_objects",
        )
    assert store.read_json(CONSTRUCTION.marker_key("fixture")) is None
    assert list((tmp_path / "objects/map/address/packs/sha256").iterdir())

    marker = CONSTRUCTION.map_task(
        input_path=projected,
        source_limits=source_limits,
        store=store,
        scratch_root=tmp_path / "scratch",
        request_sha256=request,
        task_id="fixture",
        transform_binary=binaries["transform"],
        directory_binary=binaries["directory"],
        limits=limits(),
    )
    assert marker["binding"]["records"] == 3
    assert marker["admitted_existing"] is False
    assert marker["packs"][0]["directory"]["row_groups"][0]["binding"][
        "records"
    ] == 3

    admitted = CONSTRUCTION.map_task(
        input_path=tmp_path / "must-not-be-read.parquet",
        source_limits=tmp_path / "must-not-be-read.json",
        store=store,
        scratch_root=tmp_path / "scratch",
        request_sha256=request,
        task_id="fixture",
        transform_binary=binaries["transform"],
        directory_binary=binaries["directory"],
        limits=limits(),
    )
    assert admitted["admitted_existing"] is True

    plan = CONSTRUCTION.genesis_plan([marker], row_cap=2)
    assert len(plan["partitions"]) == 2
    query = ["us", "ma", "stoneham", "stoneham", "02180", "main street", "10", ""]
    reductions = [
        CONSTRUCTION.reduce_partition(
            partition=partition,
            markers=[marker],
            store=store,
            scratch_root=tmp_path / "reduce-scratch",
            directory_binary=binaries["directory"],
            encoder_binary=binaries["encoder"],
            verifier_binary=binaries["verifier"],
            limits=limits(),
            query=query,
        )
        for partition in plan["partitions"]
    ]
    assert all(item["selected_row_groups"] == 1 for item in reductions)
    assert sorted(item["discarded_binding"]["records"] for item in reductions) == [1, 2]
    finalization = CONSTRUCTION.validate_complete_reduction(plan, reductions)
    assert finalization["reconciles"]
    query_ids = sorted(
        feature_id
        for item in reductions
        for feature_id in item["verification"]["query_feature_ids"]
    )
    assert query_ids == [str(uuid.UUID(int=1)), str(uuid.UUID(int=2))]
    (tmp_path / "checkpoint-result.json").write_text(
        json.dumps(
            {
                "marker": marker,
                "plan": plan,
                "reductions": reductions,
                "finalization": finalization,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    with pytest.raises(ValueError, match="missing, extra, or duplicate"):
        CONSTRUCTION.validate_complete_reduction(plan, reductions[:-1])
    with pytest.raises(ValueError, match="missing, extra, or duplicate"):
        CONSTRUCTION.validate_complete_reduction(plan, reductions + reductions[:1])

    # Two partitions that published each other's rows: the id set is complete and
    # the SUMMED binding is untouched, so only the per-partition comparison against
    # the plan catches it. This mirrors the places validator, whose lack of this
    # check was why places `reconciles` had to be a literal.
    assert len(reductions) >= 2
    swapped = copy.deepcopy(reductions)
    swapped[0]["selected_binding"], swapped[1]["selected_binding"] = (
        swapped[1]["selected_binding"], swapped[0]["selected_binding"],
    )
    assert CONSTRUCTION.combine_bindings(
        [item["selected_binding"] for item in swapped]
    ) == plan["binding"]
    if swapped[0]["selected_binding"] != swapped[1]["selected_binding"]:
        with pytest.raises(ValueError, match="differs from the binding the genesis"):
            CONSTRUCTION.validate_complete_reduction(plan, swapped)

    mismatched = copy.deepcopy(marker)
    mismatched["packs"][0]["directory"]["row_groups"][0]["binding"]["records"] += 1
    with pytest.raises(ValueError, match="differs from its map proof"):
        CONSTRUCTION.reduce_partition(
            partition=plan["partitions"][0],
            markers=[mismatched],
            store=store,
            scratch_root=tmp_path / "mismatch-scratch",
            directory_binary=binaries["directory"],
            encoder_binary=binaries["encoder"],
            verifier_binary=binaries["verifier"],
            limits=limits(),
        )


def run_transform_fixture(tmp_path: Path, binaries, rows: list[dict]) -> dict:
    projected = tmp_path / "input.parquet"
    SPIKE_TEST.write_fixture(projected, rows)
    hydrated = tmp_path / "input.arrow"
    SPIKE_TEST.HARNESS.hydrate_parquet(projected, hydrated, 4)
    limits_path = tmp_path / "limits.json"
    limits_path.write_text(
        json.dumps({"objects": [{"records": len(rows), "row_groups": 1}]}) + "\n"
    )
    report = tmp_path / "report.json"
    subprocess.run(
        [
            str(binaries["transform"]),
            "--input",
            str(hydrated),
            "--output",
            str(tmp_path / "output.arrow"),
            "--report",
            str(report),
            "--source-limits",
            str(limits_path),
        ],
        check=True,
    )
    return json.loads(report.read_text())


def test_nonzero_oversize_and_source_bound_rejections(tmp_path, binaries):
    oversized = base_row(1, number="10", source_row_index=0)
    oversized["unit"] = "x" * (1_048_576 + 1)
    bound = base_row(2, number="11", source_row_index=2)
    report = run_transform_fixture(tmp_path, binaries, [oversized, bound])
    assert report["rejections_by_precedence"]["record_too_large"] == 1
    assert report["rejections_by_precedence"]["invalid_source_locator"] == 1
    assert report["admitted_rows"] == 0


def test_signed_projector_locators_widen_and_negative_rejects(tmp_path, binaries):
    maximum = 2**31 - 1
    rows = [
        base_row(1, number="10", source_row_index=0),
        {
            **base_row(2, number="11", source_row_index=maximum),
            "source_object_index": maximum,
            "source_row_group": maximum,
        },
        {
            **base_row(3, number="12", source_row_index=-1),
            "source_object_index": -1,
            "source_row_group": -1,
        },
    ]
    projected = tmp_path / "signed-locators.parquet"
    SPIKE_TEST.write_fixture(projected, rows)
    hydrated = tmp_path / "signed-locators.arrow"
    SPIKE_TEST.HARNESS.hydrate_parquet(projected, hydrated, 4)
    output = tmp_path / "signed-output.arrow"
    report_path = tmp_path / "signed-report.json"
    subprocess.run(
        [
            str(binaries["transform"]),
            "--input",
            str(hydrated),
            "--output",
            str(output),
            "--report",
            str(report_path),
        ],
        check=True,
    )
    report = json.loads(report_path.read_text())
    assert report["admitted_rows"] == 2
    assert report["rejections_by_precedence"]["invalid_source_locator"] == 1
    import pyarrow.ipc as ipc

    with output.open("rb") as source:
        table = ipc.open_stream(source).read_all()
    assert table.schema.field("source_object_index").type == pa.uint32()
    assert table.schema.field("source_row_group").type == pa.uint32()
    assert table.schema.field("source_row_index").type == pa.uint64()
    assert table["source_object_index"].to_pylist() == [0, maximum]
    assert table["source_row_group"].to_pylist() == [0, maximum]
    assert table["source_row_index"].to_pylist() == [0, maximum]


def test_typed_corruption_is_task_fatal(tmp_path, binaries):
    table = pa.Table.from_arrays(
        [
            pa.array([str(uuid.UUID(int=1))]),
            pa.array(["Main Street"]),
            pa.array(["10"]),
            pa.array([""]),
            pa.array(["02180"]),
            pa.array(["Stoneham"]),
            pa.array([["MA", "Stoneham"]]),
            pa.array(["US"]),
            pa.array([SPIKE_TEST.point({"point": [-71.0, 42.0]})]),
            pa.array([0], type=pa.uint32()),
            pa.array([0], type=pa.int32()),
            pa.array([0], type=pa.int32()),
        ],
        names=list(SPIKE_TEST.HARNESS.PROJECTED_COLUMNS),
    )
    import pyarrow.ipc as ipc

    source = tmp_path / "corrupt.arrow"
    with source.open("wb") as destination:
        with ipc.new_stream(destination, table.schema) as writer:
            writer.write_table(table)
    limits_path = tmp_path / "limits.json"
    limits_path.write_text('{"objects":[{"records":1,"row_groups":1}]}\n')
    result = subprocess.run(
        [
            str(binaries["transform"]),
            "--input",
            str(source),
            "--output",
            str(tmp_path / "output.arrow"),
            "--report",
            str(tmp_path / "report.json"),
            "--source-limits",
            str(limits_path),
        ]
    )
    assert result.returncode != 0


def test_stage_watchdog_fails_closed_when_the_monitor_thread_dies(tmp_path):
    # The watchdog thread is the only thing enforcing the RSS/scratch/wall caps,
    # so a fault inside it must fail the stage instead of dying silently and
    # leaving __exit__ to report success against zeroed evidence.
    watchdog = CONSTRUCTION.StageWatchdog([tmp_path], CONSTRUCTION.Limits())
    watchdog._observe = lambda: (_ for _ in ()).throw(OSError("proc vanished"))
    with pytest.raises(RuntimeError, match="stage watchdog stopped observing"):
        with watchdog:
            pass


def test_stage_watchdog_fails_closed_when_the_thread_never_observes(tmp_path):
    watchdog = CONSTRUCTION.StageWatchdog([tmp_path], CONSTRUCTION.Limits())
    watchdog.thread = SimpleNamespace(start=lambda: None, join=lambda: None)
    with pytest.raises(RuntimeError, match="without recording an observation"):
        with watchdog:
            pass


def test_stage_watchdog_interrupts_and_reports_a_hard_cap_breach(tmp_path):
    interrupted = []
    connection = SimpleNamespace(interrupt=lambda: interrupted.append(True))
    watchdog = CONSTRUCTION.StageWatchdog(
        [tmp_path],
        CONSTRUCTION.Limits(max_rss_bytes=1),
        connection,
    )
    with pytest.raises(RuntimeError, match="RSS exceeded its hard cap"):
        with watchdog:
            watchdog.thread.join(5)
    assert interrupted == [True]
    assert watchdog.evidence()["peak_rss_bytes"] > 0


def test_stage_watchdog_keeps_its_diagnosis_when_interrupt_raises_inside_with(
    tmp_path,
):
    watchdog = CONSTRUCTION.StageWatchdog([tmp_path], CONSTRUCTION.Limits())
    watchdog.thread = SimpleNamespace(start=lambda: None, join=lambda: None)
    watchdog.failure = "whole-stage RSS exceeded its hard cap"
    with pytest.raises(RuntimeError, match="RSS exceeded") as excinfo:
        with watchdog:
            raise RuntimeError("INTERRUPT Error: Interrupted!")
    assert excinfo.value.__cause__ is not None
    assert "Interrupted" in str(excinfo.value.__cause__)


def test_streaming_plan_with_projection_callback_is_byte_identical(tmp_path):
    binding = {
        "records": 2,
        "semantic_sum_a": "1".zfill(64),
        "semantic_sum_b": "2".zfill(64),
    }
    marker = {
        "binding": binding,
        "packs": [
            {
                "directory": {
                    "bucket_summaries": [
                        {
                            "country": "us",
                            "maximum_bucket": 7,
                            "binding": binding,
                        }
                    ]
                }
            }
        ],
    }
    path = tmp_path / "000.json"
    path.write_text(json.dumps(marker))
    visited = []
    streamed = CONSTRUCTION.genesis_plan_streaming(
        [path], row_cap=10, visit_marker=lambda value: visited.append(value)
    )
    in_memory = CONSTRUCTION.genesis_plan([marker], row_cap=10)
    assert CONSTRUCTION.canonical_json(streamed) == CONSTRUCTION.canonical_json(
        in_memory
    )
    assert visited == [marker]


def test_stage_watchdog_tolerates_a_file_vanishing_between_list_and_measure(
    tmp_path, monkeypatch
):
    # DuckDB spill blocks and uploaded packs are unlinked while the guarded
    # stage runs. The old scan called is_file() and then stat(), so a file
    # removed between the two raised out of the monitor loop -- which, now that
    # the loop fails closed, would abort a healthy stage on routine churn.
    # Simulate exactly that window: the first stat of a path succeeds, the
    # second raises.
    (tmp_path / "a.bin").write_bytes(b"a" * 10)
    (tmp_path / "b.bin").write_bytes(b"b" * 5)
    real_stat = Path.stat
    seen: set[str] = set()

    def flaky_stat(self, *args, **kwargs):
        if self.name == "a.bin":
            if self.name in seen:
                raise FileNotFoundError(2, "No such file or directory")
            seen.add(self.name)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    assert CONSTRUCTION.StageWatchdog.disk_bytes([tmp_path]) == 15


def test_stage_watchdog_disk_bytes_counts_only_regular_files(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "a.bin").write_bytes(b"a" * 10)
    (tmp_path / "b.bin").write_bytes(b"b" * 5)
    assert CONSTRUCTION.StageWatchdog.disk_bytes([tmp_path]) == 15
    assert CONSTRUCTION.StageWatchdog.disk_bytes([tmp_path / "missing"]) == 0


# --------------------------------------------------------------------------- #
# Bounded hydration on the reduce path
# --------------------------------------------------------------------------- #
REDUCE_REQUEST = "b" * 64


def _many_pack_map_output(tmp_path, binaries, store, *, rows: int):
    """One map task whose output is MANY packs, published through ``store``.

    Streets and numbers vary per row so `route_hash` (FNV-1a over the eight
    normalized fields, crates/geocoder-construction/src/main.rs:165) spreads across
    the hash space and the genesis plan can cut it into several partitions.
    """
    fixture = [
        dict(
            base_row(index + 1, number=str(100 + index * 7), source_row_index=index),
            street=f"{index % 11} Divided Street",
            postcode=f"0{2000 + index}",
        )
        for index in range(rows)
    ]
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    projected = tmp_path / "many-packs.parquet"
    SPIKE_TEST.write_fixture(projected, fixture)
    source_limits = tmp_path / "many-packs-limits.json"
    source_limits.write_text(
        json.dumps({"objects": [{"records": rows, "row_groups": 1}]}) + "\n"
    )
    packed = CONSTRUCTION.Limits(
        max_input_rows=rows,
        # Small enough that one map task emits MANY packs, which is the whole point:
        # a single-pack fixture cannot distinguish a bounded reducer from an
        # unbounded one, because peak equals total either way.
        max_pack_rows=4,
        parquet_row_group_rows=2_048,
        max_rss_bytes=1_024**3,
        max_scratch_bytes=512 * 1024**2,
        max_output_bytes=128 * 1024**2,
        max_serving_bytes=16 * 1024**2,
        wall_seconds=120,
        duckdb_memory_limit="256MB",
        duckdb_threads=2,
        allow_unpinned_duckdb=True,
    )
    marker = CONSTRUCTION.map_task(
        input_path=projected,
        source_limits=source_limits,
        store=store,
        scratch_root=tmp_path / "many-packs-scratch",
        request_sha256=REDUCE_REQUEST,
        task_id="many-packs",
        transform_binary=binaries["transform"],
        directory_binary=binaries["directory"],
        limits=packed,
    )
    return marker, packed


def _staged_store(tmp_path, local_name: str, staging_root):
    """A `StagedObjectStore` over a filesystem staging backend, no credentials."""
    local = CONSTRUCTION.LocalObjectStore(tmp_path / local_name)
    backend = STAGING.staging_backend(store_root=staging_root)
    return STAGING.StagedObjectStore(
        local, backend, STAGING.staging_prefix(REDUCE_REQUEST, "addresses")
    )


def _reduce_all(plan, marker, store, tmp_path, binaries, limits_value, *, retain: bool):
    """Reduce every partition on ONE store, as a single batched reducer job would.

    ``retain`` mirrors ``construction_v1_hosted._batch_retention``: the pack keys
    later partitions of the same job still need are kept, everything else is released
    at its last use.
    """
    partitions = plan["partitions"]
    needs = [CONSTRUCTION.partition_pack_keys([marker], item) for item in partitions]
    reductions = []
    for index, partition in enumerate(partitions):
        future: set[str] = set()
        if retain:
            for later in needs[index + 1 :]:
                future |= later
        reductions.append(
            CONSTRUCTION.reduce_partition(
                partition=partition,
                markers=[marker],
                store=store,
                scratch_root=tmp_path / f"reduce-{index}",
                directory_binary=binaries["directory"],
                encoder_binary=binaries["encoder"],
                verifier_binary=binaries["verifier"],
                limits=limits_value,
                retain_keys=frozenset(future),
            )
        )
    return reductions


def _comparable(reductions):
    """The parts of a reduction that must not depend on cache behaviour."""
    return [
        {
            key: item[key]
            for key in (
                "partition",
                "selected_row_groups",
                "fetched_binding",
                "selected_binding",
                "discarded_binding",
                "artifact",
                "verification",
            )
        }
        for item in reductions
    ]


def test_reduce_partition_releases_packs_and_bounds_peak_resident(tmp_path, binaries):
    """Peak HYDRATED input resident stays ~one pack while many packs are reduced.

    This is the test the unreleased-pack defect needed and did not have. Every
    existing reduce test asserts on OUTPUT, and the output was always correct: the
    address reducer produced byte-identical artifacts while holding every pack it had
    ever opened until the process exited, so `staged_peak_resident_bytes` equalled
    `staged_bytes_hydrated` exactly. On the old code the peak assertion below fails
    while every binding, artifact and verification assertion still passes.
    """
    staging_root = tmp_path / "staging"
    map_store = _staged_store(tmp_path, "local-map", staging_root)
    marker, packed = _many_pack_map_output(tmp_path, binaries, map_store, rows=48)
    pack_sizes = [pack["object"]["bytes"] for pack in marker["packs"]]
    # Fail closed on a degenerate fixture rather than passing vacuously: with one or
    # two packs, peak == total however promptly the reducer evicts.
    assert len(pack_sizes) >= 6

    plan = CONSTRUCTION.genesis_plan([marker], row_cap=8)
    assert len(plan["partitions"]) >= 4

    # A FRESH local cache: the reducer runs on a runner that has never seen the map
    # output, exactly as a hosted reduce job does, so every pack must be hydrated.
    reduce_store = _staged_store(tmp_path, "local-reduce", staging_root)
    reductions = _reduce_all(
        plan, marker, reduce_store, tmp_path, binaries, packed, retain=False
    )
    evidence = reduce_store.evidence()

    assert evidence["staged_bytes_hydrated"] > 0
    # The fix: something was actually evicted.
    assert evidence["staged_objects_released"] > 0
    # The bound, asserted strictly. This is the line that fails on the old code.
    assert (
        evidence["staged_peak_resident_bytes"] < evidence["staged_bytes_hydrated"]
    ), evidence
    # And bounded by the DATA, not merely smaller than the total: a reducer that
    # released only its last pack would satisfy a `<` alone. No partition of this plan
    # spans more than a handful of packs, so the peak must stay within a couple of the
    # largest one rather than growing with the number of packs reduced.
    assert evidence["staged_peak_resident_bytes"] <= 2 * max(pack_sizes), evidence
    # Nothing is left resident when the job ends.
    assert evidence["staged_bytes_released"] == evidence["staged_bytes_hydrated"]

    # The SAME map output reduced through a plain LocalObjectStore, which has no
    # `release` method at all -- that absence is precisely why the reducer reaches it
    # through `getattr`, since there the local directory IS the store and evicting
    # would delete the map output. The objects are copied out of the staging tree by
    # key so the inputs are bit-identical, and the published artifacts and every
    # binding must come out identical too.
    local_root = tmp_path / "local-only"
    shutil.copytree(staging_root / map_store.prefix, local_root)
    for sidecar in local_root.rglob("*.metadata.json"):
        sidecar.unlink()
    local_only = CONSTRUCTION.LocalObjectStore(local_root)
    assert not hasattr(local_only, "release")
    local_reductions = _reduce_all(
        plan, marker, local_only, tmp_path / "local-only-reduce",
        binaries, packed, retain=False,
    )
    assert _comparable(local_reductions) == _comparable(reductions)
    # A local-only run must not have deleted its own store.
    for pack in marker["packs"]:
        assert local_only.path(pack["object"]["key"]).is_file()


def test_reduce_fails_closed_when_hydrated_packs_exceed_the_scratch_cap(
    tmp_path, binaries
):
    """The hydrated cache is under a DECLARED cap, not merely under a cache policy.

    `release()` bounds the peak, but that bound is emergent: peak resident is
    `(map tasks holding the partition's country) x pack bytes` and is
    batch-INDEPENDENT above batch 1, so lowering `--max-reduce-jobs` -- which the docs
    recommend for R2 reads -- does NOT reduce it. Unenforced, an under-provisioned
    runner meets that as ENOSPC with no diagnosis, on a plan the plan phase certified.
    Here the cap is set below one pack, so the very first hydration must abort.
    """
    staging_root = tmp_path / "staging"
    map_store = _staged_store(tmp_path, "local-map", staging_root)
    marker, packed = _many_pack_map_output(tmp_path, binaries, map_store, rows=48)
    plan = CONSTRUCTION.genesis_plan([marker], row_cap=8)
    smallest = min(pack["object"]["bytes"] for pack in marker["packs"])

    tiny = dataclasses.replace(packed, max_scratch_bytes=smallest - 1)
    reduce_store = _staged_store(tmp_path, "local-reduce", staging_root)
    with pytest.raises(ValueError, match="exceed the stage scratch cap"):
        CONSTRUCTION.reduce_partition(
            partition=plan["partitions"][0],
            markers=[marker],
            store=reduce_store,
            scratch_root=tmp_path / "reduce-tiny",
            directory_binary=binaries["directory"],
            encoder_binary=binaries["encoder"],
            verifier_binary=binaries["verifier"],
            limits=tiny,
        )

    # The other half: a local-only store is EXEMPT from the new resident accounting.
    # The directory IS the map output there, not a cache, so counting it against a
    # scratch cap would fail a legitimate offline run.
    #
    # This leg proves the exemption STRUCTURALLY -- no `resident_bytes` attribute, so
    # `check_resident` cannot fire -- and then proves the reduce still completes. It
    # deliberately does NOT re-run under `tiny`: `max_scratch_bytes` also bounds child
    # subprocesses through `run_bounded`, and `tiny` is smaller than the workspace, so a
    # local-only run under it fails with "child scratch exceeded its hard cap" from that
    # PRE-EXISTING guard rather than passing. There is no cap window that isolates the
    # new check from the old one, which is why the exemption is asserted on the
    # interface rather than on a cap value.
    local_root = tmp_path / "local-only"
    shutil.copytree(staging_root / map_store.prefix, local_root)
    for sidecar in local_root.rglob("*.metadata.json"):
        sidecar.unlink()
    local_only = CONSTRUCTION.LocalObjectStore(local_root)
    assert not hasattr(local_only, "resident_bytes")
    reduction = CONSTRUCTION.reduce_partition(
        partition=plan["partitions"][0],
        markers=[marker],
        store=local_only,
        scratch_root=tmp_path / "reduce-local-tiny",
        directory_binary=binaries["directory"],
        encoder_binary=binaries["encoder"],
        verifier_binary=binaries["verifier"],
        limits=packed,
    )
    assert reduction["selected_row_groups"] > 0


def test_reduce_batch_retention_keeps_boundary_packs_without_changing_output(
    tmp_path, binaries
):
    """Retaining a batch's future packs cuts re-fetches and leaves output identical.

    A pack straddling a partition boundary is selected by both sides of it, so
    releasing purely per-partition re-fetches and re-verifies it once per partition.
    `retain_keys` is what lets a batched reducer job pay for each pack once while
    still keeping its peak bounded.
    """
    staging_root = tmp_path / "staging"
    map_store = _staged_store(tmp_path, "local-map", staging_root)
    marker, packed = _many_pack_map_output(tmp_path, binaries, map_store, rows=48)
    plan = CONSTRUCTION.genesis_plan([marker], row_cap=8)

    per_partition = _staged_store(tmp_path, "local-a", staging_root)
    without = _reduce_all(
        plan, marker, per_partition, tmp_path / "a", binaries, packed, retain=False
    )
    batched = _staged_store(tmp_path, "local-b", staging_root)
    with_retention = _reduce_all(
        plan, marker, batched, tmp_path / "b", binaries, packed, retain=True
    )

    # Each pack the job needs is fetched exactly once when the job holds what it
    # still needs, and the distinct-pack count is the floor no cache policy can beat.
    needed = set()
    for partition in plan["partitions"]:
        needed |= CONSTRUCTION.partition_pack_keys([marker], partition)
    assert batched.evidence()["staged_objects_hydrated"] == len(needed)
    assert (
        per_partition.evidence()["staged_objects_hydrated"]
        >= batched.evidence()["staged_objects_hydrated"]
    )
    # Retention must not turn the reducer back into an unbounded one.
    retained_evidence = batched.evidence()
    assert (
        retained_evidence["staged_peak_resident_bytes"]
        <= retained_evidence["staged_bytes_hydrated"]
    )
    assert retained_evidence["staged_objects_released"] == len(needed)
    # The whole point: cache policy is invisible in the output.
    assert _comparable(with_retention) == _comparable(without)
