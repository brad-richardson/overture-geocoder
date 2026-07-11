#!/usr/bin/env python3
"""
Prototype H3 country router index builder.
Reads Overture division-area country polygons and builds H3 cell -> country mapping.

Every H3 cell is tested against the country polygons. A cell is considered interior
only when exactly one country covers the entire cell. Cells that merely intersect a
country (coasts, holes, and borders) are boundary candidates and require exact point
containment at query time.

Usage:
    python scripts/build_country_h3_index.py --parquet /path/to/division_area.parquet --resolution 2 --output country_h3.json
    python scripts/build_country_h3_index.py --parquet /path/to/division_area.parquet --compare-resolutions 2,3 --report h3-sizes.json
"""

import argparse
import datetime
import hashlib
import json
import numbers
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    import h3
    H3_AVAILABLE = True
except ImportError:
    H3_AVAILABLE = False

try:
    import shapely
    from shapely import wkb as shapely_wkb
    from shapely.affinity import translate
    from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
    from shapely.ops import unary_union
    from shapely.strtree import STRtree
    from shapely.validation import make_valid
    SHAPELY_AVAILABLE = True
except ImportError:
    SHAPELY_AVAILABLE = False


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Build H3 country index")
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--parquet",
        type=str,
        help="Path or S3 URI to division_area parquet",
    )
    source.add_argument(
        "--demo-fixtures",
        action="store_true",
        help="Use explicit synthetic rectangles instead of release data",
    )
    p.add_argument("--resolution", type=int, default=2, help="H3 resolution")
    p.add_argument("--output", type=Path, default=Path("country_h3.json"), help="Output JSON")
    p.add_argument("--sqlite", type=Path, default=None, help="Optional SQLite")
    p.add_argument("--simplify-tol", type=float, default=0.005, help="Simplify tolerance")
    p.add_argument("--countries", type=str, default=None, help="Comma separated ISO codes")
    p.add_argument("--expected-country-count", type=int, default=None)
    p.add_argument("--expected-country-codes", type=Path, default=None,
                   help="JSON array (or country_codes object) defining the exact expected set")
    p.add_argument("--allow-country-subset", action="store_true",
                   help="Explicitly allow --countries subset builds")
    p.add_argument("--overture-release", default=None,
                   help="Required release provenance for remote parquet input")
    p.add_argument(
        "--compare-resolutions",
        type=str,
        default=None,
        help="Comma separated resolutions to compare without writing index artifacts",
    )
    p.add_argument("--report", type=Path, default=None, help="Optional comparison report JSON")
    p.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Completeness/provenance manifest (default: <output>.manifest.json)",
    )
    return p.parse_args(argv)


def _merge_country_components(country_components):
    """Merge every decoded component, failing rather than dropping geometry."""
    merged = {}
    for country, geoms in country_components.items():
        if not country or not geoms:
            raise RuntimeError(f"Country {country!r} has no usable polygon components")
        try:
            geom = geoms[0] if len(geoms) == 1 else unary_union(geoms)
            if not geom.is_valid:
                geom = make_valid(geom)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to merge all {len(geoms)} components for {country}"
            ) from exc
        if geom.is_empty:
            raise RuntimeError(f"Merged geometry for {country} is empty")
        if not isinstance(geom, (Polygon, MultiPolygon, GeometryCollection)):
            raise RuntimeError(
                f"Merged geometry for {country} is not polygonal: {geom.geom_type}"
            )
        merged[country] = geom
    if not merged:
        raise RuntimeError("No country geometries were decoded")
    return merged


