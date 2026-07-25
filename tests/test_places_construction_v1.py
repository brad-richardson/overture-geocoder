from __future__ import annotations

import collections
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
GOLDEN_FIXTURE = ROOT / "tests/fixtures/places_tokenizer_v4_golden.json"


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


def test_ipc_batch_constants_are_derived_and_within_cap(construction_module):
    module = construction_module
    # The hydrate input batch is derived from the IPC cap by construction, so a
    # term batch (one output batch per input batch, MAX_TERMS_PER_FEATURE terms
    # each in the worst case) can never exceed the IPC cap that ingest enforces.
    assert module.HYDRATE_BATCH_ROWS == (
        module.MAX_IPC_BATCH_ROWS // module.MAX_TERMS_PER_FEATURE
    )
    assert (
        module.HYDRATE_BATCH_ROWS * module.MAX_TERMS_PER_FEATURE
        <= module.MAX_IPC_BATCH_ROWS
    )
    assert module.MAX_TERMS_PER_FEATURE >= 1
    # hydrate() must default to the derived batch, and ingest() must reject any
    # batch over the shared cap -- the two former hard-coded 65,536 literals now
    # come from one constant.
    import inspect

    signature = inspect.signature(module.hydrate)
    assert signature.parameters["batch_rows"].default == module.HYDRATE_BATCH_ROWS
    # The single cap constant must equal the frozen evidence-spec value.
    spec = json.loads(
        (ROOT / "benchmarks/places-construction-v1-evidence-spec.json").read_text()
    )
    assert (
        module.MAX_IPC_BATCH_ROWS
        == spec["acceptance_gates"]["resources"]["maximum_ipc_batch_rows"]
    )


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


def test_tokenizer_v4_golden_vectors(baseline_module):
    fixture = json.loads(GOLDEN_FIXTURE.read_text())
    assert fixture["tokenizer_version"] == "nfkd-lower-stripmark-cjk-bigram-v4"
    for vector in fixture["token_vectors"]:
        assert baseline_module.tokens(vector["input"]) == set(vector["expected"]), (
            f"golden token vector {vector['name']} diverged"
        )


def test_tokenizer_v4_golden_rust_baseline_equivalence(
    tmp_path, transform_binary, baseline_module
):
    fixture = json.loads(GOLDEN_FIXTURE.read_text())
    rows = fixture["rows"]
    rust, table = run_transform(tmp_path, transform_binary, rows)

    projected = tmp_path / "golden-baseline.parquet"
    write_fixture(projected, rows)
    source_limits = tmp_path / "golden-baseline-limits.json"
    source_limits.write_text(
        json.dumps({"objects": [{"records": len(rows), "row_groups": 3}]})
    )
    baseline = baseline_module.run(projected, source_limits)

    # Rust and the patched baseline must now agree byte-for-byte, and both must
    # match the frozen golden values so neither side can silently drift.
    expected = fixture["expected"]
    for field in (
        "input_features",
        "admitted_features",
        "multilingual_features",
        "cjk_features",
        "emitted_term_rows",
        "semantic_sum_a",
        "semantic_sum_b",
    ):
        assert rust[field] == baseline[field] == expected[field], field

    tokens_by_feature: dict[str, set[str]] = {}
    for token, feature_id in zip(
        table["token"].to_pylist(), table["feature_id"].to_pylist(), strict=True
    ):
        tokens_by_feature.setdefault(str(uuid.UUID(bytes=feature_id)), set()).add(token)
    for feature_id, expected_tokens in expected["per_feature_tokens"].items():
        assert tokens_by_feature[feature_id] == set(expected_tokens), feature_id


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
    # Before the map-side shuffle this asserted that SOME row group had
    # discarded rows -- i.e. that the reducer necessarily read rows belonging to
    # another partition. That is now the thing the design removes: fragments are
    # keyed by a hash bucket of the cell, so a reducer opens only fragments whose
    # bucket matches its own cell, and in this two-cell fixture each bucket holds
    # exactly one cell, so nothing is discarded at all.
    #
    # Assert the properties that survive and matter: every row group the reducer
    # touched reconciles selected+discarded against its recorded binding, and no
    # reducer touched a fragment outside its cell's bucket.
    for reduction in reductions:
        cell = reduction["partition"]["partition_cell"]
        bucket = module.shuffle_bucket(module.cell_partition_key(cell))
        touched = {item["pack_sha256"] for item in reduction["reconciled_row_groups"]}
        allowed = {
            pack["object"]["sha256"]
            for pack in marker["packs"]
            if pack["shuffle_bucket"] == bucket
        }
        assert touched, "a reducer must read at least one fragment"
        assert touched <= allowed, (
            f"partition {cell} read fragments outside bucket {bucket}"
        )
        for item in reduction["reconciled_row_groups"]:
            combined = module.A.combine_bindings([item["selected"], item["discarded"]])
            assert combined["records"] > 0

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


_VOCAB = [
    "cafe",
    "bakery",
    "museum",
    "harbor",
    "market",
    "garden",
    "temple",
    "library",
    "theater",
    "bridge",
    "castle",
    "gallery",
    "station",
    "clinic",
    "studio",
    "arcade",
]


def _sharded_head_marker(module, binaries, tmp_path, store, task_id, seed, count):
    rows = []
    for index in range(count):
        words = " ".join(
            _VOCAB[(index + offset) % len(_VOCAB)] for offset in range(3)
        )
        rows.append(
            {
                "id": str(uuid.UUID(int=int(seed, 16) * 1_000_000 + index)),
                "primary_name": f"{words} {index}",
                "category": "library",
                "locality": "Town",
                "country": "XX",
                "confidence": 1.0 - (index % 8) / 20,
                "point": [0.0, 0.0],
                "source_row_index": index,
            }
        )
    source = tmp_path / f"{task_id}.parquet"
    write_fixture(source, rows, row_group_size=64)
    parquet = pq.ParquetFile(source)
    (tmp_path / f"{task_id}-limits.json").write_text(
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
        max_input_rows=1_000,
        max_pack_rows=2_000,
        parquet_row_group_rows=700,
        max_output_bytes=512 * 1024**2,
        wall_seconds=120,
        allow_unpinned_duckdb=True,
    )
    marker = module.map_task(
        input_path=source,
        source_limits=tmp_path / f"{task_id}-limits.json",
        store=store,
        scratch_root=tmp_path / f"{task_id}-scratch",
        request_sha256=seed * 32,
        task_id=task_id,
        transform_binary=binaries["places-transform-v1"],
        proof_binary=binaries["places-proof-directory"],
        limits=limits,
    )
    return marker, limits


