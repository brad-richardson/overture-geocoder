from __future__ import annotations

import importlib.util
import json
import struct
import sys
from pathlib import Path

import duckdb
import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "build_places_region_shards.py"
SPEC = importlib.util.spec_from_file_location("build_places_region_shards", SCRIPT)
assert SPEC and SPEC.loader
region = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = region
SPEC.loader.exec_module(region)

import places_partition as partition  # noqa: E402


WORLD = [-180.0, -90.0, 180.0, 90.0]


def write_places(path: Path, count: int) -> None:
    rows = []
    for index in range(count):
        rows.append(
            {
                "id": f"00000000-0000-4000-8000-{index:012d}",
                "name": f"Cafe {index % 11}",
                "category": "cafe",
                "brand": "Bean" if index % 5 == 0 else "",
                "locality": "Town",
                "region": "MA",
                "country": "US",
                "lat": 40.0 + (index % 20) * 0.4,
                "lon": -79.0 - (index // 20) * 0.4,
                "confidence": 0.4 + (index % 6) * 0.1,
            }
        )
    path.write_text("\n".join(json.dumps(row) for row in rows))


def _fixture(tmp_path: Path, count: int) -> Path:
    path = tmp_path / f"places-{count}.jsonl"
    write_places(path, count)
    return path


def _serving_order_parquet(source: Path, output: Path, *, reverse: bool = False) -> None:
    source_sql = str(source).replace("'", "''")
    output_sql = str(output).replace("'", "''")
    direction = "DESC" if reverse else "ASC"
    morton = partition.morton_sql("lon", "lat", partition.DEFAULT_MAXIMUM_LEVEL)
    connection = duckdb.connect()
    try:
        connection.execute(
            f"""
            COPY (
              SELECT
                id AS gers_id,
                name AS primary_name,
                '' AS brand_name,
                category AS category_primary,
                locality,
                region,
                country,
                lat,
                lon,
                confidence,
                '' AS alt_names,
                {morton} AS partition_key
              FROM read_json_auto('{source_sql}', format='newline_delimited')
              ORDER BY
                partition_key {direction},
                -CAST(round_even(confidence * 255, 0) AS BIGINT) {direction},
                id {direction}
            ) TO '{output_sql}' (
              FORMAT PARQUET,
              COMPRESSION ZSTD,
              ROW_GROUP_SIZE 2048
            )
            """
        )
    finally:
        connection.close()


def _build(path: Path, output: Path, **kwargs):
    return region.build_region(
        path,
        output,
        region_name=kwargs.pop("region_name", "us-northeast"),
        coverage_bbox=kwargs.pop("coverage_bbox", WORLD),
        **kwargs,
    )


def test_world_quadkeys_are_deterministic_and_cover_boundaries():
    assert partition.point_quadkey(-180.0, -90.0, 3) == "000"
    assert partition.point_quadkey(180.0, 90.0, 3) == "333"
    cell = partition.point_quadkey(-71.0, 42.0, 6)
    xmin, ymin, xmax, ymax = partition.quadkey_bbox(cell)
    assert xmin <= -71.0 <= xmax
    assert ymin <= 42.0 <= ymax
    assert partition.morton_quadkey(partition.point_morton(-71.0, 42.0, 6), 6) == cell


def test_adaptive_splits_are_sticky_and_never_merge():
    counts = [("0000", 40), ("0001", 40)]
    cells, splits = partition.plan_partition_cells(
        counts, minimum_level=2, maximum_level=4, row_cap=50
    )
    assert [(cell.cell, cell.rows) for cell in cells] == [("0000", 40), ("0001", 40)]
    assert splits == ["00", "000"]

    smaller, retained = partition.plan_partition_cells(
        [("0000", 4), ("0001", 4)],
        minimum_level=2,
        maximum_level=4,
        row_cap=50,
        sticky_splits=splits,
    )
    assert [cell.cell for cell in smaller] == ["0000", "0001"]
    assert retained == splits


def test_split_history_rejects_duplicate_or_orphaned_cells():
    with pytest.raises(ValueError, match="unique"):
        partition.validate_split_cells(
            ["00", "00"], minimum_level=2, maximum_level=4
        )
    with pytest.raises(ValueError, match="ancestor"):
        partition.validate_split_cells(
            ["000"], minimum_level=2, maximum_level=4
        )


def test_build_region_produces_stable_spatial_shards(tmp_path):
    report = _build(
        _fixture(tmp_path, 210),
        tmp_path / "out",
        row_cap=70,
        minimum_level=2,
        maximum_level=8,
        head_minimum_candidates=2,
    )
    assert report["schema"] == "overture-places-region-build-v2"
    assert report["totals"]["shards"] >= 3
    assert report["totals"]["shard_rows"] == report["totals"]["loaded_places"]
    ids = set()
    for shard in report["shards"]:
        assert shard["rows"] <= 70
        assert shard["id"] == f"q-{shard['cell']}"
        assert shard["object"] == f"{shard['id']}.pcsh"
        assert shard["id"] not in ids
        ids.add(shard["id"])
        assert shard["bbox"] == partition.quadkey_bbox(shard["cell"])
        assert (tmp_path / "out" / shard["object"]).is_file()

    catalog = (tmp_path / "out" / "catalog.pcat").read_bytes()
    magic, length = struct.unpack("<8sI", catalog[:12])
    assert magic == b"PCAT0001"
    payload = json.loads(catalog[12 : 12 + length])
    assert payload["schema_version"] == 2
    assert payload["coverage"] == WORLD
    assert payload["partition"]["scheme"] == "world-quadkey-v1"
    assert [shard["id"] for shard in payload["shards"]] == [
        shard["id"] for shard in report["shards"]
    ]
    assert report["head"]["status"] == "built"
    assert "head.phrp" in report["produced_objects"]


def test_shard_ids_and_bytes_do_not_depend_on_region_label(tmp_path):
    fixture = _fixture(tmp_path, 210)
    first = _build(
        fixture,
        tmp_path / "a",
        region_name="ne",
        row_cap=70,
        minimum_level=2,
        maximum_level=8,
        head_minimum_candidates=2,
    )
    second = _build(
        fixture,
        tmp_path / "b",
        region_name="renamed-region",
        row_cap=70,
        minimum_level=2,
        maximum_level=8,
        head_minimum_candidates=2,
    )
    assert first["produced_objects"] == second["produced_objects"]
    for name in first["produced_objects"]:
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_streamed_morton_order_matches_in_memory_bytes(tmp_path):
    fixture = _fixture(tmp_path, 5_000)
    parquet = tmp_path / "places-serving-order.parquet"
    _serving_order_parquet(fixture, parquet)

    in_memory = _build(
        fixture,
        tmp_path / "in-memory",
        row_cap=2_000,
        build_head=False,
    )
    streamed = _build(
        parquet,
        tmp_path / "streamed",
        row_cap=2_000,
        build_head=False,
        input_serving_ordered=True,
    )

    assert streamed["totals"] == in_memory["totals"]
    assert streamed["shards"] == in_memory["shards"]
    assert streamed["catalog"] == in_memory["catalog"]
    assert streamed["produced_objects"] == in_memory["produced_objects"]
    for name in streamed["produced_objects"]:
        assert (tmp_path / "streamed" / name).read_bytes() == (
            tmp_path / "in-memory" / name
        ).read_bytes()


def test_streamed_input_rejects_false_order_claim(tmp_path):
    fixture = _fixture(tmp_path, 80)
    parquet = tmp_path / "places-reversed.parquet"
    _serving_order_parquet(fixture, parquet, reverse=True)

    with pytest.raises(ValueError, match="Morton-monotonic"):
        _build(
            parquet,
            tmp_path / "out",
            row_cap=40,
            build_head=False,
            input_serving_ordered=True,
        )


def test_previous_catalog_retains_split_ownership(tmp_path):
    fixture = _fixture(tmp_path, 80)
    first = _build(
        fixture,
        tmp_path / "first",
        row_cap=20,
        minimum_level=2,
        maximum_level=8,
        build_head=False,
    )
    assert first["catalog"]["partition"]["split_cells"]

    second = _build(
        fixture,
        tmp_path / "second",
        row_cap=1_000,
        minimum_level=2,
        maximum_level=8,
        previous_catalog=tmp_path / "first" / "catalog.pcat",
        build_head=False,
    )
    assert [item["cell"] for item in second["shards"]] == [
        item["cell"] for item in first["shards"]
    ]
    assert second["catalog"]["partition"]["split_cells"] == first["catalog"][
        "partition"
    ]["split_cells"]


def test_rejects_input_outside_declared_coverage(tmp_path):
    with pytest.raises(ValueError, match="outside the declared coverage"):
        _build(
            _fixture(tmp_path, 40),
            tmp_path / "out",
            coverage_bbox=[-72.0, 41.0, -70.0, 43.0],
        )


def test_rejects_unsafe_region_name(tmp_path):
    with pytest.raises(ValueError):
        _build(_fixture(tmp_path, 40), tmp_path / "out", region_name="bad name!")
