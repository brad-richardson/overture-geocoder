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
    REJECTION_PRECEDENCE,
    read_maximum_level_counts,
    run_map_task,
)
from places_partition import quadkey_bbox  # noqa: E402


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


def fake_writer(rows, path, metadata):
    lines = [canonical_json_bytes({"metadata": metadata})]
    lines.extend(canonical_json_bytes(row) for row in rows)
    path.write_bytes(b"\n".join(lines) + b"\n")


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
    assert len(fragments) == 3
    groups = [fragment["execution_group"] for fragment in fragments]
    assert sorted({group: groups.count(group) for group in groups}.values()) == [1, 2]
    assert all(
        f"group={item['execution_group']}" in item["object_key"] for item in fragments
    )
    assert all(item["records"] <= 2 for item in fragments)
    for item in fragments:
        path = output / item["object_key"]
        assert path.stat().st_size == item["bytes"]
        assert path.name == item["sha256"] + ".parquet"
        decoded = [json.loads(line) for line in path.read_text().splitlines()]
        fragment_rows = decoded[1:]
        assert {row["execution_group"] for row in fragment_rows} == {
            item["execution_group"]
        }
        assert all(
            item["minimum_maximum_level_cell"]
            <= row["partition_cell"]
            <= item["maximum_maximum_level_cell"]
            for row in fragment_rows
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

    count_path = output / report["counts"]["object_key"]
    counts = list(read_maximum_level_counts(count_path))
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
    assert report["counts"]["cells"] == 4096
    assert report["counts"]["records"] == 4096
    counts = list(read_maximum_level_counts(tmp_path / report["counts"]["object_key"]))
    assert len(counts) == 4096
    assert sum(records for _, records in counts) == 4096
    assert report["execution"]["execution_group_count"] == 256
    assert report["fragments"]["count"] == 256
    assert report["fragments"]["records"] == 4096
    assert all(
        item["minimum_maximum_level_cell"].startswith(item["execution_group"])
        and item["maximum_maximum_level_cell"].startswith(item["execution_group"])
        and item["maximum_level_cells"] == 16
        for item in report["fragments"]["objects"]
    )
    assert {item["execution_group"] for item in report["fragments"]["objects"]} == {
        "".join(digits)
        for digits in itertools.product("0123", repeat=EXECUTION_GROUP_LEVEL)
    }


def test_map_splits_fragments_to_enforce_actual_byte_cap(tmp_path):
    rows = [valid_row(str(uuid.UUID(int=identifier))) for identifier in range(1, 5)]

    def padded_writer(fragment_rows, path, _metadata):
        identities = canonical_json_bytes([row["gers_id"] for row in fragment_rows])
        path.write_bytes(identities + b"x" * (100 + len(fragment_rows) * 100))

    report = run_map_task(
        inventory_for_rows(rows),
        task_index=0,
        output_dir=tmp_path,
        batch_reader=reader_for(rows),
        fragment_writer=padded_writer,
        fragment_rows=100,
        max_fragment_bytes=300,
        max_task_fragments=4,
    )

    assert report["fragments"]["count"] == 4
    assert report["fragments"]["records"] == 4
    assert all(item["bytes"] <= 300 for item in report["fragments"]["objects"])
    assert len({item["object_key"] for item in report["fragments"]["objects"]}) == 4


def test_map_enforces_hard_per_task_fragment_cap(tmp_path):
    rows = [
        valid_row("00000000-0000-0000-0000-000000000001"),
        valid_row(
            "00000000-0000-0000-0000-000000000002",
            longitude=100.0,
            latitude=40.0,
        ),
    ]
    with pytest.raises(ValueError, match="hard content-fragment count cap"):
        run_map_task(
            inventory_for_rows(rows),
            task_index=0,
            output_dir=tmp_path,
            batch_reader=reader_for(rows),
            fragment_writer=fake_writer,
            max_task_fragments=1,
        )


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
