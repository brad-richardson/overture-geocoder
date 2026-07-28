#!/usr/bin/env python3
"""Regenerate tests/fixtures/reverse/cell-identifier-vectors-v1.json.

One committed vector file drives every implementation of the level-8 cell
identifier and the reverse leaf key: the Python mirrors and DuckDB SQL
(tests/test_reverse_cell_identifier_vectors.py), the real `places-transform-v1`
binary (same test), and the Rust `point_quadkey`
(`world_quadkey_matches_the_python_partition_contract` in
crates/geocoder-worker/src/places_pages.rs). Fully deterministic -- boundary
enumeration plus a fixed multiplicative spread, no runtime randomness -- so the
file regenerates byte-identically:

    python tests/generate_reverse_cell_identifier_vectors.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ADDRESS = _load("reverse_vectors_address", "scripts/address_construction_v1.py")
PARTITION = _load("reverse_vectors_partition", "scripts/places_partition.py")
REVERSE = _load("reverse_vectors_reverse", "scripts/reverse_shard_v1.py")

SCHEMA = "overture-reverse-cell-identifier-vectors-v1"
FIXTURE = ROOT / "tests/fixtures/reverse/cell-identifier-vectors-v1.json"
MAX_LEAF_LEVEL = REVERSE.MAX_SUB_CELL_LEVEL

LONGITUDE_ORIGIN = REVERSE.LONGITUDE_E7_ORIGIN
LATITUDE_ORIGIN = REVERSE.LATITUDE_E7_ORIGIN
LONGITUDE_PER_CELL = REVERSE.LONGITUDE_E7_PER_CELL
LATITUDE_PER_CELL = REVERSE.LATITUDE_E7_PER_CELL

# Cells whose sub-cell boundaries the vectors straddle: Tokyo (the dense tail),
# Seattle (the forward fixtures' cell), null island, a 72N cell where the
# longitude ceiling binds at L=4, and an 85S cell where it binds at L=2.
SUB_CELL_CELLS = ("b2e3", "c328", "8080", "e784", "0680")


def e7_points() -> list[tuple[int, int]]:
    """Deterministic E7 points spanning every documented seam of the grid."""
    points: list[tuple[int, int]] = []
    # The four world corners: the only in-world inputs that reach the clamp.
    for longitude in (-LONGITUDE_ORIGIN, LONGITUDE_ORIGIN):
        for latitude in (-LATITUDE_ORIGIN, LATITUDE_ORIGIN):
            points.append((longitude, latitude))
    # Every 16th cell boundary on both axes, one E7 unit either side, paired
    # with an equatorial and a mid/high-latitude far coordinate -- the strategy
    # of `boundary_e7_points` in tests/test_address_records_artifact.py.
    for index in range(0, REVERSE.CELL_GRID + 1, 16):
        longitude = index * LONGITUDE_PER_CELL - LONGITUDE_ORIGIN
        latitude = index * LATITUDE_PER_CELL - LATITUDE_ORIGIN
        for delta in (-1, 0, 1):
            if abs(longitude + delta) <= LONGITUDE_ORIGIN:
                points.append((longitude + delta, 0))
                points.append((longitude + delta, 620_000_001))
            if abs(latitude + delta) <= LATITUDE_ORIGIN:
                points.append((0, latitude + delta))
                points.append((-1_083_000_007, latitude + delta))
    # Sub-cell boundaries one E7 unit either side, per axis, at several depths.
    # The first E7 unit inside sub-cell k at level L is ceil(k * span / 2^L).
    for cell in SUB_CELL_CELLS:
        y, x = REVERSE.cell_yx(cell)
        base_longitude = x * LONGITUDE_PER_CELL - LONGITUDE_ORIGIN
        base_latitude = y * LATITUDE_PER_CELL - LATITUDE_ORIGIN
        interior_dx = LONGITUDE_PER_CELL // 3
        interior_dy = LATITUDE_PER_CELL // 3
        for level in (1, 5, 7):
            size = 1 << level
            for k in (1, size // 2, size - 1):
                dx = (k * LONGITUDE_PER_CELL + size - 1) // size
                dy = (k * LATITUDE_PER_CELL + size - 1) // size
                for delta in (-1, 0):
                    points.append((base_longitude + dx + delta, base_latitude + interior_dy))
                    points.append((base_longitude + interior_dx, base_latitude + dy + delta))
    # Polar rows (cell y = 0 and y = 255), equator, antimeridian, null island.
    for latitude in (-899_999_999, -893_000_003, 893_000_003, 899_999_999):
        points.append((123_456_789, latitude))
        points.append((-1_799_999_999, latitude))
    for longitude in (-1_800_000_000, -1_799_999_999, 1_799_999_999, 1_800_000_000):
        points.append((longitude, 351_234_567))
    points.extend([(0, 0), (-1, -1), (1, 1), (0, -1), (-1, 0)])
    # A deterministic interior spread, so the vectors are not only seams.
    for index in range(120):
        longitude = (index * 917_293_331) % (2 * LONGITUDE_ORIGIN + 1)
        latitude = (index * 461_168_601) % (2 * LATITUDE_ORIGIN + 1)
        points.append((longitude - LONGITUDE_ORIGIN, latitude - LATITUDE_ORIGIN))
    return list(dict.fromkeys(points))


def build_payload() -> dict:
    vectors = []
    for longitude_e7, latitude_e7 in e7_points():
        key, cell = ADDRESS.route_e7(longitude_e7, latitude_e7)
        quadkey8 = PARTITION.point_quadkey(
            longitude_e7 / 1e7, latitude_e7 / 1e7, REVERSE.CELL_LEVEL
        )
        vectors.append(
            {
                "longitude_e7": longitude_e7,
                "latitude_e7": latitude_e7,
                "partition_key": key,
                "partition_cell": cell,
                "quadkey8": quadkey8,
                "leaf_keys": [
                    REVERSE.leaf_key(
                        cell,
                        REVERSE.leaf_digits_e7(longitude_e7, latitude_e7, cell, level),
                    )
                    for level in range(MAX_LEAF_LEVEL + 1)
                ],
            }
        )
    return {"schema": SCHEMA, "cell_level": REVERSE.CELL_LEVEL, "vectors": vectors}


def main() -> None:
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(build_payload(), indent=1) + "\n")
    print(f"wrote {FIXTURE}")


if __name__ == "__main__":
    main()
