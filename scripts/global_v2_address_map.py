#!/usr/bin/env python3
"""Strict, resumable address mapper for the global v2 data plane.

The mapper consumes one provenance-bearing projected Parquet task, applies the
global address admission contract, and writes immutable content-addressed map
fragments. Its ``execution_bucket`` is only a scheduler assignment: records are
compacted into bounded country-owned, max-bucket-ordered fragments so one
country reducer pass can emit every stable serving leaf from the partition plan.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import struct
import sys
import tempfile
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from address_partition import (  # noqa: E402
    COUNT_SCHEMA,
    DEFAULT_MAXIMUM_HASH_BITS,
    MAXIMUM_SUPPORTED_HASH_BITS,
    NORMALIZATION_VERSION,
    hash_bucket,
    record_hash,
    validate_country,
)
from experiment_address_reduce import (  # noqa: E402
    FRAGMENT_MAGIC,
    MAX_RECORD_BYTES,
    canonical_json,
    decode_record,
    encode_record,
    projected_metadata,
    read_envelope,
    sha256_file,
    strict_batch_records,
    write_envelope,
)
import inventory_address_rowgroups as address_inventory  # noqa: E402


COMPLETION_SCHEMA = "overture-global-v2-address-map-completion-v2"
FRAGMENT_MANIFEST_SCHEMA = "overture-global-v2-address-data-pack-manifest-v1"
EXECUTION_BUCKET_KIND = "global-v2-map-execution-bucket-v1"
FRAGMENT_OWNERSHIP_KIND = "address-country-bucket-range-pack-v1"
WIRE_ENCODING = "address-typed-parquet-shuffle-v1"
SUMMARY_SCHEMA = "overture-global-v2-address-bucket-summary-v1"
SEMANTIC_BINDING_SCHEMA = "overture-address-semantic-multiset-binding-v1"
SEMANTIC_BINDING_DOMAIN_A = b"overture-address-semantic-record-v1\x00"
SEMANTIC_BINDING_DOMAIN_B = b"overture-address-semantic-record-v1\x01"
SEMANTIC_MODULUS = 1 << 256
PARQUET_HEADER_METADATA_KEY = b"overture.address_pack_header"
PARQUET_SUMMARY_METADATA_KEY = b"overture.address_summary_header"
DUPLICATE_ID_POLICY = "preserve-source-multiplicity-v1"
PARQUET_LAYOUT_BINDING_KIND = "parquet-footer-layout-binding-v1"
SPILL_MAGIC = b"OAV2SP1\0"
SPILL_FORMAT = 1
FRAGMENT_CAP_MIN_ROWS = 4_096
MAX_COUNTRY_TAIL_FRAGMENTS = 512
MAX_FRAGMENT_MANIFEST_ENTRIES = 4_096
MAX_OPEN_SPILLS = 128
PARQUET_ROW_GROUP_ROWS = 64_000
DEFAULT_PACK_ROWS = 1_000_000
SOURCE_INVENTORY_SCHEMA = "overture-global-source-inventory-v1"
SORT_ORDER = (
    "country/maximum_hash_bucket/normalized_lookup_key_8/id/"
    "source_object_index/source_row_group/source_row_index"
)
REQUIRED_COLUMNS = {
    "id",
    "street",
    "number",
    "unit",
    "postcode",
    "postal_city",
    "address_levels",
    "country",
    "geometry",
    "source_object_index",
    "source_row_group",
    "source_row_index",
}
SCHEMA_FINGERPRINT_METADATA_KEY = b"overture.schema_fingerprint_sha256"


class SemanticAccumulator:
    """Composable, order-independent binding of canonical address records."""

    def __init__(self) -> None:
        self.records = 0
        self.accumulator_a = 0
        self.accumulator_b = 0

    def add(self, payload: bytes) -> None:
        frame = len(payload).to_bytes(8, "big") + payload
        self.accumulator_a = (
            self.accumulator_a
            + int.from_bytes(
                hashlib.sha256(SEMANTIC_BINDING_DOMAIN_A + frame).digest()
            )
        ) % SEMANTIC_MODULUS
        self.accumulator_b = (
            self.accumulator_b
            + int.from_bytes(
                hashlib.sha256(SEMANTIC_BINDING_DOMAIN_B + frame).digest()
            )
        ) % SEMANTIC_MODULUS
        self.records += 1

    def combine(self, binding: dict[str, Any]) -> None:
        validate_semantic_binding(binding)
        self.records += binding["records"]
        self.accumulator_a = (
            self.accumulator_a + int(binding["accumulator_a"], 16)
        ) % SEMANTIC_MODULUS
        self.accumulator_b = (
            self.accumulator_b + int(binding["accumulator_b"], 16)
        ) % SEMANTIC_MODULUS

    def finish(self) -> dict[str, Any]:
        return {
            "schema": SEMANTIC_BINDING_SCHEMA,
            "records": self.records,
            "accumulator_a": f"{self.accumulator_a:064x}",
            "accumulator_b": f"{self.accumulator_b:064x}",
        }


def validate_semantic_binding(
    binding: Any, *, expected_records: int | None = None
) -> dict[str, Any]:
    if (
        not isinstance(binding, dict)
        or set(binding) != {"schema", "records", "accumulator_a", "accumulator_b"}
        or binding.get("schema") != SEMANTIC_BINDING_SCHEMA
        or type(binding.get("records")) is not int
        or binding["records"] < 0
    ):
        raise ValueError("address semantic binding is invalid")
    require_sha256(binding.get("accumulator_a"), "semantic accumulator A")
    require_sha256(binding.get("accumulator_b"), "semantic accumulator B")
    if expected_records is not None and binding["records"] != expected_records:
        raise ValueError("address semantic binding record count differs")
    return binding


def json_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def write_json(path: Path, value: Any) -> None:
    payload = json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(payload)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def require_sha256(value: Any, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


def validate_source(metadata: dict[str, Any], expected_release: str | None) -> None:
    release = metadata["release"]
    inventory = metadata["source_inventory"]
    if metadata["family"] != "addresses":
        raise ValueError("projected input family must be addresses")
    if expected_release is not None and release != expected_release:
        raise ValueError("projected input release differs from the requested release")
    if (
        not isinstance(inventory, dict)
        or inventory.get("schema") != SOURCE_INVENTORY_SCHEMA
        or inventory.get("release") != release
        or inventory.get("family") != "addresses"
        or not isinstance(inventory.get("objects"), list)
        or not inventory["objects"]
    ):
        raise ValueError("projected input has an invalid global source inventory")
    seen: set[str] = set()
    for source in inventory["objects"]:
        if not isinstance(source, dict):
            raise ValueError("source inventory objects must be JSON objects")
        uri = source.get("uri")
        if not isinstance(uri, str) or not uri.startswith("s3://") or uri in seen:
            raise ValueError("source inventory object URI is invalid or duplicated")
        if not isinstance(source.get("etag"), str) or not source["etag"]:
            raise ValueError("source inventory object ETag is required")
        for field in ("bytes", "records", "row_groups"):
            if type(source.get(field)) is not int or source[field] <= 0:
                raise ValueError(f"source inventory object {field} must be positive")
        if "sha256" in source:
            require_sha256(source["sha256"], "source inventory object sha256")
        seen.add(uri)


def validate_projected_task_identity(
    parquet_metadata: dict[bytes, bytes],
    *,
    execution_bucket: str,
    expected_inventory_sha256: str,
    expected_task_index: int,
    expected_task_digest_sha256: str,
    expected_task_source_digest_sha256: str,
) -> dict[str, Any]:
    for value, field in (
        (expected_inventory_sha256, "expected_inventory_sha256"),
        (expected_task_digest_sha256, "expected_task_digest_sha256"),
        (
            expected_task_source_digest_sha256,
            "expected_task_source_digest_sha256",
        ),
    ):
        require_sha256(value, field)
    if type(expected_task_index) is not int or expected_task_index < 0:
        raise ValueError("expected_task_index must be a non-negative integer")
    canonical_bucket = address_inventory.address_execution_bucket(expected_task_index)
    if execution_bucket != canonical_bucket:
        raise ValueError("execution bucket does not identify the expected task")
    expected = {
        address_inventory.INVENTORY_METADATA_KEY: expected_inventory_sha256,
        address_inventory.TASK_INDEX_METADATA_KEY: str(expected_task_index),
        address_inventory.TASK_DIGEST_METADATA_KEY: expected_task_digest_sha256,
        address_inventory.TASK_SOURCE_DIGEST_METADATA_KEY: (
            expected_task_source_digest_sha256
        ),
        address_inventory.EXECUTION_BUCKET_METADATA_KEY: execution_bucket,
    }
    for key, value in expected.items():
        raw = parquet_metadata.get(key)
        if raw is None:
            raise ValueError("projected input is missing its exact task identity")
        try:
            actual = raw.decode()
        except UnicodeDecodeError as exc:
            raise ValueError("projected task identity is not UTF-8") from exc
        if actual != value:
            raise ValueError("projected input task identity differs from the request")
    return {
        "inventory_sha256": expected_inventory_sha256,
        "task_index": expected_task_index,
        "task_digest_sha256": expected_task_digest_sha256,
        "task_source_digest_sha256": expected_task_source_digest_sha256,
        "execution_bucket": execution_bucket,
    }


def fragment_header(
    *,
    source_inventory_sha256: str,
    schema_fingerprint_sha256: str,
    release: str,
    execution_bucket: str,
    task_identity: dict[str, Any],
    ownership: dict[str, Any],
    index: int,
    records: int,
) -> dict[str, Any]:
    return {
        "format": "overture-global-v2-address-data-pack-v1",
        "container": "parquet",
        "wire_encoding": WIRE_ENCODING,
        "duplicate_id_policy": DUPLICATE_ID_POLICY,
        "source_inventory_sha256": source_inventory_sha256,
        "schema_fingerprint_sha256": schema_fingerprint_sha256,
        "overture_release": release,
        "records": records,
        "fragment_index": index,
        "execution_bucket": execution_bucket,
        "execution_bucket_kind": EXECUTION_BUCKET_KIND,
        "execution_bucket_is_serving_shard_id": False,
        "address_task_identity": task_identity,
        "intermediate_ownership": ownership,
        "sorted_by": SORT_ORDER,
    }


def shuffle_schema():
    """Return the frozen typed shuffle schema without provenance metadata."""

    import pyarrow as pa

    fields = [
        pa.field("country", pa.string(), nullable=False),
        pa.field("maximum_bucket", pa.uint32(), nullable=False),
        pa.field("route_hash", pa.uint64(), nullable=False),
    ]
    fields.extend(
        pa.field(f"normalized_key_{index}", pa.string(), nullable=False)
        for index in range(8)
    )
    fields.extend(
        [
            pa.field("feature_id", pa.binary(16), nullable=False),
            pa.field("longitude_e7", pa.int32(), nullable=False),
            pa.field("latitude_e7", pa.int32(), nullable=False),
            pa.field("source_object_index", pa.uint32(), nullable=False),
            pa.field("source_row_group", pa.uint32(), nullable=False),
            pa.field("source_row_index", pa.uint64(), nullable=False),
            pa.field("display_country", pa.string(), nullable=False),
            pa.field("postal_city", pa.string(), nullable=False),
            pa.field("postcode", pa.string(), nullable=False),
            pa.field("street", pa.string(), nullable=False),
            pa.field("number", pa.string(), nullable=False),
            pa.field("unit", pa.string(), nullable=False),
            pa.field(
                "address_levels",
                pa.list_(pa.field("item", pa.string(), nullable=False)),
                nullable=False,
            ),
        ]
    )
    return pa.schema(fields)


def summary_schema():
    """Return the compact associative per-maximum-bucket summary schema."""

    import pyarrow as pa

    return pa.schema(
        [
            pa.field("country", pa.string(), nullable=False),
            pa.field("maximum_bucket", pa.uint32(), nullable=False),
            pa.field("records", pa.uint64(), nullable=False),
            pa.field("semantic_sum_a", pa.binary(32), nullable=False),
            pa.field("semantic_sum_b", pa.binary(32), nullable=False),
        ]
    )


def parquet_layout_binding(parquet: Any) -> dict[str, Any]:
    """Bind the immutable Parquet footer layout used for selective reads."""

    metadata = parquet.metadata
    groups = []
    for index in range(metadata.num_row_groups):
        group = metadata.row_group(index)
        groups.append(
            {
                "index": index,
                "records": group.num_rows,
                "total_byte_size": group.total_byte_size,
                "compressed_column_bytes": sum(
                    group.column(column).total_compressed_size
                    for column in range(group.num_columns)
                ),
                "columns": [
                    {
                        "path": group.column(column).path_in_schema,
                        "compressed_bytes": group.column(
                            column
                        ).total_compressed_size,
                        "uncompressed_bytes": group.column(
                            column
                        ).total_uncompressed_size,
                        "data_page_offset": group.column(column).data_page_offset,
                        "dictionary_page_offset": group.column(
                            column
                        ).dictionary_page_offset,
                    }
                    for column in range(group.num_columns)
                ],
            }
        )
    value = {
        "created_by": metadata.created_by,
        "format_version": metadata.format_version,
        "serialized_size": metadata.serialized_size,
        "records": metadata.num_rows,
        "row_groups": metadata.num_row_groups,
        "columns": metadata.num_columns,
        "schema_sha256": hashlib.sha256(
            str(shuffle_schema()).encode()
        ).hexdigest(),
        "groups": groups,
    }
    # The fetch adapter's canonical proof framing is newline-terminated.
    binding_sha256 = hashlib.sha256(canonical_json(value) + b"\n").hexdigest()
    return {
        "kind": PARQUET_LAYOUT_BINDING_KIND,
        "sha256": binding_sha256,
        "serialized_size": metadata.serialized_size,
        "records": metadata.num_rows,
        "row_groups": metadata.num_row_groups,
        "schema_sha256": value["schema_sha256"],
    }


def validate_parquet_layout_binding(
    binding: Any, *, expected_records: int, expected_row_groups: int
) -> dict[str, Any]:
    if (
        not isinstance(binding, dict)
        or set(binding)
        != {
            "kind",
            "sha256",
            "serialized_size",
            "records",
            "row_groups",
            "schema_sha256",
        }
        or binding.get("kind") != PARQUET_LAYOUT_BINDING_KIND
        or type(binding.get("serialized_size")) is not int
        or binding["serialized_size"] <= 0
        or binding.get("records") != expected_records
        or binding.get("row_groups") != expected_row_groups
    ):
        raise ValueError("address Parquet footer/layout binding is invalid")
    require_sha256(binding.get("sha256"), "Parquet footer/layout SHA-256")
    require_sha256(binding.get("schema_sha256"), "Parquet schema SHA-256")
    return binding


def _payload_to_shuffle_row(
    payload: bytes, *, maximum_hash_bits: int
) -> dict[str, Any]:
    record = decode_record(payload)
    if encode_record(record) != payload:
        raise ValueError("address shuffle record is not canonically encoded")
    key = record["key"]
    route_hash = record_hash(key[:8])
    row: dict[str, Any] = {
        "country": key[0],
        "maximum_bucket": hash_bucket(route_hash, maximum_hash_bits),
        "route_hash": route_hash,
        "feature_id": uuid.UUID(record["id"]).bytes,
        "longitude_e7": round(record["lon"] * 10_000_000),
        "latitude_e7": round(record["lat"] * 10_000_000),
        "source_object_index": record["source_object_index"],
        "source_row_group": record["source_row_group"],
        "source_row_index": record["source_row_index"],
        "display_country": record["country"],
        "postal_city": record["postal_city"],
        "postcode": record["postcode"],
        "street": record["street"],
        "number": record["number"],
        "unit": record["unit"],
        "address_levels": record["address_levels"],
    }
    row.update({f"normalized_key_{index}": key[index] for index in range(8)})
    return row


def _shuffle_row_to_payload(
    row: dict[str, Any], *, maximum_hash_bits: int
) -> tuple[tuple[str, ...], bytes]:
    normalized = tuple(row[f"normalized_key_{index}"] for index in range(8))
    feature_id = str(uuid.UUID(bytes=bytes(row["feature_id"])))
    key = normalized + (feature_id,)
    route_hash = record_hash(normalized)
    if (
        row["country"] != normalized[0]
        or row["route_hash"] != route_hash
        or row["maximum_bucket"] != hash_bucket(route_hash, maximum_hash_bits)
    ):
        raise ValueError("typed address shuffle route columns are inconsistent")
    record = {
        "id": feature_id,
        "lon": row["longitude_e7"] / 10_000_000,
        "lat": row["latitude_e7"] / 10_000_000,
        "source_object_index": row["source_object_index"],
        "source_row_group": row["source_row_group"],
        "source_row_index": row["source_row_index"],
        "country": row["display_country"],
        "postal_city": row["postal_city"],
        "postcode": row["postcode"],
        "street": row["street"],
        "number": row["number"],
        "unit": row["unit"],
        "address_levels": row["address_levels"],
    }
    payload = encode_record(record)
    if record["id"] != key[-1] or decode_record(payload)["key"] != key:
        raise ValueError("typed address shuffle row is not canonical")
    return key, payload


def intermediate_ownership(
    country: str,
    minimum_bucket: int,
    maximum_bucket: int,
    maximum_hash_bits: int,
) -> dict[str, Any]:
    if not isinstance(country, str):
        raise ValueError("address intermediate country must be a string")
    validate_country(country)
    if (
        type(maximum_hash_bits) is not int
        or not 1 <= maximum_hash_bits <= MAXIMUM_SUPPORTED_HASH_BITS
        or type(minimum_bucket) is not int
        or type(maximum_bucket) is not int
        or not 0 <= minimum_bucket <= maximum_bucket < 1 << maximum_hash_bits
    ):
        raise ValueError("address intermediate hash bucket is outside hard bounds")
    hash_start = minimum_bucket << (64 - maximum_hash_bits)
    hash_end = ((maximum_bucket + 1) << (64 - maximum_hash_bits)) - 1
    return {
        "kind": FRAGMENT_OWNERSHIP_KIND,
        "country": country,
        "maximum_hash_bits": maximum_hash_bits,
        "minimum_bucket": minimum_bucket,
        "maximum_bucket": maximum_bucket,
        "hash_start": hash_start,
        "hash_end": hash_end,
        "is_serving_shard_id": False,
    }


def fragment_count_cap(max_rows: int, max_fragment_rows: int) -> int:
    target_rows = max(1, min(FRAGMENT_CAP_MIN_ROWS, max_fragment_rows))
    derived = (max_rows + target_rows - 1) // target_rows
    return min(
        MAX_FRAGMENT_MANIFEST_ENTRIES,
        derived + MAX_COUNTRY_TAIL_FRAGMENTS,
    )


def record_ownership(key: tuple[str, ...], maximum_hash_bits: int) -> tuple[str, int]:
    return key[0], hash_bucket(record_hash(key), maximum_hash_bits)


def record_total_sort_identity(
    key: tuple[str, ...], payload: bytes, maximum_hash_bits: int
) -> tuple[str, int, tuple[str, ...], int, int, int]:
    """Return the canonical total order, including duplicate source topology."""

    record = decode_record(payload)
    if record["key"] != key or encode_record(record) != payload:
        raise ValueError("address record differs from its canonical sort key")
    country, bucket = record_ownership(key, maximum_hash_bits)
    return (
        country,
        bucket,
        key,
        record["source_object_index"],
        record["source_row_group"],
        record["source_row_index"],
    )


def write_ownership_spill(
    path: Path,
    records: list[tuple[tuple[str, ...], bytes]],
    *,
    maximum_hash_bits: int,
) -> None:
    records.sort(
        key=lambda item: record_total_sort_identity(
            item[0], item[1], maximum_hash_bits
        )
    )
    with path.open("wb") as output:
        write_envelope(
            output,
            SPILL_MAGIC,
            {
                "format": SPILL_FORMAT,
                "maximum_hash_bits": maximum_hash_bits,
                "records": len(records),
            },
        )
        for _, payload in records:
            output.write(struct.pack("<I", len(payload)))
            output.write(payload)


class OwnershipSpillReader:
    def __init__(self, path: Path, *, maximum_hash_bits: int):
        self.path = path
        self.file = path.open("rb")
        self.header = read_envelope(self.file, SPILL_MAGIC)
        if (
            self.header.get("format") != SPILL_FORMAT
            or self.header.get("maximum_hash_bits") != maximum_hash_bits
            or type(self.header.get("records")) is not int
            or self.header["records"] < 0
        ):
            raise ValueError("invalid address ownership spill header")
        self.maximum_hash_bits = maximum_hash_bits
        self.file_size = path.stat().st_size
        self.read_count = 0
        self.previous: tuple[str, int, tuple[str, ...], int, int, int] | None = None

    def next(self) -> tuple[str, int, tuple[str, ...], bytes] | None:
        encoded_length = self.file.read(4)
        if not encoded_length:
            if self.read_count != self.header["records"]:
                raise ValueError("address ownership spill record count differs")
            return None
        if len(encoded_length) != 4:
            raise ValueError("truncated address ownership spill record length")
        length = struct.unpack("<I", encoded_length)[0]
        if length > MAX_RECORD_BYTES or self.file.tell() + length > self.file_size:
            raise ValueError("address ownership spill record exceeds bounds")
        payload = self.file.read(length)
        if len(payload) != length:
            raise ValueError("truncated address ownership spill record")
        key = decode_record(payload)["key"]
        country, bucket = record_ownership(key, self.maximum_hash_bits)
        identity = record_total_sort_identity(key, payload, self.maximum_hash_bits)
        if self.previous is not None and identity < self.previous:
            raise ValueError("address ownership spill is not sorted")
        self.previous = identity
        self.read_count += 1
        return country, bucket, key, payload

    def close(self) -> None:
        self.file.close()


class CountryFragmentReader:
    """Stream a typed Parquet pack ordered by `(max-bucket, lookup-key)`."""

    def __init__(
        self,
        path: Path,
        *,
        maximum_hash_bits: int,
        row_groups: list[int] | None = None,
    ):
        import pyarrow.parquet as pq

        self.path = path
        self.parquet = pq.ParquetFile(path)
        raw_header = (self.parquet.schema_arrow.metadata or {}).get(
            PARQUET_HEADER_METADATA_KEY
        )
        if raw_header is None:
            raise ValueError("typed address pack omits its provenance header")
        try:
            self.header = json.loads(raw_header)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("typed address pack header is not canonical JSON") from exc
        if (
            self.header.get("format")
            != "overture-global-v2-address-data-pack-v1"
            or self.header.get("container") != "parquet"
            or self.header.get("wire_encoding") != WIRE_ENCODING
            or type(self.header.get("records")) is not int
            or self.header["records"] < 0
            or self.parquet.metadata.num_rows != self.header["records"]
            or not self.parquet.schema_arrow.remove_metadata().equals(shuffle_schema())
        ):
            raise ValueError("invalid typed address pack header or schema")
        self.maximum_hash_bits = maximum_hash_bits
        if row_groups is None:
            row_groups = list(range(self.parquet.metadata.num_row_groups))
        if (
            not row_groups
            or row_groups != sorted(set(row_groups))
            or any(
                type(index) is not int
                or not 0 <= index < self.parquet.metadata.num_row_groups
                for index in row_groups
            )
        ):
            raise ValueError("typed address pack row-group selection is invalid")
        self.row_groups = row_groups
        self.expected_records = sum(
            self.parquet.metadata.row_group(index).num_rows for index in row_groups
        )
        self.read_count = 0
        self.previous: tuple[str, int, tuple[str, ...], int, int, int] | None = None
        self._semantic = SemanticAccumulator()
        self._batches = iter(
            self.parquet.iter_batches(batch_size=8_192, row_groups=row_groups)
        )
        self._rows: list[dict[str, Any]] = []
        self._position = 0

    def next(self) -> tuple[tuple[str, ...], bytes] | None:
        while self._position >= len(self._rows):
            try:
                self._rows = next(self._batches).to_pylist()
            except StopIteration:
                if self.read_count != self.expected_records:
                    raise ValueError("typed address pack record count differs")
                return None
            self._position = 0
        row = self._rows[self._position]
        self._position += 1
        key, payload = _shuffle_row_to_payload(
            row, maximum_hash_bits=self.maximum_hash_bits
        )
        if len(payload) > MAX_RECORD_BYTES:
            raise ValueError("typed address pack record exceeds bounds")
        identity = record_total_sort_identity(key, payload, self.maximum_hash_bits)
        if self.previous is not None and identity < self.previous:
            raise ValueError("typed address pack is not bucket/key sorted")
        self.previous = identity
        self._semantic.add(payload)
        self.read_count += 1
        return key, payload

    def close(self) -> None:
        self._rows = []

    def semantic_binding(self) -> dict[str, Any]:
        if self.read_count != self.expected_records:
            raise ValueError("typed address pack row-group scan is incomplete")
        return self._semantic.finish()


def fragment_size(header: dict[str, Any], records: Iterable[bytes]) -> int:
    return (
        len(FRAGMENT_MAGIC)
        + 4
        + len(canonical_json(header))
        + sum(4 + len(payload) for payload in records)
    )


def write_content_fragment(
    output_dir: Path,
    records: list[tuple[tuple[str, ...], bytes]],
    *,
    source_inventory_sha256: str,
    schema_fingerprint_sha256: str,
    release: str,
    execution_bucket: str,
    task_identity: dict[str, Any],
    ownership: dict[str, Any],
    index: int,
    max_fragment_bytes: int,
) -> dict[str, Any]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    header = fragment_header(
        source_inventory_sha256=source_inventory_sha256,
        schema_fingerprint_sha256=schema_fingerprint_sha256,
        release=release,
        execution_bucket=execution_bucket,
        task_identity=task_identity,
        ownership=ownership,
        index=index,
        records=len(records),
    )
    uncompressed_upper_bound = fragment_size(
        header, (payload for _, payload in records)
    )
    if uncompressed_upper_bound > max_fragment_bytes:
        raise ValueError("one address map fragment exceeds its hard byte cap")

    content_dir = output_dir / "data-packs" / "sha256"
    content_dir.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".address-map-", suffix=".parquet", dir=content_dir, delete=False
        ) as output:
            temporary = Path(output.name)
        schema = shuffle_schema().with_metadata(
            {PARQUET_HEADER_METADATA_KEY: canonical_json(header)}
        )
        writer = pq.ParquetWriter(
            temporary,
            schema,
            compression="zstd",
            compression_level=6,
            use_dictionary=True,
            write_statistics=True,
            data_page_version="1.0",
            version="2.6",
        )
        row_groups = []
        previous_identity = None
        try:
            for row_group_index, offset in enumerate(
                range(0, len(records), PARQUET_ROW_GROUP_ROWS)
            ):
                group = records[offset : offset + PARQUET_ROW_GROUP_ROWS]
                if not group:
                    raise ValueError("typed address row group is empty")
                accumulator = SemanticAccumulator()
                rows = []
                for key, payload in group:
                    identity = record_total_sort_identity(
                        key, payload, ownership["maximum_hash_bits"]
                    )
                    if previous_identity is not None and identity < previous_identity:
                        raise ValueError("typed address pack input is not totally sorted")
                    previous_identity = identity
                    accumulator.add(payload)
                    rows.append(
                        _payload_to_shuffle_row(
                            payload,
                            maximum_hash_bits=ownership["maximum_hash_bits"],
                        )
                    )
                table = pa.Table.from_pylist(rows, schema=schema)
                writer.write_table(table, row_group_size=len(group))
                first_owner = record_ownership(
                    group[0][0], ownership["maximum_hash_bits"]
                )
                last_owner = record_ownership(
                    group[-1][0], ownership["maximum_hash_bits"]
                )
                if (
                    first_owner[0] != last_owner[0]
                    or first_owner[0] != ownership["country"]
                ):
                    raise ValueError("typed address row group crosses countries")
                row_groups.append(
                    {
                        "index": row_group_index,
                        "records": len(group),
                        "intermediate_ownership": intermediate_ownership(
                            first_owner[0],
                            first_owner[1],
                            last_owner[1],
                            ownership["maximum_hash_bits"],
                        ),
                        "semantic_binding": accumulator.finish(),
                        "integrity": {
                            "kind": "canonical-row-multiset-binding-v1",
                            "order_verified_by_consumer": True,
                        },
                    }
                )
        finally:
            writer.close()
        expected_bytes = temporary.stat().st_size
        if expected_bytes > max_fragment_bytes:
            raise ValueError("one typed address data pack exceeds its hard byte cap")
        digest = sha256_file(temporary)
        parquet = pq.ParquetFile(temporary)
        if parquet.metadata.num_row_groups != len(row_groups):
            raise ValueError("typed address row-group layout does not reconcile")
        offset = 0
        for row_group_index, row_group in enumerate(row_groups):
            metadata = parquet.metadata.row_group(row_group_index)
            group_records = metadata.num_rows
            if row_group["records"] != group_records or group_records <= 0:
                raise ValueError("typed address row-group records do not reconcile")
            row_group["compressed_column_bytes"] = sum(
                metadata.column(index).total_compressed_size
                for index in range(metadata.num_columns)
            )
            offset += group_records
        if offset != len(records):
            raise ValueError("typed address row-group totals do not reconcile")
        bucket_width = (ownership["maximum_hash_bits"] + 3) // 4
        relative_path = (
            Path("data-packs")
            / f"country={ownership['country']}"
            / f"maxbits={ownership['maximum_hash_bits']}"
            / (
                f"buckets={ownership['minimum_bucket']:0{bucket_width}x}-"
                f"{ownership['maximum_bucket']:0{bucket_width}x}"
            )
            / "sha256"
            / f"{digest}.parquet"
        )
        final_path = output_dir / relative_path
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if final_path.exists():
            if (
                final_path.stat().st_size != expected_bytes
                or sha256_file(final_path) != digest
            ):
                raise ValueError("existing content-addressed address fragment differs")
            temporary.unlink()
        else:
            os.replace(temporary, final_path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

    return {
        "index": index,
        "sha256": digest,
        "bytes": expected_bytes,
        "records": len(records),
        "row_groups": row_groups,
        "parquet_layout_binding": parquet_layout_binding(parquet),
        "intermediate_ownership": ownership,
        "relative_path": relative_path.as_posix(),
        "object_key": (
            f"map/address-data-packs/{relative_path.relative_to('data-packs').as_posix()}"
        ),
    }


def build_owned_fragments(
    output_dir: Path,
    spill_paths: list[Path],
    *,
    source_inventory_sha256: str,
    schema_fingerprint_sha256: str,
    release: str,
    execution_bucket: str,
    task_identity: dict[str, Any],
    maximum_hash_bits: int,
    max_fragment_rows: int,
    max_fragment_bytes: int,
    max_fragments: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Merge spills and produce packs, bucket bindings, and fanout in one pass."""

    readers = [
        OwnershipSpillReader(path, maximum_hash_bits=maximum_hash_bits)
        for path in spill_paths
    ]
    fragments: list[dict[str, Any]] = []
    heap: list[
        tuple[tuple[str, int, tuple[str, ...], int, int, int], int, bytes]
    ] = []
    current_country: str | None = None
    buffered: list[tuple[tuple[str, ...], bytes]] = []
    buffered_body_bytes = 0
    buffered_minimum_bucket: int | None = None
    buffered_maximum_bucket: int | None = None
    summaries: list[dict[str, Any]] = []
    summary_owner: tuple[str, int] | None = None
    summary_accumulator = SemanticAccumulator()
    current_prefix: tuple[str, ...] | None = None
    current_prefix_count = 0
    maximum_prefix: tuple[str, ...] | None = None
    maximum_prefix_count = 0

    def finish_summary() -> None:
        nonlocal summary_accumulator
        if summary_owner is None:
            return
        binding = summary_accumulator.finish()
        if binding["records"] <= 0:
            raise ValueError("address bucket summary is empty")
        summaries.append(
            {
                "country": summary_owner[0],
                "bucket": summary_owner[1],
                "records": binding["records"],
                "accumulator_a": binding["accumulator_a"],
                "accumulator_b": binding["accumulator_b"],
            }
        )
        summary_accumulator = SemanticAccumulator()

    def finish_prefix() -> None:
        nonlocal maximum_prefix, maximum_prefix_count
        if current_prefix is not None and current_prefix_count > maximum_prefix_count:
            maximum_prefix = current_prefix
            maximum_prefix_count = current_prefix_count

    def flush() -> None:
        nonlocal buffered, buffered_body_bytes
        nonlocal buffered_minimum_bucket, buffered_maximum_bucket
        if not buffered or current_country is None:
            return
        if len(fragments) >= max_fragments:
            raise ValueError("address map fragment count exceeds its hard cap")
        assert buffered_minimum_bucket is not None
        assert buffered_maximum_bucket is not None
        fragments.append(
            write_content_fragment(
                output_dir,
                buffered,
                source_inventory_sha256=source_inventory_sha256,
                schema_fingerprint_sha256=schema_fingerprint_sha256,
                release=release,
                execution_bucket=execution_bucket,
                task_identity=task_identity,
                ownership=intermediate_ownership(
                    current_country,
                    buffered_minimum_bucket,
                    buffered_maximum_bucket,
                    maximum_hash_bits,
                ),
                index=len(fragments),
                max_fragment_bytes=max_fragment_bytes,
            )
        )
        buffered = []
        buffered_body_bytes = 0
        buffered_minimum_bucket = None
        buffered_maximum_bucket = None

    try:
        for reader_index, reader in enumerate(readers):
            item = reader.next()
            if item is not None:
                country, bucket, key, payload = item
                identity = record_total_sort_identity(
                    key, payload, maximum_hash_bits
                )
                heapq.heappush(heap, (identity, reader_index, payload))
        while heap:
            identity, reader_index, payload = heapq.heappop(heap)
            country, bucket, key = identity[:3]
            owner = (country, bucket)
            if summary_owner != owner:
                finish_summary()
                summary_owner = owner
            summary_accumulator.add(payload)
            prefix = key[:8]
            if prefix != current_prefix:
                finish_prefix()
                current_prefix = prefix
                current_prefix_count = 0
            current_prefix_count += 1
            if current_country != country:
                flush()
                current_country = country

            ownership = intermediate_ownership(
                country,
                (
                    buffered_minimum_bucket
                    if buffered_minimum_bucket is not None
                    else bucket
                ),
                bucket,
                maximum_hash_bits,
            )
            candidate_header = fragment_header(
                source_inventory_sha256=source_inventory_sha256,
                schema_fingerprint_sha256=schema_fingerprint_sha256,
                release=release,
                execution_bucket=execution_bucket,
                task_identity=task_identity,
                ownership=ownership,
                index=len(fragments),
                records=len(buffered) + 1,
            )
            candidate_bytes = (
                len(FRAGMENT_MAGIC)
                + 4
                + len(canonical_json(candidate_header))
                + buffered_body_bytes
                + 4
                + len(payload)
            )
            if buffered and (
                len(buffered) >= max_fragment_rows
                or candidate_bytes > max_fragment_bytes
            ):
                flush()
            ownership = intermediate_ownership(
                country, bucket, bucket, maximum_hash_bits
            )
            buffered.append((key, payload))
            buffered_body_bytes += 4 + len(payload)
            if buffered_minimum_bucket is None:
                buffered_minimum_bucket = bucket
            buffered_maximum_bucket = bucket
            if len(buffered) == 1:
                singleton_header = fragment_header(
                    source_inventory_sha256=source_inventory_sha256,
                    schema_fingerprint_sha256=schema_fingerprint_sha256,
                    release=release,
                    execution_bucket=execution_bucket,
                    task_identity=task_identity,
                    ownership=ownership,
                    index=len(fragments),
                    records=1,
                )
                if (
                    len(FRAGMENT_MAGIC)
                    + 4
                    + len(canonical_json(singleton_header))
                    + buffered_body_bytes
                    > max_fragment_bytes
                ):
                    raise ValueError(
                        "one retained address record exceeds the fragment byte cap"
                    )

            item = readers[reader_index].next()
            if item is not None:
                _, _, next_key, next_payload = item
                next_identity = record_total_sort_identity(
                    next_key, next_payload, maximum_hash_bits
                )
                heapq.heappush(
                    heap,
                    (next_identity, reader_index, next_payload),
                )
        flush()
        finish_summary()
        finish_prefix()
        return fragments, summaries, {
            "scope": "execution_bucket",
            "maximum_candidates": maximum_prefix_count,
            "normalized_lookup_key": (
                list(maximum_prefix) if maximum_prefix is not None else None
            ),
        }
    finally:
        for reader in readers:
            reader.close()


