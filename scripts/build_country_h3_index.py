#!/usr/bin/env python3
"""
Prototype H3 country router index builder.
Reads Overture division-area country polygons and builds H3 cell -> country mapping.
Interior cells map directly; boundary cells store candidate countries + simplified WKB.

Usage:
    python scripts/build_country_h3_index.py --parquet /path/to/division_area.parquet --resolution 2 --output country_h3.json
"""

import argparse
import json
import sys
from pathlib import Path
from collections import defaultdict

try:
    import h3
    H3_AVAILABLE = True
except ImportError:
    H3_AVAILABLE = False

try:
    from shapely import wkb as shapely_wkb
    from shapely.geometry import mapping
    from shapely.ops import unary_union
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


def build_h3_index(country_geoms, resolution=2, simplify_tol=0.005):
    if not H3_AVAILABLE:
        raise RuntimeError("h3 not available")
    cell_to_countries = defaultdict(set)
    print(f"Polyfilling {len(country_geoms)} countries at res {resolution}...")
    for country, geom in country_geoms.items():
        try:
            gj = mapping(geom)
            cells = set()
            if gj["type"] == "Polygon":
                if hasattr(h3, "geo_to_cells"):
                    cells = h3.geo_to_cells(gj, resolution)
                else:
                    cells = set(h3.polyfill(gj, resolution, geo_json_conformant=True))
            elif gj["type"] == "MultiPolygon":
                for poly_coords in gj["coordinates"]:
                    poly_gj = {"type": "Polygon", "coordinates": poly_coords}
                    if hasattr(h3, "geo_to_cells"):
                        cells |= h3.geo_to_cells(poly_gj, resolution)
                    else:
                        cells |= set(h3.polyfill(poly_gj, resolution, geo_json_conformant=True))
            for cell in cells:
                cell_to_countries[cell].add(country)
        except Exception as e:
            print(f"Warn polyfill {country}: {e}", file=sys.stderr)
            continue
    print(f"Total cells {len(cell_to_countries)}")
    boundary_cells = {c: cs for c, cs in cell_to_countries.items() if len(cs) > 1}
    interior_cells = {c: next(iter(cs)) for c, cs in cell_to_countries.items() if len(cs) == 1}
    print(f"Interior {len(interior_cells)} Boundary {len(boundary_cells)}")
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
    for cell, countries in boundary_cells.items():
        polys = {}
        for c in countries:
            try:
                polys[c] = simplified_geoms[c].wkb_hex
            except Exception:
                continue
        boundary_detail[cell] = {"candidates": sorted(list(countries)), "polys": polys}
    return interior_cells, boundary_detail, cell_to_countries


def write_json_output(output_path: Path, resolution: int, interior, boundary):
    data = {
        "resolution": resolution,
        "generated": __import__("datetime").datetime.now().isoformat(),
        "cell_count": len(interior) + len(boundary),
        "interior_count": len(interior),
        "boundary_count": len(boundary),
        "cells": interior,
        "boundary": boundary
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Wrote {output_path} ({output_path.stat().st_size / 1024:.1f} KB)")


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
    interior, boundary, _ = build_h3_index(country_geoms, resolution=args.resolution, simplify_tol=args.simplify_tol)
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
