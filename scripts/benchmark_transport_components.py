#!/usr/bin/env python3
"""Bounded Overture transportation snapshot name-cluster experiment.

This is an offline experiment, not a shard builder. It extracts a release-pinned,
road slice, then creates release-scoped primary-name clusters only when segments
share an Overture connector ID. Geometric crossings are audited but never used as
graph edges. Scoped ``names.rules`` are assertions, not topology names.

Core membership uses the exact, pinned Overture Boston locality division_area.
Its bbox only prunes remote I/O and its small outer buffer only supplies boundary
topology. "Official" here means Overture's dataset polygon, not legal authority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sys
import tempfile
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import duckdb
except ImportError as exc:  # pragma: no cover - exercised by CLI environments
    raise SystemExit("benchmark_transport_components.py requires duckdb") from exc


PINNED_RELEASE = "2026-06-17.0"
BOUNDARY_DIVISION_ID = "5df2793f-5a0a-4fcf-bd3c-7edb8cc495d8"
BOUNDARY_DIVISION_VERSION = 6
BOUNDARY_AREA_ID = "78cd3b93-9cd5-4023-9e31-4194be70701b"
BOUNDARY_AREA_VERSION = 10
DEFAULT_HALO_DEGREES = 0.01
BOUNDARY_LABEL = (
    "Overture Boston locality division_area (official dataset polygon) with "
    "an experimental extraction halo"
)
REPORT_VERSION = 3
ADDRESS_HARD_MAX_ROWS = 250_000
DEFAULT_REMOTE_TIME_CAP_SECONDS = 900


class UnionFind:
    def __init__(self, members: Iterable[str]) -> None:
        self.parent = {member: member for member in members}

    def find(self, member: str) -> str:
        parent = self.parent[member]
        if parent != member:
            self.parent[member] = self.find(parent)
        return self.parent[member]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            # The lexical choice makes results independent of traversal order.
            parent, child = sorted((left_root, right_root))
            self.parent[child] = parent


def normalize_name(value: str | None) -> str:
    """Conservatively normalize names without discarding punctuation."""
    if not value:
        return ""
    return " ".join(unicodedata.normalize("NFC", value).strip().lower().split())


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


_ROW_EXAMPLE_KEYS = {
    "examples", "missing_reference_examples", "repeated_name_examples",
}


def sanitize_aggregate_report(value: Any) -> Any:
    """Remove bounded row/name examples from the committable machine report."""
    if isinstance(value, dict):
        return {
            key: sanitize_aggregate_report(item)
            for key, item in value.items()
            if key not in _ROW_EXAMPLE_KEYS
        }
    if isinstance(value, list):
        return [sanitize_aggregate_report(item) for item in value]
    return value


def _primary_name(segment: dict[str, Any]) -> str:
    return normalize_name((segment.get("names") or {}).get("primary"))


def _common_aliases(segment: dict[str, Any]) -> list[dict[str, str]]:
    common = (segment.get("names") or {}).get("common") or {}
    return [
        {"language": language, "value": value, "normalized": normalize_name(value)}
        for language, value in sorted(common.items())
        if normalize_name(value)
    ]


def _rule_assertions(segment: dict[str, Any]) -> list[dict[str, Any]]:
    """Preserve rules without treating scoped assertions as topology identity."""
    return [
        {
            "variant": rule.get("variant"),
            "language": rule.get("language"),
            "value": rule.get("value"),
            "between": rule.get("between"),
            "side": rule.get("side"),
            "perspectives": rule.get("perspectives"),
        }
        for rule in ((segment.get("names") or {}).get("rules") or [])
        if rule.get("value")
    ]


def _connector_ids(segment: dict[str, Any]) -> list[str]:
    return sorted({
        connector["connector_id"]
        for connector in (segment.get("connectors") or [])
        if connector and connector.get("connector_id")
    })


def _root_source_datasets(segment: dict[str, Any]) -> list[str]:
    return sorted({
        source.get("dataset")
        for source in (segment.get("sources") or [])
        if source and not (source.get("property") or "") and source.get("dataset")
    })


def _bbox_intersects(
    bbox: dict[str, Any], window: tuple[float, float, float, float]
) -> bool:
    xmin, ymin, xmax, ymax = window
    return (
        float(bbox["xmax"]) >= xmin and float(bbox["xmin"]) <= xmax
        and float(bbox["ymax"]) >= ymin and float(bbox["ymin"]) <= ymax
    )


def _expanded_bbox(
    bbox: tuple[float, float, float, float], halo: float
) -> tuple[float, float, float, float]:
    return (bbox[0] - halo, bbox[1] - halo, bbox[2] + halo, bbox[3] + halo)


def _touches_frontier(
    bbox: dict[str, Any], extraction_bbox: tuple[float, float, float, float]
) -> bool:
    xmin, ymin, xmax, ymax = extraction_bbox
    epsilon = 1e-9
    return (
        float(bbox["xmin"]) <= xmin + epsilon
        or float(bbox["ymin"]) <= ymin + epsilon
        or float(bbox["xmax"]) >= xmax - epsilon
        or float(bbox["ymax"]) >= ymax - epsilon
    )


def build_snapshot_name_clusters(
    segments: list[dict[str, Any]],
    release: str,
    extraction_bbox: tuple[float, float, float, float] | None = None,
) -> list[dict[str, Any]]:
    """Build release-scoped primary-name clusters using shared connectors only.

    ``names.common`` is emitted through a separate alias lookup. ``names.rules``
    is excluded because its between/side/perspective scope is not evaluated here.
    Only clusters containing at least one ``core_seed`` segment are returned.
    """
    by_id = {str(segment["id"]): segment for segment in segments}
    memberships: dict[str, set[str]] = defaultdict(set)
    connector_members: dict[tuple[str, str], list[str]] = defaultdict(list)
    connector_frequency = Counter(
        connector_id for segment in segments for connector_id in _connector_ids(segment)
    )

    for segment_id, segment in by_id.items():
        normalized = _primary_name(segment)
        if not normalized:
            continue
        memberships[normalized].add(segment_id)
        for connector_id in _connector_ids(segment):
            connector_members[(normalized, connector_id)].append(segment_id)

    snapshot_name_clusters: list[dict[str, Any]] = []
    for normalized in sorted(memberships):
        union_find = UnionFind(memberships[normalized])
        for key, segment_ids in connector_members.items():
            name, _connector_id = key
            if name != normalized or len(segment_ids) < 2:
                continue
            anchor = min(segment_ids)
            for segment_id in segment_ids:
                union_find.union(anchor, segment_id)

        groups: dict[str, list[str]] = defaultdict(list)
        for segment_id in sorted(memberships[normalized]):
            groups[union_find.find(segment_id)].append(segment_id)

        for segment_ids in sorted(groups.values(), key=lambda values: tuple(values)):
            cluster_segments = [by_id[segment_id] for segment_id in segment_ids]
            core_segments = [segment for segment in cluster_segments if segment.get("core_seed")]
            if not core_segments:
                continue
            digest_input = f"{release}\0{normalized}\0" + "\0".join(segment_ids)
            digest = hashlib.sha256(digest_input.encode()).hexdigest()[:20]
            bboxes = [segment.get("bbox") or {} for segment in cluster_segments]
            bbox = {
                "xmin": min(float(item["xmin"]) for item in bboxes),
                "ymin": min(float(item["ymin"]) for item in bboxes),
                "xmax": max(float(item["xmax"]) for item in bboxes),
                "ymax": max(float(item["ymax"]) for item in bboxes),
            }
            representative_segment = min(core_segments, key=lambda item: str(item["id"]))
            open_connector_ids = sorted({
                connector_id
                for segment in cluster_segments
                for connector_id in _connector_ids(segment)
                if connector_frequency[connector_id] == 1
            })
            frontier = any(
                not segment.get("core_seed") or segment.get("boundary_crossing")
                for segment in cluster_segments
            )
            frontier_single_reference_ids = sorted({
                connector_id
                for segment in cluster_segments
                if not segment.get("core_seed") or segment.get("boundary_crossing")
                for connector_id in _connector_ids(segment)
                if connector_frequency[connector_id] == 1
            })
            snapshot_name_clusters.append({
                "snapshot_cluster_id": f"snapshot_name_cluster_v1_{digest}",
                "overture_release": release,
                "primary_name_normalized": normalized,
                "segment_ids": segment_ids,
                "segment_count": len(segment_ids),
                "core_seed_segment_count": len(core_segments),
                "halo_support_segment_count": len(cluster_segments) - len(core_segments),
                "frontier": frontier,
                "single_reference_connector_count": len(open_connector_ids),
                "frontier_open_connector_proxy_count": len(
                    frontier_single_reference_ids
                ),
                "classes": dict(sorted(Counter(
                    segment.get("class") or "unknown" for segment in cluster_segments
                ).items())),
                "versions": sorted({
                    int(segment["version"])
                    for segment in cluster_segments
                    if segment.get("version") is not None
                }),
                "root_source_datasets": sorted({
                    dataset
                    for segment in cluster_segments
                    for dataset in _root_source_datasets(segment)
                }),
                "bbox": bbox,
                "representative_point": {
                    "lon": representative_segment.get("representative_lon"),
                    "lat": representative_segment.get("representative_lat"),
                    "method": "centroid of lexically first segment ID",
                },
            })
    return snapshot_name_clusters


def build_alias_lookup(
    segments: list[dict[str, Any]], clusters: list[dict[str, Any]]
) -> dict[str, list[str]]:
    """Build a deduplicated common-name alias lookup separate from clusters."""
    segment_to_cluster: dict[str, str] = {}
    for cluster in clusters:
        for segment_id in cluster["segment_ids"]:
            segment_to_cluster[segment_id] = cluster["snapshot_cluster_id"]
    aliases: dict[str, set[str]] = defaultdict(set)
    for segment in segments:
        cluster_id = segment_to_cluster.get(str(segment["id"]))
        if not cluster_id:
            continue
        for alias in _common_aliases(segment):
            aliases[alias["normalized"]].add(cluster_id)
    return {name: sorted(cluster_ids) for name, cluster_ids in sorted(aliases.items())}


def _quantiles(values: list[int]) -> dict[str, int]:
    ordered = sorted(values)
    if not ordered:
        return {"p50": 0, "p90": 0, "p99": 0, "max": 0}

    def quantile(fraction: float) -> int:
        return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)]

    return {
        "p50": quantile(0.50),
        "p90": quantile(0.90),
        "p99": quantile(0.99),
        "max": ordered[-1],
    }


def summarize_snapshot_name_clusters(
    segments: list[dict[str, Any]], clusters: list[dict[str, Any]]
) -> dict[str, Any]:
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cluster in clusters:
        by_name[cluster["primary_name_normalized"]].append(cluster)
    repeated = [items for items in by_name.values() if len(items) > 1]
    examples = sorted(
        (
            {
                "primary_name_normalized": items[0]["primary_name_normalized"],
                "snapshot_cluster_count": len(items),
                "segment_counts": sorted(
                    (item["segment_count"] for item in items), reverse=True
                )[:10],
                "snapshot_cluster_ids": sorted(
                    item["snapshot_cluster_id"] for item in items
                )[:10],
            }
            for items in repeated
        ),
        key=lambda item: (-item["snapshot_cluster_count"], item["primary_name_normalized"]),
    )[:25]
    connector_refs = sum(len(_connector_ids(segment)) for segment in segments)
    connector_ids = {
        connector_id for segment in segments for connector_id in _connector_ids(segment)
    }
    root_sources = Counter(
        dataset
        for segment in segments
        for dataset in _root_source_datasets(segment)
    )
    return {
        "all_road_segments": len(segments),
        "named_road_segments": sum(bool(_primary_name(segment)) for segment in segments),
        "core_seed_road_segments": sum(bool(segment.get("core_seed")) for segment in segments),
        "core_seed_named_road_segments": sum(
            bool(segment.get("core_seed")) and bool(_primary_name(segment))
            for segment in segments
        ),
        "connector_references": connector_refs,
        "unique_connector_ids": len(connector_ids),
        "segments_without_connectors": sum(not _connector_ids(segment) for segment in segments),
        "primary_names": len(by_name),
        "core_touching_snapshot_name_clusters": len(clusters),
        "snapshot_cluster_segment_count": _quantiles([
            cluster["segment_count"] for cluster in clusters
        ]),
        "names_with_multiple_snapshot_clusters": len(repeated),
        "snapshot_clusters_on_repeated_names": sum(len(items) for items in repeated),
        "repeated_name_examples": examples,
        "core_seed_segment_memberships": sum(
            cluster["core_seed_segment_count"] for cluster in clusters
        ),
        "halo_support_segment_memberships": sum(
            cluster["halo_support_segment_count"] for cluster in clusters
        ),
        "frontier_snapshot_clusters": sum(cluster["frontier"] for cluster in clusters),
        "snapshot_clusters_with_single_reference_connector": sum(
            cluster["single_reference_connector_count"] > 0 for cluster in clusters
        ),
        "frontier_open_connector_proxy_references": sum(
            cluster["frontier_open_connector_proxy_count"] for cluster in clusters
        ),
        "frontier_open_connector_proxy_warning": (
            "A single-reference connector on a frontier segment is a bounded-snapshot "
            "continuation proxy, not proof that the global connector is open."
        ),
        "root_source_dataset_segment_memberships": dict(root_sources.most_common()),
        "segments_with_version": sum(segment.get("version") is not None for segment in segments),
        "scoped_name_rule_assertions_excluded_from_topology": sum(
            len(_rule_assertions(segment)) for segment in segments
        ),
    }


def address_name_context(
    clusters: list[dict[str, Any]], address_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Measure name-level address context without pretending it is spatially assigned."""
    contexts = {row["street_norm"]: row for row in address_rows}
    by_name = Counter(cluster["primary_name_normalized"] for cluster in clusters)
    matched = [
        cluster for cluster in clusters
        if cluster["primary_name_normalized"] in contexts
    ]
    ambiguous = [
        cluster for cluster in matched
        if by_name[cluster["primary_name_normalized"]] > 1
    ]
    return {
        "diagnostic_only": True,
        "warning": (
            "Exact normalized-name matching is not snapshot-cluster enrichment. "
            "Disconnected same-name clusters require a spatial address-to-segment join."
        ),
        "address_street_names": len(contexts),
        "snapshot_clusters_with_name_level_address_context": len(matched),
        "coverage": len(matched) / len(clusters) if clusters else None,
        "uniquely_named_snapshot_clusters_with_context": len(matched) - len(ambiguous),
        "ambiguous_repeated_name_snapshot_clusters_with_context": len(ambiguous),
    }


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _transport_path(release: str) -> str:
    return (
        "s3://overturemaps-us-west-2/release/"
        f"{release}/theme=transportation/type=segment/*"
    )


