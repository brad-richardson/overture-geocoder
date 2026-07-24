#!/usr/bin/env python3
"""Frozen typed schema for address shuffle packs.

Extracted from the retired `global_v2_address_map`, which was the only reason
the address construction spike still reached into the abandoned global-v2
modules. `spike_address_construction` is loaded by `address_construction_v1`, so
this is on the construction-v1 path and should not depend on anything outside
it.

Changing the schema changes what an already-written pack is allowed to look
like, so treat it as a versioned contract rather than a convenience helper.
"""

from __future__ import annotations

from typing import Any


def shuffle_schema() -> Any:
    """The frozen typed address shuffle schema, without provenance metadata."""

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


def payload_to_shuffle_row(payload: bytes, *, maximum_hash_bits: int) -> dict[str, Any]:
    """Decode a canonical address record into one typed shuffle row."""

    import uuid

    from address_partition import hash_bucket, record_hash
    from experiment_address_reduce import decode_record, encode_record

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
