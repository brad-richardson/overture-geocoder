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
import os
import shutil
import struct
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Iterable

try:
    import duckdb
except ImportError:  # pragma: no cover - pure encoding tests do not need DuckDB
    duckdb = None


MAGIC = b"OADDR01\0"
COMPONENT_ORDER = (
    "chain_offsets",
    "chain_blob",
    "context_offsets",
    "context_blob",
    "street_offsets",
    "street_blob",
    "label_offsets",
    "label_blob",
    "record_offsets",
    "record_blob",
)
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


def encode_feature_id(value: str | None) -> bytes:
    try:
        return uuid.UUID(str(value)).bytes
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"invalid UUID: {value!r}") from exc


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
        output.extend(encode_feature_id(division_id))
    return bytes(output)


def normalize_expression(column: str) -> str:
    return (
        "TRANSLATE(NFC_NORMALIZE(REGEXP_REPLACE(TRIM(COALESCE(CAST("
        f"{column} AS VARCHAR), '')), '\\s+', ' ', 'g')), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    )


def normalize(value: str | None) -> str:
    normalized = " ".join(unicodedata.normalize("NFC", value or "").strip().split())
    return normalized.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def sql_literal(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


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
    *,
    threads: int,
    memory_limit: str,
) -> dict[str, Any]:
    connection.execute("INSTALL spatial; LOAD spatial")
    connection.execute(f"SET threads = {threads}")
    connection.execute(f"SET memory_limit = {sql_literal(memory_limit)}")

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
            COALESCE(CAST(street AS VARCHAR), '') AS street,
            CAST(number AS VARCHAR) AS number,
            COALESCE(CAST({optional("unit")} AS VARCHAR), '') AS unit,
            COALESCE(CAST({optional("postcode")} AS VARCHAR), '') AS postcode,
            COALESCE(CAST({state_column} AS VARCHAR), '') AS state,
            COALESCE(CAST({city_column} AS VARCHAR), '') AS city,
            COALESCE(CAST({optional("postal_city")} AS VARCHAR), '') AS postal_city,
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
            a.postal_city_norm,
            ST_Within(ST_Point(a.lon, a.lat), d.geometry) AS interior
        FROM address_base a
        JOIN division_areas d
          ON a.lon BETWEEN d.bbox.xmin AND d.bbox.xmax
         AND a.lat BETWEEN d.bbox.ymin AND d.bbox.ymax
         AND ST_Covers(d.geometry, ST_Point(a.lon, a.lat))
    """)
    join_seconds = time.monotonic() - join_started

    connection.execute("""
        CREATE TABLE division_choices AS
        SELECT DISTINCT address_row, division_id, subtype, name_norm, area_m2,
               name_norm != '' AND name_norm IN (city_norm, postal_city_norm) AS label_match,
               interior
        FROM division_candidates
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
        CREATE TABLE label_dim AS
        SELECT row_number() OVER (
                   ORDER BY state, city, postal_city, postcode, street
               ) - 1 AS label_id,
               state, city, postal_city, postcode, street
        FROM (
            SELECT DISTINCT state, city, postal_city, postcode, street
            FROM enriched
        )
        ORDER BY state, city, postal_city, postcode, street
    """)
    connection.execute("""
        CREATE TABLE indexed_addresses AS
        SELECT e.*, x.context_id, c.chain_id, x.dominant_chain_id, l.label_id
        FROM enriched e
        JOIN context_dim x
          ON e.state_norm = x.state_norm
         AND e.city_norm = x.city_norm
         AND e.postal_city_norm = x.postal_city_norm
         AND e.postcode_norm = x.postcode_norm
        JOIN chain_dim c ON e.chain_signature = c.chain_signature
        JOIN label_dim l USING (state, city, postal_city, postcode, street)
    """)
    connection.execute("""
        CREATE TABLE street_dim AS
        WITH counts AS (
            SELECT context_id, street_norm, street, count(*) AS label_count
            FROM indexed_addresses
            GROUP BY context_id, street_norm, street
        ), chosen AS (
            SELECT context_id, street_norm, street AS display_street
            FROM counts
            QUALIFY row_number() OVER (
                PARTITION BY context_id, street_norm
                ORDER BY label_count DESC, street
            ) = 1
        ), totals AS (
            SELECT context_id, street_norm, count(*) AS record_count
            FROM indexed_addresses
            GROUP BY context_id, street_norm
        )
        SELECT row_number() OVER (ORDER BY t.context_id, t.street_norm) - 1 AS group_id,
               t.context_id, t.street_norm, c.display_street, t.record_count
        FROM totals t JOIN chosen c USING (context_id, street_norm)
        ORDER BY context_id, street_norm
    """)

    return {
        "address_columns": sorted(address_columns),
        "join_seconds": join_seconds,
        "threads": threads,
        "memory_limit": memory_limit,
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
            (SELECT count(*) FROM label_dim) AS source_label_sets,
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
            max(candidates) AS max_candidates,
            (SELECT count(DISTINCT address_row) FROM division_candidates
             WHERE NOT interior) AS boundary_addresses,
            (SELECT count(*) FROM address_base a
             WHERE NOT EXISTS (
                 SELECT 1 FROM division_candidates d
                 WHERE d.address_row = a.address_row
             )) AS unmatched_addresses
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
    record_group_bytes: list[int] = []

    with tempfile.TemporaryDirectory(
        prefix="address-index-", dir=output.parent
    ) as temp_name:
        temp = Path(temp_name)
        paths = {
            "chain_offsets": temp / "chain-offsets.bin",
            "chain_blob": temp / "chains.bin",
            "context_offsets": temp / "context-offsets.bin",
            "context_blob": temp / "contexts.bin",
            "street_offsets": temp / "street-offsets.bin",
            "street_blob": temp / "streets.bin",
            "label_offsets": temp / "label-offsets.bin",
            "label_blob": temp / "labels.bin",
            "record_offsets": temp / "record-offsets.bin",
            "record_blob": temp / "records.bin",
        }

        with (
            paths["chain_offsets"].open("wb") as offsets,
            paths["chain_blob"].open("wb") as blob,
        ):
            write_indexed_blob(
                (
                    encode_chain(signature)
                    for (signature,) in connection.execute(
                        "SELECT chain_signature FROM chain_dim ORDER BY chain_id"
                    ).fetchall()
                ),
                offsets,
                blob,
            )

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

        with (
            paths["label_offsets"].open("wb") as offsets,
            paths["label_blob"].open("wb") as blob,
        ):
            write_indexed_blob(
                (
                    b"".join(encode_text(value) for value in row)
                    for row in connection.execute("""
                        SELECT state, city, postal_city, postcode, street
                        FROM label_dim ORDER BY label_id
                    """).fetchall()
                ),
                offsets,
                blob,
            )

        record_file = paths["record_blob"].open("wb")
        record_offsets = paths["record_offsets"].open("wb")
        street_file = paths["street_blob"].open("wb")
        street_offsets = paths["street_offsets"].open("wb")
        try:
            cursor = connection.execute("""
                SELECT s.group_id, s.context_id, s.street_norm, s.display_street,
                       a.feature_id, a.lon, a.lat, a.number, a.unit,
                       a.chain_id, a.dominant_chain_id, a.label_id
                FROM indexed_addresses a
                JOIN street_dim s USING (context_id, street_norm)
                ORDER BY s.group_id, a.number_norm, a.unit_norm, a.feature_id
            """)
            current_group: int | None = None
            group_context = 0
            group_street = ""
            group_display = ""
            group_record_start = 0
            group_byte_start = 0
            group_count = 0
            record_ordinal = 0

            def finish_group() -> None:
                nonlocal group_count
                if current_group is None:
                    return
                street_offsets.write(struct.pack("<Q", street_file.tell()))
                street_file.write(encode_uvarint(group_context))
                street_file.write(encode_text(group_street))
                street_file.write(encode_text(group_display))
                street_file.write(encode_uvarint(group_record_start))
                street_file.write(encode_uvarint(group_count))
                record_group_bytes.append(record_file.tell() - group_byte_start)

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
                        label_id,
                    ) = row
                    if group_id != current_group:
                        finish_group()
                        current_group = group_id
                        group_context = context_id
                        group_street = street_norm
                        group_display = display_street
                        group_record_start = record_ordinal
                        group_byte_start = record_file.tell()
                        group_count = 0

                    record_offsets.write(struct.pack("<Q", record_file.tell()))
                    record_file.write(encode_feature_id(feature_id))
                    record_file.write(
                        struct.pack(
                            "<ii",
                            encode_coordinate(lon, longitude=True),
                            encode_coordinate(lat, longitude=False),
                        )
                    )
                    override = 0 if chain_id == dominant_chain_id else chain_id + 1
                    record_file.write(encode_uvarint(override))
                    record_file.write(encode_uvarint(label_id))
                    record_file.write(encode_text(number))
                    record_file.write(encode_text(unit))
                    group_count += 1
                    record_ordinal += 1
            finish_group()
            street_offsets.write(struct.pack("<Q", street_file.tell()))
            record_offsets.write(struct.pack("<Q", record_file.tell()))
        finally:
            record_file.close()
            record_offsets.close()
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
                "source_label_sets": connection.execute(
                    "SELECT count(*) FROM label_dim"
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
        "maximum_record_group_bytes": max(record_group_bytes, default=0),
        "median_record_group_bytes": sorted(record_group_bytes)[
            len(record_group_bytes) // 2
        ]
        if record_group_bytes
        else 0,
    }
    return component_sizes, shape


def decode_uvarint(payload: bytes, position: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    while position < len(payload) and shift <= 63:
        byte = payload[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7
    raise ValueError("invalid or truncated uvarint")


def decode_text(payload: bytes, position: int) -> tuple[str, int]:
    length, position = decode_uvarint(payload, position)
    end = position + length
    if end > len(payload):
        raise ValueError("truncated text")
    return payload[position:end].decode("utf-8"), end


class AddressArtifact:
    """Strict local reader used to prove the experimental lookup contract."""

    def __init__(self, path: Path):
        self.path = path
        self.file = path.open("rb")
        if self.file.read(len(MAGIC)) != MAGIC:
            raise ValueError("invalid address artifact magic")
        length_bytes = self.file.read(4)
        if len(length_bytes) != 4:
            raise ValueError("truncated address artifact header length")
        header_length = struct.unpack("<I", length_bytes)[0]
        try:
            self.header = json.loads(self.file.read(header_length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid address artifact header") from exc
        if self.header.get("format") != 1:
            raise ValueError("unsupported address artifact format")
        components = self.header.get("components")
        if not isinstance(components, dict) or set(components) != set(COMPONENT_ORDER):
            raise ValueError("invalid address artifact components")
        offset = len(MAGIC) + 4 + header_length
        self.components: dict[str, tuple[int, int]] = {}
        for name in COMPONENT_ORDER:
            size = components[name]
            if not isinstance(size, int) or size < 0:
                raise ValueError(f"invalid component size: {name}")
            self.components[name] = (offset, size)
            offset += size
        if offset != path.stat().st_size:
            raise ValueError("address artifact size does not match header")
        self._validate_indexes()

    def close(self) -> None:
        self.file.close()

    def __enter__(self) -> AddressArtifact:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _read_component(self, name: str, offset: int, length: int) -> bytes:
        component_offset, component_size = self.components[name]
        if offset < 0 or length < 0 or offset + length > component_size:
            raise ValueError(f"out-of-bounds {name} read")
        self.file.seek(component_offset + offset)
        payload = self.file.read(length)
        if len(payload) != length:
            raise ValueError(f"truncated {name}")
        return payload

    def _offset(self, index_name: str, item: int) -> int:
        payload = self._read_component(index_name, item * 8, 8)
        return struct.unpack("<Q", payload)[0]

    def _indexed_payload(self, family: str, item: int) -> bytes:
        count = self.header["counts"][family]
        if item < 0 or item >= count:
            raise ValueError(f"invalid {family} index")
        stem = {
            "chains": "chain",
            "contexts": "context",
            "street_groups": "street",
            "source_label_sets": "label",
        }[family]
        index_name = f"{stem}_offsets"
        blob_name = f"{stem}_blob"
        start = self._offset(index_name, item)
        end = self._offset(index_name, item + 1)
        if end < start:
            raise ValueError(f"non-monotonic {family} offsets")
        return self._read_component(blob_name, start, end - start)

    def _validate_indexes(self) -> None:
        for family in ("chains", "contexts", "street_groups", "source_label_sets"):
            stem = {
                "chains": "chain",
                "contexts": "context",
                "street_groups": "street",
                "source_label_sets": "label",
            }[family]
            count = self.header["counts"].get(family)
            index_size = self.components[f"{stem}_offsets"][1]
            blob_size = self.components[f"{stem}_blob"][1]
            if not isinstance(count, int) or index_size != (count + 1) * 8:
                raise ValueError(f"invalid {stem} offset count")
            offsets = self._read_component(f"{stem}_offsets", 0, index_size)
            previous = 0
            for (current,) in struct.iter_unpack("<Q", offsets):
                if current < previous or current > blob_size:
                    raise ValueError(f"invalid {stem} offset")
                previous = current
            if previous != blob_size:
                raise ValueError(f"invalid {stem} terminal offset")
        records = self.header["counts"].get("addresses")
        if self.components["record_offsets"][1] != (records + 1) * 8:
            raise ValueError("invalid record offset count")
        record_blob_size = self.components["record_blob"][1]
        offsets = self._read_component(
            "record_offsets", 0, self.components["record_offsets"][1]
        )
        previous = 0
        for (current,) in struct.iter_unpack("<Q", offsets):
            if current < previous or current > record_blob_size:
                raise ValueError("invalid record offset")
            previous = current
        if previous != record_blob_size:
            raise ValueError("invalid record terminal offset")

    def context(self, item: int) -> dict[str, Any]:
        payload = self._indexed_payload("contexts", item)
        position = 0
        values = []
        for _ in range(4):
            value, position = decode_text(payload, position)
            values.append(value)
        chain_id, position = decode_uvarint(payload, position)
        group_start, position = decode_uvarint(payload, position)
        group_count, position = decode_uvarint(payload, position)
        if position != len(payload):
            raise ValueError("trailing context bytes")
        return {
            "key": tuple(values),
            "dominant_chain_id": chain_id,
            "group_start": group_start,
            "group_count": group_count,
        }

    def street_group(self, item: int) -> dict[str, Any]:
        payload = self._indexed_payload("street_groups", item)
        context_id, position = decode_uvarint(payload)
        street, position = decode_text(payload, position)
        display, position = decode_text(payload, position)
        record_start, position = decode_uvarint(payload, position)
        record_count, position = decode_uvarint(payload, position)
        if position != len(payload):
            raise ValueError("trailing street bytes")
        return {
            "context_id": context_id,
            "street": street,
            "display_street": display,
            "record_start": record_start,
            "record_count": record_count,
        }

    def source_labels(self, item: int) -> dict[str, str]:
        payload = self._indexed_payload("source_label_sets", item)
        position = 0
        values = []
        for _ in range(5):
            value, position = decode_text(payload, position)
            values.append(value)
        if position != len(payload):
            raise ValueError("trailing source-label bytes")
        return dict(zip(("state", "city", "postal_city", "postcode", "street"), values))

    def chain(self, item: int) -> list[dict[str, str]]:
        payload = self._indexed_payload("chains", item)
        count, position = decode_uvarint(payload)
        reverse_codes = {value: key for key, value in SUBTYPE_CODES.items()}
        result = []
        for _ in range(count):
            if position + 17 > len(payload):
                raise ValueError("truncated division chain")
            subtype = reverse_codes.get(payload[position])
            if subtype is None:
                raise ValueError("unknown division subtype")
            position += 1
            division_id = str(uuid.UUID(bytes=payload[position : position + 16]))
            position += 16
            result.append({"subtype": subtype, "division_id": division_id})
        if position != len(payload):
            raise ValueError("trailing division-chain bytes")
        return result

    def record(self, item: int, dominant_chain_id: int) -> dict[str, Any]:
        count = self.header["counts"]["addresses"]
        if item < 0 or item >= count:
            raise ValueError("invalid record index")
        start = self._offset("record_offsets", item)
        end = self._offset("record_offsets", item + 1)
        payload = self._read_component("record_blob", start, end - start)
        if len(payload) < 24:
            raise ValueError("truncated address record")
        feature_id = str(uuid.UUID(bytes=payload[:16]))
        lon, lat = struct.unpack("<ii", payload[16:24])
        override, position = decode_uvarint(payload, 24)
        label_id, position = decode_uvarint(payload, position)
        number, position = decode_text(payload, position)
        unit, position = decode_text(payload, position)
        if position != len(payload):
            raise ValueError("trailing address-record bytes")
        chain_id = dominant_chain_id if override == 0 else override - 1
        return {
            "feature_id": feature_id,
            "lon": lon / 10_000_000,
            "lat": lat / 10_000_000,
            "number": number,
            "unit": unit,
            "number_norm": normalize(number),
            "unit_norm": normalize(unit),
            "source_labels": self.source_labels(label_id),
            "division_chain": self.chain(chain_id),
        }

    @staticmethod
    def _binary_search(count: int, value: tuple[Any, ...], reader: Any) -> int | None:
        low, high = 0, count
        while low < high:
            middle = (low + high) // 2
            key = reader(middle)
            if key < value:
                low = middle + 1
            else:
                high = middle
        return low if low < count and reader(low) == value else None

    def lookup(
        self,
        *,
        state: str,
        city: str,
        postal_city: str,
        postcode: str,
        street: str,
        number: str,
        unit: str | None = None,
    ) -> list[dict[str, Any]]:
        context_key = tuple(
            normalize(value) for value in (state, city, postal_city, postcode)
        )
        context_id = self._binary_search(
            self.header["counts"]["contexts"],
            context_key,
            lambda item: self.context(item)["key"],
        )
        if context_id is None:
            return []
        context = self.context(context_id)
        street_key = normalize(street)
        relative = self._binary_search(
            context["group_count"],
            (context_id, street_key),
            lambda item: (
                self.street_group(context["group_start"] + item)["context_id"],
                self.street_group(context["group_start"] + item)["street"],
            ),
        )
        if relative is None:
            return []
        group = self.street_group(context["group_start"] + relative)
        number_key = normalize(number)
        unit_key = normalize(unit) if unit is not None else None
        target = (number_key, unit_key or "")
        start = group["record_start"]
        count = group["record_count"]

        def record_key(relative_item: int) -> tuple[str, str]:
            record = self.record(start + relative_item, context["dominant_chain_id"])
            return record["number_norm"], record["unit_norm"]

        low, high = 0, count
        while low < high:
            middle = (low + high) // 2
            key = record_key(middle)
            comparison = key if unit_key is not None else (key[0], "")
            if comparison < target:
                low = middle + 1
            else:
                high = middle
        results = []
        for relative_item in range(low, count):
            record = self.record(start + relative_item, context["dominant_chain_id"])
            if record["number_norm"] != number_key:
                break
            if unit_key is not None and record["unit_norm"] != unit_key:
                if results:
                    break
                continue
            results.append(record)
        return results


def verify_artifact(connection: Any, artifact: Path) -> dict[str, Any]:
    sample = connection.execute("""
        SELECT state_norm, city_norm, postal_city_norm, postcode_norm,
               street_norm, number_norm, unit_norm
        FROM indexed_addresses
        ORDER BY context_id, street_norm, number_norm, unit_norm, feature_id
        LIMIT 1
    """).fetchone()
    if sample is None:
        raise ValueError("cannot verify empty address artifact")
    expected = {
        row[0]
        for row in connection.execute(
            """
            SELECT feature_id FROM indexed_addresses
            WHERE state_norm = ? AND city_norm = ? AND postal_city_norm = ?
              AND postcode_norm = ? AND street_norm = ?
              AND number_norm = ? AND unit_norm = ?
        """,
            sample,
        ).fetchall()
    }
    with AddressArtifact(artifact) as reader:
        actual = {
            row["feature_id"]
            for row in reader.lookup(
                state=sample[0],
                city=sample[1],
                postal_city=sample[2],
                postcode=sample[3],
                street=sample[4],
                number=sample[5],
                unit=sample[6],
            )
        }
    if actual != expected:
        raise ValueError("artifact lookup differs from database oracle")
    return {
        "sample_candidate_count": len(expected),
        "exact_candidate_set": True,
        "strict_reader_validation": True,
    }


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
        "Each record round-trips its source state/city/postal-city/postcode/street",
        "label tuple through a dictionary reference; geometric division links remain",
        "separate derived context.",
        "",
        "The storage result is encouraging: address cardinality is large, but its",
        "structured repetition compresses much better than the POI token index. The",
        "remaining hard problem is parsing and country-specific normalization, not R2",
        "storage.",
        "",
        "## Input and build",
        "",
        f"- Addresses: {rows:,} keyable Massachusetts records",
        f"- Address release: `{report['address_release']}`; SHA-256 `{report['address_source_sha256']}`",
        f"- Division areas: {report['division_area_rows']:,} from Overture release `{report['division_release']}`",
        f"- Division input SHA-256: `{report['division_source_sha256']}`",
        f"- DuckDB: `{report['duckdb_version']}`; {report['database']['threads']} threads; memory limit `{report['database']['memory_limit']}`",
        f"- Producer commit: `{report['producer_commit']}`",
        f"- Division spatial join: {report['database']['join_seconds']:.2f} seconds",
        f"- Total experiment: {report['elapsed_seconds']:.2f} seconds",
        f"- Artifact: {artifact_bytes:,} bytes ({artifact_bytes / rows:.1f} B/address)",
        f"- Artifact SHA-256: `{report['artifact']['sha256']}`",
        f"- Strict reader/oracle verification: {report['artifact']['verification']['exact_candidate_set']}",
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
            "number, unit, a source-label-set reference, and a normally-zero",
            "division-chain override. Context, street, source-label, and chain records are",
            "indexed dictionary blobs. A global per-record offset table makes the",
            "variable-length record block binary-searchable. Source provenance, raw",
            "address levels, and source-file locators are not present in this historical",
            "input and require a measured allowance in a current-release producer.",
            "",
            "## Division linking",
            "",
            f"- Addresses inside at least one selected division area: {coverage['with_any_division']:,} / {coverage['addresses']:,}",
            f"- Distinct per-address division chains: {coverage['distinct_chains']:,}",
            f"- Chain matches context-dominant chain: {coverage['using_context_dominant_chain']:,}",
            f"- Records requiring an override: {coverage['requiring_chain_override']:,}",
            f"- Contexts: {contexts['contexts']:,}; unanimous contexts: {contexts['unanimous_contexts']:,}",
            f"- Ambiguous address/subtype pairs retained in the chain: {ambiguity['ambiguous_pairs']:,} (maximum {ambiguity['max_candidates']})",
            f"- Boundary-covered addresses: {ambiguity['boundary_addresses']:,}",
            f"- Addresses unmatched by any selected division area: {ambiguity['unmatched_addresses']:,}",
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
            "All covered region/county/locality IDs are retained, including multiple IDs",
            "for one subtype. They come from containing division areas; a",
            "country division ID can be stored once in the shard manifest rather than on",
            "every address. Registry membership should be verified before describing any",
            "particular Overture division ID as a GERS ID.",
            "",
            "## Lookup shape",
            "",
            f"- Contexts: {inventory['contexts']:,}",
            f"- Context/street groups: {inventory['street_groups']:,}",
            f"- Distinct source-label sets: {inventory['source_label_sets']:,}",
            f"- Distinct normalized street names: {inventory['distinct_street_names']:,}",
            f"- Records per street group: median {groups['median']:.0f}, p90 {groups['p90']:.0f}, p99 {groups['p99']:.0f}, max {groups['maximum']:,}",
            f"- Median/max encoded record-group bytes: {report['artifact']['shape']['median_record_group_bytes']:,} / {report['artifact']['shape']['maximum_record_group_bytes']:,}",
            "",
            "The serving path is: parse or accept structured country/region/locality/",
            "postcode, resolve a context, binary-search its normalized street range,",
            "then binary-search number and unit through the record-offset table. The",
            "strict local reader verifies an exact candidate set against DuckDB. Prefix",
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
            "The bounded global producer proposal is documented in",
            "[`docs/plans/2026-07-14-global-places-address-processing-design.md`](../docs/plans/2026-07-14-global-places-address-processing-design.md).",
            "",
            "1. Repeat on one current-release country/region extract carrying source filepath",
            "   and row-group locator fields.",
            "2. Preserve raw `address_levels` and measure their dictionary cost separately",
            "   from geometric division-chain IDs.",
            "3. Implement and evaluate a bounded US one-line parser plus structured endpoint",
            "   against independently labelled queries.",
            "4. Measure Worker range reads for exact, prefix, ambiguous, unit, and no-result",
            "   cases before extrapolating a public API.",
            "5. Define political-perspective semantics for the multiple containing",
            "   division memberships that this artifact now preserves.",
            "",
            "## Reproduction",
            "",
            "```bash",
            "python3 scripts/experiment_address_division_index.py exports/US-MA.parquet \\",
            "  exports/US-MA-division-areas-2026-06-17.parquet \\",
            "  --artifact artifacts/addresses-ma-division.oadr \\",
            "  --database artifacts/addresses-ma-division.duckdb --overwrite-database \\",
            "  --address-release unknown-historical --division-release 2026-06-17.0 \\",
            "  --threads 4 --memory-limit 12GB \\",
            "  --json-out benchmarks/address-division-index-report.json \\",
            "  --markdown-out benchmarks/address-division-index-report.md",
            "```",
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
    parser.add_argument("--overwrite-database", action="store_true")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--memory-limit", default="12GB")
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument("--address-release", default="unknown-historical")
    parser.add_argument("--division-release", default="2026-06-17.0")
    parser.add_argument(
        "--producer-commit", default=os.environ.get("GITHUB_SHA", "working-tree")
    )
    args = parser.parse_args()
    if duckdb is None:
        raise SystemExit("experiment_address_division_index.py requires duckdb")

    started = time.monotonic()
    database = args.database or args.artifact.with_suffix(".duckdb")
    database.parent.mkdir(parents=True, exist_ok=True)
    if database.exists():
        if not args.overwrite_database:
            raise SystemExit(
                f"database exists: {database}; pass --overwrite-database to replace it"
            )
        database.unlink()
    connection = duckdb.connect(str(database))
    database_evidence = prepare_database(
        connection,
        args.addresses,
        args.division_areas,
        threads=args.threads,
        memory_limit=args.memory_limit,
    )
    metrics = collect_metrics(connection)
    component_sizes, artifact_shape = build_artifact(connection, args.artifact)
    verification = verify_artifact(connection, args.artifact)
    division_area_rows = connection.execute(
        "SELECT count(*) FROM division_areas"
    ).fetchone()[0]
    connection.close()

    report = {
        "date": "2026-07-14",
        "producer_commit": args.producer_commit,
        "addresses": str(args.addresses),
        "address_release": args.address_release,
        "address_source_sha256": sha256_file(args.addresses),
        "division_areas": str(args.division_areas),
        "division_release": args.division_release,
        "division_source_sha256": sha256_file(args.division_areas),
        "division_area_rows": division_area_rows,
        "duckdb_version": duckdb.__version__,
        "database": database_evidence,
        "metrics": metrics,
        "artifact": {
            "path": str(args.artifact),
            "bytes": args.artifact.stat().st_size,
            "sha256": sha256_file(args.artifact),
            "components": component_sizes,
            "shape": artifact_shape,
            "verification": verification,
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