def load_country_geoms(parquet_path: str, countries_filter=None):
    import duckdb
    con = duckdb.connect()
    where_clause = "subtype = 'country'"
    if countries_filter:
        codes = ", ".join(f"'{c}'" for c in countries_filter)
        where_clause += f" AND country IN ({codes})"
    for geom_col in ["geometry", "geom", "wkb"]:
        try:
            con.execute(f"SELECT country, {geom_col} as g FROM read_parquet('{parquet_path}') WHERE {where_clause} LIMIT 1").fetchone()
            geometry_col = geom_col
            break
        except Exception:
            continue
    else:
        raise RuntimeError("No geometry column found")
    print(f"Using {geometry_col}")
    rows = con.execute(f"SELECT country, {geometry_col} as g FROM read_parquet('{parquet_path}') WHERE {where_clause}").fetchall()
    con.close()
    print(f"Fetched {len(rows)} components")
    country_components = defaultdict(list)
    for row_number, (country, g_wkb) in enumerate(rows, 1):
        if not country:
            raise RuntimeError(f"Country component row {row_number} has no country code")
        if g_wkb is None:
            raise RuntimeError(f"Country component row {row_number} ({country}) has no geometry")
        try:
            if isinstance(g_wkb, str):
                geom = shapely_wkb.loads(bytes.fromhex(g_wkb))
            else:
                geom = shapely_wkb.loads(bytes(g_wkb))
            if not geom.is_valid:
                geom = make_valid(geom)
            if geom.is_empty:
                raise ValueError("decoded geometry is empty")
            country_components[country].append(geom)
        except Exception as e:
            raise RuntimeError(
                f"Failed to decode country component row {row_number} ({country}): {e}"
            ) from e
    merged = _merge_country_components(country_components)
    if countries_filter:
        missing = sorted(set(countries_filter) - set(merged))
        if missing:
            raise RuntimeError(
                "Requested countries missing from decoded geometry: "
                + ", ".join(missing)
            )
    print(f"Merged {len(merged)} countries")
    return merged, {
        "source_component_count": len(rows),
        "decoded_component_count": sum(len(v) for v in country_components.values()),
        "country_count": len(merged),
        "country_codes": sorted(merged),
        "components_by_country": {
            country: len(country_components[country]) for country in sorted(merged)
        },
    }


def demo_country_geoms():
    """Small synthetic fixtures, available only through --demo-fixtures."""
    from shapely.geometry import box

    geoms = {
        "JP": box(122.0, 20.0, 154.0, 46.0),
        "US": box(-125.0, 24.0, -66.0, 50.0),
        "RU": box(-180.0, 41.0, 180.0, 82.0),
        "CA": box(-141.0, 41.0, -52.0, 83.0),
    }
    return geoms, {
        "source_component_count": len(geoms),
        "decoded_component_count": len(geoms),
        "country_count": len(geoms),
        "country_codes": sorted(geoms),
        "components_by_country": {country: 1 for country in sorted(geoms)},
    }


def _cells_at_resolution(resolution):
    """Return all H3 cells at a resolution across h3-py 3.x and 4.x."""
    if hasattr(h3, "get_res0_cells"):
        roots = h3.get_res0_cells()
        if resolution == 0:
            return list(roots)
        return [child for root in roots for child in h3.cell_to_children(root, resolution)]

    roots = h3.get_res0_indexes()
    if resolution == 0:
        return list(roots)
    return [child for root in roots for child in h3.h3_to_children(root, resolution)]


def _cell_boundary(cell):
    """Return (lat, lon) pairs across h3-py 3.x and 4.x."""
    if hasattr(h3, "cell_to_boundary"):
        return h3.cell_to_boundary(cell)
    return h3.h3_to_geo_boundary(cell)


def _unwrap_ring(coords):
    """Unwrap a lon/lat ring so adjacent longitudes never jump over 180 degrees."""
    unwrapped = []
    previous_lon = None
    for coord in coords:
        lon, lat = coord[:2]
        if previous_lon is not None:
            while lon - previous_lon > 180:
                lon -= 360
            while lon - previous_lon < -180:
                lon += 360
        unwrapped.append((lon, lat))
        previous_lon = lon
    return unwrapped


def _shift_ring_near(ring, reference_lon):
    if not ring:
        return ring
    mean_lon = sum(lon for lon, _ in ring) / len(ring)
    shift = round((reference_lon - mean_lon) / 360) * 360
    return [(lon + shift, lat) for lon, lat in ring]


def _unwrap_polygon(poly):
    exterior = _unwrap_ring(poly.exterior.coords)
    reference_lon = sum(lon for lon, _ in exterior) / len(exterior)
    holes = [
        _shift_ring_near(_unwrap_ring(interior.coords), reference_lon)
        for interior in poly.interiors
    ]
    return Polygon(exterior, holes)


