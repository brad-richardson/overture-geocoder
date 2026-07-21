import copy
import itertools
import json
import math
import struct
import sys
import uuid
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from global_v2_places_inventory import (  # noqa: E402
    REQUIRED_FIELD_TYPES,
    approved_prefix,
    build_inventory,
    canonical_json_bytes,
    canonical_schema_contract,
)
from global_v2_places_map import (  # noqa: E402
    EXECUTION_GROUP_LEVEL,
    MAP_SUMMARY_FAMOUS_CAP,
    REJECTION_PRECEDENCE,
    SUMMARY_ARTIFACT_SCHEMA,
    read_map_summary,
    read_maximum_level_counts,
    run_map_task,
)
from places_partition import quadkey_bbox  # noqa: E402
import global_v2_places_map as places_map  # noqa: E402


RELEASE = "2026-06-18.0"


def wkb_point(longitude, latitude):
    return struct.pack("<BIdd", 1, 1, longitude, latitude)


def valid_row(identifier, *, longitude=0.0, latitude=0.0, confidence=0.5):
    return {
        "id": identifier,
        "geometry": wkb_point(longitude, latitude),
        "names": {
            "primary": "Example Place",
            "common": {"fr": "Lieu exemple", "en": "Example Place"},
        },
        "brand": {"names": {"primary": "Example Brand"}},
        "categories": {"primary": "restaurant"},
        "basic_category": "eat_and_drink",
        "addresses": [{"locality": "Town", "region": "Region", "country": "US"}],
        "confidence": confidence,
        "operating_status": "open",
    }


def inventory_for_rows(rows):
    prefix = approved_prefix(RELEASE)
    schema = canonical_schema_contract(
        [
            {"path": path, "type": field_type, "nullable": True}
            for path, field_type in REQUIRED_FIELD_TYPES.items()
        ]
    )
    details = {
        "records": len(rows),
        "row_group_count": 1,
        "row_groups": [
            {
                "index": 0,
                "rows": len(rows),
                "selected_compressed_bytes": 1_000,
                "selected_uncompressed_bytes": 2_000,
            }
        ],
        "schema_contract": schema,
    }
    return build_inventory(
        RELEASE,
        [{"uri": prefix + "part-0.parquet", "etag": "etag-0", "bytes": 10_000}],
        lambda _: details,
        target_rows=len(rows),
        max_selected_uncompressed_bytes=10_000,
        max_groups=1,
        max_tasks=1,
    )


def fake_writer(batches, path, metadata):
    lines = [canonical_json_bytes({"metadata": metadata})]
    records = 0
    for batch in batches:
        rows = batch.to_pylist()
        records += len(rows)
        lines.extend(canonical_json_bytes(row) for row in rows)
    path.write_bytes(b"\n".join(lines) + b"\n")
    return records


def reader_for(rows):
    def read(_source, row_range):
        assert row_range["first_row_group"] == row_range["last_row_group"] == 0
        yield from ((0, index, row) for index, row in enumerate(rows))

    return read


def rejection_rows():
    rows = []
    missing_id = valid_row("00000000-0000-0000-0000-000000000100")
    missing_id["id"] = " "
    rows.append(missing_id)
    invalid_id = valid_row("not-a-uuid")
    invalid_id["geometry"] = None  # Invalid ID wins by declared precedence.
    rows.append(invalid_id)
    missing_geometry = valid_row("00000000-0000-0000-0000-000000000101")
    missing_geometry["geometry"] = None
    rows.append(missing_geometry)
    non_point = valid_row("00000000-0000-0000-0000-000000000102")
    non_point["geometry"] = {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}
    rows.append(non_point)
    invalid_geometry = valid_row("00000000-0000-0000-0000-000000000103")
    invalid_geometry["geometry"] = {"type": "Point", "coordinates": ["0", 0]}
    rows.append(invalid_geometry)
    nonfinite = valid_row("00000000-0000-0000-0000-000000000104")
    nonfinite["geometry"] = {"type": "Point", "coordinates": [math.nan, 0.0]}
    rows.append(nonfinite)
    out_of_world = valid_row("00000000-0000-0000-0000-000000000105")
    out_of_world["geometry"] = {"type": "Point", "coordinates": [181.0, 0.0]}
    rows.append(out_of_world)
    blank_name = valid_row("00000000-0000-0000-0000-000000000106")
    blank_name["names"]["primary"] = " "
    rows.append(blank_name)
    missing_status = valid_row("00000000-0000-0000-0000-000000000107")
    missing_status["operating_status"] = None
    rows.append(missing_status)
    unsupported_status = valid_row("00000000-0000-0000-0000-000000000108")
    unsupported_status["operating_status"] = "permanently_closed"
    rows.append(unsupported_status)
    return rows


