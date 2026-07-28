#!/usr/bin/env python3
"""Reverse serving shard leaf-key contract, v1.

The reverse index (docs/plans/2026-07-25-reverse-v2-design.md) serves one shard
per populated level-8 cell and keys records inside a shard by a leaf key: the
4-hex `partition_cell` plus L base-4 sub-digits derived by subdividing that
cell's E7 bbox. This module pins that contract -- the grid constants, the digit
convention, the depth rule -- ahead of the encoder that will write it.

Everything here is MIRRORED, not imported, per the convention stated in
`scripts/address_construction_v1.py`: the authoritative cell is `route()` in
`crates/geocoder-construction/src/bin/places_transform_v1.rs` (`(y<<8)|x`
partition key, `{y:02x}{x:02x}` cell, y increasing northward), and the digit
convention is `point_quadkey` in `crates/geocoder-worker/src/places_pages.rs`
(`(y_bit << 1) | x_bit`). `tests/test_reverse_cell_identifier_vectors.py`
cross-checks every mirror -- Python, DuckDB, and the real Rust binary -- against
one committed vector file so none of them can drift silently.

Sub-digits are computed relative to the AUTHORITATIVE cell, by clamping the
record's E7 position into that cell's E7 bbox before subdividing. Not cosmetic:
`partition_cell` comes from `route()` on f64 coordinates while leaf keying uses
E7, and the two can disagree for a point within ~5 mm of a cell boundary.
Clamping makes a leaf key that escapes its own cell unrepresentable.
"""

from __future__ import annotations

from dataclasses import dataclass


CELL_GRID = 256
CELL_LEVEL = 8
LONGITUDE_E7_ORIGIN = 1_800_000_000
LATITUDE_E7_ORIGIN = 900_000_000
# A level-8 cell is a whole number of E7 units on both axes, so leaf digits are
# EXACT integer arithmetic with no float in sight.
LONGITUDE_E7_PER_CELL = 2 * LONGITUDE_E7_ORIGIN // CELL_GRID  # 14,062,500
LATITUDE_E7_PER_CELL = 2 * LATITUDE_E7_ORIGIN // CELL_GRID  # 7,031,250

# Advisory density target per leaf. The family latitude ceiling deliberately
# overrides it in the densest cells: exceeding the ceiling would break the
# `max_radius <= min leaf dimension` invariant, which is worth more than a
# uniform leaf size.
LEAF_TARGET_RECORDS = 2048


@dataclass(frozen=True)
class ReverseFamily:
    """Per-family depth ceiling and the query radius the shard must honour."""

    l_lat: int
    max_radius_m: int


FAMILIES = {
    "poi": ReverseFamily(l_lat=5, max_radius_m=2000),
    "address": ReverseFamily(l_lat=7, max_radius_m=500),
}

MAX_SUB_CELL_LEVEL = max(family.l_lat for family in FAMILIES.values())

# Longitude depth ceiling per cell row, indexed by `cell_row_index` (0 at the
# equator-adjacent rows, 127 at the polar rows). Pinned literals generated ONCE
# from the design formula
#
#   L_lon = floor(log2(1.40625 * 111195 * cos(radians(phi_edge)) / max_radius_m))
#
# with `phi_edge` the POLEWARD edge latitude of the row, `(row + 1) * 180 / 256`
# degrees -- the edge with the smaller cosine, so the whole row satisfies the
# invariant. Values are floored at -1: after the final clamp to `[0, l_lat]` in
# `sub_cell_level` every value <= -1 behaves identically (at the polar row the
# raw formula diverges to -infinity), and -1 keeps "even a whole cell is
# narrower than the radius" visible in the table. Reproduced element-wise from
# the formula by `tests/test_reverse_cell_identifier_vectors.py`, which also
# pins the design's degree thresholds (poi: L=5 up to 65.84, 4 to 78.19, 3 to
# 84.13, 2 to 87.07, 1 to 88.53, 0 to 89.27).
L_LON_BY_ROW = {
    "poi": (6,) * 49 + (5,) * 44 + (4,) * 18 + (3,) * 8 + (2,) * 4 + (1,) * 2
    + (0,) * 1 + (-1,) * 2,
    "address": (8,) * 49 + (7,) * 44 + (6,) * 18 + (5,) * 8 + (4,) * 4 + (3,) * 2
    + (2,) * 1 + (1,) * 1 + (-1,) * 1,
}
assert all(len(table) == 128 for table in L_LON_BY_ROW.values())


def cell_yx(cell: str) -> tuple[int, int]:
    """(y, x) for a `{y:02x}{x:02x}` partition cell, matching route().

    Lowercase hex only, exactly as `route()` and `route_e7` emit it.
    """
    if len(cell) != 4 or any(char not in "0123456789abcdef" for char in cell):
        raise ValueError(f"reverse partition cell is malformed: {cell!r}")
    return int(cell[:2], 16), int(cell[2:], 16)


