#!/usr/bin/env python3
"""Export the validated Monaco subset through the production SQL transforms."""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import hashlib
import json
import math
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import duckdb


SCRIPT_DIR = Path(__file__).resolve().parent
CONTRACT_PATH = SCRIPT_DIR / "monaco_smoke_contract.json"
DIVISION_TEMPLATE = SCRIPT_DIR / "download_divisions_global.sql"
AREA_TEMPLATE = SCRIPT_DIR / "download_divisions_area.sql"
PLACEHOLDER_RE = re.compile(r"__[A-Z_]+__")


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _command_output(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def load_contract(path: Path = CONTRACT_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if payload.get("contract_version") != 1 or payload.get("country_code") != "MC":
        raise RuntimeError("invalid Monaco smoke contract header")
    for key in ("required_divisions", "required_areas"):
        values = payload.get(key)
        if not isinstance(values, list) or not values:
            raise RuntimeError(f"Monaco smoke contract has no {key}")
        ids = [item.get("id") for item in values if isinstance(item, dict)]
        if len(ids) != len(values) or len(set(ids)) != len(ids):
            raise RuntimeError(f"Monaco smoke contract has invalid {key} IDs")
    return payload


def _validate_bbox(
    row: dict[str, Any], label: str
) -> tuple[float, float, float, float]:
    values = tuple(float(row[name]) for name in ("xmin", "ymin", "xmax", "ymax"))
    xmin, ymin, xmax, ymax = values
    if not all(math.isfinite(value) for value in values):
        raise RuntimeError(f"{label} has a non-finite bbox")
    if not (-180 <= xmin <= xmax <= 180 and -90 <= ymin <= ymax <= 90):
        raise RuntimeError(f"{label} has an invalid bbox {values!r}")
    return values


def _hierarchy_ids(raw: str | None, label: str) -> set[str]:
    if not raw:
        raise RuntimeError(f"{label} has no hierarchy")
    try:
        hierarchies = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{label} has invalid hierarchy JSON") from error
    if not isinstance(hierarchies, list) or not hierarchies:
        raise RuntimeError(f"{label} has no hierarchy paths")
    result: set[str] = set()
    for hierarchy in hierarchies:
        if not isinstance(hierarchy, list) or not hierarchy:
            raise RuntimeError(f"{label} has an invalid hierarchy path")
        for entry in hierarchy:
            division_id = entry.get("division_id") if isinstance(entry, dict) else None
            if not isinstance(division_id, str) or not division_id:
                raise RuntimeError(f"{label} hierarchy has no division ID")
            result.add(division_id)
    return result


def validate_divisions(
    rows: list[dict[str, Any]],
    contract: dict[str, Any],
    core_ids: set[str] | None = None,
) -> tuple[set[str], list[tuple[float, float, float, float]]]:
    core_ids = core_ids or {row["id"] for row in rows}
    by_id: dict[str, dict[str, Any]] = {}
    closure: set[str] = set()
    bboxes = []
    for row in rows:
        division_id = row["id"]
        if division_id in by_id:
            raise RuntimeError(f"duplicate Monaco division {division_id}")
        by_id[division_id] = row
        if division_id in core_ids and row["country"] != "MC":
            raise RuntimeError(f"division {division_id} escaped country MC")
        if not row.get("wkb_sha256"):
            raise RuntimeError(f"division {division_id} has no geometry hash")
        bbox = _validate_bbox(row, f"division {division_id}")
        bboxes.append(bbox)
        lon, lat = float(row["lon"]), float(row["lat"])
        if not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
            raise RuntimeError(f"division {division_id} point is outside its bbox")
        closure.update(
            _hierarchy_ids(row["hierarchies_json"], f"division {division_id}")
        )
        parent_id = row.get("parent_division_id")
        if parent_id:
            closure.add(parent_id)

    required = {item["id"]: item for item in contract["required_divisions"]}
    missing = sorted(set(required) - set(by_id))
    if missing:
        raise RuntimeError(f"required Monaco divisions are missing: {missing}")
    for division_id, expected in required.items():
        if by_id[division_id]["subtype"] != expected["subtype"]:
            raise RuntimeError(
                f"required Monaco division {division_id} changed subtype"
            )
    unresolved = sorted(closure - set(by_id))
    if unresolved:
        raise RuntimeError(f"Monaco hierarchy closure is unresolved: {unresolved}")
    for division_id, row in by_id.items():
        parent_id = row.get("parent_division_id")
        if parent_id and parent_id not in by_id:
            raise RuntimeError(f"division {division_id} parent is outside the closure")
    return set(by_id), bboxes


def validate_areas(
    rows: list[dict[str, Any]], contract: dict[str, Any], division_ids: set[str]
) -> tuple[
    list[tuple[float, float, float, float]],
    collections.Counter[tuple[Any, ...]],
]:
    by_id: dict[str, dict[str, Any]] = {}
    bboxes = []
    for row in rows:
        area_id = row["id"]
        if area_id in by_id:
            raise RuntimeError(f"duplicate Monaco division area {area_id}")
        by_id[area_id] = row
        if row["division_id"] not in division_ids:
            raise RuntimeError(f"division area {area_id} escaped the Monaco closure")
        if not row.get("wkb_sha256"):
            raise RuntimeError(f"division area {area_id} has no geometry hash")
        bbox = _validate_bbox(row, f"division area {area_id}")
        geometry_bbox = tuple(
            float(row[name])
            for name in ("geom_xmin", "geom_ymin", "geom_xmax", "geom_ymax")
        )
        tolerance = 2e-6
        if not (
            bbox[0] <= geometry_bbox[0] + tolerance
            and bbox[1] <= geometry_bbox[1] + tolerance
            and bbox[2] >= geometry_bbox[2] - tolerance
            and bbox[3] >= geometry_bbox[3] - tolerance
        ):
            raise RuntimeError(
                f"division area {area_id} bbox does not cover its geometry"
            )
        area = float(row["geometry_area"])
        if not math.isfinite(area) or area <= 0:
            raise RuntimeError(f"division area {area_id} has an invalid geometry area")
        bboxes.append(bbox)

    required = {item["id"]: item for item in contract["required_areas"]}
    missing = sorted(set(required) - set(by_id))
    if missing:
        raise RuntimeError(f"required Monaco division areas are missing: {missing}")
    for area_id, expected in required.items():
        row = by_id[area_id]
        if (
            row["division_id"] != expected["division_id"]
            or row["subtype"] != expected["subtype"]
        ):
            raise RuntimeError(
                f"required Monaco division area {area_id} changed identity"
            )
    signatures = collections.Counter(
        (
            row["division_id"],
            row["subtype"],
            float(row["geom_xmin"]),
            float(row["geom_ymin"]),
            float(row["geom_xmax"]),
            float(row["geom_ymax"]),
            float(row["geometry_area"]),
        )
        for row in rows
    )
    return bboxes, signatures


def conservative_envelope(
    bboxes: list[tuple[float, float, float, float]], epsilon: float = 1e-6
) -> tuple[float, float, float, float]:
    if not bboxes:
        raise RuntimeError("cannot derive a Monaco envelope without bboxes")
    return (
        min(value[0] for value in bboxes) - epsilon,
        min(value[1] for value in bboxes) - epsilon,
        max(value[2] for value in bboxes) + epsilon,
        max(value[3] for value in bboxes) + epsilon,
    )


def render_sql(template: Path, replacements: dict[str, str]) -> str:
    rendered = template.read_text()
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    remaining = sorted(set(PLACEHOLDER_RE.findall(rendered)))
    if remaining:
        raise RuntimeError(
            f"unrendered SQL placeholders in {template.name}: {remaining}"
        )
    return rendered


def _rows(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _referenced_division_ids(rows: list[dict[str, Any]]) -> set[str]:
    referenced: set[str] = set()
    for row in rows:
        referenced.update(
            _hierarchy_ids(row["hierarchies_json"], f"division {row['id']}")
        )
        if row.get("parent_division_id"):
            referenced.add(row["parent_division_id"])
    return referenced


def _id_list(values: set[str] | list[str]) -> str:
    if not values:
        raise RuntimeError("cannot render an empty ID list")
    return ", ".join(_sql_literal(value) for value in sorted(values))


def _required_division_ids(contract: dict[str, Any]) -> set[str]:
    return {item["id"] for item in contract["required_divisions"]} | {
        item["division_id"] for item in contract["required_areas"]
    }


def _country_or_ids_filter(
    country_column: str, id_column: str, ids: set[str]
) -> str:
    return (
        f"({country_column} = 'MC' OR {id_column} IN ({_id_list(ids)}))"
    )


def _division_select(source: str, predicate: str) -> str:
    return f"""
        SELECT id, subtype, country, region, population, names.primary AS name,
               parent_division_id, to_json(hierarchies) AS hierarchies_json,
               bbox.xmin AS xmin, bbox.ymin AS ymin,
               bbox.xmax AS xmax, bbox.ymax AS ymax,
               ST_X(geometry) AS lon, ST_Y(geometry) AS lat,
               sha256(ST_AsWKB(geometry)) AS wkb_sha256
        FROM read_parquet({_sql_literal(source)}, hive_partitioning=true)
        WHERE {predicate}
        ORDER BY id
    """


def _area_select(source: str, predicate: str) -> str:
    return f"""
        SELECT id, division_id, subtype, country,
               bbox.xmin AS xmin, bbox.ymin AS ymin,
               bbox.xmax AS xmax, bbox.ymax AS ymax,
               ST_XMin(geometry) AS geom_xmin, ST_YMin(geometry) AS geom_ymin,
               ST_XMax(geometry) AS geom_xmax, ST_YMax(geometry) AS geom_ymax,
               ST_Area(geometry) AS geometry_area,
               sha256(ST_AsWKB(geometry)) AS wkb_sha256
        FROM read_parquet({_sql_literal(source)}, hive_partitioning=true)
        WHERE {predicate}
        ORDER BY id
    """


def _area_ownership_select(source: str, predicate: str) -> str:
    return f"""
        SELECT id, division_id
        FROM read_parquet({_sql_literal(source)}, hive_partitioning=true)
        WHERE {predicate}
        ORDER BY id
    """


def _ownership_filter(division_ids: set[str]) -> str:
    return f"division_id IN ({_id_list(division_ids)})"


def _validate_ownership_rows(
    rows: list[dict[str, Any]],
    division_ids: set[str],
    known_area_ids: set[str],
) -> set[str]:
    if len(rows) != len({row["id"] for row in rows}):
        raise RuntimeError("duplicate area ID in the Monaco ownership closure")
    for row in rows:
        if row["division_id"] not in division_ids:
            raise RuntimeError(
                f"division area {row['id']} escaped the ownership closure"
            )
    return {row["id"] for row in rows if row["id"] not in known_area_ids}


def _remote_query(sql: str, threads: int = 2) -> list[dict[str, Any]]:
    con = duckdb.connect()
    try:
        con.execute("LOAD httpfs; LOAD spatial;")
        con.execute(f"SET s3_region='us-west-2'; SET threads={threads};")
        return _rows(con.execute(sql))
    finally:
        con.close()


def _run_duckdb(sql: str, label: str) -> float:
    started = time.monotonic()
    result = subprocess.run(
        ["duckdb"], input=sql, text=True, capture_output=True, check=False
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode:
        raise RuntimeError(f"{label} failed:\n{result.stderr[-4000:]}")
    if result.stderr:
        print(result.stderr, end="")
    return time.monotonic() - started


def _source_path(release: str, feature_type: str) -> str:
    return (
        "s3://overturemaps-us-west-2/release/"
        f"{release}/theme=divisions/type={feature_type}/*"
    )


def _json_nodes(value: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(value, dict):
        nodes.append(value)
        for child in value.get("children", []):
            nodes.extend(_json_nodes(child))
    elif isinstance(value, list):
        for child in value:
            nodes.extend(_json_nodes(child))
    return nodes


def _scan_node(value: Any) -> dict[str, Any]:
    candidates = []
    for node in _json_nodes(value):
        name = str(node.get("name") or node.get("operator_name") or "").upper()
        operator_type = str(node.get("operator_type") or "").upper()
        if "PARQUET" in name or "TABLE_SCAN" in operator_type:
            candidates.append(node)
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected one Parquet scan operator, found {len(candidates)}"
        )
    return candidates[0]


def _filters_from_scan(node: dict[str, Any]) -> str:
    return json.dumps(node.get("extra_info", {}).get("Filters", "")).lower()


def _scan_filter_flags(node: dict[str, Any]) -> tuple[bool, bool]:
    filters = _filters_from_scan(node).replace(" ", "")
    country = "country='mc'" in filters
    bbox = all(
        field in filters
        for field in ("bbox.xmin", "bbox.ymin", "bbox.xmax", "bbox.ymax")
    )
    return country, bbox


def _profile_scan(
    con: duckdb.DuckDBPyConnection,
    source: str,
    predicate: str,
    envelope: tuple[float, float, float, float],
    artifact_stem: Path,
    predicate_description: str,
    require_country_filter: bool = True,
) -> dict[str, Any]:
    total_files = con.execute("SELECT count(*) FROM glob(?)", [source]).fetchone()[0]
    scan_sql = (
        f"SELECT count(*) FROM read_parquet({_sql_literal(source)}, "
        f"hive_partitioning=true, filename=true) WHERE {predicate}"
    )
    plan = json.loads(con.execute(f"EXPLAIN (FORMAT JSON) {scan_sql}").fetchone()[-1])
    plan_scan = _scan_node(plan)
    plan_country, plan_bbox = _scan_filter_flags(plan_scan)
    if not plan_bbox or (require_country_filter and not plan_country):
        raise RuntimeError(
            "Monaco country and bbox predicates were not attached to the "
            "Parquet scan"
        )
    artifact_stem.parent.mkdir(parents=True, exist_ok=True)
    plan_path = artifact_stem.with_name(artifact_stem.name + "-plan.json")
    profile_path = artifact_stem.with_name(artifact_stem.name + "-profile.json")
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    con.execute("SET enable_profiling='json'")
    con.execute(f"SET profiling_output={_sql_literal(str(profile_path))}")
    result_count = con.execute(scan_sql).fetchall()[0][0]
    con.execute("SET enable_profiling='no_output'")
    profile = json.loads(profile_path.read_text())
    profile_scan = _scan_node(profile)
    profile_country, profile_bbox = _scan_filter_flags(profile_scan)
    if not profile_bbox or (require_country_filter and not profile_country):
        raise RuntimeError(
            "executed Parquet scan did not own the Monaco country and bbox predicates"
        )
    touched_files = int(
        profile_scan.get("extra_info", {}).get("Total Files Read", total_files)
    )
    rows_scanned_counter = int(profile_scan.get("operator_rows_scanned", 0))
    xmin, ymin, xmax, ymax = envelope
    row_groups = con.execute(
        f"""
        WITH row_groups AS (
            SELECT file_name, row_group_id,
                   max(row_group_bytes) AS row_group_bytes,
                   max(row_group_num_rows) AS row_group_rows,
                   max(CASE WHEN path_in_schema='country' THEN stats_min_value END) AS country_min,
                   max(CASE WHEN path_in_schema='country' THEN stats_max_value END) AS country_max,
                   max(CASE WHEN path_in_schema='bbox, xmin' THEN try_cast(stats_min_value AS DOUBLE) END) AS xmin_min,
                   max(CASE WHEN path_in_schema='bbox, xmax' THEN try_cast(stats_max_value AS DOUBLE) END) AS xmax_max,
                   max(CASE WHEN path_in_schema='bbox, ymin' THEN try_cast(stats_min_value AS DOUBLE) END) AS ymin_min,
                   max(CASE WHEN path_in_schema='bbox, ymax' THEN try_cast(stats_max_value AS DOUBLE) END) AS ymax_max
            FROM parquet_metadata({_sql_literal(source)})
            GROUP BY file_name, row_group_id
        ), classified AS (
            SELECT *,
                   country_min <= 'MC' AND country_max >= 'MC'
                   AND xmax_max >= {xmin!r} AND xmin_min <= {xmax!r}
                   AND ymax_max >= {ymin!r} AND ymin_min <= {ymax!r} AS candidate
            FROM row_groups
        )
        SELECT count(*) AS total_row_groups,
               count(*) FILTER (WHERE candidate) AS metadata_eligible_row_groups,
               sum(row_group_bytes) AS total_row_group_bytes,
               sum(row_group_bytes) FILTER (WHERE candidate) AS metadata_eligible_row_group_bytes,
               sum(row_group_rows) AS total_rows,
               sum(row_group_rows) FILTER (WHERE candidate) AS metadata_eligible_row_group_rows,
               count(*) FILTER (
                   WHERE country_min IS NULL OR country_max IS NULL
                      OR xmin_min IS NULL OR xmax_max IS NULL
                      OR ymin_min IS NULL OR ymax_max IS NULL
               ) AS row_groups_missing_stats
        FROM classified
        """
    ).fetchone()
    if row_groups[6]:
        raise RuntimeError(
            f"Monaco source has {row_groups[6]} row groups without required statistics"
        )
    if not (0 < row_groups[1] < row_groups[0]):
        raise RuntimeError(
            "Monaco predicate did not prune Parquet row groups: "
            f"{row_groups[1]}/{row_groups[0]}"
        )
    return {
        "source": source,
        "total_files": total_files,
        "touched_files": touched_files,
        "total_row_groups": row_groups[0],
        "metadata_eligible_row_groups": row_groups[1],
        "total_row_group_bytes": row_groups[2],
        "metadata_eligible_row_group_bytes": row_groups[3],
        "total_rows": row_groups[4],
        "metadata_eligible_row_group_rows": row_groups[5],
        # DuckDB 1.5.1 reports this counter as twice the entire Parquet
        # cardinality for this remote nested-column scan. Preserve the raw
        # diagnostic, but do not mislabel it as row-group pruning evidence.
        "duckdb_rows_scanned_counter": rows_scanned_counter,
        "result_rows": result_count,
        "plan_has_country_filter": profile_country,
        "plan_has_bbox_filter": profile_bbox,
        "profiled_predicate": predicate_description,
        "plan_path": str(plan_path),
        "plan_sha256": _sha256(plan_path),
        "profile_path": str(profile_path),
        "profile_sha256": _sha256(profile_path),
    }


def _validate_outputs(
    forward: Path,
    reverse: Path,
    contract: dict[str, Any],
    expected_reverse_components: collections.Counter[tuple[Any, ...]],
) -> dict[str, Any]:
    con = duckdb.connect()
    try:
        forward_rows = con.execute(
            "SELECT gers_id, subtype, country, search_name, search_context "
            "FROM read_parquet(?) ORDER BY gers_id",
            [str(forward)],
        ).fetchall()
        reverse_rows = con.execute(
            "SELECT gers_id, subtype, country, bbox_xmin, bbox_ymin, bbox_xmax, "
            "bbox_ymax, area "
            "FROM read_parquet(?) ORDER BY gers_id, bbox_xmin, bbox_ymin",
            [str(reverse)],
        ).fetchall()
    finally:
        con.close()
    required_forward = {item["id"] for item in contract["required_divisions"]}
    actual_forward = {row[0] for row in forward_rows}
    if not required_forward.issubset(actual_forward):
        raise RuntimeError("transformed forward export lost a required Monaco division")
    if any(row[2] != "MC" for row in forward_rows + reverse_rows):
        raise RuntimeError("transformed Monaco export contains a foreign country")
    actual_reverse_components = collections.Counter(
        (row[0], row[1], row[3], row[4], row[5], row[6], row[7]) for row in reverse_rows
    )
    if actual_reverse_components != expected_reverse_components:
        raise RuntimeError(
            "transformed reverse export changed area component multiplicity"
        )
    return {"forward_rows": len(forward_rows), "reverse_rows": len(reverse_rows)}


def export_monaco(
    release: str,
    forward_output: Path,
    reverse_output: Path,
    profile_output: Path,
    authoritative_forward_output: Path | None = None,
    authoritative_reverse_output: Path | None = None,
) -> dict[str, Any]:
    if (authoritative_forward_output is None) != (
        authoritative_reverse_output is None
    ):
        raise RuntimeError("both authoritative baseline outputs must be supplied")
    started = time.monotonic()
    contract = load_contract()
    contract_division_ids = _required_division_ids(contract)
    division_source = _source_path(release, "division")
    area_source = _source_path(release, "division_area")
    con = duckdb.connect()
    try:
        print("[monaco] loading DuckDB extensions", flush=True)
        con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
        con.execute("SET s3_region='us-west-2'; SET threads=2;")
        stage_started = time.monotonic()
        print(
            "[monaco] loading authoritative division and division-area rows",
            flush=True,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            division_future = executor.submit(
                _remote_query, _division_select(division_source, "country='MC'")
            )
            area_future = executor.submit(
                _remote_query, _area_select(area_source, "country='MC'")
            )
            ownership_future = executor.submit(
                _remote_query,
                _area_ownership_select(
                    area_source,
                    _ownership_filter(contract_division_ids),
                ),
                8,
            )
            division_rows = division_future.result()
            area_rows = area_future.result()
            ownership_rows = ownership_future.result()
        print(
            f"[monaco] loaded {len(division_rows)} divisions and "
            f"{len(area_rows)} division areas in "
            f"{time.monotonic() - stage_started:.3f}s",
            flush=True,
        )

        stage_started = time.monotonic()
        print("[monaco] validating authoritative ID closures", flush=True)
        core_division_ids = {row["id"] for row in division_rows}
        required_division_ids = contract_division_ids | {
            row["division_id"] for row in area_rows
        }
        for _ in range(8):
            wanted = required_division_ids | _referenced_division_ids(division_rows)
            missing = wanted - {row["id"] for row in division_rows}
            if not missing:
                break
            supplemental = _rows(
                con.execute(
                    _division_select(division_source, f"id IN ({_id_list(missing)})")
                )
            )
            if {row["id"] for row in supplemental} != missing:
                raise RuntimeError(
                    f"Monaco hierarchy closure rows are missing: {sorted(missing)}"
                )
            division_rows.extend(supplemental)
        else:
            raise RuntimeError(
                "Monaco hierarchy closure exceeded eight expansion rounds"
            )
        division_ids, division_bboxes = validate_divisions(
            division_rows, contract, core_division_ids
        )
        required_area_ids = {item["id"] for item in contract["required_areas"]}
        missing_area_ids = required_area_ids - {row["id"] for row in area_rows}
        if missing_area_ids:
            supplemental_areas = _rows(
                con.execute(
                    _area_select(area_source, f"id IN ({_id_list(missing_area_ids)})")
                )
            )
            if {row["id"] for row in supplemental_areas} != missing_area_ids:
                raise RuntimeError(
                    "required Monaco division-area closure rows are missing: "
                    f"{sorted(missing_area_ids)}"
                )
            area_rows.extend(supplemental_areas)
        extra_ownership_ids = division_ids - contract_division_ids
        if extra_ownership_ids:
            ownership_rows.extend(
                _remote_query(
                    _area_ownership_select(
                        area_source,
                        _ownership_filter(extra_ownership_ids),
                    ),
                    8,
                )
            )
        areas_by_id = {row["id"]: row for row in area_rows}
        missing_owned_ids = _validate_ownership_rows(
            ownership_rows, division_ids, set(areas_by_id)
        )
        if missing_owned_ids:
            owned_supplement = _rows(
                con.execute(
                    _area_select(
                        area_source,
                        f"id IN ({_id_list(missing_owned_ids)})",
                    )
                )
            )
            if {row["id"] for row in owned_supplement} != missing_owned_ids:
                raise RuntimeError(
                    "division-owned Monaco area rows disappeared during exact fetch"
                )
            areas_by_id.update({row["id"]: row for row in owned_supplement})
        area_rows = list(areas_by_id.values())
        area_bboxes, area_signatures = validate_areas(
            area_rows, contract, division_ids
        )
        reverse_eligible_ids = {
            row["id"]
            for row in division_rows
            if row["name"] is not None
            and (
                row["subtype"] in {"country", "region", "county"}
                or (
                    row["subtype"] == "locality"
                    and int(row["population"] or 0) >= 50_000
                )
            )
        }
        expected_reverse_components = collections.Counter(
            signature
            for signature, count in area_signatures.items()
            for _ in range(count)
            if signature[0] in reverse_eligible_ids
        )
        print(
            f"[monaco] validated {len(division_rows)} divisions and "
            f"{len(area_rows)} division areas in "
            f"{time.monotonic() - stage_started:.3f}s",
            flush=True,
        )
        envelope = conservative_envelope(division_bboxes + area_bboxes)
        xmin, ymin, xmax, ymax = envelope
        supplemental_division_ids = division_ids - core_division_ids
        division_exact = "country = 'MC'"
        if supplemental_division_ids:
            division_exact = (
                f"({division_exact} OR id IN ({_id_list(supplemental_division_ids)}))"
            )
        area_exact = _country_or_ids_filter(
            "a.country", "a.division_id", division_ids
        )
        division_filter = (
            f"{division_exact} "
            f"AND bbox.xmax >= {xmin!r} AND bbox.xmin <= {xmax!r} "
            f"AND bbox.ymax >= {ymin!r} AND bbox.ymin <= {ymax!r}"
        )
        area_filter = (
            f"{area_exact} "
            f"AND a.bbox.xmax >= {xmin!r} AND a.bbox.xmin <= {xmax!r} "
            f"AND a.bbox.ymax >= {ymin!r} AND a.bbox.ymin <= {ymax!r}"
        )
        print("[monaco] profiling division Parquet pruning", flush=True)
        division_profile = _profile_scan(
            con,
            division_source,
            division_filter,
            envelope,
            profile_output.parent / "monaco-division-scan",
            "rendered division country/required-ID closure plus bbox",
        )
        print(
            "[monaco] division row groups "
            f"{division_profile['metadata_eligible_row_groups']}/"
            f"{division_profile['total_row_groups']}",
            flush=True,
        )
        print("[monaco] profiling division-area Parquet pruning", flush=True)
        area_profile = _profile_scan(
            con,
            area_source,
            area_filter.replace("a.", ""),
            envelope,
            profile_output.parent / "monaco-division-area-scan",
            (
                "actual rendered country-or-division_id ownership predicate plus bbox"
            ),
            False,
        )
        print(
            "[monaco] division-area row groups "
            f"{area_profile['metadata_eligible_row_groups']}/"
            f"{area_profile['total_row_groups']}",
            flush=True,
        )
        scan_profiles = {"division": division_profile, "division_area": area_profile}
    finally:
        con.close()

    for output in (forward_output, reverse_output):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.unlink(missing_ok=True)
    if "'" in str(forward_output) or "'" in str(reverse_output):
        raise RuntimeError("smoke export paths may not contain single quotes")

    forward_sql = render_sql(
        DIVISION_TEMPLATE,
        {
            "__OVERTURE_RELEASE__": release,
            "__DIVISION_FILTER__": division_filter,
            "__OUTPUT_PATH__": str(forward_output),
        },
    )
    reverse_sql = render_sql(
        AREA_TEMPLATE,
        {
            "__OVERTURE_RELEASE__": release,
            "__DIVISION_FILTER__": division_filter,
            "__AREA_FILTER__": area_filter,
            "__OUTPUT_PATH__": str(reverse_output),
        },
    )
    print("[monaco] running production forward transform", flush=True)
    forward_seconds = _run_duckdb(forward_sql, "Monaco forward export")
    print("[monaco] running production reverse transform", flush=True)
    reverse_seconds = _run_duckdb(reverse_sql, "Monaco reverse export")
    output_counts = _validate_outputs(
        forward_output, reverse_output, contract, expected_reverse_components
    )
    smoke_total_seconds = time.monotonic() - started
    authoritative_baseline = None
    if authoritative_forward_output and authoritative_reverse_output:
        for output in (
            authoritative_forward_output,
            authoritative_reverse_output,
        ):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.unlink(missing_ok=True)
            if "'" in str(output):
                raise RuntimeError("authoritative export paths may not contain quotes")
        authoritative_forward_sql = render_sql(
            DIVISION_TEMPLATE,
            {
                "__OVERTURE_RELEASE__": release,
                "__DIVISION_FILTER__": division_exact,
                "__OUTPUT_PATH__": str(authoritative_forward_output),
            },
        )
        authoritative_area_exact = "a.country = 'MC'"
        supplemental_area_ids = {
            row["id"] for row in area_rows if row["country"] != "MC"
        }
        if supplemental_area_ids:
            authoritative_area_exact = _country_or_ids_filter(
                "a.country", "a.id", supplemental_area_ids
            )
        authoritative_reverse_sql = render_sql(
            AREA_TEMPLATE,
            {
                "__OVERTURE_RELEASE__": release,
                "__DIVISION_FILTER__": division_exact,
                "__AREA_FILTER__": authoritative_area_exact,
                "__OUTPUT_PATH__": str(authoritative_reverse_output),
            },
        )
        print("[monaco] running no-bbox authoritative transforms", flush=True)
        baseline_started = time.monotonic()
        _run_duckdb(authoritative_forward_sql, "authoritative Monaco forward export")
        _run_duckdb(authoritative_reverse_sql, "authoritative Monaco reverse export")
        baseline_counts = _validate_outputs(
            authoritative_forward_output,
            authoritative_reverse_output,
            contract,
            expected_reverse_components,
        )
        authoritative_baseline = {
            "predicate": (
                "validated country plus exact discovered supplemental IDs without bbox"
            ),
            "timing_seconds": round(time.monotonic() - baseline_started, 3),
            "forward": {
                "path": str(authoritative_forward_output),
                "sha256": _sha256(authoritative_forward_output),
                "size_bytes": authoritative_forward_output.stat().st_size,
                "rows": baseline_counts["forward_rows"],
                "rendered_sql_sha256": _sha256_text(authoritative_forward_sql),
            },
            "reverse": {
                "path": str(authoritative_reverse_output),
                "sha256": _sha256(authoritative_reverse_output),
                "size_bytes": authoritative_reverse_output.stat().st_size,
                "rows": baseline_counts["reverse_rows"],
                "rendered_sql_sha256": _sha256_text(authoritative_reverse_sql),
            },
        }
    provenance_command = [
        "python",
        "scripts/download_divisions_smoke.py",
        "--release",
        release,
        "--forward-output",
        str(forward_output),
        "--reverse-output",
        str(reverse_output),
        "--profile-output",
        str(profile_output),
    ]
    if authoritative_forward_output and authoritative_reverse_output:
        provenance_command.extend(
            [
                "--authoritative-forward-output",
                str(authoritative_forward_output),
                "--authoritative-reverse-output",
                str(authoritative_reverse_output),
            ]
        )
    report = {
        "contract_version": contract["contract_version"],
        "country_code": "MC",
        "overture_release": release,
        "required_division_ids": sorted(
            item["id"] for item in contract["required_divisions"]
        ),
        "required_area_ids": sorted(item["id"] for item in contract["required_areas"]),
        "selected_division_ids": sorted(division_ids),
        "selected_area_count": len(area_rows),
        "selected_area_ids": sorted(row["id"] for row in area_rows),
        "derived_bbox": list(envelope),
        "source_geometry_sha256": {
            "divisions": {
                row["id"]: row["wkb_sha256"]
                for row in sorted(division_rows, key=lambda row: row["id"])
            },
            "division_areas": {
                row["id"]: row["wkb_sha256"]
                for row in sorted(area_rows, key=lambda row: row["id"])
            },
        },
        "scan_profiles": scan_profiles,
        "timings_seconds": {
            "forward_transform": round(forward_seconds, 3),
            "reverse_transform": round(reverse_seconds, 3),
            "total": round(smoke_total_seconds, 3),
        },
        "outputs": {
            "forward": {
                "path": str(forward_output),
                "sha256": _sha256(forward_output),
                "size_bytes": forward_output.stat().st_size,
                "rows": output_counts["forward_rows"],
            },
            "reverse": {
                "path": str(reverse_output),
                "sha256": _sha256(reverse_output),
                "size_bytes": reverse_output.stat().st_size,
                "rows": output_counts["reverse_rows"],
            },
        },
        "sql_sha256": {
            "forward": _sha256(DIVISION_TEMPLATE),
            "reverse": _sha256(AREA_TEMPLATE),
            "contract": _sha256(CONTRACT_PATH),
        },
        "rendered_sql_sha256": {
            "forward": _sha256_text(forward_sql),
            "reverse": _sha256_text(reverse_sql),
        },
        "provenance": {
            "git_sha": _command_output(["git", "rev-parse", "HEAD"]),
            "duckdb_python_version": duckdb.__version__,
            "duckdb_cli_version": _command_output(["duckdb", "--version"]),
            "command": provenance_command,
        },
    }
    if authoritative_baseline:
        report["authoritative_baseline"] = authoritative_baseline
    profile_output.parent.mkdir(parents=True, exist_ok=True)
    profile_output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        f"Monaco extraction complete: {output_counts['forward_rows']} forward rows, "
        f"{output_counts['reverse_rows']} reverse rows, "
        f"{report['timings_seconds']['total']:.3f}s total"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True)
    parser.add_argument("--forward-output", type=Path, required=True)
    parser.add_argument("--reverse-output", type=Path, required=True)
    parser.add_argument("--profile-output", type=Path, required=True)
    parser.add_argument("--authoritative-forward-output", type=Path)
    parser.add_argument("--authoritative-reverse-output", type=Path)
    args = parser.parse_args()
    export_monaco(
        args.release,
        args.forward_output,
        args.reverse_output,
        args.profile_output,
        args.authoritative_forward_output,
        args.authoritative_reverse_output,
    )


if __name__ == "__main__":
    main()
