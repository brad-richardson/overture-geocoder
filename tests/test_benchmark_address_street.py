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
            overture_id VARCHAR, overture_release VARCHAR,
            number VARCHAR, street VARCHAR, unit VARCHAR,
            postcode VARCHAR, state VARCHAR, locality VARCHAR, postal_city VARCHAR,
            country VARCHAR, lon DOUBLE, lat DOUBLE, version INTEGER,
            root_source_count BIGINT, search_text VARCHAR,
            sources STRUCT(
                property VARCHAR, dataset VARCHAR, license VARCHAR, record_id VARCHAR,
                update_time TIMESTAMPTZ, confidence DOUBLE
            )[],
            root_sources STRUCT(
                property VARCHAR, dataset VARCHAR, license VARCHAR, record_id VARCHAR,
                update_time TIMESTAMPTZ, confidence DOUBLE
            )[]
        )
    """)
    rows = [
        ("a", "2026-06-17.0", "12-14", "O'Connell St.", "A", "02101", "MA", "Boston", None,
         "US", -71.0, 42.0, 3, 1, "12-14 o'connell st. a", None, None),
        ("b", "2026-06-17.0", "12-14", "O'Connell St.", "A", "02101", "MA", "Boston", None,
         "US", -71.0, 42.00005, 3, 1, "12-14 o'connell st. a", None, None),
        ("c", "2026-06-17.0", "12-14", "O'Connell St.", "B", "02101", "MA", "Boston", None,
         "US", -71.0, 42.0, 3, 1, "12-14 o'connell st. b", None, None),
        ("d", "2026-06-17.0", "1214", "OConnell St", "A", "02101", "MA", "Boston", None,
         "US", -71.0, 42.0, 3, 1, "1214 oconnell st a", None, None),
        ("e", "2026-06-17.0", "5", "Main-St", None, "02139", "MA", "Cambridge", None,
         "US", -71.1, 42.3, 2, 1, "5 main-st", None, None),
        ("f", "2026-06-17.0", "5", "Main St", None, "02139", "MA", "Cambridge", None,
         "US", -71.1, 42.3, 2, 1, "5 main st", None, None),
        ("g", "2026-06-17.0", "5", "Main-St", None, "02139", "MA", "Cambridge", None,
         "US", -71.1, 42.32, 2, 1, "5 main-st", None, None),
        ("h", "2026-06-17.0", "1", "Broadway", None, "02144", "MA", "Somerville", "Boston",
         "US", -71.1, 42.4, 1, 2, "1 broadway somerville boston", None, None),
        ("i", "2026-06-17.0", "2", "Broadway", None, "02116", "MA", "Boston", None,
         "US", -71.05, 42.35, 1, 1, "2 broadway", None, None),
        ("j", "2026-06-17.0", None, "Unnamed", None, None, "MA", None, "Boston",
         "US", -71.05, 42.35, None, None, None, None, None),
    ]
    connection.executemany(
        "INSERT INTO addresses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    root = """struct_pack(
        property := '', dataset := 'demo/root', license := 'CC0-1.0',
        record_id := overture_id, update_time := TIMESTAMPTZ '2026-01-01T00:00:00Z',
        confidence := 0.8
    )"""
    second_root = """struct_pack(
        property := '', dataset := 'demo/second', license := NULL::VARCHAR,
        record_id := 'second-' || overture_id, update_time := NULL::TIMESTAMPTZ,
        confidence := NULL::DOUBLE
    )"""
    property_source = """struct_pack(
        property := '/geometry', dataset := 'demo/geometry', license := 'CC0-1.0',
        record_id := 'geometry-' || overture_id,
        update_time := TIMESTAMPTZ '2026-01-02T00:00:00Z', confidence := 0.6
    )"""
    connection.execute(f"""
        UPDATE addresses SET sources = CASE
            WHEN overture_id = 'a' THEN [{property_source}]
            WHEN overture_id = 'b' THEN [{root}, {property_source}]
            WHEN overture_id = 'c' THEN [{root}, {second_root}]
            ELSE [{root}]
        END
    """)
    connection.execute("""
        UPDATE addresses
        SET root_sources = list_filter(sources, lambda source: source.property = ''),
            root_source_count = list_count(list_filter(
                sources, lambda source: source.property = ''
            ))
    """)
    connection.execute(f"COPY addresses TO '{output}' (FORMAT PARQUET)")
    connection.close()
    return output


def test_benchmark_reports_collisions_units_and_coordinate_buckets(tmp_path: Path):
    report = benchmark.benchmark(make_addresses(tmp_path))

    assert report["input"]["row_count"] == 10
    coverage = {row["field"]: row for row in report["field_coverage"]}
    assert coverage["unit"]["populated_rows"] == 4
    assert coverage["overture_id"]["populated_rows"] == 10
    assert coverage["overture_release"]["populated_rows"] == 10
    assert coverage["root_source_count"]["populated_rows"] == 9

    identity = report["identity_and_provenance"]
    assert identity["release_distinct_count"] == 1
    assert identity["feature_version"]["populated_rows"] == 9
    assert identity["root_cardinality"] == {
        "zero_root_rows": 1,
        "one_root_rows": 8,
        "multiple_root_rows": 1,
        "max_root_sources": 2,
    }
    assert identity["root_source_field_coverage"]["root_source_records"] == 10
    assert identity["root_source_field_coverage"]["license_populated_records"] == 9
    assert identity["property_specific_source_records"] == 2
    assert identity["root_source_count_mismatch_rows"] == 0
    assert len(identity["root_datasets"]) == 2
    assert identity["source_stratified_normalization"]
    assert identity["source_stratified_coordinate_variation"]

    locality = report["locality_and_postal_city"]
    assert locality["rows_with_distinct_postal_city"] == 1
    assert locality["postal_city_only_rows"] == 1

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

    variation = report["exact_key_coordinate_variation"]
    assert variation["coordinate_variant_keys"] == 2
    assert variation["materially_separated_keys_over_10m"] == 1
    buckets = {row["distance_bucket"]: row for row in variation["distance_buckets"]}
    assert buckets["1-10m"]["exact_keys"] == 1
    assert buckets[">1km"]["exact_keys"] == 1


def test_street_metrics_are_labeled_as_address_proxy(tmp_path: Path):
    report = benchmark.benchmark(make_addresses(tmp_path))
    street = report["address_derived_street_proxy"]

    assert street["proxy_only"] is True
    assert "not transportation" in street["warning"]
    assert street["names_spanning_multiple_localities"] == 1
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


def test_lossy_audit_preserves_unicode_letters_and_numbers():
    connection = duckdb.connect()
    expression = benchmark._lossy_sql(benchmark._normalize_sql("?"))
    value = connection.execute(
        f"SELECT {expression}", ["Straße Montréal 東京 １２-3"]
    ).fetchone()[0]
    connection.close()

    assert value == "straßemontréal東京１２3"


def test_mixed_releases_fail_closed_but_can_be_explicitly_reported(tmp_path: Path):
    source = make_addresses(tmp_path)
    mixed = tmp_path / "mixed.parquet"
    connection = duckdb.connect()
    connection.execute("CREATE TABLE mixed AS SELECT * FROM read_parquet(?)", [str(source)])
    connection.execute(
        "UPDATE mixed SET overture_release = '2026-07-15.0' WHERE overture_id = 'a'"
    )
    connection.execute(f"COPY mixed TO '{mixed}' (FORMAT PARQUET)")
    connection.close()

    with pytest.raises(ValueError, match="multiple Overture releases"):
        benchmark.benchmark(mixed)
    report = benchmark.benchmark(mixed, allow_mixed_release=True)
    assert report["identity_and_provenance"]["mixed_release"] is True
    assert report["identity_and_provenance"]["release_distinct_count"] == 2
    assert "multiple Overture releases" in benchmark.render_markdown(report)


@pytest.mark.parametrize(
    ("longitudes", "expected_span"),
    [([179.0, -179.0], 2.0), ([-170.0, 0.0, 170.0], 190.0)],
)
def test_coordinate_span_uses_largest_circular_gap(
    tmp_path: Path,
    longitudes: list[float],
    expected_span: float,
):
    output = tmp_path / f"longitude-{expected_span}.parquet"
    connection = duckdb.connect()
    connection.execute("""
        CREATE TABLE coordinates(
            overture_id VARCHAR, number VARCHAR, street VARCHAR, unit VARCHAR,
            locality VARCHAR, state VARCHAR, postcode VARCHAR, country VARCHAR,
            lon DOUBLE, lat DOUBLE
        )
    """)
    connection.executemany(
        "INSERT INTO coordinates VALUES (?, '1', 'Dateline Road', NULL, "
        "'Example', 'EX', '00000', 'ZZ', ?, 0.0)",
        [(str(index), longitude) for index, longitude in enumerate(longitudes)],
    )
    connection.execute(f"COPY coordinates TO '{output}' (FORMAT PARQUET)")
    connection.close()

    variation = benchmark.benchmark(output)["exact_key_coordinate_variation"]
    assert variation["max_circular_longitude_span_degrees"] == pytest.approx(expected_span)


def test_address_extraction_keeps_identity_release_and_complete_sources():
    sql = (Path(__file__).resolve().parents[1] / "scripts" / "download_addresses.sql").read_text()

    assert "version," in sql
    assert "id as overture_id" in sql
    assert "'__OVERTURE_RELEASE__' as overture_release" in sql
    assert "COALESCE(unit, '')" in sql
    assert "sources," in sql
    assert "as root_sources" in sql
    assert "as root_source_count" in sql
    assert "license and record_id" in sql
    assert "COALESCE(address_levels[2].value, '')" in sql
    assert "COALESCE(postal_city, '')" in sql
