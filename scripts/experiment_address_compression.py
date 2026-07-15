#!/usr/bin/env python3
"""Compare range-readable address encodings against one reduce artifact.

Each format keeps the exact normalized candidate key, UUID, quantized
coordinates, and Overture source locator.  The ``useful`` formats additionally
preserve every display field and raw address-level value from the reducer.
Pages are independent so a Worker never needs to inflate an entire shard.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import hashlib
import json
import math
import statistics
import struct
import time
import uuid
import zlib
from pathlib import Path
from typing import Any

from experiment_address_reduce import (
    AddressReduceArtifact,
    canonical_json,
    decode_text,
    decode_uvarint,
    encode_text,
    encode_uvarint,
    sha256_file,
)


DATA_MAGIC = b"OACMP01\0"
INDEX_MAGIC = b"OACIX01\0"
DISPLAY_FIELDS = ("country", "postal_city", "postcode", "street", "number", "unit")
VARIANTS = {
    "bare": {"useful": False, "gzip": False},
    "bare_gzip": {"useful": False, "gzip": True},
    "useful": {"useful": True, "gzip": False},
    "useful_gzip": {"useful": True, "gzip": True},
}
MAX_INDEX_KEY_BYTES = 64 * 1024


def common_prefix(left: bytes, right: bytes) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def encode_front_key(key: tuple[str, ...], previous: tuple[bytes, ...]) -> tuple[bytes, tuple[bytes, ...]]:
    pieces = []
    encoded = tuple(value.encode("utf-8") for value in key)
    for value, old in zip(encoded, previous):
        prefix = common_prefix(old, value)
        suffix = value[prefix:]
        pieces.extend((encode_uvarint(prefix), encode_uvarint(len(suffix)), suffix))
    return b"".join(pieces), encoded


def decode_front_key(payload: bytes, position: int, previous: tuple[bytes, ...]) -> tuple[tuple[str, ...], tuple[bytes, ...], int]:
    values = []
    encoded = []
    for old in previous:
        prefix, position = decode_uvarint(payload, position)
        length, position = decode_uvarint(payload, position)
        end = position + length
        if prefix > len(old) or end > len(payload):
            raise ValueError("invalid front-coded key")
        value = old[:prefix] + payload[position:end]
        values.append(value.decode("utf-8"))
        encoded.append(value)
        position = end
    return tuple(values), tuple(encoded), position


def core_payload(record: dict[str, Any], previous: tuple[bytes, ...]) -> tuple[bytes, tuple[bytes, ...]]:
    key_payload, encoded = encode_front_key(record["key"][:8], previous)
    payload = b"".join(
        (
            key_payload,
            uuid.UUID(record["id"]).bytes,
            struct.pack("<ii", round(record["lon"] * 10_000_000), round(record["lat"] * 10_000_000)),
            encode_uvarint(record["source_row_group"]),
            encode_uvarint(record["source_row_index"]),
        )
    )
    return payload, encoded


def page_dictionaries(records: list[dict[str, Any]]) -> tuple[list[str], list[tuple[int, ...]], dict[str, int], dict[tuple[int, ...], int]]:
    strings = sorted(
        {
            value
            for record in records
            for value in (
                *(record[field] for field in DISPLAY_FIELDS),
                *record["address_levels"],
            )
        }
    )
    string_ids = {value: index for index, value in enumerate(strings)}
    sequences = sorted({tuple(string_ids[value] for value in record["address_levels"]) for record in records})
    return strings, sequences, string_ids, {value: index for index, value in enumerate(sequences)}


def encode_page(records: list[dict[str, Any]], *, useful: bool) -> bytes:
    pieces = [encode_uvarint(len(records))]
    string_ids: dict[str, int] = {}
    sequence_ids: dict[tuple[int, ...], int] = {}
    if useful:
        strings, sequences, string_ids, sequence_ids = page_dictionaries(records)
        pieces.append(encode_uvarint(len(strings)))
        pieces.extend(encode_text(value) for value in strings)
        pieces.append(encode_uvarint(len(sequences)))
        for sequence in sequences:
            pieces.append(encode_uvarint(len(sequence)))
            pieces.extend(encode_uvarint(value) for value in sequence)
    previous = (b"",) * 8
    for record in records:
        core, previous = core_payload(record, previous)
        pieces.append(core)
        if useful:
            pieces.extend(encode_uvarint(string_ids[record[field]]) for field in DISPLAY_FIELDS)
            sequence = tuple(string_ids[value] for value in record["address_levels"])
            pieces.append(encode_uvarint(sequence_ids[sequence]))
    return b"".join(pieces)


def decode_page(payload: bytes, *, useful: bool) -> list[dict[str, Any]]:
    position = 0
    count, position = decode_uvarint(payload, position)
    strings: list[str] = []
    sequences: list[tuple[int, ...]] = []
    if useful:
        string_count, position = decode_uvarint(payload, position)
        for _ in range(string_count):
            value, position = decode_text(payload, position)
            strings.append(value)
        sequence_count, position = decode_uvarint(payload, position)
        for _ in range(sequence_count):
            length, position = decode_uvarint(payload, position)
            values = []
            for _ in range(length):
                value, position = decode_uvarint(payload, position)
                if value >= len(strings):
                    raise ValueError("address-level dictionary ID is out of range")
                values.append(value)
            sequences.append(tuple(values))
    previous = (b"",) * 8
    records = []
    for _ in range(count):
        key, previous, position = decode_front_key(payload, position, previous)
        if position + 24 > len(payload):
            raise ValueError("truncated compressed address core")
        feature_id = str(uuid.UUID(bytes=payload[position:position + 16]))
        position += 16
        lon, lat = struct.unpack_from("<ii", payload, position)
        position += 8
        row_group, position = decode_uvarint(payload, position)
        row_index, position = decode_uvarint(payload, position)
        record: dict[str, Any] = {
            "key": key + (feature_id,), "id": feature_id,
            "lon": lon / 10_000_000, "lat": lat / 10_000_000,
            "source_row_group": row_group, "source_row_index": row_index,
        }
        if useful:
            display = []
            for _ in DISPLAY_FIELDS:
                value, position = decode_uvarint(payload, position)
                if value >= len(strings):
                    raise ValueError("display dictionary ID is out of range")
                display.append(strings[value])
            sequence_id, position = decode_uvarint(payload, position)
            if sequence_id >= len(sequences):
                raise ValueError("address-level sequence ID is out of range")
            record.update(zip(DISPLAY_FIELDS, display))
            record["address_levels"] = [strings[value] for value in sequences[sequence_id]]
        records.append(record)
    if position != len(payload):
        raise ValueError("trailing compressed page bytes")
    return records


def digest_record(digest: Any, record: dict[str, Any], *, useful: bool) -> None:
    fields: list[Any] = [
        list(record["key"]), record["id"], record["lon"], record["lat"],
        record["source_row_group"], record["source_row_index"],
    ]
    if useful:
        fields.extend(record[field] for field in DISPLAY_FIELDS)
        fields.append(record["address_levels"])
    digest.update(canonical_json(fields))


def percentile(values: list[int], fraction: float) -> int:
    return sorted(values)[min(len(values) - 1, math.ceil(len(values) * fraction) - 1)]


def decompress_gzip_bounded(payload: bytes, max_bytes: int) -> bytes:
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    decoded = decoder.decompress(payload, max_bytes + 1)
    if len(decoded) > max_bytes or decoder.unconsumed_tail or not decoder.eof:
        raise ValueError("decoded compression page exceeds hard byte cap")
    decoded += decoder.flush()
    if len(decoded) > max_bytes:
        raise ValueError("decoded compression page exceeds hard byte cap")
    return decoded


def read_index(path: Path, *, max_bytes: int) -> list[dict[str, Any]]:
    if path.stat().st_size > max_bytes:
        raise ValueError("compression index exceeds hard byte cap")
    payload = path.read_bytes()
    if not payload.startswith(INDEX_MAGIC):
        raise ValueError("invalid compression index magic")
    position = len(INDEX_MAGIC)
    entries = []
    previous_key: tuple[str, ...] | None = None
    previous_offset = -1
    while position < len(payload):
        offset, position = decode_uvarint(payload, position)
        length, position = decode_uvarint(payload, position)
        rows, position = decode_uvarint(payload, position)
        key_length, position = decode_uvarint(payload, position)
        end = position + key_length
        if key_length > MAX_INDEX_KEY_BYTES or end > len(payload):
            raise ValueError("compression index key is outside hard bounds")
        key_payload = payload[position:end]
        key_position = 0
        key = []
        for _ in range(8):
            value, key_position = decode_text(key_payload, key_position)
            key.append(value)
        if key_position != len(key_payload):
            raise ValueError("invalid compression index key")
        normalized = tuple(key)
        if rows <= 0 or length <= 4 or offset <= previous_offset:
            raise ValueError("invalid compression index page extent")
        if previous_key is not None and normalized <= previous_key:
            raise ValueError("compression index keys are not strictly increasing")
        entries.append({"key": normalized, "offset": offset, "length": length, "rows": rows})
        previous_key = normalized
        previous_offset = offset
        position = end
    if not entries:
        raise ValueError("compression index is empty")
    return entries


def indexed_lookup(
    data_path: Path,
    index_path: Path,
    key: tuple[str, ...],
    *,
    useful: bool,
    compressed: bool,
    max_index_bytes: int,
    max_page_bytes: int,
) -> list[dict[str, Any]]:
    entries = read_index(index_path, max_bytes=max_index_bytes)
    keys = [entry["key"] for entry in entries]
    page_index = bisect.bisect_right(keys, key) - 1
    if page_index < 0:
        return []
    entry = entries[page_index]
    if entry["length"] > max_page_bytes + 4:
        raise ValueError("stored compression page exceeds hard byte cap")
    with data_path.open("rb") as source:
        source.seek(entry["offset"])
        encoded_length = source.read(4)
        if len(encoded_length) != 4:
            raise ValueError("truncated compression page length")
        length = struct.unpack("<I", encoded_length)[0]
        if length + 4 != entry["length"] or length > max_page_bytes:
            raise ValueError("compression page differs from index")
        stored = source.read(length)
        if len(stored) != length:
            raise ValueError("truncated compression page")
    payload = decompress_gzip_bounded(stored, max_page_bytes) if compressed else stored
    if len(payload) > max_page_bytes:
        raise ValueError("decoded compression page exceeds hard byte cap")
    return [record for record in decode_page(payload, useful=useful) if record["key"][:8] == key]


def run(
    input_path: Path,
    output_dir: Path,
    *,
    page_rows: int,
    planning_rows: int,
    max_input_bytes: int = 1_000_000_000,
    max_output_bytes: int = 1_000_000_000,
    max_workspace_bytes: int = 6_000_000_000,
    max_page_bytes: int = 8 * 1024 * 1024,
    max_page_rows: int = 10_000,
) -> dict[str, Any]:
    if page_rows <= 0 or page_rows > 4096:
        raise ValueError("page rows must be between 1 and 4096")
    if min(planning_rows, max_input_bytes, max_output_bytes, max_workspace_bytes, max_page_bytes, max_page_rows) <= 0:
        raise ValueError("compression planning values and hard caps must be positive")
    input_bytes = input_path.stat().st_size
    if input_bytes > max_input_bytes:
        raise ValueError("compression input exceeds hard byte cap")
    with AddressReduceArtifact(input_path) as artifact:
        if artifact.header["records"] == 0:
            raise ValueError("empty reduce artifacts do not require compression pages")
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    states: dict[str, dict[str, Any]] = {}
    for name, config in VARIANTS.items():
        data_path = output_dir / f"{name}.bin"
        index_path = output_dir / f"{name}.idx"
        header = canonical_json({"format": 1, "variant": name, "page_rows": page_rows})
        data_file = data_path.open("wb")
        data_file.write(DATA_MAGIC + struct.pack("<I", len(header)) + header)
        index_file = index_path.open("wb")
        index_file.write(INDEX_MAGIC)
        states[name] = {
            "config": config, "data_path": data_path, "index_path": index_path,
            "data": data_file, "index": index_file, "page_bytes": [],
            "encode_seconds": 0.0, "decode_verify_seconds": 0.0,
            "digest": hashlib.sha256(),
        }

    source_bare = hashlib.sha256()
    source_useful = hashlib.sha256()
    rows = pages = 0
    distinct_keys = 0
    max_fanout = 0
    max_group_digest = ""
    previous_key: tuple[str, ...] | None = None
    group_count = 0
    group_digest = hashlib.sha256()
    verification_groups: list[dict[str, Any]] = []
    maximum_group: dict[str, Any] | None = None

    def finish_group() -> None:
        nonlocal distinct_keys, max_fanout, max_group_digest, maximum_group
        if previous_key is None:
            return
        distinct_keys += 1
        group = {"key": previous_key, "count": group_count, "id_sha256": group_digest.hexdigest()}
        if len(verification_groups) < 2:
            verification_groups.append(group)
        if maximum_group is None or group_count > maximum_group["count"]:
            maximum_group = group
        if group_count > max_fanout:
            max_fanout = group_count
            max_group_digest = group_digest.hexdigest()

    with AddressReduceArtifact(input_path) as artifact:
        offset = 0
        pending: dict[str, Any] | None = None
        while pending is not None or offset < artifact.header["record_bytes"]:
            page = []
            if pending is not None:
                page.append(pending)
                pending = None
            while offset < artifact.header["record_bytes"]:
                record, next_offset = artifact._record_at(offset)
                key = record["key"][:8]
                if page and len(page) >= page_rows and key != page[-1]["key"][:8]:
                    pending = record
                    offset = next_offset
                    break
                page.append(record)
                offset = next_offset
                if len(page) > max_page_rows:
                    raise ValueError("candidate group exceeds hard page-row cap")
            for record in page:
                key = record["key"][:8]
                if key != previous_key:
                    finish_group()
                    previous_key = key
                    group_count = 0
                    group_digest = hashlib.sha256()
                group_count += 1
                group_digest.update(uuid.UUID(record["id"]).bytes)
                digest_record(source_bare, record, useful=False)
                digest_record(source_useful, record, useful=True)
                rows += 1
            for name, state in states.items():
                config = state["config"]
                encode_started = time.monotonic()
                raw = encode_page(page, useful=config["useful"])
                if len(raw) > max_page_bytes:
                    raise ValueError("decoded compression page exceeds hard byte cap")
                stored = gzip.compress(raw, compresslevel=6, mtime=0) if config["gzip"] else raw
                if len(stored) > max_page_bytes:
                    raise ValueError("stored compression page exceeds hard byte cap")
                state["encode_seconds"] += time.monotonic() - encode_started
                page_offset = state["data"].tell()
                projected_variant = page_offset + 4 + len(stored) + state["index"].tell() + MAX_INDEX_KEY_BYTES
                if projected_variant > max_output_bytes:
                    raise ValueError("compression variant exceeds hard output byte cap")
                state["data"].write(struct.pack("<I", len(stored)))
                state["data"].write(stored)
                first_key = b"".join(encode_text(value) for value in page[0]["key"][:8])
                state["index"].write(encode_uvarint(page_offset))
                state["index"].write(encode_uvarint(4 + len(stored)))
                state["index"].write(encode_uvarint(len(page)))
                state["index"].write(encode_uvarint(len(first_key)))
                state["index"].write(first_key)
                state["page_bytes"].append(4 + len(stored))
                workspace = input_bytes + sum(
                    item["data"].tell() + item["index"].tell() for item in states.values()
                )
                if workspace > max_workspace_bytes:
                    raise ValueError("compression workspace exceeds hard byte cap")
                verify_started = time.monotonic()
                decoded_payload = (
                    decompress_gzip_bounded(stored, max_page_bytes)
                    if config["gzip"] else stored
                )
                decoded = decode_page(decoded_payload, useful=config["useful"])
                for record in decoded:
                    digest_record(state["digest"], record, useful=config["useful"])
                state["decode_verify_seconds"] += time.monotonic() - verify_started
            pages += 1
        finish_group()
        if maximum_group is not None and all(group["key"] != maximum_group["key"] for group in verification_groups):
            verification_groups.append(maximum_group)
        if rows != artifact.header["records"]:
            raise ValueError("compression input record count does not reconcile")

    source_digests = {False: source_bare.hexdigest(), True: source_useful.hexdigest()}
    variants = {}
    for name, state in states.items():
        state["data"].close()
        state["index"].close()
        config = state["config"]
        if state["digest"].hexdigest() != source_digests[config["useful"]]:
            raise ValueError(f"{name} decode verification differs from source")
        data_bytes = state["data_path"].stat().st_size
        index_bytes = state["index_path"].stat().st_size
        total = data_bytes + index_bytes
        page_bytes = state["page_bytes"]
        variants[name] = {
            "accuracy": (
                "lossless relative to the reducer: exact candidate keys, display fields, raw address levels, coordinates, IDs, and locators"
                if config["useful"] else
                "exact candidate keys, normalized response fields, coordinates, IDs, and locators; drops display casing and raw address levels"
            ),
            "compression": "independent gzip page" if config["gzip"] else "none",
            "data_bytes": data_bytes, "index_bytes": index_bytes, "total_bytes": total,
            "bytes_per_indexed_row": round(total / rows, 6),
            "encode_seconds": round(state["encode_seconds"], 6),
            "decode_and_verify_seconds": round(state["decode_verify_seconds"], 6),
            "range_page_bytes": {
                "mean": round(statistics.mean(page_bytes), 3),
                "p50": percentile(page_bytes, 0.50), "p95": percentile(page_bytes, 0.95),
                "max": max(page_bytes),
            },
            "linear_all_planning_rows_gb": round(total / rows * planning_rows / 1_000_000_000, 3),
            "sha256": sha256_file(state["data_path"]),
            "index_sha256": sha256_file(state["index_path"]),
            "full_decode_digest_match": True,
        }
        for group in verification_groups:
            candidates = indexed_lookup(
                state["data_path"], state["index_path"], group["key"],
                useful=config["useful"], compressed=config["gzip"],
                max_index_bytes=max_output_bytes, max_page_bytes=max_page_bytes,
            )
            candidate_digest = hashlib.sha256()
            for candidate in candidates:
                candidate_digest.update(uuid.UUID(candidate["id"]).bytes)
            if len(candidates) != group["count"] or candidate_digest.hexdigest() != group["id_sha256"]:
                raise ValueError(f"{name} indexed lookup differs from candidate oracle")
        variants[name]["indexed_candidate_sets_verified"] = len(verification_groups)
    baseline_bytes = input_path.stat().st_size
    return {
        "schema": "overture-address-compression-spike-v1",
        "input": {
            "path": str(input_path), "bytes": baseline_bytes, "sha256": sha256_file(input_path),
            "records": rows, "bytes_per_indexed_row": round(baseline_bytes / rows, 6),
        },
        "page_rows": page_rows, "pages": pages, "variants": variants,
        "oracle": {
            "distinct_lookup_keys": distinct_keys, "maximum_candidate_fanout": max_fanout,
            "maximum_fanout_id_sha256": max_group_digest,
            "candidate_order_and_ids_preserved": True,
            "candidate_groups_never_cross_pages": True,
            "indexed_candidate_sets_verified": len(verification_groups),
        },
        "planning": {
            "rows": planning_rows,
            "warning": "linear all-row diagnostics are not global forecasts; the measured source range retained only rows with street and number",
        },
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "limits": {
            "max_input_bytes": max_input_bytes, "max_output_bytes_per_variant": max_output_bytes,
            "max_workspace_bytes": max_workspace_bytes, "max_page_bytes": max_page_bytes,
            "max_page_rows": max_page_rows,
        },
        "limitations": [
            "one purposively selected source-object range, not globally representative",
            "gzip support and latency are measured locally in Python, not in a Cloudflare Worker",
            "the side index is measured but a production shard catalog and HTTP range request are not",
            "no R2 objects or production state are written",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--page-rows", type=int, default=256)
    parser.add_argument("--planning-rows", type=int, default=473_000_000)
    parser.add_argument("--max-input-bytes", type=int, default=1_000_000_000)
    parser.add_argument("--max-output-bytes", type=int, default=1_000_000_000)
    parser.add_argument("--max-workspace-bytes", type=int, default=6_000_000_000)
    parser.add_argument("--max-page-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--max-page-rows", type=int, default=10_000)
    args = parser.parse_args()
    report = run(
        args.input, args.output_dir, page_rows=args.page_rows, planning_rows=args.planning_rows,
        max_input_bytes=args.max_input_bytes, max_output_bytes=args.max_output_bytes,
        max_workspace_bytes=args.max_workspace_bytes, max_page_bytes=args.max_page_bytes,
        max_page_rows=args.max_page_rows,
    )
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
