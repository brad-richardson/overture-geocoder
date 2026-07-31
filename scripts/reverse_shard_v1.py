#!/usr/bin/env python3
"""Reverse serving shard leaf-key contract, v1.

The reverse index (docs/plans/2026-07-25-reverse-v2-design.md) serves one shard
per populated level-8 cell and keys records inside a shard by a leaf key: the
4-hex `partition_cell` plus L base-4 sub-digits derived by subdividing that
cell's E7 bbox. This module pins that contract -- the grid constants, the digit
convention, the depth rule -- and carries the Python oracle decoder
(`ReverseShard`) for the `.plrx` shards `reverse-encode-v1` writes.

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

import hashlib
import struct
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


# --------------------------------------------------------------------------- #
# .plrx oracle decoder
# --------------------------------------------------------------------------- #
# Python mirror of the `.plrx` reverse serving shard written by
# `reverse-encode-v1` and independently re-decoded by `reverse-verify-v1`
# (crates/geocoder-construction/src/bin/). Mirrored, never imported, following
# `experiment_places_compact_shard.CompactShard`: a third implementation of the
# wire format so the Rust encoder, the Rust verifier and this oracle are
# mutually fuzz-comparable.

SERVING_MAGIC = b"PLRX0001"
SERVING_HEADER_BYTES = 32
SERVING_INDEX_ENTRY_BYTES = 40
# feature id (16) + longitude/latitude E7 (8) + source object/row group/row (16).
# Places records carry an extra confidence_rank byte after the coordinates.
ADDRESS_RECORD_PREFIX_BYTES = 40
SERVING_INDEX_DOMAIN = b"overture-reverse-index-v1\x00"
SERVING_DIGEST_DOMAIN_A = b"overture-reverse-shard-v1\x00"
SERVING_DIGEST_DOMAIN_B = b"overture-reverse-shard-v1\x01"
ADDRESS_DICTIONARY_MAGIC = b"ARDX0002"


def address_code_width(count: int) -> int:
    """Per-field dictionary code width, mirroring the construction encoder.

    ARDX0001 fixed every code at u16, which the planet densest Address cell
    overflows on ``street`` and ``postcode``. Each field now declares the
    narrowest width its own cardinality admits.
    """
    if count <= 0x100:
        return 1
    if count <= 0x1_0000:
        return 2
    return 4


def _code(data: bytes, position: int, width: int) -> tuple[int, int]:
    return (
        int.from_bytes(data[position : position + width], "little"),
        position + width,
    )
ADDRESS_DICTIONARY_FLAG = 1
ADDRESS_DICTIONARY_FIELDS = 7
MAX_ADDRESS_DICTIONARY_BYTES = 8 * 1024 * 1024
# Header family byte -> encoder --family value. The serving families map onto
# the depth-ceiling families above as places -> "poi", addresses -> "address".
SERVING_FAMILY_CODES = {0: "places", 1: "addresses"}
DEPTH_FAMILY_BY_SERVING = {"places": "poi", "addresses": "address"}

_PLACES_TEXT_FIELDS = (
    "primary_name",
    "brand_name",
    "category",
    "locality",
    "region",
    "country",
)
_ADDRESS_TEXT_FIELDS = (
    "display_country",
    "postal_city",
    "postcode",
    "street",
    "number",
    "unit",
)


def serving_index_hash(key: bytes) -> int:
    """u64 index hash: first 8 big-endian bytes of the domain-separated SHA-256."""
    digest = hashlib.sha256(SERVING_INDEX_DOMAIN + key).digest()
    return int.from_bytes(digest[:8], "big")


@dataclass(frozen=True)
class LeafRange:
    """One index entry: the payload extent of one populated leaf."""

    key: str
    payload_offset: int
    payload_bytes: int
    records: int


def _text(data: bytes, position: int) -> tuple[str, int]:
    (length,) = struct.unpack_from("<H", data, position)
    position += 2
    if position + length > len(data):
        raise ValueError("reverse shard text is truncated")
    return data[position : position + length].decode("utf-8"), position + length


class ReverseShard:
    """Full `.plrx` decoder over in-memory bytes.

    The constructor validates the header and the index frame (hashes, key
    shapes, blob extent); `leaf_ranges` exposes the payload extents in payload
    order; `decode_leaf` decodes one leaf's records and re-derives every leaf
    key from the record's own E7 coordinates, so a payload row filed under the
    wrong leaf cannot decode silently.
    """

    def __init__(self, data: bytes):
        if len(data) < SERVING_HEADER_BYTES + 4 or data[:8] != SERVING_MAGIC:
            raise ValueError("not a reverse serving shard")
        (self.records,) = struct.unpack_from("<Q", data, 8)
        (index_offset,) = struct.unpack_from("<Q", data, 16)
        (index_count,) = struct.unpack_from("<I", data, 24)
        family_code, self.cell_level, self.sub_cell_level, flags = data[28:32]
        if family_code not in SERVING_FAMILY_CODES:
            raise ValueError(f"reverse shard family is unknown: {family_code}")
        self.family = SERVING_FAMILY_CODES[family_code]
        expected_flags = (
            ADDRESS_DICTIONARY_FLAG if self.family == "addresses" else 0
        )
        if flags != expected_flags or self.cell_level != CELL_LEVEL:
            raise ValueError("reverse shard header is malformed")
        self.index_offset = index_offset
        if not SERVING_HEADER_BYTES <= index_offset <= len(data) - 4:
            raise ValueError("reverse shard index offset is out of range")
        dictionary_position = SERVING_HEADER_BYTES
        self._address_dictionary: list[list[str]] | None = None
        self._address_widths: list[int] | None = None
        if self.family == "addresses":
            if (
                data[dictionary_position : dictionary_position + 8]
                != ADDRESS_DICTIONARY_MAGIC
                or data[dictionary_position + 8 : dictionary_position + 10]
                != bytes((ADDRESS_DICTIONARY_FIELDS, 0))
                or struct.unpack_from("<H", data, dictionary_position + 10)[0] != 0
            ):
                raise ValueError("reverse address dictionary header is malformed")
            dictionary_position += 12
            fields = []
            widths = []
            for _ in range(ADDRESS_DICTIONARY_FIELDS):
                (count,) = struct.unpack_from("<I", data, dictionary_position)
                dictionary_position += 4
                width = data[dictionary_position]
                dictionary_position += 1
                # Derived from the count, so any other value would admit two
                # encodings of one dictionary and break the shard digest.
                if width != address_code_width(count):
                    raise ValueError(
                        "reverse address dictionary code width is not canonical"
                    )
                widths.append(width)
                values = []
                for _ in range(count):
                    value, dictionary_position = _text(data, dictionary_position)
                    if values and value <= values[-1]:
                        raise ValueError(
                            "reverse address dictionary is not unique and sorted"
                        )
                    values.append(value)
                fields.append(values)
            self._address_dictionary = fields
            self._address_widths: list[int] | None = widths
        self.dictionary_bytes = dictionary_position - SERVING_HEADER_BYTES
        if self.dictionary_bytes > MAX_ADDRESS_DICTIONARY_BYTES:
            raise ValueError("reverse address dictionary exceeds serving read cap")
        self.payload_offset = dictionary_position
        if self.payload_offset > self.index_offset:
            raise ValueError("reverse address dictionary overlaps the index")
        (stored_count,) = struct.unpack_from("<I", data, index_offset)
        if stored_count != index_count:
            raise ValueError("reverse shard index counts disagree")
        fixed_start = index_offset + 4
        key_start = fixed_start + SERVING_INDEX_ENTRY_BYTES * index_count
        if key_start > len(data):
            raise ValueError("reverse shard index is truncated")
        self._entries: list[LeafRange] = []
        self._by_key: dict[str, LeafRange] = {}
        previous: tuple[int, bytes] | None = None
        expected_key_offset = 0
        for item in range(index_count):
            hash_, key_offset, key_length, records, offset, length = struct.unpack_from(
                "<QQIIQQ", data, fixed_start + SERVING_INDEX_ENTRY_BYTES * item
            )
            raw = data[key_start + key_offset : key_start + key_offset + key_length]
            if len(raw) != key_length or key_offset != expected_key_offset:
                raise ValueError("reverse shard index key blob is malformed")
            expected_key_offset += key_length
            key = raw.decode("ascii")
            digits = key[4:]
            if len(digits) != self.sub_cell_level:
                raise ValueError("reverse shard leaf key depth differs from header")
            leaf_key(key[:4], digits)
            if hash_ != serving_index_hash(raw):
                raise ValueError("reverse shard index hash is wrong")
            if previous is not None and previous > (hash_, raw):
                raise ValueError("reverse shard index is not sorted by (hash, key)")
            previous = (hash_, raw)
            entry = LeafRange(key, offset, length, records)
            self._entries.append(entry)
            self._by_key[key] = entry
        if len(self._by_key) != index_count:
            raise ValueError("reverse shard index repeats a leaf key")
        if key_start + expected_key_offset != len(data):
            raise ValueError("reverse shard index length differs")
        cells = {entry.key[:4] for entry in self._entries}
        if len(cells) > 1:
            raise ValueError("reverse shard mixes cells")
        self.cell = cells.pop() if cells else None
        self._data = data

    def leaf_ranges(self) -> list[LeafRange]:
        """Populated leaves in payload (row-major) order."""
        return sorted(self._entries, key=lambda entry: entry.payload_offset)

    def decode_leaf(self, key: str) -> list[dict]:
        entry = self._by_key[key]
        data = self._data
        position = entry.payload_offset
        end = entry.payload_offset + entry.payload_bytes
        if not self.payload_offset <= position <= end <= self.index_offset:
            raise ValueError("reverse shard leaf extent is out of range")
        records = []
        while position < end:
            if self.family == "places":
                (length,) = struct.unpack_from("<I", data, position)
                record_start = position + 4
                record_end = record_start + length
            else:
                record_start = position
                widths = self._address_widths
                if widths is None:
                    raise ValueError("reverse address dictionary is absent")
                # Address records are self-delimiting, but only through the
                # dictionary's per-field code widths: the display codes are no
                # longer a fixed 6 x 2 bytes.
                display_bytes = sum(widths[:6])
                levels_at = record_start + ADDRESS_RECORD_PREFIX_BYTES + display_bytes
                if levels_at + 2 > end:
                    raise ValueError("reverse address record is truncated")
                (levels,) = struct.unpack_from("<H", data, levels_at)
                record_end = levels_at + 2 + levels * widths[6]
            if record_end > end:
                raise ValueError("reverse shard record overruns its leaf")
            records.append(self._decode_record(data[record_start:record_end], key))
            position = record_end
        if len(records) != entry.records:
            raise ValueError("reverse shard leaf record count differs")
        return records

    def _decode_record(self, entry: bytes, key: str) -> dict:
        feature_id = entry[:16]
        if len(feature_id) != 16:
            raise ValueError("reverse shard record is truncated")
        longitude_e7, latitude_e7 = struct.unpack_from("<ii", entry, 16)
        record = {
            "feature_id": feature_id,
            "longitude_e7": longitude_e7,
            "latitude_e7": latitude_e7,
        }
        position = 24
        if self.family == "places":
            record["confidence_rank"] = entry[position]
            position += 1
        (
            record["source_object_index"],
            record["source_row_group"],
            record["source_row_index"],
        ) = struct.unpack_from("<IIQ", entry, position)
        position += 16
        if self.family == "places":
            for field in _PLACES_TEXT_FIELDS:
                record[field], position = _text(entry, position)
        else:
            if self._address_dictionary is None:
                raise ValueError("reverse address dictionary is absent")
            widths = self._address_widths
            if widths is None:
                raise ValueError("reverse address dictionary is absent")
            for field, values, width in zip(
                _ADDRESS_TEXT_FIELDS,
                self._address_dictionary[:6],
                widths[:6],
                strict=True,
            ):
                code, position = _code(entry, position, width)
                if code >= len(values):
                    raise ValueError(
                        "reverse address dictionary code is out of range"
                    )
                record[field] = values[code]
            (count,) = struct.unpack_from("<H", entry, position)
            position += 2
            levels = []
            for _ in range(count):
                code, position = _code(entry, position, widths[6])
                values = self._address_dictionary[6]
                if code >= len(values):
                    raise ValueError(
                        "reverse address-level dictionary code is out of range"
                    )
                levels.append(values[code])
            record["address_levels"] = levels
        if position != len(entry):
            raise ValueError("reverse shard record has trailing bytes")
        digits = leaf_digits_e7(
            longitude_e7, latitude_e7, key[:4], self.sub_cell_level
        )
        if key != leaf_key(key[:4], digits):
            raise ValueError("reverse shard record is filed under the wrong leaf")
        return record