def cell_row_index(y: int) -> int:
    """Symmetric row index, 0 (equator-adjacent) to 127 (polar), from cell y.

    `route()` maps latitude so y increases northward; y=127 and y=128 are the
    two equator-adjacent rows and share the poleward-edge latitude 180/256
    degrees, so the index pairs rows by |latitude| of their poleward edge.
    """
    if not 0 <= y < CELL_GRID:
        raise ValueError(f"reverse cell row is out of range: {y!r}")
    return y - 128 if y >= 128 else 127 - y


def leaf_digits_e7(longitude_e7: int, latitude_e7: int, cell: str, level: int) -> str:
    """L base-4 sub-digits of an E7 point within the authoritative cell.

    The E7 offset is clamped into the cell's E7 bbox first, so a record whose
    f64-routed cell disagrees with its E7 position by one cell (the ~5 mm seam)
    still keys inside the shard that owns it. Digits follow the point_quadkey
    convention `(y_bit << 1) | x_bit`, most significant first.
    """
    _validate_level(level)
    y, x = cell_yx(cell)
    dx = min(
        LONGITUDE_E7_PER_CELL - 1,
        max(0, longitude_e7 + LONGITUDE_E7_ORIGIN - x * LONGITUDE_E7_PER_CELL),
    )
    dy = min(
        LATITUDE_E7_PER_CELL - 1,
        max(0, latitude_e7 + LATITUDE_E7_ORIGIN - y * LATITUDE_E7_PER_CELL),
    )
    sub_x = (dx << level) // LONGITUDE_E7_PER_CELL
    sub_y = (dy << level) // LATITUDE_E7_PER_CELL
    return "".join(
        str((((sub_y >> bit) & 1) << 1) | ((sub_x >> bit) & 1))
        for bit in range(level - 1, -1, -1)
    )


def leaf_key(cell: str, digits: str) -> str:
    """4 hex chars + L base-4 chars: the leaf key extends its cell key."""
    cell_yx(cell)
    if len(digits) > MAX_SUB_CELL_LEVEL or any(
        char not in "0123" for char in digits
    ):
        raise ValueError(f"reverse leaf digits are malformed: {digits!r}")
    return cell + digits


def sub_cell_level(records: int, cell: str, family: str) -> int:
    """Shard depth from the three ceilings (design section 2): min of density,
    family latitude ceiling, and the per-row longitude ceiling, clamped to
    `[0, l_lat]`. Derived from a record count and a cell identifier only, so the
    encoder, the verifier and the worker all reproduce it independently.
    """
    if family not in FAMILIES:
        raise ValueError(f"reverse family is unknown: {family!r}")
    if records < 0:
        raise ValueError(f"reverse record count is negative: {records!r}")
    spec = FAMILIES[family]
    l_records = 0
    while records > LEAF_TARGET_RECORDS << (2 * l_records):
        l_records += 1
    y, _ = cell_yx(cell)
    l_lon = L_LON_BY_ROW[family][cell_row_index(y)]
    return min(max(min(l_records, spec.l_lat, l_lon), 0), spec.l_lat)


def leaf_sql(
    level: int,
    *,
    longitude_e7: str = "longitude_e7",
    latitude_e7: str = "latitude_e7",
    cell: str = "partition_cell",
) -> str:
    """DuckDB expression for the leaf digits, mirroring leaf_digits_e7 exactly.

    Same pattern as `route_e7_sql`: ``greatest`` then ``least`` reproduce the
    clamp and keep the dividend non-negative so DuckDB's truncating ``//`` is a
    floor. The cell column must be the lowercase 4-hex form.
    """
    _validate_level(level)
    if level == 0:
        return "''"

    def hex_digit(position: int) -> str:
        return f"(strpos('0123456789abcdef', substr({cell}, {position}, 1)) - 1)"

    x = f"({hex_digit(3)} * 16 + {hex_digit(4)})"
    y = f"({hex_digit(1)} * 16 + {hex_digit(2)})"
    dx = (
        f"least({LONGITUDE_E7_PER_CELL - 1}, greatest(0, "
        f"({longitude_e7})::BIGINT + {LONGITUDE_E7_ORIGIN} - "
        f"{x} * {LONGITUDE_E7_PER_CELL}))"
    )
    dy = (
        f"least({LATITUDE_E7_PER_CELL - 1}, greatest(0, "
        f"({latitude_e7})::BIGINT + {LATITUDE_E7_ORIGIN} - "
        f"{y} * {LATITUDE_E7_PER_CELL}))"
    )
    sub_x = f"(({dx}) * {1 << level} // {LONGITUDE_E7_PER_CELL})"
    sub_y = f"(({dy}) * {1 << level} // {LATITUDE_E7_PER_CELL})"
    digits = [
        f"chr((48 + ({sub_y} // {1 << bit} % 2) * 2 + "
        f"({sub_x} // {1 << bit} % 2))::INTEGER)"
        for bit in range(level - 1, -1, -1)
    ]
    return "(" + " || ".join(digits) + ")"


def _validate_level(level: int) -> None:
    if not 0 <= level <= MAX_SUB_CELL_LEVEL:
        raise ValueError(f"reverse sub-cell level is out of range: {level!r}")