def test_sharded_global_head_partitions_reconciles_and_serves(
    tmp_path, construction_binaries, construction_module
):
    module = construction_module
    store = module.A.LocalObjectStore(tmp_path / "store")
    marker_a, limits = _sharded_head_marker(
        module, construction_binaries, tmp_path, store, "places-a", "ab", 160
    )
    marker_b, _ = _sharded_head_marker(
        module, construction_binaries, tmp_path, store, "places-b", "cd", 140
    )

    result = module.build_sharded_global_head_from_markers(
        markers=[marker_a, marker_b],
        store=store,
        scratch_root=tmp_path / "head-scratch",
        encoder_binary=construction_binaries["places-serving-encode-v1"],
        verifier_binary=construction_binaries["places-serving-verify-v1"],
        limits=limits,
        shard_bits=4,
    )
    assert result["shard_count"] == 16
    # The vocabulary spreads tokens across several shards.
    assert result["populated_shards"] >= 2
    # build_* already ran the sharded verifier; totals reconcile.
    assert result["total_records"] > 0
    assert result["total_index_entries"] > 0

    # Every entry lands in the shard its index hash addresses, and the union of
    # all shard entries equals a single un-sharded head over the same merge.
    seen_records = 0
    union_tokens = set()
    for shard in result["shard_objects"]:
        head_rows = decode_serving(store.path(shard["key"]), "head")
        seen_records += len(head_rows)
        for row in head_rows:
            assert module.head_shard_of(row["token"], 4) == shard["shard_id"]
            union_tokens.add(row["token"])
    assert seen_records == result["total_records"]
    assert len(union_tokens) == result["total_index_entries"]

    # Worker-style serving path: token -> shard id -> that shard answers.
    sample_token = sorted(union_tokens)[0]
    target = module.head_shard_of(sample_token, 4)
    shard_key = next(
        shard["key"] for shard in result["shard_objects"] if shard["shard_id"] == target
    )
    hits = [
        row
        for row in decode_serving(store.path(shard_key), "head")
        if row["token"] == sample_token
    ]
    assert hits and hits[0]["rank"] == max(row["rank"] for row in hits)


def test_sharded_global_head_merge_is_order_independent(
    tmp_path, construction_binaries, construction_module
):
    module = construction_module
    store = module.A.LocalObjectStore(tmp_path / "store")
    marker_a, limits = _sharded_head_marker(
        module, construction_binaries, tmp_path, store, "places-a", "ab", 130
    )
    marker_b, _ = _sharded_head_marker(
        module, construction_binaries, tmp_path, store, "places-b", "cd", 170
    )

    def build(order):
        return module.build_sharded_global_head_from_markers(
            markers=order,
            store=store,
            scratch_root=tmp_path / "head-scratch",
            encoder_binary=construction_binaries["places-serving-encode-v1"],
            verifier_binary=construction_binaries["places-serving-verify-v1"],
            limits=limits,
            shard_bits=5,
        )

    forward = build([marker_a, marker_b])
    reverse = build([marker_b, marker_a])
    # Associative/idempotent tree-merge: fold order cannot change the bound set.
    for field in (
        "total_records",
        "total_index_entries",
        "populated_shards",
        "head_sum_a",
        "head_sum_b",
    ):
        assert forward[field] == reverse[field]


def test_sharded_head_manifest_tamper_fails_closed(
    tmp_path, construction_binaries, construction_module
):
    module = construction_module
    store = module.A.LocalObjectStore(tmp_path / "store")
    marker, limits = _sharded_head_marker(
        module, construction_binaries, tmp_path, store, "places-a", "ab", 120
    )
    result = module.build_sharded_global_head_from_markers(
        markers=[marker],
        store=store,
        scratch_root=tmp_path / "head-scratch",
        encoder_binary=construction_binaries["places-serving-encode-v1"],
        verifier_binary=construction_binaries["places-serving-verify-v1"],
        limits=limits,
        shard_bits=4,
    )
    manifest = json.loads(store.path(result["manifest_object"]["key"]).read_text())
    # Copy shard files to a stable local path the tampered manifest points at.
    shard_dir = tmp_path / "verify-shards"
    shard_dir.mkdir()
    for shard in manifest["shards"]:
        source = store.path(
            next(
                obj["key"]
                for obj in result["shard_objects"]
                if obj["shard_id"] == shard["shard_id"]
            )
        )
        target = shard_dir / f"shard-{shard['shard_id']:06d}.plhd"
        target.write_bytes(source.read_bytes())
        shard["path"] = str(target)

    def verify(doc) -> int:
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(doc))
        return subprocess.run(
            [
                str(construction_binaries["places-serving-verify-v1"]),
                "--mode",
                "head-sharded",
                "--manifest",
                str(path),
            ]
        ).returncode

    assert verify(manifest) == 0
    assert manifest["schema"] == "overture-places-global-head-sharded-v2"
    assert manifest["merged_head_binding"]["records"] == manifest["total_records"]
    inflated = json.loads(json.dumps(manifest))
    inflated["total_records"] += 1
    assert verify(inflated) != 0
    reassigned = json.loads(json.dumps(manifest))
    reassigned["shards"][0]["shard_id"] = (
        reassigned["shards"][0]["shard_id"] + 1
    ) % reassigned["shard_count"]
    assert verify(reassigned) != 0
    # Independent reduce-side binding closes the consistent-drop MAJOR: tampering
    # only the independent binding (leaving the self-declared totals intact) is
    # caught, and dropping a whole shard fails because the independent binding
    # still counts the dropped shard's tokens.
    tampered_binding = json.loads(json.dumps(manifest))
    tampered_binding["merged_head_binding"]["head_sum_a"] = "0" * 64
    assert verify(tampered_binding) != 0
    if len(manifest["shards"]) > 1:
        dropped = json.loads(json.dumps(manifest))
        dropped["shards"] = dropped["shards"][1:]
        assert verify(dropped) != 0


def test_baseline_tokenizer_pins_the_contract_unicode_version(baseline_module):
    assert baseline_module.TOKENIZER_UNICODE_VERSION == "17.0.0"
    assert baseline_module.unicodedata.unidata_version == "17.0.0"


