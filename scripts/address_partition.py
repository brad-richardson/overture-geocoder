#!/usr/bin/env python3
"""Stable country/hash partition contract for structured address lookup.

The complete normalized eight-field lookup key owns its shard. FNV-1a is the
already deployed cross-runtime routing hash; adaptive high-bit prefixes keep all
duplicates for one exact key together. Split history is sticky, so a country
partition can split but never merge in a later release.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


PLAN_SCHEMA = "overture-address-partition-plan-v1"
COUNT_SCHEMA = "overture-address-partition-counts-v1"
PARTITION_SCHEME = "country-fnv1a-high-bits-v1"
NORMALIZATION_VERSION = "nfc-uniws-collapse-ascii-lower-1"
DEFAULT_MAXIMUM_HASH_BITS = 16
DEFAULT_SHARD_ROW_CAP = 1_000_000
MAXIMUM_SUPPORTED_HASH_BITS = 24
COUNTRY_RE = re.compile(r"[a-z0-9]{2,3}")
ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


@dataclass(frozen=True)
class AddressPartition:
    id: str
    country: str
    hash_prefix: str
    hash_bits: int
    hash_start: int
    hash_end: int
    rows: int


def normalize(value: str | None) -> str:
    return " ".join(unicodedata.normalize("NFC", value or "").split()).translate(
        ASCII_LOWER
    )


def validate_country(country: str) -> None:
    if not COUNTRY_RE.fullmatch(country):
        raise ValueError("address country must be a normalized 2-3 character code")


def address_key_hash(fields: Sequence[str]) -> int:
    if len(fields) != 8:
        raise ValueError("address lookup hash requires exactly eight fields")
    value = 0xCBF29CE484222325
    for index, field in enumerate(fields):
        if index:
            value ^= 0x1F
            value = value * 0x100000001B3 & 0xFFFFFFFFFFFFFFFF
        for byte in field.encode():
            value ^= byte
            value = value * 0x100000001B3 & 0xFFFFFFFFFFFFFFFF
    return value


def record_hash(key: Sequence[str]) -> int:
    return address_key_hash([normalize(value) for value in key[:8]])


def hash_bucket(value: int, bits: int) -> int:
    if not 0 <= bits <= MAXIMUM_SUPPORTED_HASH_BITS:
        raise ValueError("address hash bits are outside hard bounds")
    if not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("address hash is outside the unsigned 64-bit range")
    return value >> (64 - bits) if bits else 0


def bucket_prefix(bucket: int, bits: int) -> str:
    if not 0 <= bits <= MAXIMUM_SUPPORTED_HASH_BITS or not 0 <= bucket < 1 << bits:
        raise ValueError("address hash bucket is outside the configured level")
    return f"{bucket:0{bits}b}" if bits else ""


def prefix_range(prefix: str) -> tuple[int, int]:
    if any(bit not in "01" for bit in prefix) or len(prefix) > MAXIMUM_SUPPORTED_HASH_BITS:
        raise ValueError("invalid address hash prefix")
    value = int(prefix or "0", 2)
    remaining = 64 - len(prefix)
    start = value << remaining
    return start, start + (1 << remaining) - 1


def partition_id(country: str, prefix: str) -> str:
    validate_country(country)
    prefix_range(prefix)
    return f"a-{country}" if not prefix else f"a-{country}-h-{prefix}"


def split_id(country: str, prefix: str) -> str:
    return f"{country}:{prefix}"


def parse_split_id(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or ":" not in value:
        raise ValueError("invalid address split identity")
    country, prefix = value.split(":", 1)
    validate_country(country)
    if len(prefix) >= MAXIMUM_SUPPORTED_HASH_BITS:
        raise ValueError("address split prefix cannot be at the maximum level")
    prefix_range(prefix)
    return country, prefix


def validate_split_ids(
    values: Iterable[str], *, maximum_hash_bits: int
) -> set[tuple[str, str]]:
    if not 1 <= maximum_hash_bits <= MAXIMUM_SUPPORTED_HASH_BITS:
        raise ValueError("maximum_hash_bits is outside hard bounds")
    supplied = list(values)
    if len(set(supplied)) != len(supplied):
        raise ValueError("address split identities must be unique")
    result = {parse_split_id(value) for value in supplied}
    for country, prefix in result:
        if len(prefix) >= maximum_hash_bits:
            raise ValueError("address split identity exceeds the configured maximum")
        if prefix and (country, prefix[:-1]) not in result:
            raise ValueError("address split history omits an ancestor")
    return result


def _plan_prefix(
    country: str,
    prefix: str,
    counts: list[tuple[str, int]],
    *,
    maximum_hash_bits: int,
    row_cap: int,
    sticky: set[tuple[str, str]],
    splits: set[tuple[str, str]],
) -> Iterator[AddressPartition]:
    rows = sum(count for _, count in counts)
    must_split = (country, prefix) in sticky or rows > row_cap
    if not must_split:
        start, end = prefix_range(prefix)
        yield AddressPartition(
            id=partition_id(country, prefix),
            country=country,
            hash_prefix=prefix,
            hash_bits=len(prefix),
            hash_start=start,
            hash_end=end,
            rows=rows,
        )
        return
    if len(prefix) >= maximum_hash_bits:
        raise ValueError(
            f"address partition {partition_id(country, prefix)} has {rows} rows "
            f"above cap {row_cap} at the maximum hash level"
        )
    splits.add((country, prefix))
    for bit in "01":
        child = prefix + bit
        yield from _plan_prefix(
            country,
            child,
            [item for item in counts if item[0].startswith(child)],
            maximum_hash_bits=maximum_hash_bits,
            row_cap=row_cap,
            sticky=sticky,
            splits=splits,
        )


def plan_partitions(
    full_counts: Iterable[tuple[str, int, int]],
    *,
    maximum_hash_bits: int = DEFAULT_MAXIMUM_HASH_BITS,
    row_cap: int = DEFAULT_SHARD_ROW_CAP,
    sticky_split_ids: Iterable[str] = (),
) -> tuple[list[AddressPartition], list[str]]:
    """Plan stable leaves from sorted `(country, max-bit bucket, rows)` counts."""
    if not 1 <= maximum_hash_bits <= MAXIMUM_SUPPORTED_HASH_BITS:
        raise ValueError("maximum_hash_bits is outside hard bounds")
    if row_cap < 1:
        raise ValueError("row_cap must be a positive integer")
    sticky = validate_split_ids(
        sticky_split_ids, maximum_hash_bits=maximum_hash_bits
    )
    splits = set(sticky)
    partitions: list[AddressPartition] = []
    current_country: str | None = None
    country_counts: list[tuple[str, int]] = []
    previous: tuple[str, int] | None = None

    def flush() -> None:
        if current_country is not None:
            partitions.extend(
                _plan_prefix(
                    current_country,
                    "",
                    country_counts,
                    maximum_hash_bits=maximum_hash_bits,
                    row_cap=row_cap,
                    sticky=sticky,
                    splits=splits,
                )
            )

    for country, bucket, rows in full_counts:
        validate_country(country)
        if not 0 <= bucket < 1 << maximum_hash_bits:
            raise ValueError("address maximum-level bucket is outside hard bounds")
        if type(rows) is not int or rows <= 0:
            raise ValueError("address maximum-level counts must be positive integers")
        identity = (country, bucket)
        if previous is not None and identity <= previous:
            raise ValueError("address maximum-level counts must be strictly ordered")
        if current_country is not None and country != current_country:
            flush()
            country_counts = []
        current_country = country
        country_counts.append((bucket_prefix(bucket, maximum_hash_bits), rows))
        previous = identity
    flush()
    if not partitions:
        raise ValueError("address partition input contains no retained rows")
    return partitions, sorted(split_id(*item) for item in splits)


def validate_plan(
    plan: dict[str, Any], *, expected_maximum_hash_bits: int | None = None
) -> list[dict[str, Any]]:
    """Validate a complete plan, including leaf ancestry and hash coverage."""
    contract = plan.get("partition")
    if (
        plan.get("schema") != PLAN_SCHEMA
        or plan.get("normalization_version") != NORMALIZATION_VERSION
        or not isinstance(plan.get("overture_release"), str)
        or not plan["overture_release"]
        or not isinstance(contract, dict)
        or contract.get("scheme") != PARTITION_SCHEME
        or not isinstance(contract.get("maximum_hash_bits"), int)
        or not isinstance(contract.get("split_row_cap"), int)
        or not isinstance(contract.get("split_ids"), list)
        or not isinstance(plan.get("partitions"), list)
        or not plan["partitions"]
    ):
        raise ValueError("unsupported address partition plan")
    maximum_hash_bits = contract["maximum_hash_bits"]
    row_cap = contract["split_row_cap"]
    if (
        not 1 <= maximum_hash_bits <= MAXIMUM_SUPPORTED_HASH_BITS
        or row_cap < 1
        or (
            expected_maximum_hash_bits is not None
            and maximum_hash_bits != expected_maximum_hash_bits
        )
    ):
        raise ValueError("address partition plan limits are incompatible")
    splits = validate_split_ids(
        contract["split_ids"], maximum_hash_bits=maximum_hash_bits
    )
    partitions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in plan["partitions"]:
        if not isinstance(raw, dict):
            raise ValueError("address plan partitions must be objects")
        country = raw.get("country")
        prefix = raw.get("hash_prefix")
        rows = raw.get("rows")
        if not isinstance(country, str) or not isinstance(prefix, str):
            raise ValueError("address plan partition identity is invalid")
        validate_country(country)
        start, end = prefix_range(prefix)
        identifier = partition_id(country, prefix)
        if (
            raw.get("id") != identifier
            or raw.get("hash_bits") != len(prefix)
            or raw.get("hash_start") != start
            or raw.get("hash_end") != end
            or type(rows) is not int
            or not 0 <= rows <= row_cap
            or (country, prefix) in splits
            or (prefix and (country, prefix[:-1]) not in splits)
            or identifier in seen_ids
        ):
            raise ValueError("address plan leaf is inconsistent with split history")
        partitions.append(dict(raw))
        seen_ids.add(identifier)

    by_country: dict[str, list[dict[str, Any]]] = {}
    used_splits: set[tuple[str, str]] = set()
    for item in partitions:
        by_country.setdefault(item["country"], []).append(item)
        used_splits.update(
            (item["country"], item["hash_prefix"][:length])
            for length in range(len(item["hash_prefix"]))
        )
    if splits != used_splits:
        raise ValueError("address plan split history differs from the leaf tree")
    for country, leaves in by_country.items():
        leaves.sort(key=lambda item: item["hash_start"])
        expected = 0
        for leaf in leaves:
            if leaf["hash_start"] != expected:
                raise ValueError(f"address plan leaves do not cover {country} contiguously")
            expected = leaf["hash_end"] + 1
        if expected != 1 << 64:
            raise ValueError(f"address plan leaves do not cover all hashes for {country}")
    totals = plan.get("totals")
    if (
        not isinstance(totals, dict)
        or totals.get("retained_rows") != sum(item["rows"] for item in partitions)
        or totals.get("partitions") != len(partitions)
        or totals.get("nonempty_partitions")
        != sum(item["rows"] > 0 for item in partitions)
        or totals.get("empty_partitions") != sum(item["rows"] == 0 for item in partitions)
    ):
        raise ValueError("address plan totals do not reconcile")
    return sorted(partitions, key=lambda item: (item["country"], item["hash_start"]))


def build_plan(
    counts: dict[str, Any],
    *,
    maximum_hash_bits: int,
    row_cap: int,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if counts.get("schema") != COUNT_SCHEMA or not isinstance(counts.get("counts"), list):
        raise ValueError("unsupported address partition counts")
    release = counts.get("overture_release")
    if not isinstance(release, str) or not release:
        raise ValueError("address partition counts omit the Overture release")
    sticky: list[str] = []
    if previous is not None:
        validate_plan(
            previous, expected_maximum_hash_bits=maximum_hash_bits
        )
        sticky = previous["partition"]["split_ids"]
    rows = [
        (item["country"], item["bucket"], item["rows"])
        for item in counts["counts"]
        if isinstance(item, dict)
    ]
    if len(rows) != len(counts["counts"]):
        raise ValueError("address partition count entries must be objects")
    partitions, split_ids = plan_partitions(
        rows,
        maximum_hash_bits=maximum_hash_bits,
        row_cap=row_cap,
        sticky_split_ids=sticky,
    )
    return {
        "schema": PLAN_SCHEMA,
        "overture_release": release,
        "normalization_version": NORMALIZATION_VERSION,
        "partition": {
            "scheme": PARTITION_SCHEME,
            "maximum_hash_bits": maximum_hash_bits,
            "split_row_cap": row_cap,
            "split_ids": split_ids,
        },
        "totals": {
            "retained_rows": sum(item.rows for item in partitions),
            "partitions": len(partitions),
            "nonempty_partitions": sum(item.rows > 0 for item in partitions),
            "empty_partitions": sum(item.rows == 0 for item in partitions),
        },
        "partitions": [asdict(item) for item in partitions],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", type=Path, required=True)
    parser.add_argument("--previous-plan", type=Path)
    parser.add_argument("--maximum-hash-bits", type=int, default=DEFAULT_MAXIMUM_HASH_BITS)
    parser.add_argument("--shard-row-cap", type=int, default=DEFAULT_SHARD_ROW_CAP)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    counts = json.loads(args.counts.read_text())
    previous = (
        None if args.previous_plan is None else json.loads(args.previous_plan.read_text())
    )
    plan = build_plan(
        counts,
        maximum_hash_bits=args.maximum_hash_bits,
        row_cap=args.shard_row_cap,
        previous=previous,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    print(json.dumps(plan["totals"], sort_keys=True))


if __name__ == "__main__":
    main()