def _unwrap_geometry(geom):
    """Make dateline-crossing rings usable by planar Shapely predicates."""
    if isinstance(geom, Polygon):
        return _unwrap_polygon(geom)
    if isinstance(geom, MultiPolygon):
        return MultiPolygon([_unwrap_polygon(poly) for poly in geom.geoms])
    if isinstance(geom, GeometryCollection):
        polygonal = [
            _unwrap_geometry(part)
            for part in geom.geoms
            if isinstance(part, (Polygon, MultiPolygon, GeometryCollection))
        ]
        return unary_union([part for part in polygonal if not part.is_empty])
    raise TypeError(f"Unsupported country geometry: {geom.geom_type}")


def _cell_polygon(cell):
    # h3 returns lat/lon; Shapely expects lon/lat. Unwrapping prevents a small
    # dateline cell from becoming a nearly global planar polygon.
    ring = _unwrap_ring([(lon, lat) for lat, lon in _cell_boundary(cell)])
    return Polygon(ring)


class _CountryGeometryIndex:
    """Spatial index containing shifted copies for robust dateline matching."""

    def __init__(self, country_geoms):
        self.records = []
        for country, geom in country_geoms.items():
            normalized = _unwrap_geometry(geom)
            for xoff in (-360, 0, 360):
                self.records.append((country, translate(normalized, xoff=xoff)))
        self.geoms = [geom for _, geom in self.records]
        self.tree = STRtree(self.geoms)
        # Shapely 1.x returns geometries from query; 2.x returns integer indexes.
        self.index_by_id = {id(geom): i for i, geom in enumerate(self.geoms)}

    def intersecting_countries(self, cell_geom):
        matches = defaultdict(list)
        for hit in self.tree.query(cell_geom):
            if isinstance(hit, numbers.Integral):
                index = int(hit)
            else:
                index = self.index_by_id[id(hit)]
            country, geom = self.records[index]
            if geom.intersects(cell_geom):
                matches[country].append(geom)
        return matches


def build_h3_index(
    country_geoms,
    resolution=2,
    simplify_tol=0.005,
    candidate_cells=None,
):
    """Classify cells using exact polygon intersection and full-cell coverage.

    ``candidate_cells`` exists for focused tests. Normal builds deliberately scan
    the complete low-resolution H3 grid (5,882 cells at r2; 41,162 at r3), avoiding
    center-polyfill omissions for thin islands, coastlines, and borders.
    """
    if not H3_AVAILABLE:
        raise RuntimeError("h3 not available")
    cell_to_countries = defaultdict(set)
    cells = list(candidate_cells) if candidate_cells is not None else _cells_at_resolution(resolution)
    print(f"Classifying {len(cells)} cells against {len(country_geoms)} countries at res {resolution}...")
    geom_index = _CountryGeometryIndex(country_geoms)
    interior_cells = {}
    boundary_candidates = {}
    for cell in cells:
        cell_geom = _cell_polygon(cell)
        matches = geom_index.intersecting_countries(cell_geom)
        countries = set(matches)
        if not countries:
            continue
        cell_to_countries[cell].update(countries)
        covered_by = [
            country
            for country, shifted_geoms in matches.items()
            if any(geom.covers(cell_geom) for geom in shifted_geoms)
        ]
        if len(countries) == 1 and len(covered_by) == 1:
            interior_cells[cell] = covered_by[0]
        else:
            boundary_candidates[cell] = countries
    print(f"Total cells {len(cell_to_countries)}")
    print(f"Interior {len(interior_cells)} Boundary {len(boundary_candidates)}")
    simplified_geoms = {}
    for country, geom in country_geoms.items():
        try:
            simp = geom.simplify(simplify_tol, preserve_topology=True)
            if not simp.is_valid:
                simp = make_valid(simp)
            simplified_geoms[country] = simp
        except Exception:
            simplified_geoms[country] = geom
    boundary_detail = {}
    for cell, countries in boundary_candidates.items():
        polys = {}
        for c in countries:
            try:
                polys[c] = simplified_geoms[c].wkb_hex
            except Exception:
                continue
        boundary_detail[cell] = {"candidates": sorted(list(countries)), "polys": polys}
    return interior_cells, boundary_detail, cell_to_countries


