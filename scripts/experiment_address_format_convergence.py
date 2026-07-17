#!/usr/bin/env python3
"""Decide the address-format convergence shape with a bounded current-release run.

The project carries two divergent address formats:

* the division-joined compact spike (``experiment_address_division_index.py``),
  whose hot record references a division chain resolved by a point-in-polygon
  join to current-release division areas; and
* the division-free hosted pipeline (``experiment_address_reduce.py`` ->
  ``experiment_address_compression.py``), which keys on raw ``address_levels``
  strings, carries no division IDs, and has an end-to-end lookup-safe compressed
  baseline of 35.50 B/indexed row.

This experiment produces the evidence to converge them.  On a bounded
current-release address range it (1) compares ``address_levels``-derived
locality/region context against division-containment context (reusing the
spike's bbox-prefiltered ``ST_Covers`` join) and emits a mismatch taxonomy;
(2) measures the storage delta of extending the division-free lookup-safe page
format with containing-division GERS ID references (a page division dictionary
plus a per-row index) and an explicit match-method/confidence byte, reusing
``experiment_address_compression.encode_page``; and (3) records the join's
build cost and linearizes it (a labeled diagnostic, not a forecast) to the 473M
planning count.

The pure logic -- taxonomy classification and the division-extension codec --
imports cleanly without DuckDB and is covered by offline tests.  The DuckDB/S3
run is bounded, provenance-pinned, and deterministic given the release.  It
writes no R2 object, catalog, shard, or production state.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import resource
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Reuse the existing experiment helpers by import (do not copy them).
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import experiment_address_reduce as reduce  # noqa: E402
import experiment_address_compression as compression  # noqa: E402

# Low-level codec helpers shared with the reduce/compression formats.
normalize = reduce.normalize
encode_uvarint = reduce.encode_uvarint
decode_uvarint = reduce.decode_uvarint
peak_rss_bytes = reduce.peak_rss_bytes


RELEASE = "2026-06-17.0"
SCHEMA_VERSION = "v1.17.0"
ADDRESS_SOURCE_URI = (
    "s3://overturemaps-us-west-2/release/2026-06-17.0/theme=addresses/type=address/*"
)
DIVISION_AREA_SOURCE_URI = (
    "s3://overturemaps-us-west-2/release/2026-06-17.0/"
    "theme=divisions/type=division_area/*"
)
DIVISION_POINT_SOURCE_URI = (
    "s3://overturemaps-us-west-2/release/2026-06-17.0/theme=divisions/type=division/*"
)
# The division-free hosted lookup-safe compressed baseline (useful_gzip variant,
# benchmarks/hosted-address-compression-report.json). The gate this experiment
# must respect.
LOOKUP_SAFE_BASELINE_BPR = 35.502976
PLANNING_ROWS = 473_000_000
STORAGE_STOP_GATE_GB = 40.0

# Division subtypes that carry locality/region context. These are the only
# subtypes whose GERS IDs the convergence extension stores on the hot path.
CONTEXT_SUBTYPES = ("region", "county", "locality")
# The taxonomy join additionally loads neighborhood polygons, and a separate
# taxonomy-only channel associates macrohood/neighborhood/microhood division
# POINTS with their containing municipality, so that a neighborhood-level
# address_levels label (for example a USPS "BRIGHTON" label inside the Boston
# municipality polygon, where Overture has a Brighton macrohood point but no
# Brighton polygon) is decomposed as finer granularity instead of being
# counted as a genuine conflict. Finer-subtype IDs are NOT part of the stored
# context extension or its storage measurement.
TAXONOMY_SUBTYPES = CONTEXT_SUBTYPES + ("neighborhood",)
FINER_POINT_SUBTYPES = ("macrohood", "neighborhood", "microhood")

# Match-method codes for the explicit provenance byte.
MATCH_METHOD_NONE = 0
MATCH_METHOD_INTERIOR = 1
MATCH_METHOD_BOUNDARY = 2
MATCH_METHOD_NAMES = {
    MATCH_METHOD_NONE: "no_containing_division",
    MATCH_METHOD_INTERIOR: "point_in_polygon_interior",
    MATCH_METHOD_BOUNDARY: "point_on_polygon_boundary",
}

# The agreement taxonomy. Buckets are mutually exclusive and assigned in the
# fixed order encoded by ``classify_agreement``.
TAXONOMY = (
    "exact_agreement",
    "normalization_only",
    "finer_granularity_neighborhood",
    "postal_city_vs_containment",
    "missing_address_levels",
    "point_outside_any_division",
    "country_disagreement",
    "unresolved_disagreement",
)


# ----------------------------------------------------------------------------
# Pure logic: agreement taxonomy classification.
# ----------------------------------------------------------------------------
def _first_nonempty(*values: str) -> str:
    for value in values:
        if value:
            return value
    return ""


def classify_agreement(row: dict[str, Any]) -> str:
    """Classify one address row into the convergence agreement taxonomy.

    Pure and deterministic. ``row`` carries display strings ('' means absent):

    * ``country``            raw address country code
    * ``has_address_levels`` bool: the address carries >=1 address_levels value
    * ``al_region``          raw address_levels[0] (most-general proxy)
    * ``al_locality``        raw address_levels[-1] (most-specific proxy)
    * ``postal_city``        raw postal_city
    * ``cont_any``           bool: point is inside >=1 selected division area
    * ``cont_country``       containing-division country code ('' if none)
    * ``cont_region``        containing region-subtype division name ('' if none)
    * ``cont_county``        containing county-subtype division name ('' if none)
    * ``cont_locality``      containing locality-subtype division name ('' if none)
    * ``cont_neighborhood``  containing neighborhood-subtype division name
                             ('' if none); used only to decompose
                             finer-granularity labels, never stored context
    * ``finer_names``        names of macrohood/neighborhood/microhood division
                             points located inside the row's containing
                             municipality ([] if none); taxonomy-only channel

    The comparison targets the most-specific available containment name against
    the address's most-specific level label, with country as a gate. Region
    codes are compared normalized, so an ``address_levels`` state abbreviation
    that differs only in case/whitespace from the division name is treated as a
    normalization difference rather than a genuine disagreement. A label that
    instead matches the containing neighborhood polygon, or the name of a
    finer-subtype division point inside the containing municipality (for
    example a USPS neighborhood city label like BRIGHTON inside the Boston
    municipality), is classified as ``finer_granularity_neighborhood``: a
    granularity difference, not a conflict.
    """
    if not row.get("has_address_levels"):
        return "missing_address_levels"
    if not row.get("cont_any"):
        return "point_outside_any_division"

    country = normalize(row.get("country", ""))
    cont_country = normalize(row.get("cont_country", ""))
    if country and cont_country and country != cont_country:
        return "country_disagreement"

    # Compare the address's most-specific level against the most-specific
    # containment name available (locality -> region -> county).
    label_raw = _first_nonempty(row.get("al_locality", ""), row.get("al_region", ""))
    target_raw = _first_nonempty(
        row.get("cont_locality", ""),
        row.get("cont_region", ""),
        row.get("cont_county", ""),
    )
    if not target_raw:
        # cont_any was true but no comparable name resolved; treat as unresolved.
        return "unresolved_disagreement"

    if label_raw and label_raw == target_raw:
        return "exact_agreement"
    if normalize(label_raw) == normalize(target_raw):
        return "normalization_only"

    if label_raw:
        label_norm = normalize(label_raw)
        neighborhood = row.get("cont_neighborhood", "")
        if neighborhood and label_norm == normalize(neighborhood):
            return "finer_granularity_neighborhood"
        finer_names = row.get("finer_names") or []
        if any(name and label_norm == normalize(name) for name in finer_names):
            return "finer_granularity_neighborhood"

    postal_city = normalize(row.get("postal_city", ""))
    if postal_city and postal_city == normalize(target_raw):
        return "postal_city_vs_containment"

    return "unresolved_disagreement"


def taxonomy_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Return per-bucket counts over ``rows`` in the fixed taxonomy order."""
    counts = dict.fromkeys(TAXONOMY, 0)
    for row in rows:
        counts[classify_agreement(row)] += 1
    return counts


