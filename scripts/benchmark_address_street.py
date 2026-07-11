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
    "gers_id": (("gers_id", "id"), "VARCHAR"),
    "number": (("number",), "VARCHAR"),
    "street": (("street",), "VARCHAR"),
    "unit": (("unit",), "VARCHAR"),
    "postcode": (("postcode",), "VARCHAR"),
    "state": (("state", "region"), "VARCHAR"),
    "city": (("city", "locality"), "VARCHAR"),
    "postal_city": (("postal_city",), "VARCHAR"),
    "country": (("country",), "VARCHAR"),
    "lon": (("lon", "longitude"), "DOUBLE"),
    "lat": (("lat", "latitude"), "DOUBLE"),
    "version": (("version",), "BIGINT"),
    "source_dataset": (("source_dataset", "root_source_dataset"), "VARCHAR"),
    "source_update_time": (("source_update_time", "root_source_update_time"), "TIMESTAMPTZ"),
    "source_confidence": (("source_confidence", "root_source_confidence"), "DOUBLE"),
    "search_text": (("search_text",), "VARCHAR"),
}

TEXT_COLUMNS = {
    "gers_id", "number", "street", "unit", "postcode", "state", "city",
    "postal_city", "country", "source_dataset", "search_text",
}


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _normalize_sql(expression: str) -> str:
    """Case/space normalize while preserving punctuation and diacritics."""
    return (
        "LOWER(NFC_NORMALIZE(REGEXP_REPLACE(TRIM(COALESCE(CAST("
        f"{expression} AS VARCHAR), '')), '\\s+', ' ', 'g')))"
    )


def _lossy_sql(expression: str) -> str:
    """Deliberately lossy punctuation removal used only for collision audits."""
    return f"REGEXP_REPLACE({expression}, '[^[:alnum:]]+', '', 'g')"


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
        "city_norm": _normalize_sql("COALESCE(city, postal_city)"),
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
        if field in TEXT_COLUMNS:
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
            SELECT number_norm, street_norm, city_norm, state_norm, postcode_norm, country_norm,
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
            "number_norm", "street_norm", "unit_norm", "city_norm",
            "state_norm", "postcode_norm", "country_norm",
        )
    }
    base_cte = f"""
        WITH keyed AS (
            SELECT *,
                   {lossy['number_norm']} AS l_number,
                   {lossy['street_norm']} AS l_street,
                   {lossy['unit_norm']} AS l_unit,
                   {lossy['city_norm']} AS l_city,
                   {lossy['state_norm']} AS l_state,
                   {lossy['postcode_norm']} AS l_postcode,
                   {lossy['country_norm']} AS l_country
            FROM normalized_addresses
            WHERE number_norm != '' AND street_norm != ''
        ), collisions AS (
            SELECT l_number, l_street, l_unit, l_city, l_state, l_postcode, l_country,
                   COUNT(*) AS row_count,
                   COUNT(DISTINCT struct_pack(
                       number := number_norm, street := street_norm, unit := unit_norm,
                       city := city_norm, state := state_norm, postcode := postcode_norm,
                       country := country_norm
                   )) AS conservative_key_count,
                   LIST(DISTINCT CONCAT_WS(' | ', number_norm, street_norm, unit_norm,
                                           city_norm, state_norm, postcode_norm, country_norm)) AS examples
            FROM keyed
            GROUP BY l_number, l_street, l_unit, l_city, l_state, l_postcode, l_country
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
        SELECT row_count, conservative_key_count, examples[1:5] AS conservative_examples
        FROM collisions
        ORDER BY conservative_key_count DESC, row_count DESC
        LIMIT 10
    """)
    summary["recommended_normalization"] = (
        "NFC normalize, lowercase, trim, and collapse whitespace per structured "
        "component; preserve punctuation, diacritics, and component boundaries."
    )
    summary["lossy_normalization"] = (
        "The comparison removes every non-alphanumeric character inside each "
        "component. It is deliberately lossy and is not recommended for keys."
    )
    return summary


