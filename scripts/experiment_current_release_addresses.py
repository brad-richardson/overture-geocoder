#!/usr/bin/env python3
"""Run a bounded, reproducible current-release Overture address experiment.

The experiment deliberately samples small, geographically stratified boxes.
It is not a global-random or population-weighted sample. Sample Parquet is
temporary; only aggregate JSON/Markdown reports are suitable for committing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb

import benchmark_address_street


RELEASE = "2026-06-17.0"
SCHEMA_VERSION = "v1.17.0"
SOURCE_URI = (
    "s3://overturemaps-us-west-2/release/2026-06-17.0/theme=addresses/type=address/*"
)
DEFAULT_ROW_CAP = 2_000
MAX_ROW_CAP = 5_000
DEFAULT_PER_BOX_BYTE_CAP = 8 * 1024 * 1024
DEFAULT_OVERALL_BYTE_CAP = 96 * 1024 * 1024
DEFAULT_CANDIDATE_COUNT_CAP = 250_000
DEFAULT_DUCKDB_TEMP_CAP = 512 * 1024 * 1024
DEFAULT_REMOTE_TIME_CAP_SECONDS = 600
REPORT_VERSION = 3
DATA_MATURITY = "Alpha"
CANDIDATE_CAP_SCOPE = (
    "Post-query artifact acceptance only: the cap removes a completed sample whose "
    "observed box population is too large; it does not bound the remote scan, count, "
    "window, or deterministic sort. The wall-clock and DuckDB workspace guards bound "
    "that work."
)

REQUIRED_SAMPLE_COLUMNS = {
    "overture_id",
    "overture_release",
    "version",
    "lon",
    "lat",
    "bbox_xmin",
    "bbox_ymin",
    "bbox_xmax",
    "bbox_ymax",
    "bbox_missing",
    "bbox_geometry_discordant",
    "country",
    "number",
    "street",
    "unit",
    "postcode",
    "postal_city",
    "address_levels",
    "state",
    "locality",
    "sources",
    "root_sources",
    "root_source_count",
    "sample_box",
    "sample_stratum",
    "expected_country",
    "bbox_population",
    "deterministic_sample_rank",
    "sample_selection_contract",
}

PROXY_CONTEXT_KEY_DEFINITION = (
    "NFC/lowercase/whitespace-normalized country, first address_levels value "
    "(most-general proxy), last address_levels value (most-specific proxy), "
    "postcode, number, street, and unit. Address-level meanings remain "
    "country-dependent, so this is not a globally typed structured address key."
)

_REDACT_KEYS = {
    "examples",
    "highest_fanout",
    "largest",
    "overture_id",
    "feature_id",
    "gers_id",
    "record_id",
    "conservative_example",
    "conservative_examples",
}
_BENCHMARK_WHITELIST = {
    "report_version",
    "input",
    "identity_and_provenance",
    "field_coverage",
    "locality_and_postal_city",
    "unit_and_base_key_density",
    "normalization_collisions",
    "exact_key_coordinate_variation",
    "postcode_shard_distribution",
    "address_derived_street_proxy",
    "historical_byte_extrapolations",
    "limitations",
}


@dataclass(frozen=True)
class SampleBox:
    name: str
    stratum: str
    expected_country: str
    xmin: float
    ymin: float
    xmax: float
    ymax: float
    rationale: str


# Purposive geographic/source-diversity coverage, not statistical strata.
SAMPLE_BOXES = (
    SampleBox(
        "manhattan",
        "high-rise",
        "US",
        -74.010,
        40.700,
        -73.990,
        40.720,
        "US high-rise core; municipal/open-address source mix",
    ),
    SampleBox(
        "singapore-cbd",
        "high-rise",
        "SG",
        103.845,
        1.275,
        103.865,
        1.295,
        "Asian high-rise core and non-US address conventions",
    ),
    SampleBox(
        "melbourne-cbd",
        "high-rise",
        "AU",
        144.950,
        -37.825,
        144.980,
        -37.805,
        "G-NAF-backed high-rise core with units",
    ),
    SampleBox(
        "paris",
        "dense",
        "FR",
        2.335,
        48.850,
        2.355,
        48.870,
        "Dense European addressing and diacritics",
    ),
    SampleBox(
        "mexico-city",
        "dense",
        "MX",
        -99.145,
        19.420,
        -99.125,
        19.440,
        "Dense Latin American addressing",
    ),
    SampleBox(
        "sao-paulo",
        "dense",
        "BR",
        -46.655,
        -23.560,
        -46.635,
        -23.540,
        "AddressForAll-relevant dense South American coverage",
    ),
    SampleBox(
        "cambridge-ma",
        "suburban",
        "US",
        -71.160,
        42.360,
        -71.130,
        42.390,
        "US mixed residential/institutional fabric",
    ),
    SampleBox(
        "parramatta",
        "suburban",
        "AU",
        150.990,
        -33.830,
        151.020,
        -33.800,
        "Australian suburban G-NAF coverage",
    ),
    SampleBox(
        "auckland",
        "suburban",
        "NZ",
        174.730,
        -36.900,
        174.770,
        -36.860,
        "New Zealand suburban source and addressing conventions",
    ),
    SampleBox(
        "rural-kansas",
        "rural",
        "US",
        -97.050,
        38.980,
        -96.950,
        39.080,
        "Sparse US rural coverage",
    ),
    SampleBox(
        "rural-france",
        "rural",
        "FR",
        1.800,
        46.750,
        1.900,
        46.850,
        "Sparse European rural coverage",
    ),
    SampleBox(
        "rural-kwazulu-natal",
        "rural",
        "ZA",
        30.150,
        -29.850,
        30.250,
        -29.750,
        "Sparse African coverage and possible sample-coverage gap",
    ),
)


def _sql_string(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _geometry_predicate(box: SampleBox) -> str:
    return f"""geometry IS NOT NULL
              AND ST_GeometryType(geometry) = 'POINT'
              AND ST_X(geometry) >= {box.xmin}
              AND ST_X(geometry) <= {box.xmax}
              AND ST_Y(geometry) >= {box.ymin}
              AND ST_Y(geometry) <= {box.ymax}"""


def _bbox_prefilter_predicate(box: SampleBox) -> str:
    """Cheap superset prefilter; exact Point coordinates still select rows."""
    return f"""bbox IS NOT NULL
              AND bbox.xmax >= {box.xmin}
              AND bbox.xmin <= {box.xmax}
              AND bbox.ymax >= {box.ymin}
              AND bbox.ymin <= {box.ymax}"""


def multi_box_sample_query(
    boxes: tuple[SampleBox, ...], row_cap: int, candidate_count_cap: int
) -> str:
    """Scan once, bbox-prune, then assign rows by exact Point coordinates.

    The bbox predicate is deliberately a remote-I/O optimization, not the final
    membership test. Consequently rows with missing or spatially discordant bbox
    metadata are unobservable in this bounded run; the report says so directly.
    A wall-clock interrupter in ``run`` is the remote-work backstop.
    """
    if not boxes:
        raise ValueError("at least one sample box is required")
    if not 1 <= row_cap <= MAX_ROW_CAP:
        raise ValueError(f"row_cap must be between 1 and {MAX_ROW_CAP}")
    if candidate_count_cap < row_cap:
        raise ValueError("candidate_count_cap must be at least row_cap")
    values = ",\n                ".join(
        "("
        + ", ".join(
            (
                _sql_string(box.name),
                _sql_string(box.stratum),
                _sql_string(box.expected_country),
                str(box.xmin),
                str(box.ymin),
                str(box.xmax),
                str(box.ymax),
            )
        )
        + ")"
        for box in boxes
    )
    bbox_prefilter = "\n                OR ".join(
        f"({_bbox_prefilter_predicate(box)})" for box in boxes
    )
    return f"""
        WITH configured_boxes(
            sample_box, sample_stratum, expected_country,
            xmin, ymin, xmax, ymax
        ) AS (VALUES
                {values}
        ), bbox_pruned AS MATERIALIZED (
            SELECT *
            FROM read_parquet('{SOURCE_URI}', hive_partitioning = true)
            WHERE ({bbox_prefilter})
        ), matched AS (
            SELECT address.*, box.*
            FROM bbox_pruned address
            JOIN configured_boxes box ON
                address.geometry IS NOT NULL
                AND ST_GeometryType(address.geometry) = 'POINT'
                AND ST_X(address.geometry) >= box.xmin
                AND ST_X(address.geometry) <= box.xmax
                AND ST_Y(address.geometry) >= box.ymin
                AND ST_Y(address.geometry) <= box.ymax
        ), ranked AS (
            SELECT
                id AS overture_id,
                '{RELEASE}' AS overture_release,
                '{SCHEMA_VERSION}' AS overture_schema_version,
                version,
                ST_X(geometry) AS lon,
                ST_Y(geometry) AS lat,
                bbox.xmin AS bbox_xmin,
                bbox.ymin AS bbox_ymin,
                bbox.xmax AS bbox_xmax,
                bbox.ymax AS bbox_ymax,
                FALSE AS bbox_missing,
                NOT (
                    ST_X(geometry) >= bbox.xmin AND ST_X(geometry) <= bbox.xmax
                    AND ST_Y(geometry) >= bbox.ymin AND ST_Y(geometry) <= bbox.ymax
                ) AS bbox_geometry_discordant,
                country, number, street, unit, postcode, postal_city,
                address_levels,
                list_extract(address_levels, 1).value AS state,
                list_extract(address_levels, -1).value AS locality,
                sources,
                list_filter(sources, lambda source: source.property = '') AS root_sources,
                list_count(list_filter(sources, lambda source: source.property = ''))
                    AS root_source_count,
                sample_box, sample_stratum, expected_country,
                'bbox-prefiltered-geometry-verified' AS sample_selection_contract,
                COUNT(*) OVER (PARTITION BY sample_box)::BIGINT AS bbox_population,
                ROW_NUMBER() OVER (
                    PARTITION BY sample_box ORDER BY md5(id)
                ) AS deterministic_sample_rank
            FROM matched
        )
        SELECT * FROM ranked
        WHERE deterministic_sample_rank <= {row_cap}
        ORDER BY sample_box, deterministic_sample_rank
    """.strip()


def over_cap_boxes(
    box_metrics: list[dict[str, Any]], candidate_count_cap: int
) -> list[tuple[str, int]]:
    """Return explicitly rejected boxes; never conflate rejection with absence."""
    return sorted(
        (str(item["box"]), int(item["bbox_population"]))
        for item in box_metrics
        if int(item.get("bbox_population") or 0) > candidate_count_cap
    )


def candidate_count_query(box: SampleBox) -> str:
    """Count required Point geometries before any deterministic sample sort."""
    return f"""SELECT COUNT(*)
        FROM read_parquet('{SOURCE_URI}', hive_partitioning = true)
        WHERE {_geometry_predicate(box)}"""


def sample_query(box: SampleBox, row_cap: int, candidate_count: int) -> str:
    """Return the output-bounded extraction query for one box.

    Geometry is authoritative. bbox is retained only for diagnostics; it is not
    a correctness filter because bbox is optional and can be discordant.
    """
    if not 1 <= row_cap <= MAX_ROW_CAP:
        raise ValueError(f"row_cap must be between 1 and {MAX_ROW_CAP}")
    if candidate_count < 0:
        raise ValueError("candidate_count must be non-negative")
    return f"""
        WITH geometry_rows AS (
            SELECT
                id AS overture_id,
                '{RELEASE}' AS overture_release,
                '{SCHEMA_VERSION}' AS overture_schema_version,
                version,
                ST_X(geometry) AS lon,
                ST_Y(geometry) AS lat,
                bbox.xmin AS bbox_xmin,
                bbox.ymin AS bbox_ymin,
                bbox.xmax AS bbox_xmax,
                bbox.ymax AS bbox_ymax,
                bbox IS NULL AS bbox_missing,
                CASE WHEN bbox IS NULL THEN NULL ELSE NOT (
                    ST_X(geometry) >= bbox.xmin AND ST_X(geometry) <= bbox.xmax
                    AND ST_Y(geometry) >= bbox.ymin AND ST_Y(geometry) <= bbox.ymax
                ) END AS bbox_geometry_discordant,
                country,
                number,
                street,
                unit,
                postcode,
                postal_city,
                address_levels,
                list_extract(address_levels, 1).value AS state,
                list_extract(address_levels, -1).value AS locality,
                sources,
                list_filter(sources, lambda source: source.property = '') AS root_sources,
                list_count(list_filter(sources, lambda source: source.property = ''))
                    AS root_source_count,
                '{box.name}' AS sample_box,
                '{box.stratum}' AS sample_stratum,
                '{box.expected_country}' AS expected_country,
                'geometry-authoritative' AS sample_selection_contract,
                {candidate_count}::BIGINT AS bbox_population,
                ROW_NUMBER() OVER (ORDER BY md5(id)) AS deterministic_sample_rank
            FROM read_parquet('{SOURCE_URI}', hive_partitioning = true)
            WHERE {_geometry_predicate(box)}
        )
        SELECT * FROM geometry_rows
        WHERE deterministic_sample_rank <= {row_cap}
        ORDER BY deterministic_sample_rank
    """.strip()


def _fetch_dicts(
    connection: duckdb.DuckDBPyConnection, query: str
) -> list[dict[str, Any]]:
    cursor = connection.execute(query)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sanitize_recursive(value: Any) -> Any:
    """Recursively remove row-level/example fields from a report subtree."""
    if isinstance(value, dict):
        return {
            key: _sanitize_recursive(item)
            for key, item in value.items()
            if key not in _REDACT_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_recursive(item) for item in value]
    return value


def _rename_proxy_metrics(value: Any) -> Any:
    """Publish benchmark identity metrics under their actual proxy semantics."""
    key_names = {
        "exact_key_coordinate_variation": "proxy_context_key_coordinate_variation",
        "exact_keys_with_coordinates": "proxy_context_keys_with_coordinates",
        "duplicate_exact_keys": "duplicate_proxy_context_keys",
        "coordinate_variant_keys": "proxy_context_coordinate_variant_keys",
        "materially_separated_keys_over_10m": "proxy_context_keys_over_10m_envelope_spread",
        "exact_keys": "proxy_context_keys",
        "rows_on_coordinate_variant_keys": "rows_on_proxy_context_coordinate_variant_keys",
        "distance_buckets": "envelope_spread_buckets",
        "max_spread_m": "max_envelope_spread_m",
    }
    if isinstance(value, dict):
        return {
            key_names.get(key, key): _rename_proxy_metrics(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rename_proxy_metrics(item) for item in value]
    if isinstance(value, str):
        return (
            value.replace("Exact-key", "Proxy-context-key")
            .replace("exact-key", "proxy-context-key")
            .replace("Exact keys", "Proxy-context keys")
            .replace("exact keys", "proxy-context keys")
        )
    return value


def sanitize_benchmark_report(base: dict[str, Any]) -> dict[str, Any]:
    """Whitelist aggregate benchmark sections, then recursively redact rows."""
    whitelisted = {
        key: value for key, value in base.items() if key in _BENCHMARK_WHITELIST
    }
    return _rename_proxy_metrics(_sanitize_recursive(whitelisted))


def validate_sample(
    sample_path: Path,
    boxes: tuple[SampleBox, ...],
    row_cap: int,
    workspace_byte_cap: int,
) -> dict[str, Any]:
    """Fail closed on reused sample bytes, schema, release, boxes, and rows."""
    if not sample_path.is_file():
        raise ValueError(f"sample does not exist: {sample_path}")
    size = sample_path.stat().st_size
    if size > workspace_byte_cap:
        raise ValueError(
            f"sample is {size} bytes, over workspace cap {workspace_byte_cap}"
        )
    configured = {box.name: box for box in boxes}
    connection = duckdb.connect()
    try:
        described = connection.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(sample_path)]
        ).fetchall()
        columns = {str(row[0]) for row in described}
        missing = REQUIRED_SAMPLE_COLUMNS - columns
        if missing:
            raise ValueError(
                f"sample schema missing columns: {', '.join(sorted(missing))}"
            )
        releases = connection.execute(
            """
            SELECT DISTINCT overture_release, overture_schema_version
            FROM read_parquet(?)
            ORDER BY ALL
        """,
            [str(sample_path)],
        ).fetchall()
        if releases and releases != [(RELEASE, SCHEMA_VERSION)]:
            raise ValueError(f"sample release/schema mismatch: {releases!r}")
        selection_contracts = [
            row[0]
            for row in connection.execute(
                """
            SELECT DISTINCT sample_selection_contract FROM read_parquet(?) ORDER BY 1
        """,
                [str(sample_path)],
            ).fetchall()
        ]
        allowed_contracts = {
            "geometry-authoritative",
            "legacy-bbox-selected",
            "bbox-prefiltered-geometry-verified",
        }
        if not selection_contracts or not set(selection_contracts) <= allowed_contracts:
            raise ValueError(
                f"sample selection contract is missing or invalid: {selection_contracts!r}"
            )
        rows = _fetch_dicts(
            connection,
            """
            SELECT sample_box, MIN(sample_stratum) AS sample_stratum,
                   MAX(sample_stratum) AS max_stratum,
                   MIN(expected_country) AS expected_country,
                   MAX(expected_country) AS max_country,
                   COUNT(*) AS sampled_rows,
                   COUNT(DISTINCT deterministic_sample_rank) AS distinct_ranks,
                   MIN(deterministic_sample_rank) AS min_rank,
                   MAX(deterministic_sample_rank) AS max_rank,
                   MIN(bbox_population) AS min_population,
                   MAX(bbox_population) AS max_population,
                   COUNT_IF(lon IS NULL OR lat IS NULL) AS null_coordinate_rows,
                   MIN(lon) AS min_lon, MAX(lon) AS max_lon,
                   MIN(lat) AS min_lat, MAX(lat) AS max_lat
            FROM read_parquet('"""
            + str(sample_path).replace("'", "''")
            + """')
            GROUP BY sample_box
            ORDER BY sample_box
        """,
        )
        unknown = {row["sample_box"] for row in rows} - set(configured)
        if unknown:
            raise ValueError(f"sample contains unconfigured boxes: {sorted(unknown)!r}")
        for row in rows:
            box = configured[row["sample_box"]]
            if (
                row["sample_stratum"] != box.stratum
                or row["max_stratum"] != box.stratum
            ):
                raise ValueError(f"sample stratum mismatch for {box.name}")
            if (
                row["expected_country"] != box.expected_country
                or row["max_country"] != box.expected_country
            ):
                raise ValueError(f"sample expected-country mismatch for {box.name}")
            if row["sampled_rows"] > row_cap:
                raise ValueError(f"sample row cap exceeded for {box.name}")
            if row["distinct_ranks"] != row["sampled_rows"]:
                raise ValueError(f"duplicate sample ranks for {box.name}")
            if row["sampled_rows"] and (
                row["min_rank"] != 1 or row["max_rank"] != row["sampled_rows"]
            ):
                raise ValueError(f"non-contiguous sample ranks for {box.name}")
            if row["min_population"] != row["max_population"]:
                raise ValueError(f"inconsistent candidate population for {box.name}")
            if row["max_population"] < row["sampled_rows"]:
                raise ValueError(
                    f"candidate population below sample rows for {box.name}"
                )
            if row["null_coordinate_rows"]:
                raise ValueError(f"null required coordinates for {box.name}")
            if not (
                box.xmin <= row["min_lon"] <= row["max_lon"] <= box.xmax
                and box.ymin <= row["min_lat"] <= row["max_lat"] <= box.ymax
            ):
                raise ValueError(f"geometry outside configured box for {box.name}")
        row_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM read_parquet(?)", [str(sample_path)]
            ).fetchone()[0]
        )
    finally:
        connection.close()
    return {
        "sha256": _sha256(sample_path),
        "bytes": size,
        "rows": row_count,
        "mtime_utc": datetime.fromtimestamp(
            sample_path.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
        "validated_boxes_with_rows": sorted(row["sample_box"] for row in rows),
        "release_schema_values": [list(item) for item in releases],
        "selection_contracts": selection_contracts,
    }


def analyze_sample(
    sample_path: Path,
    boxes: tuple[SampleBox, ...],
    row_cap: int,
    extraction: list[dict[str, Any]],
    per_box_byte_cap: int = DEFAULT_PER_BOX_BYTE_CAP,
    overall_byte_cap: int = DEFAULT_OVERALL_BYTE_CAP,
    candidate_count_cap: int = DEFAULT_CANDIDATE_COUNT_CAP,
    duckdb_temp_cap: int = DEFAULT_DUCKDB_TEMP_CAP,
    remote_time_cap_seconds: int = DEFAULT_REMOTE_TIME_CAP_SECONDS,
) -> dict[str, Any]:
    """Combine the existing address benchmark with sample/source diagnostics."""
    validation = validate_sample(sample_path, boxes, row_cap, overall_byte_cap)
    bbox_prefiltered = bool(
        {
            "legacy-bbox-selected",
            "bbox-prefiltered-geometry-verified",
        }
        & set(validation["selection_contracts"])
    )
    base = sanitize_benchmark_report(benchmark_address_street.benchmark(sample_path))
    connection = duckdb.connect()
    try:
        connection.execute(
            "CREATE TEMP VIEW sampled AS SELECT * FROM read_parquet("
            f"{_sql_string(sample_path)})"
        )
        box_rows = _fetch_dicts(
            connection,
            """
            SELECT sample_box, sample_stratum, expected_country,
                   COUNT(*) AS sampled_rows,
                   MAX(bbox_population) AS bbox_population,
                   COUNT_IF(country != expected_country) AS unexpected_country_rows,
                   COUNT_IF(NULLIF(TRIM(number), '') IS NOT NULL) AS number_rows,
                   COUNT_IF(NULLIF(TRIM(street), '') IS NOT NULL) AS street_rows,
                   COUNT_IF(NULLIF(TRIM(unit), '') IS NOT NULL) AS unit_rows,
                   COUNT_IF(NULLIF(TRIM(postcode), '') IS NOT NULL) AS postcode_rows,
                   COUNT_IF(NULLIF(TRIM(postal_city), '') IS NOT NULL) AS postal_city_rows,
                   COUNT_IF(list_count(address_levels) > 0) AS address_level_rows,
                   COUNT_IF(COALESCE(list_count(root_sources), 0) = 0) AS zero_root_rows,
                   COUNT_IF(bbox_missing) AS missing_bbox_rows,
                   COUNT_IF(bbox_geometry_discordant) AS discordant_bbox_rows,
                   COUNT(DISTINCT root_source.dataset) AS distinct_root_datasets
            FROM sampled
            LEFT JOIN UNNEST(root_sources) AS roots(root_source) ON TRUE
            GROUP BY sample_box, sample_stratum, expected_country
            ORDER BY sample_stratum, sample_box
        """,
        )
        # The unnest above repeats feature rows for multi-root features. Recompute
        # row/field counts without an unnest and join only the dataset cardinality.
        feature_rows = _fetch_dicts(
            connection,
            """
            SELECT sample_box, sample_stratum, expected_country,
                   COUNT(*) AS sampled_rows,
                   MAX(bbox_population) AS bbox_population,
                   COUNT_IF(country != expected_country) AS unexpected_country_rows,
                   COUNT_IF(NULLIF(TRIM(number), '') IS NOT NULL) AS number_rows,
                   COUNT_IF(NULLIF(TRIM(street), '') IS NOT NULL) AS street_rows,
                   COUNT_IF(NULLIF(TRIM(unit), '') IS NOT NULL) AS unit_rows,
                   COUNT_IF(NULLIF(TRIM(postcode), '') IS NOT NULL) AS postcode_rows,
                   COUNT_IF(NULLIF(TRIM(postal_city), '') IS NOT NULL) AS postal_city_rows,
                   COUNT_IF(list_count(address_levels) > 0) AS address_level_rows,
                   COUNT_IF(COALESCE(list_count(root_sources), 0) = 0) AS zero_root_rows,
                   COUNT_IF(bbox_missing) AS missing_bbox_rows,
                   COUNT_IF(bbox_geometry_discordant) AS discordant_bbox_rows
            FROM sampled
            GROUP BY sample_box, sample_stratum, expected_country
            ORDER BY sample_stratum, sample_box
        """,
        )
        dataset_counts = {
            row["sample_box"]: row["distinct_root_datasets"] for row in box_rows
        }
        for row in feature_rows:
            row["distinct_root_datasets"] = dataset_counts.get(row["sample_box"], 0)
            population = int(row["bbox_population"] or 0)
            row["sample_fraction"] = (
                row["sampled_rows"] / population if population else None
            )
            row["cap_hit"] = population > row_cap
            if bbox_prefiltered:
                # Bbox pruning excludes null/discordant metadata before exact
                # geometry verification, so zero cannot measure that coverage.
                row["missing_bbox_rows"] = None

        source_records_cte = """
            WITH all_source_records AS (
                SELECT overture_id, sample_box, sample_stratum,
                       source_item.property AS property,
                       source_item.dataset AS dataset,
                       source_item.license AS license,
                       source_item.record_id AS record_id,
                       TRY_CAST(source_item.update_time AS TIMESTAMP) AS update_time,
                       source_item.confidence AS confidence,
                       source_item.between AS between_value
                FROM sampled,
                     UNNEST(sources) AS source_items(source_item)
            ), root_records AS (
                SELECT * FROM all_source_records WHERE property = ''
            )
        """
        source_item_coverage = _fetch_dicts(
            connection,
            source_records_cte
            + """
            SELECT scope, COUNT(*) AS source_item_records,
                   COUNT_IF(NULLIF(TRIM(property), '') IS NOT NULL) AS non_root_property_records,
                   COUNT_IF(NULLIF(TRIM(dataset), '') IS NOT NULL) AS dataset_records,
                   COUNT_IF(NULLIF(TRIM(license), '') IS NOT NULL) AS license_records,
                   COUNT_IF(NULLIF(TRIM(record_id), '') IS NOT NULL) AS record_id_records,
                   COUNT(update_time) AS update_time_records,
                   COUNT(confidence) AS confidence_records,
                   COUNT(between_value) AS between_records
            FROM (
                SELECT 'all' AS scope, * FROM all_source_records
                UNION ALL
                SELECT 'root' AS scope, * FROM root_records
            )
            GROUP BY scope ORDER BY scope
        """,
        )
        source_properties = _fetch_dicts(
            connection,
            source_records_cte
            + """
            SELECT property, COUNT(*) AS source_item_records
            FROM all_source_records
            GROUP BY property
            ORDER BY source_item_records DESC, property
            LIMIT 100
        """,
        )
        source_summary = _fetch_dicts(
            connection,
            source_records_cte
            + """
            SELECT COUNT(*) AS root_source_records,
                   COUNT(DISTINCT overture_id) AS features_with_root_source,
                   COUNT(DISTINCT dataset) AS distinct_datasets,
                   COUNT(DISTINCT license) FILTER (WHERE NULLIF(TRIM(license), '') IS NOT NULL)
                       AS distinct_populated_licenses,
                   COUNT(update_time) AS update_time_records,
                   MIN(update_time) AS oldest_update_time,
                   MAX(update_time) AS newest_update_time,
                   COUNT(confidence) AS confidence_records,
                   MIN(confidence) AS confidence_min,
                   QUANTILE_CONT(confidence, 0.5) AS confidence_median,
                   QUANTILE_CONT(confidence, 0.9) AS confidence_p90,
                   MAX(confidence) AS confidence_max
            FROM root_records
        """,
        )[0]
        source_datasets = _fetch_dicts(
            connection,
            source_records_cte
            + """
            SELECT dataset, MIN(license) AS example_license,
                   COUNT(*) AS root_source_records,
                   COUNT(DISTINCT overture_id) AS distinct_features,
                   COUNT(DISTINCT sample_box) AS boxes,
                   STRING_AGG(DISTINCT sample_stratum, ', ' ORDER BY sample_stratum)
                       AS strata,
                   COUNT(update_time) AS update_time_records,
                   MIN(update_time) AS oldest_update_time,
                   MAX(update_time) AS newest_update_time,
                   COUNT(confidence) AS confidence_records,
                   MIN(confidence) AS confidence_min,
                   AVG(confidence) AS confidence_mean,
                   MAX(confidence) AS confidence_max
            FROM root_records
            GROUP BY dataset
            ORDER BY root_source_records DESC, dataset
            LIMIT 100
        """,
        )
    finally:
        connection.close()

    base["input"]["path"] = "<validated temporary output-bounded sample; not committed>"
    script_path = Path(__file__).resolve()
    benchmark_path = Path(benchmark_address_street.__file__).resolve()
    return _jsonable(
        {
            "report_version": REPORT_VERSION,
            "release": RELEASE,
            "schema_version": SCHEMA_VERSION,
            "data_maturity": DATA_MATURITY,
            "report_generated_utc": datetime.now(timezone.utc).isoformat(),
            "source_uri": SOURCE_URI,
            "tooling": {
                "python_version": sys.version.split()[0],
                "duckdb_version": duckdb.__version__,
                "experiment_script_sha256": _sha256(script_path),
                "benchmark_script_sha256": _sha256(benchmark_path),
                "sample_input": validation,
                "sanitizer": (
                    "top-level benchmark whitelist plus recursive row/example key redaction"
                ),
            },
            "sampling": {
                "method": "purposive 12-box strata; deterministic md5(id) sample within each box",
                "statistically_representative": False,
                "count_basis": (
                    "Counts are raw, unweighted sample counts. Recorded candidate populations "
                    "come from bbox-prefiltered, exact-geometry-verified candidates and cannot "
                    "observe missing or spatially discordant bbox rows; no "
                    "global design-weighted estimate is reported."
                    if bbox_prefiltered
                    else "Counts are raw, unweighted sample counts. Candidate populations are "
                    "geometry-authoritative preflight counts inside purposively selected boxes; "
                    "no global design-weighted estimate is reported."
                ),
                "geometry_contract": (
                    "Required Point geometry and ST_X/ST_Y select rows. Optional bbox is never a "
                    "correctness filter; missing and geometry-discordant bbox values are reported."
                ),
                "sample_contract_warning": (
                    "Remote I/O was bounded with one combined bbox-prefiltered scan, followed by "
                    "exact Point-coordinate membership. Missing or spatially discordant bbox rows "
                    "are unobservable, so candidate populations are not fully geometry-authoritative."
                    if bbox_prefiltered
                    else None
                ),
                "row_cap_per_box": row_cap,
                "post_query_candidate_acceptance_cap_per_box": candidate_count_cap,
                "candidate_cap_scope_warning": CANDIDATE_CAP_SCOPE,
                "per_box_output_byte_cap": per_box_byte_cap,
                "workspace_output_byte_cap": overall_byte_cap,
                "duckdb_temp_byte_cap": duckdb_temp_cap,
                "remote_wall_clock_cap_seconds": remote_time_cap_seconds,
                "duckdb_memory_limit": "2GB",
                "peak_output_parquet_bytes": max(
                    [
                        validation["bytes"],
                        *(
                            item.get("workspace_parquet_bytes", 0)
                            for item in extraction
                        ),
                    ]
                ),
                "total_extraction_seconds": round(
                    sum(item.get("elapsed_seconds", 0) for item in extraction), 3
                ),
                "box_count": len(boxes),
                "boxes": [
                    {
                        **asdict(box),
                        **next(
                            (
                                row
                                for row in feature_rows
                                if row["sample_box"] == box.name
                            ),
                            {
                                "sampled_rows": 0,
                                "bbox_population": 0,
                                "sample_fraction": None,
                                "cap_hit": False,
                                "distinct_root_datasets": 0,
                            },
                        ),
                    }
                    for box in boxes
                ],
                "extraction": extraction,
                "current_harness_query_sql": multi_box_sample_query(
                    boxes, row_cap, candidate_count_cap
                ),
                "bounds_warning": (
                    "Row/byte/temp/memory guards bound local outputs and DuckDB workspace only. "
                    "They do not meter S3 bytes scanned or HTTP requests. The single remote "
                    "query is interrupted at the reported wall-clock cap."
                ),
            },
            "source_evidence": {
                "summary": source_summary,
                "datasets": source_datasets,
                "source_item_coverage": source_item_coverage,
                "source_properties": source_properties,
                "confidence_warning": (
                    "Source confidence is source-supplied and is not calibrated across datasets; "
                    "missing confidence is not zero confidence."
                ),
                "freshness_warning": (
                    "update_time describes a source record when populated; it is neither Overture "
                    "release time nor a guaranteed observation time."
                ),
            },
            "proxy_context_key": {
                "definition": PROXY_CONTEXT_KEY_DEFINITION,
                "coordinate_spread_warning": (
                    "Coordinate spread is a latitude-range/minimum-circular-longitude-envelope "
                    "proxy, not maximum pairwise or road-network distance."
                ),
            },
            "address_benchmark": base,
            "gold_queries": {
                "created": 0,
                "reason": (
                    "Sampled source records are not independently verified relevance labels. "
                    "Promoting them to gold queries would manufacture circular ground truth."
                ),
            },
            "limitations": [
                "The Overture Addresses theme is Alpha in the cited release.",
                "The boxes are purposive and tiny; estimates must not be extrapolated globally.",
                "All counts are raw unweighted sample counts unless explicitly labeled as preflight box populations.",
                "Rows are deterministic pseudo-random samples only when a box exceeds its cap.",
                "A zero-row box is evidence of no records in that exact box, not country-wide absence.",
                "address_levels are preserved in full. The first/last values are only general/particular routing proxies; their semantic meaning is country-dependent.",
                "Source-stratified records are multi-membership: one feature may count in multiple root datasets.",
                "Coordinate variation uses an envelope-spread proxy, not pairwise distance.",
                "Local output/workspace guards do not meter S3 scan bytes or HTTP requests; the remote query has an explicit wall-clock interrupt.",
                "No production shard, catalog, Worker, R2 object, deployment, or interpolation is changed.",
            ],
        }
    )


def render_markdown(report: dict[str, Any]) -> str:
    sampling = report["sampling"]
    source = report["source_evidence"]["summary"]
    benchmark = report["address_benchmark"]
    identity = benchmark["identity_and_provenance"]
    variation = benchmark["proxy_context_key_coordinate_variation"]
    collisions = benchmark["normalization_collisions"]
    unit = benchmark["unit_and_base_key_density"]
    elapsed = float(sampling.get("total_extraction_seconds", 0))
    sample_bytes = int(
        report.get("tooling", {}).get("sample_input", {}).get("bytes", 0)
    )
    remote_cap = int(sampling.get("remote_wall_clock_cap_seconds", 0))
    lines = [
        "# Current-release bounded address/source experiment",
        "",
        f"Release: `{report['release']}` / schema `{report['schema_version']}` / "
        f"Addresses maturity: **{report['data_maturity']}**.",
        "",
        "> This is a purposive, bounded 12-box experiment, not a statistically representative sample.",
        "> Counts are raw unweighted sample counts; no global design-weighted estimate is reported.",
        f"> {sampling['bounds_warning']}",
        *(
            [f"> {sampling['sample_contract_warning']}"]
            if sampling.get("sample_contract_warning")
            else []
        ),
        *(
            [f"> {sampling['candidate_cap_scope_warning']}"]
            if sampling.get("candidate_cap_scope_warning")
            else []
        ),
        "",
        "Official references: [address schema](https://docs.overturemaps.org/schema/reference/addresses/address/), "
        "[source item schema](https://docs.overturemaps.org/schema/reference/core/source_item/), and "
        "[address data guide](https://docs.overturemaps.org/guides/addresses/).",
        "",
        "## Operational cost",
        "",
        f"- Single combined remote pass: **{elapsed:.2f} seconds**",
        f"- Combined bounded sample: **{sample_bytes:,} bytes**",
        f"- Wall-clock interrupt: **{remote_cap} seconds**",
        "- S3 bytes and request count were not metered; bbox metadata pruned input before exact Point verification.",
        "",
        "## Sample coverage",
        "",
        "| Box | Stratum | Country | Sample / recorded candidate count | Fraction | Root datasets |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for box in sampling["boxes"]:
        fraction = box.get("sample_fraction")
        lines.append(
            f"| `{box['name']}` | {box['stratum']} | {box['expected_country']} | "
            f"{box.get('sampled_rows', 0):,} / {box.get('bbox_population', 0):,} | "
            f"{'n/a' if fraction is None else f'{fraction:.1%}'} | "
            f"{box.get('distinct_root_datasets', 0):,} |"
        )
    lines += [
        "",
        "## Source evidence",
        "",
        f"- Root source records: {source['root_source_records']:,}",
        f"- Features with a root source: {source['features_with_root_source']:,}",
        f"- Distinct root datasets: {source['distinct_datasets']:,}",
        f"- Distinct populated licenses: {source['distinct_populated_licenses']:,}",
        f"- Root records with update time: {source['update_time_records']:,}",
        f"- Root records with confidence: {source['confidence_records']:,}",
        f"- Property-specific source records: {identity.get('property_specific_source_records', 0):,}",
        "",
        f"> {report['source_evidence']['confidence_warning']}",
        "",
        f"> {report['source_evidence']['freshness_warning']}",
        "",
        "| Root dataset | Records | Features | Boxes | License example | Updated range | Confidence coverage |",
        "|---|---:|---:|---:|---|---|---:|",
    ]
    for item in report["source_evidence"]["datasets"]:
        updated = "n/a"
        if item["oldest_update_time"]:
            updated = f"{item['oldest_update_time']} — {item['newest_update_time']}"
        lines.append(
            f"| `{item['dataset'] or '(missing)'}` | {item['root_source_records']:,} | "
            f"{item['distinct_features']:,} | {item['boxes']:,} | "
            f"{item['example_license'] or 'n/a'} | {updated} | "
            f"{item['confidence_records']:,}/{item['root_source_records']:,} |"
        )
    lines += [
        "",
        "### SourceItem field coverage",
        "",
        "| Scope | Records | Dataset | License | Record ID | Update time | Confidence | Between |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report["source_evidence"]["source_item_coverage"]:
        lines.append(
            f"| {item['scope']} | {item['source_item_records']:,} | "
            f"{item['dataset_records']:,} | {item['license_records']:,} | "
            f"{item['record_id_records']:,} | {item['update_time_records']:,} | "
            f"{item['confidence_records']:,} | {item['between_records']:,} |"
        )
    lines += [
        "",
        "## Per-box field shape",
        "",
        "| Box | Number | Street | Unit | Postcode | Postal city | Address levels | Missing bbox | Discordant bbox |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for box in sampling["boxes"]:
        missing_bbox = box.get("missing_bbox_rows")
        lines.append(
            f"| `{box['name']}` | {box.get('number_rows') or 0:,} | "
            f"{box.get('street_rows') or 0:,} | {box.get('unit_rows') or 0:,} | "
            f"{box.get('postcode_rows') or 0:,} | {box.get('postal_city_rows') or 0:,} | "
            f"{box.get('address_level_rows') or 0:,} | "
            f"{'unobservable' if missing_bbox is None else f'{missing_bbox:,}'} | "
            f"{box.get('discordant_bbox_rows') or 0:,} |"
        )
    lines += [
        "",
        "## Address identity evidence",
        "",
        f"- Sample rows: {benchmark['input']['row_count']:,}",
        f"- Rows with multiple root sources: {identity['root_cardinality']['multiple_root_rows']:,}",
        f"- Keyable number+street rows: {unit['keyable_rows']:,}",
        f"- Rows with unit: {unit['rows_with_unit']:,}",
        f"- Lossy normalization collision keys: {collisions['lossy_collision_keys']:,}",
        f"- Proxy-context keys with multiple coordinates: {variation['proxy_context_coordinate_variant_keys']:,}",
        f"- Proxy-context keys over 10 m by envelope-spread proxy: {variation['proxy_context_keys_over_10m_envelope_spread']:,}",
        "",
        f"Proxy-context key definition: {report['proxy_context_key']['definition']}",
        "",
        f"> {report['proxy_context_key']['coordinate_spread_warning']}",
        "",
        "Per-source normalization and coordinate-variation tables are retained in the JSON report.",
        "The current bbox-prefiltered, exact-Point-verification SQL is retained in the JSON report.",
        "",
        "## Gold-query status",
        "",
        f"No gold queries were created. {report['gold_queries']['reason']}",
        "",
        "## Architecture implications",
        "",
        "- Preserve the full source array and multi-root membership; do not flatten to a single provider.",
        "- Preserve full `address_levels`; use positional ends only as routing proxies, not globally typed locality/region fields.",
        "- Treat source confidence as within-source metadata until calibration evidence exists.",
        "  In this sample it was absent for every root record, so it cannot weight candidates.",
        "- Dictionary-code source dataset identity and omit all-null optional provenance columns from hot lookup records; keep complete provenance in a colder detail record.",
        "- Retain candidate lists for coordinate-varying proxy-context keys and keep unit in identity.",
        "- Measure current-release source and spatial skew before choosing regional/postcode shard boundaries.",
        "",
        "## Limitations",
        "",
    ]
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def run(
    output_dir: Path,
    row_cap: int = DEFAULT_ROW_CAP,
    per_box_byte_cap: int = DEFAULT_PER_BOX_BYTE_CAP,
    overall_byte_cap: int = DEFAULT_OVERALL_BYTE_CAP,
    candidate_count_cap: int = DEFAULT_CANDIDATE_COUNT_CAP,
    duckdb_temp_cap: int = DEFAULT_DUCKDB_TEMP_CAP,
    remote_time_cap_seconds: int = DEFAULT_REMOTE_TIME_CAP_SECONDS,
    selected_boxes: set[str] | None = None,
) -> tuple[dict[str, Any], Path]:
    if not 1 <= row_cap <= MAX_ROW_CAP:
        raise ValueError(f"row_cap must be between 1 and {MAX_ROW_CAP}")
    boxes = tuple(
        box for box in SAMPLE_BOXES if not selected_boxes or box.name in selected_boxes
    )
    unknown = (selected_boxes or set()) - {box.name for box in SAMPLE_BOXES}
    if unknown:
        raise ValueError(f"unknown boxes: {', '.join(sorted(unknown))}")
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = output_dir / "duckdb-temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    if remote_time_cap_seconds <= 0:
        raise ValueError("remote_time_cap_seconds must be positive")
    extraction: list[dict[str, Any]] = []
    try:
        connection.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial")
        connection.execute("SET s3_region = 'us-west-2'")
        connection.execute("SET threads = 4")
        connection.execute("SET memory_limit = '2GB'")
        connection.execute(f"SET temp_directory = {_sql_string(temp_dir)}")
        connection.execute(f"SET max_temp_directory_size = '{duckdb_temp_cap}B'")
        combined = output_dir / "current-release-address-sample.parquet"
        combined.unlink(missing_ok=True)
        query = multi_box_sample_query(boxes, row_cap, candidate_count_cap)
        started_utc = datetime.now(timezone.utc)
        started = time.monotonic()
        interrupted = threading.Event()

        def interrupt_remote_query() -> None:
            interrupted.set()
            connection.interrupt()

        timer = threading.Timer(remote_time_cap_seconds, interrupt_remote_query)
        timer.daemon = True
        timer.start()
        try:
            connection.execute(
                f"COPY ({query}) TO {_sql_string(combined)} "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        except duckdb.Error as exc:
            combined.unlink(missing_ok=True)
            if interrupted.is_set():
                raise RuntimeError(
                    f"remote address query exceeded {remote_time_cap_seconds}s wall-clock cap"
                ) from exc
            raise
        finally:
            timer.cancel()
        elapsed = time.monotonic() - started
        combined_size = combined.stat().st_size
        if combined_size > overall_byte_cap:
            combined.unlink(missing_ok=True)
            raise RuntimeError(
                f"sample reached {combined_size} bytes, over workspace cap {overall_byte_cap}"
            )
        cursor = connection.execute(
            """
            SELECT sample_box AS box, COUNT(*) AS rows,
                   MAX(bbox_population) AS bbox_population
            FROM read_parquet(?) GROUP BY sample_box ORDER BY sample_box
        """,
            [str(combined)],
        )
        columns = [item[0] for item in cursor.description]
        box_metrics = [dict(zip(columns, row)) for row in cursor.fetchall()]
        rejected = over_cap_boxes(box_metrics, candidate_count_cap)
        if rejected:
            combined.unlink(missing_ok=True)
            detail = ", ".join(f"{name}={count:,}" for name, count in rejected)
            raise RuntimeError(
                f"candidate count cap {candidate_count_cap:,} exceeded: {detail}; "
                "bounded sample removed"
            )
        per_box_bytes: dict[str, int] = {}
        for box in boxes:
            part = output_dir / f"byte-guard-{box.name}.parquet"
            part.unlink(missing_ok=True)
            connection.execute(f"""
                COPY (SELECT * FROM read_parquet({_sql_string(combined)})
                      WHERE sample_box = {_sql_string(box.name)})
                TO {_sql_string(part)} (FORMAT PARQUET, COMPRESSION ZSTD)
            """)
            size = part.stat().st_size
            part.unlink()
            if size > per_box_byte_cap:
                combined.unlink(missing_ok=True)
                raise RuntimeError(
                    f"{box.name} produced {size} bytes, over per-box cap "
                    f"{per_box_byte_cap}; bounded sample removed"
                )
            per_box_bytes[box.name] = size
        extraction = [
            {
                "scope": "single-pass-multi-box",
                "boxes": [box.name for box in boxes],
                "rows": sum(int(item["rows"]) for item in box_metrics),
                "candidate_populations": {
                    str(item["box"]): int(item["bbox_population"])
                    for item in box_metrics
                },
                "per_box_parquet_bytes": per_box_bytes,
                "parquet_bytes": combined_size,
                "workspace_parquet_bytes": combined_size,
                "elapsed_seconds": round(elapsed, 3),
                "started_utc": started_utc.isoformat(),
                "completed_utc": datetime.now(timezone.utc).isoformat(),
                "shared_single_pass": True,
                "sample_sql_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            }
        ]
    finally:
        connection.close()
    return analyze_sample(
        combined,
        boxes,
        row_cap,
        extraction,
        per_box_byte_cap,
        overall_byte_cap,
        candidate_count_cap,
        duckdb_temp_cap,
        remote_time_cap_seconds,
    ), combined


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="Temporary sample directory")
    parser.add_argument("--row-cap", type=int, default=DEFAULT_ROW_CAP)
    parser.add_argument(
        "--per-box-byte-cap", type=int, default=DEFAULT_PER_BOX_BYTE_CAP
    )
    parser.add_argument(
        "--overall-byte-cap", type=int, default=DEFAULT_OVERALL_BYTE_CAP
    )
    parser.add_argument(
        "--candidate-count-cap",
        type=int,
        default=DEFAULT_CANDIDATE_COUNT_CAP,
        help=CANDIDATE_CAP_SCOPE,
    )
    parser.add_argument(
        "--duckdb-temp-cap",
        type=int,
        default=DEFAULT_DUCKDB_TEMP_CAP,
        help="DuckDB temporary-workspace byte cap (does not bound remote scan bytes)",
    )
    parser.add_argument(
        "--remote-time-cap-seconds",
        type=int,
        default=DEFAULT_REMOTE_TIME_CAP_SECONDS,
        help="Interrupt the single remote sample query after this wall-clock cap",
    )
    parser.add_argument(
        "--only-box", action="append", choices=[box.name for box in SAMPLE_BOXES]
    )
    parser.add_argument(
        "--sample",
        type=Path,
        help="Analyze an existing combined temporary sample without remote extraction",
    )
    parser.add_argument(
        "--prior-report",
        type=Path,
        help="Preserve extraction timings when re-analyzing an existing sample",
    )
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    selected = set(args.only_box or ())
    boxes = tuple(box for box in SAMPLE_BOXES if not selected or box.name in selected)
    if args.sample:
        extraction: list[dict[str, Any]] = []
        if args.prior_report:
            prior = json.loads(args.prior_report.read_text(encoding="utf-8"))
            extraction = prior.get("sampling", {}).get("extraction", [])
            prior_queries = prior.get("sampling", {}).get("query_sql", {})
            for item in extraction:
                # Selection truth is validated from sample rows below. Do not
                # carry a stale label forward from an earlier report wrapper.
                item.pop("selection_contract", None)
                query = prior_queries.get(item.get("box"))
                if isinstance(query, str):
                    item["legacy_sample_sql"] = query
                    item["legacy_sample_sql_sha256"] = hashlib.sha256(
                        query.encode("utf-8")
                    ).hexdigest()
        report = analyze_sample(
            args.sample,
            boxes,
            args.row_cap,
            extraction,
            args.per_box_byte_cap,
            args.overall_byte_cap,
            args.candidate_count_cap,
            args.duckdb_temp_cap,
            args.remote_time_cap_seconds,
        )
    elif args.output_dir:
        report, _ = run(
            args.output_dir,
            row_cap=args.row_cap,
            per_box_byte_cap=args.per_box_byte_cap,
            overall_byte_cap=args.overall_byte_cap,
            candidate_count_cap=args.candidate_count_cap,
            duckdb_temp_cap=args.duckdb_temp_cap,
            remote_time_cap_seconds=args.remote_time_cap_seconds,
            selected_boxes=selected,
        )
    else:
        with tempfile.TemporaryDirectory(
            prefix="overture-address-sample-"
        ) as directory:
            report, _ = run(
                Path(directory),
                row_cap=args.row_cap,
                per_box_byte_cap=args.per_box_byte_cap,
                overall_byte_cap=args.overall_byte_cap,
                candidate_count_cap=args.candidate_count_cap,
                duckdb_temp_cap=args.duckdb_temp_cap,
                remote_time_cap_seconds=args.remote_time_cap_seconds,
                selected_boxes=selected,
            )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    args.markdown_out.write_text(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