def _output_data(resolution, interior, boundary, generated=None):
    return {
        "classification": "intersection-and-full-cell-coverage-v1",
        "resolution": resolution,
        "generated": (
            generated
            if generated is not None
            else datetime.datetime.now(datetime.timezone.utc).isoformat()
        ),
        "global_cell_count": len(_cells_at_resolution(resolution)),
        "cell_count": len(interior) + len(boundary),
        "interior_count": len(interior),
        "boundary_count": len(boundary),
        "cells": interior,
        "boundary": boundary,
    }


def estimate_serialized_size(resolution, interior, boundary):
    data = _output_data(resolution, interior, boundary, generated="")
    return len(json.dumps(data, separators=(",", ":")).encode("utf-8"))


def write_json_output(output_path: Path, resolution: int, interior, boundary):
    data = _output_data(resolution, interior, boundary)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Wrote {output_path} ({output_path.stat().st_size / 1024:.1f} KB)")


def compare_resolutions(country_geoms, resolutions, simplify_tol=0.005):
    report = []
    for resolution in resolutions:
        interior, boundary, _ = build_h3_index(
            country_geoms,
            resolution=resolution,
            simplify_tol=simplify_tol,
        )
        serialized_bytes = estimate_serialized_size(resolution, interior, boundary)
        report.append({
            "resolution": resolution,
            "global_cells": len(_cells_at_resolution(resolution)),
            "land_cells": len(interior) + len(boundary),
            "interior_cells": len(interior),
            "boundary_cells": len(boundary),
            "boundary_percent_of_land": round(
                100 * len(boundary) / max(1, len(interior) + len(boundary)), 2
            ),
            "estimated_compact_json_bytes": serialized_bytes,
        })
    print("resolution global land interior boundary boundary% compact-json")
    for row in report:
        print(
            f"{row['resolution']:>10} {row['global_cells']:>6} {row['land_cells']:>5} "
            f"{row['interior_cells']:>8} {row['boundary_cells']:>8} "
            f"{row['boundary_percent_of_land']:>8.2f} "
            f"{row['estimated_compact_json_bytes'] / 1024:>9.1f}KB"
        )
    return report


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_record(path):
    return {"path": str(path), "size_bytes": path.stat().st_size, "sha256": _sha256(path)}


def _expected_country_codes(path):
    value = json.loads(path.read_text())
    if isinstance(value, dict):
        value = value.get("country_codes")
    if not isinstance(value, list) or not all(isinstance(code, str) for code in value):
        raise ValueError("expected-country-codes must contain a JSON country-code array")
    return sorted({code.strip().upper() for code in value if code.strip()})


def _validate_release_provenance(parquet, overture_release):
    if not overture_release:
        raise ValueError("non-demo builds require --overture-release")
    if parquet.startswith("s3://"):
        match = re.search(r"/release/([^/]+)/", parquet)
        if not match:
            raise ValueError("remote parquet must use the standard /release/<tag>/ path")
        if match.group(1) != overture_release:
            raise ValueError(
                f"remote path release {match.group(1)} does not match {overture_release}"
            )


