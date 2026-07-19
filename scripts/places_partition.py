#!/usr/bin/env python3
"""Stable spatial partition primitives for compact Overture Places shards.

The contract intentionally uses a small, dependency-free world quadtree rather
than region-local ordinal chunks. A cell identifier is therefore stable across
Overture releases until the cell exceeds the configured row cap. Once a cell
splits, callers carry the split marker forward so it never merges again.

Coordinates use a plate-carree world grid. ``y`` increases northward and each
quadkey digit is ``(y_bit << 1) | x_bit``. Cells are half-open on their eastern
and northern edges except that +180/+90 clamp into the final world cell.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass


PARTITION_SCHEME = "world-quadkey-v1"
DEFAULT_MINIMUM_LEVEL = 6
DEFAULT_MAXIMUM_LEVEL = 12
MAX_SUPPORTED_LEVEL = 15


@dataclass(frozen=True)
class PartitionCell:
    cell: str
    rows: int


def validate_levels(minimum_level: int, maximum_level: int) -> None:
    if not 1 <= minimum_level <= maximum_level <= MAX_SUPPORTED_LEVEL:
        raise ValueError(
            "partition levels must satisfy "
            f"1 <= minimum <= maximum <= {MAX_SUPPORTED_LEVEL}"
        )


def validate_quadkey(cell: str, *, minimum: int, maximum: int) -> None:
    if not minimum <= len(cell) <= maximum or any(digit not in "0123" for digit in cell):
        raise ValueError("invalid world-quadkey-v1 cell")


def _coordinate_index(value: float, *, offset: float, span: float, level: int) -> int:
    if not math.isfinite(value):
        raise ValueError("partition coordinates must be finite")
    size = 1 << level
    return min(size - 1, max(0, math.floor((value + offset) / span * size)))


def point_morton(longitude: float, latitude: float, level: int) -> int:
    validate_levels(level, level)
    if not -180.0 <= longitude <= 180.0 or not -90.0 <= latitude <= 90.0:
        raise ValueError("partition coordinates are outside the world bounds")
    x = _coordinate_index(longitude, offset=180.0, span=360.0, level=level)
    y = _coordinate_index(latitude, offset=90.0, span=180.0, level=level)
    morton = 0
    for bit in range(level - 1, -1, -1):
        morton = (morton << 2) | (((y >> bit) & 1) << 1) | ((x >> bit) & 1)
    return morton


def morton_quadkey(morton: int, level: int) -> str:
    validate_levels(level, level)
    if not 0 <= morton < 1 << (2 * level):
        raise ValueError("Morton key is outside the configured level")
    return "".join(
        str((morton >> (2 * bit)) & 0b11) for bit in range(level - 1, -1, -1)
    )


def point_quadkey(longitude: float, latitude: float, level: int) -> str:
    return morton_quadkey(point_morton(longitude, latitude, level), level)


def quadkey_bbox(cell: str) -> list[float]:
    validate_quadkey(cell, minimum=1, maximum=MAX_SUPPORTED_LEVEL)
    x = 0
    y = 0
    for digit in cell:
        value = ord(digit) - ord("0")
        x = (x << 1) | (value & 1)
        y = (y << 1) | ((value >> 1) & 1)
    size = 1 << len(cell)
    width = 360.0 / size
    height = 180.0 / size
    return [
        -180.0 + x * width,
        -90.0 + y * height,
        -180.0 + (x + 1) * width,
        -90.0 + (y + 1) * height,
    ]


def morton_sql(longitude_sql: str, latitude_sql: str, level: int) -> str:
    """Return a DuckDB expression matching :func:`point_morton` exactly."""
    validate_levels(level, level)
    size = 1 << level
    x = (
        "CAST(LEAST("
        f"{size - 1}, GREATEST(0, FLOOR((({longitude_sql}) + 180.0) / 360.0 * {size})))"
        " AS UBIGINT)"
    )
    y = (
        "CAST(LEAST("
        f"{size - 1}, GREATEST(0, FLOOR((({latitude_sql}) + 90.0) / 180.0 * {size})))"
        " AS UBIGINT)"
    )
    terms: list[str] = []
    for bit in range(level):
        terms.append(f"((({x} >> {bit}) & 1) << {2 * bit})")
        terms.append(f"((({y} >> {bit}) & 1) << {2 * bit + 1})")
    return "(" + " | ".join(terms) + ")"


def validate_split_cells(
    cells: Iterable[str], *, minimum_level: int, maximum_level: int
) -> set[str]:
    validate_levels(minimum_level, maximum_level)
    result = set(cells)
    for cell in result:
        validate_quadkey(
            cell, minimum=minimum_level, maximum=maximum_level - 1
        )
        if len(cell) > minimum_level and cell[:-1] not in result:
            raise ValueError("split cells must include every split ancestor")
    return result


def _plan_base_cell(
    prefix: str,
    full_counts: list[tuple[str, int]],
    *,
    maximum_level: int,
    row_cap: int,
    sticky_splits: set[str],
    split_cells: set[str],
) -> Iterator[PartitionCell]:
    total = sum(count for _, count in full_counts)
    must_split = prefix in sticky_splits or total > row_cap
    if not must_split:
        if total:
            yield PartitionCell(prefix, total)
        return
    if len(prefix) >= maximum_level:
        raise ValueError(
            f"partition cell {prefix} has {total} rows above cap {row_cap} "
            "at the maximum level"
        )
    split_cells.add(prefix)
    for digit in "0123":
        child = prefix + digit
        child_counts = [item for item in full_counts if item[0].startswith(child)]
        if child_counts:
            yield from _plan_base_cell(
                child,
                child_counts,
                maximum_level=maximum_level,
                row_cap=row_cap,
                sticky_splits=sticky_splits,
                split_cells=split_cells,
            )


def plan_partition_cells(
    full_counts: Iterable[tuple[str, int]],
    *,
    minimum_level: int,
    maximum_level: int,
    row_cap: int,
    sticky_splits: Iterable[str] = (),
) -> tuple[list[PartitionCell], list[str]]:
    """Collapse sorted maximum-level counts into stable adaptive leaf cells.

    Only one minimum-level base cell is retained at once. With the default
    levels that bounds planning state to at most 4,096 occupied maximum-level
    keys even when the complete global input contains tens of millions of rows.
    """
    validate_levels(minimum_level, maximum_level)
    if row_cap < 1:
        raise ValueError("row_cap must be a positive integer")
    sticky = validate_split_cells(
        sticky_splits,
        minimum_level=minimum_level,
        maximum_level=maximum_level,
    )
    split_cells = set(sticky)
    leaves: list[PartitionCell] = []
    previous_full: str | None = None
    current_base: str | None = None
    base_counts: list[tuple[str, int]] = []

    def flush() -> None:
        if current_base is not None:
            leaves.extend(
                _plan_base_cell(
                    current_base,
                    base_counts,
                    maximum_level=maximum_level,
                    row_cap=row_cap,
                    sticky_splits=sticky,
                    split_cells=split_cells,
                )
            )

    for full_cell, count in full_counts:
        validate_quadkey(full_cell, minimum=maximum_level, maximum=maximum_level)
        if count < 1:
            raise ValueError("maximum-level cell counts must be positive")
        if previous_full is not None and full_cell <= previous_full:
            raise ValueError("maximum-level cell counts must be strictly ordered")
        base = full_cell[:minimum_level]
        if current_base is not None and base != current_base:
            flush()
            base_counts = []
        current_base = base
        base_counts.append((full_cell, count))
        previous_full = full_cell
    flush()
    if not leaves:
        raise ValueError("input contains no named Places")
    return leaves, sorted(split_cells)