def test_map_is_strict_reconciled_cell_fetchable_bounded_and_content_addressed(
    tmp_path,
):
    # Three rows land in one execution group. fragment_rows=2 deliberately
    # emits two bounded objects for it. A fourth row lands in another group.
    retained = [
        valid_row("00000000-0000-0000-0000-000000000003", confidence=0.1),
        valid_row("00000000-0000-0000-0000-000000000001", confidence=0.9),
        valid_row("00000000-0000-0000-0000-000000000002", confidence=0.5),
        valid_row(
            "00000000-0000-0000-0000-000000000004",
            longitude=100.0,
            latitude=40.0,
        ),
    ]
    rows = retained + rejection_rows()
    inventory = inventory_for_rows(rows)
    output = tmp_path / "first"
    report = run_map_task(
        inventory,
        task_index=0,
        output_dir=output,
        batch_reader=reader_for(rows),
        fragment_writer=fake_writer,
        fragment_rows=2,
    )

    assert report["accounting"] == {
        "expected_input_records": 14,
        "input_records": 14,
        "retained_records": 4,
        "rejected_records": 10,
        "rejections_by_precedence": [
            {"reason": reason, "records": 1} for reason in REJECTION_PRECEDENCE
        ],
    }
    assert report["execution"]["task_identity_is_serving_identity"] is False
    assert report["execution"]["fragment_grouping_is_final_shard_identity"] is False
    assert report["execution"]["execution_group_level"] == EXECUTION_GROUP_LEVEL
    fragments = report["fragments"]["objects"]
    assert len(fragments) == 1
    assert fragments[0]["execution_groups"] == sorted(
        {row_group["execution_group"] for row_group in fragments[0]["row_groups"]}
    )
    assert all(item["records"] <= 2 for item in fragments[0]["row_groups"])
    for item in fragments:
        path = output / item["object_key"]
        assert path.stat().st_size == item["bytes"]
        assert path.name == item["sha256"] + ".parquet"
        decoded = [json.loads(line) for line in path.read_text().splitlines()]
        fragment_rows = decoded[1:]
        assert {row["execution_group"] for row in fragment_rows} == set(
            item["execution_groups"]
        )
        sort_keys = [
            (
                row["partition_cell"],
                row["partition_key"],
                -round(row["confidence"] * 255),
                row["gers_id"],
                row["source_uri"],
                row["source_row_group"],
                row["source_row_index"],
            )
            for row in fragment_rows
        ]
        assert sort_keys == sorted(sort_keys)

    summary_path = output / report["summary"]["object_key"]
    counts = list(read_maximum_level_counts(summary_path))
    assert counts == sorted(counts)
    assert sum(count for _, count in counts) == 4

    # Staging names and output roots never enter content/provenance identities.
    second = run_map_task(
        copy.deepcopy(inventory),
        task_index=0,
        output_dir=tmp_path / "second",
        batch_reader=reader_for(rows),
        fragment_writer=fake_writer,
        fragment_rows=2,
    )
    assert second == report


