import sys
from pathlib import Path

import duckdb
import pytest


SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import prepare_id_smoke_preview as preview  # noqa: E402


def test_preview_catalog_is_fixed_and_isolated():
    catalog = preview.build_preview_catalog("smoketest-id")
    child = next(link for link in catalog["links"] if link["rel"] == "child")
    assert child == {
        "rel": "child",
        "href": "./id-collection.json",
        "type": "application/json",
        "latest": True,
    }
    self_link = next(link for link in catalog["links"] if link["rel"] == "self")
    assert self_link["href"] == "./catalog.json"
    with pytest.raises(ValueError, match="fixed"):
        preview.build_preview_catalog("2026-07-12.0")


def test_select_preview_cases_requires_current_and_historical_v3_rows(tmp_path):
    shard = tmp_path / "000.parquet"
    con = duckdb.connect()
    try:
        con.execute(
            """
            CREATE TABLE cases(
                id UUID, bbox_xmin FLOAT, bbox_ymin FLOAT,
                bbox_xmax FLOAT, bbox_ymax FLOAT,
                source_file_id INTEGER, last_seen_release_id INTEGER,
                registry_member BOOLEAN
            )
            """
        )
        con.execute(
            """
            INSERT INTO cases VALUES
              ('00000000-0000-4000-8000-000000000001', 1, 2, 3, 4, 7, NULL, false),
              ('00000000-0000-4000-8000-000000000002', 5, 6, 7, 8, NULL, 2, true)
            """
        )
        con.execute(f"COPY cases TO '{shard}' (FORMAT PARQUET)")
        selected = preview.select_preview_cases(con, [str(shard)])
    finally:
        con.close()
    assert selected["current"]["source_file_id"] == 7
    assert selected["historical"]["last_seen_release_id"] == 2
    assert selected["historical"]["registry_member"] is True


def test_select_preview_cases_fails_without_both_classes(tmp_path):
    shard = tmp_path / "000.parquet"
    con = duckdb.connect()
    try:
        con.execute(
            """
            COPY (
              SELECT '00000000-0000-4000-8000-000000000001'::UUID AS id,
                     1::FLOAT bbox_xmin, 2::FLOAT bbox_ymin,
                     3::FLOAT bbox_xmax, 4::FLOAT bbox_ymax,
                     7::INTEGER source_file_id,
                     NULL::INTEGER last_seen_release_id,
                     false::BOOLEAN registry_member
            ) TO ? (FORMAT PARQUET)
            """,
            [str(shard)],
        )
        with pytest.raises(RuntimeError, match="historical"):
            preview.select_preview_cases(con, [str(shard)])
    finally:
        con.close()


def test_historical_case_is_bound_to_a_non_current_dictionary_release():
    cases = {
        "current": {},
        "historical": {"last_seen_release_id": 2},
    }
    bound = preview.bind_expected_releases(
        cases,
        {"last_seen_releases": ["2026-04-01.0", "2026-05-01.0"]},
        "2026-06-01.0",
    )
    assert bound["current"]["expected_last_seen_release"] == "2026-06-01.0"
    assert bound["historical"]["expected_last_seen_release"] == "2026-05-01.0"


def test_historical_case_cannot_expand_to_current_release():
    with pytest.raises(RuntimeError, match="current release"):
        preview.bind_expected_releases(
            {"current": {}, "historical": {"last_seen_release_id": 1}},
            {"last_seen_releases": ["2026-06-01.0"]},
            "2026-06-01.0",
        )


def test_current_case_is_bound_to_exact_dictionary_entry_and_bbox():
    cases = {
        "current": {
            "source_file_id": 2,
            "registry_member": False,
            "bbox_xmin": 1.25,
            "bbox_ymin": 2.5,
            "bbox_xmax": 3.75,
            "bbox_ymax": 4.0,
        },
        "historical": {
            "bbox_xmin": 5.0,
            "bbox_ymin": 6.0,
            "bbox_xmax": 7.0,
            "bbox_ymax": 8.0,
        },
    }
    dictionary = {
        "source_files": [
            {"theme": "addresses", "feature_type": "address", "filename": "a.parquet"},
            {"theme": "places", "feature_type": "place", "filename": "b.parquet"},
        ]
    }
    bound = preview.bind_expected_current_locator(
        cases, dictionary, "2026-06-01.0"
    )
    assert bound["current"]["expected_feature_type"] == "place"
    assert bound["current"]["expected_theme"] == "places"
    assert bound["current"]["expected_filename"] == "b.parquet"
    assert bound["current"]["expected_registry_member"] is False
    assert bound["current"]["expected_overture_path"] == (
        "release/2026-06-01.0/theme=places/type=place/b.parquet"
    )
    assert bound["current"]["expected_bbox"] == {
        "xmin": 1.25,
        "ymin": 2.5,
        "xmax": 3.75,
        "ymax": 4.0,
    }
    assert bound["historical"]["expected_bbox"]["xmin"] == 5.0


def test_current_case_rejects_invalid_compact_source_file_id():
    cases = {
        "current": {"source_file_id": 2},
        "historical": {},
    }
    with pytest.raises(RuntimeError, match="source-file ID"):
        preview.bind_expected_current_locator(
            cases,
            {"source_files": [{"theme": "places", "feature_type": "place", "filename": "a.parquet"}]},
            "2026-06-01.0",
        )