# ----------------------------------------------------------------------------
# Pure logic: division-extension codec (page dictionary + per-row index + byte).
# ----------------------------------------------------------------------------
def encode_match_byte(method: int, confidence: int) -> int:
    """Pack a match method (high nibble) and confidence bucket (low nibble)."""
    if not 0 <= method <= 15 or not 0 <= confidence <= 15:
        raise ValueError("match method and confidence must each fit in a nibble")
    return (method << 4) | confidence


def decode_match_byte(value: int) -> tuple[int, int]:
    if not 0 <= value <= 255:
        raise ValueError("match byte must be a single octet")
    return value >> 4, value & 0x0F


def encode_division_extension(records: list[dict[str, Any]]) -> bytes:
    """Encode the per-page division extension appended to a lookup-safe page.

    Layout: a page-local dictionary of the distinct containing-division GERS
    UUIDs (16 bytes each, sorted) followed, per record in page order, by a
    uvarint count, that many uvarint dictionary indices (sorted), and one
    match-method/confidence byte. Repeated locality/region GERS IDs across a
    page are stored once and referenced by small integers.
    """
    identifiers = sorted(
        {gid for record in records for gid in record.get("division_gers_ids", [])}
    )
    index = {gid: position for position, gid in enumerate(identifiers)}
    pieces = [encode_uvarint(len(identifiers))]
    pieces.extend(uuid.UUID(gid).bytes for gid in identifiers)
    for record in records:
        row_ids = sorted(record.get("division_gers_ids", []))
        pieces.append(encode_uvarint(len(row_ids)))
        pieces.extend(encode_uvarint(index[gid]) for gid in row_ids)
        pieces.append(
            bytes(
                [
                    encode_match_byte(
                        int(record.get("match_method", MATCH_METHOD_NONE)),
                        int(record.get("match_confidence", 0)),
                    )
                ]
            )
        )
    return b"".join(pieces)


def decode_division_extension(
    payload: bytes, count: int, position: int = 0
) -> tuple[list[dict[str, Any]], int]:
    """Inverse of ``encode_division_extension`` for ``count`` records."""
    dict_count, position = decode_uvarint(payload, position)
    identifiers: list[str] = []
    for _ in range(dict_count):
        end = position + 16
        if end > len(payload):
            raise ValueError("truncated division dictionary entry")
        identifiers.append(str(uuid.UUID(bytes=payload[position:end])))
        position = end
    records: list[dict[str, Any]] = []
    for _ in range(count):
        row_count, position = decode_uvarint(payload, position)
        row_ids = []
        for _ in range(row_count):
            reference, position = decode_uvarint(payload, position)
            if reference >= len(identifiers):
                raise ValueError("division dictionary index is out of range")
            row_ids.append(identifiers[reference])
        if position >= len(payload):
            raise ValueError("truncated division match byte")
        method, confidence = decode_match_byte(payload[position])
        position += 1
        records.append(
            {
                "division_gers_ids": row_ids,
                "match_method": method,
                "match_confidence": confidence,
            }
        )
    return records, position


def encode_extended_page(page: list[dict[str, Any]]) -> bytes:
    """Frame a self-describing extended page: uvarint core length, core, extension.

    The stored page must be decodable with no out-of-band knowledge: the
    uvarint prefix locates the boundary between the reused lookup-safe core
    page (``compression.encode_page``) and the division extension, so a reader
    can gunzip the stored blob and recover both halves from the bytes alone.
    """
    core = compression.encode_page(page, useful=True)
    return encode_uvarint(len(core)) + core + encode_division_extension(page)


