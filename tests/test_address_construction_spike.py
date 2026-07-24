from __future__ import annotations

import json
import importlib.util
import struct
import subprocess
import sys
from pathlib import Path

import pytest


pa = pytest.importorskip("pyarrow")
ipc = pytest.importorskip("pyarrow.ipc")
pq = pytest.importorskip("pyarrow.parquet")

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/address_construction_v1.json"
HARNESS_SPEC = importlib.util.spec_from_file_location(
    "spike_address_construction", ROOT / "scripts/spike_address_construction.py"
)
assert HARNESS_SPEC and HARNESS_SPEC.loader
HARNESS = importlib.util.module_from_spec(HARNESS_SPEC)
HARNESS_SPEC.loader.exec_module(HARNESS)


def point(row: dict) -> bytes:
    if "geometry_hex" in row:
        return bytes.fromhex(row["geometry_hex"])
    lon, lat = row.get("point", [-71.0, 42.0])
    return b"\x01" + struct.pack("<Idd", 1, lon, lat)


def write_fixture(path: Path, rows: list[dict]) -> None:
    def values(name: str, default):
        return [row.get(name, default) for row in rows]

    arrays = [
        pa.array(values("id", None), type=pa.string()),
        pa.array(values("street", "Main"), type=pa.string()),
        pa.array(values("number", "1"), type=pa.string()),
        pa.array(values("unit", ""), type=pa.string()),
        pa.array(values("postcode", ""), type=pa.string()),
        pa.array(values("postal_city", ""), type=pa.string()),
        pa.array(values("address_levels", []), type=pa.list_(pa.string())),
        pa.array(values("country", "US"), type=pa.string()),
        pa.array([point(row) for row in rows], type=pa.binary()),
        pa.array(values("source_object_index", 0), type=pa.int32()),
        pa.array(values("source_row_group", 0), type=pa.int32()),
        pa.array(values("source_row_index", 0), type=pa.int32()),
    ]
    table = pa.Table.from_arrays(
        arrays,
        names=[
            "id",
            "street",
            "number",
            "unit",
            "postcode",
            "postal_city",
            "address_levels",
            "country",
            "geometry",
            "source_object_index",
            "source_row_group",
            "source_row_index",
        ],
    )
    pq.write_table(table, path, row_group_size=4)


