#!/usr/bin/env python3
"""Measure a bounded, streaming map-fragment/address-reduce data path.

The input is the projected Parquet produced by experiment_hosted_rowgroups.py.
This spike deliberately keeps the expensive correctness fields: raw address
levels and an exact source row-group/row locator.  It does not join divisions,
upload to R2, or publish a catalog.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import heapq
import json
import os
import resource
import shutil
import struct
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, BinaryIO


FRAGMENT_MAGIC = b"OAMAP01\0"
ARTIFACT_MAGIC = b"OARED01\0"
FORMAT_VERSION = 2
KEY_FIELDS = 8
MAX_HEADER_BYTES = 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024
MAX_SPARSE_KEY_BYTES = 64 * 1024
MAX_FRAGMENTS = 1024
MAX_LOOKUP_CANDIDATES = 10_000
MAX_LOOKUP_SCAN_BYTES = 8 * 1024 * 1024
SPIKE_PARTITION_ID = "bounded-single-partition"


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def encode_uvarint(value: int) -> bytes:
    if value < 0:
        raise ValueError("uvarint cannot encode a negative value")
    result = bytearray()
    while value >= 0x80:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value)
    return bytes(result)


def decode_uvarint(payload: bytes, position: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    while position < len(payload) and shift <= 63:
        byte = payload[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7
    raise ValueError("invalid or truncated uvarint")


def encode_text(value: str | None) -> bytes:
    encoded = (value or "").encode("utf-8")
    return encode_uvarint(len(encoded)) + encoded


def decode_text(payload: bytes, position: int) -> tuple[str, int]:
    length, position = decode_uvarint(payload, position)
    end = position + length
    if end > len(payload):
        raise ValueError("truncated text")
    return payload[position:end].decode("utf-8"), end


ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def normalize(value: str | None) -> str:
    return " ".join(unicodedata.normalize("NFC", value or "").split()).translate(
        ASCII_LOWER
    )


def address_level_values(value: Any) -> list[str]:
    result = []
    for level in value or []:
        if isinstance(level, dict):
            item = level.get("value")
        else:
            item = getattr(level, "value", None)
        if item is not None:
            result.append(str(item))
    return result


def point_coordinates(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return None
    payload = bytes(value)
    if len(payload) < 21 or payload[0] not in (0, 1):
        return None
    endian = "<" if payload[0] == 1 else ">"
    geometry_type = struct.unpack_from(f"{endian}I", payload, 1)[0] & 0xFF
    if geometry_type != 1:
        return None
    lon, lat = struct.unpack_from(f"{endian}dd", payload, 5)
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return lon, lat


def feature_id_bytes(value: str) -> bytes:
    try:
        return uuid.UUID(value).bytes
    except (ValueError, AttributeError) as exc:
        raise ValueError("address ID is not a UUID") from exc


def record_key(record: dict[str, Any]) -> tuple[str, ...]:
    levels = record["address_levels"]
    return (
        normalize(record["country"]),
        normalize(levels[0] if levels else ""),
        normalize(levels[-1] if levels else ""),
        normalize(record["postal_city"]),
        normalize(record["postcode"]),
        normalize(record["street"]),
        normalize(record["number"]),
        normalize(record["unit"]),
        record["id"],
    )


def encode_record(record: dict[str, Any]) -> bytes:
    key = record_key(record)
    levels = record["address_levels"]
    pieces = [encode_text(value) for value in key[:-1]]
    pieces.extend(
        (
            feature_id_bytes(record["id"]),
            struct.pack(
                "<ii",
                round(record["lon"] * 10_000_000),
                round(record["lat"] * 10_000_000),
            ),
            encode_uvarint(record["source_object_index"]),
            encode_uvarint(record["source_row_group"]),
            encode_uvarint(record["source_row_index"]),
            encode_text(record["country"]),
            encode_text(record["postal_city"]),
            encode_text(record["postcode"]),
            encode_text(record["street"]),
            encode_text(record["number"]),
            encode_text(record["unit"]),
            encode_uvarint(len(levels)),
        )
    )
    pieces.extend(encode_text(value) for value in levels)
    return b"".join(pieces)


def decode_record(payload: bytes) -> dict[str, Any]:
    position = 0
    normalized = []
    for _ in range(KEY_FIELDS):
        value, position = decode_text(payload, position)
        normalized.append(value)
    if position + 24 > len(payload):
        raise ValueError("truncated address record")
    feature_id = str(uuid.UUID(bytes=payload[position : position + 16]))
    position += 16
    lon, lat = struct.unpack_from("<ii", payload, position)
    position += 8
    source_object_index, position = decode_uvarint(payload, position)
    source_row_group, position = decode_uvarint(payload, position)
    source_row_index, position = decode_uvarint(payload, position)
    display = []
    for _ in range(6):
        value, position = decode_text(payload, position)
        display.append(value)
    level_count, position = decode_uvarint(payload, position)
    levels = []
    for _ in range(level_count):
        value, position = decode_text(payload, position)
        levels.append(value)
    if position != len(payload):
        raise ValueError("trailing address record bytes")
    return {
        "key": tuple(normalized) + (feature_id,),
        "id": feature_id,
        "lon": lon / 10_000_000,
        "lat": lat / 10_000_000,
        "source_object_index": source_object_index,
        "source_row_group": source_row_group,
        "source_row_index": source_row_index,
        "country": display[0],
        "postal_city": display[1],
        "postcode": display[2],
        "street": display[3],
        "number": display[4],
        "unit": display[5],
        "address_levels": levels,
    }


def write_envelope(output: BinaryIO, magic: bytes, header: dict[str, Any]) -> None:
    encoded = canonical_json(header)
    if len(encoded) > MAX_HEADER_BYTES:
        raise ValueError("file header exceeds hard byte cap")
    output.write(magic)
    output.write(struct.pack("<I", len(encoded)))
    output.write(encoded)


def read_envelope(source: BinaryIO, magic: bytes) -> dict[str, Any]:
    if source.read(len(magic)) != magic:
        raise ValueError("invalid file magic")
    length = source.read(4)
    if len(length) != 4:
        raise ValueError("truncated file header")
    header_length = struct.unpack("<I", length)[0]
    if header_length > MAX_HEADER_BYTES:
        raise ValueError("file header exceeds hard byte cap")
    payload = source.read(header_length)
    if len(payload) != header_length:
        raise ValueError("truncated file header payload")
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid file header") from exc


def projected_metadata(parquet: Any) -> dict[str, Any]:
    metadata = parquet.schema_arrow.metadata or {}
    required = (
        b"overture.source_inventory_sha256",
        b"overture.source_inventory_json",
        b"overture.release",
        b"overture.family",
    )
    missing = [key.decode() for key in required if key not in metadata]
    if missing:
        raise ValueError(f"projected input is missing source metadata: {missing}")
    inventory = json.loads(metadata[b"overture.source_inventory_json"])
    digest = hashlib.sha256(canonical_json(inventory)).hexdigest()
    stored_digest = metadata[b"overture.source_inventory_sha256"].decode()
    if digest != stored_digest:
        raise ValueError("projected source inventory digest differs from its JSON")
    release = metadata[b"overture.release"].decode()
    family = metadata[b"overture.family"].decode()
    if inventory.get("release") != release or inventory.get("family") != family:
        raise ValueError("projected source inventory identity differs")
    return {
        "source_inventory_sha256": stored_digest,
        "source_inventory": inventory,
        "release": release,
        "family": family,
    }


def batch_records(
    batch: Any,
) -> tuple[list[tuple[tuple[str, ...], bytes]], dict[str, int]]:
    columns = {name: batch.column(name).to_pylist() for name in batch.schema.names}
    encoded: list[tuple[tuple[str, ...], bytes]] = []
    rejected = {"missing_street_or_number": 0, "invalid_geometry": 0}
    for index in range(batch.num_rows):
        street = columns["street"][index]
        number = columns["number"][index]
        if not normalize(street) or not normalize(
            str(number) if number is not None else ""
        ):
            rejected["missing_street_or_number"] += 1
            continue
        point = point_coordinates(columns["geometry"][index])
        if point is None:
            rejected["invalid_geometry"] += 1
            continue
        record = {
            "id": str(columns["id"][index]),
            "street": str(street),
            "number": str(number),
            "unit": ""
            if columns["unit"][index] is None
            else str(columns["unit"][index]),
            "postcode": ""
            if columns["postcode"][index] is None
            else str(columns["postcode"][index]),
            "postal_city": ""
            if columns["postal_city"][index] is None
            else str(columns["postal_city"][index]),
            "country": ""
            if columns["country"][index] is None
            else str(columns["country"][index]),
            "address_levels": address_level_values(columns["address_levels"][index]),
            "lon": point[0],
            "lat": point[1],
            "source_object_index": int(columns["source_object_index"][index]),
            "source_row_group": int(columns["source_row_group"][index]),
            "source_row_index": int(columns["source_row_index"][index]),
        }
        encoded.append((record_key(record), encode_record(record)))
    encoded.sort(key=lambda item: item[0])
    return encoded, rejected


def build_fragments(
    input_path: Path,
    fragment_dir: Path,
    *,
    fragment_rows: int,
    max_rows: int,
    max_workspace_bytes: int,
    input_bytes: int,
) -> dict[str, Any]:
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(input_path)
    metadata = projected_metadata(parquet)
    if input_bytes > max_workspace_bytes:
        raise ValueError("projected input exceeds reduce workspace hard cap")
    if parquet.metadata.num_rows > max_rows:
        raise ValueError("projected input exceeds reduce spike row cap")
    fragment_dir.mkdir(parents=True, exist_ok=True)
    fragments = []
    fragment_bytes = 0
    input_rows = selected_rows = 0
    rejected = {"missing_street_or_number": 0, "invalid_geometry": 0}
    started = time.monotonic()
    for index, batch in enumerate(parquet.iter_batches(batch_size=fragment_rows)):
        if index >= MAX_FRAGMENTS:
            raise ValueError("map fragment count exceeds hard cap")
        input_rows += batch.num_rows
        records, batch_rejected = batch_records(batch)
        selected_rows += len(records)
        for reason, count in batch_rejected.items():
            rejected[reason] += count
        path = fragment_dir / f"fragment-{index:04d}.bin"
        header = {
            "format": FORMAT_VERSION,
            "source_inventory_sha256": metadata["source_inventory_sha256"],
            "records": len(records),
            "fragment_index": index,
            "partition_id": SPIKE_PARTITION_ID,
            "sorted_by": "country/general/specific/postal_city/postcode/street/number/unit/id",
        }
        header_size = len(FRAGMENT_MAGIC) + 4 + len(canonical_json(header))
        if input_bytes + fragment_bytes + header_size > max_workspace_bytes:
            raise ValueError("map fragment workspace exceeds hard byte cap")
        with path.open("wb") as output:
            write_envelope(output, FRAGMENT_MAGIC, header)
            for _, payload in records:
                if len(payload) > MAX_RECORD_BYTES:
                    raise ValueError("fragment record exceeds hard byte cap")
                projected = (
                    input_bytes + fragment_bytes + output.tell() + 4 + len(payload)
                )
                if projected > max_workspace_bytes:
                    raise ValueError("map fragment workspace exceeds hard byte cap")
                output.write(struct.pack("<I", len(payload)))
                output.write(payload)
        fragment_bytes += path.stat().st_size
        if input_bytes + fragment_bytes > max_workspace_bytes:
            raise ValueError("map fragment workspace exceeds hard byte cap")
        fragments.append(
            {
                "index": index,
                "partition_id": SPIKE_PARTITION_ID,
                "path": str(path),
                "bytes": path.stat().st_size,
                "records": len(records),
                "sha256": sha256_file(path),
            }
        )
    if input_rows != parquet.metadata.num_rows or input_rows != selected_rows + sum(
        rejected.values()
    ):
        raise ValueError("map fragment accounting does not reconcile")
    return {
        "source": metadata,
        "input_rows": input_rows,
        "selected_rows": selected_rows,
        "rejected": rejected,
        "fragments": fragments,
        "seconds": time.monotonic() - started,
    }


class FragmentReader:
    def __init__(self, path: Path):
        self.path = path
        self.file = self.path.open("rb")
        self.header = read_envelope(self.file, FRAGMENT_MAGIC)
        if self.header.get("format") != FORMAT_VERSION:
            raise ValueError("unsupported fragment format")
        self.read_count = 0
        self.previous: tuple[str, ...] | None = None

    def next(self) -> tuple[tuple[str, ...], bytes] | None:
        length = self.file.read(4)
        if not length:
            if self.read_count != self.header["records"]:
                raise ValueError("fragment record count mismatch")
            return None
        if len(length) != 4:
            raise ValueError("truncated fragment record length")
        record_length = struct.unpack("<I", length)[0]
        if record_length > MAX_RECORD_BYTES:
            raise ValueError("fragment record exceeds hard byte cap")
        remaining = self.path.stat().st_size - self.file.tell()
        if record_length > remaining:
            raise ValueError("fragment record extends beyond file")
        payload = self.file.read(record_length)
        if len(payload) != record_length:
            raise ValueError("truncated fragment record")
        key = decode_record(payload)["key"]
        if self.previous is not None and key < self.previous:
            raise ValueError("fragment is not sorted")
        self.previous = key
        self.read_count += 1
        return key, payload

    def close(self) -> None:
        self.file.close()


def key_prefix_payload(key: tuple[str, ...]) -> bytes:
    return b"".join(encode_text(value) for value in key[:KEY_FIELDS])


def build_artifact(
    fragments: list[dict[str, Any]],
    output_path: Path,
    *,
    source: dict[str, str],
    sparse_stride: int,
    max_artifact_bytes: int,
    max_workspace_bytes: int,
    input_bytes: int,
) -> dict[str, Any]:
    started = time.monotonic()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if type(sparse_stride) is not int or sparse_stride <= 0:
        raise ValueError("sparse stride must be a positive integer")
    if not fragments or len(fragments) > MAX_FRAGMENTS:
        raise ValueError("fragment inventory count is outside hard limits")
    seen_indexes: set[int] = set()
    seen_paths: set[Path] = set()
    expected_fragment_records = 0
    fragment_bytes = 0
    for manifest in fragments:
        path = Path(manifest["path"])
        index = manifest.get("index")
        if type(index) is not int or index < 0 or index in seen_indexes:
            raise ValueError("fragment indexes must be unique non-negative integers")
        if path in seen_paths:
            raise ValueError("fragment paths must be unique")
        if manifest.get("partition_id") != SPIKE_PARTITION_ID:
            raise ValueError("fragment partition identity differs")
        manifest_bytes = manifest.get("bytes")
        if type(manifest_bytes) is not int or manifest_bytes <= 0:
            raise ValueError("fragment byte count must be a positive integer")
        if path.stat().st_size != manifest_bytes:
            raise ValueError("fragment size differs from manifest")
        if sha256_file(path) != manifest.get("sha256"):
            raise ValueError("fragment SHA-256 differs from manifest")
        records = manifest.get("records")
        if type(records) is not int or records < 0:
            raise ValueError("fragment record count must be a non-negative integer")
        expected_fragment_records += records
        fragment_bytes += manifest_bytes
        seen_indexes.add(index)
        seen_paths.add(path)
    fragments = sorted(fragments, key=lambda item: item["index"])
    readers = [FragmentReader(Path(item["path"])) for item in fragments]
    try:
        source_digests = {
            reader.header["source_inventory_sha256"] for reader in readers
        }
        if source_digests != {source["source_inventory_sha256"]}:
            raise ValueError("fragment source inventories differ")
        for manifest, reader in zip(fragments, readers):
            if (
                reader.header.get("records") != manifest["records"]
                or reader.header.get("fragment_index") != manifest["index"]
                or reader.header.get("partition_id") != manifest["partition_id"]
            ):
                raise ValueError("fragment header differs from manifest")
        heap: list[tuple[tuple[str, ...], int, bytes]] = []
        for index, reader in enumerate(readers):
            item = reader.next()
            if item is not None:
                heapq.heappush(heap, (item[0], index, item[1]))
        with tempfile.TemporaryDirectory(
            prefix="address-reduce-", dir=output_path.parent
        ) as temp_name:
            temp = Path(temp_name)
            record_path = temp / "records.bin"
            sparse_path = temp / "sparse.bin"
            rows = 0
            previous: tuple[str, ...] | None = None
            current_prefix: tuple[str, ...] | None = None
            current_count = max_fanout = distinct_keys = 0
            verification_groups: list[dict[str, Any]] = []
            maximum_group: dict[str, Any] | None = None
            current_id_digest = hashlib.sha256()

            def finish_group() -> None:
                nonlocal max_fanout, distinct_keys, maximum_group
                if current_prefix is None:
                    return
                distinct_keys += 1
                max_fanout = max(max_fanout, current_count)
                group = {
                    "key": list(current_prefix),
                    "count": current_count,
                    "id_sha256": current_id_digest.hexdigest(),
                }
                if len(verification_groups) < 2:
                    verification_groups.append(group)
                if maximum_group is None or current_count > maximum_group["count"]:
                    maximum_group = group

            with (
                record_path.open("wb") as record_file,
                sparse_path.open("wb") as sparse_file,
            ):
                while heap:
                    key, reader_index, payload = heapq.heappop(heap)
                    if previous is not None and key < previous:
                        raise ValueError("reduce merge output is not sorted")
                    prefix = key[:KEY_FIELDS]
                    if prefix != current_prefix:
                        finish_group()
                        current_prefix = prefix
                        current_count = 0
                        current_id_digest = hashlib.sha256()
                    current_count += 1
                    current_id_digest.update(uuid.UUID(key[-1]).bytes)
                    if rows % sparse_stride == 0:
                        key_payload = key_prefix_payload(key)
                        if len(key_payload) > MAX_SPARSE_KEY_BYTES:
                            raise ValueError("sparse key exceeds hard byte cap")
                        sparse_entry = (
                            encode_uvarint(rows)
                            + encode_uvarint(record_file.tell())
                            + encode_uvarint(len(key_payload))
                            + key_payload
                        )
                        projected_sparse = (
                            input_bytes
                            + fragment_bytes
                            + record_file.tell()
                            + sparse_file.tell()
                            + len(sparse_entry)
                        )
                        if projected_sparse > max_workspace_bytes:
                            raise ValueError("reduce workspace exceeds hard byte cap")
                        sparse_file.write(sparse_entry)
                    if len(payload) > MAX_RECORD_BYTES:
                        raise ValueError("artifact record exceeds hard byte cap")
                    projected_records = (
                        input_bytes
                        + fragment_bytes
                        + record_file.tell()
                        + sparse_file.tell()
                        + 4
                        + len(payload)
                    )
                    if projected_records > max_workspace_bytes:
                        raise ValueError("reduce workspace exceeds hard byte cap")
                    record_file.write(struct.pack("<I", len(payload)))
                    record_file.write(payload)
                    rows += 1
                    previous = key
                    item = readers[reader_index].next()
                    if item is not None:
                        heapq.heappush(heap, (item[0], reader_index, item[1]))
                finish_group()
            if maximum_group is not None and all(
                group["key"] != maximum_group["key"] for group in verification_groups
            ):
                verification_groups.append(maximum_group)

            record_bytes = record_path.stat().st_size
            sparse_bytes = sparse_path.stat().st_size
            if rows != expected_fragment_records:
                raise ValueError("reduce rows differ from fragment manifests")
            header = {
                "format": FORMAT_VERSION,
                "records": rows,
                "distinct_lookup_keys": distinct_keys,
                "sparse_stride": sparse_stride,
                "sparse_bytes": sparse_bytes,
                "record_bytes": record_bytes,
                "source": source,
                "fragment_sha256": [item["sha256"] for item in fragments],
                "fields": [
                    "id",
                    "coordinates",
                    "country",
                    "postal_city",
                    "postcode",
                    "street",
                    "number",
                    "unit",
                    "raw_address_levels",
                    "source_object_index",
                    "source_row_group",
                    "source_row_index",
                ],
            }
            header_bytes = canonical_json(header)
            projected_artifact_bytes = (
                len(ARTIFACT_MAGIC)
                + 4
                + len(header_bytes)
                + sparse_bytes
                + record_bytes
            )
            peak_workspace_estimate = (
                input_bytes
                + fragment_bytes
                + record_bytes
                + sparse_bytes
                + projected_artifact_bytes
            )
            if projected_artifact_bytes > max_artifact_bytes:
                raise ValueError("reduced artifact exceeds hard byte cap")
            if peak_workspace_estimate > max_workspace_bytes:
                raise ValueError("reduce workspace exceeds hard byte cap")
            temporary_output: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix=f".{output_path.name}.",
                    suffix=".tmp",
                    dir=output_path.parent,
                    delete=False,
                ) as output:
                    temporary_output = Path(output.name)
                    write_envelope(output, ARTIFACT_MAGIC, header)
                    with sparse_path.open("rb") as source_file:
                        shutil.copyfileobj(source_file, output)
                    with record_path.open("rb") as source_file:
                        shutil.copyfileobj(source_file, output)
                    if output.tell() > max_artifact_bytes:
                        raise ValueError("final reduced artifact exceeds hard byte cap")
                os.replace(temporary_output, output_path)
                temporary_output = None
            finally:
                if temporary_output is not None:
                    temporary_output.unlink(missing_ok=True)
    finally:
        for reader in readers:
            reader.close()

    return {
        "rows": rows,
        "distinct_lookup_keys": distinct_keys,
        "maximum_candidate_fanout": max_fanout,
        "bytes": output_path.stat().st_size,
        "sha256": sha256_file(output_path),
        "sparse_bytes": sparse_bytes,
        "record_bytes": record_bytes,
        "peak_workspace_bytes_conservative": peak_workspace_estimate,
        "verification_groups": verification_groups,
        "seconds": time.monotonic() - started,
    }


class AddressReduceArtifact:
    def __init__(self, path: Path):
        self.path = path
        self.file = path.open("rb")
        self.header = read_envelope(self.file, ARTIFACT_MAGIC)
        if self.header.get("format") != FORMAT_VERSION:
            raise ValueError("unsupported reduce artifact format")
        for field in ("records", "sparse_bytes", "record_bytes"):
            if type(self.header.get(field)) is not int or self.header[field] < 0:
                raise ValueError(f"invalid reduce artifact {field}")
        if (
            type(self.header.get("sparse_stride")) is not int
            or self.header["sparse_stride"] <= 0
        ):
            raise ValueError("invalid reduce artifact sparse_stride")
        if self.header["records"] == 0:
            if self.header["sparse_bytes"] != 0 or self.header["record_bytes"] != 0:
                raise ValueError("empty reduce artifact has non-empty sections")
        elif self.header["sparse_bytes"] == 0 or self.header["record_bytes"] == 0:
            raise ValueError("non-empty reduce artifact has an empty section")
        fragment_digests = self.header.get("fragment_sha256")
        if (
            not isinstance(fragment_digests, list)
            or not 0 < len(fragment_digests) <= MAX_FRAGMENTS
        ):
            raise ValueError("invalid reduce artifact fragment inventory")
        self.sparse_start = self.file.tell()
        self.records_start = self.sparse_start + self.header["sparse_bytes"]
        expected = self.records_start + self.header["record_bytes"]
        if expected != path.stat().st_size:
            raise ValueError("reduce artifact size does not match header")
        self.sparse: list[tuple[tuple[str, ...], int, int]] = []
        self.file.seek(self.sparse_start)
        end = self.records_start
        while self.file.tell() < end:
            ordinal = self._read_uvarint_file()
            offset = self._read_uvarint_file()
            length = self._read_uvarint_file()
            if length > MAX_SPARSE_KEY_BYTES or self.file.tell() + length > end:
                raise ValueError("sparse key length is outside section bounds")
            payload = self.file.read(length)
            position = 0
            key = []
            for _ in range(KEY_FIELDS):
                value, position = decode_text(payload, position)
                key.append(value)
            if position != len(payload):
                raise ValueError("invalid sparse key")
            self.sparse.append((tuple(key), ordinal, offset))
        if self.file.tell() != end or (self.header["records"] > 0 and not self.sparse):
            raise ValueError("invalid sparse directory")
        previous_key: tuple[str, ...] | None = None
        previous_offset = -1
        for index, (key, ordinal, offset) in enumerate(self.sparse):
            if ordinal != index * self.header["sparse_stride"]:
                raise ValueError("invalid sparse ordinal stride")
            if previous_key is not None and key < previous_key:
                raise ValueError("sparse keys are not sorted")
            if offset <= previous_offset or offset >= self.header["record_bytes"]:
                raise ValueError("sparse record offsets are not strictly increasing")
            record, _ = self._record_at(offset)
            if record["key"][:KEY_FIELDS] != key:
                raise ValueError("sparse entry does not identify its target record")
            previous_key = key
            previous_offset = offset

    def _read_uvarint_file(self) -> int:
        value = shift = 0
        while shift <= 63:
            byte = self.file.read(1)
            if not byte:
                raise ValueError("truncated sparse directory")
            item = byte[0]
            value |= (item & 0x7F) << shift
            if not item & 0x80:
                return value
            shift += 7
        raise ValueError("invalid sparse directory integer")

    def close(self) -> None:
        self.file.close()

    def __enter__(self) -> "AddressReduceArtifact":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _record_at(self, offset: int) -> tuple[dict[str, Any], int]:
        if offset < 0 or offset + 4 > self.header["record_bytes"]:
            raise ValueError("artifact record offset is outside record section")
        self.file.seek(self.records_start + offset)
        length = self.file.read(4)
        if len(length) != 4:
            raise ValueError("truncated artifact record")
        size = struct.unpack("<I", length)[0]
        if size > MAX_RECORD_BYTES or offset + 4 + size > self.header["record_bytes"]:
            raise ValueError("artifact record length is outside section bounds")
        payload = self.file.read(size)
        if len(payload) != size:
            raise ValueError("truncated artifact record payload")
        return decode_record(payload), offset + 4 + size

    def lookup(
        self,
        key: tuple[str, ...],
        *,
        max_candidates: int = MAX_LOOKUP_CANDIDATES,
        max_scan_bytes: int = MAX_LOOKUP_SCAN_BYTES,
    ) -> list[dict[str, Any]]:
        if len(key) != KEY_FIELDS:
            raise ValueError(f"lookup key must have {KEY_FIELDS} fields")
        if (
            type(max_candidates) is not int
            or max_candidates <= 0
            or type(max_scan_bytes) is not int
            or max_scan_bytes <= 0
        ):
            raise ValueError("lookup caps must be positive integers")
        normalized = tuple(normalize(value) for value in key)
        if self.header["records"] == 0:
            return []
        directory_keys = [item[0] for item in self.sparse]
        block = max(0, bisect.bisect_left(directory_keys, normalized) - 1)
        offset = self.sparse[block][2]
        scan_start = offset
        results = []
        while offset < self.header["record_bytes"]:
            self.file.seek(self.records_start + offset)
            encoded_length = self.file.read(4)
            if len(encoded_length) != 4:
                raise ValueError("truncated artifact record")
            next_size = struct.unpack("<I", encoded_length)[0]
            if offset - scan_start + 4 + next_size > max_scan_bytes:
                raise ValueError("lookup exceeds hard scan-byte cap")
            record, offset = self._record_at(offset)
            prefix = record["key"][:KEY_FIELDS]
            if prefix < normalized:
                continue
            if prefix > normalized:
                break
            results.append(record)
            if len(results) > max_candidates:
                raise ValueError("lookup exceeds hard candidate cap")
        return results

    def verify(self, groups: list[dict[str, Any]]) -> dict[str, Any]:
        count = 0
        previous: tuple[str, ...] | None = None
        offset = 0
        while offset < self.header["record_bytes"]:
            record, offset = self._record_at(offset)
            if previous is not None and record["key"] < previous:
                raise ValueError("artifact records are not sorted")
            previous = record["key"]
            count += 1
        if offset != self.header["record_bytes"] or count != self.header["records"]:
            raise ValueError("artifact record inventory does not reconcile")
        for group in groups:
            candidates = self.lookup(tuple(group["key"]))
            candidate_digest = hashlib.sha256()
            for candidate in candidates:
                candidate_digest.update(uuid.UUID(candidate["id"]).bytes)
            if (
                len(candidates) != group["count"]
                or candidate_digest.hexdigest() != group["id_sha256"]
            ):
                raise ValueError("artifact lookup differs from reduce oracle")
        return {
            "full_sorted_scan": True,
            "record_count_match": True,
            "exact_candidate_sets": len(groups),
        }


def peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value * 1024 if value < 10_000_000 else value


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    input_bytes = args.input.stat().st_size
    disk_before = shutil.disk_usage(args.work_dir)
    if disk_before.free < args.max_workspace_bytes:
        raise ValueError("free disk is below configured workspace reservation")
    with tempfile.TemporaryDirectory(
        prefix="address-map-fragments-", dir=args.work_dir
    ) as name:
        fragment_dir = Path(name)
        map_report = build_fragments(
            args.input,
            fragment_dir,
            fragment_rows=args.fragment_rows,
            max_rows=args.max_rows,
            max_workspace_bytes=args.max_workspace_bytes,
            input_bytes=input_bytes,
        )
        reduce_report = build_artifact(
            map_report["fragments"],
            args.output,
            source=map_report["source"],
            sparse_stride=args.sparse_stride,
            max_artifact_bytes=args.max_artifact_bytes,
            max_workspace_bytes=args.max_workspace_bytes,
            input_bytes=input_bytes,
        )
        with AddressReduceArtifact(args.output) as artifact:
            verification = artifact.verify(reduce_report["verification_groups"])
        fragment_bytes = sum(item["bytes"] for item in map_report["fragments"])
    report = {
        "schema": "overture-address-reduce-spike-v1",
        "input": {
            "path": str(args.input),
            "bytes": input_bytes,
            "sha256": sha256_file(args.input),
        },
        "map_fragments": {**map_report, "bytes": fragment_bytes},
        "reduce": {
            **reduce_report,
            "path": str(args.output),
            "verification": verification,
        },
        "resources": {
            "elapsed_seconds": time.monotonic() - started,
            "peak_rss_bytes": peak_rss_bytes(),
            "disk_free_before": disk_before.free,
            "disk_free_after": shutil.disk_usage(args.work_dir).free,
        },
        "limits": {
            "max_rows": args.max_rows,
            "fragment_rows": args.fragment_rows,
            "sparse_stride": args.sparse_stride,
            "max_artifact_bytes": args.max_artifact_bytes,
            "max_workspace_bytes": args.max_workspace_bytes,
        },
        "limitations": [
            "one purposively selected source-object range, not a globally representative partition",
            "local fragments stand in for content-addressed R2 downloads; R2 shuffle is not measured",
            "division enrichment and a one-line address parser are not included",
            "format keeps raw address levels and exact source locators but does not dictionary-compress display strings",
        ],
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, default=Path("/tmp"))
    parser.add_argument("--max-rows", type=int, default=4_000_000)
    parser.add_argument("--fragment-rows", type=int, default=128_000)
    parser.add_argument("--sparse-stride", type=int, default=256)
    parser.add_argument("--max-artifact-bytes", type=int, default=1_000_000_000)
    parser.add_argument("--max-workspace-bytes", type=int, default=12_000_000_000)
    args = parser.parse_args()
    if (
        min(
            args.max_rows,
            args.fragment_rows,
            args.sparse_stride,
            args.max_artifact_bytes,
            args.max_workspace_bytes,
        )
        <= 0
    ):
        raise SystemExit("all limits must be positive")
    print(json.dumps(run(args), sort_keys=True))


if __name__ == "__main__":
    main()
