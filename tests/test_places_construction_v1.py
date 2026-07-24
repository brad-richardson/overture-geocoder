from __future__ import annotations

import json
import importlib.util
import struct
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest


pa = pytest.importorskip("pyarrow")
ipc = pytest.importorskip("pyarrow.ipc")
pq = pytest.importorskip("pyarrow.parquet")
pytest.importorskip("pyarrow.compute")  # register pa.compute; not auto-imported in pyarrow 25

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/places_construction_v1.json"


def geometry(row: dict) -> bytes:
    if "geometry_hex" in row:
        return bytes.fromhex(row["geometry_hex"])
    longitude, latitude = row.get("point", [0.0, 0.0])
    return b"\x01" + struct.pack("<Idd", 1, longitude, latitude)


def write_fixture(path: Path, rows: list[dict], *, row_group_size: int = 4) -> None:
    def values(name: str, default):
        return [row.get(name, default) for row in rows]

    table = pa.Table.from_arrays(
        [
            pa.array(values("id", None), type=pa.string()),
            pa.array(values("primary_name", ""), type=pa.string()),
            pa.array(values("common_names", []), type=pa.list_(pa.string())),
            pa.array(values("brand_name", ""), type=pa.string()),
            pa.array(values("category", ""), type=pa.string()),
            pa.array(values("locality", ""), type=pa.string()),
            pa.array(values("region", ""), type=pa.string()),
            pa.array(values("country", ""), type=pa.string()),
            pa.array(values("confidence", 0.5), type=pa.float64()),
            pa.array(values("operating_status", "open"), type=pa.string()),
            pa.array([geometry(row) for row in rows], type=pa.binary()),
            pa.array(values("source_object_index", 0), type=pa.int32()),
            pa.array(values("source_row_group", 0), type=pa.int32()),
            pa.array(values("source_row_index", 0), type=pa.int32()),
        ],
        names=[
            "id",
            "primary_name",
            "common_names",
            "brand_name",
            "category",
            "locality",
            "region",
            "country",
            "confidence",
            "operating_status",
            "geometry",
            "source_object_index",
            "source_row_group",
            "source_row_index",
        ],
    )
    pq.write_table(table, path, row_group_size=row_group_size)


@pytest.fixture(scope="module")
def construction_binaries() -> dict[str, Path]:
    names = [
        "places-transform-v1",
        "places-proof-directory",
        "places-serving-encode-v1",
        "places-serving-verify-v1",
    ]
    subprocess.run(
        ["cargo", "build", "-p", "geocoder-construction", "--bins"],
        cwd=ROOT / "crates",
        check=True,
    )
    return {name: ROOT / "crates/target/debug" / name for name in names}


@pytest.fixture(scope="module")
def transform_binary(construction_binaries) -> Path:
    return construction_binaries["places-transform-v1"]


