#!/usr/bin/env python3
"""Generate cross-language page/extension fixtures for the range-reader core.

Writes deterministic binary fixtures under ``tests/fixtures/pages/`` using the
Python page codecs, so the Rust decoders can decode the exact same bytes:

* ``plain_page.bin`` -- ``experiment_address_compression.encode_page(useful=True)``
  output (the decompressed lookup-safe core page); decoded by the address record
  payload decoder in ``geocoder-worker``.
* ``extended_page.bin`` -- ``experiment_address_format_convergence.encode_extended_page``
  output (uvarint core length + core + division extension); its framing and
  division extension are decoded by the payload-agnostic ``geocoder-core::pages``.
* ``truncated_extended_page.bin`` -- a corrupt page whose declared core length
  runs past the buffer; every reader must reject it.

This pins the cross-language wire contract the way
``tests/fixtures/router_normalization_cases.json`` pins router tokenization.
Only imports the existing experiment scripts; it does not modify them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import experiment_address_compression as compression  # noqa: E402
import experiment_address_format_convergence as convergence  # noqa: E402

record_key = convergence.reduce.record_key

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "pages"
# A core length claim far larger than any bytes that follow it.
TRUNCATED_CORE_LENGTH = 10_000


def _record(index: int, number: str, gers_ints, method: int, confidence: int) -> dict:
    feature_id = str(uuid.UUID(int=index))
    record = {
        "id": feature_id,
        "lon": -71.1 + index / 10_000_000,
        "lat": 42.37,
        "country": "US",
        "postal_city": "Cambridge",
        "postcode": "02139",
        "street": "Main Street",
        "number": number,
        "unit": "",
        "address_levels": ["MA", "Cambridge"],
        "source_object_index": 0,
        "source_row_group": 0,
        "source_row_index": index,
        "division_gers_ids": [str(uuid.UUID(int=n)) for n in gers_ints],
        "match_method": method,
        "match_confidence": confidence,
    }
    record["key"] = record_key(record)
    return record


def build_records() -> list[dict]:
    """Three records, two distinct eight-field keys, sorted like a real page."""
    records = [
        _record(1, "10", [7, 8], convergence.MATCH_METHOD_INTERIOR, 2),
        _record(2, "11", [7], convergence.MATCH_METHOD_BOUNDARY, 1),
        _record(3, "11", [], convergence.MATCH_METHOD_NONE, 0),
    ]
    records.sort(key=lambda item: item["key"])
    return records


def build_fixtures() -> tuple[list[dict], dict[str, bytes]]:
    records = build_records()
    files = {
        "plain_page.bin": compression.encode_page(records, useful=True),
        "extended_page.bin": convergence.encode_extended_page(records),
        "truncated_extended_page.bin": convergence.encode_uvarint(
            TRUNCATED_CORE_LENGTH
        ),
    }
    return records, files


def build_report(records: list[dict], files: dict[str, bytes]) -> dict:
    return {
        "schema": "overture-page-fixture-v1",
        "record_count": len(records),
        "lookup_keys": [list(record["key"][:8]) for record in records],
        "files": {
            name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(files.items())
        },
    }


def write(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    records, files = build_fixtures()
    for name, data in files.items():
        (output_dir / name).write_bytes(data)
    report = build_report(records, files)
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=FIXTURE_DIR)
    args = parser.parse_args()
    print(json.dumps(write(args.output_dir), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