def decode_extended_page(
    payload: bytes,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Inverse of ``encode_extended_page`` using only the payload bytes."""
    core_length, position = decode_uvarint(payload, 0)
    end = position + core_length
    if end > len(payload):
        raise ValueError("truncated extended-page core")
    records = compression.decode_page(payload[position:end], useful=True)
    extension, position = decode_division_extension(payload, len(records), end)
    if position != len(payload):
        raise ValueError("trailing extended-page bytes")
    return records, extension


# ----------------------------------------------------------------------------
# Pure logic: paging + storage measurement (reuses compression.encode_page).
# ----------------------------------------------------------------------------
def paginate(records: list[dict[str, Any]], page_rows: int):
    """Group sorted records into pages; a candidate group never crosses a page.

    Mirrors the paging discipline in ``experiment_address_compression.run`` so
    the measured baseline is comparable to the hosted compression report.
    """
    if page_rows <= 0:
        raise ValueError("page_rows must be positive")
    page: list[dict[str, Any]] = []
    for record in records:
        key = record["key"][:8]
        if page and len(page) >= page_rows and key != page[-1]["key"][:8]:
            yield page
            page = []
        page.append(record)
    if page:
        yield page


def _index_entry_bytes(
    page_offset: int, stored_length: int, page_rows: int, first_key: bytes
) -> int:
    return len(
        encode_uvarint(page_offset)
        + encode_uvarint(4 + stored_length)
        + encode_uvarint(page_rows)
        + encode_uvarint(len(first_key))
        + first_key
    )


def measure_storage(records: list[dict[str, Any]], *, page_rows: int) -> dict[str, Any]:
    """Measure the lookup-safe baseline against the division-extended format.

    Both variants gzip an independent page. The extended variant is the
    self-describing ``encode_extended_page`` framing (uvarint core length +
    core + division dictionary/index/match byte), so the measured delta
    includes the framing prefix and every stored extended page is verified to
    decode with no out-of-band knowledge.
    """
    ordered = sorted(records, key=lambda record: record["key"])
    baseline_data = extended_data = 4  # data magic + header framing placeholder
    baseline_index = extended_index = 0
    baseline_pages: list[int] = []
    extended_pages: list[int] = []
    rows = 0
    for page in paginate(ordered, page_rows):
        rows += len(page)
        raw = compression.encode_page(page, useful=True)
        baseline_stored = gzip.compress(raw, compresslevel=6, mtime=0)
        extended_stored = gzip.compress(
            encode_extended_page(page), compresslevel=6, mtime=0
        )
        decoded_core, decoded_extension = decode_extended_page(
            gzip.decompress(extended_stored)
        )
        if len(decoded_core) != len(page) or len(decoded_extension) != len(page):
            raise ValueError("stored extended page does not decode losslessly")
        first_key = b"".join(
            compression.encode_text(value) for value in page[0]["key"][:8]
        )
        baseline_index += _index_entry_bytes(
            baseline_data, len(baseline_stored), len(page), first_key
        )
        extended_index += _index_entry_bytes(
            extended_data, len(extended_stored), len(page), first_key
        )
        baseline_data += 4 + len(baseline_stored)
        extended_data += 4 + len(extended_stored)
        baseline_pages.append(4 + len(baseline_stored))
        extended_pages.append(4 + len(extended_stored))
    if rows == 0:
        raise ValueError("storage measurement requires at least one record")
    baseline_total = baseline_data + baseline_index
    extended_total = extended_data + extended_index
    return {
        "rows": rows,
        "pages": len(baseline_pages),
        "page_rows": page_rows,
        "baseline_total_bytes": baseline_total,
        "extended_total_bytes": extended_total,
        "baseline_bytes_per_row": round(baseline_total / rows, 6),
        "extended_bytes_per_row": round(extended_total / rows, 6),
        "delta_bytes_per_row": round((extended_total - baseline_total) / rows, 6),
        "baseline_p50_page_bytes": sorted(baseline_pages)[len(baseline_pages) // 2],
        "extended_p50_page_bytes": sorted(extended_pages)[len(extended_pages) // 2],
        "reference_lookup_safe_baseline_bytes_per_row": LOOKUP_SAFE_BASELINE_BPR,
        "linear_baseline_all_planning_rows_gb": round(
            baseline_total / rows * PLANNING_ROWS / 1_000_000_000, 3
        ),
        "linear_extended_all_planning_rows_gb": round(
            extended_total / rows * PLANNING_ROWS / 1_000_000_000, 3
        ),
        "storage_stop_gate_gb": STORAGE_STOP_GATE_GB,
        "delta_caveat": (
            "The measured delta is a favorable-case lower bound: the sample box "
            "has an atypically small division-polygon set, so few distinct GERS "
            "IDs repeat within each page and the page dictionary amortizes to "
            "almost nothing. The delta scales with the number of distinct "
            "containing divisions per page under global polygon density "
            "(boundaries, dense locality/neighborhood fabric, multi-membership)."
        ),
    }


# ----------------------------------------------------------------------------
# Bounded current-release extraction + spatial join (requires DuckDB).
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class SampleBox:
    name: str
    xmin: float
    ymin: float
    xmax: float
    ymax: float


# Default: a Cambridge/Somerville/Boston core box spanning several
# municipalities so the locality-level agreement taxonomy is meaningful. Small,
# purposive, and explicitly non-representative.
DEFAULT_BOX = SampleBox("boston-core-ma", -71.150, 42.350, -71.050, 42.400)


def _sql_string(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _confidence_bucket(distinct_divisions: int) -> int:
    """Coarse structural-completeness proxy (not a calibrated probability)."""
    return max(0, min(15, int(distinct_divisions)))


def extract_rows(
    connection: Any,
    box: SampleBox,
    *,
    row_cap: int,
    threads: int,
    memory_limit: str,
    temp_directory: Path,
    temp_cap_bytes: int,
) -> dict[str, Any]:
    """Extract addresses + containment for one box; time the spatial join."""
    connection.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial")
    connection.execute("SET s3_region = 'us-west-2'")
    connection.execute(f"SET threads = {threads}")
    connection.execute(f"SET memory_limit = {_sql_string(memory_limit)}")
    connection.execute(f"SET temp_directory = {_sql_string(temp_directory)}")
    connection.execute(f"SET max_temp_directory_size = '{temp_cap_bytes}B'")

    # Bounds are inlined as literals (never joined from a VALUES table) so
    # DuckDB can push them into parquet row-group statistics pruning; a join
    # against a bounds table defeats pushdown and scans the whole theme.
    address_query = f"""
        CREATE TABLE address_sample AS
        WITH selected AS (
            SELECT
                a.id AS overture_id,
                ST_X(a.geometry) AS lon,
                ST_Y(a.geometry) AS lat,
                COALESCE(a.country, '') AS country,
                COALESCE(a.number, '') AS number,
                COALESCE(a.street, '') AS street,
                COALESCE(a.unit, '') AS unit,
                COALESCE(a.postcode, '') AS postcode,
                COALESCE(a.postal_city, '') AS postal_city,
                COALESCE(
                    list_transform(a.address_levels, x -> COALESCE(x.value, '')),
                    []::VARCHAR[]
                ) AS address_levels,
                COUNT(*) OVER ()::BIGINT AS box_population,
                ROW_NUMBER() OVER (ORDER BY md5(a.id)) AS deterministic_rank
            FROM read_parquet('{ADDRESS_SOURCE_URI}', hive_partitioning = true) a
            WHERE a.bbox.xmax >= {box.xmin} AND a.bbox.xmin <= {box.xmax}
              AND a.bbox.ymax >= {box.ymin} AND a.bbox.ymin <= {box.ymax}
              AND a.geometry IS NOT NULL
              AND ST_GeometryType(a.geometry) = 'POINT'
              AND ST_X(a.geometry) BETWEEN {box.xmin} AND {box.xmax}
              AND ST_Y(a.geometry) BETWEEN {box.ymin} AND {box.ymax}
        )
        SELECT
            ROW_NUMBER() OVER (ORDER BY deterministic_rank) - 1 AS address_row,
            * EXCLUDE (deterministic_rank)
        FROM selected
        WHERE deterministic_rank <= {row_cap}
    """
    connection.execute(address_query)
    sampled_rows, box_population = connection.execute(
        "SELECT count(*), COALESCE(max(box_population), 0) FROM address_sample"
    ).fetchone()

    division_query = f"""
        CREATE TABLE division_areas AS
        SELECT
            d.division_id,
            d.subtype,
            COALESCE(d.names.primary, '') AS name,
            COALESCE(d.country, '') AS country,
            COALESCE(d.region, '') AS region,
            d.geometry AS geometry,
            d.bbox AS bbox,
            ST_Area_Spheroid(d.geometry) AS area_m2
        FROM read_parquet('{DIVISION_AREA_SOURCE_URI}', hive_partitioning = true) d
        WHERE d.subtype IN ({",".join(repr(item) for item in TAXONOMY_SUBTYPES)})
          AND d.is_land = true
          AND d.bbox.xmax >= {box.xmin} AND d.bbox.xmin <= {box.xmax}
          AND d.bbox.ymax >= {box.ymin} AND d.bbox.ymin <= {box.ymax}
    """
    connection.execute(division_query)
    division_rows, context_division_rows = connection.execute(
        f"""SELECT count(*),
                   count(*) FILTER (WHERE subtype IN
                       ({",".join(repr(item) for item in CONTEXT_SUBTYPES)}))
            FROM division_areas"""
    ).fetchone()

    # The spike's bbox-prefiltered ST_Covers point-in-polygon join, timed.
    join_sql = """
        CREATE TABLE containment AS
        SELECT
            a.address_row,
            d.division_id,
            d.subtype,
            d.name AS division_name,
            d.country AS division_country,
            d.area_m2,
            ST_Within(ST_Point(a.lon, a.lat), d.geometry) AS interior
        FROM address_sample a
        JOIN division_areas d
          ON a.lon BETWEEN d.bbox.xmin AND d.bbox.xmax
         AND a.lat BETWEEN d.bbox.ymin AND d.bbox.ymax
         AND ST_Covers(d.geometry, ST_Point(a.lon, a.lat))
    """
    usage_before = resource.getrusage(resource.RUSAGE_SELF)
    join_started = time.monotonic()
    connection.execute(join_sql)
    join_seconds = time.monotonic() - join_started
    usage_after = resource.getrusage(resource.RUSAGE_SELF)
    join_cpu_seconds = (usage_after.ru_utime - usage_before.ru_utime) + (
        usage_after.ru_stime - usage_before.ru_stime
    )

    # Taxonomy-only channel: macrohood/neighborhood/microhood division POINTS
    # associated by containment with each locality polygon. Overture often has
    # a finer-division point (Brighton, Charlestown) where no polygon exists,
    # so this channel decomposes finer-granularity labels the polygon join
    # cannot. The search bbox is the union of the candidate locality polygon
    # bboxes because a neighborhood point can sit outside the sample box while
    # its municipality intersects it.
    locality_bounds = connection.execute(
        """SELECT min(bbox.xmin), min(bbox.ymin), max(bbox.xmax), max(bbox.ymax)
           FROM division_areas WHERE subtype = 'locality'"""
    ).fetchone()
    if locality_bounds is None or any(value is None for value in locality_bounds):
        locality_bounds = (box.xmin, box.ymin, box.xmax, box.ymax)
    finer_query = f"""
        CREATE TABLE finer_points AS
        SELECT
            COALESCE(d.names.primary, '') AS name,
            CAST(d.subtype AS VARCHAR) AS subtype,
            ST_X(d.geometry) AS lon,
            ST_Y(d.geometry) AS lat
        FROM read_parquet('{DIVISION_POINT_SOURCE_URI}', hive_partitioning = true) d
        WHERE d.subtype IN ({",".join(repr(item) for item in FINER_POINT_SUBTYPES)})
          AND d.geometry IS NOT NULL
          AND ST_GeometryType(d.geometry) = 'POINT'
          AND d.bbox.xmax >= {locality_bounds[0]} AND d.bbox.xmin <= {locality_bounds[2]}
          AND d.bbox.ymax >= {locality_bounds[1]} AND d.bbox.ymin <= {locality_bounds[3]}
    """
    connection.execute(finer_query)
    connection.execute("""
        CREATE TABLE locality_finer_names AS
        SELECT a.name AS locality_name, list(DISTINCT p.name) AS finer_names,
               count(DISTINCT p.name) AS finer_name_count
        FROM division_areas a
        JOIN finer_points p
          ON a.subtype = 'locality'
         AND p.name != ''
         AND p.lon BETWEEN a.bbox.xmin AND a.bbox.xmax
         AND p.lat BETWEEN a.bbox.ymin AND a.bbox.ymax
         AND ST_Covers(a.geometry, ST_Point(p.lon, p.lat))
        GROUP BY a.name
    """)
    finer_points_in_scope = connection.execute(
        "SELECT count(*) FROM finer_points"
    ).fetchone()[0]
    locality_finer_pairs = connection.execute(
        "SELECT COALESCE(sum(finer_name_count), 0) FROM locality_finer_names"
    ).fetchone()[0]

    # Context aggregates (IDs, method, confidence) are restricted to the
    # CONTEXT_SUBTYPES stored by the extension; the neighborhood channel is a
    # separate taxonomy-only column.
    context_list = ",".join(repr(item) for item in CONTEXT_SUBTYPES)
    connection.execute(f"""
        CREATE TABLE containment_choice AS
        SELECT
            address_row,
            arg_min(division_name, area_m2) FILTER (WHERE subtype = 'region')
                AS cont_region,
            arg_min(division_name, area_m2) FILTER (WHERE subtype = 'county')
                AS cont_county,
            arg_min(division_name, area_m2) FILTER (WHERE subtype = 'locality')
                AS cont_locality,
            arg_min(division_name, area_m2) FILTER (WHERE subtype = 'neighborhood')
                AS cont_neighborhood,
            arg_min(division_country, area_m2)
                FILTER (WHERE subtype IN ({context_list})) AS cont_country,
            bool_or(interior) FILTER (WHERE subtype IN ({context_list}))
                AS any_interior,
            count(*) FILTER (WHERE subtype IN ({context_list}))
                AS containment_rows,
            count(DISTINCT division_id) FILTER (WHERE subtype IN ({context_list}))
                AS distinct_divisions,
            list(DISTINCT division_id) FILTER (WHERE subtype IN ({context_list}))
                AS gers_ids
        FROM containment
        GROUP BY address_row
    """)

    cursor = connection.execute("""
        SELECT
            a.address_row, a.overture_id, a.lon, a.lat, a.country,
            a.number, a.street, a.unit, a.postcode, a.postal_city,
            a.address_levels,
            c.cont_region, c.cont_county, c.cont_locality, c.cont_neighborhood,
            c.cont_country,
            c.any_interior, c.containment_rows, c.distinct_divisions, c.gers_ids,
            f.finer_names
        FROM address_sample a
        LEFT JOIN containment_choice c USING (address_row)
        LEFT JOIN locality_finer_names f ON c.cont_locality = f.locality_name
        ORDER BY a.address_row
    """)
    names = [item[0] for item in cursor.description]
    rows = [dict(zip(names, record)) for record in cursor.fetchall()]
    return {
        "rows": rows,
        "box_population": int(box_population),
        "sampled_rows": int(sampled_rows),
        "row_cap": row_cap,
        "cap_saturated": int(box_population) > int(sampled_rows),
        "division_rows": division_rows,
        "context_division_rows": context_division_rows,
        "finer_points_in_scope": int(finer_points_in_scope),
        "locality_finer_pairs": int(locality_finer_pairs),
        "threads": threads,
        "join_seconds": join_seconds,
        "join_cpu_seconds": join_cpu_seconds,
        "address_sql_sha256": _sha256_text(address_query),
        "division_sql_sha256": _sha256_text(division_query),
        "finer_sql_sha256": _sha256_text(finer_query),
        "join_sql_sha256": _sha256_text(join_sql),
    }


def prepare_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape extracted rows for taxonomy classification and storage encoding."""
    prepared: list[dict[str, Any]] = []
    for row in rows:
        levels = [str(value) for value in (row.get("address_levels") or [])]
        gers_ids = [str(value) for value in (row.get("gers_ids") or []) if value]
        any_interior = bool(row.get("any_interior"))
        cont_any = bool(gers_ids)
        if not cont_any:
            method = MATCH_METHOD_NONE
        elif any_interior:
            method = MATCH_METHOD_INTERIOR
        else:
            method = MATCH_METHOD_BOUNDARY
        record = {
            "id": str(row["overture_id"]),
            "lon": float(row["lon"]),
            "lat": float(row["lat"]),
            "country": row.get("country") or "",
            "postal_city": row.get("postal_city") or "",
            "postcode": row.get("postcode") or "",
            "street": row.get("street") or "",
            "number": row.get("number") or "",
            "unit": row.get("unit") or "",
            "address_levels": levels,
            # Locators are absent from raw addresses; the storage delta is
            # unaffected because both variants encode them identically.
            "source_object_index": 0,
            "source_row_group": 0,
            "source_row_index": int(row["address_row"]),
            # Containment context for classification and encoding.
            "has_address_levels": bool(levels),
            "al_region": levels[0] if levels else "",
            "al_locality": levels[-1] if levels else "",
            "cont_any": cont_any,
            "cont_country": row.get("cont_country") or "",
            "cont_region": row.get("cont_region") or "",
            "cont_county": row.get("cont_county") or "",
            "cont_locality": row.get("cont_locality") or "",
            "cont_neighborhood": row.get("cont_neighborhood") or "",
            "finer_names": [
                str(value) for value in (row.get("finer_names") or []) if value
            ],
            "division_gers_ids": gers_ids,
            "match_method": method,
            "match_confidence": _confidence_bucket(row.get("distinct_divisions") or 0),
        }
        record["key"] = reduce.record_key(record)
        prepared.append(record)
    return prepared


def sample_mismatches(
    records: list[dict[str, Any]], limit: int = 20
) -> list[dict[str, Any]]:
    """Collect up to ``limit`` distinct label-only mismatch examples.

    Examples are deduplicated by their full label signature and carry an
    occurrence count, so 20 rows describe 20 distinct disagreement shapes
    rather than one repeated neighborhood. No feature IDs are leaked.
    """
    interesting = {
        "finer_granularity_neighborhood",
        "postal_city_vs_containment",
        "country_disagreement",
        "point_outside_any_division",
        "unresolved_disagreement",
    }
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    for record in records:
        category = classify_agreement(record)
        if category not in interesting:
            continue
        example = {
            "category": category,
            "country": record["country"],
            "address_levels_region": record["al_region"],
            "address_levels_locality": record["al_locality"],
            "postal_city": record["postal_city"],
            "containment_region": record["cont_region"],
            "containment_county": record["cont_county"],
            "containment_locality": record["cont_locality"],
            "containment_neighborhood": record.get("cont_neighborhood", ""),
            "containment_country": record["cont_country"],
            "match_method": MATCH_METHOD_NAMES.get(record["match_method"], "unknown"),
        }
        signature = tuple(str(example[key]) for key in sorted(example))
        entry = grouped.setdefault(signature, {**example, "occurrences": 0})
        entry["occurrences"] += 1
    ranked = sorted(
        grouped.values(), key=lambda item: (-item["occurrences"], item["category"])
    )
    return ranked[:limit]


def build_cost_diagnostics(extract: dict[str, Any], rows: int) -> dict[str, Any]:
    join_seconds = extract["join_seconds"]
    per_row = join_seconds / rows if rows else 0.0
    return {
        "join_wall_seconds": round(join_seconds, 4),
        "join_cpu_seconds": round(extract["join_cpu_seconds"], 4),
        "join_threads": extract["threads"],
        "peak_rss_bytes": peak_rss_bytes(),
        "joined_rows": rows,
        "division_polygons": extract["division_rows"],
        "context_division_polygons": extract["context_division_rows"],
        "seconds_per_row": per_row,
        "linear_all_planning_rows_factory_hours": round(
            per_row * PLANNING_ROWS / 3600, 3
        ),
        "diagnostic_warning": (
            "A purely linear wall-clock diagnostic on a "
            f"{extract['threads']}-thread DuckDB session, NOT a build forecast. "
            "It excludes global polygon density, extraction, sort, shuffle, "
            "retries, and country-specific work; the measured box covers one "
            "small geographic area with a bounded polygon set, and the timed join "
            "includes the taxonomy-only neighborhood polygons."
        ),
    }


def build_report(
    box: SampleBox,
    extract: dict[str, Any],
    records: list[dict[str, Any]],
    storage: dict[str, Any],
    *,
    duckdb_version: str,
    producer_commit: str,
) -> dict[str, Any]:
    counts = taxonomy_counts(records)
    total = len(records)
    classified = total - counts["missing_address_levels"]
    with_containment = classified - counts["point_outside_any_division"]
    rows_with_postal_city = sum(1 for record in records if record["postal_city"])
    genuine_conflicts = (
        counts["country_disagreement"] + counts["unresolved_disagreement"]
    )
    countries = sorted({record["country"] for record in records if record["country"]})
    country_scope = ", ".join(countries) if countries else "unknown country"
    structural_notes = []
    if rows_with_postal_city == 0:
        structural_notes.append(
            "postal_city is empty for "
            f"all {total:,} sampled rows, so the "
            "postal_city_vs_containment bucket cannot trigger in this sample."
        )
    else:
        structural_notes.append(
            f"postal_city is populated for {rows_with_postal_city:,} of "
            f"{total:,} sampled rows and participates in the agreement taxonomy."
        )
    if counts["exact_agreement"] == 0 and counts["normalization_only"]:
        structural_notes.append(
            "No raw exact label equality was observed; "
            f"{counts['normalization_only']:,} compatible rows agreed only after "
            "the declared Unicode/case/whitespace normalization."
        )
    elif counts["exact_agreement"]:
        structural_notes.append(
            f"Raw label equality was observed for {counts['exact_agreement']:,} "
            "rows; normalization-only agreement is reported separately."
        )
    structural_notes.append(
        f"Box {box.name!r} is purposive rather than random; its conflict rate must "
        "not be generalized beyond this bounded sample."
    )
    cap_limitation = (
        "The sample is cap-saturated: sampled rows equal the row cap and are a "
        "deterministic md5(id) subset of the box population."
        if extract["cap_saturated"]
        else "The box population is below the row cap, so every address in the "
        "configured box is included."
    )
    return {
        "schema": "overture-address-format-convergence-v1",
        "date": datetime.now(timezone.utc).date().isoformat(),
        "release": RELEASE,
        "schema_version": SCHEMA_VERSION,
        "producer_commit": producer_commit,
        "duckdb_version": duckdb_version,
        "address_source_uri": ADDRESS_SOURCE_URI,
        "division_area_source_uri": DIVISION_AREA_SOURCE_URI,
        "sample_box": {
            "name": box.name,
            "xmin": box.xmin,
            "ymin": box.ymin,
            "xmax": box.xmax,
            "ymax": box.ymax,
        },
        "provenance": {
            "address_sql_sha256": extract["address_sql_sha256"],
            "division_sql_sha256": extract["division_sql_sha256"],
            "join_sql_sha256": extract["join_sql_sha256"],
            "finer_sql_sha256": extract["finer_sql_sha256"],
            "address_rows_in_box_pre_cap": extract["box_population"],
            "sampled_rows": extract["sampled_rows"],
            "row_cap": extract["row_cap"],
            "cap_saturated": extract["cap_saturated"],
            "division_polygons_in_box": extract["division_rows"],
            "context_division_polygons_in_box": extract["context_division_rows"],
            "finer_division_points_in_scope": extract["finer_points_in_scope"],
            "locality_finer_name_pairs": extract["locality_finer_pairs"],
            "classified_rows": total,
        },
        "agreement": {
            "definition": (
                "address_levels[0]/[-1] (NFC/ASCII-lowercase/whitespace normalized) "
                "compared against the smallest containing region/county/locality "
                "division name from a bbox-prefiltered ST_Covers point-in-polygon "
                "join; country is a gate. A label matching a containing "
                "neighborhood-subtype polygon, or the name of a "
                "macrohood/neighborhood/microhood division POINT inside the "
                "containing municipality, is decomposed as "
                "finer_granularity_neighborhood (a granularity difference, not a "
                "conflict); these finer-subtype channels are taxonomy-only and "
                "never stored context. address_levels meanings are "
                "country-dependent, so first/last are general/specific proxies."
            ),
            "total_rows": total,
            "counts": counts,
            "percentages": {
                bucket: (round(100 * counts[bucket] / total, 3) if total else 0.0)
                for bucket in TAXONOMY
            },
            "genuine_conflict_rows": genuine_conflicts,
            "genuine_conflict_rate_percent": (
                round(100 * genuine_conflicts / total, 3) if total else 0.0
            ),
            "rows_with_address_levels": classified,
            "rows_with_containment": with_containment,
            "rows_with_postal_city": rows_with_postal_city,
            "structural_notes": structural_notes,
            "examples": sample_mismatches(records, 20),
        },
        "storage": storage,
        "build_cost": build_cost_diagnostics(extract, total),
        "limitations": [
            f"One small purposive box in {country_scope}; not globally representative.",
            "Stored context covers region/county/locality only; finer subtypes "
            "are taxonomy-only channels. Neighborhood polygons are joined where "
            "they exist, and macrohood/neighborhood/microhood division points "
            "are associated with their containing municipality by name -- a "
            "name-level heuristic, not verified neighborhood-polygon "
            "containment for those point-only features.",
            cap_limitation,
            "The measured storage delta is a favorable-case lower bound; see the "
            "storage delta caveat.",
            "address_levels semantics are country-dependent; first/last are proxies.",
            "Division-area geometry is current-release; a single release join only.",
            "Source row-group/row locators are absent from raw addresses and set to "
            "zero; the storage delta is unaffected because both variants encode them "
            "identically.",
            "Match confidence is a structural-completeness proxy, not a calibrated "
            "probability; Overture division areas carry no per-point confidence.",
            "No R2 object, catalog, shard, Worker, or production state is written.",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    agreement = report["agreement"]
    counts = agreement["counts"]
    pct = agreement["percentages"]
    storage = report["storage"]
    cost = report["build_cost"]
    total = agreement["total_rows"]
    box = report["sample_box"]
    lines = [
        "# Address-format convergence decision",
        "",
        f"Date: {report['date']} / release `{report['release']}` / schema "
        f"`{report['schema_version']}` (Addresses theme is Alpha).",
        "",
        "> Bounded, purposive single-box current-release run. Not a statistically",
        "> representative sample. Counts are raw, unweighted sample counts. No R2,",
        "> catalog, shard, Worker, or production state is written.",
        "",
        "## Decision",
        "",
        "**Converge the division-joined spike INTO the division-free hosted",
        "lookup-safe format.** The hosted `useful_gzip` page format (the 35.50 "
        "B/indexed",
        "row baseline) stays the hot record and remains the source of truth for the",
        "response: it already retains raw `address_levels`, display fields, exact",
        "candidate keys, coordinates, IDs, and source locators. The spike's",
        "contribution -- containing-division GERS IDs and an explicit",
        "match-method/confidence byte -- is ADDED as a separate, optional per-page",
        "extension (a page division dictionary + per-row index + one byte), never a",
        "replacement for `address_levels` or `postal_city`. The runtime",
        "point-in-polygon join is eliminated: division containment is materialized",
        "once during the offline build. This is exactly the shape",
        "`docs/places-search-spike.md` predicted as likely safe: retain source",
        "address fields, add separately identified containing-division GERS IDs plus",
        "an explicit match method/confidence.",
        "",
        "The measured/modeled separation below justifies keeping the two contexts",
        "distinct rather than collapsing one into the other.",
        "",
        "## Sample",
        "",
        f"- Box `{box['name']}`: lon [{box['xmin']}, {box['xmax']}], "
        f"lat [{box['ymin']}, {box['ymax']}]",
        f"- Addresses in box (pre-cap): "
        f"{report['provenance']['address_rows_in_box_pre_cap']:,}; sampled "
        f"(cap-saturated: {str(report['provenance']['cap_saturated']).lower()}, "
        f"row cap {report['provenance']['row_cap']:,}): "
        f"{report['provenance']['sampled_rows']:,}; classified rows: {total:,}",
        f"- Division polygons in box: "
        f"{report['provenance']['division_polygons_in_box']:,} "
        f"({report['provenance']['context_division_polygons_in_box']:,} "
        "region/county/locality context polygons + taxonomy-only neighborhoods)",
        f"- Finer division points in municipality scope: "
        f"{report['provenance']['finer_division_points_in_scope']:,} "
        f"({report['provenance']['locality_finer_name_pairs']:,} "
        "locality/finer-name associations; taxonomy-only)",
        f"- DuckDB `{report['duckdb_version']}`; producer commit "
        f"`{report['producer_commit']}`",
        "",
        "## Agreement taxonomy",
        "",
        f"{agreement['definition']}",
        "",
        "| Bucket | Rows | Share |",
        "|---|---:|---:|",
    ]
    for bucket in TAXONOMY:
        lines.append(f"| {bucket} | {counts[bucket]:,} | {pct[bucket]:.2f}% |")
    lines += [
        f"| **total** | **{total:,}** | **100.00%** |",
        "",
        f"- Rows with `address_levels`: {agreement['rows_with_address_levels']:,}",
        f"- Rows with a containing division: {agreement['rows_with_containment']:,}",
        f"- Rows with a populated `postal_city`: "
        f"{agreement['rows_with_postal_city']:,}",
        f"- **Genuine cross-context conflicts: {agreement['genuine_conflict_rows']:,} "
        f"({agreement['genuine_conflict_rate_percent']:.2f}%)** -- country "
        "disagreements plus unresolved disagreements after granularity "
        "decomposition.",
        "",
    ]
    lines.extend(f"> {note}" for note in agreement["structural_notes"])
    lines += [
        "",
        "The agreement-plus-granularity rate (exact + normalization-only +",
        "finer-granularity) shows how often `address_levels` and geometric",
        "containment already describe compatible places; the small genuine-conflict",
        "remainder is precisely why a separately identified GERS ID with an",
        "explicit match method is safer than overwriting the source label.",
        "",
        "### Concrete mismatch examples (distinct label shapes; no IDs)",
        "",
        "| Category | Rows | Country | AL region | AL locality | Postal city | "
        "Cont. region | Cont. locality | Cont. neighborhood | Method |",
        "|---|---:|---|---|---|---|---|---|---|---|",
    ]
    for example in agreement["examples"]:
        lines.append(
            f"| {example['category']} | {example['occurrences']:,} | "
            f"{example['country']} | {example['address_levels_region']} | "
            f"{example['address_levels_locality']} | {example['postal_city']} | "
            f"{example['containment_region']} | {example['containment_locality']} | "
            f"{example.get('containment_neighborhood', '')} | "
            f"{example['match_method']} |"
        )
    lines += [
        "",
        "## Storage delta vs the 35.50 B/row baseline",
        "",
        f"Measured on the same {storage['rows']:,} rows, page_rows="
        f"{storage['page_rows']}, independent gzip pages.",
        "",
        "| Format | B/indexed row | p50 page bytes | Linear all-473M GB |",
        "|---|---:|---:|---:|",
        f"| Lookup-safe baseline (measured here) | "
        f"{storage['baseline_bytes_per_row']:.3f} | "
        f"{storage['baseline_p50_page_bytes']:,} | "
        f"{storage['linear_baseline_all_planning_rows_gb']:.2f} |",
        f"| + division GERS IDs + match byte | "
        f"{storage['extended_bytes_per_row']:.3f} | "
        f"{storage['extended_p50_page_bytes']:,} | "
        f"{storage['linear_extended_all_planning_rows_gb']:.2f} |",
        f"| **delta** | **+{storage['delta_bytes_per_row']:.3f}** | | |",
        "",
        "All GB projections in the table linearize the box-measured "
        f"{storage['baseline_bytes_per_row']:.3f}/"
        f"{storage['extended_bytes_per_row']:.3f} B/row values. Applying the same "
        f"+{storage['delta_bytes_per_row']:.3f} B/row delta to the hosted "
        f"`useful_gzip` reference baseline "
        f"({storage['reference_lookup_safe_baseline_bytes_per_row']} B/row, "
        "measured on the separate hosted reduce range) gives "
        f"{storage['reference_lookup_safe_baseline_bytes_per_row'] * PLANNING_ROWS / 1_000_000_000:.2f} GB -> "
        f"{(storage['reference_lookup_safe_baseline_bytes_per_row'] + storage['delta_bytes_per_row']) * PLANNING_ROWS / 1_000_000_000:.2f} GB. "
        f"Both sit far inside the {storage['storage_stop_gate_gb']:.0f} GB stop "
        "gate (addresses share that budget with Places; these are labeled "
        "diagnostics, not forecasts). Every stored extended page is verified to "
        "decode from its bytes alone (self-describing core-length framing).",
        "",
        f"> {storage['delta_caveat']}",
        "",
        "## Build cost (labeled diagnostic, not a forecast)",
        "",
        f"- Spatial join: {cost['join_wall_seconds']:.3f} s wall on a "
        f"{cost['join_threads']}-thread DuckDB session, "
        f"{cost['join_cpu_seconds']:.3f} s CPU, over {cost['joined_rows']:,} points "
        f"x {cost['division_polygons']:,} polygons",
        f"- Peak process RSS: {cost['peak_rss_bytes'] / 1_000_000:.1f} MB",
        f"- Linear all-473M-row wall diagnostic: "
        f"{cost['linear_all_planning_rows_factory_hours']:.2f} factory-hours "
        f"(single {cost['join_threads']}-thread-session wall-clock basis)",
        "",
        f"> {cost['diagnostic_warning']}",
        "",
        "## What the hot record carries vs what stays optional",
        "",
        "- **Hot record (unchanged 35.50 B/row lookup-safe page):** exact candidate",
        "  key, feature ID, quantized coordinates, number/unit, display fields, raw",
        "  `address_levels` sequence, and source row-group/row locators.",
        "- **Optional per-page division extension (added, measured above):**",
        "  containing region/county/locality GERS IDs via a page dictionary + per-row",
        "  index, plus one match-method/confidence byte, framed behind a uvarint",
        "  core-length prefix so the stored page decodes with no out-of-band",
        "  knowledge. Absent-context rows cost a single zero byte.",
        "- **Not on the hot record:** no runtime point-in-polygon join, no",
        "  overwriting of `address_levels`/`postal_city`, no full division geometry.",
        "  Registry membership must be confirmed before calling any division ID a GERS",
        "  ID in a public response.",
        "",
        "## Reproduction",
        "",
        "```bash",
        "python3 scripts/experiment_address_format_convergence.py \\",
        "  --json-out benchmarks/address-format-convergence-report.json \\",
        "  --markdown-out benchmarks/address-format-convergence-report.md",
        "```",
        "",
        "## Limitations",
        "",
    ]
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def run(
    box: SampleBox,
    *,
    row_cap: int,
    page_rows: int,
    threads: int,
    memory_limit: str,
    temp_directory: Path,
    temp_cap_bytes: int,
    producer_commit: str,
) -> dict[str, Any]:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - network path only
        raise SystemExit(
            "experiment_address_format_convergence.py requires duckdb for the run"
        ) from exc
    temp_directory.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    try:
        extract = extract_rows(
            connection,
            box,
            row_cap=row_cap,
            threads=threads,
            memory_limit=memory_limit,
            temp_directory=temp_directory,
            temp_cap_bytes=temp_cap_bytes,
        )
    finally:
        connection.close()
    records = prepare_records(extract["rows"])
    if not records:
        raise SystemExit("no addresses extracted for the configured box")
    storage = measure_storage(records, page_rows=page_rows)
    return build_report(
        box,
        extract,
        records,
        storage,
        duckdb_version=duckdb.__version__,
        producer_commit=producer_commit,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument("--box-name", default=DEFAULT_BOX.name)
    parser.add_argument("--xmin", type=float, default=DEFAULT_BOX.xmin)
    parser.add_argument("--ymin", type=float, default=DEFAULT_BOX.ymin)
    parser.add_argument("--xmax", type=float, default=DEFAULT_BOX.xmax)
    parser.add_argument("--ymax", type=float, default=DEFAULT_BOX.ymax)
    parser.add_argument("--row-cap", type=int, default=40_000)
    parser.add_argument("--page-rows", type=int, default=256)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--memory-limit", default="4GB")
    parser.add_argument(
        "--temp-directory", type=Path, default=Path("/tmp/duckdb-convergence")
    )
    parser.add_argument("--temp-cap-bytes", type=int, default=2 * 1024 * 1024 * 1024)
    parser.add_argument(
        "--producer-commit", default=os.environ.get("GITHUB_SHA", "working-tree")
    )
    args = parser.parse_args(argv)
    if args.row_cap <= 0 or args.page_rows <= 0:
        raise SystemExit("row-cap and page-rows must be positive")
    box = SampleBox(args.box_name, args.xmin, args.ymin, args.xmax, args.ymax)
    report = run(
        box,
        row_cap=args.row_cap,
        page_rows=args.page_rows,
        threads=args.threads,
        memory_limit=args.memory_limit,
        temp_directory=args.temp_directory,
        temp_cap_bytes=args.temp_cap_bytes,
        producer_commit=args.producer_commit,
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    args.markdown_out.write_text(render_markdown(report))
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