def write_manifest(path, args, source_stats, resolution_reports, artifacts):
    source = {
        "mode": "demo-fixtures" if args.demo_fixtures else "parquet",
        "parquet": args.parquet,
        "overture_release": args.overture_release,
        "countries_filter": (
            sorted(c.strip().upper() for c in args.countries.split(",") if c.strip())
            if args.countries else None
        ),
        "expected_country_count": args.expected_country_count,
        "expected_country_codes_file": (
            str(args.expected_country_codes) if args.expected_country_codes else None
        ),
        "allow_country_subset": args.allow_country_subset,
    }
    if args.parquet and not args.parquet.startswith("s3://"):
        local_source = Path(args.parquet)
        source["local_input"] = _artifact_record(local_source)
    manifest = {
        "schema_version": 1,
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": source,
        "completeness": source_stats,
        "classifier": "intersection-and-full-cell-coverage-v1",
        "simplify_tolerance_degrees": args.simplify_tol,
        "dependencies": {
            "h3": getattr(h3, "__version__", "unknown"),
            "shapely": getattr(shapely, "__version__", "unknown"),
        },
        "resolutions": resolution_reports,
        "artifacts": [_artifact_record(artifact) for artifact in artifacts],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {path}")


def main():
    args = parse_args()
    if not SHAPELY_AVAILABLE:
        print("shapely not available", file=sys.stderr)
        sys.exit(1)
    if not H3_AVAILABLE:
        print("h3 not available", file=sys.stderr)
        sys.exit(1)
    countries_filter = None
    if args.countries:
        countries_filter = [c.strip().upper() for c in args.countries.split(",") if c.strip()]
    expected_codes = None
    if not args.demo_fixtures:
        _validate_release_provenance(args.parquet, args.overture_release)
        if args.expected_country_count is None or args.expected_country_codes is None:
            raise ValueError(
                "release builds require --expected-country-count and --expected-country-codes"
            )
        expected_codes = _expected_country_codes(args.expected_country_codes)
        if args.expected_country_count != len(expected_codes):
            raise ValueError("expected country count does not match code manifest")
        if countries_filter and not args.allow_country_subset:
            raise ValueError("--countries requires explicit --allow-country-subset")
        if countries_filter and set(countries_filter) != set(expected_codes):
            raise ValueError("subset country filter must exactly match expected code manifest")
    if args.demo_fixtures:
        country_geoms, source_stats = demo_country_geoms()
    else:
        country_geoms, source_stats = load_country_geoms(args.parquet, countries_filter)
        if set(country_geoms) != set(expected_codes):
            raise RuntimeError("decoded countries do not exactly match expected code manifest")
    manifest_path = args.manifest or args.output.with_suffix(".manifest.json")
    if args.compare_resolutions:
        resolutions = [int(value.strip()) for value in args.compare_resolutions.split(",")]
        report = compare_resolutions(country_geoms, resolutions, args.simplify_tol)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps({"resolutions": report}, indent=2) + "\n")
            print(f"Wrote {args.report}")
        artifacts = [args.report] if args.report else []
        write_manifest(manifest_path, args, source_stats, report, artifacts)
        return

    interior, boundary, _ = build_h3_index(
        country_geoms,
        resolution=args.resolution,
        simplify_tol=args.simplify_tol,
    )
    write_json_output(args.output, args.resolution, interior, boundary)
    artifacts = [args.output]
    if args.sqlite:
        import sqlite3
        args.sqlite.parent.mkdir(parents=True, exist_ok=True)
        if args.sqlite.exists():
            args.sqlite.unlink()
        db = sqlite3.connect(args.sqlite)
        db.executescript("CREATE TABLE h3_cells(cell TEXT PRIMARY KEY, country TEXT, is_boundary BOOL); CREATE TABLE boundary_polys(cell TEXT, country TEXT, wkb_hex TEXT, PRIMARY KEY(cell,country));")
        db.executemany("INSERT INTO h3_cells VALUES (?,?,?)", [(c, country, 0) for c, country in interior.items()])
        db.executemany("INSERT INTO h3_cells VALUES (?,?,?)", [(c, ",".join(d["candidates"]), 1) for c, d in boundary.items()])
        b_rows = [(cell, country, wkb) for cell, d in boundary.items() for country, wkb in d["polys"].items()]
        db.executemany("INSERT INTO boundary_polys VALUES (?,?,?)", b_rows)
        db.commit()
        db.execute("VACUUM")
        db.close()
        print(f"Wrote SQLite {args.sqlite}")
        artifacts.append(args.sqlite)
    write_manifest(
        manifest_path,
        args,
        source_stats,
        [{
            "resolution": args.resolution,
            "global_cells": len(_cells_at_resolution(args.resolution)),
            "land_cells": len(interior) + len(boundary),
            "interior_cells": len(interior),
            "boundary_cells": len(boundary),
        }],
        artifacts,
    )
    if H3_AVAILABLE:
        tokyo_lat, tokyo_lon = 35.68, 139.69
        try:
            cell = h3.latlng_to_cell(tokyo_lat, tokyo_lon, args.resolution) if hasattr(h3, "latlng_to_cell") else h3.geo_to_h3(tokyo_lat, tokyo_lon, args.resolution)
            country = interior.get(cell)
            if country:
                print(f"Tokyo {tokyo_lat},{tokyo_lon} -> {cell} -> {country} interior")
            elif cell in boundary:
                print(f"Tokyo -> {cell} boundary {boundary[cell]['candidates']}")
            else:
                print(f"Tokyo cell {cell} not found")
        except Exception as e:
            print(f"Demo fail {e}")


if __name__ == "__main__":
    main()
