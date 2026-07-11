#!/usr/bin/env python3
"""Benchmark flattened address data without loading it into Python memory.

The input is a local Parquet file with flattened address columns. All
high-cardinality work stays in DuckDB; Python receives only aggregate rows and
small top-N samples.

The recommended conservative normalization preserves punctuation and field
boundaries. A deliberately lossy punctuation-stripping normalization is
reported only to quantify collisions, not as an indexing recommendation.

Street-name metrics in this report are inferred from address records. They are
a coverage/routing proxy and are not evidence about Overture transportation
segments, connectors, topology, road class, or routability.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

try:
    import duckdb
except ImportError as exc:  # pragma: no cover - exercised by CLI environments
    raise SystemExit("benchmark_address_street.py requires the duckdb package") from exc


REPORT_VERSION = 1
PROXY_WARNING = (
    "Address-derived street metrics are a proxy for searchable name/context "
    "coverage; they are not transportation-segment, connector, topology, "
    "road-class, or routability evidence."
)

CANONICAL_COLUMNS: dict[str, tuple[tuple[str, ...], str]] = {
    "overture_id": (("overture_id", "feature_id", "id", "gers_id"), "VARCHAR"),
    "overture_release": (("overture_release", "release"), "VARCHAR"),
    "number": (("number",), "VARCHAR"),
    "street": (("street",), "VARCHAR"),
    "unit": (("unit",), "VARCHAR"),
    "postcode": (("postcode",), "VARCHAR"),
    "state": (("state", "region"), "VARCHAR"),
    "locality": (("locality", "city"), "VARCHAR"),
    "postal_city": (("postal_city",), "VARCHAR"),
    "country": (("country",), "VARCHAR"),
    "lon": (("lon", "longitude"), "DOUBLE"),
    "lat": (("lat", "latitude"), "DOUBLE"),
    "version": (("version",), "BIGINT"),
    "root_source_count": (("root_source_count",), "BIGINT"),
    "search_text": (("search_text",), "VARCHAR"),
}

TEXT_COLUMNS = {
    "overture_id", "overture_release", "number", "street", "unit", "postcode",
    "state", "locality", "postal_city", "country", "search_text",
}


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalize_sql(expression: str) -> str:
    """Case/space normalize while preserving punctuation and diacritics."""
    return (
        "LOWER(NFC_NORMALIZE(REGEXP_REPLACE(TRIM(COALESCE(CAST("
        f"{expression} AS VARCHAR), '')), "
        r"'\s+', ' ', 'g')))"
    )


def _lossy_sql(expression: str) -> str:
    """Deliberately lossy punctuation removal used only for collision audits."""
    return rf"REGEXP_REPLACE({expression}, '[^\p{{L}}\p{{N}}]+', '', 'g')"


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _fetch_dicts(connection: duckdb.DuckDBPyConnection, query: str) -> list[dict[str, Any]]:
    cursor = connection.execute(query)
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _fetch_one(connection: duckdb.DuckDBPyConnection, query: str) -> dict[str, Any]:
    rows = _fetch_dicts(connection, query)
    return rows[0] if rows else {}


def _input_columns(connection: duckdb.DuckDBPyConnection, path: Path) -> dict[str, str]:
    cursor = connection.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)])
    return {str(row[0]).lower(): str(row[0]) for row in cursor.fetchall()}


def _create_views(
    connection: duckdb.DuckDBPyConnection,
    path: Path,
    input_columns: dict[str, str],
) -> dict[str, bool]:
    availability: dict[str, bool] = {}
    select_items: list[str] = []
    for canonical, (aliases, cast_type) in CANONICAL_COLUMNS.items():
        source = next((input_columns[name] for name in aliases if name in input_columns), None)
        availability[canonical] = source is not None
        expression = _quote_identifier(source) if source else "NULL"
        select_items.append(f"TRY_CAST({expression} AS {cast_type}) AS {canonical}")

    for nested in ("sources", "root_sources"):
        source = input_columns.get(nested)
        availability[nested] = source is not None
        if source is not None:
            select_items.append(f"{_quote_identifier(source)} AS {nested}")

    connection.execute(
        "CREATE TEMP VIEW addresses AS SELECT\n  "
        + ",\n  ".join(select_items)
        + f"\nFROM read_parquet({_sql_literal(str(path))})"
    )

    normalized_items = {
        "number_norm": _normalize_sql("number"),
        "street_norm": _normalize_sql("street"),
        "unit_norm": _normalize_sql("unit"),
        "postcode_norm": _normalize_sql("postcode"),
        "state_norm": _normalize_sql("state"),
        "locality_norm": _normalize_sql("locality"),
        "postal_city_norm": _normalize_sql("postal_city"),
        "country_norm": _normalize_sql("country"),
    }
    connection.execute(
        "CREATE TEMP VIEW normalized_addresses AS SELECT *,\n  "
        + ",\n  ".join(f"{expr} AS {name}" for name, expr in normalized_items.items())
        + "\nFROM addresses"
    )
    return availability


def _field_coverage(
    connection: duckdb.DuckDBPyConnection,
    row_count: int,
    availability: dict[str, bool],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for field in CANONICAL_COLUMNS:
        if field == "root_source_count":
            populated = connection.execute(
                "SELECT COALESCE(COUNT_IF(root_source_count > 0), 0) FROM addresses"
            ).fetchone()[0]
        elif field in TEXT_COLUMNS:
            populated = connection.execute(
                f"SELECT COUNT_IF(NULLIF(TRIM({field}), '') IS NOT NULL) FROM addresses"
            ).fetchone()[0]
        else:
            populated = connection.execute(
                f"SELECT COUNT({field}) FROM addresses"
            ).fetchone()[0]
        result.append({
            "field": field,
            "available_in_input": availability[field],
            "populated_rows": int(populated),
            "coverage": (float(populated) / row_count) if row_count else None,
        })
    return result


def _unit_and_base_density(connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    return _fetch_one(connection, """
        WITH eligible AS (
            SELECT * FROM normalized_addresses
            WHERE number_norm != '' AND street_norm != ''
        ), base_groups AS (
            SELECT number_norm, street_norm, locality_norm, state_norm, postcode_norm, country_norm,
                   COUNT(*) AS row_count,
                   COUNT(DISTINCT unit_norm) FILTER (WHERE unit_norm != '') AS distinct_units
            FROM eligible
            GROUP BY ALL
        )
        SELECT
            (SELECT COUNT(*) FROM eligible) AS keyable_rows,
            (SELECT COUNT_IF(unit_norm != '') FROM eligible) AS rows_with_unit,
            COUNT(*) AS distinct_base_keys,
            COUNT_IF(distinct_units > 1) AS base_keys_with_multiple_units,
            COALESCE(SUM(row_count) FILTER (WHERE distinct_units > 1), 0) AS rows_on_multi_unit_bases,
            COALESCE(MAX(distinct_units), 0) AS max_units_on_one_base,
            COALESCE(AVG(row_count), 0) AS mean_rows_per_base
        FROM base_groups
    """)


def _normalization_collisions(connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    lossy = {
        name: _lossy_sql(name)
        for name in (
            "number_norm", "street_norm", "unit_norm", "locality_norm",
            "state_norm", "postcode_norm", "country_norm",
        )
    }
    base_cte = f"""
        WITH keyed AS (
            SELECT *,
                   {lossy['number_norm']} AS l_number,
                   {lossy['street_norm']} AS l_street,
                   {lossy['unit_norm']} AS l_unit,
                   {lossy['locality_norm']} AS l_locality,
                   {lossy['state_norm']} AS l_state,
                   {lossy['postcode_norm']} AS l_postcode,
                   {lossy['country_norm']} AS l_country
            FROM normalized_addresses
            WHERE number_norm != '' AND street_norm != ''
        ), collisions AS (
            SELECT l_number, l_street, l_unit, l_locality, l_state, l_postcode, l_country,
                   COUNT(*) AS row_count,
                   COUNT(DISTINCT struct_pack(
                       number := number_norm, street := street_norm, unit := unit_norm,
                       locality := locality_norm, state := state_norm, postcode := postcode_norm,
                       country := country_norm
                   )) AS conservative_key_count
            FROM keyed
            GROUP BY l_number, l_street, l_unit, l_locality, l_state, l_postcode, l_country
            HAVING conservative_key_count > 1
        )
    """
    summary = _fetch_one(connection, base_cte + """
        SELECT COUNT(*) AS lossy_collision_keys,
               COALESCE(SUM(conservative_key_count), 0) AS conservative_keys_collapsed,
               COALESCE(SUM(row_count), 0) AS rows_on_lossy_collision_keys,
               COALESCE(MAX(conservative_key_count), 0) AS max_conservative_keys_per_lossy_key
        FROM collisions
    """)
    summary["examples"] = _fetch_dicts(connection, base_cte + """
        , top_collisions AS (
            SELECT * FROM collisions
            ORDER BY conservative_key_count DESC, row_count DESC
            LIMIT 10
        ), distinct_examples AS (
            SELECT DISTINCT l_number, l_street, l_unit, l_locality, l_state,
                   l_postcode, l_country,
                   CONCAT_WS(' | ', number_norm, street_norm, unit_norm,
                             locality_norm, state_norm, postcode_norm, country_norm)
                       AS conservative_example
            FROM keyed
        ), ranked_examples AS (
            SELECT top_collisions.row_count, top_collisions.conservative_key_count,
                   top_collisions.l_number, top_collisions.l_street,
                   top_collisions.l_unit, top_collisions.l_locality,
                   top_collisions.l_state, top_collisions.l_postcode,
                   top_collisions.l_country,
                   distinct_examples.conservative_example,
                   ROW_NUMBER() OVER (
                       PARTITION BY l_number, l_street, l_unit, l_locality, l_state,
                                    l_postcode, l_country
                       ORDER BY distinct_examples.conservative_example
                   ) AS example_rank
            FROM top_collisions
            JOIN distinct_examples USING (
                l_number, l_street, l_unit, l_locality, l_state, l_postcode, l_country
            )
        )
        SELECT row_count, conservative_key_count,
               LIST(conservative_example ORDER BY conservative_example)
                   FILTER (WHERE example_rank <= 5) AS conservative_examples
        FROM ranked_examples
        GROUP BY row_count, conservative_key_count, l_number, l_street, l_unit,
                 l_locality, l_state, l_postcode, l_country
        ORDER BY conservative_key_count DESC, row_count DESC
    """)
    summary["recommended_normalization"] = (
        "NFC normalize, lowercase, trim, and collapse whitespace per structured "
        "component; preserve punctuation, diacritics, and component boundaries."
    )
    summary["lossy_normalization"] = (
        "The comparison removes every character except Unicode letters and numbers "
        "inside each component. It is deliberately lossy and is not recommended "
        "for identity keys."
    )
    return summary


def _locality_alias_evidence(connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    return _fetch_one(connection, """
        SELECT
            COUNT_IF(locality_norm != '') AS rows_with_locality,
            COUNT_IF(postal_city_norm != '') AS rows_with_postal_city,
            COUNT_IF(locality_norm != '' AND postal_city_norm != '') AS rows_with_both,
            COUNT_IF(
                locality_norm != '' AND postal_city_norm != ''
                AND locality_norm != postal_city_norm
            ) AS rows_with_distinct_postal_city,
            COUNT_IF(locality_norm = '' AND postal_city_norm != '') AS postal_city_only_rows
        FROM normalized_addresses
    """)


def _coordinate_ctes(relation: str, dimensions: tuple[str, ...] = ()) -> str:
    key_columns = dimensions + (
        "number_norm", "street_norm", "unit_norm", "locality_norm", "state_norm",
        "postcode_norm", "country_norm",
    )
    keys = ", ".join(key_columns)
    return f"""
        coordinate_rows AS (
            SELECT {keys}, lat, lon,
                   ((lon % 360.0) + 360.0) % 360.0 AS lon360
            FROM {relation}
            WHERE number_norm != '' AND street_norm != ''
              AND lat IS NOT NULL AND lon IS NOT NULL
        ), exact_groups AS (
            SELECT {keys}, COUNT(*) AS row_count,
                   COUNT(DISTINCT struct_pack(lat := lat, lon := lon)) AS coordinate_count,
                   MIN(lat) AS min_lat, MAX(lat) AS max_lat
            FROM coordinate_rows
            GROUP BY {keys}
        ), distinct_longitudes AS (
            SELECT DISTINCT {keys}, lon360 FROM coordinate_rows
        ), longitude_gaps AS (
            SELECT {keys}, lon360,
                   COALESCE(
                       LEAD(lon360) OVER (PARTITION BY {keys} ORDER BY lon360),
                       MIN(lon360) OVER (PARTITION BY {keys}) + 360.0
                   ) - lon360 AS gap_degrees
            FROM distinct_longitudes
        ), longitude_spans AS (
            SELECT {keys}, 360.0 - MAX(gap_degrees) AS longitude_span_degrees
            FROM longitude_gaps
            GROUP BY {keys}
        ), measured AS (
            SELECT exact_groups.*,
                   longitude_spans.longitude_span_degrees,
                   111320.0 * SQRT(
                       POW(max_lat - min_lat, 2)
                       + POW(
                           COS(RADIANS((max_lat + min_lat) / 2.0))
                           * longitude_spans.longitude_span_degrees,
                           2
                       )
                   ) AS spread_m
            FROM exact_groups
            JOIN longitude_spans USING ({keys})
        ), bucketed AS (
            SELECT *, CASE
                WHEN coordinate_count = 1 THEN 'same_coordinate'
                WHEN spread_m <= 1 THEN '0-1m'
                WHEN spread_m <= 10 THEN '1-10m'
                WHEN spread_m <= 100 THEN '10-100m'
                WHEN spread_m <= 1000 THEN '100m-1km'
                ELSE '>1km'
            END AS distance_bucket
            FROM measured
        )
    """


def _coordinate_variation(connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    cte = "WITH " + _coordinate_ctes("normalized_addresses")
    summary = _fetch_one(connection, cte + """
        SELECT COUNT(*) AS exact_keys_with_coordinates,
               COUNT_IF(row_count > 1) AS duplicate_exact_keys,
               COUNT_IF(coordinate_count > 1) AS coordinate_variant_keys,
               COALESCE(SUM(row_count) FILTER (WHERE coordinate_count > 1), 0)
                   AS rows_on_coordinate_variant_keys,
               COUNT_IF(coordinate_count > 1 AND spread_m > 10)
                   AS materially_separated_keys_over_10m,
               COALESCE(MAX(longitude_span_degrees), 0)
                   AS max_circular_longitude_span_degrees,
               COALESCE(MAX(spread_m), 0) AS max_spread_m
        FROM bucketed
    """)
    summary["distance_buckets"] = _fetch_dicts(connection, cte + """
        SELECT distance_bucket, COUNT(*) AS exact_keys, SUM(row_count) AS rows
        FROM bucketed
        WHERE row_count > 1
        GROUP BY distance_bucket
        ORDER BY CASE distance_bucket
            WHEN 'same_coordinate' THEN 0 WHEN '0-1m' THEN 1 WHEN '1-10m' THEN 2
            WHEN '10-100m' THEN 3 WHEN '100m-1km' THEN 4 ELSE 5 END
    """)
    return summary


def _identity_and_provenance(
    connection: duckdb.DuckDBPyConnection,
    availability: dict[str, bool],
) -> dict[str, Any]:
    releases = _fetch_dicts(connection, """
        SELECT overture_release AS release, COUNT(*) AS rows
        FROM addresses
        WHERE NULLIF(TRIM(overture_release), '') IS NOT NULL
        GROUP BY overture_release
        ORDER BY overture_release
        LIMIT 20
    """)
    release_count = int(connection.execute("""
        SELECT COUNT(DISTINCT overture_release)
        FROM addresses
        WHERE NULLIF(TRIM(overture_release), '') IS NOT NULL
    """).fetchone()[0])
    version_rows = _fetch_dicts(connection, """
        SELECT version, COUNT(*) AS rows
        FROM addresses
        WHERE version IS NOT NULL
        GROUP BY version
        ORDER BY rows DESC, version DESC
        LIMIT 20
    """)
    result: dict[str, Any] = {
        "release_distinct_count": release_count,
        "release_values": releases,
        "mixed_release": release_count > 1,
        "mixed_release_warning": (
            "Input contains multiple Overture releases; comparisons and feature versions "
            "must not be combined without explicit --allow-mixed-release."
            if release_count > 1 else None
        ),
        "feature_version": {
            "populated_rows": int(connection.execute(
                "SELECT COUNT(version) FROM addresses"
            ).fetchone()[0]),
            "distinct_count": int(connection.execute(
                "SELECT COUNT(DISTINCT version) FROM addresses WHERE version IS NOT NULL"
            ).fetchone()[0]),
            "top_values": version_rows,
        },
        "nested_sources_available": availability.get("sources", False),
        "nested_root_sources_available": availability.get("root_sources", False),
    }

    if not availability.get("sources") and not availability.get("root_sources"):
        result["source_status"] = "nested_sources_not_available"
        result["root_cardinality"] = {
            "zero_root_rows": None,
            "one_root_rows": None,
            "multiple_root_rows": None,
        }
        result["root_datasets"] = []
        result["source_stratified_normalization"] = []
        result["source_stratified_coordinate_variation"] = []
        return result

    if availability.get("root_sources"):
        root_expression = "root_sources"
    else:
        root_expression = "list_filter(sources, lambda source: source.property = '')"

    root_summary = _fetch_one(connection, f"""
        SELECT
            COUNT_IF(COALESCE(list_count({root_expression}), 0) = 0) AS zero_root_rows,
            COUNT_IF(list_count({root_expression}) = 1) AS one_root_rows,
            COUNT_IF(list_count({root_expression}) > 1) AS multiple_root_rows,
            COALESCE(MAX(list_count({root_expression})), 0) AS max_root_sources
        FROM addresses
    """)
    root_records_cte = f"""
        WITH root_records AS (
            SELECT overture_id, root_source
            FROM addresses,
                 UNNEST({root_expression}) AS roots(root_source)
        )
    """
    root_coverage = _fetch_one(connection, root_records_cte + """
        SELECT COUNT(*) AS root_source_records,
               COUNT_IF(NULLIF(TRIM(root_source.dataset), '') IS NOT NULL)
                   AS dataset_populated_records,
               COUNT_IF(NULLIF(TRIM(root_source.license), '') IS NOT NULL)
                   AS license_populated_records,
               COUNT(root_source.update_time) AS update_time_populated_records,
               COUNT(root_source.confidence) AS confidence_populated_records,
               MIN(root_source.confidence) AS confidence_min,
               MAX(root_source.confidence) AS confidence_max
        FROM root_records
    """)
    root_datasets = _fetch_dicts(connection, root_records_cte + """
        SELECT root_source.dataset AS dataset,
               COUNT(*) AS root_source_records,
               COUNT(DISTINCT overture_id) AS distinct_feature_ids,
               COUNT_IF(NULLIF(TRIM(root_source.license), '') IS NOT NULL)
                   AS license_populated_records,
               COUNT(root_source.update_time) AS update_time_populated_records,
               COUNT(root_source.confidence) AS confidence_populated_records
        FROM root_records
        GROUP BY root_source.dataset
        ORDER BY root_source_records DESC, dataset
        LIMIT 20
    """)
    result["source_status"] = "computed"
    result["root_cardinality"] = root_summary
    result["root_source_field_coverage"] = root_coverage
    result["root_datasets"] = root_datasets

    if availability.get("sources"):
        result["property_specific_source_records"] = int(connection.execute("""
            SELECT COUNT(*)
            FROM addresses,
                 UNNEST(sources) AS source_items(source_item)
            WHERE source_item.property != ''
        """).fetchone()[0])
        if availability.get("root_sources"):
            result["root_source_count_mismatch_rows"] = int(connection.execute("""
                SELECT COUNT_IF(
                    COALESCE(list_count(root_sources), 0)
                    != COALESCE(list_count(list_filter(
                        sources, lambda source: source.property = ''
                    )), 0)
                )
                FROM addresses
            """).fetchone()[0])

    source_relation = f"""
        source_addresses AS (
            SELECT DISTINCT n.overture_id, n.number_norm, n.street_norm, n.unit_norm,
                   n.locality_norm, n.state_norm, n.postcode_norm, n.country_norm,
                   n.lat, n.lon, root_source.dataset AS root_dataset
            FROM normalized_addresses n,
                 UNNEST({root_expression}) AS roots(root_source)
            WHERE NULLIF(TRIM(root_source.dataset), '') IS NOT NULL
        )
    """
    lossy_fields = {
        name: _lossy_sql(name)
        for name in (
            "number_norm", "street_norm", "unit_norm", "locality_norm",
            "state_norm", "postcode_norm", "country_norm",
        )
    }
    result["source_stratified_normalization"] = _fetch_dicts(connection, f"""
        WITH {source_relation}, keyed AS (
            SELECT *,
                   {lossy_fields['number_norm']} AS l_number,
                   {lossy_fields['street_norm']} AS l_street,
                   {lossy_fields['unit_norm']} AS l_unit,
                   {lossy_fields['locality_norm']} AS l_locality,
                   {lossy_fields['state_norm']} AS l_state,
                   {lossy_fields['postcode_norm']} AS l_postcode,
                   {lossy_fields['country_norm']} AS l_country
            FROM source_addresses
            WHERE number_norm != '' AND street_norm != ''
        ), collision_groups AS (
            SELECT root_dataset, l_number, l_street, l_unit, l_locality, l_state,
                   l_postcode, l_country,
                   COUNT(DISTINCT struct_pack(
                       number := number_norm, street := street_norm, unit := unit_norm,
                       locality := locality_norm, state := state_norm,
                       postcode := postcode_norm, country := country_norm
                   )) AS conservative_key_count
            FROM keyed
            GROUP BY ALL
            HAVING conservative_key_count > 1
        ), dataset_rows AS (
            SELECT root_dataset, COUNT(*) AS source_address_rows
            FROM source_addresses GROUP BY root_dataset
        ), dataset_collisions AS (
            SELECT root_dataset, COUNT(*) AS lossy_collision_keys,
                   SUM(conservative_key_count) AS conservative_keys_collapsed
            FROM collision_groups GROUP BY root_dataset
        )
        SELECT dataset_rows.root_dataset AS dataset, source_address_rows,
               COALESCE(lossy_collision_keys, 0) AS lossy_collision_keys,
               COALESCE(conservative_keys_collapsed, 0) AS conservative_keys_collapsed
        FROM dataset_rows
        LEFT JOIN dataset_collisions USING (root_dataset)
        ORDER BY source_address_rows DESC, dataset
        LIMIT 20
    """)
    coordinate_ctes = _coordinate_ctes("source_addresses", ("root_dataset",))
    result["source_stratified_coordinate_variation"] = _fetch_dicts(
        connection,
        f"""
        WITH {source_relation}, {coordinate_ctes}
        SELECT root_dataset AS dataset,
               SUM(row_count) AS address_rows_with_coordinates,
               COUNT(*) AS exact_keys_with_coordinates,
               COUNT_IF(coordinate_count > 1) AS coordinate_variant_keys,
               COUNT_IF(coordinate_count > 1 AND spread_m > 10)
                   AS materially_separated_keys_over_10m
        FROM bucketed
        GROUP BY root_dataset
        ORDER BY address_rows_with_coordinates DESC, dataset
        LIMIT 20
        """,
    )
    return result


def _postcode_distribution(connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    cte = """
        WITH postcodes AS (
            SELECT postcode_norm AS postcode, COUNT(*) AS row_count
            FROM normalized_addresses
            WHERE postcode_norm != ''
            GROUP BY postcode_norm
        )
    """
    summary = _fetch_one(connection, cte + """
        SELECT COUNT(*) AS postcode_count,
               COALESCE(SUM(row_count), 0) AS rows_with_postcode,
               COALESCE(MIN(row_count), 0) AS min_rows,
               COALESCE(QUANTILE_CONT(row_count, 0.5), 0) AS median_rows,
               COALESCE(QUANTILE_CONT(row_count, 0.9), 0) AS p90_rows,
               COALESCE(MAX(row_count), 0) AS max_rows,
               COUNT_IF(row_count > 50000) AS postcodes_over_50k_rows
        FROM postcodes
    """)
    summary["largest"] = _fetch_dicts(connection, cte + """
        SELECT postcode, row_count FROM postcodes
        ORDER BY row_count DESC, postcode
        LIMIT 20
    """)
    return summary


def _street_proxy(connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    cte = """
        WITH streets AS (
            SELECT street_norm AS street,
                   COUNT(*) AS address_rows,
                   COUNT(DISTINCT struct_pack(locality := locality_norm, state := state_norm))
                       FILTER (WHERE locality_norm != '') AS locality_fanout,
                   COUNT(DISTINCT postcode_norm) FILTER (WHERE postcode_norm != '')
                       AS postcode_fanout
            FROM normalized_addresses
            WHERE street_norm != ''
            GROUP BY street_norm
        )
    """
    summary = _fetch_one(connection, cte + """
        SELECT COUNT(*) AS distinct_address_street_names,
               COALESCE(SUM(address_rows), 0) AS address_rows_with_street,
               COUNT_IF(locality_fanout > 1) AS names_spanning_multiple_localities,
               COUNT_IF(postcode_fanout > 1) AS names_spanning_multiple_postcodes,
               COALESCE(QUANTILE_CONT(locality_fanout, 0.5), 0) AS median_locality_fanout,
               COALESCE(QUANTILE_CONT(locality_fanout, 0.9), 0) AS p90_locality_fanout,
               COALESCE(MAX(locality_fanout), 0) AS max_locality_fanout,
               COALESCE(QUANTILE_CONT(postcode_fanout, 0.5), 0) AS median_postcode_fanout,
               COALESCE(QUANTILE_CONT(postcode_fanout, 0.9), 0) AS p90_postcode_fanout,
               COALESCE(MAX(postcode_fanout), 0) AS max_postcode_fanout
        FROM streets
    """)
    summary["highest_fanout"] = _fetch_dicts(connection, cte + """
        SELECT street, address_rows, locality_fanout, postcode_fanout
        FROM streets
        ORDER BY locality_fanout DESC, postcode_fanout DESC, address_rows DESC, street
        LIMIT 20
    """)
    summary["proxy_only"] = True
    summary["warning"] = PROXY_WARNING
    return summary


def historical_byte_extrapolations(
    metrics_path: Path | None,
    current_rows: int,
) -> dict[str, Any]:
    if metrics_path is None:
        return {
            "status": "not_provided",
            "historical_only": True,
            "warning": "Pass --historical-metrics to extrapolate old measured artifacts.",
            "estimates": [],
        }
    raw = json.loads(metrics_path.read_text(encoding="utf-8"))
    addresses = raw.get("addresses", {})
    candidates: list[tuple[str, dict[str, Any]]] = []
    if isinstance(addresses.get("raw"), dict):
        candidates.append(("raw_parquet", addresses["raw"]))
    for name, value in addresses.get("shards", {}).items():
        if isinstance(value, dict):
            candidates.append((str(name), value))

    estimates = []
    for name, value in candidates:
        record_count = int(value.get("record_count") or 0)
        size_bytes = int(value.get("size_bytes") or 0)
        if record_count <= 0 or size_bytes < 0:
            continue
        bytes_per_row = size_bytes / record_count
        estimates.append({
            "artifact": name,
            "historical_record_count": record_count,
            "historical_size_bytes": size_bytes,
            "historical_bytes_per_row": bytes_per_row,
            "linear_extrapolation_rows": current_rows,
            "linear_extrapolation_bytes": int(round(bytes_per_row * current_rows)),
        })
    return {
        "status": "computed",
        "historical_only": True,
        "source": str(metrics_path),
        "source_release": raw.get("release"),
        "source_bbox": raw.get("bbox"),
        "warning": (
            "Historical linear extrapolations are not build-size forecasts. "
            "They inherit the old bbox sample's density, schema, page overhead, "
            "index implementation, and compression behavior."
        ),
        "estimates": estimates,
    }


def benchmark(
    input_path: Path,
    historical_metrics: Path | None = None,
    allow_mixed_release: bool = False,
) -> dict[str, Any]:
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    if historical_metrics is not None and not historical_metrics.is_file():
        raise FileNotFoundError(historical_metrics)

    connection = duckdb.connect()
    try:
        connection.execute("SET threads = 4")
        connection.execute("SET preserve_insertion_order = false")
        columns = _input_columns(connection, input_path)
        availability = _create_views(connection, input_path, columns)
        row_count = int(connection.execute("SELECT COUNT(*) FROM addresses").fetchone()[0])
        identity = _identity_and_provenance(connection, availability)
        if identity["mixed_release"] and not allow_mixed_release:
            releases = ", ".join(item["release"] for item in identity["release_values"])
            raise ValueError(
                "input contains multiple Overture releases "
                f"({releases}); pass allow_mixed_release=True only for an explicit comparison"
            )
        report = {
            "report_version": REPORT_VERSION,
            "input": {
                "path": str(input_path),
                "size_bytes": input_path.stat().st_size,
                "row_count": row_count,
                "columns": sorted(columns.values()),
            },
            "identity_and_provenance": identity,
            "field_coverage": _field_coverage(connection, row_count, availability),
            "locality_and_postal_city": _locality_alias_evidence(connection),
            "unit_and_base_key_density": _unit_and_base_density(connection),
            "normalization_collisions": _normalization_collisions(connection),
            "exact_key_coordinate_variation": _coordinate_variation(connection),
            "postcode_shard_distribution": _postcode_distribution(connection),
            "address_derived_street_proxy": _street_proxy(connection),
            "historical_byte_extrapolations": historical_byte_extrapolations(
                historical_metrics, row_count
            ),
            "limitations": [
                PROXY_WARNING,
                "Exact-key spread uses latitude range plus the minimum circular longitude arc, not all pairwise distances.",
                "A collision reports key collapse, not proof that the source strings identify different addresses.",
                "Postal city is a query alias and is measured separately from geographic locality.",
                "Outputs are bounded, but DuckDB aggregate and sort memory is input-dependent and may spill to disk.",
                "No address interpolation, transportation topology, or query-quality benchmark is performed.",
            ],
        }
        return _jsonable(report)
    finally:
        connection.close()


def _format_pct(value: Any) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.1f}%"


def _format_bytes(value: int | float) -> str:
    size = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or suffix == "TiB":
            return f"{size:.1f} {suffix}"
        size /= 1024
    return f"{size:.1f} TiB"


def render_markdown(report: dict[str, Any]) -> str:
    source = report["input"]
    identity = report["identity_and_provenance"]
    locality = report["locality_and_postal_city"]
    unit = report["unit_and_base_key_density"]
    collisions = report["normalization_collisions"]
    variation = report["exact_key_coordinate_variation"]
    postcode = report["postcode_shard_distribution"]
    street = report["address_derived_street_proxy"]
    lines = [
        "# Address and street experiment report",
        "",
        f"Input: `{source['path']}` — {source['row_count']:,} rows, {_format_bytes(source['size_bytes'])}.",
        "",
        "## Field coverage",
        "",
        "| Field | Present | Populated | Coverage |",
        "|---|---:|---:|---:|",
    ]
    for item in report["field_coverage"]:
        lines.append(
            f"| `{item['field']}` | {'yes' if item['available_in_input'] else 'no'} | "
            f"{item['populated_rows']:,} | {_format_pct(item['coverage'])} |"
        )
    lines += [
        "",
        "## Identity and provenance",
        "",
        f"- Distinct embedded releases: {identity['release_distinct_count']:,}",
        f"- Feature-version rows: {identity['feature_version']['populated_rows']:,}",
        f"- Nested source status: {identity['source_status']}",
    ]
    root = identity["root_cardinality"]
    if root["zero_root_rows"] is not None:
        lines += [
            f"- Rows with zero root sources: {root['zero_root_rows']:,}",
            f"- Rows with one root source: {root['one_root_rows']:,}",
            f"- Rows with multiple root sources: {root['multiple_root_rows']:,}",
        ]
    if identity["mixed_release_warning"]:
        lines += ["", f"> {identity['mixed_release_warning']}"]
    if identity.get("root_source_field_coverage"):
        coverage = identity["root_source_field_coverage"]
        lines += [
            f"- Root source records: {coverage['root_source_records']:,}",
            f"- Root records with license: {coverage['license_populated_records']:,}",
            f"- Root records with update time: {coverage['update_time_populated_records']:,}",
            f"- Root records with source confidence: {coverage['confidence_populated_records']:,}",
        ]
    if identity["root_datasets"]:
        collision_by_dataset = {
            item["dataset"]: item
            for item in identity["source_stratified_normalization"]
        }
        variation_by_dataset = {
            item["dataset"]: item
            for item in identity["source_stratified_coordinate_variation"]
        }
        lines += [
            "",
            "| Root dataset | Root records | Lossy collision keys | Coordinate-varying keys | >10 m keys |",
            "|---|---:|---:|---:|---:|",
        ]
        for dataset in identity["root_datasets"]:
            name = dataset["dataset"] or "(missing)"
            collisions_for_source = collision_by_dataset.get(dataset["dataset"], {})
            variation_for_source = variation_by_dataset.get(dataset["dataset"], {})
            lines.append(
                f"| `{name}` | {dataset['root_source_records']:,} | "
                f"{collisions_for_source.get('lossy_collision_keys', 0):,} | "
                f"{variation_for_source.get('coordinate_variant_keys', 0):,} | "
                f"{variation_for_source.get('materially_separated_keys_over_10m', 0):,} |"
            )
    lines += [
        "",
        "## Locality and postal-city aliases",
        "",
        f"- Rows with geographic locality: {locality['rows_with_locality']:,}",
        f"- Rows with postal city: {locality['rows_with_postal_city']:,}",
        f"- Rows where both differ: {locality['rows_with_distinct_postal_city']:,}",
        f"- Rows with only postal city: {locality['postal_city_only_rows']:,}",
        "",
        "## Structured address density",
        "",
        f"- Keyable number+street rows: {unit['keyable_rows']:,}",
        f"- Rows with units: {unit['rows_with_unit']:,}",
        f"- Distinct base keys (unit excluded): {unit['distinct_base_keys']:,}",
        f"- Base keys with multiple units: {unit['base_keys_with_multiple_units']:,}",
        f"- Maximum units on one base: {unit['max_units_on_one_base']:,}",
        "",
        "## Normalization collisions",
        "",
        f"Recommended: {collisions['recommended_normalization']}",
        "",
        f"Lossy audit only: {collisions['lossy_normalization']}",
        "",
        f"- Lossy keys with collisions: {collisions['lossy_collision_keys']:,}",
        f"- Conservative keys collapsed: {collisions['conservative_keys_collapsed']:,}",
        f"- Rows on those keys: {collisions['rows_on_lossy_collision_keys']:,}",
        "",
        "## Exact-key coordinate variation",
        "",
        f"- Exact keys with coordinates: {variation['exact_keys_with_coordinates']:,}",
        f"- Duplicate exact keys: {variation['duplicate_exact_keys']:,}",
        f"- Keys with more than one exact coordinate: {variation['coordinate_variant_keys']:,}",
        f"- Keys with envelope spread over 10 m: {variation['materially_separated_keys_over_10m']:,}",
        "",
        "| Envelope distance | Exact keys | Rows |",
        "|---|---:|---:|",
    ]
    for item in variation["distance_buckets"]:
        lines.append(f"| {item['distance_bucket']} | {item['exact_keys']:,} | {item['rows']:,} |")
    lines += [
        "",
        "## Postcode shard proxy",
        "",
        f"- Postcodes: {postcode['postcode_count']:,}",
        f"- Median rows/postcode: {postcode['median_rows']:,.1f}",
        f"- P90 rows/postcode: {postcode['p90_rows']:,.1f}",
        f"- Maximum rows/postcode: {postcode['max_rows']:,}",
        "",
        "## Address-derived street-name proxy",
        "",
        f"> {street['warning']}",
        "",
        f"- Distinct address street names: {street['distinct_address_street_names']:,}",
        f"- Names spanning multiple localities: {street['names_spanning_multiple_localities']:,}",
        f"- Names spanning multiple postcodes: {street['names_spanning_multiple_postcodes']:,}",
        f"- P90 locality fanout: {street['p90_locality_fanout']:,.1f}",
        f"- P90 postcode fanout: {street['p90_postcode_fanout']:,.1f}",
        "",
        "## Historical byte extrapolations",
        "",
        f"> {report['historical_byte_extrapolations']['warning']}",
        "",
    ]
    estimates = report["historical_byte_extrapolations"]["estimates"]
    if estimates:
        lines += [
            "| Historical artifact | Historical bytes/row | Linear extrapolation |",
            "|---|---:|---:|",
        ]
        for item in estimates:
            lines.append(
                f"| `{item['artifact']}` | {item['historical_bytes_per_row']:.1f} | "
                f"{_format_bytes(item['linear_extrapolation_bytes'])} |"
            )
    else:
        lines.append("No historical metrics file was supplied.")
    lines += ["", "## Limitations", ""]
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def _write(path: str | None, text: str) -> None:
    if path is None:
        return
    if path == "-":
        print(text, end="")
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Flattened address Parquet")
    parser.add_argument(
        "--historical-metrics", type=Path,
        help="Optional historical experiment metrics JSON for labeled linear extrapolations",
    )
    parser.add_argument("--json-out", help="JSON output path, or - for stdout")
    parser.add_argument("--markdown-out", help="Markdown output path, or - for stdout")
    parser.add_argument(
        "--allow-mixed-release", action="store_true",
        help="Allow an explicitly mixed-release input and emit a warning",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    report = benchmark(
        args.input,
        args.historical_metrics,
        allow_mixed_release=args.allow_mixed_release,
    )
    json_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    markdown_text = render_markdown(report)
    if args.json_out is None and args.markdown_out is None:
        print(json_text, end="")
    else:
        _write(args.json_out, json_text)
        _write(args.markdown_out, markdown_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
