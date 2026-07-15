#!/usr/bin/env python3
"""Create deterministic R2 fixtures for the isolated address Worker smoke."""

from __future__ import annotations

import argparse
import gzip
import json
import struct
import sys
import uuid
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from experiment_address_compression import (  # noqa: E402
    DATA_MAGIC,
    INDEX_MAGIC,
    encode_page,
)
from experiment_address_reduce import canonical_json, encode_text, encode_uvarint  # noqa: E402


LOOKUP_KEY = ("us", "ma", "stoneham", "stoneham", "02180", "main street", "10", "")
CANDIDATES = 137


def record(index: int) -> dict:
    feature_id = str(uuid.UUID(int=index))
    return {
        "key": LOOKUP_KEY + (feature_id,),
        "id": feature_id,
        "lon": -71.0999 + index / 10_000_000,
        "lat": 42.4801,
        "source_row_group": 12,
        "source_row_index": index,
        "country": "US",
        "postal_city": "Stoneham",
        "postcode": "02180",
        "street": "Main Street",
        "number": "10",
        "unit": "",
        "address_levels": ["MA", "Middlesex", "Stoneham"],
    }


def build(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    records = [record(index) for index in range(1, CANDIDATES + 1)]
    raw = encode_page(records, useful=True)
    stored = gzip.compress(raw, compresslevel=6, mtime=0)
    header = canonical_json({"format": 1, "variant": "useful_gzip", "page_rows": 256})
    data = DATA_MAGIC + struct.pack("<I", len(header)) + header
    page_offset = len(data)
    page = struct.pack("<I", len(stored)) + stored
    data += page

    key_payload = b"".join(encode_text(value) for value in LOOKUP_KEY)
    index = b"".join(
        (
            INDEX_MAGIC,
            encode_uvarint(page_offset),
            encode_uvarint(len(page)),
            encode_uvarint(len(records)),
            encode_uvarint(len(key_payload)),
            key_payload,
        )
    )
    (output_dir / "useful_gzip.bin").write_bytes(data)
    (output_dir / "useful_gzip.idx").write_bytes(index)
    report = {
        "schema": "overture-address-worker-smoke-fixture-v1",
        "candidate_count": len(records),
        "data_bytes": len(data),
        "index_bytes": len(index),
        "lookup_key": list(LOOKUP_KEY),
        "first_id": records[0]["id"],
        "last_id": records[-1]["id"],
        "page_offset": page_offset,
        "page_length": len(page),
    }
    (output_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.output_dir), sort_keys=True))


if __name__ == "__main__":
    main()
