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
                # A spread of spatial cells so the serving-order split yields
                # spatially clustered shards with distinct bboxes.
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
                '' AS alt_names
              FROM read_json_auto('{source_sql}', format='newline_delimited')
              ORDER BY
                FLOOR((lat + 90.0) / 0.25) {direction},
                FLOOR((lon + 180.0) / 0.25) {direction},
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


def test_split_respects_cap_and_preserves_every_row(tmp_path):
    loaded = region.load_places(_fixture(tmp_path, 210))
    for cap in (50, 70, 200, 500):
        chunks = region.split_serving_order(loaded, region.DEFAULT_CELL_DEGREES, cap)
        assert sum(len(chunk) for chunk in chunks) == len(loaded)
        assert all(len(chunk) <= cap for chunk in chunks)
        # ceil(n / cap) shards, and the split is near-even.
        assert len(chunks) == -(-len(loaded) // cap)
        sizes = sorted(len(chunk) for chunk in chunks)
        assert sizes[-1] - sizes[0] <= 1


def test_build_region_produces_multiple_routed_shards(tmp_path):
    fixture = _fixture(tmp_path, 210)
    report = region.build_region(
        fixture,
        tmp_path / "out",
        region_name="us-northeast",
        row_cap=70,
        head_minimum_candidates=2,
    )
    assert report["schema"] == "overture-places-region-build-v1"
    assert report["totals"]["shards"] >= 3
    assert report["totals"]["shard_rows"] == report["totals"]["loaded_places"]
    # Every shard is under the cap, carries its own bbox, and a unique id.
    ids = set()
    for shard in report["shards"]:
        assert shard["rows"] <= 70
        assert shard["id"].startswith("us-northeast-")
        assert shard["id"] not in ids
        ids.add(shard["id"])
        assert len(shard["bbox"]) == 4
        assert shard["bbox"][0] <= shard["bbox"][2]
        assert shard["bbox"][1] <= shard["bbox"][3]
        assert (tmp_path / "out" / shard["object"]).is_file()
    # The routing catalog and packed head are produced objects.
    assert "catalog.pcat" in report["produced_objects"]
    catalog = (tmp_path / "out" / "catalog.pcat").read_bytes()
    magic, length = struct.unpack("<8sI", catalog[:12])
    assert magic == b"PCAT0001"
    payload = json.loads(catalog[12 : 12 + length])
    assert [shard["id"] for shard in payload["shards"]] == [
        shard["id"] for shard in report["shards"]
    ]
    assert report["head"]["status"] == "built"
    assert "head.phrp" in report["produced_objects"]


def test_build_region_is_byte_deterministic(tmp_path):
    fixture = _fixture(tmp_path, 210)
    first = region.build_region(
        fixture, tmp_path / "a", region_name="ne", row_cap=70, head_minimum_candidates=2
    )
    second = region.build_region(
        fixture, tmp_path / "b", region_name="ne", row_cap=70, head_minimum_candidates=2
    )
    assert first["produced_objects"] == second["produced_objects"]
    for name in first["produced_objects"]:
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_streamed_serving_order_matches_in_memory_bytes(tmp_path):
    # Span multiple Parquet row groups so the sequential-reader contract is
    # exercised rather than accidentally passing on a one-group fixture.
    fixture = _fixture(tmp_path, 5_000)
    parquet = tmp_path / "places-serving-order.parquet"
    _serving_order_parquet(fixture, parquet)

    in_memory = region.build_region(
        fixture,
        tmp_path / "in-memory",
        region_name="ne",
        row_cap=2_000,
        build_head=False,
    )
    streamed = region.build_region(
        parquet,
        tmp_path / "streamed",
        region_name="ne",
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


def test_streamed_serving_order_rejects_false_order_claim(tmp_path):
    fixture = _fixture(tmp_path, 80)
    parquet = tmp_path / "places-reversed.parquet"
    _serving_order_parquet(fixture, parquet, reverse=True)

    with pytest.raises(ValueError, match="not monotonic"):
        list(
            region.iter_serving_order_chunks(
                parquet,
                row_cap=40,
                cell_degrees=region.DEFAULT_CELL_DEGREES,
            )
        )


def test_single_shard_when_under_cap(tmp_path):
    fixture = _fixture(tmp_path, 40)
    report = region.build_region(
        fixture,
        tmp_path / "out",
        region_name="ne",
        row_cap=1_000_000,
        head_minimum_candidates=2,
    )
    assert report["totals"]["shards"] == 1


def test_rejects_unsafe_region_name(tmp_path):
    fixture = _fixture(tmp_path, 40)
    with pytest.raises(ValueError):
        region.build_region(fixture, tmp_path / "out", region_name="bad name!")