def _address_path(release: str) -> str:
    return (
        "s3://overturemaps-us-west-2/release/"
        f"{release}/theme=addresses/type=address/*"
    )


def _connector_path(release: str) -> str:
    return (
        "s3://overturemaps-us-west-2/release/"
        f"{release}/theme=transportation/type=connector/*"
    )


def address_hash_sample_plan(population: int, row_cap: int) -> dict[str, Any]:
    """Plan a reproducible bounded sample without a global hash sort."""
    if population < 0 or row_cap <= 0:
        raise ValueError("population must be non-negative and row_cap positive")
    sampled = population > row_cap
    target = min(population, int(row_cap * 0.95) if sampled else row_cap)
    hash_space = 2**32
    threshold = math.floor(target / population * hash_space) if population else hash_space
    return {
        "sampled": sampled, "target_rows": target,
        "hash_space": hash_space, "hash_threshold": threshold,
    }


def _bbox_filter(bbox: tuple[float, float, float, float], halo: float) -> str:
    xmin, ymin, xmax, ymax = bbox
    return (
        f"bbox.xmax >= {xmin - halo} AND bbox.xmin <= {xmax + halo} "
        f"AND bbox.ymax >= {ymin - halo} AND bbox.ymin <= {ymax + halo}"
    )


def configure_remote(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("INSTALL httpfs; LOAD httpfs")
    connection.execute("INSTALL spatial; LOAD spatial")
    connection.execute("SET s3_region='us-west-2'")
    connection.execute("SET threads=2")
    connection.execute("SET memory_limit='4GB'")
    connection.execute("SET preserve_insertion_order=false")


def extract_boundary(
    connection: duckdb.DuckDBPyConnection, output: Path, release: str
) -> dict[str, Any]:
    """Pin and materialize the exact unsimplified Overture Boston polygon."""
    division_path = (
        "s3://overturemaps-us-west-2/release/"
        f"{release}/theme=divisions/type=division/*"
    )
    area_path = (
        "s3://overturemaps-us-west-2/release/"
        f"{release}/theme=divisions/type=division_area/*"
    )
    started = time.monotonic()
    connection.execute(f"""
        COPY (
            SELECT d.id AS division_id, d.version AS division_version,
                   d.names.primary AS division_name, d.subtype, d.country, d.region,
                   a.id AS area_id, a.version AS area_version, a.geometry, a.bbox
            FROM read_parquet({_sql_literal(division_path)}, hive_partitioning=true) d
            JOIN read_parquet({_sql_literal(area_path)}, hive_partitioning=true) a
              ON a.division_id = d.id
            WHERE d.id = {_sql_literal(BOUNDARY_DIVISION_ID)}
              AND a.id = {_sql_literal(BOUNDARY_AREA_ID)}
        ) TO {_sql_literal(str(output))} (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    row = connection.execute("""
        SELECT division_id, division_version, division_name, subtype, country, region,
               area_id, area_version, ST_XMin(geometry), ST_YMin(geometry),
               ST_XMax(geometry), ST_YMax(geometry), ST_AsWKB(geometry)
        FROM read_parquet(?)
    """, [str(output)]).fetchone()
    if row is None or connection.execute(
        "SELECT COUNT(*) FROM read_parquet(?)", [str(output)]
    ).fetchone()[0] != 1:
        output.unlink(missing_ok=True)
        raise RuntimeError("pinned Boston division_area did not resolve to exactly one row")
    if (
        row[0] != BOUNDARY_DIVISION_ID or row[1] != BOUNDARY_DIVISION_VERSION
        or row[6] != BOUNDARY_AREA_ID or row[7] != BOUNDARY_AREA_VERSION
        or row[2:6] != ("Boston", "locality", "US", "US-MA")
    ):
        output.unlink(missing_ok=True)
        raise RuntimeError(f"pinned Boston division_area metadata changed: {row[:8]!r}")
    geometry_wkb = bytes(row[12])
    return {
        "release": release,
        "division_id": row[0], "division_version": row[1],
        "division_name": row[2], "subtype": row[3],
        "country": row[4], "region": row[5],
        "division_area_id": row[6], "division_area_version": row[7],
        "bbox": [float(value) for value in row[8:12]],
        "geometry_sha256": hashlib.sha256(geometry_wkb).hexdigest(),
        "geometry_wkb_bytes": len(geometry_wkb),
        "geometry_handling": (
            "exact Overture division_area geometry; no simplification or buffering for "
            "core membership; bbox is only a remote prefilter"
        ),
        "materialized_parquet_bytes": output.stat().st_size,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def extract_transportation(
    connection: duckdb.DuckDBPyConnection,
    output: Path,
    release: str,
    boundary_path: Path,
    bbox: tuple[float, float, float, float],
    halo: float,
    max_rows: int,
    max_bytes: int,
) -> dict[str, Any]:
    path = _transport_path(release)
    where = "segment.subtype = 'road' AND " + _bbox_filter(bbox, halo).replace(
        "bbox.", "segment.bbox."
    )
    started = time.monotonic()
    connection.execute(f"""
        COPY (
            SELECT segment.id, segment.names, segment.subtype, segment.class,
                   segment.subclass, segment.connectors, segment.road_flags,
                   segment.level_rules, segment.sources, segment.version,
                   segment.geometry, segment.bbox,
                   ST_X(ST_Centroid(segment.geometry)) AS representative_lon,
                   ST_Y(ST_Centroid(segment.geometry)) AS representative_lat,
                   ST_Intersects(
                       segment.geometry, boundary.geometry
                   ) AS core_seed,
                   ST_Intersects(segment.geometry, boundary.geometry)
                       AND NOT ST_Within(segment.geometry, boundary.geometry)
                       AS boundary_crossing
            FROM read_parquet({_sql_literal(path)}, hive_partitioning=true) segment,
                 read_parquet({_sql_literal(str(boundary_path))}) boundary
            WHERE {where}
              AND ST_Intersects(segment.geometry, ST_Buffer(boundary.geometry, {halo}))
            ORDER BY segment.id
            LIMIT {max_rows + 1}
        ) TO {_sql_literal(str(output))} (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    row_count = int(connection.execute(
        "SELECT COUNT(*) FROM read_parquet(?)", [str(output)]
    ).fetchone()[0])
    if row_count > max_rows:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"transport row guard exceeded: >{max_rows:,}; bounded artifact removed"
        )
    actual_bytes = output.stat().st_size
    if actual_bytes > max_bytes:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"transport byte guard exceeded: {actual_bytes:,} > {max_bytes:,}; artifact removed"
        )
    return {
        "row_guard": max_rows,
        "byte_guard": max_bytes,
        "rows": row_count,
        "materialized_parquet_bytes": actual_bytes,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "remote_path": path,
        "filter": where,
        "guard_scope": (
            "Guards bound materialized rows and Parquet bytes; remote Parquet range-scan "
            "traffic is controlled by bbox predicate pushdown but is not byte-metered."
        ),
    }


def extract_connectors(
    connection: duckdb.DuckDBPyConnection,
    output: Path,
    release: str,
    boundary_path: Path,
    bbox: tuple[float, float, float, float],
    halo: float,
    max_rows: int,
    max_bytes: int,
) -> dict[str, Any]:
    path = _connector_path(release)
    where = _bbox_filter(bbox, halo).replace("bbox.", "connector.bbox.")
    started = time.monotonic()
    connection.execute(f"""
        COPY (
            SELECT connector.id, connector.sources, connector.version, connector.bbox
            FROM read_parquet({_sql_literal(path)}, hive_partitioning=true) connector,
                 read_parquet({_sql_literal(str(boundary_path))}) boundary
            WHERE {where}
              AND ST_Intersects(connector.geometry, ST_Buffer(boundary.geometry, {halo}))
            ORDER BY connector.id
            LIMIT {max_rows + 1}
        ) TO {_sql_literal(str(output))} (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    row_count = int(connection.execute(
        "SELECT COUNT(*) FROM read_parquet(?)", [str(output)]
    ).fetchone()[0])
    if row_count > max_rows:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"connector row guard exceeded: >{max_rows:,}; bounded artifact removed"
        )
    actual_bytes = output.stat().st_size
    if actual_bytes > max_bytes:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"connector byte guard exceeded: {actual_bytes:,} > {max_bytes:,}; artifact removed"
        )
    return {
        "row_guard": max_rows,
        "byte_guard": max_bytes,
        "rows": row_count,
        "materialized_parquet_bytes": actual_bytes,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "remote_path": path,
    }


def extract_address_context(
    connection: duckdb.DuckDBPyConnection,
    release: str,
    boundary_path: Path,
    bbox: tuple[float, float, float, float],
    max_rows: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if max_rows <= 0 or max_rows > ADDRESS_HARD_MAX_ROWS:
        raise ValueError(
            f"address row guard must be in 1..{ADDRESS_HARD_MAX_ROWS:,}"
        )
    path = _address_path(release)
    where = "address.street IS NOT NULL AND " + _bbox_filter(bbox, 0).replace(
        "bbox.", "address.bbox."
    )
    geometry_where = (
        f"{where} AND address.geometry IS NOT NULL "
        "AND ST_GeometryType(address.geometry) = 'POINT' "
        "AND ST_Within(address.geometry, boundary.geometry)"
    )
    started = time.monotonic()
    count = int(connection.execute(
        f"SELECT COUNT(*) FROM read_parquet({_sql_literal(path)}, "
        f"hive_partitioning=true) address, "
        f"read_parquet({_sql_literal(str(boundary_path))}) boundary "
        f"WHERE {geometry_where}"
    ).fetchone()[0])
    plan = address_hash_sample_plan(count, max_rows)
    sampled = plan["sampled"]
    hash_space = plan["hash_space"]
    hash_threshold = plan["hash_threshold"]
    cursor = connection.execute(f"""
        WITH candidates AS (
            SELECT address.id, address.street, address.postcode,
                   address.postal_city
            FROM read_parquet({_sql_literal(path)}, hive_partitioning=true) address,
                 read_parquet({_sql_literal(str(boundary_path))}) boundary
            WHERE {geometry_where}
              AND hash(address.id) % {hash_space} < {hash_threshold}
            LIMIT {max_rows}
        )
        SELECT LOWER(NFC_NORMALIZE(REGEXP_REPLACE(TRIM(street), '\\s+', ' ', 'g')))
                   AS street_norm,
               COUNT(*) AS address_rows,
               COUNT(DISTINCT NULLIF(TRIM(postal_city), '')) AS postal_cities,
               COUNT(DISTINCT NULLIF(TRIM(postcode), '')) AS postcodes
        FROM candidates
        GROUP BY street_norm
        ORDER BY street_norm
    """)
    columns = [description[0] for description in cursor.description]
    rows: list[dict[str, Any]] = []
    while batch := cursor.fetchmany(10_000):
        rows.extend(dict(zip(columns, row)) for row in batch)
    return rows, {
        "skipped": False,
        "row_guard": max_rows,
        "geometry_authoritative_population": count,
        "rows": sum(int(row["address_rows"]) for row in rows),
        "sampled": sampled,
        "sample_fraction": (
            sum(int(row["address_rows"]) for row in rows) / count if count else None
        ),
        "sampling_method": (
            "pinned hash(id) threshold targeting 95% of the row cap, followed by a "
            "hard LIMIT; avoids a global deterministic sort"
            if sampled else "complete polygon population"
        ),
        "hash_threshold_u32": hash_threshold,
        "aggregated_street_names": len(rows),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "selection_contract": (
            "bbox-prefiltered for I/O, then exact Point-within unsimplified division_area; "
            "missing/discordant bbox rows are unobservable"
        ),
        "warning": "Name-level proxy only; no contexts are assigned to snapshot clusters.",
    }


def load_segments(
    connection: duckdb.DuckDBPyConnection, path: Path
) -> list[dict[str, Any]]:
    cursor = connection.execute(
        "SELECT id, names, class, connectors, sources, version, bbox, "
        "representative_lon, representative_lat, core_seed, boundary_crossing "
        "FROM read_parquet(?) ORDER BY id",
        [str(path)],
    )
    columns = [description[0] for description in cursor.description]
    result: list[dict[str, Any]] = []
    while rows := cursor.fetchmany(10_000):
        result.extend(dict(zip(columns, row)) for row in rows)
    return result


def load_connector_records(
    connection: duckdb.DuckDBPyConnection, path: Path
) -> list[dict[str, Any]]:
    cursor = connection.execute(
        "SELECT id, sources, version, bbox FROM read_parquet(?) ORDER BY id", [str(path)]
    )
    columns = [description[0] for description in cursor.description]
    result: list[dict[str, Any]] = []
    while rows := cursor.fetchmany(20_000):
        result.extend(dict(zip(columns, row)) for row in rows)
    return result


def connector_validation(
    segments: list[dict[str, Any]], connector_feature_ids: set[str]
) -> dict[str, Any]:
    reference_counts: Counter[str] = Counter()
    endpoint = interior = invalid_at = 0
    duplicate_connector_on_segment = duplicate_at_on_segment = 0
    for segment in segments:
        connectors = segment.get("connectors") or []
        connector_ids = [
            item.get("connector_id") for item in connectors if item.get("connector_id")
        ]
        at_values = [item.get("at") for item in connectors]
        reference_counts.update(connector_ids)
        duplicate_connector_on_segment += len(connector_ids) - len(set(connector_ids))
        comparable_at = [float(value) for value in at_values if value is not None]
        duplicate_at_on_segment += len(comparable_at) - len(set(comparable_at))
        for value in at_values:
            if value is None or not 0 <= float(value) <= 1:
                invalid_at += 1
            elif math.isclose(float(value), 0) or math.isclose(float(value), 1):
                endpoint += 1
            else:
                interior += 1
    referenced_ids = set(reference_counts)
    missing = referenced_ids - connector_feature_ids
    return {
        "connector_references": sum(reference_counts.values()),
        "unique_referenced_connector_ids": len(referenced_ids),
        "endpoint_connector_references": endpoint,
        "interior_connector_references": interior,
        "invalid_at_references": invalid_at,
        "duplicate_connector_id_references_within_segment": duplicate_connector_on_segment,
        "duplicate_at_references_within_segment": duplicate_at_on_segment,
        "single_segment_reference_connector_ids": sum(
            count == 1 for count in reference_counts.values()
        ),
        "referenced_connector_ids_missing_from_bounded_connector_extract": len(missing),
        "missing_reference_examples": sorted(missing)[:20],
        "missing_reference_warning": (
            "A missing connector feature can be boundary clipping; it is not proof of "
            "invalid Overture topology in this bounded snapshot."
        ),
    }


def provenance_coverage(features: list[dict[str, Any]]) -> dict[str, Any]:
    fields = (
        "dataset", "license", "record_id", "update_time", "confidence", "between",
    )
    all_sources = [source for feature in features for source in (feature.get("sources") or [])]
    root_sources = [source for source in all_sources if not (source.get("property") or "")]
    return {
        "feature_rows": len(features),
        "features_with_version": sum(feature.get("version") is not None for feature in features),
        "features_with_any_source": sum(bool(feature.get("sources")) for feature in features),
        "features_with_root_source": sum(any(
            not (source.get("property") or "") for source in (feature.get("sources") or [])
        ) for feature in features),
        "all_source_records": len(all_sources),
        "root_source_records": len(root_sources),
        "property_specific_source_records": len(all_sources) - len(root_sources),
        "all_source_field_populated_records": {
            field: sum(source.get(field) is not None for source in all_sources)
            for field in fields
        },
        "root_source_field_populated_records": {
            field: sum(source.get(field) is not None for source in root_sources)
            for field in fields
        },
        "root_dataset_records": dict(Counter(
            source.get("dataset") or "<missing>" for source in root_sources
        ).most_common()),
        "source_property_records": dict(Counter(
            source.get("property") or "<root>" for source in all_sources
        ).most_common()),
        "confidence_interpretation": (
            "source-supplied and not calibrated across datasets; missing is not zero"
        ),
    }


def rule_assertion_summary(segments: list[dict[str, Any]]) -> dict[str, Any]:
    assertions = [
        {"segment_id": str(segment["id"]), **assertion}
        for segment in segments
        for assertion in _rule_assertions(segment)
    ]
    return {
        "excluded_from_topology_grouping": True,
        "reason": (
            "between/side/perspectives are scoped assertions; this snapshot does not "
            "evaluate them against connector.at positions and query perspective."
        ),
        "assertions": len(assertions),
        "with_between": sum(item.get("between") is not None for item in assertions),
        "with_side": sum(item.get("side") is not None for item in assertions),
        "with_perspectives": sum(item.get("perspectives") is not None for item in assertions),
        "examples": _jsonable(assertions[:10]),
    }


def serialized_snapshot_estimate(
    segments: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    alias_lookup: dict[str, list[str]],
) -> dict[str, Any]:
    """Measure compact JSON bytes with segment metadata serialized exactly once."""
    relevant_ids = {
        segment_id for cluster in clusters for segment_id in cluster["segment_ids"]
    }
    segment_index = {
        str(segment["id"]): {
            "names": _jsonable(segment.get("names")),
            "class": segment.get("class"),
            "version": segment.get("version"),
            "sources": _jsonable(segment.get("sources") or []),
            "bbox": _jsonable(segment.get("bbox")),
            "representative_lon": segment.get("representative_lon"),
            "representative_lat": segment.get("representative_lat"),
        }
        for segment in segments
        if str(segment["id"]) in relevant_ids
    }
    compact = lambda value: len(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8"))
    segment_bytes = compact(segment_index)
    cluster_bytes = compact(clusters)
    alias_bytes = compact(alias_lookup)
    hot_clusters = [{
        "id": cluster["snapshot_cluster_id"],
        "name": cluster["primary_name_normalized"],
        "bbox": cluster["bbox"],
        "point": cluster["representative_point"],
        "classes": cluster["classes"],
        "segment_count": cluster["segment_count"],
        "frontier": cluster["frontier"],
    } for cluster in clusters]
    hot_cluster_bytes = compact(hot_clusters)
    return {
        "encoding": "compact UTF-8 JSON measurement; not a production forecast",
        "deduplicated_segment_index_bytes": segment_bytes,
        "snapshot_cluster_records_bytes": cluster_bytes,
        "common_alias_lookup_bytes": alias_bytes,
        "hot_cluster_lookup_bytes": hot_cluster_bytes + alias_bytes,
        "hot_cluster_records_bytes": hot_cluster_bytes,
        "detail_segment_index_bytes": segment_bytes,
        "total_bytes": segment_bytes + cluster_bytes + alias_bytes,
        "segment_records": len(segment_index),
        "snapshot_cluster_records": len(clusters),
        "common_alias_keys": len(alias_lookup),
    }


def compare_halo_snapshots(
    segments: list[dict[str, Any]],
    release: str,
    core_bbox: tuple[float, float, float, float],
    outer_halo: float,
) -> dict[str, Any]:
    inner_segments = [segment for segment in segments if segment.get("core_seed")]
    inner = build_snapshot_name_clusters(inner_segments, release, core_bbox)
    outer = build_snapshot_name_clusters(
        segments, release, _expanded_bbox(core_bbox, outer_halo)
    )

    def core_peers(clusters: list[dict[str, Any]]) -> dict[str, frozenset[str]]:
        result: dict[str, frozenset[str]] = {}
        core_ids = {
            str(segment["id"]) for segment in segments
            if segment.get("core_seed") and _primary_name(segment)
        }
        for cluster in clusters:
            peers = frozenset(set(cluster["segment_ids"]) & core_ids)
            for segment_id in peers:
                result[segment_id] = peers
        return result

    inner_peers = core_peers(inner)
    outer_peers = core_peers(outer)
    all_core_ids = set(inner_peers) | set(outer_peers)
    changed = sum(inner_peers.get(item) != outer_peers.get(item) for item in all_core_ids)
    return {
        "inner_halo_degrees": 0.0,
        "outer_halo_degrees": outer_halo,
        "inner_core_touching_snapshot_clusters": len(inner),
        "outer_core_touching_snapshot_clusters": len(outer),
        "core_named_segments_compared": len(all_core_ids),
        "core_named_segments_with_changed_core_peer_membership": changed,
        "changed_fraction": changed / len(all_core_ids) if all_core_ids else None,
        "interpretation": (
            "A change means halo support altered connectivity among core named segments; "
            "snapshot cluster IDs are intentionally release/member scoped."
        ),
    }


def geometric_crossing_reference_audit(
    connection: duckdb.DuckDBPyConnection,
    path: Path,
    max_segments: int,
    max_tile_memberships: int,
    max_candidate_pairs: int,
    tile_degrees: float,
) -> dict[str, Any]:
    """Materialize a strictly bounded, tiled geometric crossing audit once."""
    connection.execute("INSTALL spatial; LOAD spatial")
    source = _sql_literal(str(path))
    try:
        connection.execute(f"""
            CREATE OR REPLACE TEMP TABLE crossing_segments AS
            SELECT id, names, class, connectors, geometry, bbox
            FROM read_parquet({source})
            WHERE core_seed AND names.primary IS NOT NULL
            ORDER BY id
            LIMIT {max_segments + 1}
        """)
        segment_count = int(connection.execute(
            "SELECT COUNT(*) FROM crossing_segments"
        ).fetchone()[0])
        if segment_count > max_segments:
            return {
                "skipped": True,
                "reason": f"crossing segment guard exceeded: >{max_segments:,}",
                "segment_guard": max_segments,
            }
        tile_memberships = int(connection.execute(f"""
            SELECT COALESCE(SUM(
                (FLOOR(bbox.xmax / {tile_degrees})
                 - FLOOR(bbox.xmin / {tile_degrees}) + 1)
                *
                (FLOOR(bbox.ymax / {tile_degrees})
                 - FLOOR(bbox.ymin / {tile_degrees}) + 1)
            ), 0)::BIGINT
            FROM crossing_segments
        """).fetchone()[0])
        if tile_memberships > max_tile_memberships:
            return {
                "skipped": True,
                "reason": (
                    f"crossing tile-membership guard exceeded: "
                    f"{tile_memberships:,} > {max_tile_memberships:,}"
                ),
                "segment_rows": segment_count,
                "tile_membership_guard": max_tile_memberships,
                "tile_degrees": tile_degrees,
            }
        connection.execute(f"""
            CREATE OR REPLACE TEMP TABLE crossing_pair_candidates AS
            WITH tiled AS (
                SELECT segment.*, tile_x, tile_y
                FROM crossing_segments segment,
                UNNEST(generate_series(
                    CAST(FLOOR(bbox.xmin / {tile_degrees}) AS BIGINT),
                    CAST(FLOOR(bbox.xmax / {tile_degrees}) AS BIGINT)
                )) AS x(tile_x),
                UNNEST(generate_series(
                    CAST(FLOOR(bbox.ymin / {tile_degrees}) AS BIGINT),
                    CAST(FLOOR(bbox.ymax / {tile_degrees}) AS BIGINT)
                )) AS y(tile_y)
            )
            SELECT DISTINCT a.id AS left_id, b.id AS right_id
            FROM tiled a JOIN tiled b
              ON a.tile_x = b.tile_x AND a.tile_y = b.tile_y AND a.id < b.id
            WHERE a.bbox.xmax >= b.bbox.xmin AND a.bbox.xmin <= b.bbox.xmax
              AND a.bbox.ymax >= b.bbox.ymin AND a.bbox.ymin <= b.bbox.ymax
            LIMIT {max_candidate_pairs + 1}
        """)
        candidate_count = int(connection.execute(
            "SELECT COUNT(*) FROM crossing_pair_candidates"
        ).fetchone()[0])
        if candidate_count > max_candidate_pairs:
            return {
                "skipped": True,
                "reason": f"crossing candidate-pair guard exceeded: >{max_candidate_pairs:,}",
                "segment_rows": segment_count,
                "candidate_pair_guard": max_candidate_pairs,
                "tile_degrees": tile_degrees,
            }
        connection.execute("""
            CREATE OR REPLACE TEMP TABLE
                geometric_crossings_without_shared_connector_reference AS
            SELECT a.id AS left_id, b.id AS right_id,
                   a.names.primary AS left_name, b.names.primary AS right_name,
                   a.class AS left_class, b.class AS right_class
            FROM crossing_pair_candidates pair
            JOIN crossing_segments a ON a.id = pair.left_id
            JOIN crossing_segments b ON b.id = pair.right_id
            WHERE ST_Crosses(a.geometry, b.geometry)
              AND NOT list_has_any(
                  COALESCE(list_transform(a.connectors, x -> x.connector_id),
                           []::VARCHAR[]),
                  COALESCE(list_transform(b.connectors, x -> x.connector_id),
                           []::VARCHAR[])
              )
        """)
        total = int(connection.execute(
            "SELECT COUNT(*) FROM "
            "geometric_crossings_without_shared_connector_reference"
        ).fetchone()[0])
        cursor = connection.execute("""
            SELECT * FROM geometric_crossings_without_shared_connector_reference
            ORDER BY left_id, right_id LIMIT 10
        """)
        columns = [description[0] for description in cursor.description]
        examples = [_jsonable(dict(zip(columns, row))) for row in cursor.fetchmany(10)]
        return {
            "geometric_crossings_without_shared_connector_reference": total,
            "segment_rows": segment_count,
            "segment_guard": max_segments,
            "tile_memberships": tile_memberships,
            "tile_membership_guard": max_tile_memberships,
            "tiled_bbox_candidate_pairs": candidate_count,
            "candidate_pair_guard": max_candidate_pairs,
            "tile_degrees": tile_degrees,
            "examples": examples,
            "interpretation": (
                "This only shows that two geometries cross without sharing a connector "
                "reference; it does not classify grade, bridge, tunnel, or data validity."
            ),
        }
    except duckdb.Error as exc:
        return {
            "skipped": True,
            "reason": f"bounded spatial crossing audit unavailable: {exc}",
        }


def render_markdown(report: dict[str, Any]) -> str:
    transport = report["transport_extract"]
    summary = report["snapshot_name_cluster_summary"]
    sizes = summary["snapshot_cluster_segment_count"]
    address = report["address_extract"]
    serialized = report["serialized_snapshot_estimate"]
    halo = report["halo_comparison"]
    connectors = report["connector_validation"]
    segment_sources = report["provenance_coverage"]["segments"]
    connector_sources = report["provenance_coverage"]["connectors"]
    lines = [
        "# Bounded transportation snapshot name-cluster experiment",
        "",
        f"- Overture release: `{report['overture_release']}`",
        f"- Boundary: {report['boundary']['label']}",
        f"- Polygon scan bbox: `{report['boundary']['bbox']}`; topology halo: `{report['boundary']['halo_degrees']}°`",
        f"- Road / named-road segments: **{summary['all_road_segments']:,} / {summary['named_road_segments']:,}**",
        f"- Materialized input: **{transport['materialized_parquet_bytes'] / 1024 / 1024:.1f} MiB**",
        f"- Primary names/core-touching snapshot clusters: **{summary['primary_names']:,} / {summary['core_touching_snapshot_name_clusters']:,}**",
        f"- Unique connector IDs: **{summary['unique_connector_ids']:,}**",
        f"- Names split into snapshot clusters: **{summary['names_with_multiple_snapshot_clusters']:,}**",
        f"- Snapshot cluster segment counts p50/p90/p99/max: **{sizes['p50']} / {sizes['p90']} / {sizes['p99']} / {sizes['max']}**",
        f"- Core named segments changed by halo: **{halo['core_named_segments_with_changed_core_peer_membership']:,} / {halo['core_named_segments_compared']:,}**",
        f"- Measured hot cluster+alias lookup: **{serialized['hot_cluster_lookup_bytes'] / 1024 / 1024:.2f} MiB**",
        f"- Measured full deduplicated JSON detail: **{serialized['total_bytes'] / 1024 / 1024:.2f} MiB**",
        "",
        "## Build and source evidence",
        "",
        f"- Boundary / road / connector extraction: **{report['boundary']['elapsed_seconds']:.2f}s / {transport['elapsed_seconds']:.2f}s / {report['connector_extract']['elapsed_seconds']:.2f}s**",
        f"- Connector/name cluster build: **{report['snapshot_cluster_build_seconds']:.2f}s**",
        f"- Connector references endpoint/interior/invalid: **{connectors['endpoint_connector_references']:,} / {connectors['interior_connector_references']:,} / {connectors['invalid_at_references']:,}**",
        f"- Segment root/property-specific source records: **{segment_sources['root_source_records']:,} / {segment_sources['property_specific_source_records']:,}**",
        f"- Segment source records with `between` / confidence: **{segment_sources['all_source_field_populated_records']['between']:,} / {segment_sources['all_source_field_populated_records']['confidence']:,}**",
        f"- Connector root/property-specific source records: **{connector_sources['root_source_records']:,} / {connector_sources['property_specific_source_records']:,}**",
        "- Source confidence is source-supplied and not calibrated across datasets; missing is not zero.",
        "",
        "## Architecture implications",
        "",
        "- These are release-scoped snapshot clusters, not stable real-world street IDs.",
        "- Primary names define this conservative topology grouping; common aliases live in a separate lookup.",
        "- Scoped name rules remain assertions until between/side/perspective semantics are evaluated.",
        "- Shared Overture connector IDs are the only graph edges.",
        "- Serialize segment metadata once and let cluster records reference segment IDs.",
        "- Locality/postcode must be spatially enriched. Exact name matching is useful only as a coverage diagnostic.",
        "- Frontier clusters need continuation metadata or overlapping regional partitions.",
        "",
        "## Address context diagnostic",
        "",
        f"- Full street-bearing polygon population: **{address['geometry_authoritative_population']:,}**",
        f"- Hash sample contributing normalized-name context: **{address['rows']:,}** / cap **{address['row_guard']:,}**",
        "- The sampler targets 95% of the cap before the hard LIMIT, avoiding cap-edge truncation without a global sort; therefore a result below 100,000 is expected.",
        f"- Name-level cluster coverage: **{report['address_name_context']['snapshot_clusters_with_name_level_address_context']:,} / {summary['core_touching_snapshot_name_clusters']:,} ({report['address_name_context']['coverage']:.2%})**",
        f"- Extraction: `{json.dumps(address, sort_keys=True)}`",
        f"- Diagnostic: `{json.dumps(report['address_name_context'], sort_keys=True)}`",
        "",
        "## Geometric crossings without shared connector reference",
        "",
        f"`{json.dumps(report['geometric_crossing_reference_audit'], sort_keys=True)}`",
        "",
        "## Connector feature clipping diagnostic",
        "",
        f"- Referenced connector IDs absent from the polygon+halo connector-feature extract: **{connectors['referenced_connector_ids_missing_from_bounded_connector_extract']:,} / {connectors['unique_referenced_connector_ids']:,} ({connectors['referenced_connector_ids_missing_from_bounded_connector_extract'] / connectors['unique_referenced_connector_ids']:.2%})**",
        f"- {connectors['missing_reference_warning']}",
        "",
        "## Limitations",
        "",
        "The core is Overture's pinned Boston locality dataset polygon, not a claim of legal boundary authority; snapshot clusters remain clipped at its halo, and the address join is name-level only. "
        "Conservative exact-name normalization can split abbreviations or spelling variants, and unnamed intermediate segments can fragment a named road. "
        "Results are experimental sizing/topology evidence, not a production shard or quality benchmark.",
        "",
    ]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.release != PINNED_RELEASE:
        raise ValueError(
            f"this experiment is pinned to {PINNED_RELEASE}; got {args.release}"
        )
    guard_names = (
        "max_transport_rows", "max_transport_bytes", "max_connector_rows",
        "max_connector_bytes", "max_address_rows", "max_crossing_segments",
        "max_crossing_tile_memberships",
        "max_crossing_candidate_pairs",
        "remote_time_cap_seconds",
    )
    for name in guard_names:
        if getattr(args, name) <= 0:
            raise ValueError(f"{name} must be positive")
    if args.halo <= 0:
        raise ValueError("halo must be positive so instability can be measured")
    if args.crossing_tile_degrees <= 0:
        raise ValueError("crossing_tile_degrees must be positive")
    if args.max_address_rows > ADDRESS_HARD_MAX_ROWS:
        raise ValueError(
            f"max_address_rows cannot exceed hard cap {ADDRESS_HARD_MAX_ROWS:,}"
        )

    connection = duckdb.connect()
    configure_remote(connection)
    interrupted = threading.Event()

    def interrupt_remote_work() -> None:
        interrupted.set()
        connection.interrupt()

    timer = threading.Timer(args.remote_time_cap_seconds, interrupt_remote_work)
    timer.daemon = True
    timer.start()
    try:
      with tempfile.TemporaryDirectory(prefix="overture-transport-") as directory:
        connection.execute(
            "SET temp_directory = "
            + _sql_literal(str(Path(directory) / "duckdb-spill"))
        )
        boundary_parquet = Path(directory) / "boundary.parquet"
        parquet = Path(directory) / "segments.parquet"
        connector_parquet = Path(directory) / "connectors.parquet"
        boundary = extract_boundary(
            connection, boundary_parquet, args.release
        )
        bbox = tuple(boundary["bbox"])
        transport_extract = extract_transportation(
            connection, parquet, args.release, boundary_parquet, bbox, args.halo,
            args.max_transport_rows, args.max_transport_bytes,
        )
        connector_extract = extract_connectors(
            connection, connector_parquet, args.release, boundary_parquet,
            bbox, args.halo,
            args.max_connector_rows, args.max_connector_bytes,
        )
        segments = load_segments(connection, parquet)
        connector_records = load_connector_records(connection, connector_parquet)
        started = time.monotonic()
        clusters = build_snapshot_name_clusters(
            segments, args.release, _expanded_bbox(bbox, args.halo)
        )
        cluster_seconds = round(time.monotonic() - started, 3)
        alias_lookup = build_alias_lookup(segments, clusters)
        crossing = geometric_crossing_reference_audit(
            connection, parquet, args.max_crossing_segments,
            args.max_crossing_tile_memberships,
            args.max_crossing_candidate_pairs, args.crossing_tile_degrees,
        )
        address_rows, address_extract = extract_address_context(
            connection, args.release, boundary_parquet, bbox, args.max_address_rows
        )
    except duckdb.Error as exc:
        if interrupted.is_set():
            raise RuntimeError(
                f"remote experiment exceeded {args.remote_time_cap_seconds}s wall-clock cap"
            ) from exc
        raise
    finally:
        timer.cancel()
        connection.close()

    report = {
        "report_version": REPORT_VERSION,
        "overture_release": args.release,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "boundary": {
            "bbox": list(bbox),
            "halo_degrees": args.halo,
            "label": BOUNDARY_LABEL,
            **boundary,
        },
        "transport_extract": transport_extract,
        "connector_extract": connector_extract,
        "snapshot_cluster_build_seconds": cluster_seconds,
        "snapshot_name_cluster_summary": summarize_snapshot_name_clusters(
            segments, clusters
        ),
        "common_alias_lookup_summary": {
            "keys": len(alias_lookup),
            "cluster_references": sum(len(value) for value in alias_lookup.values()),
            "separate_from_snapshot_cluster_records": True,
        },
        "scoped_name_rule_assertions": rule_assertion_summary(segments),
        "connector_validation": connector_validation(
            segments, {str(record["id"]) for record in connector_records}
        ),
        "provenance_coverage": {
            "segments": provenance_coverage(segments),
            "connectors": provenance_coverage(connector_records),
        },
        "serialized_snapshot_estimate": serialized_snapshot_estimate(
            segments, clusters, alias_lookup
        ),
        "halo_comparison": compare_halo_snapshots(
            segments, args.release, bbox, args.halo
        ),
        "geometric_crossing_reference_audit": crossing,
        "address_extract": address_extract,
        "address_name_context": (
            {"skipped": True, "reason": address_extract["reason"]}
            if address_extract.get("skipped")
            else address_name_context(clusters, address_rows)
        ),
        "execution": {
            "invocation": [str(value) for value in sys.argv],
            "python_version": platform.python_version(),
            "duckdb_version": duckdb.__version__,
            "platform": platform.platform(),
            "remote_wall_clock_cap_seconds": args.remote_time_cap_seconds,
        },
        "architecture_warning": (
            "No production shards, APIs, Worker paths, R2 objects, or deploy state were changed."
        ),
    }
    return sanitize_aggregate_report(_jsonable(report))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", default=PINNED_RELEASE)
    parser.add_argument("--halo", type=float, default=DEFAULT_HALO_DEGREES)
    parser.add_argument("--max-transport-rows", type=int, default=250_000)
    parser.add_argument("--max-transport-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--max-connector-rows", type=int, default=250_000)
    parser.add_argument("--max-connector-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--max-address-rows", type=int, default=ADDRESS_HARD_MAX_ROWS)
    parser.add_argument("--max-crossing-segments", type=int, default=25_000)
    parser.add_argument("--max-crossing-tile-memberships", type=int, default=1_000_000)
    parser.add_argument("--max-crossing-candidate-pairs", type=int, default=2_000_000)
    parser.add_argument("--crossing-tile-degrees", type=float, default=0.01)
    parser.add_argument(
        "--remote-time-cap-seconds", type=int,
        default=DEFAULT_REMOTE_TIME_CAP_SECONDS,
    )
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = run(args)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.write_text(payload)
    else:
        print(payload, end="")
    if args.markdown_out:
        args.markdown_out.write_text(render_markdown(report))


if __name__ == "__main__":
    main()
