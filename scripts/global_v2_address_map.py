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
import heapq
import json
import os
import struct
import sys
import tempfile
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
    FORMAT_VERSION,
    FRAGMENT_MAGIC,
    MAX_RECORD_BYTES,
    canonical_json,
    decode_record,
    projected_metadata,
    read_envelope,
    sha256_file,
    strict_batch_records,
    write_envelope,
)
import inventory_address_rowgroups as address_inventory  # noqa: E402


COMPLETION_SCHEMA = "overture-global-v2-address-map-completion-v1"
FRAGMENT_MANIFEST_SCHEMA = "overture-global-v2-address-fragment-manifest-v1"
EXECUTION_BUCKET_KIND = "global-v2-map-execution-bucket-v1"
FRAGMENT_OWNERSHIP_KIND = "address-country-bucket-range-fragment-v1"
WIRE_ENCODING = "address-reduce-2"
SPILL_MAGIC = b"OAV2SP1\0"
SPILL_FORMAT = 1
FRAGMENT_CAP_MIN_ROWS = 4_096
MAX_COUNTRY_TAIL_FRAGMENTS = 512
MAX_FRAGMENT_MANIFEST_ENTRIES = 4_096
MAX_OPEN_SPILLS = 128
SOURCE_INVENTORY_SCHEMA = "overture-global-source-inventory-v1"
SORT_ORDER = "maximum_hash_bucket/lookup_key/id within one country"
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
        "format": FORMAT_VERSION,
        "wire_encoding": WIRE_ENCODING,
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


def write_ownership_spill(
    path: Path,
    records: list[tuple[tuple[str, ...], bytes]],
    *,
    maximum_hash_bits: int,
) -> None:
    records.sort(
        key=lambda item: (*record_ownership(item[0], maximum_hash_bits), item[0])
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
        self.previous: tuple[str, int, tuple[str, ...]] | None = None

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
        identity = (country, bucket, key)
        if self.previous is not None and identity < self.previous:
            raise ValueError("address ownership spill is not sorted")
        self.previous = identity
        self.read_count += 1
        return country, bucket, key, payload

    def close(self) -> None:
        self.file.close()


class CountryFragmentReader:
    """Read a country fragment whose order is `(max-bucket, lookup-key)`."""

    def __init__(self, path: Path, *, maximum_hash_bits: int):
        self.path = path
        self.file_size = path.stat().st_size
        self.file = path.open("rb")
        self.header = read_envelope(self.file, FRAGMENT_MAGIC)
        if (
            self.header.get("format") != FORMAT_VERSION
            or type(self.header.get("records")) is not int
            or self.header["records"] < 0
        ):
            raise ValueError("invalid address country fragment header")
        self.maximum_hash_bits = maximum_hash_bits
        self.read_count = 0
        self.previous: tuple[int, tuple[str, ...]] | None = None

    def next(self) -> tuple[tuple[str, ...], bytes] | None:
        encoded_length = self.file.read(4)
        if not encoded_length:
            if self.read_count != self.header["records"]:
                raise ValueError("address country fragment record count differs")
            return None
        if len(encoded_length) != 4:
            raise ValueError("truncated address country fragment record length")
        length = struct.unpack("<I", encoded_length)[0]
        if length > MAX_RECORD_BYTES or self.file.tell() + length > self.file_size:
            raise ValueError("address country fragment record exceeds bounds")
        payload = self.file.read(length)
        if len(payload) != length:
            raise ValueError("truncated address country fragment record")
        key = decode_record(payload)["key"]
        identity = (record_ownership(key, self.maximum_hash_bits)[1], key)
        if self.previous is not None and identity < self.previous:
            raise ValueError("address country fragment is not bucket/key sorted")
        self.previous = identity
        self.read_count += 1
        return key, payload

    def close(self) -> None:
        self.file.close()


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
    expected_bytes = fragment_size(header, (payload for _, payload in records))
    if expected_bytes > max_fragment_bytes:
        raise ValueError("one address map fragment exceeds its hard byte cap")

    content_dir = output_dir / "fragments" / "sha256"
    content_dir.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".address-map-", suffix=".tmp", dir=content_dir, delete=False
        ) as output:
            temporary = Path(output.name)
            write_envelope(output, FRAGMENT_MAGIC, header)
            for _, payload in records:
                output.write(struct.pack("<I", len(payload)))
                output.write(payload)
            if output.tell() != expected_bytes:
                raise ValueError("address fragment byte accounting does not reconcile")
        digest = sha256_file(temporary)
        bucket_width = (ownership["maximum_hash_bits"] + 3) // 4
        relative_path = (
            Path("fragments")
            / f"country={ownership['country']}"
            / f"maxbits={ownership['maximum_hash_bits']}"
            / (
                f"buckets={ownership['minimum_bucket']:0{bucket_width}x}-"
                f"{ownership['maximum_bucket']:0{bucket_width}x}"
            )
            / "sha256"
            / f"{digest}.bin"
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
        "intermediate_ownership": ownership,
        "relative_path": relative_path.as_posix(),
        "object_key": (
            f"map/address-fragments/{relative_path.relative_to('fragments').as_posix()}"
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
) -> list[dict[str, Any]]:
    """Merge spills into bounded country fragments ordered by bucket and key."""

    readers = [
        OwnershipSpillReader(path, maximum_hash_bits=maximum_hash_bits)
        for path in spill_paths
    ]
    fragments: list[dict[str, Any]] = []
    heap: list[tuple[str, int, tuple[str, ...], int, bytes]] = []
    current_country: str | None = None
    buffered: list[tuple[tuple[str, ...], bytes]] = []
    buffered_body_bytes = 0
    buffered_minimum_bucket: int | None = None
    buffered_maximum_bucket: int | None = None

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
                heapq.heappush(heap, (country, bucket, key, reader_index, payload))
        while heap:
            country, bucket, key, reader_index, payload = heapq.heappop(heap)
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
                next_country, next_bucket, next_key, next_payload = item
                heapq.heappush(
                    heap,
                    (
                        next_country,
                        next_bucket,
                        next_key,
                        reader_index,
                        next_payload,
                    ),
                )
        flush()
        return fragments
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
    previous_sort: tuple[str, int, tuple[str, ...]] | None = None

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
                key = item[0]
                prefix = key[:8]
                actual_owner = record_ownership(prefix, maximum_hash_bits)
                sort_identity = (actual_owner[0], actual_owner[1], key)
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
        fragments = build_owned_fragments(
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

    counts, fanout = summarize_fragments(
        output_dir,
        fragments,
        maximum_hash_bits=maximum_hash_bits,
        execution_bucket=execution_bucket,
        source_inventory_sha256=source["source_inventory_sha256"],
        schema_fingerprint_sha256=schema_fingerprint_sha256,
        task_identity=task_identity,
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
        "fragments": fragments,
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
    parser.add_argument("--max-fragment-rows", type=int, default=64_000)
    parser.add_argument("--max-fragment-bytes", type=int, default=256_000_000)
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