def test_adversarial_sparse_max_cells_have_at_most_256_execution_fragments(tmp_path):
    rows = []
    identifier = 1
    # Six-level cell centers are necessarily distinct at level 12. Cover all
    # 256 level-4 groups with 16 sparse level-12 cells in each group.
    for group_digits in itertools.product("0123", repeat=EXECUTION_GROUP_LEVEL):
        group = "".join(group_digits)
        for suffix_digits in itertools.product("0123", repeat=2):
            cell = group + "".join(suffix_digits)
            xmin, ymin, xmax, ymax = quadkey_bbox(cell)
            rows.append(
                valid_row(
                    str(uuid.UUID(int=identifier)),
                    longitude=(xmin + xmax) / 2,
                    latitude=(ymin + ymax) / 2,
                )
            )
            identifier += 1

    report = run_map_task(
        inventory_for_rows(rows),
        task_index=0,
        output_dir=tmp_path,
        batch_reader=reader_for(rows),
        fragment_writer=fake_writer,
        fragment_rows=100,
        max_task_fragments=256,
    )

    assert report["accounting"]["input_records"] == 4096
    assert report["accounting"]["retained_records"] == 4096
    assert report["accounting"]["rejected_records"] == 0
    assert report["summary"]["cells"] == 4096
    assert report["summary"]["records"] == 4096
    counts = list(
        read_maximum_level_counts(tmp_path / report["summary"]["object_key"])
    )
    assert len(counts) == 4096
    assert sum(records for _, records in counts) == 4096
    assert report["execution"]["execution_group_count"] == 256
    assert report["fragments"]["count"] == 1
    assert report["fragments"]["records"] == 4096
    pack = report["fragments"]["objects"][0]
    assert len(pack["row_groups"]) == 256
    assert {item["execution_group"] for item in pack["row_groups"]} == {
        "".join(digits)
        for digits in itertools.product("0123", repeat=EXECUTION_GROUP_LEVEL)
    }


def test_diffuse_100k_shape_coalesces_into_one_coarse_tail_pack(tmp_path):
    count = 100_001
    groups = ["".join(digits) for digits in itertools.product("0123", repeat=4)]
    centers = []
    for group in groups:
        xmin, ymin, xmax, ymax = quadkey_bbox(group + "00")
        centers.append(((xmin + xmax) / 2, (ymin + ymax) / 2))

    def diffuse_reader(_source, _row_range):
        for index in range(count):
            longitude, latitude = centers[index % len(centers)]
            yield 0, index, valid_row(
                str(uuid.UUID(int=index + 1)),
                longitude=longitude,
                latitude=latitude,
            )

    report = run_map_task(
        inventory_for_rows(range(count)),
        task_index=0,
        output_dir=tmp_path,
        batch_reader=diffuse_reader,
        fragment_writer=fake_writer,
    )

    assert report["fragments"]["count"] == 1
    pack = report["fragments"]["objects"][0]
    assert pack["records"] == count
    assert len(pack["execution_groups"]) == 256
    assert pack["row_group_count"] >= 256
    assert report["execution"]["packs"]["ordered_queries"] == 1
    assert report["execution"]["packs"]["sort_extent_queries"] == 0


def test_map_fails_closed_when_one_streamed_pack_exceeds_actual_byte_cap(tmp_path):
    rows = [valid_row(str(uuid.UUID(int=identifier))) for identifier in range(1, 5)]

    def padded_writer(batches, path, _metadata):
        identifiers = []
        for batch in batches:
            identifiers.extend(batch.column("gers_id").to_pylist())
        identities = canonical_json_bytes(identifiers)
        path.write_bytes(identities + b"x" * (100 + len(identifiers) * 100))
        return len(identifiers)

    with pytest.raises(ValueError, match="physical pack exceeds"):
        run_map_task(
            inventory_for_rows(rows),
            task_index=0,
            output_dir=tmp_path,
            batch_reader=reader_for(rows),
            fragment_writer=padded_writer,
            fragment_rows=100,
            max_fragment_bytes=300,
            max_task_fragments=4,
        )


