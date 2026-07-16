"""Tests for the non-promotable Places experiment (experiment_places_shard.py)."""

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from experiment_places_shard import (
    build_places_shard,
    build_places_shards,
    compute_places_importance,
    compute_places_stored_importance,
    write_places_build_meta,
)

import duckdb


class TestPlacesShardBuild:
    @staticmethod
    def write_places_parquet(path: Path):
        duckdb.sql(f"""
            COPY (
                SELECT * FROM (VALUES
                    ('confidence-only', 1, 'Confidence Only', 34.0, -118.0,
                     -118.01, 33.99, -117.99, 34.01, 'US', 'US-CA', 'Los Angeles',
                     NULL, NULL, NULL, NULL, 0.99, NULL, NULL, 'open'),
                    ('prominent-place', 1, 'Prominent Place', 37.6, -122.4,
                     -122.41, 37.59, -122.39, 37.61, 'US', 'US-CA', 'San Francisco',
                     'airport', 'transport', 'Known Brand', 'Q123', 0.70, NULL, NULL, 'open'),
                    ('closed-place', 1, 'Closed Place', 37.7, -122.3,
                     -122.31, 37.69, -122.29, 37.71, 'US', 'US-CA', 'San Francisco',
                     'airport', 'transport', 'Closed Brand', 'Q999', 1.0, NULL, NULL,
                     'permanently_closed')
                ) AS t(
                    gers_id, version, primary_name, lat, lon,
                    bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                    country, region, locality,
                    category_primary, basic_category,
                    brand_name, brand_wikidata, confidence,
                    search_name_base, search_context_base, operating_status
                )
            ) TO '{path}' (FORMAT PARQUET)
        """)

    def test_sampling_and_stored_ranking_are_independent(self, tmp_path):
        source = tmp_path / "places.parquet"
        self.write_places_parquet(source)
        shard_path = tmp_path / "US-CA-places.db"

        info = build_places_shard(
            source,
            shard_path,
            "test",
            region_code="US-CA",
            limit=1,
            sampling_strategy="experimental-prominence",
        )

        db = sqlite3.connect(shard_path)
        rows = db.execute(
            "SELECT gers_id, type, importance FROM divisions"
        ).fetchall()
        db.close()
        assert info["record_count"] == 1
        # The rejected prominence baseline selects this row, but default stored
        # importance remains confidence-only.
        assert rows == [("prominent-place", "place", pytest.approx(0.7))]
        assert compute_places_importance(
            0.70, "Known Brand", "Q123", "airport", None
        ) == pytest.approx(0.9)
        assert compute_places_stored_importance(
            "experimental-prominence",
            0.70,
            "Known Brand",
            "Q123",
            "airport",
            None,
        ) == pytest.approx(0.9)

    def test_permanently_closed_flat_places_are_excluded(self, tmp_path):
        source = tmp_path / "places.parquet"
        self.write_places_parquet(source)
        shard_path = tmp_path / "US-CA-places.db"

        info = build_places_shard(source, shard_path, "test", region_code="US-CA")

        db = sqlite3.connect(shard_path)
        ids = {row[0] for row in db.execute("SELECT gers_id FROM divisions")}
        db.close()
        assert info["record_count"] == 2
        assert ids == {"confidence-only", "prominent-place"}

    def test_collection_uses_worker_visible_shard_id_and_href(self, tmp_path):
        source = tmp_path / "places.parquet"
        self.write_places_parquet(source)
        version_dir = tmp_path / "test-version"
        version_dir.mkdir()
        (version_dir / "collection.json").write_text("""{
            "items": {
                "HEAD": {
                    "record_count": 2,
                    "size_bytes": 100,
                    "href": "./shards/HEAD.db",
                    "bbox": [-180, -90, 180, 90]
                }
            },
            "summaries": {}
        }""")
        args = SimpleNamespace(
            places_parquet=source,
            places_region="US-CA",
            places_limit=1,
            places_sampling_strategy="experimental-prominence",
            places_ranking_strategy="confidence",
            experimental_places_bbox_slice=True,
            overture_release=None,
        )

        infos = build_places_shards(args, "test-version", version_dir)

        assert set(infos) == {"EXPERIMENT-CA-BBOX-places"}
        places_collection = json.loads(
            (version_dir / "places-collection.json").read_text()
        )
        item = places_collection["items"]["EXPERIMENT-CA-BBOX-places"]
        assert item["href"] == (
            "./places-experimental/EXPERIMENT-CA-BBOX-places.db"
        )
        assert item["sha256"]
        assert places_collection["extent"]["spatial"]["bbox"] == [item["bbox"]]
        assert next(
            link for link in places_collection["links"] if link["rel"] == "self"
        )["href"] == "./places-collection.json"
        forward_collection = json.loads(
            (version_dir / "collection.json").read_text()
        )
        assert "EXPERIMENT-CA-BBOX-places" not in forward_collection["items"]
        assert forward_collection["summaries"] == {}

    def test_places_bbox_build_requires_explicit_non_promotable_acknowledgement(
        self, tmp_path
    ):
        args = SimpleNamespace(
            places_parquet=tmp_path / "missing.parquet",
            places_region="US-CA",
            places_limit=None,
            places_sampling_strategy=None,
            experimental_places_bbox_slice=False,
        )
        with pytest.raises(ValueError, match="non-promotable|cannot be promoted"):
            build_places_shards(args, "test-version", tmp_path)

    def test_places_limit_requires_explicit_sampling_strategy(self, tmp_path):
        source = tmp_path / "places.parquet"
        self.write_places_parquet(source)
        with pytest.raises(ValueError, match="explicit strategy"):
            build_places_shard(
                source,
                tmp_path / "slice.db",
                "test",
                region_code="US-CA",
                limit=1,
            )

    def test_places_download_preserves_all_root_sources_and_bbox_label(self):
        sql = (
            Path(__file__).parent.parent / "scripts" / "download_places.sql"
        ).read_text()
        assert "overture_release" in sql
        assert "sources," in sql
        assert "root_sources" in sql
        assert "root_source_count" in sql
        assert "list_extract(list_filter(sources" not in sql.lower()
        assert "places-CA-bbox.parquet" in sql


class TestPlacesBuildMeta:
    def test_places_build_records_places_input(self, tmp_path):
        places_input = tmp_path / "places.parquet"
        places_input.write_bytes(b"test")
        args = SimpleNamespace(
            places_parquet=places_input,
            experimental_places_bbox_slice=True,
            places_region="US-CA",
            places_limit=None,
            places_sampling_strategy=None,
            places_ranking_strategy="confidence",
            overture_release="2026-06-17.0",
        )
        out = write_places_build_meta(
            "test-version",
            tmp_path,
            {"EXPERIMENT-CA-BBOX-places": {"record_count": 1, "size_bytes": 4}},
            args,
        )

        meta = json.loads(out.read_text())
        assert meta["input"] == {"parquet": str(places_input), "size_bytes": 4}
        assert meta["args"]["places"] is True
        assert meta["division_s3_paths"] == []
        assert meta["source_s3_paths"] == [
            "s3://overturemaps-us-west-2/release/2026-06-17.0/"
            "theme=places/type=place/*"
        ]