def test_independent_address_fixture(tmp_path):
    fixture = json.loads(FIXTURE.read_text())
    projected = tmp_path / "projected.parquet"
    write_fixture(projected, fixture["rows"])
    hydrated = tmp_path / "hydrated.arrow"

    hydration = HARNESS.hydrate_parquet(projected, hydrated, 4)
    assert hydration["rows"] == fixture["expected"]["input_rows"]

    output = tmp_path / "transformed.arrow"
    report_path = tmp_path / "report.json"
    source_limits = tmp_path / "source-limits.json"
    source_limits.write_text(
        json.dumps({"objects": [{"records": len(fixture["rows"]), "row_groups": 3}]})
    )
    subprocess.run(
        [
            "cargo",
            "run",
            "--quiet",
            "-p",
            "geocoder-construction",
            "--bin",
            "address-transform-v1",
            "--",
            "--input",
            str(hydrated),
            "--output",
            str(output),
            "--report",
            str(report_path),
            "--source-limits",
            str(source_limits),
        ],
        cwd=ROOT / "crates",
        check=True,
    )
    report = json.loads(report_path.read_text())
    expected = fixture["expected"]
    assert report["input_rows"] == expected["input_rows"]
    assert report["admitted_rows"] == expected["admitted_rows"]
    assert report["rejections_by_precedence"] == expected["rejections_by_precedence"]

    with output.open("rb") as source:
        table = ipc.open_stream(source).read_all().combine_chunks()
    assert table.num_rows == 2
    assert table["route_hash"][0].as_py() == expected["route_hash"]
    assert table["maximum_bucket"][0].as_py() == expected["maximum_bucket"]
    assert [
        table[f"normalized_key_{index}"][0].as_py() for index in range(8)
    ] == expected["normalized_lookup_key"]

    ordered = table.sort_by(
        [
            ("normalized_key_0", "ascending"),
            ("normalized_key_1", "ascending"),
            ("normalized_key_2", "ascending"),
            ("normalized_key_3", "ascending"),
            ("normalized_key_4", "ascending"),
            ("normalized_key_5", "ascending"),
            ("normalized_key_6", "ascending"),
            ("normalized_key_7", "ascending"),
            ("feature_id", "ascending"),
            ("source_object_index", "ascending"),
            ("source_row_group", "ascending"),
            ("source_row_index", "ascending"),
        ]
    )
    assert [ordered["feature_id"][index].as_py().hex() for index in range(2)] == expected[
        "ordered_feature_ids_hex"
    ]
    assert [ordered["source_row_index"][index].as_py() for index in range(2)] == expected[
        "ordered_source_row_indexes"
    ]
    assert [table["street"][index].as_py() for index in range(2)] == expected[
        "display_streets_in_input_order"
    ]
    assert report["semantic_sum_a"] == expected["semantic_sum_a"]
    assert report["semantic_sum_b"] == expected["semantic_sum_b"]

    multi_batch_output = tmp_path / "multi-batch-transformed.arrow"
    with multi_batch_output.open("wb") as destination:
        with ipc.new_stream(destination, table.schema) as writer:
            writer.write_batch(table.slice(0, 1).to_batches()[0])
            writer.write_batch(table.slice(1, 1).to_batches()[0])

    first = HARNESS.run_isolated_construction(
        multi_batch_output,
        tmp_path / "first-construction",
        memory_limit="256MB",
        threads=1,
        allow_unpinned_duckdb=True,
    )
    second = HARNESS.run_isolated_construction(
        multi_batch_output,
        tmp_path / "second-construction",
        memory_limit="256MB",
        threads=1,
        allow_unpinned_duckdb=True,
    )
    first_process = first["isolated_process"]
    second_process = second["isolated_process"]
    assert first_process["pid"] != second_process["pid"]
    assert first_process["process_group_id"] != second_process["process_group_id"]
    assert first_process["pid"] == first_process["process_group_id"]
    assert second_process["pid"] == second_process["process_group_id"]
    assert first["scratch_bytes_after_close"] == 0
    assert second["scratch_bytes_after_close"] == 0
    for construction in (first, second):
        ingestion = construction["bounded_ingestion"]
        assert ingestion["kind"] == "arrow-record-batch-to-on-disk-duckdb-v1"
        assert ingestion["batches"] > 1
        assert ingestion["maximum_batch_rows"] == 65_536
        assert ingestion["observed_maximum_batch_rows"] <= 65_536
        assert ingestion["full_table_read_all"] is False
    assert first["pack"]["sha256"] == second["pack"]["sha256"]
    assert first["summary"]["sha256"] == second["summary"]["sha256"]


def test_legacy_reference_uses_bounded_row_batches(tmp_path):
    fixture = json.loads(FIXTURE.read_text())
    projected = tmp_path / "projected.parquet"
    write_fixture(projected, fixture["rows"])
    hydrated = tmp_path / "hydrated.arrow"
    HARNESS.hydrate_parquet(projected, hydrated, 4)
    evidence = HARNESS.run_legacy_baseline(
        hydrated,
        tmp_path / "legacy.parquet",
        chunk_rows=1,
        max_open_chunks=16,
    )
    materialization = evidence["row_materialization"]
    assert materialization["kind"] == "python-row-external-sort-merge-v1"
    assert materialization["chunks"] > 1
    assert materialization["peak_pending_records"] <= 4
    assert materialization["maximum_batch_rows"] == 65_536
    assert materialization["peak_batch_rows"] <= 65_536
    assert materialization["full_python_row_table_materialized"] is False

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import global_v2_address_map as address_map
        from experiment_address_reduce import strict_batch_records
    finally:
        sys.path.pop(0)
    records = []
    with hydrated.open("rb") as source:
        for batch in ipc.open_stream(source):
            batch_records, _ = strict_batch_records(batch)
            records.extend(batch_records)
    records.sort(key=lambda item: (item[0], item[1]))
    rows = [
        address_map._payload_to_shuffle_row(payload, maximum_hash_bits=16)
        for _, payload in records
    ]
    expected = tmp_path / "legacy-in-memory.parquet"
    pq.write_table(
        pa.Table.from_pylist(rows, schema=address_map.shuffle_schema()),
        expected,
        compression="zstd",
        compression_level=6,
        row_group_size=65_536,
        version="2.6",
    )
    assert (tmp_path / "legacy.parquet").read_bytes() == expected.read_bytes()
