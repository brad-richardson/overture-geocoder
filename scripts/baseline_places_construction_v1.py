#!/usr/bin/env python3
"""Frozen streaming Python semantic baseline for Places construction-v1."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import time
import unicodedata
import uuid
from collections import Counter
from pathlib import Path
from typing import Any


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
    folded = "".join(
        character
        for character in unicodedata.normalize("NFKD", value.strip().lower())
        if not unicodedata.category(character).startswith("M")
    )
    words: list[str] = []
    current: list[str] = []
    for character in folded:
        if character.isalnum() or character == "_":
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
    output.extend((field_mask, rank))
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
            primary = text(columns["primary_name"], index).strip()
            if not primary:
                rejected["missing_primary_name"] += 1
                continue
            raw_id = text(columns["id"], index).strip()
            try:
                identifier = uuid.UUID(raw_id)
            except (ValueError, AttributeError):
                rejected["invalid_uuid"] += 1
                continue
            if str(identifier) != raw_id.lower():
                rejected["invalid_uuid"] += 1
                continue
            if (
                text(columns["operating_status"], index).strip().lower()
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
                value and value.strip() != primary for value in common
            )
            values = [
                (primary, 1),
                *((value or "", 1) for value in common),
                (text(columns["brand_name"], index), 2),
                (text(columns["category"], index), 4),
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
