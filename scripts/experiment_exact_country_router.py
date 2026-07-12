#!/usr/bin/env python3
"""Build and measure a research-only exact-country decision artifact.

The artifact is deliberately independent of the production Worker, shard build,
and R2 pipeline.  It stores one R-tree row per normalized claim component and
deduplicates the canonical 2D WKB bytes behind those rows.  Runtime decisions
are fail-closed: anything except one unambiguous, non-synthetic country falls
back to HEAD with an auditable reason.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import os
import re
import resource
import random
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import duckdb
except ImportError:  # pragma: no cover - exercised by CLI dependency checks
    duckdb = None

try:
    import shapely
    from shapely import affinity
    from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon, box
except ImportError:  # pragma: no cover - exercised by CLI dependency checks
    shapely = None


SCHEMA_VERSION = 1
MANIFEST_VERSION = 1
REPORT_VERSION = 1
RELEASE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
AREA_COLUMN_ALIASES = {
    "area_id": ("area_id", "id"),
    "division_id": ("division_id",),
    "country": ("country",),
    "is_land": ("is_land",),
    "is_territorial": ("is_territorial",),
    "geometry": ("geometry", "geom", "wkb"),
    "version": ("version", "area_version"),
    "division_version": ("division_version",),
    "subtype": ("subtype",),
    "perspectives": ("area_perspectives_json", "perspectives"),
    "division_perspectives": ("division_perspectives_json", "perspectives_json"),
    "area_sources": ("area_sources_json", "sources_json", "sources"),
    "geometry_sources": ("geometry_sources_json",),
    "division_sources": ("division_sources_json",),
    "division_country": ("division_country",),
    "overture_release": ("overture_release",),
}
DIVISION_COLUMN_ALIASES = {
    "division_id": ("id", "division_id"),
    "country": ("country",),
    "version": ("version", "division_version"),
    "subtype": ("subtype",),
    "perspectives": ("perspectives", "perspectives_json"),
    "sources": ("sources", "sources_json", "division_sources_json"),
}
SCHEMA_SQL = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE geometries (
    geometry_id INTEGER PRIMARY KEY,
    wkb_sha256 TEXT NOT NULL UNIQUE,
    wkb BLOB NOT NULL
);
CREATE TABLE claims (
    claim_id INTEGER PRIMARY KEY,
    source_row INTEGER NOT NULL,
    area_id TEXT NOT NULL,
    division_id TEXT NOT NULL,
    area_version TEXT,
    division_version TEXT,
    country TEXT NOT NULL,
    is_land INTEGER NOT NULL CHECK (is_land IN (0, 1)),
    is_territorial INTEGER NOT NULL CHECK (is_territorial IN (0, 1)),
    area_perspectives_json TEXT,
    division_perspectives_json TEXT,
    CHECK (is_land + is_territorial >= 1),
    UNIQUE (area_id),
    UNIQUE (source_row)
);
CREATE TABLE components (
    component_id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL REFERENCES claims(claim_id),
    source_component INTEGER NOT NULL,
    normalized_piece INTEGER NOT NULL,
    geometry_id INTEGER NOT NULL REFERENCES geometries(geometry_id),
    UNIQUE (claim_id, source_component, normalized_piece)
);
CREATE VIRTUAL TABLE component_rtree USING rtree(
    component_id,
    min_lon, max_lon,
    min_lat, max_lat
);
CREATE INDEX claims_country ON claims(country);
CREATE INDEX components_claim ON components(claim_id);
CREATE INDEX components_geometry ON components(geometry_id);
"""


@dataclass(frozen=True)
class AreaRow:
    source_row: int
    area_id: str
    division_id: str
    area_version: str | None
    country: str
    is_land: bool
    is_territorial: bool
    area_perspectives_json: str | None
    division_version: str | None
    division_perspectives_json: str | None
    area_sources_json: str | None
    geometry_sources_json: str | None
    division_sources_json: str | None
    geometry_value: Any


@dataclass(frozen=True)
class _VerifiedArtifact:
    artifact: Path
    manifest_path: Path
    sha256: str
    manifest: dict[str, Any]


def _require_dependencies() -> None:
    missing = []
    if duckdb is None:
        missing.append("duckdb")
    if shapely is None:
        missing.append("shapely")
    if missing:
        raise RuntimeError("missing required packages: " + ", ".join(missing))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _parquet_columns(connection: Any, path: Path) -> dict[str, str]:
    try:
        rows = connection.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]
        ).fetchall()
    except Exception as error:
        raise RuntimeError(f"cannot read parquet input {path}: {error}") from error
    return {str(row[0]).lower(): str(row[0]) for row in rows}


def _resolve_columns(
    available: dict[str, str],
    aliases: dict[str, tuple[str, ...]],
    required: set[str],
    label: str,
) -> dict[str, str | None]:
    selected: dict[str, str | None] = {}
    for logical, choices in aliases.items():
        selected[logical] = next(
            (available[item.lower()] for item in choices if item.lower() in available),
            None,
        )
        if logical in required and selected[logical] is None:
            raise RuntimeError(
                f"{label} is missing required {logical!r} column; accepted names: "
                + ", ".join(choices)
            )
    return selected


def _canonical_perspectives(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"{label} is not valid JSON: {error}") from error
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{label} cannot be represented as JSON") from error
    return None if decoded is None else encoded


def _perspectives_block(value: str | None) -> bool:
    if value is None:
        return False
    decoded = json.loads(value)
    return decoded not in (None, [], {})


def _as_required_text(value: Any, label: str) -> str:
    if value is None or not str(value).strip():
        raise RuntimeError(f"{label} is empty")
    return str(value)


def _as_flag(value: Any, label: str) -> bool:
    if value is True or value == 1:
        return True
    if value is False or value == 0:
        return False
    raise RuntimeError(f"{label} must be a non-null boolean")


def _select_expression(column: str | None, alias: str) -> str:
    return (
        f"{_quote_identifier(column)} AS {_quote_identifier(alias)}"
        if column is not None
        else f"NULL AS {_quote_identifier(alias)}"
    )


def _load_divisions(
    connection: Any, path: Path
) -> tuple[dict[str, dict[str, str | None]], dict[str, Any]]:
    available = _parquet_columns(connection, path)
    columns = _resolve_columns(
        available,
        DIVISION_COLUMN_ALIASES,
        {"division_id", "country"},
        "division parquet",
    )
    expressions = [
        _select_expression(columns[name], name)
        for name in (
            "division_id",
            "country",
            "version",
            "subtype",
            "perspectives",
            "sources",
        )
    ]
    query = "SELECT " + ", ".join(expressions) + " FROM read_parquet(?)"
    parents: dict[str, dict[str, str | None]] = {}
    for row_number, row in enumerate(
        connection.execute(query, [str(path)]).fetchall(), 1
    ):
        division_id = _as_required_text(row[0], f"division row {row_number} id")
        country = _as_required_text(row[1], f"division row {row_number} country")
        if columns["subtype"] is not None and row[3] != "country":
            continue
        if division_id in parents:
            raise RuntimeError(f"duplicate country division id {division_id!r}")
        parents[division_id] = {
            "country": country,
            "version": None if row[2] is None else str(row[2]),
            "perspectives": _canonical_perspectives(
                row[4], f"division row {row_number} perspectives"
            ),
            "sources": _canonical_perspectives(
                row[5], f"division row {row_number} sources"
            ),
        }
    if not parents:
        raise RuntimeError("division parquet contains no country rows")
    return parents, {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "column_mapping": columns,
        "country_rows": len(parents),
    }