@pytest.mark.parametrize(
    "codepoint,script",
    [
        (0x11F00, "Kawi sign, assigned in Unicode 15.0"),
        (0x1E4EC, "Nag Mundari mark, assigned in Unicode 15.0"),
        (0x10D69, "Garay vowel sign, assigned in Unicode 16.0"),
    ],
)
def test_baseline_strips_marks_from_the_pinned_tables(
    baseline_module, codepoint, script
):
    # Combining marks assigned after CPython's embedded tables. The baseline
    # must classify and strip them from the pinned Unicode 17.0 tables, which is
    # what the authoritative Rust implementation does.
    mark = chr(codepoint)
    assert baseline_module.unicodedata.category(mark).startswith("M"), script
    assert baseline_module.tokens(f"kova{mark}") == {"kova"}


def test_pinned_tables_do_not_follow_the_running_interpreter():
    # The divergence this pin exists to remove: on any interpreter older than
    # the pinned version, the stdlib tables disagree about at least one of the
    # marks above, so a baseline reading them would keep marks Rust strips.
    import unicodedata as interpreter_tables

    if interpreter_tables.unidata_version == "17.0.0":
        pytest.skip("interpreter already carries the pinned Unicode version")
    marks = [chr(0x11F00), chr(0x1E4EC), chr(0x10D69)]
    assert any(
        not interpreter_tables.category(mark).startswith("M") for mark in marks
    )


