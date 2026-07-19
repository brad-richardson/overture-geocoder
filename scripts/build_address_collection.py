#!/usr/bin/env python3
"""Build a strict Worker-readable address collection from a partition plan.

Only non-empty leaves have page artifacts. Empty hash ranges remain explicit in
the collection so an exact query can return a proven empty result without an R2
read; a missing range is never confused with a malformed release.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from address_partition import (  # noqa: E402
    NORMALIZATION_VERSION,
    PARTITION_SCHEME,
    validate_plan as validate_partition_plan,
)
from common import sha256_file  # noqa: E402


COLLECTION_SCHEMA_VERSION = 2
WORLD_COVERAGE = [-180.0, -90.0, 180.0, 90.0]


def validate_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return validate_partition_plan(plan)


def parse_artifacts(values: list[str]) -> dict[str, tuple[Path, Path]]:
    result: dict[str, tuple[Path, Path]] = {}
    for raw in values:
        parts = raw.split("=")
        if len(parts) != 3 or not all(parts):
            raise ValueError("--artifact must be PARTITION_ID=INDEX_PATH=DATA_PATH")
        identifier, index, data = parts
        if identifier in result:
            raise ValueError(f"duplicate address artifact: {identifier}")
        result[identifier] = (Path(index), Path(data))
    return result


def build_collection(
    plan: dict[str, Any], artifacts: dict[str, tuple[Path, Path]]
) -> dict[str, Any]:
    partitions = validate_plan(plan)
    expected = {item["id"] for item in partitions if item["rows"]}
    if set(artifacts) != expected:
        raise ValueError(
            "address artifacts differ from non-empty plan leaves: "
            f"missing={sorted(expected - set(artifacts))}, "
            f"extra={sorted(set(artifacts) - expected)}"
        )
    items: dict[str, dict[str, Any]] = {}
    empty_ranges = []
    for partition in partitions:
        identity = partition["id"]
        route = {
            "country": partition["country"],
            "hash_prefix": partition["hash_prefix"],
            "hash_bits": partition["hash_bits"],
            "hash_start": partition["hash_start"],
            "hash_end": partition["hash_end"],
            "rows": partition["rows"],
        }
        if not partition["rows"]:
            empty_ranges.append({"id": identity, **route})
            continue
        index_path, data_path = artifacts[identity]
        if not index_path.is_file() or not data_path.is_file():
            raise ValueError(f"address artifact paths do not exist: {identity}")
        index_bytes = index_path.stat().st_size
        data_bytes = data_path.stat().st_size
        if index_bytes < 1 or data_bytes < 1:
            raise ValueError(f"address artifacts must be non-empty: {identity}")
        items[identity] = {
            **route,
            "index_href": f"families/addresses/shards/{identity}.aidx",
            "data_href": f"families/addresses/shards/{identity}.adat",
            "index_bytes": index_bytes,
            "index_sha256": sha256_file(index_path),
            "data_bytes": data_bytes,
            "data_sha256": sha256_file(data_path),
        }
    contract = plan["partition"]
    return {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "overture_release": plan["overture_release"],
        "normalization_version": NORMALIZATION_VERSION,
        "coverage": WORLD_COVERAGE,
        "partition": {
            "scheme": PARTITION_SCHEME,
            "maximum_hash_bits": contract["maximum_hash_bits"],
            "split_row_cap": contract["split_row_cap"],
            "split_ids": contract["split_ids"],
        },
        "items": items,
        "empty_ranges": empty_ranges,
        "totals": {
            "retained_rows": sum(item["rows"] for item in partitions),
            "serving_shards": len(items),
            "empty_ranges": len(empty_ranges),
            "index_bytes": sum(item["index_bytes"] for item in items.values()),
            "data_bytes": sum(item["data_bytes"] for item in items.values()),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    collection = build_collection(
        json.loads(args.plan.read_text()), parse_artifacts(args.artifact)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(collection, indent=2, sort_keys=True) + "\n")
    print(json.dumps(collection["totals"], sort_keys=True))


if __name__ == "__main__":
    main()