def load_area_rows(
    area_path: Path, division_path: Path | None = None
) -> tuple[list[AreaRow], dict[str, Any]]:
    """Load and strictly validate country claim rows from local parquet."""
    _require_dependencies()
    if not area_path.is_file():
        raise RuntimeError(f"division_area parquet does not exist: {area_path}")
    if division_path is not None and not division_path.is_file():
        raise RuntimeError(f"division parquet does not exist: {division_path}")
    connection = duckdb.connect()
    try:
        available = _parquet_columns(connection, area_path)
        columns = _resolve_columns(
            available,
            AREA_COLUMN_ALIASES,
            {
                "area_id",
                "division_id",
                "country",
                "is_land",
                "is_territorial",
                "geometry",
            },
            "division_area parquet",
        )
        if division_path is None and columns["division_country"] is None:
            raise RuntimeError(
                "division_area parquet must include division_country or be paired "
                "with --division-parquet so parent country claims are validated"
            )
        parents = None
        division_source = None
        if division_path is not None:
            parents, division_source = _load_divisions(connection, division_path)
        logical_names = (
            "area_id",
            "division_id",
            "country",
            "is_land",
            "is_territorial",
            "geometry",
            "version",
            "subtype",
            "perspectives",
            "division_perspectives",
            "division_version",
            "area_sources",
            "geometry_sources",
            "division_sources",
            "division_country",
            "overture_release",
        )
        expressions = [
            _select_expression(columns[name], name) for name in logical_names
        ]
        query = (
            "SELECT "
            + ", ".join(expressions)
            + ' FROM read_parquet(?) ORDER BY "area_id"'
        )
        raw_rows = connection.execute(query, [str(area_path)]).fetchall()
    finally:
        connection.close()

    rows: list[AreaRow] = []
    seen_area_ids: set[str] = set()
    skipped_non_country = 0
    for source_row, raw in enumerate(raw_rows, 1):
        subtype = raw[7]
        if columns["subtype"] is not None and subtype != "country":
            skipped_non_country += 1
            continue
        area_id = _as_required_text(raw[0], f"area row {source_row} area_id")
        if area_id in seen_area_ids:
            raise RuntimeError(f"duplicate country division_area id {area_id!r}")
        seen_area_ids.add(area_id)
        division_id = _as_required_text(raw[1], f"area row {source_row} division_id")
        country = _as_required_text(raw[2], f"area row {source_row} country")
        if not COUNTRY_RE.fullmatch(country):
            raise RuntimeError(
                f"area row {source_row} has invalid country code {country!r}"
            )
        _as_required_text(raw[15], f"area row {source_row} overture_release")
        joined_parent_country = None
        if columns["division_country"] is not None:
            joined_parent_country = _as_required_text(
                raw[14], f"area row {source_row} joined parent country"
            )
            if not COUNTRY_RE.fullmatch(joined_parent_country):
                raise RuntimeError(
                    f"area row {source_row} has invalid joined parent country "
                    f"{joined_parent_country!r}"
                )
            if joined_parent_country != country:
                raise RuntimeError(
                    f"area row {source_row} country {country!r} disagrees with "
                    f"joined parent country {joined_parent_country!r}"
                )
        is_land = _as_flag(raw[3], f"area row {source_row} is_land")
        is_territorial = _as_flag(raw[4], f"area row {source_row} is_territorial")
        if not is_land and not is_territorial:
            raise RuntimeError(
                f"area row {source_row} must set at least one of is_land and "
                "is_territorial; dual true flags are retained and audited"
            )
        if raw[5] is None:
            raise RuntimeError(f"area row {source_row} geometry is null")
        parent = None if parents is None else parents.get(division_id)
        if parents is not None and parent is None:
            raise RuntimeError(
                f"area row {source_row} parent division {division_id!r} is missing"
            )
        if parent is not None and parent["country"] != country:
            raise RuntimeError(
                f"area row {source_row} country {country!r} disagrees with parent "
                f"country {parent['country']!r}"
            )
        rows.append(
            AreaRow(
                source_row=source_row,
                area_id=area_id,
                division_id=division_id,
                area_version=None if raw[6] is None else str(raw[6]),
                country=country,
                is_land=is_land,
                is_territorial=is_territorial,
                area_perspectives_json=_canonical_perspectives(
                    raw[8], f"area row {source_row} perspectives"
                ),
                division_version=(None if raw[10] is None else str(raw[10]))
                if parent is None
                else parent["version"],
                division_perspectives_json=(
                    _canonical_perspectives(
                        raw[9], f"area row {source_row} division perspectives"
                    )
                    if parent is None
                    else parent["perspectives"]
                ),
                area_sources_json=_canonical_perspectives(
                    raw[11], f"area row {source_row} area sources"
                ),
                geometry_sources_json=_canonical_perspectives(
                    raw[12], f"area row {source_row} geometry sources"
                ),
                division_sources_json=_canonical_perspectives(
                    raw[13], f"area row {source_row} division sources"
                )
                if parent is None or parent["sources"] is None
                else parent["sources"],
                geometry_value=raw[5],
            )
        )
    if not rows:
        raise RuntimeError("division_area parquet contains no country rows")
    source = {
        "division_area": {
            "path": str(area_path.resolve()),
            "size_bytes": area_path.stat().st_size,
            "sha256": sha256_file(area_path),
            "column_mapping": columns,
            "input_rows": len(raw_rows),
            "country_rows": len(rows),
            "skipped_non_country_rows": skipped_non_country,
            "parent_country_validation": (
                "separate-division-parquet"
                if division_path is not None
                else (
                    "joined-division-country-column"
                    if columns["division_country"] is not None
                    else "not-available"
                )
            ),
            "overture_releases": sorted(
                {
                    _as_required_text(raw[15], "division_area overture_release")
                    for raw in raw_rows
                    if columns["subtype"] is None or raw[7] == "country"
                }
            ),
        },
        "division": division_source,
    }
    return rows, source


def _polygon_parts(geometry: Any, label: str) -> list[Any]:
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        polygons: list[Any] = []
        for part in geometry.geoms:
            if part.is_empty:
                continue
            if isinstance(part, (Polygon, MultiPolygon, GeometryCollection)):
                polygons.extend(_polygon_parts(part, label))
            else:
                raise RuntimeError(
                    f"{label} contains non-polygonal {part.geom_type} geometry"
                )
        return polygons
    raise RuntimeError(f"{label} is not polygonal: {geometry.geom_type}")


def _overlay_polygon_parts(geometry: Any, label: str) -> list[Any]:
    """Extract polygon area from a clipping result, ignoring zero-area seams."""
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        polygons: list[Any] = []
        for part in geometry.geoms:
            if isinstance(part, (Polygon, MultiPolygon, GeometryCollection)):
                polygons.extend(_overlay_polygon_parts(part, label))
            elif not part.is_empty and part.geom_type not in (
                "Point",
                "MultiPoint",
                "LineString",
                "MultiLineString",
                "LinearRing",
            ):
                raise RuntimeError(
                    f"{label} clipping returned unsupported {part.geom_type}"
                )
        return polygons
    # Intersecting a polygon with a neighboring closed strip can return only
    # their shared line or point. It contains no claim area and is safe to omit.
    if geometry.geom_type in (
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "LinearRing",
    ):
        return []
    raise RuntimeError(f"{label} clipping returned unsupported {geometry.geom_type}")