def test_map_combiner_discards_below_serving_rank_and_proves_additivity(
    tmp_path, construction_binaries, construction_module
):
    # The slice fixture above splits 450 features across two cells, so every
    # (cell, token) group lands at 225 -- just under the 256 serving cap, and the
    # combiner never fires. Put every feature in ONE cell so it does.
    module = construction_module
    rows = [
        {
            "id": str(uuid.UUID(int=2_000 + index)),
            "primary_name": f"Common Place {index}",
            "category": "library",
            "locality": "Town",
            "country": "XX",
            "confidence": 1.0 - (index % 10) / 20,
            "point": [0.0, 0.0],
            "source_row_index": index,
        }
        for index in range(450)
    ]
    source = tmp_path / "source.parquet"
    write_fixture(source, rows, row_group_size=64)
    parquet = pq.ParquetFile(source)
    source_limits = tmp_path / "source-limits.json"
    source_limits.write_text(
        json.dumps(
            {
                "objects": [
                    {"records": len(rows), "row_groups": parquet.metadata.num_row_groups}
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
    marker = module.map_task(
        input_path=source,
        source_limits=source_limits,
        store=module.A.LocalObjectStore(tmp_path / "store"),
        scratch_root=tmp_path / "scratch",
        request_sha256="ef" * 32,
        task_id="places-hot",
        transform_binary=construction_binaries["places-transform-v1"],
        proof_binary=construction_binaries["places-proof-directory"],
        limits=limits,
    )

    combiner = marker["combiner"]
    cap = limits.maximum_serving_candidates
    # Five tokens are shared by all 450 features in this single cell -- "common"
    # and "place" from the name, plus "library", "town", "xx" -- and each
    # feature also contributes one unique index token. So the exact arithmetic
    # is: five groups cut to the cap, 450 singleton groups untouched.
    shared, singletons = 5, len(rows)
    assert combiner["input_rows"] == shared * len(rows) + singletons
    assert combiner["retained_rows"] == shared * cap + singletons
    assert combiner["discarded"]["records"] == shared * (len(rows) - cap)
    assert marker["binding"]["records"] == combiner["retained_rows"]

    # The per-place artifact must survive the combiner untouched. This fixture
    # saturates every shared token, so the combiner discards thousands of term
    # rows -- and a place whose tokens ALL sit in saturated groups could vanish
    # from the term set entirely. Anything that must enumerate places (a spatial
    # reverse index above all) reads this artifact, so it is emitted before the
    # combiner and must still hold exactly one row per admitted feature.
    records = marker["place_records"]
    assert records["schema"] == module.PLACE_RECORDS_SCHEMA
    assert records["records"] == marker["transform"]["admitted_features"]
    assert records["records"] == len(rows)
    assert combiner["discarded"]["records"] > 0, "fixture must exercise discarding"

    # The proof the combiner rests on: kept + discarded reconstructs the
    # transform's binding exactly, over both digest lanes.
    reconstructed = module.A.combine_bindings(
        [marker["binding"], combiner["discarded"]]
    )
    assert reconstructed["records"] == marker["transform"]["emitted_term_rows"]
    assert reconstructed["semantic_sum_a"] == marker["transform"]["semantic_sum_a"]
    assert reconstructed["semantic_sum_b"] == marker["transform"]["semantic_sum_b"]


def test_combiner_retains_the_highest_ranked_candidates_not_merely_the_right_count(
    tmp_path, construction_binaries, construction_module
):
    # Counts and binding additivity are ORDER-INVARIANT: they hold identically if
    # the combiner sorts backwards. Inverting confidence_rank in the combiner
    # would make every task retain its WORST candidates and the reducer would
    # serve them -- no row lost, no binding violated, nothing else failing. So
    # this asserts WHICH rows survive.
    module = construction_module
    cap = module.Limits().maximum_serving_candidates
    total = cap + 60
    # confidence_rank is a coarse u8 bucket, so confidence must span the full
    # 0..1 range for the ranking to be observable at all -- a narrow band
    # collapses every row into one bucket and the assertion below cannot
    # discriminate. Ascending with index means the top `cap` rows are the
    # highest indices; a reversed sort keeps the lowest instead.
    rows = [
        {
            "id": str(uuid.UUID(int=7_000 + index)),
            "primary_name": "Shared Name",
            "category": "library",
            "locality": "Town",
            "country": "XX",
            "confidence": index / (total - 1),
            "point": [0.0, 0.0],
            "source_row_index": index,
        }
        for index in range(total)
    ]
    source = tmp_path / "source.parquet"
    write_fixture(source, rows, row_group_size=64)
    source_limits = tmp_path / "source-limits.json"
    source_limits.write_text(
        json.dumps({"objects": [{"records": total,
                                 "row_groups": pq.ParquetFile(source).metadata.num_row_groups}]})
    )
    store = module.A.LocalObjectStore(tmp_path / "store")
    marker = module.map_task(
        input_path=source,
        source_limits=source_limits,
        store=store,
        scratch_root=tmp_path / "scratch",
        request_sha256="12" * 32,
        task_id="places-rank",
        transform_binary=construction_binaries["places-transform-v1"],
        proof_binary=construction_binaries["places-proof-directory"],
        limits=module.Limits(
            max_input_rows=total + 10,
            max_pack_rows=100_000,
            parquet_row_group_rows=4_096,
            max_rss_bytes=2 * 1024**3,
            max_scratch_bytes=2 * 1024**3,
            max_output_bytes=512 * 1024**2,
            wall_seconds=180,
            allow_unpinned_duckdb=True,
        ),
    )

    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect()
    packs = [str(store.path(pack["object"]["key"])) for pack in marker["packs"]]
    minimum_kept, maximum_kept, kept_rows = connection.execute(
        "SELECT min(confidence_rank), max(confidence_rank), count(*) "
        f"FROM read_parquet({packs!r}) WHERE token = 'shared'"
    ).fetchone()
    connection.close()

    # Compare against the range the SOURCE spans, not the packs': every token in
    # this fixture saturates, so the packs' own min/max is the combined band and
    # comparing to it is vacuous. Confidence runs 0.0 -> 1.0, so the full rank
    # band is 0 -> 255 by construction.
    assert kept_rows == cap, "the saturated group should be cut to exactly the cap"
    # The decisive pair: the surviving band sits at the TOP of the source range.
    # Invert the combiner's sort and these flip -- max drops well below 255 and
    # min becomes 0 -- which is precisely the mutation that counts and binding
    # additivity cannot see.
    assert maximum_kept == 255
    assert minimum_kept > 0


def test_per_task_combining_then_global_topn_equals_a_single_global_topn():
    # The exactness claim the combiner rests on: top-N under a TOTAL order is
    # decomposable. Combining per map task and then taking the reducer's global
    # top-N must select the same rows as taking the global top-N over every
    # untouched row. The multi-task case is the one that could break.
    duckdb = pytest.importorskip("duckdb")
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE terms AS SELECT "
        "  (i % 3)::INTEGER AS task, 'cell' AS partition_cell, 'tok' AS token, "
        "  ((i * 37) % 11)::UTINYINT AS confidence_rank, "
        "  ((i * 53) % 997)::INTEGER AS feature_id, "
        "  0::INTEGER AS source_object_index, 0::INTEGER AS source_row_group, "
        "  i::INTEGER AS source_row_index "
        "FROM range(900) t(i)"
    )
    order = (
        "confidence_rank DESC, feature_id, source_object_index, "
        "source_row_group, source_row_index"
    )
    cap = 16

    direct = connection.execute(
        f"SELECT source_row_index FROM terms QUALIFY row_number() OVER ("
        f"PARTITION BY partition_cell, token ORDER BY {order}) <= {cap} "
        "ORDER BY source_row_index"
    ).fetchall()
    staged = connection.execute(
        "SELECT source_row_index FROM ("
        f"  SELECT * FROM terms QUALIFY row_number() OVER ("
        f"    PARTITION BY task, partition_cell, token ORDER BY {order}) <= {cap}"
        f") QUALIFY row_number() OVER ("
        f"PARTITION BY partition_cell, token ORDER BY {order}) <= {cap} "
        "ORDER BY source_row_index"
    ).fetchall()
    connection.close()

    assert len(direct) == cap
    assert staged == direct


def test_shuffle_bucket_python_mirror_matches_the_sql(construction_module):
    # The bucket is computed in SQL during map and in Python everywhere else.
    # If they disagree, a consumer looks in the wrong shard and silently sees no
    # rows for a cell -- an empty result, not an error.
    module = construction_module
    duckdb = pytest.importorskip("duckdb")
    keys = list(range(0, 65536, 97)) + [0, 1, 255, 256, 65535]
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE t AS SELECT unnest(?::INTEGER[]) AS partition_key", [keys]
    )
    for bits in (4, 8, 10):
        rows = connection.execute(
            f"SELECT partition_key, {module.shuffle_bucket_sql(bits)} FROM t"
        ).fetchall()
        assert rows, "no rows produced"
        for key, sql_bucket in rows:
            assert sql_bucket == module.shuffle_bucket(key, bits), (key, bits)
            assert 0 <= sql_bucket < (1 << bits)
    connection.close()


def test_shuffle_bucket_depends_on_both_grid_axes(construction_module):
    # Regression. partition_key is (y << 8) | x, so taking the LOW bits of the
    # multiplicative hash (`% buckets`) yields a bucket that depends only on x:
    # every cell in a longitude column lands together, a pole-to-pole meridian
    # strip per consumer. Cell COUNTS stay perfectly uniform under that bug, so
    # only a test that varies one axis at a time catches it.
    module = construction_module
    for x in (0, 7, 128, 255):
        down_column = {module.shuffle_bucket((y << 8) | x) for y in range(256)}
        assert len(down_column) > 32, f"x={x} collapses to {len(down_column)} buckets"
    for y in (0, 7, 128, 255):
        across_row = {module.shuffle_bucket((y << 8) | x) for x in range(256)}
        assert len(across_row) > 32, f"y={y} collapses to {len(across_row)} buckets"
    counts = collections.Counter(
        module.shuffle_bucket((y << 8) | x) for y in range(256) for x in range(256)
    )
    assert len(counts) == module.SHUFFLE_BUCKETS
    assert max(counts.values()) <= 2 * min(counts.values())


def test_cell_partition_key_round_trips_the_transform_route(construction_module):
    module = construction_module
    for y, x in ((0, 0), (178, 227), (255, 255), (43, 7)):
        assert module.cell_partition_key(f"{y:02x}{x:02x}") == (y << 8) | x
    for bad in ("abc", "abcde", ""):
        with pytest.raises(ValueError):
            module.cell_partition_key(bad)


def test_map_shuffle_keeps_every_cell_in_exactly_one_fragment(
    tmp_path, construction_binaries, construction_module
):
    # The property the whole design rests on: a consumer that wants a cell reads
    # ONE fragment per map task, so its input is bounded by the shuffle rather
    # than by how the source happened to be ordered.
    module = construction_module
    points = [[0.0, 0.0], [-90.0, -45.0], [139.7, 35.7], [7.4, 43.7], [-58.4, -34.6]]
    rows = [
        {
            "id": str(uuid.UUID(int=9_000 + index)),
            "primary_name": f"Place {index}", "category": "library",
            "locality": "Town", "country": "XX", "confidence": 0.5,
            "point": points[index % len(points)], "source_row_index": index,
        }
        for index in range(400)
    ]
    source = tmp_path / "source.parquet"
    write_fixture(source, rows, row_group_size=64)
    limits_path = tmp_path / "source-limits.json"
    limits_path.write_text(json.dumps({"objects": [
        {"records": len(rows), "row_groups": pq.ParquetFile(source).metadata.num_row_groups}]}))
    marker = module.map_task(
        input_path=source, source_limits=limits_path,
        store=module.A.LocalObjectStore(tmp_path / "store"),
        scratch_root=tmp_path / "scratch", request_sha256="34" * 32,
        task_id="places-shuffle", transform_binary=construction_binaries["places-transform-v1"],
        proof_binary=construction_binaries["places-proof-directory"],
        limits=module.Limits(
            max_input_rows=500, max_pack_rows=100_000, parquet_row_group_rows=4_096,
            max_rss_bytes=2 * 1024**3, max_scratch_bytes=2 * 1024**3,
            max_output_bytes=512 * 1024**2, wall_seconds=180, allow_unpinned_duckdb=True),
    )

    buckets_of_cell = collections.defaultdict(set)
    for pack in marker["packs"]:
        assert pack["shuffle_bucket"] == pack["pack_id"]
        for group in pack["directory"]["row_groups"]:
            for routing in group["routing_groups"]:
                buckets_of_cell[routing["partition_cell"]].add(pack["shuffle_bucket"])

    assert len(buckets_of_cell) >= 4, "fixture must span several cells"
    for cell, buckets in buckets_of_cell.items():
        assert len(buckets) == 1, f"cell {cell} split across fragments {buckets}"
        # and the fragment it landed in is the one the Python mirror predicts
        expected = module.shuffle_bucket(module.cell_partition_key(cell))
        assert next(iter(buckets)) == expected


# --------------------------------------------------------------------------- #
# Reduce by shuffle-bucket range
# --------------------------------------------------------------------------- #
def _bucket_space_splits(bits: int) -> list[list[tuple[int, int]]]:
    """Several different partitions of the INCLUSIVE bucket space into ranges."""
    buckets = 1 << bits
    splits = [[(0, buckets - 1)]]
    for stride in (1, 2, 3, 7):
        if stride > buckets:
            continue
        splits.append(
            [
                (start, min(start + stride, buckets) - 1)
                for start in range(0, buckets, stride)
            ]
        )
    # A deliberately uneven split: nothing may assume equal-width ranges.
    if buckets >= 4:
        splits.append([(0, 0), (1, 1), (2, buckets - 2), (buckets - 1, buckets - 1)])
    return splits


def test_bucket_ranges_own_every_cell_exactly_once(construction_module):
    # The claim the whole bucket-range reducer rests on: for ANY partition of the
    # bucket space into ranges, every cell is emitted by exactly one range and the
    # union covers all of them. A cell hashes to one bucket and a bucket lies in
    # one range, so this is structural -- but its violation is SILENT (a missing
    # cell is an empty result, not an error), so assert it over the whole grid.
    module = construction_module
    for bits in (2, 4, 8):
        plan = {
            "partitions": [
                {
                    "id": f"p-{y:02x}{x:02x}",
                    "partition_cell": f"{y:02x}{x:02x}",
                    "shuffle_bucket": module.shuffle_bucket((y << 8) | x, bits),
                }
                for y in range(256)
                for x in range(256)
            ]
        }
        total = len(plan["partitions"])
        for split in _bucket_space_splits(bits):
            owners: dict[int, list[tuple[int, int]]] = collections.defaultdict(list)
            for start, end in split:
                for index, _partition in module.bucket_range_partitions(
                    plan, bucket_start=start, bucket_end=end, bits=bits
                ):
                    owners[index].append((start, end))
            assert len(owners) == total, f"bits={bits} split={split[:3]} dropped cells"
            assert all(len(ranges) == 1 for ranges in owners.values()), (
                f"bits={bits} split={split[:3]} emitted a cell more than once"
            )


def test_bucket_range_rejects_a_range_outside_the_bucket_space(construction_module):
    module = construction_module
    for start, end in ((-1, 4), (0, 256), (5, 4)):
        with pytest.raises(ValueError, match="bucket range"):
            module.validate_bucket_range(start, end, 8)


def test_partition_shuffle_bucket_does_not_trust_the_recorded_value(construction_module):
    module = construction_module
    partition = {"partition_cell": "b2e3", "shuffle_bucket": 0}
    with pytest.raises(ValueError, match="records shuffle bucket"):
        module.partition_shuffle_bucket(partition)
    partition["shuffle_bucket"] = module.shuffle_bucket(module.cell_partition_key("b2e3"))
    assert module.partition_shuffle_bucket(partition) == partition["shuffle_bucket"]


def _shuffled_slice(module, binaries, tmp_path, *, bits: int, task_id: str = "places-r"):
    """A real map marker plus adaptive plan spanning several cells per bucket.

    ``bits`` is deliberately small so a fragment holds MORE than one of the plan's
    cells -- otherwise "reads each fragment once" is trivially true because a
    fragment only ever feeds one partition.
    """
    points = [[0.0, 0.0], [-90.0, -45.0], [139.7, 35.7], [7.4, 43.7], [-58.4, -34.6]]
    rows = [
        {
            "id": str(uuid.UUID(int=11_000 + index)),
            "primary_name": f"Place {index}",
            "category": "library",
            "locality": "Town",
            "country": "XX",
            "confidence": 1.0 - (index % 10) / 20,
            "point": points[index % len(points)],
            "source_row_index": index,
        }
        for index in range(400)
    ]
    source = tmp_path / f"{task_id}.parquet"
    write_fixture(source, rows, row_group_size=64)
    source_limits = tmp_path / f"{task_id}-limits.json"
    source_limits.write_text(
        json.dumps(
            {
                "objects": [
                    {
                        "records": len(rows),
                        "row_groups": pq.ParquetFile(source).metadata.num_row_groups,
                    }
                ]
            }
        )
    )
    limits = module.Limits(
        max_input_rows=500,
        max_pack_rows=100_000,
        parquet_row_group_rows=512,
        max_rss_bytes=2 * 1024**3,
        max_scratch_bytes=2 * 1024**3,
        max_output_bytes=512 * 1024**2,
        wall_seconds=180,
        allow_unpinned_duckdb=True,
        shuffle_bucket_bits=bits,
    )
    store = module.A.LocalObjectStore(tmp_path / "store")
    marker = module.map_task(
        input_path=source,
        source_limits=source_limits,
        store=store,
        scratch_root=tmp_path / f"{task_id}-scratch",
        request_sha256="56" * 32,
        task_id=task_id,
        transform_binary=binaries["places-transform-v1"],
        proof_binary=binaries["places-proof-directory"],
        limits=limits,
    )
    plan = module.adaptive_genesis_plan(
        [marker], store=store, scratch_root=tmp_path / f"{task_id}-plan", limits=limits
    )
    return store, marker, plan, limits


def _count_parquet_opens(monkeypatch):
    """Count ParquetFile opens by path, through the module the reducer imports."""
    opens: collections.Counter = collections.Counter()
    original = pq.ParquetFile

    def counting(path, *args, **kwargs):
        opens[str(path)] += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pq, "ParquetFile", counting)
    return opens


def test_bucket_range_reduce_reads_each_fragment_once_and_matches_per_partition(
    tmp_path, construction_binaries, construction_module, monkeypatch
):
    module = construction_module
    bits = 2
    store, marker, plan, limits = _shuffled_slice(
        module, construction_binaries, tmp_path, bits=bits
    )
    fragments = {store.path(pack["object"]["key"]) for pack in marker["packs"]}
    cells = {item["partition_cell"] for item in plan["partitions"]}
    assert len(cells) > len(fragments), (
        "fixture must put several cells in one fragment, or 'once' is trivial"
    )

    # Per-partition reduce: the reference behaviour, and the open count to beat.
    per_partition_opens = _count_parquet_opens(monkeypatch)
    reference = [
        module.reduce_partition(
            partition=partition,
            plan=plan,
            markers=[marker],
            store=store,
            scratch_root=tmp_path / "reduce-reference",
            encoder_binary=construction_binaries["places-serving-encode-v1"],
            verifier_binary=construction_binaries["places-serving-verify-v1"],
            limits=limits,
        )
        for partition in plan["partitions"]
    ]
    reference_fragment_opens = sum(
        count for path, count in per_partition_opens.items() if Path(path) in fragments
    )
    monkeypatch.undo()

    # Bucket-range reduce over the whole space: one job, each fragment once.
    range_opens = _count_parquet_opens(monkeypatch)
    result = module.reduce_bucket_range(
        bucket_start=0,
        bucket_end=(1 << bits) - 1,
        plan=plan,
        markers=[marker],
        store=store,
        scratch_root=tmp_path / "reduce-range",
        encoder_binary=construction_binaries["places-serving-encode-v1"],
        verifier_binary=construction_binaries["places-serving-verify-v1"],
        limits=limits,
    )
    monkeypatch.undo()

    assert result["schema"] == module.REDUCE_RANGE_SCHEMA
    assert result["partition_indexes"] == list(range(len(plan["partitions"])))
    for path in fragments:
        assert range_opens[str(path)] == 1, f"fragment {path.name} opened twice"
    assert result["fragments_opened"] == len(fragments)
    # The point of the change: the per-partition reducer re-opened fragments.
    assert reference_fragment_opens > len(fragments)

    # Output equivalence: same partitions, same bindings, and byte-identical
    # serving artifacts (the store is content-addressed, so an equal key IS an
    # equal digest).
    for expected, actual in zip(reference, result["reductions"], strict=True):
        assert actual["partition"] == expected["partition"]
        assert actual["binding"] == expected["binding"]
        assert actual["leaf_object"] == expected["leaf_object"]
        assert actual["routed_object"] == expected["routed_object"]
        assert actual["serving_candidate_rows"] == expected["serving_candidate_rows"]
        assert actual["reconciled_row_groups"] == expected["reconciled_row_groups"]

    # Reduce is now watched: the caps reach the Python/pyarrow/DuckDB ingest, not
    # only the encoder and verifier subprocesses, and the evidence proves it ran.
    for evidence in (
        result["ingest_evidence"],
        result["reductions"][0]["serving_evidence"],
    ):
        assert evidence["peak_rss_bytes"] > 0
        assert evidence["peak_scratch_and_output_bytes"] > 0
        assert evidence["wall_seconds"] > 0
    assert reference[0]["ingest_evidence"]["peak_rss_bytes"] > 0


def test_bucket_range_splits_emit_every_partition_exactly_once(
    tmp_path, construction_binaries, construction_module
):
    # However the planner cuts the bucket space, the union of the jobs is the
    # whole plan, nothing twice, and every artifact is the same one.
    module = construction_module
    bits = 2
    store, marker, plan, limits = _shuffled_slice(
        module, construction_binaries, tmp_path, bits=bits, task_id="places-split"
    )
    whole = module.reduce_bucket_range(
        bucket_start=0,
        bucket_end=(1 << bits) - 1,
        plan=plan,
        markers=[marker],
        store=store,
        scratch_root=tmp_path / "reduce-whole",
        encoder_binary=construction_binaries["places-serving-encode-v1"],
        verifier_binary=construction_binaries["places-serving-verify-v1"],
        limits=limits,
    )
    by_index = dict(zip(whole["partition_indexes"], whole["reductions"], strict=True))

    for split in _bucket_space_splits(bits):
        emitted: list[int] = []
        for start, end in split:
            result = module.reduce_bucket_range(
                bucket_start=start,
                bucket_end=end,
                plan=plan,
                markers=[marker],
                store=store,
                scratch_root=tmp_path / f"reduce-{start}-{end}",
                encoder_binary=construction_binaries["places-serving-encode-v1"],
                verifier_binary=construction_binaries["places-serving-verify-v1"],
                limits=limits,
            )
            assert result["bucket_start"] == start and result["bucket_end"] == end
            # A range reads only fragments in its own range.
            for pack in marker["packs"]:
                inside = start <= pack["shuffle_bucket"] <= end
                assert (pack["object"]["key"] in result["fragment_keys"]) == inside
            for index, reduction in zip(
                result["partition_indexes"], result["reductions"], strict=True
            ):
                emitted.append(index)
                assert reduction["leaf_object"] == by_index[index]["leaf_object"]
                assert reduction["routed_object"] == by_index[index]["routed_object"]
        assert sorted(emitted) == list(range(len(plan["partitions"])))
        assert len(emitted) == len(set(emitted)), f"split {split} emitted a partition twice"


def test_bucket_range_reduce_fails_closed_when_the_watchdog_faults(
    tmp_path, construction_binaries, construction_module
):
    # The reduce ingest is now the watchdog's to enforce, so a breach must abort
    # the stage rather than produce a serving artifact nothing bounded.
    module = construction_module
    store, marker, plan, limits = _shuffled_slice(
        module, construction_binaries, tmp_path, bits=2, task_id="places-guard"
    )
    with pytest.raises(Exception) as error:
        module.reduce_bucket_range(
            bucket_start=0,
            bucket_end=3,
            plan=plan,
            markers=[marker],
            store=store,
            scratch_root=tmp_path / "reduce-capped",
            encoder_binary=construction_binaries["places-serving-encode-v1"],
            verifier_binary=construction_binaries["places-serving-verify-v1"],
            limits=replace(limits, max_rss_bytes=1),
        )
    # Either the watchdog's own report or the DuckDB interrupt it raises -- both
    # are the stage failing closed. A silent success is the failure mode.
    message = str(error.value).lower()
    assert "hard cap" in message or "interrupt" in message, message


def test_bucket_range_with_no_partitions_reads_nothing(
    tmp_path, construction_binaries, construction_module
):
    # Empty ranges are legal: the bucket space is covered whether or not every
    # bucket is populated. An empty range must not fail and must not read.
    module = construction_module
    store, marker, plan, limits = _shuffled_slice(
        module, construction_binaries, tmp_path, bits=8, task_id="places-sparse"
    )
    occupied = {
        module.shuffle_bucket(module.cell_partition_key(item["partition_cell"]))
        for item in plan["partitions"]
    }
    empty = next(bucket for bucket in range(256) if bucket not in occupied)
    result = module.reduce_bucket_range(
        bucket_start=empty,
        bucket_end=empty,
        plan=plan,
        markers=[marker],
        store=store,
        scratch_root=tmp_path / "reduce-empty",
        encoder_binary=construction_binaries["places-serving-encode-v1"],
        verifier_binary=construction_binaries["places-serving-verify-v1"],
        limits=limits,
    )
    assert result["reductions"] == []
    assert result["fragment_keys"] == []
    assert result["fragments_opened"] == 0


class _RewritingConnection:
    """A DuckDB connection whose SQL is corrupted on the way through.

    Stands in for the class of defect the emit step used to be blind to: the
    published bytes come out of `WHERE __reduce_partition = <position>`, and
    nothing measured what that predicate actually returned.
    """

    def __init__(self, connection, corrupt):
        self._connection = connection
        self._corrupt = corrupt

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def execute(self, sql, *args, **kwargs):
        return self._connection.execute(self._corrupt(sql), *args, **kwargs)


def _reduce_with_corrupted_sql(module, monkeypatch, corrupt):
    original = module._reduce_connection

    def wrapped(workspace, limits):
        return _RewritingConnection(original(workspace, limits), corrupt)

    monkeypatch.setattr(module, "_reduce_connection", wrapped)


def test_emit_predicate_corruption_fails_closed(
    tmp_path, construction_binaries, construction_module, monkeypatch
):
    # The reviewer's repro. Rewrite the emit predicate so every partition
    # publishes partition 0's rows. Every binding in the reducer is computed
    # pyarrow-side and still matches the plan, and finalize's reconciliation sums
    # those same bindings -- so before the emit checks existed, 15 of 16 serving
    # artifacts were wrong and everything downstream said the run was good.
    module = construction_module
    store, marker, plan, limits = _shuffled_slice(
        module, construction_binaries, tmp_path, bits=2, task_id="places-corrupt"
    )
    assert len(plan["partitions"]) > 1, "the fixture needs partitions to confuse"
    import re

    _reduce_with_corrupted_sql(
        module,
        monkeypatch,
        lambda sql: re.sub(rf"{module.PARTITION_INDEX_COLUMN}=\d+",
                           f"{module.PARTITION_INDEX_COLUMN}=0", sql),
    )
    with pytest.raises(ValueError, match="emit predicate does not select this partition"):
        module.reduce_bucket_range(
            bucket_start=0, bucket_end=3, plan=plan, markers=[marker], store=store,
            scratch_root=tmp_path / "reduce-corrupt",
            encoder_binary=construction_binaries["places-serving-encode-v1"],
            verifier_binary=construction_binaries["places-serving-verify-v1"],
            limits=limits,
        )


def test_emitted_leaf_bytes_must_digest_back_to_the_plan(
    tmp_path, construction_binaries, construction_module, monkeypatch
):
    # The same corruption, but applied ONLY to the COPY that writes the leaf, so
    # the SQL row-count/ownership check still measures the true set and passes.
    # What catches it then is the digest of the bytes about to be published.
    module = construction_module
    store, marker, plan, limits = _shuffled_slice(
        module, construction_binaries, tmp_path, bits=2, task_id="places-bytes"
    )
    import re

    def corrupt(sql: str) -> str:
        if not sql.lstrip().upper().startswith("COPY"):
            return sql
        return re.sub(rf"{module.PARTITION_INDEX_COLUMN}=\d+",
                      f"{module.PARTITION_INDEX_COLUMN}=0", sql)

    _reduce_with_corrupted_sql(module, monkeypatch, corrupt)
    with pytest.raises(ValueError, match="do not digest back to the plan binding"):
        module.reduce_bucket_range(
            bucket_start=0, bucket_end=3, plan=plan, markers=[marker], store=store,
            scratch_root=tmp_path / "reduce-bytes",
            encoder_binary=construction_binaries["places-serving-encode-v1"],
            verifier_binary=construction_binaries["places-serving-verify-v1"],
            limits=limits,
        )


def test_reduce_ingest_refuses_a_term_column_that_shadows_the_partition_tag(
    tmp_path, construction_module
):
    # No code bug required: on the pinned DuckDB 1.5.1 a term column named
    # __reduce_partition does NOT raise. The injected tag is renamed to
    # __reduce_partition_1, so `* EXCLUDE(__reduce_partition)` drops the DATA
    # column and the emit predicate filters on it -- silently publishing the
    # wrong rows for every partition. Refuse the collision instead.
    module = construction_module
    duckdb = pytest.importorskip("duckdb")
    fragment = tmp_path / "shadowed.parquet"
    digests = [bytes(32), bytes(32)]
    pq.write_table(
        pa.table(
            {
                "partition_cell": pa.array(["0000", "0000"], pa.string()),
                "token_hash": pa.array([1, 2], pa.uint64()),
                "semantic_digest_a": pa.array(digests, pa.binary()),
                "semantic_digest_b": pa.array(digests, pa.binary()),
                module.PARTITION_INDEX_COLUMN: pa.array([7, 7], pa.int32()),
            }
        ),
        fragment,
    )
    store = module.A.LocalObjectStore(tmp_path / "store")
    identity = store.put_content(fragment, "map/places-v1/packs", ".parquet")
    pack = {
        "pack_id": 0,
        "shuffle_bucket": 0,
        "object": identity,
        "directory": {
            "row_groups": [
                {
                    "index": 0,
                    "routing_groups": [{"partition_cell": "0000"}],
                    "binding": module.A.zero_binding(),
                }
            ]
        },
    }
    connection = duckdb.connect()
    try:
        with pytest.raises(ValueError, match="collides with the reducer's partition tag"):
            module._reduce_ingest(
                connection=connection,
                fragments=[pack],
                partitions=[(0, {"id": "p-0000", "partition_cell": "0000"})],
                store=store,
                require_complete=False,
            )
    finally:
        connection.close()


def test_bucket_range_fragment_guards_fail_closed(
    tmp_path, construction_binaries, construction_module
):
    # Every fragment-side precondition, each with its own failing case.
    module = construction_module
    _store, marker, _plan, _limits = _shuffled_slice(
        module, construction_binaries, tmp_path, bits=2, task_id="places-fragments"
    )
    kwargs = {"bucket_start": 0, "bucket_end": 3, "bits": 2}

    pre_shuffle = json.loads(json.dumps(marker))
    del pre_shuffle["packs"][0]["shuffle_bucket"]
    with pytest.raises(ValueError, match="predates the map-side shuffle"):
        module.bucket_range_fragments([pre_shuffle], **kwargs)

    mismatched = json.loads(json.dumps(marker))
    mismatched["packs"][0]["pack_id"] = mismatched["packs"][0]["shuffle_bucket"] + 1
    with pytest.raises(ValueError, match="pack id differs from its shuffle bucket"):
        module.bucket_range_fragments([mismatched], **kwargs)

    # A duplicated fragment would double-count every row it holds, and because
    # bindings are additive sums the inflation is invisible until far downstream.
    duplicated = json.loads(json.dumps(marker))
    duplicated["packs"].append(json.loads(json.dumps(duplicated["packs"][0])))
    with pytest.raises(ValueError, match="same map fragment twice"):
        module.bucket_range_fragments([duplicated], **kwargs)


def test_bucket_range_reduce_fails_closed_on_an_incomplete_plan(
    tmp_path, construction_binaries, construction_module
):
    # The central exactness argument, tested by breaking it: drop one cell from
    # the plan and the range's partitions no longer claim every row of every
    # fragment they read. That must abort, not silently publish a short run.
    module = construction_module
    store, marker, plan, limits = _shuffled_slice(
        module, construction_binaries, tmp_path, bits=2, task_id="places-incomplete"
    )
    assert len(plan["partitions"]) > 1
    incomplete = json.loads(json.dumps(plan))
    dropped = incomplete["partitions"].pop()
    bucket = module.shuffle_bucket(
        module.cell_partition_key(dropped["partition_cell"]), 2
    )
    with pytest.raises(ValueError, match="unclaimed"):
        module.reduce_bucket_range(
            bucket_start=bucket, bucket_end=bucket, plan=incomplete, markers=[marker],
            store=store, scratch_root=tmp_path / "reduce-incomplete",
            encoder_binary=construction_binaries["places-serving-encode-v1"],
            verifier_binary=construction_binaries["places-serving-verify-v1"],
            limits=limits,
        )
    # And the degenerate version of the same break: a range whose buckets hold
    # map fragments but whose plan has no partitions at all for them.
    emptied = {**incomplete, "partitions": [
        item for item in incomplete["partitions"]
        if module.shuffle_bucket(module.cell_partition_key(item["partition_cell"]), 2)
        != bucket
    ]}
    with pytest.raises(ValueError, match="fragments but no plan partitions"):
        module.reduce_bucket_range(
            bucket_start=bucket, bucket_end=bucket, plan=emptied, markers=[marker],
            store=store, scratch_root=tmp_path / "reduce-orphan",
            encoder_binary=construction_binaries["places-serving-encode-v1"],
            verifier_binary=construction_binaries["places-serving-verify-v1"],
            limits=limits,
        )


def test_bucket_range_reduce_fails_closed_when_a_partition_binding_is_wrong(
    tmp_path, construction_binaries, construction_module
):
    module = construction_module
    store, marker, plan, limits = _shuffled_slice(
        module, construction_binaries, tmp_path, bits=2, task_id="places-binding"
    )
    tampered = json.loads(json.dumps(plan))
    tampered["partitions"][0]["binding"]["records"] += 1
    with pytest.raises(ValueError, match="binding differs from plan"):
        module.reduce_bucket_range(
            bucket_start=0, bucket_end=3, plan=tampered, markers=[marker], store=store,
            scratch_root=tmp_path / "reduce-binding",
            encoder_binary=construction_binaries["places-serving-encode-v1"],
            verifier_binary=construction_binaries["places-serving-verify-v1"],
            limits=limits,
        )


def test_reduce_job_is_wall_bounded_not_only_each_serving_stage(
    tmp_path, construction_binaries, construction_module
):
    # A bucket-range job runs one ingest and then a serving stage per partition.
    # Giving each stage a fresh `wall_seconds` would leave the JOB unbounded --
    # tens of partitions x the full budget against a hard Actions kill that
    # produces no evidence. The budget is the job's, and it is spent down.
    module = construction_module
    started = module.time.monotonic() - 100.0
    limits = module.Limits(wall_seconds=120)
    assert 0 < module._remaining_wall(started, limits) <= 20
    with pytest.raises(ValueError, match="exhausted its wall budget"):
        module._remaining_wall(module.time.monotonic() - 121.0, limits)
