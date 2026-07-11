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
import json
import numbers
import sys
from collections import defaultdict
from pathlib import Path

try:
    import h3
    H3_AVAILABLE = True
except ImportError:
    H3_AVAILABLE = False

try:
    from shapely import wkb as shapely_wkb
    from shapely.affinity import translate
    from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
    from shapely.ops import unary_union
    from shapely.strtree import STRtree
    from shapely.validation import make_valid
    SHAPELY_AVAILABLE = True
except ImportError:
    SHAPELY_AVAILABLE = False


def parse_args():
    p = argparse.ArgumentParser(description="Build H3 country index")
    p.add_argument("--parquet", type=str, required=True, help="Path or S3 URI to division_area parquet")
    p.add_argument("--resolution", type=int, default=2, help="H3 resolution")
    p.add_argument("--output", type=Path, default=Path("country_h3.json"), help="Output JSON")
    p.add_argument("--sqlite", type=Path, default=None, help="Optional SQLite")
    p.add_argument("--simplify-tol", type=float, default=0.005, help="Simplify tolerance")
    p.add_argument("--countries", type=str, default=None, help="Comma separated ISO codes")
    p.add_argument(
        "--compare-resolutions",
        type=str,
        default=None,
        help="Comma separated resolutions to compare without writing index artifacts",
    )
    p.add_argument("--report", type=Path, default=None, help="Optional comparison report JSON")
    return p.parse_args()


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
    country_geoms = defaultdict(list)
    for country, g_wkb in rows:
        if g_wkb is None:
            continue
        try:
            if isinstance(g_wkb, str):
                geom = shapely_wkb.loads(bytes.fromhex(g_wkb))
            else:
                geom = shapely_wkb.loads(bytes(g_wkb))
            if not geom.is_valid:
                geom = make_valid(geom)
            country_geoms[country].append(geom)
        except Exception as e:
            print(f"Warn {country}: {e}", file=sys.stderr)
            continue
    merged = {}
    for country, geoms in country_geoms.items():
        if len(geoms) == 1:
            merged[country] = geoms[0]
        else:
            try:
                merged[country] = unary_union(geoms)
            except Exception:
                merged[country] = geoms[0]
    print(f"Merged {len(merged)} countries")
    return merged


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
        countries_filter = [c.strip().upper() for c in args.countries.split(",")]
    try:
        country_geoms = load_country_geoms(args.parquet, countries_filter)
    except Exception as e:
        print(f"Failed load: {e}, using dummy bboxes", file=sys.stderr)
        from shapely.geometry import box
        country_geoms = {
            "JP": box(122.0, 20.0, 154.0, 46.0),
            "US": box(-125.0, 24.0, -66.0, 50.0),
            "RU": box(-180.0, 41.0, 180.0, 82.0),
            "CA": box(-141.0, 41.0, -52.0, 83.0),
        }
    if args.compare_resolutions:
        resolutions = [int(value.strip()) for value in args.compare_resolutions.split(",")]
        report = compare_resolutions(country_geoms, resolutions, args.simplify_tol)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps({"resolutions": report}, indent=2) + "\n")
            print(f"Wrote {args.report}")
        return

    interior, boundary, _ = build_h3_index(
        country_geoms,
        resolution=args.resolution,
        simplify_tol=args.simplify_tol,
    )
    write_json_output(args.output, args.resolution, interior, boundary)
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
