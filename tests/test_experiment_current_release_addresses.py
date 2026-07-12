"""Tests for the bounded current-release address/source experiment."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import experiment_current_release_addresses as experiment  # noqa: E402


def test_sample_design_is_pinned_stratified_and_globally_varied():
    assert experiment.RELEASE == "2026-06-17.0"
    assert len(experiment.SAMPLE_BOXES) == 12
    assert {box.stratum for box in experiment.SAMPLE_BOXES} == {
        "high-rise",
        "dense",
        "suburban",
        "rural",
    }
    assert all(
        sum(box.stratum == stratum for box in experiment.SAMPLE_BOXES) == 3
        for stratum in {box.stratum for box in experiment.SAMPLE_BOXES}
    )
    assert len({box.expected_country for box in experiment.SAMPLE_BOXES}) >= 8
    assert len({box.name for box in experiment.SAMPLE_BOXES}) == 12


def test_query_has_identity_provenance_and_hard_cap():
    box = experiment.SAMPLE_BOXES[0]
    count_query = experiment.candidate_count_query(box)
    query = experiment.sample_query(box, 123, 456)

    assert "'2026-06-17.0' AS overture_release" in query
    assert "'v1.17.0' AS overture_schema_version" in query
    assert "version," in query
    assert "address_levels," in query
    assert "postal_city," in query
    assert "unit," in query
    assert "sources," in query
    assert "AS root_sources" in query
    assert "456::BIGINT AS bbox_population" in query
    assert "ROW_NUMBER() OVER (ORDER BY md5(id))" in query
    assert "deterministic_sample_rank <= 123" in query
    assert "geometry IS NOT NULL" in query
    assert "ST_GeometryType(geometry) = 'POINT'" in query
    assert "ST_X(geometry) >=" in query and "ST_Y(geometry) <=" in query
    assert "bbox IS NULL AS bbox_missing" in query
    where_clause = query.split("WHERE", 1)[1]
    assert "bbox.xmin" not in where_clause
    assert "ROW_NUMBER" not in count_query
    assert "ST_X(geometry)" in count_query


def test_multi_box_query_scans_once_then_exactly_verifies_geometry():
    query = experiment.multi_box_sample_query(
        experiment.SAMPLE_BOXES[:2], row_cap=123, candidate_count_cap=456
    )

    assert query.count("read_parquet(") == 1
    assert "bbox_pruned AS MATERIALIZED" in query
    assert "bbox.xmax >=" in query and "bbox.ymin <=" in query
    assert "ST_GeometryType(address.geometry) = 'POINT'" in query
    assert "ST_X(address.geometry) >= box.xmin" in query
    assert "PARTITION BY sample_box ORDER BY md5(id)" in query
    assert "bbox_population <= 456" not in query
    assert "deterministic_sample_rank <= 123" in query
    assert "bbox-prefiltered-geometry-verified" in query
    assert "Post-query artifact acceptance" in experiment.CANDIDATE_CAP_SCOPE
    assert "does not bound the remote scan" in experiment.CANDIDATE_CAP_SCOPE


def test_multi_box_query_rejects_empty_or_unsafe_caps():
    with pytest.raises(ValueError, match="at least one"):
        experiment.multi_box_sample_query((), 10, 100)
    with pytest.raises(ValueError, match="at least row_cap"):
        experiment.multi_box_sample_query(experiment.SAMPLE_BOXES[:1], 10, 9)


def test_over_cap_boxes_distinguishes_rejection_from_true_absence():
    metrics = [
        {"box": "empty", "bbox_population": 0},
        {"box": "accepted", "bbox_population": 100},
        {"box": "rejected", "bbox_population": 501},
    ]
    assert experiment.over_cap_boxes(metrics, 500) == [("rejected", 501)]


def test_query_rejects_unbounded_row_caps():
    with pytest.raises(ValueError, match="row_cap"):
        experiment.sample_query(experiment.SAMPLE_BOXES[0], 0, 1)
    with pytest.raises(ValueError, match="row_cap"):
        experiment.sample_query(
            experiment.SAMPLE_BOXES[0], experiment.MAX_ROW_CAP + 1, 1
        )


def make_sample(tmp_path: Path, release: str = experiment.RELEASE) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "sample.parquet"
    connection = duckdb.connect()
    connection.execute("""
        CREATE TABLE sample(
            overture_id VARCHAR, overture_release VARCHAR,
            overture_schema_version VARCHAR, version INTEGER,
            lon DOUBLE, lat DOUBLE,
            bbox_xmin DOUBLE, bbox_ymin DOUBLE, bbox_xmax DOUBLE, bbox_ymax DOUBLE,
            bbox_missing BOOLEAN, bbox_geometry_discordant BOOLEAN,
            country VARCHAR, number VARCHAR, street VARCHAR, unit VARCHAR,
            postcode VARCHAR, postal_city VARCHAR,
            address_levels STRUCT(value VARCHAR)[], state VARCHAR, locality VARCHAR,
            sources STRUCT(
                property VARCHAR, dataset VARCHAR, license VARCHAR, record_id VARCHAR,
                update_time VARCHAR, confidence DOUBLE, between VARCHAR
            )[],
            root_sources STRUCT(
                property VARCHAR, dataset VARCHAR, license VARCHAR, record_id VARCHAR,
                update_time VARCHAR, confidence DOUBLE, between VARCHAR
            )[],
            root_source_count BIGINT, sample_box VARCHAR, sample_stratum VARCHAR,
            expected_country VARCHAR, bbox_population BIGINT,
            deterministic_sample_rank BIGINT, sample_selection_contract VARCHAR
        )
    """)
    root = """struct_pack(
        property := '', dataset := 'synthetic/root', license := 'CC0-1.0',
        record_id := 'record-1', update_time := '2026-06-01T00:00:00Z',
        confidence := 0.8, "between" := '0.0-1.0'
    )"""
    property_source = """struct_pack(
        property := '/geometry', dataset := 'synthetic/geometry',
        license := NULL::VARCHAR, record_id := 'geometry-1',
        update_time := NULL::VARCHAR, confidence := NULL::DOUBLE,
        "between" := NULL::VARCHAR
    )"""
    connection.execute(f"""
        INSERT INTO sample VALUES
        ('a', '{release}', '{experiment.SCHEMA_VERSION}', 1,
         -74.000, 40.710, NULL, NULL, NULL, NULL, TRUE, NULL,
         'US', '1', 'Main St', 'A', '10001', NULL,
         [struct_pack(value := 'NY'), struct_pack(value := 'New York')],
         'NY', 'New York', [{root}, {property_source}], [{root}], 1,
         'manhattan', 'high-rise', 'US', 2, 1, 'geometry-authoritative'),
        ('b', '{release}', '{experiment.SCHEMA_VERSION}', 1,
         -73.999, 40.711, -73.5, 41.0, -73.4, 41.1, FALSE, TRUE,
         'US', '1', 'Main St', 'B', '10001', NULL,
         [struct_pack(value := 'NY'), struct_pack(value := 'New York')],
         'NY', 'New York', [{root}], [{root}], 1,
         'manhattan', 'high-rise', 'US', 2, 2, 'geometry-authoritative')
    """)
    connection.execute(f"COPY sample TO '{path}' (FORMAT PARQUET)")
    connection.close()
    return path


def test_synthetic_sample_reports_bbox_and_all_sourceitem_provenance(tmp_path: Path):
    path = make_sample(tmp_path)
    report = experiment.analyze_sample(
        path,
        (experiment.SAMPLE_BOXES[0],),
        2,
        [],
        overall_byte_cap=1_000_000,
    )

    box = report["sampling"]["boxes"][0]
    assert box["missing_bbox_rows"] == 1
    assert box["discordant_bbox_rows"] == 1
    coverage = {
        item["scope"]: item
        for item in report["source_evidence"]["source_item_coverage"]
    }
    assert coverage["all"]["source_item_records"] == 3
    assert coverage["all"]["record_id_records"] == 3
    assert coverage["all"]["between_records"] == 2
    assert coverage["root"]["source_item_records"] == 2
    assert coverage["root"]["non_root_property_records"] == 0
    assert report["tooling"]["sample_input"]["sha256"]
    benchmark = report["address_benchmark"]
    assert "proxy_context_key_coordinate_variation" in benchmark
    assert "exact_key_coordinate_variation" not in benchmark


def test_reused_sample_validation_fails_closed_on_release_rows_and_bytes(
    tmp_path: Path,
):
    path = make_sample(tmp_path, release="wrong-release")
    with pytest.raises(ValueError, match="release/schema mismatch"):
        experiment.validate_sample(path, (experiment.SAMPLE_BOXES[0],), 2, 1_000_000)

    valid = make_sample(tmp_path / "valid")
    with pytest.raises(ValueError, match="row cap exceeded"):
        experiment.validate_sample(valid, (experiment.SAMPLE_BOXES[0],), 1, 1_000_000)
    with pytest.raises(ValueError, match="workspace cap"):
        experiment.validate_sample(valid, (experiment.SAMPLE_BOXES[0],), 2, 1)


def test_recursive_sanitizer_uses_whitelist_and_removes_row_examples():
    sanitized = experiment.sanitize_benchmark_report(
        {
            "normalization_collisions": {
                "lossy_collision_keys": 1,
                "examples": [{"record_id": "secret", "nested": {"overture_id": "x"}}],
            },
            "address_derived_street_proxy": {"highest_fanout": [{"street": "Main"}]},
            "exact_key_coordinate_variation": {"exact_keys_with_coordinates": 2},
            "not_whitelisted": {"safe_looking": "but excluded"},
        }
    )

    text = str(sanitized)
    assert "secret" not in text and "Main" not in text
    assert "not_whitelisted" not in sanitized
    assert sanitized["proxy_context_key_coordinate_variation"] == {
        "proxy_context_keys_with_coordinates": 2
    }


def test_markdown_keeps_representativeness_and_confidence_warnings():
    report = {
        "release": experiment.RELEASE,
        "schema_version": experiment.SCHEMA_VERSION,
        "sampling": {
            "boxes": [
                {
                    "name": "demo",
                    "stratum": "dense",
                    "expected_country": "ZZ",
                    "sampled_rows": 5,
                    "bbox_population": 10,
                    "sample_fraction": 0.5,
                    "distinct_root_datasets": 2,
                }
            ]
        },
        "source_evidence": {
            "summary": {
                "root_source_records": 5,
                "features_with_root_source": 5,
                "distinct_datasets": 2,
                "distinct_populated_licenses": 1,
                "update_time_records": 2,
                "confidence_records": 1,
            },
            "confidence_warning": "not calibrated across datasets",
            "freshness_warning": "not a guaranteed observation time",
            "datasets": [],
            "source_item_coverage": [],
        },
        "data_maturity": "Alpha",
        "address_benchmark": {
            "input": {"row_count": 5},
            "identity_and_provenance": {"root_cardinality": {"multiple_root_rows": 1}},
            "unit_and_base_key_density": {"keyable_rows": 4, "rows_with_unit": 1},
            "normalization_collisions": {"lossy_collision_keys": 0},
            "proxy_context_key_coordinate_variation": {
                "proxy_context_coordinate_variant_keys": 1,
                "proxy_context_keys_over_10m_envelope_spread": 0,
            },
        },
        "proxy_context_key": {
            "definition": experiment.PROXY_CONTEXT_KEY_DEFINITION,
            "coordinate_spread_warning": "envelope proxy, not pairwise distance",
        },
        "gold_queries": {"reason": "not independently verified"},
        "limitations": ["purposive"],
    }
    report["sampling"]["bounds_warning"] = "does not bound S3 bytes scanned"

    markdown = experiment.render_markdown(report)
    assert "not a statistically representative sample" in markdown
    assert "not calibrated across datasets" in markdown
    assert "No gold queries were created" in markdown
