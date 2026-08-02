#!/usr/bin/env python3
"""Frozen streaming Python semantic baseline for Places construction-v1.

The tokenizer emulates Rust `places-transform-v1` string semantics exactly
(tokenizer_version `nfkd-lower-stripmark-cjk-bigram-v4`). Rust is the
authoritative side; CPython's own `str.lower`/`str.strip`/`str.isalnum` differ
from Rust on three well-defined Unicode behaviours (Greek `Final_Sigma`, the C0
separators U+001C..U+001F, and the `Other_Alphabetic` symbol word class) and on
Unicode version. Instead of trusting CPython's tables, the baseline drives the
lowercase mapping, whitespace set and word-character set from
`places_unicode_tables_v1.json`, which is exported from the very same Rust
`std::char` tables the transform uses (regenerate with
`places-unicode-tables-v1`). This removes CPython's independent Unicode opinion
on those properties, so the two implementations cannot drift apart by version.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import struct
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

# The tokenizer contract pins one Unicode version for both implementations.
# `unicodedata2` ships the tables as a wheel, so the baseline's NFKD and
# combining-mark answers come from the pinned version rather than from whichever
# version the running interpreter happens to embed: CPython 3.11 carries Unicode
# 14.0 and 3.12 carries 15.0, while Rust `std` and `unicode-normalization` both
# carry 17.0. Combining marks assigned after 14.0 (Kawi, Nag Mundari, Garay) are
# `Cn` to the runner's stdlib tables and `Mn` to Rust, so the stdlib baseline
# would keep marks that Rust strips and diverge on both digest lanes.
import unicodedata2 as unicodedata

TOKENIZER_UNICODE_VERSION = "17.0.0"
if unicodedata.unidata_version != TOKENIZER_UNICODE_VERSION:
    raise RuntimeError(
        "the tokenizer contract pins Unicode "
        f"{TOKENIZER_UNICODE_VERSION}, but unicodedata2 provides "
        f"{unicodedata.unidata_version}"
    )


DOMAIN_A = b"overture-places-construction-v1\0"
DOMAIN_B = b"overture-places-construction-v1\x01"
UINT256 = 1 << 256
MAX_RECORD_BYTES = 1_048_576
REJECTIONS = (
    "missing_primary_name",
    "invalid_uuid",
    "permanently_closed",
    "invalid_geometry",
    "invalid_confidence",
    "invalid_source_locator",
    "record_too_large",
    "invalid_record",
)


# ---------------------------------------------------------------------------
# Rust-exported Unicode property tables (see module docstring). Loading fails
# closed: without the tables the baseline must not silently fall back to
# CPython semantics that the contract does not pin.
# ---------------------------------------------------------------------------
_TABLES_PATH = Path(__file__).with_name("places_unicode_tables_v1.json")
_TABLES = json.loads(_TABLES_PATH.read_text())
_WHITESPACE = frozenset(chr(codepoint) for codepoint in _TABLES["whitespace"])
_LOWERCASE_MAP = {
    int(key): "".join(chr(codepoint) for codepoint in value)
    for key, value in _TABLES["lowercase_map"].items()
}
_WORD_RANGES: list[list[int]] = _TABLES["word_char_ranges"]
_WORD_RANGE_STARTS = [start for start, _ in _WORD_RANGES]
_SIGMA_SOURCE = chr(_TABLES["sigma_fold"][0])
_SIGMA_TARGET = chr(_TABLES["sigma_fold"][1])


def rust_trim(value: str) -> str:
    """Trim exactly Unicode `White_Space` (Rust `str::trim`), unlike
    CPython `str.strip()` which also strips the C0 separators U+001C..U+001F."""
    start, end = 0, len(value)
    while start < end and value[start] in _WHITESPACE:
        start += 1
    while end > start and value[end - 1] in _WHITESPACE:
        end -= 1
    return value[start:end]


def rust_lower(character: str) -> str:
    """Context-free per-char lowercase from Rust `char::to_lowercase`; unlike
    CPython `str.lower`, never applies the Greek `Final_Sigma` context rule."""
    return _LOWERCASE_MAP.get(ord(character), character)


def is_word_char(character: str) -> bool:
    """Rust `char::is_alphanumeric()` from the exported ranges; includes the
    `Other_Alphabetic` symbols CPython `str.isalnum()` excludes."""
    codepoint = ord(character)
    index = bisect.bisect_right(_WORD_RANGE_STARTS, codepoint) - 1
    return (
        index >= 0
        and _WORD_RANGES[index][0] <= codepoint <= _WORD_RANGES[index][1]
    )


def is_cjk(character: str) -> bool:
    value = ord(character)
    return (
        0x3400 <= value <= 0x4DBF
        or 0x4E00 <= value <= 0x9FFF
        or 0x3040 <= value <= 0x30FF
        or 0x31F0 <= value <= 0x31FF
        or 0xAC00 <= value <= 0xD7AF
    )


def tokens(value: str) -> set[str]:
    # Mirror Rust `normalized_words`: trim White_Space, NFKD-decompose, then
    # lowercase per-char (after NFKD, so styled capitals fold), fold final
    # sigma, and drop combining marks.
    folded_chars: list[str] = []
    for character in unicodedata.normalize("NFKD", rust_trim(value)):
        for lowered in rust_lower(character):
            if lowered == _SIGMA_SOURCE:
                lowered = _SIGMA_TARGET
            if not unicodedata.category(lowered).startswith("M"):
                folded_chars.append(lowered)
    folded = "".join(folded_chars)
    words: list[str] = []
    current: list[str] = []
    for character in folded:
        if is_word_char(character) or character == "_":
            current.append(character)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    result = set(words)
    for word in words:
        characters = list(word)
        start = 0
        while start < len(characters):
            if not is_cjk(characters[start]):
                start += 1
                continue
            end = start + 1
            while end < len(characters) and is_cjk(characters[end]):
                end += 1
            if end - start == 1:
                result.add(characters[start])
            else:
                result.update(
                    "".join(characters[offset : offset + 2])
                    for offset in range(start, end - 1)
                )
            start = end
    return result


def point(value: bytes | None) -> tuple[float, float] | None:
    if value is None or len(value) != 21 or value[0] not in (0, 1):
        return None
    prefix = "<" if value[0] == 1 else ">"
    kind, longitude, latitude = struct.unpack_from(prefix + "Idd", value, 1)
    if (
        kind != 1
        or not math.isfinite(longitude)
        or not math.isfinite(latitude)
        or not -180 <= longitude <= 180
        or not -90 <= latitude <= 90
    ):
        return None
    return longitude, latitude


def route(longitude: float, latitude: float) -> tuple[int, str, str]:
    x = min(255, max(0, math.floor((longitude + 180) / 360 * 256)))
    y = min(255, max(0, math.floor((latitude + 90) / 180 * 256)))
    key = (y << 8) | x
    cell = f"{y:02x}{x:02x}"
    return key, cell[:2], cell


def text(array: Any, index: int) -> str:
    value = array[index].as_py()
    return value if isinstance(value, str) else ""


def payload(
    *,
    execution_group: str,
    partition_cell: str,
    partition_key: int,
    token: str,
    field_mask: int,
    rank: int,
    prominence: int,
    identifier: uuid.UUID,
    longitude: float,
    latitude: float,
    display: list[str],
    object_index: int,
    row_group: int,
    row_index: int,
) -> bytes:
    output = bytearray()
    for value in (execution_group, partition_cell, token):
        encoded = value.encode()
        output.extend(len(encoded).to_bytes(4, "big"))
        output.extend(encoded)
    output.extend(partition_key.to_bytes(4, "big"))
    output.extend((field_mask, rank, prominence))
    output.extend(identifier.bytes)
    output.extend(
        struct.pack(">Q", struct.unpack(">Q", struct.pack(">d", longitude))[0])
    )
    output.extend(
        struct.pack(">Q", struct.unpack(">Q", struct.pack(">d", latitude))[0])
    )
    for value in display:
        encoded = value.encode()
        output.extend(len(encoded).to_bytes(4, "big"))
        output.extend(encoded)
    output.extend(object_index.to_bytes(4, "big"))
    output.extend(row_group.to_bytes(4, "big"))
    output.extend(row_index.to_bytes(8, "big"))
    return bytes(output)


def run(input_path: Path, source_limits_path: Path) -> dict[str, Any]:
    import pyarrow.parquet as pq

    started = time.monotonic()
    source_limits = json.loads(source_limits_path.read_text())["objects"]
    rejected = Counter({reason: 0 for reason in REJECTIONS})
    input_features = admitted = emitted = multilingual = cjk = 0
    sum_a = sum_b = 0
    parquet = pq.ParquetFile(input_path)
    for batch in parquet.iter_batches(batch_size=65_536, use_threads=False):
        columns = {name: batch[name] for name in batch.schema.names}
        for index in range(batch.num_rows):
            input_features += 1
            primary = rust_trim(text(columns["primary_name"], index))
            if not primary:
                rejected["missing_primary_name"] += 1
                continue
            raw_id = rust_trim(text(columns["id"], index))
            try:
                identifier = uuid.UUID(raw_id)
            except (ValueError, AttributeError):
                rejected["invalid_uuid"] += 1
                continue
            if str(identifier) != raw_id.lower():
                rejected["invalid_uuid"] += 1
                continue
            if (
                rust_trim(text(columns["operating_status"], index)).lower()
                == "permanently_closed"
            ):
                rejected["permanently_closed"] += 1
                continue
            coordinates = point(columns["geometry"][index].as_py())
            if coordinates is None:
                rejected["invalid_geometry"] += 1
                continue
            confidence = columns["confidence"][index].as_py()
            if (
                not isinstance(confidence, float)
                or not math.isfinite(confidence)
                or not 0 <= confidence <= 1
            ):
                rejected["invalid_confidence"] += 1
                continue
            object_index = columns["source_object_index"][index].as_py()
            row_group = columns["source_row_group"][index].as_py()
            row_index = columns["source_row_index"][index].as_py()
            if (
                not all(
                    isinstance(value, int) and value >= 0
                    for value in (object_index, row_group, row_index)
                )
                or object_index >= len(source_limits)
                or row_group >= source_limits[object_index]["row_groups"]
                or row_index >= source_limits[object_index]["records"]
            ):
                rejected["invalid_source_locator"] += 1
                continue
            fields: dict[str, int] = {}
            common = columns["common_names"][index].as_py() or []
            has_multilingual = any(
                value and rust_trim(value) != primary for value in common
            )
            category_terms = columns.get("category_terms")
            searchable_category = (
                text(category_terms, index)
                if category_terms is not None
                else text(columns["category"], index)
            )
            values = [
                (primary, 1),
                *((value or "", 1) for value in common),
                (text(columns["brand_name"], index), 2),
                (searchable_category, 4),
                (text(columns["locality"], index), 8),
                (text(columns["region"], index), 8),
                (text(columns["country"], index), 8),
            ]
            for value, mask in values:
                for token in tokens(value):
                    fields[token] = fields.get(token, 0) | mask
            if not fields:
                rejected["invalid_record"] += 1
                continue
            longitude, latitude = coordinates
            partition_key, execution_group, partition_cell = route(longitude, latitude)
            rank = math.floor(confidence * 255 + 0.5)
            # Mirrors the Rust transform's optional read: the projection carries
            # `prominence_rank` when it was produced by a projector that knows
            # about it, and zero otherwise.
            prominence_column = columns.get("prominence_rank")
            prominence = 0
            if prominence_column is not None:
                value = prominence_column[index].as_py()
                if isinstance(value, int) and 0 <= value <= 255:
                    prominence = value
            display = [
                primary,
                text(columns["brand_name"], index),
                text(columns["category"], index),
                text(columns["locality"], index),
                text(columns["region"], index),
                text(columns["country"], index),
            ]
            encoded = [
                payload(
                    execution_group=execution_group,
                    partition_cell=partition_cell,
                    partition_key=partition_key,
                    token=token,
                    field_mask=mask,
                    rank=rank,
                    prominence=prominence,
                    identifier=identifier,
                    longitude=longitude,
                    latitude=latitude,
                    display=display,
                    object_index=object_index,
                    row_group=row_group,
                    row_index=row_index,
                )
                for token, mask in sorted(fields.items())
            ]
            if any(len(value) > MAX_RECORD_BYTES for value in encoded):
                rejected["record_too_large"] += 1
                continue
            admitted += 1
            multilingual += int(has_multilingual)
            cjk += int(
                any(any(is_cjk(character) for character in token) for token in fields)
            )
            emitted += len(encoded)
            for value in encoded:
                sum_a = (
                    sum_a
                    + int.from_bytes(hashlib.sha256(DOMAIN_A + value).digest(), "big")
                ) % UINT256
                sum_b = (
                    sum_b
                    + int.from_bytes(hashlib.sha256(DOMAIN_B + value).digest(), "big")
                ) % UINT256
    return {
        "schema": "overture-places-python-semantic-baseline-v1",
        "input_features": input_features,
        "admitted_features": admitted,
        "multilingual_features": multilingual,
        "cjk_features": cjk,
        "emitted_term_rows": emitted,
        "rejected_features": sum(rejected.values()),
        "rejections_by_precedence": dict(rejected),
        "semantic_sum_a": f"{sum_a:064x}",
        "semantic_sum_b": f"{sum_b:064x}",
        "elapsed_seconds": time.monotonic() - started,
        "maximum_batch_rows": 65_536,
        "full_table_read_all": False,
        "python_feature_rows_materialized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--source-limits", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.input, args.source_limits)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