@pytest.fixture(scope="module")
def construction_module():
    spec = importlib.util.spec_from_file_location(
        "places_construction_v1_test", ROOT / "scripts/places_construction_v1.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def baseline_module():
    spec = importlib.util.spec_from_file_location(
        "baseline_places_construction_v1_test",
        ROOT / "scripts/baseline_places_construction_v1.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_transform(
    tmp_path: Path, binary: Path, rows: list[dict], *, use_limits: bool = True
):
    projected = tmp_path / "places.parquet"
    write_fixture(projected, rows)
    hydrated = tmp_path / "places.arrow"
    parquet = pq.ParquetFile(projected)
    with hydrated.open("wb") as destination:
        writer = None
        try:
            for batch in parquet.iter_batches(batch_size=4):
                if writer is None:
                    writer = ipc.new_stream(destination, batch.schema)
                writer.write_batch(batch)
        finally:
            if writer:
                writer.close()
    report = tmp_path / "report.json"
    output = tmp_path / "terms.arrow"
    limits = tmp_path / "limits.json"
    limits.write_text(
        json.dumps({"objects": [{"records": len(rows), "row_groups": 3}]})
    )
    command = [
        str(binary),
        "--input",
        str(hydrated),
        "--output",
        str(output),
        "--report",
        str(report),
    ]
    if use_limits:
        command.extend(["--source-limits", str(limits)])
    subprocess.run(command, check=True)
    with output.open("rb") as source:
        table = ipc.open_stream(source).read_all()
    return json.loads(report.read_text()), table


def test_hand_authored_places_transform_contract(tmp_path, transform_binary):
    fixture = json.loads(FIXTURE.read_text())
    report, table = run_transform(tmp_path, transform_binary, fixture["rows"])
    expected = fixture["expected"]
    assert report["input_features"] == expected["input_features"]
    assert report["admitted_features"] == expected["admitted_features"]
    assert report["emitted_term_rows"] == expected["emitted_term_rows"]
    assert report["rejections_by_precedence"] == expected["rejections_by_precedence"]
    assert table.num_rows == expected["emitted_term_rows"]

    first_id = uuid.UUID(int=1).bytes
    first = table.filter(pa.compute.equal(table["feature_id"], first_id))
    assert first["confidence_rank"].to_pylist() == [204] * 11
    assert (
        dict(zip(first["token"].to_pylist(), first["field_mask"].to_pylist()))
        == expected["first_feature"]["terms"]
    )
    for field, value in expected["shared_route"].items():
        assert set(first[field].to_pylist()) == {value}

    duplicate_id = uuid.UUID(int=2).bytes
    duplicate = table.filter(pa.compute.equal(table["feature_id"], duplicate_id))
    assert duplicate.num_rows == 10
    copies: dict[int, dict[str, int]] = {}
    for token, mask, row_index in zip(
        duplicate["token"].to_pylist(),
        duplicate["field_mask"].to_pylist(),
        duplicate["source_row_index"].to_pylist(),
        strict=True,
    ):
        copies.setdefault(row_index, {})[token] = mask
    assert sorted(copies) == expected["duplicate_feature"]["ordered_source_row_indexes"]
    assert all(
        value == expected["duplicate_feature"]["terms"] for value in copies.values()
    )


def test_places_locator_bounds_and_typed_corruption(tmp_path, transform_binary):
    maximum = 2**31 - 1
    good = {
        "id": str(uuid.UUID(int=1)),
        "primary_name": "Maximum",
        "confidence": 1.0,
        "point": [180.0, 90.0],
        "source_object_index": maximum,
        "source_row_group": maximum,
        "source_row_index": maximum,
    }
    negative = {**good, "id": str(uuid.UUID(int=2)), "source_row_index": -1}
    report, table = run_transform(
        tmp_path, transform_binary, [good, negative], use_limits=False
    )
    assert report["admitted_features"] == 1
    assert report["rejections_by_precedence"]["invalid_source_locator"] == 1
    assert set(table["source_object_index"].to_pylist()) == {maximum}
    assert table.schema.field("source_row_index").type == pa.uint64()

    corrupt = tmp_path / "corrupt.arrow"
    projected = tmp_path / "corrupt.parquet"
    write_fixture(projected, [good])
    source_table = pq.read_table(projected).set_column(
        11, "source_object_index", pa.array([0], type=pa.uint32())
    )
    with corrupt.open("wb") as destination:
        with ipc.new_stream(destination, source_table.schema) as writer:
            writer.write_table(source_table)
    result = subprocess.run(
        [
            str(transform_binary),
            "--input",
            str(corrupt),
            "--output",
            str(tmp_path / "bad-output.arrow"),
            "--report",
            str(tmp_path / "bad-report.json"),
        ]
    )
    assert result.returncode != 0


def test_python_baseline_matches_rust_semantic_binding(
    tmp_path, transform_binary, baseline_module
):
    rows = json.loads(FIXTURE.read_text())["rows"]
    rust, _ = run_transform(tmp_path, transform_binary, rows)
    projected = tmp_path / "baseline.parquet"
    write_fixture(projected, rows)
    source_limits = tmp_path / "baseline-limits.json"
    source_limits.write_text(
        json.dumps({"objects": [{"records": len(rows), "row_groups": 3}]})
    )
    baseline = baseline_module.run(projected, source_limits)
    for field in (
        "input_features",
        "admitted_features",
        "multilingual_features",
        "cjk_features",
        "emitted_term_rows",
        "rejections_by_precedence",
        "semantic_sum_a",
        "semantic_sum_b",
    ):
        assert baseline[field] == rust[field]


def decode_serving(path: Path, mode: str) -> list[dict]:
    data = path.read_bytes()
    assert data[:8] == (b"PLRV0002" if mode == "routed" else b"PLHD0002")
    expected = struct.unpack_from("<Q", data, 8)[0]
    index_offset = struct.unpack_from("<Q", data, 16)[0]
    assert struct.unpack_from("<I", data, 28)[0] == 0
    position = 32
    output = []

    def text(entry: bytes, at: int) -> tuple[str, int]:
        length = struct.unpack_from("<H", entry, at)[0]
        at += 2
        return entry[at : at + length].decode(), at + length

    while position < index_offset:
        length = struct.unpack_from("<I", data, position)[0]
        position += 4
        entry = data[position : position + length]
        position += length
        at = 0
        token, at = text(entry, at)
        cell = None
        if mode == "routed":
            cell, at = text(entry, at)
        mask, rank = struct.unpack_from("<BB", entry, at)
        at += 2
        identifier = str(uuid.UUID(bytes=entry[at : at + 16]))
        at += 16
        longitude, latitude, object_index, row_group, row_index = struct.unpack_from(
            "<ddIIQ", entry, at
        )
        at += struct.calcsize("<ddIIQ")
        display = []
        for _ in range(6):
            value, at = text(entry, at)
            display.append(value)
        assert at == len(entry)
        output.append(
            {
                "token": token,
                "cell": cell,
                "mask": mask,
                "rank": rank,
                "id": identifier,
                "longitude": longitude,
                "latitude": latitude,
                "object": object_index,
                "row_group": row_group,
                "row_index": row_index,
                "display": display,
            }
        )
    assert len(output) == expected
    return output


def test_complete_local_places_slice_interrupt_resume_and_reconcile(
    tmp_path, construction_binaries, construction_module
):
    module = construction_module
    rows = []
    for index in range(450):
        rows.append(
            {
                "id": str(uuid.UUID(int=1_000 + index)),
                "primary_name": f"Common Place {index}",
                "category": "library",
                "locality": "Town",
                "country": "XX",
                "confidence": 1.0 - (index % 10) / 20,
                "point": [0.0, 0.0] if index < 225 else [-90.0, -45.0],
                "source_row_index": index,
            }
        )
    source = tmp_path / "source.parquet"
    write_fixture(source, rows, row_group_size=64)
    parquet = pq.ParquetFile(source)
    source_limits = tmp_path / "source-limits.json"
    source_limits.write_text(
        json.dumps(
            {
                "objects": [
                    {
                        "records": len(rows),
                        "row_groups": parquet.metadata.num_row_groups,
                    }
                ]
            }
        )
    )
    limits = module.Limits(
        max_input_rows=500,
        max_pack_rows=1_500,
        parquet_row_group_rows=700,
        max_rss_bytes=2 * 1024**3,
        max_scratch_bytes=2 * 1024**3,
        max_output_bytes=512 * 1024**2,
        wall_seconds=120,
        allow_unpinned_duckdb=True,
    )
    store = module.A.LocalObjectStore(tmp_path / "store")
    arguments = {
        "input_path": source,
        "source_limits": source_limits,
        "store": store,
        "scratch_root": tmp_path / "scratch",
        "request_sha256": "ab" * 32,
        "task_id": "places-a",
        "transform_binary": construction_binaries["places-transform-v1"],
        "proof_binary": construction_binaries["places-proof-directory"],
        "limits": limits,
    }
    with pytest.raises(RuntimeError, match="local_write"):
        module.map_task(**arguments, failpoint="local_write")
    assert not store.path(module.marker_key("places-a")).exists()
    with pytest.raises(RuntimeError, match="after_objects"):
        module.map_task(**arguments, failpoint="after_objects")
    assert not store.path(module.marker_key("places-a")).exists()
    with pytest.raises(RuntimeError, match="before_marker"):
        module.map_task(**arguments, failpoint="before_marker")
    assert not store.path(module.marker_key("places-a")).exists()

    marker = module.map_task(**arguments)
    assert marker["admitted_existing"] is False
    assert marker["binding"]["records"] == 2_700
    assert len(marker["packs"]) == 2
    assert sum(len(pack["directory"]["row_groups"]) for pack in marker["packs"]) >= 2
    resumed = module.map_task(**arguments)
    assert resumed["admitted_existing"] is True
    assert resumed["binding"] == marker["binding"]
    marker_b = module.map_task(
        **{**arguments, "request_sha256": "cd" * 32, "task_id": "places-b"}
    )
    combined_marker_binding = module.A.combine_bindings(
        [marker["binding"], marker_b["binding"]]
    )

    adaptive_limits = replace(
        limits,
        partition_term_rows=500,
        partition_estimated_bytes=64 * 1024**2,
        partition_distinct_tokens=100,
    )
    adaptive = module.adaptive_genesis_plan(
        [marker, marker_b],
        store=store,
        scratch_root=tmp_path / "adaptive-scratch",
        limits=adaptive_limits,
    )
    assert adaptive["predecessor"] is None
    assert adaptive["binding"] == combined_marker_binding
    assert any(item["ownership"]["depth"] > 0 for item in adaptive["partitions"])
    assert all(item["term_rows"] <= 500 for item in adaptive["partitions"])
    subdivided = next(
        item for item in adaptive["partitions"] if item["ownership"]["depth"] > 0
    )
    subdivided_reduction = module.reduce_partition(
        partition=subdivided,
        plan=adaptive,
        markers=[marker, marker_b],
        store=store,
        scratch_root=tmp_path / "adaptive-reduce",
        encoder_binary=construction_binaries["places-serving-encode-v1"],
        verifier_binary=construction_binaries["places-serving-verify-v1"],
        limits=adaptive_limits,
    )
    assert subdivided_reduction["binding"] == subdivided["binding"]
    assert subdivided_reduction["streaming_ingestion"]["full_table_read_all"] is False

    plan = module.genesis_plan([marker], row_cap=2_000)
    assert [item["partition_cell"] for item in plan["partitions"]] == ["4040", "8080"]
    assert plan["binding"] == marker["binding"]
    reductions = [
        module.reduce_partition(
            partition=partition,
            plan=plan,
            markers=[marker],
            store=store,
            scratch_root=tmp_path / "reduce-scratch",
            encoder_binary=construction_binaries["places-serving-encode-v1"],
            verifier_binary=construction_binaries["places-serving-verify-v1"],
            limits=limits,
        )
        for partition in plan["partitions"]
    ]
    assert (
        module.A.combine_bindings([item["binding"] for item in reductions])
        == marker["binding"]
    )
    assert any(
        item["discarded"]["records"] > 0
        for reduction in reductions
        for item in reduction["reconciled_row_groups"]
    )

    routed = []
    for reduction in reductions:
        values = decode_serving(store.path(reduction["routed_object"]["key"]), "routed")
        assert {value["cell"] for value in values} == {
            reduction["partition"]["partition_cell"]
        }
        routed.extend(values)
    assert len(routed) == marker["binding"]["records"]
    common = [value for value in routed if value["token"] == "common"]
    assert len(common) == 450
    assert common[0]["rank"] == 255

    head = module.build_global_head_from_markers(
        markers=[marker, marker_b],
        store=store,
        scratch_root=tmp_path / "head-scratch",
        encoder_binary=construction_binaries["places-serving-encode-v1"],
        verifier_binary=construction_binaries["places-serving-verify-v1"],
        limits=limits,
    )
    head_rows = decode_serving(store.path(head["head_object"]["key"]), "head")
    common_head = [value for value in head_rows if value["token"] == "common"]
    assert len(common_head) == 10
    assert [value["rank"] for value in common_head] == sorted(
        (value["rank"] for value in common_head), reverse=True
    )
    assert head["input_binding"] == combined_marker_binding

    with pytest.raises(ValueError, match="marker set is missing or extra"):
        module.reduce_partition(
            partition=plan["partitions"][0],
            plan=plan,
            markers=[],
            store=store,
            scratch_root=tmp_path / "bad-reduce",
            encoder_binary=construction_binaries["places-serving-encode-v1"],
            verifier_binary=construction_binaries["places-serving-verify-v1"],
            limits=limits,
        )

    artifact = store.path(reductions[0]["routed_object"]["key"])
    corrupt = tmp_path / "corrupt.plrv"
    corrupt.write_bytes(artifact.read_bytes()[:-1])
    result = subprocess.run(
        [
            str(construction_binaries["places-serving-verify-v1"]),
            "--input",
            str(corrupt),
            "--mode",
            "routed",
        ]
    )
    assert result.returncode != 0