def _unwrap_ring(coordinates: Iterable[Sequence[float]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    previous = None
    for coordinate in coordinates:
        longitude, latitude = float(coordinate[0]), float(coordinate[1])
        if previous is not None:
            while longitude - previous > 180:
                longitude -= 360
            while longitude - previous < -180:
                longitude += 360
        result.append((longitude, latitude))
        previous = longitude
    return result


def _shift_ring_near(
    coordinates: list[tuple[float, float]], reference_longitude: float
) -> list[tuple[float, float]]:
    mean_longitude = statistics.fmean(item[0] for item in coordinates)
    offset = round((reference_longitude - mean_longitude) / 360) * 360
    return [(longitude + offset, latitude) for longitude, latitude in coordinates]


def _unwrap_polygon(polygon: Any, label: str) -> Any:
    exterior = _unwrap_ring(polygon.exterior.coords)
    reference = statistics.fmean(item[0] for item in exterior)
    interiors = [
        _shift_ring_near(_unwrap_ring(interior.coords), reference)
        for interior in polygon.interiors
    ]
    unwrapped = Polygon(exterior, interiors)
    if unwrapped.is_empty or not unwrapped.is_valid:
        raise RuntimeError(f"{label} becomes invalid while unwrapping antimeridian")
    if unwrapped.bounds[2] - unwrapped.bounds[0] > 360 + 1e-9:
        raise RuntimeError(f"{label} spans more than 360 degrees after unwrapping")
    return unwrapped


def _validate_coordinates(geometry: Any, label: str) -> None:
    coordinates = shapely.get_coordinates(geometry, include_z=False)
    if len(coordinates) == 0:
        raise RuntimeError(f"{label} has no coordinates")
    for longitude, latitude in coordinates:
        if not math.isfinite(float(longitude)) or not math.isfinite(float(latitude)):
            raise RuntimeError(f"{label} has non-finite coordinates")
        if longitude < -180 or longitude > 180 or latitude < -90 or latitude > 90:
            raise RuntimeError(f"{label} has coordinates outside lon/lat bounds")


def _decode_geometry(value: Any, label: str) -> Any:
    try:
        if isinstance(value, str):
            encoded = value[2:] if value.startswith("\\x") else value
            raw = bytes.fromhex(encoded)
        else:
            raw = bytes(value)
        geometry = shapely.from_wkb(raw)
    except Exception as error:
        raise RuntimeError(f"{label} is not valid WKB: {error}") from error
    geometry = shapely.force_2d(geometry)
    if geometry.is_empty:
        raise RuntimeError(f"{label} is empty")
    if not geometry.is_valid:
        raise RuntimeError(f"{label} is invalid: {shapely.is_valid_reason(geometry)}")
    _validate_coordinates(geometry, label)
    return geometry


def normalize_geometry(
    value: Any, label: str, simplify_tolerance: float = 0.0
) -> list[tuple[int, int, bytes, tuple[float, ...]]]:
    """Return canonical, antimeridian-split 2D polygon pieces.

    Tuples contain ``(source_component, normalized_piece, wkb, bounds)``.
    No repair is attempted; invalid source or intermediate geometry aborts the
    build so a malformed claim can never silently disappear.
    """
    geometry = _decode_geometry(value, label)
    output: list[tuple[int, int, bytes, tuple[float, ...]]] = []
    for source_component, polygon in enumerate(_polygon_parts(geometry, label), 1):
        unwrapped = _unwrap_polygon(polygon, label)
        min_lon, _, max_lon, _ = unwrapped.bounds
        first_strip = math.floor((min_lon + 180) / 360) - 1
        last_strip = math.floor((max_lon + 180) / 360) + 1
        pieces: list[Any] = []
        exact_clipped_area = 0.0
        for strip in range(first_strip, last_strip + 1):
            lower = -180 + strip * 360
            upper = 180 + strip * 360
            clipped = unwrapped.intersection(box(lower, -90, upper, 90))
            for piece in (
                _overlay_polygon_parts(clipped, label) if not clipped.is_empty else []
            ):
                if piece.area <= 0:
                    continue
                exact_clipped_area += float(piece.area)
                canonical = affinity.translate(piece, xoff=-360 * strip)
                canonical = shapely.normalize(shapely.force_2d(canonical))
                if simplify_tolerance:
                    canonical = shapely.normalize(
                        canonical.simplify(simplify_tolerance, preserve_topology=True)
                    )
                if canonical.is_empty or not canonical.is_valid:
                    raise RuntimeError(f"{label} produced an invalid normalized piece")
                bounds = tuple(float(item) for item in canonical.bounds)
                if (
                    bounds[0] < -180 - 1e-9
                    or bounds[2] > 180 + 1e-9
                    or bounds[1] < -90 - 1e-9
                    or bounds[3] > 90 + 1e-9
                ):
                    raise RuntimeError(f"{label} normalized outside lon/lat bounds")
                pieces.append(canonical)
        if not pieces:
            raise RuntimeError(
                f"{label} component {source_component} produced no pieces"
            )
        if not math.isclose(
            exact_clipped_area,
            float(unwrapped.area),
            rel_tol=1e-10,
            abs_tol=1e-12,
        ):
            raise RuntimeError(
                f"{label} component {source_component} changed area while splitting "
                "at the antimeridian"
            )
        pieces.sort(key=lambda item: shapely.to_wkb(item, byte_order=1))
        for normalized_piece, piece in enumerate(pieces, 1):
            wkb = bytes(shapely.to_wkb(piece, output_dimension=2, byte_order=1))
            output.append(
                (
                    source_component,
                    normalized_piece,
                    wkb,
                    tuple(float(item) for item in piece.bounds),
                )
            )
    if not output:
        raise RuntimeError(f"{label} produced no polygon components")
    return output


def _load_expected_codes(path: Path) -> set[str]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"cannot read expected country codes {path}: {error}"
        ) from error
    if isinstance(value, dict):
        value = value.get("country_codes")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(
            "expected country codes must be a JSON array or country_codes array"
        )
    codes = set(value)
    if len(codes) != len(value) or any(
        not COUNTRY_RE.fullmatch(item) for item in codes
    ):
        raise RuntimeError(
            "expected country codes contain duplicates or invalid values"
        )
    return codes


def _publish_output_set(
    staged_targets: Sequence[tuple[Path, Path]], *, overwrite: bool
) -> None:
    """Publish a related output set with rollback if any replacement fails."""
    targets = [target for _, target in staged_targets]
    if not overwrite:
        existing = [target for target in targets if target.exists()]
        if existing:
            raise RuntimeError(f"refusing to overwrite existing output {existing[0]}")
    nonce = f"{os.getpid()}-{time.time_ns()}"
    backups: dict[Path, Path] = {}
    published: list[Path] = []
    try:
        if overwrite:
            for index, target in enumerate(targets):
                if not target.exists():
                    continue
                backup = target.with_name(f".{target.name}.backup-{nonce}-{index}")
                os.replace(target, backup)
                backups[target] = backup
        for staged, target in staged_targets:
            os.replace(staged, target)
            published.append(target)
    except Exception as error:
        rollback_errors: list[str] = []
        for target in reversed(published):
            try:
                target.unlink(missing_ok=True)
            except OSError as rollback_error:
                rollback_errors.append(f"remove {target}: {rollback_error}")
        for target, backup in backups.items():
            try:
                os.replace(backup, target)
            except OSError as rollback_error:
                rollback_errors.append(f"restore {target}: {rollback_error}")
        for staged, _ in staged_targets:
            staged.unlink(missing_ok=True)
        if rollback_errors:
            raise RuntimeError(
                f"output publication failed ({error}); rollback also failed: "
                + "; ".join(rollback_errors)
            ) from error
        raise
    else:
        for backup in backups.values():
            backup.unlink(missing_ok=True)


def build_artifact(
    area_path: Path,
    output: Path,
    release: str,
    *,
    manifest_path: Path | None = None,
    audit_sidecar_path: Path | None = None,
    division_path: Path | None = None,
    expected_country_count: int | None = None,
    expected_country_codes: Path | None = None,
    extraction_sql: Path | None = None,
    claim_policy: str = "all-claims",
    simplify_tolerance: float = 0.0,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Build the SQLite artifact and a SHA-bound external manifest."""
    _require_dependencies()
    if not RELEASE_RE.fullmatch(release):
        raise ValueError("overture release must look like YYYY-MM-DD.N")
    if claim_policy not in ("all-claims", "territorial-primary"):
        raise ValueError("claim policy must be all-claims or territorial-primary")
    if not math.isfinite(simplify_tolerance) or simplify_tolerance < 0:
        raise ValueError("simplify tolerance must be a finite non-negative number")
    manifest_path = manifest_path or output.with_name(output.name + ".manifest.json")
    audit_sidecar_path = audit_sidecar_path or output.with_name(
        output.name + ".audit.json"
    )
    extraction_sql = extraction_sql or Path(__file__).with_name(
        "extract_country_router.sql"
    )
    if not extraction_sql.is_file():
        raise RuntimeError(f"declared extraction SQL does not exist: {extraction_sql}")
    if (
        len({output.resolve(), manifest_path.resolve(), audit_sidecar_path.resolve()})
        != 3
    ):
        raise ValueError("artifact, audit sidecar, and manifest paths must differ")
    for path in (output, manifest_path, audit_sidecar_path):
        if path.exists() and not overwrite:
            raise RuntimeError(f"refusing to overwrite existing output {path}")
    rows, source = load_area_rows(area_path, division_path)
    source_releases = source["division_area"]["overture_releases"]
    if source_releases != [release]:
        raise RuntimeError(
            f"division_area release values {source_releases!r} do not match "
            f"declared release {release!r}"
        )
    source["extraction_sql"] = {
        "path": str(extraction_sql.resolve()),
        "size_bytes": extraction_sql.stat().st_size,
        "sha256": sha256_file(extraction_sql),
        "contract": "template bytes before release/output placeholder substitution",
    }
    country_codes = {row.country for row in rows}
    retained_rows = (
        rows
        if claim_policy == "all-claims"
        else [row for row in rows if row.is_territorial]
    )
    if claim_policy == "territorial-primary":
        territorial_counts = collections.Counter(row.country for row in retained_rows)
        missing = sorted(country_codes - set(territorial_counts))
        duplicates = sorted(
            country for country, count in territorial_counts.items() if count != 1
        )
        if missing or duplicates:
            raise RuntimeError(
                "territorial-primary requires exactly one territorial claim per "
                f"country: missing={missing!r} non_unique={duplicates!r}"
            )
    if (
        expected_country_count is not None
        and len(country_codes) != expected_country_count
    ):
        raise RuntimeError(
            f"country count {len(country_codes)} does not match expected "
            f"{expected_country_count}"
        )
    expected_codes = (
        None
        if expected_country_codes is None
        else _load_expected_codes(expected_country_codes)
    )
    if expected_codes is not None and country_codes != expected_codes:
        raise RuntimeError(
            "country code set mismatch: missing="
            + repr(sorted(expected_codes - country_codes))
            + " unexpected="
            + repr(sorted(country_codes - expected_codes))
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    audit_sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    staging_nonce = f"{os.getpid()}-{time.time_ns()}"
    temporary = output.with_name(f".{output.name}.staged-{staging_nonce}")
    temporary_manifest = manifest_path.with_name(
        f".{manifest_path.name}.staged-{staging_nonce}"
    )
    temporary_audit = audit_sidecar_path.with_name(
        f".{audit_sidecar_path.name}.staged-{staging_nonce}"
    )
    started = time.monotonic()
    geometry_ids: dict[str, int] = {}
    geometry_bytes = 0
    component_count = 0
    claim_count = 0
    source_polygon_components = 0
    split_source_components = 0
    retained_polygon_components = 0
    retained_split_source_components = 0
    connection = sqlite3.connect(temporary)
    try:
        connection.execute("PRAGMA page_size=4096")
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(SCHEMA_SQL)
        metadata = {
            "schema_version": str(SCHEMA_VERSION),
            "overture_release": release,
            "division_area_sha256": source["division_area"]["sha256"],
            "division_sha256": (
                "" if source["division"] is None else source["division"]["sha256"]
            ),
            "extraction_sql_sha256": source["extraction_sql"]["sha256"],
            "normalization": "strict-valid-2d-antimeridian-split-v1",
            "claim_policy": claim_policy,
            "simplify_tolerance_degrees": repr(simplify_tolerance),
            "geometry_semantics": (
                "exact-unsimplified"
                if simplify_tolerance == 0
                else "topology-preserving-simplified"
            ),
            "decision_contract": "unique-non-X-country-without-blockers-else-HEAD-v1",
        }
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)", sorted(metadata.items())
        )
        retained_area_ids = {row.area_id for row in retained_rows}
        for area in rows:
            pieces = normalize_geometry(
                area.geometry_value,
                f"area row {area.source_row} ({area.area_id})",
                simplify_tolerance if area.area_id in retained_area_ids else 0.0,
            )
            source_components = {piece[0] for piece in pieces}
            source_polygon_components += len(source_components)
            split_source_components += sum(
                1
                for source_component in source_components
                if sum(piece[0] == source_component for piece in pieces) > 1
            )
            if area.area_id not in retained_area_ids:
                continue
            claim_count += 1
            connection.execute(
                """
                INSERT INTO claims(
                    claim_id, source_row, area_id, division_id, area_version,
                    division_version, country, is_land, is_territorial,
                    area_perspectives_json, division_perspectives_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim_count,
                    area.source_row,
                    area.area_id,
                    area.division_id,
                    area.area_version,
                    area.division_version,
                    area.country,
                    int(area.is_land),
                    int(area.is_territorial),
                    area.area_perspectives_json,
                    area.division_perspectives_json,
                ),
            )
            retained_polygon_components += len(source_components)
            retained_split_source_components += sum(
                1
                for source_component in source_components
                if sum(piece[0] == source_component for piece in pieces) > 1
            )
            for source_component, normalized_piece, wkb, bounds in pieces:
                digest = _sha256_bytes(wkb)
                geometry_id = geometry_ids.get(digest)
                if geometry_id is None:
                    geometry_id = len(geometry_ids) + 1
                    geometry_ids[digest] = geometry_id
                    geometry_bytes += len(wkb)
                    connection.execute(
                        "INSERT INTO geometries(geometry_id, wkb_sha256, wkb) "
                        "VALUES (?, ?, ?)",
                        (geometry_id, digest, sqlite3.Binary(wkb)),
                    )
                component_count += 1
                connection.execute(
                    """
                    INSERT INTO components(
                        component_id, claim_id, source_component, normalized_piece,
                        geometry_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        component_count,
                        claim_count,
                        source_component,
                        normalized_piece,
                        geometry_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO component_rtree VALUES (?, ?, ?, ?, ?)",
                    (component_count, bounds[0], bounds[2], bounds[1], bounds[3]),
                )
        stored_counts = {
            "claims": int(
                connection.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
            ),
            "components": int(
                connection.execute("SELECT COUNT(*) FROM components").fetchone()[0]
            ),
            "geometries": int(
                connection.execute("SELECT COUNT(*) FROM geometries").fetchone()[0]
            ),
            "rtree": int(
                connection.execute("SELECT COUNT(*) FROM component_rtree").fetchone()[0]
            ),
        }
        expected_counts = {
            "claims": len(retained_rows),
            "components": component_count,
            "geometries": len(geometry_ids),
            "rtree": component_count,
        }
        if stored_counts != expected_counts:
            raise RuntimeError(
                f"artifact row reconciliation failed: stored={stored_counts!r} "
                f"expected={expected_counts!r}"
            )
        unused_geometries = connection.execute(
            """
            SELECT COUNT(*) FROM geometries g
            LEFT JOIN components c USING (geometry_id)
            WHERE c.component_id IS NULL
            """
        ).fetchone()[0]
        if unused_geometries:
            raise RuntimeError(f"artifact has {unused_geometries} unused geometries")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or foreign_keys:
            raise RuntimeError(
                f"artifact integrity failure: integrity={integrity!r}, "
                f"foreign_keys={foreign_keys!r}"
            )
        connection.commit()
        connection.execute("VACUUM")
    except Exception:
        connection.close()
        temporary.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)
        temporary_audit.unlink(missing_ok=True)
        raise
    else:
        connection.close()

    audit = {
        "source_claim_rows": len(rows),
        "retained_claim_rows": len(retained_rows),
        "stored_claim_rows": claim_count,
        "excluded_claim_rows": len(rows) - len(retained_rows),
        "source_polygon_components": source_polygon_components,
        "retained_polygon_components": retained_polygon_components,
        "normalized_components": component_count,
        "rtree_entries": component_count,
        "source_antimeridian_split_components": split_source_components,
        "antimeridian_split_source_components": retained_split_source_components,
        "unique_wkb_records": len(geometry_ids),
        "deduplicated_wkb_references": component_count - len(geometry_ids),
        "unique_wkb_bytes": geometry_bytes,
        "country_count": len(country_codes),
        "country_codes": sorted(country_codes),
        "synthetic_country_codes": sorted(
            code for code in country_codes if code.startswith("X")
        ),
        "land_claim_rows": sum(row.is_land for row in rows),
        "territorial_claim_rows": sum(row.is_territorial for row in rows),
        "dual_land_territorial_claim_rows": sum(
            row.is_land and row.is_territorial for row in rows
        ),
        "area_perspective_claim_rows": sum(
            _perspectives_block(row.area_perspectives_json) for row in rows
        ),
        "division_perspective_claim_rows": sum(
            _perspectives_block(row.division_perspectives_json) for row in rows
        ),
    }
    audit_sidecar = {
        "audit_version": 1,
        "research_only": True,
        "overture_release": release,
        "claim_policy": claim_policy,
        "division_area_sha256": source["division_area"]["sha256"],
        "extraction_sql_sha256": source["extraction_sql"]["sha256"],
        "source_claim_count": len(rows),
        "retained_claim_count": len(retained_rows),
        "claims_by_area_id": {
            area.area_id: {
                "source_row": area.source_row,
                "division_id": area.division_id,
                "area_version": area.area_version,
                "division_version": area.division_version,
                "country": area.country,
                "is_land": area.is_land,
                "is_territorial": area.is_territorial,
                "retained": area.area_id in retained_area_ids,
                "area_perspectives_json": area.area_perspectives_json,
                "division_perspectives_json": area.division_perspectives_json,
                "area_sources_json": area.area_sources_json,
                "geometry_sources_json": area.geometry_sources_json,
                "division_sources_json": area.division_sources_json,
            }
            for area in sorted(rows, key=lambda item: item.area_id)
        },
    }
    audit_bytes = _json_bytes(audit_sidecar)
    temporary_audit.write_bytes(audit_bytes)
    artifact_sha = sha256_file(temporary)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "artifact_schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "research_only": True,
        "overture_release": release,
        "claim_policy": claim_policy,
        "simplify_tolerance_degrees": simplify_tolerance,
        "geometry_semantics": (
            "exact-unsimplified"
            if simplify_tolerance == 0
            else "topology-preserving-simplified-not-an-exact-oracle"
        ),
        "source": source,
        "completeness_gates": {
            "expected_country_count": expected_country_count,
            "expected_country_codes_path": (
                None
                if expected_country_codes is None
                else str(expected_country_codes.resolve())
            ),
        },
        "normalization": {
            "geometry": "strict valid polygonal WKB; force 2D; unwrap and split at antimeridian; optional topology-preserving simplification; canonical little-endian WKB",
            "simplify_tolerance_degrees": simplify_tolerance,
            "geometry_semantics": (
                "exact-unsimplified"
                if simplify_tolerance == 0
                else "topology-preserving-simplified-not-an-exact-oracle"
            ),
            "repair_invalid_geometry": False,
            "rtree_identity": "one row per retained source claim component/piece",
            "wkb_storage": "SHA-256 deduplicated across claim components",
        },
        "audit": audit,
        "decision_reasons": [
            "route",
            "no_match",
            "multiple_countries",
            "synthetic_country",
            "perspective_claim",
            "boundary",
            "input_error",
            "artifact_error",
        ],
        "artifact": {
            "path": str(output.resolve()),
            "size_bytes": temporary.stat().st_size,
            "sha256": artifact_sha,
            "role": "hot runtime decision data",
        },
        "audit_sidecar": {
            "path": str(audit_sidecar_path.resolve()),
            "size_bytes": temporary_audit.stat().st_size,
            "sha256": _sha256_bytes(audit_bytes),
            "role": "cold source provenance; not required by runtime lookup",
            "source_claim_count": len(rows),
            "retained_claim_count": len(retained_rows),
        },
        "builder": {
            "path": str(Path(__file__).resolve()),
            "sha256": sha256_file(Path(__file__).resolve()),
            "schema_sql_sha256": _sha256_bytes(SCHEMA_SQL.encode()),
            "python": sys.version.split()[0],
            "duckdb": duckdb.__version__,
            "shapely": shapely.__version__,
            "elapsed_seconds": round(time.monotonic() - started, 6),
        },
    }
    try:
        temporary_manifest.write_bytes(_json_bytes(manifest))
        _publish_output_set(
            (
                (temporary, output),
                (temporary_audit, audit_sidecar_path),
                (temporary_manifest, manifest_path),
            ),
            overwrite=overwrite,
        )
    except Exception:
        temporary.unlink(missing_ok=True)
        temporary_audit.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)
        raise
    return manifest


def _head(reason: str, **details: Any) -> dict[str, Any]:
    return {"decision": "HEAD", "country": None, "reason": reason, **details}


def _validate_artifact(connection: sqlite3.Connection) -> dict[str, str]:
    metadata = dict(connection.execute("SELECT key, value FROM metadata"))
    if metadata.get("schema_version") != str(SCHEMA_VERSION):
        raise RuntimeError("unsupported or missing artifact schema version")
    if (
        metadata.get("decision_contract")
        != "unique-non-X-country-without-blockers-else-HEAD-v1"
    ):
        raise RuntimeError("unsupported or missing artifact decision contract")
    if not RELEASE_RE.fullmatch(metadata.get("overture_release", "")):
        raise RuntimeError("unsupported or missing artifact Overture release")
    source_sha = metadata.get("division_area_sha256", "")
    if len(source_sha) != 64 or any(
        character not in "0123456789abcdef" for character in source_sha
    ):
        raise RuntimeError("unsupported or missing artifact source SHA-256")
    return metadata


def _default_manifest_path(artifact: Path) -> Path:
    return artifact.with_name(artifact.name + ".manifest.json")


def _verify_artifact_manifest(
    artifact: Path, manifest_path: Path | None = None
) -> tuple[dict[str, Any], Path]:
    manifest_path = manifest_path or _default_manifest_path(artifact)
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"cannot read artifact manifest {manifest_path}: {error}"
        ) from error
    if not isinstance(manifest, dict):
        raise RuntimeError("artifact manifest must be a JSON object")
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise RuntimeError("unsupported or missing artifact manifest version")
    if manifest.get("artifact_schema_version") != SCHEMA_VERSION:
        raise RuntimeError("artifact manifest schema version mismatch")
    record = manifest.get("artifact")
    if not isinstance(record, dict):
        raise RuntimeError("artifact manifest is missing the artifact record")
    expected_size = record.get("size_bytes")
    expected_sha = record.get("sha256")
    if (
        not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 1
    ):
        raise RuntimeError("artifact manifest has an invalid size")
    if (
        not isinstance(expected_sha, str)
        or len(expected_sha) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha)
    ):
        raise RuntimeError("artifact manifest has an invalid SHA-256")
    try:
        actual_size = artifact.stat().st_size
        actual_sha = sha256_file(artifact)
    except OSError as error:
        raise RuntimeError(f"cannot read artifact {artifact}: {error}") from error
    if actual_size != expected_size or actual_sha != expected_sha:
        raise RuntimeError(
            "artifact size/SHA-256 does not match manifest: "
            f"expected=({expected_size}, {expected_sha}) "
            f"actual=({actual_size}, {actual_sha})"
        )
    return manifest, manifest_path


def _verified_artifact(
    artifact: Path, manifest_path: Path | None = None
) -> _VerifiedArtifact:
    manifest, resolved_manifest_path = _verify_artifact_manifest(
        artifact, manifest_path
    )
    return _VerifiedArtifact(
        artifact=artifact,
        manifest_path=resolved_manifest_path,
        sha256=str(manifest["artifact"]["sha256"]),
        manifest=manifest,
    )


def _validate_connection_artifact(
    connection: sqlite3.Connection, artifact: Path
) -> None:
    rows = connection.execute("PRAGMA database_list").fetchall()
    main = next((row for row in rows if row[1] == "main"), None)
    if main is None or not main[2]:
        raise RuntimeError("artifact connection has no file-backed main database")
    if Path(main[2]).resolve() != artifact.resolve():
        raise RuntimeError(
            f"artifact connection path {main[2]!r} does not match {str(artifact)!r}"
        )


def _validated_coordinates(longitude: Any, latitude: Any) -> tuple[float, float]:
    longitude = float(longitude)
    latitude = float(latitude)
    if (
        not math.isfinite(longitude)
        or not math.isfinite(latitude)
        or longitude < -180
        or longitude > 180
        or latitude < -90
        or latitude > 90
    ):
        raise ValueError("coordinates are outside finite lon/lat bounds")
    return (-180.0 if longitude == 180 else longitude), latitude


def _resolve_point_verified(
    verified: _VerifiedArtifact,
    longitude: float,
    latitude: float,
    *,
    connection: sqlite3.Connection | None = None,
    geometry_cache: dict[tuple[str, int], Any] | None = None,
) -> dict[str, Any]:
    """Resolve with a context created only after manifest/hash verification."""
    artifact = verified.artifact
    try:
        longitude, latitude = _validated_coordinates(longitude, latitude)
    except (TypeError, ValueError) as error:
        return _head("input_error", error=str(error), candidate_count=0, exact_tests=0)

    owns_connection = connection is None
    try:
        if connection is None:
            connection = sqlite3.connect(f"file:{artifact.resolve()}?mode=ro", uri=True)
        _validate_connection_artifact(connection, artifact)
        metadata = _validate_artifact(connection)
        candidates = connection.execute(
            """
            SELECT c.component_id, q.country, q.is_land, q.is_territorial,
                   q.area_perspectives_json, q.division_perspectives_json,
                   q.area_id, q.division_id, c.source_component,
                   c.normalized_piece, g.geometry_id
            FROM component_rtree r
            JOIN components c ON c.component_id = r.component_id
            JOIN claims q ON q.claim_id = c.claim_id
            JOIN geometries g ON g.geometry_id = c.geometry_id
            WHERE ((r.min_lon <= ? AND r.max_lon >= ?)
                   OR (r.min_lon <= ? AND r.max_lon >= ?))
              AND r.min_lat <= ? AND r.max_lat >= ?
            ORDER BY c.component_id
            """,
            (
                longitude,
                longitude,
                180.0 if longitude == -180 else longitude,
                180.0 if longitude == -180 else longitude,
                latitude,
                latitude,
            ),
        ).fetchall()
        point = Point(longitude, latitude)
        hits: list[dict[str, Any]] = []
        boundary_components: list[int] = []
        errors: list[str] = []
        candidate_details: dict[int, tuple[Any, dict[str, Any], tuple[str, int]]] = {}
        for candidate in candidates:
            component_id = int(candidate[0])
            try:
                if candidate[2] not in (0, 1) or candidate[3] not in (0, 1):
                    raise RuntimeError("invalid claim flags")
                if int(candidate[2]) + int(candidate[3]) < 1:
                    raise RuntimeError("claim has neither land nor territorial flag")
                country = str(candidate[1])
                if not COUNTRY_RE.fullmatch(country):
                    raise RuntimeError("invalid country code")
                geometry_id = int(candidate[10])
                cache_key = (verified.sha256, geometry_id)
                geometry = (
                    None if geometry_cache is None else geometry_cache.get(cache_key)
                )
                if geometry is None:
                    geometry_row = connection.execute(
                        "SELECT wkb FROM geometries WHERE geometry_id = ?",
                        (geometry_id,),
                    ).fetchone()
                    if geometry_row is None:
                        raise RuntimeError("missing stored polygon WKB")
                    geometry = shapely.from_wkb(bytes(geometry_row[0]))
                    if (
                        geometry.is_empty
                        or not geometry.is_valid
                        or not isinstance(geometry, Polygon)
                    ):
                        raise RuntimeError("invalid stored polygon WKB")
                    if geometry_cache is not None:
                        geometry_cache[cache_key] = geometry
                # Every R-tree candidate is exact-tested, including duplicate WKB claims.
                hit = {
                    "component_id": component_id,
                    "country": country,
                    "claim": (
                        "dual"
                        if candidate[2] and candidate[3]
                        else "land"
                        if candidate[2]
                        else "territorial"
                    ),
                    "area_perspectives_json": candidate[4],
                    "division_perspectives_json": candidate[5],
                    "area_id": candidate[6],
                    "division_id": candidate[7],
                }
                candidate_details[component_id] = (
                    geometry,
                    hit,
                    (str(candidate[6]), int(candidate[8])),
                )
                test_point = (
                    Point(180.0, latitude)
                    if (longitude == -180 and geometry.bounds[0] > 0)
                    else point
                )
                if geometry.contains(test_point):
                    hits.append(hit)
                elif geometry.boundary.covers(test_point):
                    boundary_components.append(component_id)
            except Exception as error:
                errors.append(f"component {component_id}: {error}")
        # Splitting at +/-180 creates an indexing seam, not a political border.
        # Rejoin pieces from the same source polygon only for an exact dateline
        # query, and retain boundary status if the reconstructed source boundary
        # really does pass through the point.
        if longitude == -180 and boundary_components and not errors:
            boundary_set = set(boundary_components)
            grouped: dict[tuple[str, int], list[tuple[int, Any, dict[str, Any]]]] = {}
            for component_id, (geometry, hit, group) in candidate_details.items():
                grouped.setdefault(group, []).append((component_id, geometry, hit))
            for group_items in grouped.values():
                group_boundary = boundary_set.intersection(
                    item[0] for item in group_items
                )
                if not group_boundary or len(group_items) < 2:
                    continue
                shifted = [
                    affinity.translate(item[1], xoff=360)
                    if item[1].bounds[0] < 0
                    else item[1]
                    for item in group_items
                ]
                reconstructed = shapely.union_all(shifted)
                dateline_point = Point(180.0, latitude)
                if reconstructed.contains(dateline_point):
                    boundary_set.difference_update(group_boundary)
                    if not any(
                        hit["area_id"] == group_items[0][2]["area_id"]
                        and hit["component_id"] in {item[0] for item in group_items}
                        for hit in hits
                    ):
                        hits.append(group_items[0][2])
            boundary_components = sorted(boundary_set)
        common = {
            "claim_policy": metadata.get("claim_policy"),
            "geometry_semantics": metadata.get("geometry_semantics"),
            "simplify_tolerance_degrees": float(
                metadata.get("simplify_tolerance_degrees", "0")
            ),
            "candidate_count": len(candidates),
            "exact_tests": len(candidates),
            "matched_component_count": len(hits),
            "matched_countries": sorted({hit["country"] for hit in hits}),
        }
        if errors:
            return _head("artifact_error", errors=errors, **common)
        if boundary_components:
            return _head(
                "boundary", boundary_component_ids=boundary_components, **common
            )
        if not hits:
            return _head("no_match", **common)
        synthetic = sorted(
            {hit["country"] for hit in hits if hit["country"].startswith("X")}
        )
        if synthetic:
            return _head("synthetic_country", synthetic_countries=synthetic, **common)
        perspective_hits = [
            hit["component_id"]
            for hit in hits
            if _perspectives_block(hit["area_perspectives_json"])
            or _perspectives_block(hit["division_perspectives_json"])
        ]
        if perspective_hits:
            return _head(
                "perspective_claim",
                perspective_component_ids=perspective_hits,
                **common,
            )
        countries = {hit["country"] for hit in hits}
        if len(countries) != 1:
            return _head("multiple_countries", **common)
        country = next(iter(countries))
        return {
            "decision": "route",
            "country": country,
            "reason": "route",
            "claim_kinds": sorted({hit["claim"] for hit in hits}),
            **common,
        }
    except Exception as error:
        return _head(
            "artifact_error",
            errors=[str(error)],
            candidate_count=0,
            exact_tests=0,
            matched_component_count=0,
            matched_countries=[],
        )
    finally:
        if owns_connection and connection is not None:
            connection.close()


def resolve_point(
    artifact: Path,
    longitude: float,
    latitude: float,
    *,
    manifest_path: Path | None = None,
    geometry_cache: dict[tuple[str, int], Any] | None = None,
) -> dict[str, Any]:
    """Resolve a point after mandatory manifest/hash verification."""
    try:
        longitude, latitude = _validated_coordinates(longitude, latitude)
    except (TypeError, ValueError) as error:
        return _head("input_error", error=str(error), candidate_count=0, exact_tests=0)
    try:
        verified = _verified_artifact(artifact, manifest_path)
    except Exception as error:
        return _head(
            "artifact_error",
            errors=[str(error)],
            candidate_count=0,
            exact_tests=0,
            matched_component_count=0,
            matched_countries=[],
        )
    return _resolve_point_verified(
        verified,
        longitude,
        latitude,
        geometry_cache=geometry_cache,
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _timing_summary(values: list[float]) -> dict[str, float]:
    return {
        "min": round(min(values), 6) if values else 0.0,
        "p50": round(_percentile(values, 0.50), 6),
        "p95": round(_percentile(values, 0.95), 6),
        "max": round(max(values), 6) if values else 0.0,
    }


def _rss_high_water_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _load_queries(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read benchmark queries {path}: {error}") from error
    if isinstance(value, dict):
        value = value.get("queries")
    if not isinstance(value, list) or not value:
        raise RuntimeError(
            "benchmark input must be a non-empty JSON array or queries array"
        )
    queries = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict) or "lon" not in item or "lat" not in item:
            raise RuntimeError(f"benchmark query {index} must have lon and lat")
        queries.append(item)
    return queries


def benchmark_artifact(
    artifact: Path,
    query_path: Path,
    *,
    iterations: int = 100,
    open_iterations: int = 20,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    if iterations <= 0 or open_iterations <= 0:
        raise ValueError("benchmark iteration counts must be positive")
    verified = _verified_artifact(artifact, manifest_path)
    queries = _load_queries(query_path)
    rss_start = _rss_high_water_bytes()
    open_timings: list[float] = []
    for _ in range(open_iterations):
        started = time.perf_counter_ns()
        connection = sqlite3.connect(f"file:{artifact.resolve()}?mode=ro", uri=True)
        _validate_artifact(connection)
        connection.execute("SELECT COUNT(*) FROM component_rtree").fetchone()
        connection.close()
        open_timings.append((time.perf_counter_ns() - started) / 1_000_000)
    rss_after_open = _rss_high_water_bytes()

    connection = sqlite3.connect(f"file:{artifact.resolve()}?mode=ro", uri=True)
    geometry_cache: dict[tuple[str, int], Any] = {}
    results = [
        _resolve_point_verified(
            verified,
            query["lon"],
            query["lat"],
            connection=connection,
            geometry_cache=geometry_cache,
        )
        for query in queries
    ]
    rss_after_first_lookup_set = _rss_high_water_bytes()
    timings: list[float] = []
    for _ in range(iterations):
        for query in queries:
            started = time.perf_counter_ns()
            _resolve_point_verified(
                verified,
                query["lon"],
                query["lat"],
                connection=connection,
                geometry_cache=geometry_cache,
            )
            timings.append((time.perf_counter_ns() - started) / 1_000)
    connection.close()
    rss_after_benchmark = _rss_high_water_bytes()
    fanout = [float(result["candidate_count"]) for result in results]
    mismatches = []
    for index, (query, result) in enumerate(zip(queries, results), 1):
        expected = query.get("expected")
        if "expected" in query and expected != result.get("country"):
            mismatches.append(
                {"query": index, "expected": expected, "actual": result.get("country")}
            )
        expected_reason = query.get("expected_reason")
        if expected_reason is not None and expected_reason != result["reason"]:
            mismatches.append(
                {
                    "query": index,
                    "expected_reason": expected_reason,
                    "actual_reason": result["reason"],
                }
            )
    return {
        "report_version": REPORT_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "research_only": True,
        "artifact": {
            "path": str(artifact.resolve()),
            "size_bytes": artifact.stat().st_size,
            "sha256": verified.sha256,
            "manifest_path": str(verified.manifest_path.resolve()),
            "manifest_sha256": sha256_file(verified.manifest_path),
        },
        "queries": {
            "path": str(query_path.resolve()),
            "sha256": sha256_file(query_path),
            "count": len(queries),
            "iterations": iterations,
            "mismatches": mismatches,
        },
        "local_sqlite_open_and_metadata_milliseconds": _timing_summary(open_timings),
        "warm_lookup_microseconds": _timing_summary(timings),
        "candidate_fanout": _timing_summary(fanout),
        "decision_reasons": dict(
            collections.Counter(item["reason"] for item in results)
        ),
        "resource_proxy": {
            "process_rss_high_water_start_bytes": rss_start,
            "after_local_open_bytes": rss_after_open,
            "after_first_lookup_set_bytes": rss_after_first_lookup_set,
            "after_benchmark_bytes": rss_after_benchmark,
            "observed_high_water_delta_bytes": max(0, rss_after_benchmark - rss_start),
            "scope": "native local Python process including SQLite and Shapely allocations",
            "warning": (
                "A process high-water proxy, not incremental Worker/wasm heap. "
                "SQLite local open is lazy and is not artifact download or Worker "
                "deserialization time."
            ),
        },
        "results": [
            {"query": index, "label": query.get("label"), **result}
            for index, (query, result) in enumerate(zip(queries, results), 1)
        ],
    }


def _artifact_descriptor(path: Path) -> dict[str, Any]:
    verified = _verified_artifact(path)
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        metadata = _validate_artifact(connection)
    finally:
        connection.close()
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": verified.sha256,
        "manifest_path": str(verified.manifest_path.resolve()),
        "manifest_sha256": sha256_file(verified.manifest_path),
        "overture_release": metadata.get("overture_release"),
        "division_area_sha256": metadata.get("division_area_sha256"),
        "claim_policy": metadata.get("claim_policy"),
        "geometry_semantics": metadata.get("geometry_semantics"),
        "simplify_tolerance_degrees": float(
            metadata.get("simplify_tolerance_degrees", "0")
        ),
    }


def _diagnostic_corpus(
    boundary_source: Path,
    *,
    seed: int,
    global_points: int,
    boundary_points: int,
    jitters: tuple[float, ...],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if global_points < 0 or boundary_points < 0:
        raise ValueError("diagnostic point counts cannot be negative")
    if any(not math.isfinite(item) or item <= 0 for item in jitters):
        raise ValueError("diagnostic jitters must be finite positive values")
    randomizer = random.Random(seed)
    corpus: list[dict[str, Any]] = [
        {
            "group": "global",
            "lon": randomizer.uniform(-180, 180),
            "lat": randomizer.uniform(-90, 90),
        }
        for _ in range(global_points)
    ]
    boundary_verified = _verified_artifact(boundary_source)
    connection = sqlite3.connect(f"file:{boundary_source.resolve()}?mode=ro", uri=True)
    try:
        _validate_artifact(connection)
        component_rows = connection.execute(
            "SELECT component_id, geometry_id FROM components ORDER BY component_id"
        ).fetchall()
        if boundary_points and not component_rows:
            raise RuntimeError("boundary source artifact has no components")
        selected = sorted(
            component_rows,
            key=lambda row: hashlib.sha256(f"{seed}:{int(row[0])}".encode()).digest(),
        )
        boundaries: list[dict[str, Any]] = []
        boundary_source_reasons: collections.Counter[str] = collections.Counter()
        boundary_cache: dict[tuple[str, int], Any] = {}
        examined = 0
        max_jitter = max(jitters, default=0.0)
        for component_id, geometry_id in selected:
            if len(boundaries) == boundary_points:
                break
            examined += 1
            wkb_row = connection.execute(
                "SELECT wkb FROM geometries WHERE geometry_id = ?", (geometry_id,)
            ).fetchone()
            if wkb_row is None:
                raise RuntimeError(
                    f"boundary source component {component_id} has no geometry"
                )
            geometry = shapely.from_wkb(bytes(wkb_row[0]))
            if geometry.is_empty or not isinstance(geometry, Polygon):
                raise RuntimeError(
                    f"boundary source component {component_id} is not a polygon"
                )
            rings = [geometry.exterior, *geometry.interiors]
            digest = hashlib.sha256(
                f"{seed}:{int(component_id)}:vertex".encode()
            ).digest()
            ring = rings[int.from_bytes(digest[:4], "big") % len(rings)]
            coordinates = list(ring.coords)[:-1]
            if not coordinates:
                raise RuntimeError(
                    f"boundary source component {component_id} has no ring vertices"
                )
            coordinate = coordinates[
                int.from_bytes(digest[4:8], "big") % len(coordinates)
            ]
            longitude = float(coordinate[0])
            latitude = float(coordinate[1])
            point = Point(longitude, latitude)
            if not geometry.boundary.covers(point):
                raise RuntimeError(
                    f"boundary source component {component_id} vertex is not on boundary"
                )
            if abs(latitude) + max_jitter > 90:
                continue
            result = _resolve_point_verified(
                boundary_verified,
                longitude,
                latitude,
                connection=connection,
                geometry_cache=boundary_cache,
            )
            boundary_source_reasons[result["reason"]] += 1
            if result["reason"] != "boundary":
                continue
            boundaries.append(
                {
                    "group": "boundary-derived",
                    "component_id": int(component_id),
                    "lon": longitude,
                    "lat": latitude,
                }
            )
        if len(boundaries) != boundary_points:
            raise RuntimeError(
                f"found only {len(boundaries)} predicate-verified boundary points; "
                f"requested {boundary_points}"
            )
    finally:
        connection.close()
    corpus.extend(boundaries)
    directions = ((1, 0), (-1, 0), (0, 1), (0, -1))
    for boundary in boundaries:
        for epsilon in jitters:
            for longitude_direction, latitude_direction in directions:
                corpus.append(
                    {
                        "group": "boundary-jitter",
                        "component_id": boundary["component_id"],
                        "epsilon": epsilon,
                        "lon": (
                            (boundary["lon"] + longitude_direction * epsilon + 180)
                            % 360
                        )
                        - 180,
                        "lat": boundary["lat"] + latitude_direction * epsilon,
                    }
                )
    corpus_bytes = json.dumps(corpus, sort_keys=True, separators=(",", ":")).encode()
    counts = collections.Counter(item["group"] for item in corpus)
    return corpus, {
        "seed": seed,
        "global_generation": "random.Random(seed).uniform(-180,180), uniform(-90,90)",
        "boundary_generation": (
            "boundary-source components ordered by SHA-256(seed:component_id); "
            "deterministic stored ring vertex retained only when the source "
            "artifact resolves it as boundary"
        ),
        "boundary_label_note": (
            "Every boundary-derived point is a stored polygon vertex, is covered by "
            "that polygon boundary, and must resolve as boundary in the source artifact."
        ),
        "boundary_candidates_examined": examined,
        "boundary_source_reason_counts": dict(sorted(boundary_source_reasons.items())),
        "boundary_source_accepted_reason_counts": {"boundary": len(boundaries)},
        "jitter_generation": "cardinal +/- longitude/latitude offsets at each epsilon",
        "jitters_degrees": list(jitters),
        "counts": dict(sorted(counts.items())),
        "total": len(corpus),
        "sha256": _sha256_bytes(corpus_bytes),
    }


def _resolve_corpus(
    artifact: Path, corpus: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], float]:
    verified = _verified_artifact(artifact)
    connection = sqlite3.connect(f"file:{artifact.resolve()}?mode=ro", uri=True)
    cache: dict[tuple[str, int], Any] = {}
    results = []
    started = time.monotonic()
    try:
        for point in corpus:
            result = _resolve_point_verified(
                verified,
                point["lon"],
                point["lat"],
                connection=connection,
                geometry_cache=cache,
            )
            results.append(
                {
                    "target": (
                        result["country"] if result["decision"] == "route" else "HEAD"
                    ),
                    "reason": result["reason"],
                }
            )
    finally:
        connection.close()
    return results, round(time.monotonic() - started, 6)


def compare_artifacts(
    oracle: Path,
    candidates: list[tuple[str, Path]],
    *,
    boundary_source: Path,
    seed: int = 20260712,
    global_points: int = 5_000,
    boundary_points: int = 200,
    jitters: tuple[float, ...] = (0.0001, 0.001, 0.0025),
    allow_cross_source: bool = False,
) -> dict[str, Any]:
    """Compare route targets over a deterministic global/boundary corpus."""
    if not candidates:
        raise ValueError("at least one candidate artifact is required")
    labels = [label for label, _ in candidates]
    if len(set(labels)) != len(labels):
        raise ValueError("candidate labels must be unique")
    oracle_descriptor = _artifact_descriptor(oracle)
    boundary_descriptor = _artifact_descriptor(boundary_source)
    candidate_descriptors = {
        label: _artifact_descriptor(path) for label, path in candidates
    }
    descriptors = [
        oracle_descriptor,
        boundary_descriptor,
        *candidate_descriptors.values(),
    ]
    provenance = {
        (item["overture_release"], item["division_area_sha256"]) for item in descriptors
    }
    if len(provenance) != 1 and not allow_cross_source:
        raise RuntimeError(
            "comparison artifacts do not share one Overture release and source "
            "parquet SHA-256; pass allow_cross_source only for an intentional "
            "cross-source diagnostic"
        )
    corpus, corpus_metadata = _diagnostic_corpus(
        boundary_source,
        seed=seed,
        global_points=global_points,
        boundary_points=boundary_points,
        jitters=jitters,
    )
    oracle_results, oracle_seconds = _resolve_corpus(oracle, corpus)
    group_names = sorted({item["group"] for item in corpus})
    comparisons = []
    for label, path in candidates:
        candidate_results, elapsed = _resolve_corpus(path, corpus)
        groups: dict[str, dict[str, Any]] = {}
        for group in group_names:
            indexes = [
                index for index, point in enumerate(corpus) if point["group"] == group
            ]
            transitions: collections.Counter[str] = collections.Counter()
            reason_transitions: collections.Counter[str] = collections.Counter()
            false_unique = false_negative = wrong_country = drift = 0
            for index in indexes:
                expected = oracle_results[index]
                actual = candidate_results[index]
                if expected["target"] != actual["target"]:
                    drift += 1
                    transitions[f"{expected['target']}->{actual['target']}"] += 1
                    if expected["target"] == "HEAD" and actual["target"] != "HEAD":
                        false_unique += 1
                    elif expected["target"] != "HEAD" and actual["target"] == "HEAD":
                        false_negative += 1
                    else:
                        wrong_country += 1
                if expected["reason"] != actual["reason"]:
                    reason_transitions[f"{expected['reason']}->{actual['reason']}"] += 1
            groups[group] = {
                "queries": len(indexes),
                "route_target_drift": drift,
                "false_unique_routes": false_unique,
                "false_negative_routes": false_negative,
                "wrong_country_routes": wrong_country,
                "route_target_transitions": dict(sorted(transitions.items())),
                "reason_drift": sum(reason_transitions.values()),
                "reason_transitions": dict(sorted(reason_transitions.items())),
            }
        comparisons.append(
            {
                "label": label,
                "artifact": candidate_descriptors[label],
                "elapsed_seconds": elapsed,
                "totals": {
                    "queries": len(corpus),
                    "route_target_drift": sum(
                        group["route_target_drift"] for group in groups.values()
                    ),
                    "false_unique_routes": sum(
                        group["false_unique_routes"] for group in groups.values()
                    ),
                    "false_negative_routes": sum(
                        group["false_negative_routes"] for group in groups.values()
                    ),
                    "wrong_country_routes": sum(
                        group["wrong_country_routes"] for group in groups.values()
                    ),
                },
                "groups": groups,
            }
        )
    return {
        "diagnostic_version": 1,
        "research_only": True,
        "decision_key": "routed country code, otherwise HEAD; reason drift is separate",
        "cross_source_comparison": len(provenance) != 1,
        "corpus": corpus_metadata,
        "oracle": {
            "artifact": oracle_descriptor,
            "elapsed_seconds": oracle_seconds,
        },
        "boundary_source": boundary_descriptor,
        "comparisons": comparisons,
    }


def _candidate_argument(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("candidate must be LABEL=PATH")
    label, path = value.split("=", 1)
    if not label or not path:
        raise argparse.ArgumentTypeError("candidate must be LABEL=PATH")
    return label, Path(path)


def _jitter_argument(value: str) -> tuple[float, ...]:
    try:
        result = tuple(float(item) for item in value.split(",") if item)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "jitters must be comma-separated numbers"
        ) from error
    if not result:
        raise argparse.ArgumentTypeError("at least one jitter is required")
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="build a research-only SQLite artifact")
    build.add_argument("--division-area-parquet", type=Path, required=True)
    build.add_argument("--division-parquet", type=Path)
    build.add_argument("--overture-release", required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--manifest", type=Path)
    build.add_argument("--audit-sidecar", type=Path)
    build.add_argument("--expected-country-count", type=int)
    build.add_argument("--expected-country-codes", type=Path)
    build.add_argument(
        "--extraction-sql",
        type=Path,
        default=Path(__file__).with_name("extract_country_router.sql"),
        help="SQL template whose bytes are bound into provenance",
    )
    build.add_argument(
        "--claim-policy",
        choices=("all-claims", "territorial-primary"),
        default="all-claims",
    )
    build.add_argument(
        "--simplify-tolerance",
        type=float,
        default=0.0,
        help="topology-preserving tolerance in degrees; 0 is the exact baseline",
    )
    build.add_argument("--overwrite", action="store_true")
    query = commands.add_parser("query", help="resolve one point")
    query.add_argument("--artifact", type=Path, required=True)
    query.add_argument("--manifest", type=Path)
    query.add_argument("--lon", type=float, required=True)
    query.add_argument("--lat", type=float, required=True)
    benchmark = commands.add_parser("benchmark", help="measure a JSON query set")
    benchmark.add_argument("--artifact", type=Path, required=True)
    benchmark.add_argument("--manifest", type=Path)
    benchmark.add_argument("--queries", type=Path, required=True)
    benchmark.add_argument("--report", type=Path, required=True)
    benchmark.add_argument("--iterations", type=int, default=100)
    benchmark.add_argument("--open-iterations", type=int, default=20)
    benchmark.add_argument(
        "--fail-on-mismatch",
        action="store_true",
        help="exit nonzero after writing the report when expectations differ",
    )
    compare = commands.add_parser(
        "compare", help="run a deterministic oracle/candidate drift diagnostic"
    )
    compare.add_argument("--oracle", type=Path, required=True)
    compare.add_argument("--boundary-source", type=Path, required=True)
    compare.add_argument(
        "--candidate",
        type=_candidate_argument,
        action="append",
        required=True,
        help="repeatable LABEL=PATH candidate",
    )
    compare.add_argument("--seed", type=int, default=20260712)
    compare.add_argument("--global-points", type=int, default=5_000)
    compare.add_argument("--boundary-points", type=int, default=200)
    compare.add_argument(
        "--jitters",
        type=_jitter_argument,
        default=(0.0001, 0.001, 0.0025),
    )
    compare.add_argument(
        "--allow-cross-source",
        action="store_true",
        help="permit intentional comparison across release/source provenance",
    )
    compare.add_argument("--report", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    if args.command == "build":
        result = build_artifact(
            args.division_area_parquet,
            args.output,
            args.overture_release,
            manifest_path=args.manifest,
            audit_sidecar_path=args.audit_sidecar,
            division_path=args.division_parquet,
            expected_country_count=args.expected_country_count,
            expected_country_codes=args.expected_country_codes,
            extraction_sql=args.extraction_sql,
            claim_policy=args.claim_policy,
            simplify_tolerance=args.simplify_tolerance,
            overwrite=args.overwrite,
        )
    elif args.command == "query":
        result = resolve_point(
            args.artifact, args.lon, args.lat, manifest_path=args.manifest
        )
    elif args.command == "benchmark":
        result = benchmark_artifact(
            args.artifact,
            args.queries,
            iterations=args.iterations,
            open_iterations=args.open_iterations,
            manifest_path=args.manifest,
        )
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_bytes(_json_bytes(result))
        if args.fail_on_mismatch and result["queries"]["mismatches"]:
            print(json.dumps(result, sort_keys=True, indent=2))
            raise SystemExit(1)
    else:
        result = compare_artifacts(
            args.oracle,
            args.candidate,
            boundary_source=args.boundary_source,
            seed=args.seed,
            global_points=args.global_points,
            boundary_points=args.boundary_points,
            jitters=args.jitters,
            allow_cross_source=args.allow_cross_source,
        )
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_bytes(_json_bytes(result))
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
