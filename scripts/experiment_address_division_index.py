#!/usr/bin/env python3
"""Build and measure a compact structured-address index with division links.

The experiment intentionally targets deterministic known-address lookup. It is
not an address parser, fuzzy matcher, interpolation engine, or relevance test.
It keeps source address labels separate from geometric Overture division links.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Iterable

try:
    import duckdb
except ImportError:  # pragma: no cover - pure encoding tests do not need DuckDB
    duckdb = None


MAGIC = b"OADDR01\0"
SUBTYPE_CODES = {
    "region": 1,
    "county": 2,
    "macrohood": 3,
    "locality": 4,
    "neighborhood": 5,
    "microhood": 6,
}
SUBTYPE_ORDER_SQL = """
CASE subtype
    WHEN 'region' THEN 1
    WHEN 'county' THEN 2
    WHEN 'macrohood' THEN 3
    WHEN 'locality' THEN 4
    WHEN 'neighborhood' THEN 5
    WHEN 'microhood' THEN 6
    ELSE 99
END
"""


def encode_uvarint(value: int) -> bytes:
    if value < 0:
        raise ValueError("uvarint cannot encode a negative value")
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def encode_text(value: str | None) -> bytes:
    payload = (value or "").encode("utf-8")
    return encode_uvarint(len(payload)) + payload


def encode_feature_id(value: str | None) -> tuple[bytes, bool]:
    try:
        return uuid.UUID(str(value)).bytes, True
    except (ValueError, TypeError, AttributeError):
        payload = (value or "").encode("utf-8")
        return hashlib.blake2b(payload, digest_size=16).digest(), False


def encode_coordinate(value: float, *, longitude: bool) -> int:
    minimum, maximum = (-180.0, 180.0) if longitude else (-90.0, 90.0)
    if not minimum <= value <= maximum:
        raise ValueError(f"coordinate {value} outside [{minimum}, {maximum}]")
    return round(value * 10_000_000)


def parse_chain(signature: str) -> list[tuple[str, str]]:
    if not signature:
        return []
    result = []
    for item in signature.split("|"):
        subtype, division_id = item.split(":", 1)
        result.append((subtype, division_id))
    return result


def encode_chain(signature: str) -> bytes:
    items = parse_chain(signature)
    output = bytearray(encode_uvarint(len(items)))
    for subtype, division_id in items:
        output.append(SUBTYPE_CODES.get(subtype, 255))
        encoded_id, _ = encode_feature_id(division_id)
        output.extend(encoded_id)
    return bytes(output)


def normalize_expression(column: str) -> str:
    return (
        "LOWER(NFC_NORMALIZE(REGEXP_REPLACE(TRIM(COALESCE(CAST("
        f"{column} AS VARCHAR), '')), '\\s+', ' ', 'g')))"
    )


def sql_literal(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def fetch_one(connection: Any, query: str) -> dict[str, Any]:
    cursor = connection.execute(query)
    columns = [column[0] for column in cursor.description]
    row = cursor.fetchone()
    return dict(zip(columns, row)) if row else {}


def write_indexed_blob(
    entries: Iterable[bytes], index_file: BinaryIO, blob_file: BinaryIO
) -> int:
    count = 0
    for payload in entries:
        index_file.write(struct.pack("<Q", blob_file.tell()))
        blob_file.write(payload)
        count += 1
    index_file.write(struct.pack("<Q", blob_file.tell()))
    return count


def prepare_database(
    connection: Any,
    addresses_path: Path,
    division_areas_path: Path,
) -> dict[str, Any]:
    connection.execute("INSTALL spatial; LOAD spatial")
    connection.execute("SET threads = 12")
    connection.execute("SET memory_limit = '40GB'")

    address_columns = {
        row[0].lower()
        for row in connection.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(addresses_path)]
        ).fetchall()
    }
    city_column = "city" if "city" in address_columns else "locality"
    state_column = "state" if "state" in address_columns else "region"
    id_column = "gers_id" if "gers_id" in address_columns else "id"
    required = {id_column, "lon", "lat", "street", "number"}
    missing = sorted(required - address_columns)
    if missing:
        raise ValueError(
            f"address input missing required columns: {', '.join(missing)}"
        )

    def optional(column: str) -> str:
        return column if column in address_columns else "NULL"

    connection.execute(f"""
        CREATE TABLE address_base AS
        SELECT
            row_number() OVER () - 1 AS address_row,
            CAST({id_column} AS VARCHAR) AS feature_id,
            CAST(lon AS DOUBLE) AS lon,
            CAST(lat AS DOUBLE) AS lat,
            CAST(street AS VARCHAR) AS street,
            CAST(number AS VARCHAR) AS number,
            CAST({optional("unit")} AS VARCHAR) AS unit,
            CAST({optional("postcode")} AS VARCHAR) AS postcode,
            CAST({state_column} AS VARCHAR) AS state,
            CAST({city_column} AS VARCHAR) AS city,
            CAST({optional("postal_city")} AS VARCHAR) AS postal_city,
            {normalize_expression("street")} AS street_norm,
            {normalize_expression("number")} AS number_norm,
            {normalize_expression(optional("unit"))} AS unit_norm,
            {normalize_expression(optional("postcode"))} AS postcode_norm,
            {normalize_expression(state_column)} AS state_norm,
            {normalize_expression(city_column)} AS city_norm,
            {normalize_expression(optional("postal_city"))} AS postal_city_norm
        FROM read_parquet({sql_literal(addresses_path)})
        WHERE lon IS NOT NULL AND lat IS NOT NULL
          AND NULLIF(TRIM(CAST(street AS VARCHAR)), '') IS NOT NULL
          AND NULLIF(TRIM(CAST(number AS VARCHAR)), '') IS NOT NULL
    """)

    connection.execute(f"""
        CREATE TABLE division_areas AS
        SELECT
            CAST(division_id AS VARCHAR) AS division_id,
            CAST(subtype AS VARCHAR) AS subtype,
            CAST(name AS VARCHAR) AS name,
            {normalize_expression("name")} AS name_norm,
            CAST(area_m2 AS DOUBLE) AS area_m2,
            geometry AS geometry,
            bbox
        FROM read_parquet({sql_literal(division_areas_path)})
        WHERE subtype IN ({",".join(repr(item) for item in SUBTYPE_CODES)})
    """)

    join_started = time.monotonic()
    connection.execute("""
        CREATE TABLE division_candidates AS
        SELECT
            a.address_row,
            d.division_id,
            d.subtype,
            d.name_norm,
            d.area_m2,
            a.city_norm,
            a.postal_city_norm
        FROM address_base a
        JOIN division_areas d
          ON a.lon BETWEEN d.bbox.xmin AND d.bbox.xmax
         AND a.lat BETWEEN d.bbox.ymin AND d.bbox.ymax
         AND ST_Within(ST_Point(a.lon, a.lat), d.geometry)
    """)
    join_seconds = time.monotonic() - join_started

    connection.execute("""
        CREATE TABLE division_choices AS
        SELECT address_row, division_id, subtype, name_norm, area_m2,
               name_norm != '' AND name_norm IN (city_norm, postal_city_norm) AS label_match
        FROM division_candidates
        QUALIFY row_number() OVER (
            PARTITION BY address_row, subtype
            ORDER BY
                (name_norm != '' AND name_norm IN (city_norm, postal_city_norm)) DESC,
                area_m2 ASC,
                division_id
        ) = 1
    """)
    connection.execute(f"""
        CREATE TABLE address_chains AS
        SELECT
            address_row,
            string_agg(subtype || ':' || division_id, '|'
                       ORDER BY {SUBTYPE_ORDER_SQL}, division_id) AS chain_signature
        FROM division_choices
        GROUP BY address_row
    """)
    connection.execute("""
        CREATE TABLE enriched AS
        SELECT a.*, COALESCE(c.chain_signature, '') AS chain_signature
        FROM address_base a
        LEFT JOIN address_chains c USING (address_row)
    """)
    connection.execute("""
        CREATE TABLE chain_dim AS
        SELECT row_number() OVER (ORDER BY chain_signature) - 1 AS chain_id,
               chain_signature
        FROM (SELECT DISTINCT chain_signature FROM enriched)
        ORDER BY chain_signature
    """)
    connection.execute("""
        CREATE TABLE context_chain_counts AS
        SELECT state_norm, city_norm, postal_city_norm, postcode_norm,
               chain_signature, count(*) AS row_count
        FROM enriched
        GROUP BY ALL
    """)
    connection.execute("""
        CREATE TABLE context_dim AS
        WITH dominant AS (
            SELECT state_norm, city_norm, postal_city_norm, postcode_norm,
                   chain_signature, row_count,
                   sum(row_count) OVER (
                       PARTITION BY state_norm, city_norm, postal_city_norm, postcode_norm
                   ) AS context_rows
            FROM context_chain_counts
            QUALIFY row_number() OVER (
                PARTITION BY state_norm, city_norm, postal_city_norm, postcode_norm
                ORDER BY row_count DESC, chain_signature
            ) = 1
        )
        SELECT row_number() OVER (
                   ORDER BY state_norm, city_norm, postal_city_norm, postcode_norm
               ) - 1 AS context_id,
               d.*,
               c.chain_id AS dominant_chain_id
        FROM dominant d
        JOIN chain_dim c USING (chain_signature)
        ORDER BY state_norm, city_norm, postal_city_norm, postcode_norm
    """)
    connection.execute("""
        CREATE TABLE indexed_addresses AS
        SELECT e.*, x.context_id, c.chain_id, x.dominant_chain_id
        FROM enriched e
        JOIN context_dim x
          ON e.state_norm = x.state_norm
         AND e.city_norm = x.city_norm
         AND e.postal_city_norm = x.postal_city_norm
         AND e.postcode_norm = x.postcode_norm
        JOIN chain_dim c ON e.chain_signature = c.chain_signature
    """)
    connection.execute("""
        CREATE TABLE street_dim AS
        SELECT row_number() OVER (ORDER BY context_id, street_norm) - 1 AS group_id,
               context_id,
               street_norm,
               mode(street) AS display_street,
               count(*) AS record_count
        FROM indexed_addresses
        GROUP BY context_id, street_norm
        ORDER BY context_id, street_norm
    """)

    return {
        "address_columns": sorted(address_columns),
        "join_seconds": join_seconds,
    }


def collect_metrics(connection: Any) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    metrics["inventory"] = fetch_one(
        connection,
        """
        SELECT
            (SELECT count(*) FROM address_base) AS indexed_addresses,
            (SELECT count(*) FROM chain_dim) AS division_chains,
            (SELECT count(*) FROM context_dim) AS contexts,
            (SELECT count(*) FROM street_dim) AS street_groups,
            (SELECT count(DISTINCT street_norm) FROM street_dim) AS distinct_street_names
    """,
    )
    metrics["division_coverage"] = fetch_one(
        connection,
        """
        SELECT
            count(*) AS addresses,
            count_if(chain_signature != '') AS with_any_division,
            count(DISTINCT chain_signature) AS distinct_chains,
            count_if(chain_id = dominant_chain_id) AS using_context_dominant_chain,
            count_if(chain_id != dominant_chain_id) AS requiring_chain_override
        FROM indexed_addresses
    """,
    )
    metrics["candidate_ambiguity"] = fetch_one(
        connection,
        """
        WITH grouped AS (
            SELECT address_row, subtype, count(*) AS candidates
            FROM division_candidates
            GROUP BY address_row, subtype
        )
        SELECT
            count(*) AS address_subtype_pairs,
            count_if(candidates > 1) AS ambiguous_pairs,
            max(candidates) AS max_candidates
        FROM grouped
    """,
    )
    metrics["division_subtypes"] = [
        dict(zip([column[0] for column in cursor.description], row))
        for cursor in [
            connection.execute("""
            SELECT subtype,
                   count(*) AS linked_addresses,
                   count_if(label_match) AS source_label_matches,
                   count(DISTINCT division_id) AS distinct_divisions
            FROM division_choices
            GROUP BY subtype
            ORDER BY CASE subtype
                WHEN 'region' THEN 1 WHEN 'county' THEN 2 WHEN 'macrohood' THEN 3
                WHEN 'locality' THEN 4 WHEN 'neighborhood' THEN 5
                WHEN 'microhood' THEN 6 ELSE 99 END
        """)
        ]
        for row in cursor.fetchall()
    ]
    metrics["context_stability"] = fetch_one(
        connection,
        """
        SELECT
            count(*) AS contexts,
            count_if(row_count = context_rows) AS unanimous_contexts,
            sum(row_count) AS rows_on_dominant_chain,
            sum(context_rows) AS address_rows,
            quantile_cont(row_count::DOUBLE / context_rows, 0.5) AS median_dominant_share,
            quantile_cont(row_count::DOUBLE / context_rows, 0.9) AS p90_dominant_share
        FROM context_dim
    """,
    )
    metrics["street_group_shape"] = fetch_one(
        connection,
        """
        SELECT
            min(record_count) AS minimum,
            quantile_cont(record_count, 0.5) AS median,
            quantile_cont(record_count, 0.9) AS p90,
            quantile_cont(record_count, 0.99) AS p99,
            max(record_count) AS maximum
        FROM street_dim
    """,
    )
    return metrics


def build_artifact(
    connection: Any, output: Path
) -> tuple[dict[str, int], dict[str, Any]]:
    output.parent.mkdir(parents=True, exist_ok=True)
    invalid_feature_ids = 0
    record_group_bytes: list[int] = []

    with tempfile.TemporaryDirectory(
        prefix="address-index-", dir=output.parent
    ) as temp_name:
        temp = Path(temp_name)
        paths = {
            "chain_blob": temp / "chains.bin",
            "context_offsets": temp / "context-offsets.bin",
            "context_blob": temp / "contexts.bin",
            "street_offsets": temp / "street-offsets.bin",
            "street_blob": temp / "streets.bin",
            "record_blob": temp / "records.bin",
        }

        with paths["chain_blob"].open("wb") as chain_file:
            for (signature,) in connection.execute(
                "SELECT chain_signature FROM chain_dim ORDER BY chain_id"
            ).fetchall():
                chain_file.write(encode_chain(signature))

        context_group_shape = {
            row[0]: (row[1], row[2])
            for row in connection.execute("""
                SELECT context_id, min(group_id), count(*)
                FROM street_dim GROUP BY context_id
            """).fetchall()
        }
        with (
            paths["context_offsets"].open("wb") as offsets,
            paths["context_blob"].open("wb") as blob,
        ):
            rows = connection.execute("""
                SELECT context_id, state_norm, city_norm, postal_city_norm, postcode_norm,
                       dominant_chain_id
                FROM context_dim ORDER BY context_id
            """).fetchall()

            def context_entries() -> Iterable[bytes]:
                for context_id, state, city, postal_city, postcode, chain_id in rows:
                    group_start, group_count = context_group_shape.get(
                        context_id, (0, 0)
                    )
                    yield b"".join(
                        (
                            encode_text(state),
                            encode_text(city),
                            encode_text(postal_city),
                            encode_text(postcode),
                            encode_uvarint(chain_id),
                            encode_uvarint(group_start),
                            encode_uvarint(group_count),
                        )
                    )

            write_indexed_blob(context_entries(), offsets, blob)

        record_file = paths["record_blob"].open("wb")
        street_file = paths["street_blob"].open("wb")
        street_offsets = paths["street_offsets"].open("wb")
        try:
            cursor = connection.execute("""
                SELECT s.group_id, s.context_id, s.street_norm, s.display_street,
                       a.feature_id, a.lon, a.lat, a.number, a.unit,
                       a.chain_id, a.dominant_chain_id
                FROM indexed_addresses a
                JOIN street_dim s USING (context_id, street_norm)
                ORDER BY s.group_id, a.number_norm, a.unit_norm, a.feature_id
            """)
            current_group: int | None = None
            group_context = 0
            group_street = ""
            group_display = ""
            group_start = 0
            group_count = 0

            def finish_group() -> None:
                nonlocal group_count
                if current_group is None:
                    return
                street_offsets.write(struct.pack("<Q", street_file.tell()))
                street_file.write(encode_uvarint(group_context))
                street_file.write(encode_text(group_street))
                street_file.write(encode_text(group_display))
                street_file.write(encode_uvarint(group_start))
                street_file.write(encode_uvarint(group_count))
                record_group_bytes.append(record_file.tell() - group_start)

            while batch := cursor.fetchmany(100_000):
                for row in batch:
                    (
                        group_id,
                        context_id,
                        street_norm,
                        display_street,
                        feature_id,
                        lon,
                        lat,
                        number,
                        unit,
                        chain_id,
                        dominant_chain_id,
                    ) = row
                    if group_id != current_group:
                        finish_group()
                        current_group = group_id
                        group_context = context_id
                        group_street = street_norm
                        group_display = display_street
                        group_start = record_file.tell()
                        group_count = 0

                    encoded_id, valid_id = encode_feature_id(feature_id)
                    invalid_feature_ids += int(not valid_id)
                    record_file.write(encoded_id)
                    record_file.write(
                        struct.pack(
                            "<ii",
                            encode_coordinate(lon, longitude=True),
                            encode_coordinate(lat, longitude=False),
                        )
                    )
                    override = 0 if chain_id == dominant_chain_id else chain_id + 1
                    record_file.write(encode_uvarint(override))
                    record_file.write(encode_text(number))
                    record_file.write(encode_text(unit))
                    group_count += 1
            finish_group()
            street_offsets.write(struct.pack("<Q", street_file.tell()))
        finally:
            record_file.close()
            street_file.close()
            street_offsets.close()

        component_sizes = {name: path.stat().st_size for name, path in paths.items()}
        header = {
            "format": 1,
            "components": component_sizes,
            "counts": {
                "chains": connection.execute(
                    "SELECT count(*) FROM chain_dim"
                ).fetchone()[0],
                "contexts": connection.execute(
                    "SELECT count(*) FROM context_dim"
                ).fetchone()[0],
                "street_groups": connection.execute(
                    "SELECT count(*) FROM street_dim"
                ).fetchone()[0],
                "addresses": connection.execute(
                    "SELECT count(*) FROM indexed_addresses"
                ).fetchone()[0],
            },
        }
        encoded_header = json.dumps(
            header, sort_keys=True, separators=(",", ":")
        ).encode()
        with output.open("wb") as artifact:
            artifact.write(MAGIC)
            artifact.write(struct.pack("<I", len(encoded_header)))
            artifact.write(encoded_header)
            for path in paths.values():
                with path.open("rb") as source:
                    shutil.copyfileobj(source, artifact, length=1024 * 1024)

    shape = {
        "invalid_feature_ids": invalid_feature_ids,
        "maximum_record_group_bytes": max(record_group_bytes, default=0),
        "median_record_group_bytes": sorted(record_group_bytes)[
            len(record_group_bytes) // 2
        ]
        if record_group_bytes
        else 0,
    }
    return component_sizes, shape


def markdown_report(report: dict[str, Any]) -> str:
    inventory = report["metrics"]["inventory"]
    coverage = report["metrics"]["division_coverage"]
    contexts = report["metrics"]["context_stability"]
    groups = report["metrics"]["street_group_shape"]
    ambiguity = report["metrics"]["candidate_ambiguity"]
    sizes = report["artifact"]["components"]
    rows = inventory["indexed_addresses"]
    artifact_bytes = report["artifact"]["bytes"]
    lines = [
        "# Compact address + division-link spike",
        "",
        f"Date: {report['date']}",
        "",
        "## Verdict",
        "",
        "This experiment measures a deterministic known-address lookup format, not",
        "free-form parsing, fuzzy matching, interpolation, or human relevance.",
        "Source address labels remain authoritative display/query data; geometric",
        "division links are separate derived context.",
        "",
        "The storage result is encouraging: address cardinality is large, but its",
        "structured repetition compresses much better than the POI token index. The",
        "remaining hard problem is parsing and country-specific normalization, not R2",
        "storage.",
        "",
        "## Input and build",
        "",
        f"- Addresses: {rows:,} keyable Massachusetts records",
        f"- Division areas: {report['division_area_rows']:,} from Overture release `{report['division_release']}`",
        f"- Division spatial join: {report['database']['join_seconds']:.2f} seconds",
        f"- Total experiment: {report['elapsed_seconds']:.2f} seconds",
        f"- Artifact: {artifact_bytes:,} bytes ({artifact_bytes / rows:.1f} B/address)",
        "",
        "The address artifact is historical while the division areas are current-release;",
        "the result is architecture evidence, not a release-valid production join.",
        "",
        "## Artifact components",
        "",
        "| component | bytes | B/address |",
        "|---|---:|---:|",
    ]
    for name, size in sizes.items():
        lines.append(f"| {name} | {size:,} | {size / rows:.2f} |")
    lines.extend(
        [
            f"| header/magic | {artifact_bytes - sum(sizes.values()):,} | {(artifact_bytes - sum(sizes.values())) / rows:.2f} |",
            f"| **total** | **{artifact_bytes:,}** | **{artifact_bytes / rows:.2f}** |",
            "",
            "The hot record contains a 16-byte feature ID, quantized coordinates,",
            "number, unit, and a normally-zero division-chain override. Context and street",
            "strings are dictionary/group encoded. Source provenance and source-file",
            "locators are not present in this historical input and require a measured",
            "allowance in a current-release producer.",
            "",
            "## Division linking",
            "",
            f"- Addresses inside at least one selected division area: {coverage['with_any_division']:,} / {coverage['addresses']:,}",
            f"- Distinct per-address division chains: {coverage['distinct_chains']:,}",
            f"- Chain matches context-dominant chain: {coverage['using_context_dominant_chain']:,}",
            f"- Records requiring an override: {coverage['requiring_chain_override']:,}",
            f"- Contexts: {contexts['contexts']:,}; unanimous contexts: {contexts['unanimous_contexts']:,}",
            f"- Address/subtype pairs with two containing areas: {ambiguity['ambiguous_pairs']:,} (maximum {ambiguity['max_candidates']})",
            "",
            "| subtype | linked addresses | source-label matches | distinct divisions |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in report["metrics"]["division_subtypes"]:
        lines.append(
            f"| {row['subtype']} | {row['linked_addresses']:,} | "
            f"{row['source_label_matches']:,} | {row['distinct_divisions']:,} |"
        )
    lines.extend(
        [
            "",
            "A chain dictionary ID is useful for response context, routing, and linking",
            "to the existing divisions index. It must not replace `address_levels` or",
            "`postal_city`: Overture explicitly notes that address levels are country-",
            "dependent addressing units and need not correspond to administrative divisions.",
            "The region/county/locality IDs here come from containing division areas; a",
            "country division ID can be stored once in the shard manifest rather than on",
            "every address. Registry membership should be verified before describing any",
            "particular Overture division ID as a GERS ID.",
            "",
            "## Lookup shape",
            "",
            f"- Contexts: {inventory['contexts']:,}",
            f"- Context/street groups: {inventory['street_groups']:,}",
            f"- Distinct normalized street names: {inventory['distinct_street_names']:,}",
            f"- Records per street group: median {groups['median']:.0f}, p90 {groups['p90']:.0f}, p99 {groups['p99']:.0f}, max {groups['maximum']:,}",
            f"- Median/max encoded record-group bytes: {report['artifact']['shape']['median_record_group_bytes']:,} / {report['artifact']['shape']['maximum_record_group_bytes']:,}",
            "",
            "The serving path is: parse or accept structured country/region/locality/",
            "postcode, resolve a context, binary-search its normalized street range,",
            "then exact-search number and unit within one compact record block. Prefix",
            "street lookup follows adjacent sorted street entries. This does not yet",
            "supply a robust parser for arbitrary one-line input.",
            "",
            "## Linear diagnostics, not forecasts",
            "",
            f"At the measured {artifact_bytes / rows:.1f} B/address, 473M addresses would be",
            f"about {artifact_bytes / rows * 473_000_000 / 1_000_000_000:.2f} GB per release before source-locator",
            "metadata, country-specific parser indexes, manifests, or rollback retention.",
            "Massachusetts source and unit distributions are not globally representative.",
            f"A purely linear processing diagnostic is about {report['elapsed_seconds'] / rows * 473_000_000 / 3600:.2f} factory-hours;",
            "global polygon density, extraction, sorting, upload, and country-specific work",
            "make that a lower-confidence shape rather than a build-time forecast.",
            "",
            "Official schema references: [Address](https://docs.overturemaps.org/schema/reference/addresses/address/),",
            "[AddressLevel](https://docs.overturemaps.org/schema/reference/addresses/types/address_level/), and",
            "[DivisionArea](https://docs.overturemaps.org/schema/reference/divisions/division_area/).",
            "",
            "## Next gate",
            "",
            "1. Repeat on one current-release country/region extract carrying source filepath",
            "   and row-group locator fields.",
            "2. Preserve raw `address_levels` and measure their dictionary cost separately",
            "   from geometric division-chain IDs.",
            "3. Implement and evaluate a bounded US one-line parser plus structured endpoint",
            "   against independently labelled queries.",
            "4. Measure Worker range reads for exact, prefix, ambiguous, unit, and no-result",
            "   cases before extrapolating a public API.",
            "5. Define how overlapping political perspectives and multiple containing",
            "   division areas are represented rather than silently choosing one globally.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("addresses", type=Path)
    parser.add_argument("division_areas", type=Path)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument("--division-release", default="2026-06-17.0")
    args = parser.parse_args()
    if duckdb is None:
        raise SystemExit("experiment_address_division_index.py requires duckdb")

    started = time.monotonic()
    database = args.database or args.artifact.with_suffix(".duckdb")
    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        database.unlink()
    connection = duckdb.connect(str(database))
    database_evidence = prepare_database(
        connection, args.addresses, args.division_areas
    )
    metrics = collect_metrics(connection)
    component_sizes, artifact_shape = build_artifact(connection, args.artifact)
    division_area_rows = connection.execute(
        "SELECT count(*) FROM division_areas"
    ).fetchone()[0]
    connection.close()

    report = {
        "date": "2026-07-14",
        "addresses": str(args.addresses),
        "division_areas": str(args.division_areas),
        "division_release": args.division_release,
        "division_area_rows": division_area_rows,
        "database": database_evidence,
        "metrics": metrics,
        "artifact": {
            "path": str(args.artifact),
            "bytes": args.artifact.stat().st_size,
            "components": component_sizes,
            "shape": artifact_shape,
        },
        "elapsed_seconds": time.monotonic() - started,
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    args.markdown_out.write_text(markdown_report(report))
    print(args.markdown_out.read_text())


if __name__ == "__main__":
    main()