def _coordinate_ambiguity(connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    cte = """
        WITH exact_groups AS (
            SELECT number_norm, street_norm, unit_norm, city_norm, state_norm,
                   postcode_norm, country_norm,
                   COUNT(*) AS row_count,
                   COUNT(DISTINCT struct_pack(lat := lat, lon := lon)) AS coordinate_count,
                   MIN(lat) AS min_lat, MAX(lat) AS max_lat,
                   MIN(lon) AS min_lon, MAX(lon) AS max_lon
            FROM normalized_addresses
            WHERE number_norm != '' AND street_norm != ''
              AND lat IS NOT NULL AND lon IS NOT NULL
            GROUP BY ALL
        ), measured AS (
            SELECT *,
                   111320.0 * SQRT(
                       POW(max_lat - min_lat, 2)
                       + POW(COS(RADIANS((max_lat + min_lat) / 2.0)) * (max_lon - min_lon), 2)
                   ) AS spread_m
            FROM exact_groups
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
    summary = _fetch_one(connection, cte + """
        SELECT COUNT(*) AS exact_keys_with_coordinates,
               COUNT_IF(row_count > 1) AS duplicate_exact_keys,
               COUNT_IF(coordinate_count > 1) AS coordinate_ambiguous_keys,
               COALESCE(SUM(row_count) FILTER (WHERE coordinate_count > 1), 0)
                   AS rows_on_coordinate_ambiguous_keys,
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
                   COUNT(DISTINCT struct_pack(city := city_norm, state := state_norm))
                       FILTER (WHERE city_norm != '') AS city_fanout,
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
               COUNT_IF(city_fanout > 1) AS names_spanning_multiple_cities,
               COUNT_IF(postcode_fanout > 1) AS names_spanning_multiple_postcodes,
               COALESCE(QUANTILE_CONT(city_fanout, 0.5), 0) AS median_city_fanout,
               COALESCE(QUANTILE_CONT(city_fanout, 0.9), 0) AS p90_city_fanout,
               COALESCE(MAX(city_fanout), 0) AS max_city_fanout,
               COALESCE(QUANTILE_CONT(postcode_fanout, 0.5), 0) AS median_postcode_fanout,
               COALESCE(QUANTILE_CONT(postcode_fanout, 0.9), 0) AS p90_postcode_fanout,
               COALESCE(MAX(postcode_fanout), 0) AS max_postcode_fanout
        FROM streets
    """)
    summary["highest_fanout"] = _fetch_dicts(connection, cte + """
        SELECT street, address_rows, city_fanout, postcode_fanout
        FROM streets
        ORDER BY city_fanout DESC, postcode_fanout DESC, address_rows DESC, street
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
        report = {
            "report_version": REPORT_VERSION,
            "input": {
                "path": str(input_path),
                "size_bytes": input_path.stat().st_size,
                "row_count": row_count,
                "columns": sorted(columns.values()),
            },
            "field_coverage": _field_coverage(connection, row_count, availability),
            "unit_and_base_key_density": _unit_and_base_density(connection),
            "normalization_collisions": _normalization_collisions(connection),
            "exact_key_coordinate_ambiguity": _coordinate_ambiguity(connection),
            "postcode_shard_distribution": _postcode_distribution(connection),
            "address_derived_street_proxy": _street_proxy(connection),
            "historical_byte_extrapolations": historical_byte_extrapolations(
                historical_metrics, row_count
            ),
            "limitations": [
                PROXY_WARNING,
                "Exact-key spread uses the diagonal of each group's coordinate envelope, not all pairwise distances.",
                "A collision reports key ambiguity, not proof that either source record is incorrect.",
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
    unit = report["unit_and_base_key_density"]
    collisions = report["normalization_collisions"]
    ambiguity = report["exact_key_coordinate_ambiguity"]
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
        "## Exact-key coordinate ambiguity",
        "",
        f"- Exact keys with coordinates: {ambiguity['exact_keys_with_coordinates']:,}",
        f"- Duplicate exact keys: {ambiguity['duplicate_exact_keys']:,}",
        f"- Coordinate-ambiguous keys: {ambiguity['coordinate_ambiguous_keys']:,}",
        "",
        "| Envelope distance | Exact keys | Rows |",
        "|---|---:|---:|",
    ]
    for item in ambiguity["distance_buckets"]:
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
        f"- Names spanning multiple cities: {street['names_spanning_multiple_cities']:,}",
        f"- Names spanning multiple postcodes: {street['names_spanning_multiple_postcodes']:,}",
        f"- P90 city fanout: {street['p90_city_fanout']:,.1f}",
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
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    report = benchmark(args.input, args.historical_metrics)
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
