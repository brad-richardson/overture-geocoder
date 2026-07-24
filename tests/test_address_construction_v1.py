from __future__ import annotations

import importlib.util
import copy
import json
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