def summarize_fragments(
    output_dir: Path,
    fragments: list[dict[str, Any]],
    *,
    maximum_hash_bits: int,
    execution_bucket: str,
    source_inventory_sha256: str,
    schema_fingerprint_sha256: str,
    task_identity: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Sequentially verify bucket-ordered fragments and compute exact summaries."""

    current_prefix: tuple[str, ...] | None = None
    current_count = 0
    maximum_prefix: tuple[str, ...] | None = None
    maximum_count = 0
    total_records = 0
    bucket_counts: Counter[tuple[str, int]] = Counter()
    previous_sort: tuple[str, int, tuple[str, ...], int, int, int] | None = None

    def finish_group() -> None:
        nonlocal maximum_prefix, maximum_count
        if current_prefix is not None and current_count > maximum_count:
            maximum_prefix = current_prefix
            maximum_count = current_count

    for expected_index, fragment in enumerate(fragments):
        path = output_dir / fragment["relative_path"]
        if (
            fragment.get("index") != expected_index
            or path.stat().st_size != fragment["bytes"]
            or sha256_file(path) != fragment["sha256"]
        ):
            raise ValueError("address fragment identity changed before completion")
        ownership = fragment["intermediate_ownership"]
        country = ownership.get("country")
        minimum_bucket = ownership.get("minimum_bucket")
        maximum_bucket = ownership.get("maximum_bucket")
        if (
            ownership.get("kind") != FRAGMENT_OWNERSHIP_KIND
            or ownership.get("is_serving_shard_id") is not False
            or ownership.get("maximum_hash_bits") != maximum_hash_bits
            or ownership
            != intermediate_ownership(
                country, minimum_bucket, maximum_bucket, maximum_hash_bits
            )
        ):
            raise ValueError("address fragment intermediate ownership is invalid")

        reader = CountryFragmentReader(path, maximum_hash_bits=maximum_hash_bits)
        observed_minimum_bucket: int | None = None
        observed_maximum_bucket: int | None = None
        try:
            if (
                reader.header.get("records") != fragment["records"]
                or reader.header.get("fragment_index") != fragment["index"]
                or reader.header.get("execution_bucket") != execution_bucket
                or reader.header.get("execution_bucket_is_serving_shard_id")
                is not False
                or reader.header.get("source_inventory_sha256")
                != source_inventory_sha256
                or reader.header.get("schema_fingerprint_sha256")
                != schema_fingerprint_sha256
                or reader.header.get("address_task_identity") != task_identity
                or reader.header.get("intermediate_ownership") != ownership
            ):
                raise ValueError("address fragment header differs from its manifest")
            while True:
                item = reader.next()
                if item is None:
                    break
                key, payload = item
                prefix = key[:8]
                actual_owner = record_ownership(prefix, maximum_hash_bits)
                sort_identity = record_total_sort_identity(
                    key, payload, maximum_hash_bits
                )
                if (
                    actual_owner[0] != country
                    or not minimum_bucket <= actual_owner[1] <= maximum_bucket
                    or (previous_sort is not None and sort_identity < previous_sort)
                ):
                    raise ValueError(
                        "address fragment record differs from intermediate ownership"
                    )
                if prefix != current_prefix:
                    finish_group()
                    current_prefix = prefix
                    current_count = 0
                current_count += 1
                total_records += 1
                bucket_counts[actual_owner] += 1
                observed_minimum_bucket = (
                    actual_owner[1]
                    if observed_minimum_bucket is None
                    else min(observed_minimum_bucket, actual_owner[1])
                )
                observed_maximum_bucket = (
                    actual_owner[1]
                    if observed_maximum_bucket is None
                    else max(observed_maximum_bucket, actual_owner[1])
                )
                previous_sort = sort_identity
        finally:
            reader.close()
        if (
            observed_minimum_bucket != minimum_bucket
            or observed_maximum_bucket != maximum_bucket
        ):
            raise ValueError("address fragment bucket range is not exact")

    finish_group()
    counts = [
        {"country": country, "bucket": bucket, "rows": rows}
        for (country, bucket), rows in sorted(bucket_counts.items())
    ]
    if total_records != sum(
        item["records"] for item in fragments
    ) or total_records != sum(item["rows"] for item in counts):
        raise ValueError("address count/fanout scan does not reconcile")
    return counts, {
        "scope": "execution_bucket",
        "maximum_candidates": maximum_count,
        "normalized_lookup_key": list(maximum_prefix) if maximum_prefix else None,
    }


def write_semantic_summary(
    output_dir: Path,
    rows: list[dict[str, Any]],
    *,
    release: str,
    source_inventory_sha256: str,
    schema_fingerprint_sha256: str,
    task_identity: dict[str, Any],
    maximum_hash_bits: int,
) -> dict[str, Any]:
    """Write the content-addressed associative planner input."""

    import pyarrow as pa
    import pyarrow.parquet as pq

    total_records = sum(item["records"] for item in rows)
    header = {
        "schema": SUMMARY_SCHEMA,
        "overture_release": release,
        "source_inventory_sha256": source_inventory_sha256,
        "schema_fingerprint_sha256": schema_fingerprint_sha256,
        "address_task_identity": task_identity,
        "maximum_hash_bits": maximum_hash_bits,
        "entries": len(rows),
        "records": total_records,
        "semantic_binding_schema": SEMANTIC_BINDING_SCHEMA,
    }
    summary_rows = [
        {
            "country": item["country"],
            "maximum_bucket": item["bucket"],
            "records": item["records"],
            "semantic_sum_a": bytes.fromhex(item["accumulator_a"]),
            "semantic_sum_b": bytes.fromhex(item["accumulator_b"]),
        }
        for item in rows
    ]
    schema = summary_schema().with_metadata(
        {PARQUET_SUMMARY_METADATA_KEY: canonical_json(header)}
    )
    directory = output_dir / "summaries" / "sha256"
    directory.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".address-summary-",
            suffix=".parquet",
            dir=directory,
            delete=False,
        ) as output:
            temporary = Path(output.name)
        table = pa.Table.from_pylist(summary_rows, schema=schema)
        pq.write_table(
            table,
            temporary,
            compression="zstd",
            compression_level=6,
            use_dictionary=["country"],
            write_statistics=True,
            data_page_version="1.0",
            version="2.6",
            row_group_size=max(1, min(65_536, len(rows))),
        )
        digest = sha256_file(temporary)
        relative = Path("summaries") / "sha256" / f"{digest}.parquet"
        final = output_dir / relative
        if final.exists():
            if final.stat().st_size != temporary.stat().st_size or sha256_file(final) != digest:
                raise ValueError("existing content-addressed address summary differs")
            temporary.unlink()
        else:
            os.replace(temporary, final)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    identity = {
        "schema": SUMMARY_SCHEMA,
        "relative_path": relative.as_posix(),
        "object_key": f"map/address-summaries/sha256/{digest}.parquet",
        "sha256": digest,
        "bytes": final.stat().st_size,
        "entries": len(rows),
        "records": total_records,
    }
    read_semantic_summary(final, expected_identity=identity, expected_header=header)
    return identity


def read_semantic_summary(
    path: Path,
    *,
    expected_identity: dict[str, Any] | None = None,
    expected_header: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate and stream a compact summary without opening any data pack."""

    import pyarrow.parquet as pq

    if expected_identity is not None and (
        not path.is_file()
        or path.stat().st_size != expected_identity.get("bytes")
        or sha256_file(path) != expected_identity.get("sha256")
    ):
        raise ValueError("address semantic summary identity differs")
    parquet = pq.ParquetFile(path)
    raw = (parquet.schema_arrow.metadata or {}).get(PARQUET_SUMMARY_METADATA_KEY)
    if raw is None:
        raise ValueError("address semantic summary omits its header")
    try:
        header = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("address semantic summary header is invalid") from exc
    if (
        header.get("schema") != SUMMARY_SCHEMA
        or header.get("semantic_binding_schema") != SEMANTIC_BINDING_SCHEMA
        or type(header.get("maximum_hash_bits")) is not int
        or not 1 <= header["maximum_hash_bits"] <= MAXIMUM_SUPPORTED_HASH_BITS
        or type(header.get("entries")) is not int
        or header["entries"] < 0
        or type(header.get("records")) is not int
        or header["records"] < 0
        or not parquet.schema_arrow.remove_metadata().equals(summary_schema())
        or parquet.metadata.num_rows != header.get("entries")
        or (
            expected_identity is not None
            and (
                expected_identity.get("schema") != SUMMARY_SCHEMA
                or expected_identity.get("entries") != header["entries"]
                or expected_identity.get("records") != header["records"]
            )
        )
        or (expected_header is not None and header != expected_header)
    ):
        raise ValueError("address semantic summary contract differs")
    result: list[dict[str, Any]] = []
    previous: tuple[str, int] | None = None
    total = 0
    for batch in parquet.iter_batches(batch_size=65_536):
        for row in batch.to_pylist():
            identity = (row["country"], row["maximum_bucket"])
            address_inventory_country = row["country"]
            validate_country(address_inventory_country)
            if (
                type(row["maximum_bucket"]) is not int
                or not 0
                <= row["maximum_bucket"]
                < 1 << header["maximum_hash_bits"]
                or type(row["records"]) is not int
                or row["records"] <= 0
                or len(row["semantic_sum_a"]) != 32
                or len(row["semantic_sum_b"]) != 32
                or (previous is not None and identity <= previous)
            ):
                raise ValueError("address semantic summary row is invalid")
            result.append(
                {
                    "country": row["country"],
                    "bucket": row["maximum_bucket"],
                    "records": row["records"],
                    "semantic_binding": {
                        "schema": SEMANTIC_BINDING_SCHEMA,
                        "records": row["records"],
                        "accumulator_a": bytes(row["semantic_sum_a"]).hex(),
                        "accumulator_b": bytes(row["semantic_sum_b"]).hex(),
                    },
                }
            )
            previous = identity
            total += row["records"]
    if len(result) != header["entries"] or total != header["records"]:
        raise ValueError("address semantic summary totals do not reconcile")
    return header, result


def build_map(
    input_path: Path,
    output_dir: Path,
    completion_path: Path,
    *,
    execution_bucket: str,
    expected_release: str | None,
    expected_schema_fingerprint_sha256: str,
    expected_inventory_sha256: str,
    expected_task_index: int,
    expected_task_digest_sha256: str,
    expected_task_source_digest_sha256: str,
    maximum_hash_bits: int,
    scan_batch_rows: int,
    max_fragment_rows: int,
    max_fragment_bytes: int,
    max_rows: int,
) -> dict[str, Any]:
    import pyarrow.parquet as pq

    if not isinstance(execution_bucket, str) or not execution_bucket.strip():
        raise ValueError("execution_bucket must be a non-empty scheduler identity")
    if len(execution_bucket.encode()) > 256:
        raise ValueError("execution_bucket exceeds its hard byte cap")
    require_sha256(
        expected_schema_fingerprint_sha256,
        "expected_schema_fingerprint_sha256",
    )
    if not 1 <= maximum_hash_bits <= MAXIMUM_SUPPORTED_HASH_BITS:
        raise ValueError("maximum_hash_bits is outside hard bounds")
    if min(scan_batch_rows, max_fragment_rows, max_fragment_bytes, max_rows) <= 0:
        raise ValueError("address map limits must be positive")

    parquet = pq.ParquetFile(input_path)
    missing = sorted(REQUIRED_COLUMNS - set(parquet.schema_arrow.names))
    if missing:
        raise ValueError(f"projected address input is missing columns: {missing}")
    if parquet.metadata.num_rows > max_rows:
        raise ValueError("projected address input exceeds the map row cap")
    source = projected_metadata(parquet)
    validate_source(source, expected_release)
    parquet_metadata = parquet.schema_arrow.metadata or {}
    task_identity = validate_projected_task_identity(
        parquet_metadata,
        execution_bucket=execution_bucket,
        expected_inventory_sha256=expected_inventory_sha256,
        expected_task_index=expected_task_index,
        expected_task_digest_sha256=expected_task_digest_sha256,
        expected_task_source_digest_sha256=expected_task_source_digest_sha256,
    )
    raw_schema_fingerprint = parquet_metadata.get(SCHEMA_FINGERPRINT_METADATA_KEY)
    if raw_schema_fingerprint is None:
        raise ValueError("projected input is missing its schema fingerprint")
    try:
        schema_fingerprint_sha256 = raw_schema_fingerprint.decode()
    except UnicodeDecodeError as exc:
        raise ValueError("projected input schema fingerprint is not UTF-8") from exc
    require_sha256(schema_fingerprint_sha256, "projected schema fingerprint")
    if schema_fingerprint_sha256 != expected_schema_fingerprint_sha256:
        raise ValueError("projected input schema fingerprint differs from the request")

    output_dir.mkdir(parents=True, exist_ok=True)
    rejected: Counter[str] = Counter()
    input_rows = retained_rows = 0
    source_limits = [
        (item["records"], item["row_groups"])
        for item in source["source_inventory"]["objects"]
    ]
    with tempfile.TemporaryDirectory(
        prefix=".address-map-spills-", dir=output_dir
    ) as spill_name:
        spill_dir = Path(spill_name)
        spill_paths: list[Path] = []
        for batch_index, batch in enumerate(
            parquet.iter_batches(batch_size=scan_batch_rows)
        ):
            input_rows += batch.num_rows
            records, batch_rejected = strict_batch_records(
                batch, source_limits=source_limits
            )
            retained_rows += len(records)
            rejected.update(batch_rejected)
            if records:
                if len(spill_paths) >= MAX_OPEN_SPILLS:
                    raise ValueError(
                        "address map spill count exceeds the open-file hard cap"
                    )
                spill_path = spill_dir / f"spill-{batch_index:06d}.bin"
                write_ownership_spill(
                    spill_path,
                    records,
                    maximum_hash_bits=maximum_hash_bits,
                )
                spill_paths.append(spill_path)
        fragments, bucket_summaries, fanout = build_owned_fragments(
            output_dir,
            spill_paths,
            source_inventory_sha256=source["source_inventory_sha256"],
            schema_fingerprint_sha256=schema_fingerprint_sha256,
            release=source["release"],
            execution_bucket=execution_bucket,
            task_identity=task_identity,
            maximum_hash_bits=maximum_hash_bits,
            max_fragment_rows=max_fragment_rows,
            max_fragment_bytes=max_fragment_bytes,
            max_fragments=fragment_count_cap(max_rows, max_fragment_rows),
        )

    rejected_rows = sum(rejected.values())
    if (
        input_rows != parquet.metadata.num_rows
        or input_rows != retained_rows + rejected_rows
        or retained_rows != sum(item["records"] for item in fragments)
    ):
        raise ValueError("global address map accounting does not reconcile")

    counts = [
        {
            "country": item["country"],
            "bucket": item["bucket"],
            "rows": item["records"],
        }
        for item in bucket_summaries
    ]
    summary_identity = write_semantic_summary(
        output_dir,
        bucket_summaries,
        release=source["release"],
        source_inventory_sha256=source["source_inventory_sha256"],
        schema_fingerprint_sha256=schema_fingerprint_sha256,
        task_identity=task_identity,
        maximum_hash_bits=maximum_hash_bits,
    )
    execution = {
        "id": execution_bucket,
        "kind": EXECUTION_BUCKET_KIND,
        "is_serving_shard_id": False,
    }
    manifest = {
        "schema": FRAGMENT_MANIFEST_SCHEMA,
        "overture_release": source["release"],
        "source_inventory_sha256": source["source_inventory_sha256"],
        "schema_fingerprint_sha256": schema_fingerprint_sha256,
        "wire_encoding": WIRE_ENCODING,
        "execution": execution,
        "address_task_identity": task_identity,
        "intermediate_ownership": {
            "kind": FRAGMENT_OWNERSHIP_KIND,
            "maximum_hash_bits": maximum_hash_bits,
            "is_serving_shard_id": False,
        },
        "data_packs": fragments,
        # Temporary compatibility alias for consumers migrating to v3. Both
        # fields are validated as byte-for-byte equal by the planner.
        "fragments": fragments,
        "summary": summary_identity,
        "totals": {
            "fragments": len(fragments),
            "bytes": sum(item["bytes"] for item in fragments),
            "records": sum(item["records"] for item in fragments),
        },
    }
    manifest_path = output_dir / "fragment-manifest.json"
    write_json(manifest_path, manifest)
    manifest_identity = {
        "relative_path": manifest_path.relative_to(output_dir).as_posix(),
        "sha256": sha256_file(manifest_path),
        "bytes": manifest_path.stat().st_size,
        "records": len(fragments),
    }
    completion = {
        "schema": COMPLETION_SCHEMA,
        "family": "addresses",
        "overture_release": source["release"],
        "normalization_version": NORMALIZATION_VERSION,
        "wire_encoding": WIRE_ENCODING,
        "duplicate_id_policy": DUPLICATE_ID_POLICY,
        "source": {
            "inventory_sha256": source["source_inventory_sha256"],
            "schema_fingerprint_sha256": schema_fingerprint_sha256,
            "inventory": source["source_inventory"],
            "projected_input": {
                "sha256": sha256_file(input_path),
                "bytes": input_path.stat().st_size,
                "records": parquet.metadata.num_rows,
            },
        },
        "execution": execution,
        "address_task_identity": task_identity,
        "configuration": {
            "maximum_hash_bits": maximum_hash_bits,
            "scan_batch_rows": scan_batch_rows,
            "max_fragment_rows": max_fragment_rows,
            "max_fragment_bytes": max_fragment_bytes,
            "max_rows": max_rows,
            "max_open_spills": MAX_OPEN_SPILLS,
            "max_fragments": fragment_count_cap(max_rows, max_fragment_rows),
            "parquet_row_group_rows": PARQUET_ROW_GROUP_ROWS,
            "pack_target_bytes": {"minimum": 128_000_000, "maximum": 512_000_000},
        },
        "accounting": {
            "input_rows": input_rows,
            "retained_rows": retained_rows,
            "rejected_rows": rejected_rows,
            "rejections": dict(rejected),
            "reconciles": input_rows == retained_rows + rejected_rows,
        },
        "partition_counts": {
            "schema": COUNT_SCHEMA,
            "overture_release": source["release"],
            "maximum_hash_bits": maximum_hash_bits,
            "scope": "execution_bucket",
            "counts": counts,
            "rows": sum(item["rows"] for item in counts),
        },
        "exact_lookup_fanout": fanout,
        "fragment_manifest": manifest_identity,
        "summary": summary_identity,
        "data_packs": {
            "schema": FRAGMENT_MANIFEST_SCHEMA,
            "objects": fragments,
            "totals": manifest["totals"],
        },
        "fragment_totals": manifest["totals"],
    }
    write_json(completion_path, completion)
    return completion


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--completion-out", type=Path, required=True)
    parser.add_argument("--execution-bucket", required=True)
    parser.add_argument("--expected-release", required=True)
    parser.add_argument("--expected-schema-fingerprint-sha256", required=True)
    parser.add_argument("--expected-inventory-sha256", required=True)
    parser.add_argument("--expected-task-index", type=int, required=True)
    parser.add_argument("--expected-task-digest-sha256", required=True)
    parser.add_argument("--expected-task-source-digest-sha256", required=True)
    parser.add_argument(
        "--maximum-hash-bits", type=int, default=DEFAULT_MAXIMUM_HASH_BITS
    )
    parser.add_argument("--scan-batch-rows", type=int, default=128_000)
    parser.add_argument("--max-fragment-rows", type=int, default=DEFAULT_PACK_ROWS)
    parser.add_argument("--max-fragment-bytes", type=int, default=512_000_000)
    parser.add_argument("--max-rows", type=int, default=4_000_000)
    args = parser.parse_args()
    report = build_map(
        args.input,
        args.output_dir,
        args.completion_out,
        execution_bucket=args.execution_bucket,
        expected_release=args.expected_release,
        expected_schema_fingerprint_sha256=(args.expected_schema_fingerprint_sha256),
        expected_inventory_sha256=args.expected_inventory_sha256,
        expected_task_index=args.expected_task_index,
        expected_task_digest_sha256=args.expected_task_digest_sha256,
        expected_task_source_digest_sha256=(args.expected_task_source_digest_sha256),
        maximum_hash_bits=args.maximum_hash_bits,
        scan_batch_rows=args.scan_batch_rows,
        max_fragment_rows=args.max_fragment_rows,
        max_fragment_bytes=args.max_fragment_bytes,
        max_rows=args.max_rows,
    )
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