def test_map_summary_census_is_disk_backed_streamed_and_famous_bounded(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(places_map, "MAP_CENSUS_BATCH_ROWS", 128)
    rows = [
        valid_row(str(uuid.UUID(int=identifier)), confidence=1.0)
        for identifier in range(1, MAP_SUMMARY_FAMOUS_CAP + 1)
    ]
    excluded = valid_row(
        str(uuid.UUID(int=MAP_SUMMARY_FAMOUS_CAP + 1)), confidence=0.0
    )
    rows.append(excluded)

    report = run_map_task(
        inventory_for_rows(rows),
        task_index=0,
        output_dir=tmp_path,
        batch_reader=reader_for(rows),
        fragment_writer=fake_writer,
    )

    summary = report["summary"]
    assert summary["famous_candidate_cap"] == MAP_SUMMARY_FAMOUS_CAP
    assert summary["famous_candidates"] == MAP_SUMMARY_FAMOUS_CAP
    values = list(read_map_summary(tmp_path / summary["object_key"]))
    kinds = [value["kind"] for value in values]
    assert kinds.count("famous") == MAP_SUMMARY_FAMOUS_CAP
    assert str(uuid.UUID(int=MAP_SUMMARY_FAMOUS_CAP + 1)) not in {
        value["gers_id"] for value in values if value["kind"] == "famous"
    }

    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(tmp_path / summary["object_key"])
    metadata = {
        key.decode(): value.decode()
        for key, value in (parquet.schema_arrow.metadata or {}).items()
    }
    assert metadata["artifact_schema"] == SUMMARY_ARTIFACT_SCHEMA
    assert int(metadata["famous_candidate_cap"]) == MAP_SUMMARY_FAMOUS_CAP
    assert int(metadata["summary_rows"]) == parquet.metadata.num_rows == len(values)
    assert int(metadata["cell_rows"]) == summary["cells"]
    assert int(metadata["cell_records"]) == summary["records"]
    assert int(metadata["exact_rows"]) == summary["exact_keys"]
    assert int(metadata["prefix_rows"]) == summary["prefix_keys"]
    assert int(metadata["famous_rows"]) == summary["famous_candidates"]
    assert int(metadata["key_bytes"]) == summary["key_bytes"]
    census = report["execution"]["census"]
    assert census["kind"] == "duckdb-typed-bounded-task-census-v1"
    assert census["engine_version"] == "1.5.1"
    assert census["maximum_batch_rows"] >= census["peak_pending_count_rows"]
    assert census["maximum_batch_rows"] >= census["peak_pending_famous_rows"]
    assert census["famous_candidate_cap"] == MAP_SUMMARY_FAMOUS_CAP
    assert census["famous_candidate_identity"] == "gers_id"
    assert census["famous_deduplicate_before_cap"] is True
    workspace = report["execution"]["workspace"]
    assert workspace["kind"] == "combined-map-workspace-hard-cap-v1"
    assert workspace["peak_bytes"] <= workspace["maximum_bytes"]
    assert sum(workspace["peak_components"].values()) == workspace["peak_bytes"]
    assert workspace["component_peak_bytes"]["census_database_bytes"] > 0
    assert workspace["component_peak_bytes"]["sort_database_bytes"] > 0
    assert workspace["component_peak_bytes"]["staged_output_bytes"] > 0
    assert workspace["observations"] > 3
    sort = report["execution"]["sort"]
    assert sort["kind"] == "duckdb-arrow-batch-external-sort-v1"
    assert sort["registered_arrow_batches"] is True
    assert sort["python_sorted_runs"] is False
    assert sort["python_heap_merge"] is False
    assert sort["threads"] == 1
    assert sort["preserve_insertion_order"] is False
    assert sort["peak_pending_rows"] <= sort["maximum_batch_rows"]
    packs = report["execution"]["packs"]
    assert packs["writer"] == "single-duckdb-order-group-aligned-row-groups-v2"
    assert packs["maximum_batch_rows"] == places_map.MAP_OUTPUT_BATCH_ROWS
    assert packs["python_pack_rows_materialized"] is False


def test_map_famous_deduplicates_gers_best_occurrence_before_cap(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(places_map, "MAP_SUMMARY_FAMOUS_CAP", 3)
    monkeypatch.setattr(places_map, "MAP_CENSUS_BATCH_ROWS", 2)
    duplicate_id = str(uuid.UUID(int=1))
    rows = [
        valid_row(duplicate_id, confidence=confidence)
        for confidence in (0.91, 0.92, 0.93, 0.99, 0.95, 0.94)
    ]
    rows.extend(
        [
            valid_row(str(uuid.UUID(int=2)), confidence=0.2),
            valid_row(str(uuid.UUID(int=3)), confidence=0.1),
        ]
    )

    report = run_map_task(
        inventory_for_rows(rows),
        task_index=0,
        output_dir=tmp_path,
        batch_reader=reader_for(rows),
        fragment_writer=fake_writer,
    )

    famous = [
        row
        for row in read_map_summary(tmp_path / report["summary"]["object_key"])
        if row["kind"] == "famous"
    ]
    assert [row["gers_id"] for row in famous] == [
        duplicate_id,
        str(uuid.UUID(int=2)),
        str(uuid.UUID(int=3)),
    ]
    assert famous[0]["confidence"] == 0.99
    assert famous[0]["source_row_index"] == 3
    assert report["summary"]["famous_candidates"] == 3
    assert report["execution"]["census"]["famous_deduplicate_before_cap"] is True


def test_map_enforces_combined_workspace_hard_cap(tmp_path):
    rows = [valid_row(str(uuid.UUID(int=1)))]
    with pytest.raises(ValueError, match="hard combined workspace cap"):
        run_map_task(
            inventory_for_rows(rows),
            task_index=0,
            output_dir=tmp_path,
            batch_reader=reader_for(rows),
            fragment_writer=fake_writer,
            max_workspace_bytes=1,
        )


def test_duckdb_record_batch_packs_match_fixture_writer_logical_rows(tmp_path):
    rows = [
        valid_row(str(uuid.UUID(int=index)), longitude=-120 + index, latitude=30 + index)
        for index in range(1, 7)
    ]
    inventory = inventory_for_rows(rows)
    parquet_report = run_map_task(
        inventory,
        task_index=0,
        output_dir=tmp_path / "parquet",
        batch_reader=reader_for(rows),
        fragment_rows=2,
    )
    fixture_report = run_map_task(
        inventory,
        task_index=0,
        output_dir=tmp_path / "fixture",
        batch_reader=reader_for(rows),
        fragment_writer=fake_writer,
        fragment_rows=2,
    )
    import pyarrow.parquet as pq

    parquet_rows = [
        row
        for fragment in parquet_report["fragments"]["objects"]
        for row in pq.read_table(
            tmp_path / "parquet" / fragment["object_key"]
        ).to_pylist()
    ]
    for pack in parquet_report["fragments"]["objects"]:
        parquet = pq.ParquetFile(tmp_path / "parquet" / pack["object_key"])
        assert parquet.metadata.num_row_groups == pack["row_group_count"]
        for row_group in pack["row_groups"]:
            physical_rows = parquet.read_row_group(row_group["index"]).to_pylist()
            assert {row["execution_group"] for row in physical_rows} == {
                row_group["execution_group"]
            }
            assert len(physical_rows) == row_group["records"]
            assert row_group["semantic_sha256"]
            assert row_group["ownership_layout_sha256"]
    fixture_rows = [
        json.loads(line)
        for fragment in fixture_report["fragments"]["objects"]
        for line in (
            tmp_path / "fixture" / fragment["object_key"]
        ).read_text().splitlines()[1:]
    ]
    def key(row):
        return (
            row["partition_cell"], row["partition_key"],
            -round(row["confidence"] * 255), row["gers_id"], row["source_uri"],
            row["source_row_group"], row["source_row_index"],
        )
    assert sorted(parquet_rows, key=key) == sorted(fixture_rows, key=key)
    assert parquet_report["execution"]["packs"]["python_pack_rows_materialized"] is False


def test_duckdb_sort_ingests_registered_bounded_arrow_batches(tmp_path):
    workspace = places_map._WorkspaceBudget(tmp_path / "sort", 100_000_000)
    store = places_map._IntermediateRowStore(
        tmp_path / "sort", workspace=workspace, run_rows=2
    )
    try:
        for index in range(5):
            row, reason = places_map.project_row(
                valid_row(str(uuid.UUID(int=index + 1))),
                maximum_level=12,
                source_uri="s3://fixture/places.parquet",
                row_group=0,
                row_index=index,
            )
            assert reason is None
            store.add(row)
        store.finish()
        evidence = store.evidence()
        assert evidence["insert_batches"] == 3
        assert evidence["peak_pending_rows"] == 2
        assert evidence["registered_arrow_batches"] is True
        assert not hasattr(store, "runs")
    finally:
        store.close()


def test_map_pack_target_keeps_one_execution_group_row_group_intact(tmp_path):
    first_cell = "000000000000"
    second_cell = "000000000001"

    def center(cell):
        xmin, ymin, xmax, ymax = quadkey_bbox(cell)
        return (xmin + xmax) / 2, (ymin + ymax) / 2

    first_lon, first_lat = center(first_cell)
    second_lon, second_lat = center(second_cell)
    rows = [
        valid_row(str(uuid.UUID(int=1)), longitude=first_lon, latitude=first_lat),
        valid_row(str(uuid.UUID(int=2)), longitude=first_lon, latitude=first_lat),
        valid_row(str(uuid.UUID(int=3)), longitude=second_lon, latitude=second_lat),
    ]
    report = run_map_task(
        inventory_for_rows(rows),
        task_index=0,
        output_dir=tmp_path,
        batch_reader=reader_for(rows),
        fragment_writer=fake_writer,
        target_fragment_input_bytes=1,
        max_fragment_input_bytes=1_000_000,
        max_fragment_bytes=1_000_000,
    )

    fragments = report["fragments"]["objects"]
    assert [fragment["records"] for fragment in fragments] == [3]
    assert fragments[0]["row_groups"][0]["minimum_maximum_level_cell"] == first_cell
    assert fragments[0]["row_groups"][0]["maximum_maximum_level_cell"] == second_cell
    packs = report["execution"]["packs"]
    assert packs == {
        "kind": "task-wide-order-coarse-pack-v2",
        "writer": "single-duckdb-order-group-aligned-row-groups-v2",
        "maximum_batch_rows": places_map.MAP_OUTPUT_BATCH_ROWS,
        "python_pack_rows_materialized": False,
        "ordinary_boundary": "execution-group-or-bounded-row-group",
        "physical_pack_target_bytes": 1,
        "row_group_boundary": "execution-group",
        "maximum_row_group_input_bytes": 1_000_000,
        "packs_may_span_execution_groups": True,
        "ordered_queries": 1,
        "sort_extent_queries": 0,
        "target_output_bytes": 1,
        "hard_row_group_input_bytes": 1_000_000,
        "hard_output_bytes": 1_000_000,
        "cell_boundary_flushes": 0,
        "hot_cell_hard_splits": 0,
        "output_cap_splits": 0,
    }


def test_highly_compressible_logical_input_does_not_force_undersized_pack_close():
    logical_bytes = 0
    physical_bytes = 0
    closes = 0
    # Simulate twenty 32 MiB row groups compressing 16:1. Logical input crosses
    # the former 512 MiB aggregate limit while the physical pack remains a tail.
    for _ in range(20):
        logical_bytes += places_map.MAX_PACK_ROW_GROUP_INPUT_BYTES
        physical_bytes += places_map.MAX_PACK_ROW_GROUP_INPUT_BYTES // 16
        closes += places_map._physical_pack_target_reached(  # noqa: SLF001
            physical_bytes=physical_bytes,
            target_bytes=places_map.DEFAULT_TARGET_FRAGMENT_INPUT_BYTES,
        )
    assert logical_bytes > places_map.DEFAULT_MAX_FRAGMENT_INPUT_BYTES
    assert physical_bytes < places_map.DEFAULT_TARGET_FRAGMENT_INPUT_BYTES // 2
    assert closes == 0


def test_map_enforces_hard_per_task_fragment_cap(tmp_path):
    rows = [
        valid_row("00000000-0000-0000-0000-000000000001"),
        valid_row(
            "00000000-0000-0000-0000-000000000002",
            longitude=100.0,
            latitude=40.0,
        ),
    ]
    with pytest.raises(ValueError, match="hard content-pack count cap"):
        run_map_task(
            inventory_for_rows(rows),
            task_index=0,
            output_dir=tmp_path,
            batch_reader=reader_for(rows),
            fragment_writer=fake_writer,
            target_fragment_input_bytes=1,
            max_task_fragments=1,
        )


def test_map_accepts_exact_fragment_cap_when_final_pack_reaches_target(tmp_path):
    rows = [valid_row("00000000-0000-0000-0000-000000000001")]

    report = run_map_task(
        inventory_for_rows(rows),
        task_index=0,
        output_dir=tmp_path,
        batch_reader=reader_for(rows),
        fragment_writer=fake_writer,
        target_fragment_input_bytes=1,
        max_task_fragments=1,
    )

    assert report["fragments"]["count"] == 1
    assert report["fragments"]["records"] == 1


def test_map_rejects_reader_that_does_not_reconcile_inventory_range(tmp_path):
    rows = [valid_row("00000000-0000-0000-0000-000000000001")]
    inventory = inventory_for_rows(rows)

    def empty_reader(_source, _row_range):
        return iter(())

    with pytest.raises(ValueError, match="range input differs"):
        run_map_task(
            inventory,
            task_index=0,
            output_dir=tmp_path,
            batch_reader=empty_reader,
            fragment_writer=fake_writer,
        )
