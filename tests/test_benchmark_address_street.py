"""Synthetic tests for the bounded address/street benchmark."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark_address_street as benchmark  # noqa: E402


def make_addresses(tmp_path: Path) -> Path:
    output = tmp_path / "addresses.parquet"
    connection = duckdb.connect()
    connection.execute("""
        CREATE TABLE addresses(
            gers_id VARCHAR, number VARCHAR, street VARCHAR, unit VARCHAR,
            postcode VARCHAR, state VARCHAR, city VARCHAR, postal_city VARCHAR,
            country VARCHAR, lon DOUBLE, lat DOUBLE, version INTEGER,
            source_dataset VARCHAR, source_update_time TIMESTAMPTZ,
            source_confidence DOUBLE, search_text VARCHAR
        )
    """)
    rows = [
        ("a", "12-14", "O'Connell St.", "A", "02101", "MA", "Boston", None,
         "US", -71.0, 42.0, 3, "demo", "2026-01-01T00:00:00Z", 0.9, "12-14 o'connell st. a"),
        ("b", "12-14", "O'Connell St.", "A", "02101", "MA", "Boston", None,
         "US", -71.0, 42.00005, 3, "demo", "2026-01-01T00:00:00Z", 0.9, "12-14 o'connell st. a"),
        ("c", "12-14", "O'Connell St.", "B", "02101", "MA", "Boston", None,
         "US", -71.0, 42.0, 3, "demo", "2026-01-01T00:00:00Z", 0.9, "12-14 o'connell st. b"),
        ("d", "1214", "OConnell St", "A", "02101", "MA", "Boston", None,
         "US", -71.0, 42.0, 3, "demo", "2026-01-01T00:00:00Z", 0.9, "1214 oconnell st a"),
        ("e", "5", "Main-St", None, "02139", "MA", "Cambridge", None,
         "US", -71.1, 42.3, 2, "demo", "2025-12-01T00:00:00Z", 0.8, "5 main-st"),
        ("f", "5", "Main St", None, "02139", "MA", "Cambridge", None,
         "US", -71.1, 42.3, 2, "demo", "2025-12-01T00:00:00Z", 0.8, "5 main st"),
        ("g", "5", "Main-St", None, "02139", "MA", "Cambridge", None,
         "US", -71.1, 42.32, 2, "demo", "2025-12-01T00:00:00Z", 0.8, "5 main-st"),
        ("h", "1", "Broadway", None, "02144", "MA", "Somerville", None,
         "US", -71.1, 42.4, 1, "demo", "2025-11-01T00:00:00Z", 0.7, "1 broadway"),
        ("i", "2", "Broadway", None, "02116", "MA", "Boston", None,
         "US", -71.05, 42.35, 1, "demo", "2025-11-01T00:00:00Z", 0.7, "2 broadway"),
        ("j", None, "Unnamed", None, None, "MA", None, "Boston",
         "US", -71.05, 42.35, None, None, None, None, None),
    ]
    connection.executemany(
        "INSERT INTO addresses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    connection.execute(f"COPY addresses TO '{output}' (FORMAT PARQUET)")
    connection.close()
    return output


def test_benchmark_reports_collisions_units_and_coordinate_buckets(tmp_path: Path):
    report = benchmark.benchmark(make_addresses(tmp_path))

    assert report["input"]["row_count"] == 10
    coverage = {row["field"]: row for row in report["field_coverage"]}
    assert coverage["unit"]["populated_rows"] == 4
    assert coverage["source_dataset"]["populated_rows"] == 9

    units = report["unit_and_base_key_density"]
    assert units["keyable_rows"] == 9
    assert units["base_keys_with_multiple_units"] == 1
    assert units["max_units_on_one_base"] == 2

    collisions = report["normalization_collisions"]
    assert collisions["lossy_collision_keys"] == 2
    assert collisions["conservative_keys_collapsed"] == 4
    examples = json.dumps(collisions["examples"])
    assert "12-14" in examples
    assert "1214" in examples
    assert "preserve punctuation" in collisions["recommended_normalization"]

    ambiguity = report["exact_key_coordinate_ambiguity"]
    assert ambiguity["coordinate_ambiguous_keys"] == 2
    buckets = {row["distance_bucket"]: row for row in ambiguity["distance_buckets"]}
    assert buckets["1-10m"]["exact_keys"] == 1
    assert buckets[">1km"]["exact_keys"] == 1


def test_street_metrics_are_labeled_as_address_proxy(tmp_path: Path):
    report = benchmark.benchmark(make_addresses(tmp_path))
    street = report["address_derived_street_proxy"]

    assert street["proxy_only"] is True
    assert "not transportation" in street["warning"]
    assert street["names_spanning_multiple_cities"] == 1
    assert street["names_spanning_multiple_postcodes"] >= 1
    markdown = benchmark.render_markdown(report)
    assert "Address-derived street-name proxy" in markdown
    assert "not transportation-segment" in markdown


def test_historical_bytes_are_explicit_linear_extrapolations(tmp_path: Path):
    parquet = make_addresses(tmp_path)
    metrics = tmp_path / "metrics.json"
    metrics.write_text(json.dumps({
        "release": "historical",
        "bbox": [0, 0, 1, 1],
        "addresses": {
            "shards": {"minimal": {"record_count": 5, "size_bytes": 500}}
        },
    }))

    report = benchmark.benchmark(parquet, metrics)
    history = report["historical_byte_extrapolations"]
    assert history["historical_only"] is True
    assert "not build-size forecasts" in history["warning"]
    assert history["estimates"][0]["historical_bytes_per_row"] == 100
    assert history["estimates"][0]["linear_extrapolation_bytes"] == 1000


def test_address_extraction_keeps_unit_version_and_root_source_fields():
    sql = (Path(__file__).resolve().parents[1] / "scripts" / "download_addresses.sql").read_text()

    assert "version," in sql
    assert "COALESCE(unit, '')" in sql
    assert "x.property = ''" in sql
    assert "as source_dataset" in sql
    assert "as source_update_time" in sql
    assert "as source_confidence" in sql
