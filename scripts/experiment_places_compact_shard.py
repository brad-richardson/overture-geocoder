#!/usr/bin/env python3
"""Build and measure a range-readable spatial Places shard.

The artifact stores one unified exact-token lexicon, lexicographically adjacent
posting lists, a fixed-width record index, and minimal result projections.
Prefixes are resolved through a lexicon range and one contiguous posting span;
no prefix posting unions are materialized. This is an offline architecture
spike and does not access Cloudflare services.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from experiment_places_compact_index import (  # noqa: E402
    Place,
    TOKENIZER_VERSION,
    common_prefix,
    decode_varint,
    encode_varint,
    load_places,
    normalize,
    pack_text,
    tokens,
    unpack_text,
)
from experiment_places_kv_r2_pages import (  # noqa: E402
    CASES,
    FIELD_BITS,
    FIELDS,
    Clause,
    QueryCase,
)
from experiment_places_locality_head import ordered_places  # noqa: E402


MAGIC = b"PCSH0001"
PREAMBLE = struct.Struct("<8sI")
RECORD_INDEX = struct.Struct("<II")


def encode_identity(place: Place) -> bytes:
    try:
        return b"\x01" + uuid.UUID(place.place_id).bytes
    except (ValueError, AttributeError):
        return b"\x00" + pack_text(place.place_id)


def encode_projection_fields(place: Place, fields: tuple[str, ...]) -> bytes:
    fixed = struct.pack("<ffB", place.lat, place.lon, round(place.confidence * 255))
    return (
        fixed
        + encode_identity(place)
        + b"".join(pack_text(getattr(place, field)) for field in fields)
    )


def encode_projection(place: Place) -> bytes:
    return encode_projection_fields(
        place, ("name", "category", "locality", "region", "country")
    )


def decode_projection(data: bytes) -> dict[str, Any]:
    if len(data) < 10:
        raise ValueError("truncated projection")
    lat, lon, rank = struct.unpack_from("<ffB", data)
    offset = 9
    encoding = data[offset]
    offset += 1
    if encoding == 1:
        if offset + 16 > len(data):
            raise ValueError("truncated UUID")
        place_id = str(uuid.UUID(bytes=data[offset : offset + 16]))
        offset += 16
    elif encoding == 0:
        place_id, offset = unpack_text(data, offset)
    else:
        raise ValueError("unknown ID encoding")
    values = []
    for _ in range(5):
        value, offset = unpack_text(data, offset)
        values.append(value)
    return dict(
        zip(("name", "category", "locality", "region", "country"), values),
        id=place_id,
        lat=lat,
        lon=lon,
        confidence=rank / 255,
    )


def posting_map(places: list[Place]) -> dict[str, dict[int, tuple[int, int]]]:
    result: dict[str, dict[int, tuple[int, int]]] = {}
    for doc_id, place in enumerate(places):
        rank = round(place.confidence * 255)
        for field, value in place.field_text().items():
            bit = FIELD_BITS[field]
            for token in tokens(value):
                docs = result.setdefault(token, {})
                old_mask, old_rank = docs.get(doc_id, (0, rank))
                docs[doc_id] = (old_mask | bit, max(old_rank, rank))
    return result


def encode_posting_items(items: list[tuple[int, tuple[int, int]]]) -> bytes:
    out = bytearray()
    previous = 0
    for index, (doc_id, (mask, rank)) in enumerate(items):
        out += encode_varint(doc_id if index == 0 else doc_id - previous)
        out += bytes((mask, rank))
        previous = doc_id
    return bytes(out)


def encode_postings(docs: dict[int, tuple[int, int]]) -> bytes:
    return encode_posting_items(sorted(docs.items()))


def varint_size(value: int) -> int:
    size = 1
    while value >= 0x80:
        size += 1
        value >>= 7
    return size


def posting_variant_size(
    items: list[tuple[int, tuple[int, int]]],
    allowed_mask: int,
) -> tuple[int, int]:
    size = count = 0
    previous = 0
    for doc_id, (mask, _) in items:
        if not mask & allowed_mask:
            continue
        size += varint_size(doc_id if count == 0 else doc_id - previous) + 2
        previous = doc_id
        count += 1
    return size, count


def decode_postings(data: bytes, count: int) -> list[tuple[int, int, int]]:
    result = []
    offset = 0
    previous = 0
    for index in range(count):
        delta, offset = decode_varint(data, offset)
        doc_id = delta if index == 0 else previous + delta
        if offset + 2 > len(data):
            raise ValueError("truncated posting")
        result.append((doc_id, data[offset], data[offset + 1]))
        offset += 2
        previous = doc_id
    if offset != len(data):
        raise ValueError("posting length mismatch")
    return result


def build_artifact(
    places: list[Place],
    output: Path,
    block_entries: int = 256,
    cell_degrees: float = 0.25,
) -> tuple[list[Place], dict[str, Any]]:
    started = time.perf_counter()
    ordered = ordered_places(places, cell_degrees)
    exact = posting_map(ordered)
    postings = bytearray()
    entries: list[tuple[str, int, int, int]] = []
    posting_variants = {
        "all_fields": {"mask": sum(FIELD_BITS.values()), "bytes": 0, "tokens": 0},
        "no_context": {
            "mask": FIELD_BITS["name"] | FIELD_BITS["brand"] | FIELD_BITS["category"],
            "bytes": 0,
            "tokens": 0,
        },
        "name_and_brand": {
            "mask": FIELD_BITS["name"] | FIELD_BITS["brand"],
            "bytes": 0,
            "tokens": 0,
        },
        "name_only": {"mask": FIELD_BITS["name"], "bytes": 0, "tokens": 0},
    }
    for token in sorted(exact, key=lambda value: value.encode("utf-8")):
        items = sorted(exact[token].items())
        encoded = encode_posting_items(items)
        entries.append((token, len(postings), len(encoded), len(exact[token])))
        postings += encoded
        for variant in posting_variants.values():
            size, count = posting_variant_size(items, variant["mask"])
            variant["bytes"] += size
            variant["tokens"] += int(count > 0)

    lexicon = bytearray()
    blocks = []
    for first in range(0, len(entries), block_entries):
        group = entries[first : first + block_entries]
        block_start = len(lexicon)
        lexicon += encode_varint(len(group))
        previous = b""
        for token, posting_offset, posting_length, count in group:
            key = token.encode("utf-8")
            shared = common_prefix(previous, key)
            suffix = key[shared:]
            lexicon += encode_varint(shared) + encode_varint(len(suffix)) + suffix
            lexicon += encode_varint(posting_offset)
            lexicon += encode_varint(posting_length)
            lexicon += encode_varint(count)
            previous = key
        blocks.append(
            {
                "first": group[0][0],
                "last": group[-1][0],
                "offset": block_start,
                "length": len(lexicon) - block_start,
                "entries": len(group),
            }
        )

    records = bytearray()
    record_index = bytearray()
    projection_variants = {
        "locator_only": {
            "fields": (),
            "semantics": "ID, coordinates, confidence only; every display result requires hydration",
        },
        "name_only": {
            "fields": ("name",),
            "semantics": "adds display name; category and administrative context require hydration",
        },
        "search_response": {
            "fields": ("name", "category", "locality", "region", "country"),
            "semantics": "self-contained basic geocoder result; non-search Overture properties require hydration",
        },
    }
    for variant in projection_variants.values():
        variant["records_bytes"] = 0
    for place in ordered:
        encoded = encode_projection(place)
        if len(records) + len(encoded) >= 2**32:
            raise ValueError("record section exceeds 32-bit offset format")
        record_index += RECORD_INDEX.pack(len(records), len(encoded))
        records += encoded
        for variant in projection_variants.values():
            variant["records_bytes"] += len(
                encode_projection_fields(place, variant["fields"])
            )

    components = {
        "lexicon": bytes(lexicon),
        "postings": bytes(postings),
        "record_index": bytes(record_index),
        "records": bytes(records),
    }
    directory = {
        "schema_version": 1,
        "tokenizer_version": TOKENIZER_VERSION,
        "record_count": len(ordered),
        "token_count": len(entries),
        "cell_degrees": cell_degrees,
        "field_bits": FIELD_BITS,
        "lexicon_blocks": blocks,
        "components": {
            name: {"length": len(data)} for name, data in components.items()
        },
    }
    directory_bytes = b""
    for _ in range(12):
        directory_bytes = json.dumps(
            directory, sort_keys=True, separators=(",", ":")
        ).encode()
        cursor = PREAMBLE.size + len(directory_bytes)
        changed = False
        for name, data in components.items():
            if directory["components"][name].get("offset") != cursor:
                directory["components"][name]["offset"] = cursor
                changed = True
            cursor += len(data)
        if not changed:
            break
    else:
        raise RuntimeError("artifact directory offsets did not stabilize")
    directory_bytes = json.dumps(
        directory, sort_keys=True, separators=(",", ":")
    ).encode()

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as dst:
        dst.write(PREAMBLE.pack(MAGIC, len(directory_bytes)))
        dst.write(directory_bytes)
        for data in components.values():
            dst.write(data)
    elapsed = time.perf_counter() - started
    size = output.stat().st_size
    fixed_without_records = size - len(records)
    for variant in projection_variants.values():
        variant["artifact_bytes_if_substituted"] = (
            fixed_without_records + variant["records_bytes"]
        )
        variant["bytes_per_place_if_substituted"] = variant[
            "artifact_bytes_if_substituted"
        ] / len(ordered)
        variant["fields"] = list(variant["fields"])
    for variant in posting_variants.values():
        variant.pop("mask")
    return ordered, {
        "build_seconds": elapsed,
        "places": len(ordered),
        "tokens": len(entries),
        "artifact_bytes": size,
        "bytes_per_place": size / len(ordered),
        "objects": 1,
        "components": {
            "directory": PREAMBLE.size + len(directory_bytes),
            **{name: len(data) for name, data in components.items()},
        },
        "projection_variants": projection_variants,
        "posting_field_variants": posting_variants,
    }


@dataclass(frozen=True)
class LexiconEntry:
    token: str
    posting_offset: int
    posting_length: int
    posting_count: int


@dataclass(frozen=True)
class ReadChunk:
    offset: int
    data: bytes


class RangeReader:
    def __init__(self, path: Path):
        self.path = path
        self.reads: list[dict[str, Any]] = []

    def read(self, offset: int, length: int, stage: str) -> bytes:
        with self.path.open("rb") as src:
            src.seek(offset)
            data = src.read(length)
        if len(data) != length:
            raise ValueError("short shard range read")
        self.reads.append({"stage": stage, "offset": offset, "length": length})
        return data

    def read_ranges(
        self,
        ranges: Iterable[tuple[int, int]],
        stage: str,
        max_gap: int,
    ) -> list[ReadChunk]:
        selected = sorted((start, start + length) for start, length in ranges if length)
        if not selected:
            return []
        plans = []
        start, end = selected[0]
        for next_start, next_end in selected[1:]:
            if next_start > end + max_gap:
                plans.append((start, end))
                start, end = next_start, next_end
            else:
                end = max(end, next_end)
        plans.append((start, end))
        return [
            ReadChunk(start, self.read(start, end - start, stage))
            for start, end in plans
        ]


def chunk_slice(chunks: list[ReadChunk], offset: int, length: int) -> bytes:
    for chunk in chunks:
        relative = offset - chunk.offset
        if relative >= 0 and relative + length <= len(chunk.data):
            return chunk.data[relative : relative + length]
    raise ValueError("requested bytes are outside fetched chunks")


def decode_lexicon_block(data: bytes) -> list[LexiconEntry]:
    count, offset = decode_varint(data)
    result = []
    previous = b""
    for _ in range(count):
        shared, offset = decode_varint(data, offset)
        suffix_length, offset = decode_varint(data, offset)
        end = offset + suffix_length
        key = previous[:shared] + data[offset:end]
        offset = end
        posting_offset, offset = decode_varint(data, offset)
        posting_length, offset = decode_varint(data, offset)
        posting_count, offset = decode_varint(data, offset)
        result.append(
            LexiconEntry(key.decode(), posting_offset, posting_length, posting_count)
        )
        previous = key
    return result


class CompactShard:
    def __init__(self, path: Path):
        self.reader = RangeReader(path)
        preamble = self.reader.read(0, PREAMBLE.size, "directory")
        magic, length = PREAMBLE.unpack(preamble)
        if magic != MAGIC:
            raise ValueError("not a compact Places shard")
        self.directory = json.loads(
            self.reader.read(PREAMBLE.size, length, "directory")
        )
        self._directory_reads = len(self.reader.reads)
        self._directory_bytes = sum(row["length"] for row in self.reader.reads)

    def component(self, name: str) -> tuple[int, int]:
        component = self.directory["components"][name]
        return component["offset"], component["length"]

    def lexicon_matches(self, value: str, prefix: bool) -> list[LexiconEntry]:
        blocks = []
        upper = value + "\U0010ffff"
        for block in self.directory["lexicon_blocks"]:
            relevant = block["first"] <= value <= block["last"]
            if prefix:
                relevant = block["last"] >= value and block["first"] <= upper
            if relevant:
                blocks.append(block)
        base, _ = self.component("lexicon")
        chunks = self.reader.read_ranges(
            ((base + block["offset"], block["length"]) for block in blocks),
            "lexicon",
            0,
        )
        matches = []
        for block in blocks:
            data = chunk_slice(chunks, base + block["offset"], block["length"])
            for entry in decode_lexicon_block(data):
                if entry.token == value or (prefix and entry.token.startswith(value)):
                    matches.append(entry)
        return matches

    def clause_docs(self, clause: Clause) -> dict[int, tuple[int, int]]:
        value = normalize(clause.value)
        entries = self.lexicon_matches(value, clause.prefix)
        if not entries:
            return {}
        postings_base, _ = self.component("postings")
        start = postings_base + entries[0].posting_offset
        end = postings_base + entries[-1].posting_offset + entries[-1].posting_length
        data = self.reader.read(start, end - start, "postings")
        docs: dict[int, tuple[int, int]] = {}
        for entry in entries:
            relative = postings_base + entry.posting_offset - start
            encoded = data[relative : relative + entry.posting_length]
            for doc_id, mask, rank in decode_postings(encoded, entry.posting_count):
                old_mask, old_rank = docs.get(doc_id, (0, rank))
                docs[doc_id] = (old_mask | mask, max(old_rank, rank))
        if clause.field:
            bit = FIELD_BITS[clause.field]
            docs = {doc: value for doc, value in docs.items() if value[0] & bit}
        return docs

    def records(
        self,
        doc_ids: list[int],
        index_gap: int,
        record_gap: int,
    ) -> list[dict[str, Any]]:
        index_base, _ = self.component("record_index")
        index_ranges = [
            (index_base + doc_id * RECORD_INDEX.size, RECORD_INDEX.size)
            for doc_id in doc_ids
        ]
        index_chunks = self.reader.read_ranges(
            index_ranges, "record_index", max_gap=index_gap
        )
        positions = []
        for doc_id in doc_ids:
            absolute = index_base + doc_id * RECORD_INDEX.size
            positions.append(
                RECORD_INDEX.unpack(
                    chunk_slice(index_chunks, absolute, RECORD_INDEX.size)
                )
            )
        records_base, _ = self.component("records")
        record_ranges = [
            (records_base + offset, length) for offset, length in positions
        ]
        record_chunks = self.reader.read_ranges(
            record_ranges, "records", max_gap=record_gap
        )
        return [
            decode_projection(chunk_slice(record_chunks, records_base + offset, length))
            for offset, length in positions
        ]

    def query(
        self,
        case: QueryCase,
        limit: int = 10,
        index_gap: int = 64 * 1024,
        record_gap: int = 256 * 1024,
    ) -> dict[str, Any]:
        before = len(self.reader.reads)
        candidates: set[int] | None = None
        ranks: dict[int, int] = {}
        matched_tokens = []
        for clause in case.clauses:
            docs = self.clause_docs(clause)
            matched_tokens.append(len(docs))
            ids = set(docs)
            candidates = ids if candidates is None else candidates & ids
            for doc_id, (_, rank) in docs.items():
                ranks[doc_id] = max(ranks.get(doc_id, 0), rank)
        candidates = candidates or set()
        best = sorted(candidates, key=lambda doc: (-ranks[doc], doc))[:limit]
        results = self.records(best, index_gap, record_gap)
        reads = self.reader.reads[before:]
        stages: dict[str, dict[str, int]] = {}
        for stage in ("lexicon", "postings", "record_index", "records"):
            selected = [row for row in reads if row["stage"] == stage]
            stages[stage] = {
                "reads": len(selected),
                "bytes": sum(row["length"] for row in selected),
            }
        return {
            "candidate_count": len(candidates),
            "candidate_doc_ids": sorted(candidates),
            "result_ids": [row["id"] for row in results],
            "results": results,
            "range_reads": len(reads),
            "bytes_transferred": sum(row["length"] for row in reads),
            "cold_range_reads": len(reads) + self._directory_reads,
            "cold_bytes_transferred": sum(row["length"] for row in reads)
            + self._directory_bytes,
            "stages": stages,
            "clause_candidate_counts": matched_tokens,
        }


def brute_clause(places: list[Place], clause: Clause) -> set[int]:
    query = normalize(clause.value)
    fields = (clause.field,) if clause.field else FIELDS
    result = set()
    for doc_id, place in enumerate(places):
        values = [
            token for field in fields for token in tokens(place.field_text()[field])
        ]
        if any(
            token.startswith(query) if clause.prefix else token == query
            for token in values
        ):
            result.add(doc_id)
    return result


def oracle(
    places: list[Place], case: QueryCase, limit: int = 10
) -> tuple[set[int], list[str]]:
    candidates: set[int] | None = None
    for clause in case.clauses:
        docs = brute_clause(places, clause)
        candidates = docs if candidates is None else candidates & docs
    candidates = candidates or set()
    best = sorted(
        candidates,
        key=lambda doc: (-round(places[doc].confidence * 255), doc),
    )[:limit]
    return candidates, [places[doc].place_id for doc in best]


def benchmark(
    places: list[Place],
    artifact: Path,
    index_gap: int = 64 * 1024,
    record_gap: int = 256 * 1024,
) -> dict[str, Any]:
    rows = []
    for case in CASES:
        shard = CompactShard(artifact)
        result = shard.query(case, index_gap=index_gap, record_gap=record_gap)
        expected, expected_ids = oracle(places, case)
        actual_candidates = set(result.pop("candidate_doc_ids"))
        result.update(
            {
                "name": case.name,
                "oracle_candidate_count": len(expected),
                "complete_candidate_recall": actual_candidates == expected,
                "top_k_exact": result["result_ids"] == expected_ids,
            }
        )
        rows.append(result)
    nonempty = [row for row in rows if row["candidate_count"]]
    return {
        "range_profile": {
            "record_index_max_gap": index_gap,
            "records_max_gap": record_gap,
        },
        "queries": rows,
        "summary": {
            "query_count": len(rows),
            "nonempty_query_count": len(nonempty),
            "complete_candidate_recall": all(
                row["complete_candidate_recall"] for row in rows
            ),
            "top_k_exact": all(row["top_k_exact"] for row in rows),
            "warm_max_range_reads": max(
                (row["range_reads"] for row in nonempty), default=0
            ),
            "warm_max_bytes": max(
                (row["bytes_transferred"] for row in nonempty), default=0
            ),
            "cold_max_range_reads": max(
                (row["cold_range_reads"] for row in nonempty), default=0
            ),
            "cold_max_bytes": max(
                (row["cold_bytes_transferred"] for row in nonempty), default=0
            ),
        },
    }


def markdown(report: dict[str, Any]) -> str:
    build = report["build"]
    summary = report["benchmark"]["summary"]
    lines = [
        "# Places compact spatial-shard spike",
        "",
        f"- Input: {build['places']:,} Places",
        f"- Artifact: {build['artifact_bytes']:,} bytes ({build['bytes_per_place']:.1f} B/place)",
        f"- Tokens: {build['tokens']:,}",
        f"- Build: {build['build_seconds']:.2f} seconds",
        f"- Warm max: {summary['warm_max_range_reads']} ranges / {summary['warm_max_bytes']:,} B",
        f"- Cold max: {summary['cold_max_range_reads']} ranges / {summary['cold_max_bytes']:,} B",
        f"- Complete recall: {summary['complete_candidate_recall']}; exact top-k: {summary['top_k_exact']}",
        "",
        "## Components",
        "",
        "| component | bytes | B/place |",
        "|---|---:|---:|",
    ]
    for name, size in build["components"].items():
        lines.append(f"| {name} | {size:,} | {size / build['places']:.1f} |")
    lines += [
        "",
        "## Projection storage floors",
        "",
        "| projection | artifact bytes | B/place | tradeoff |",
        "|---|---:|---:|---|",
    ]
    for name, value in build["projection_variants"].items():
        lines.append(
            f"| {name} | {value['artifact_bytes_if_substituted']:,} | "
            f"{value['bytes_per_place_if_substituted']:.1f} | {value['semantics']} |"
        )
    lines += [
        "",
        "## Posting field floors",
        "",
        "These figures cover posting payload bytes only; a substituted artifact would also have a smaller lexicon.",
        "",
        "| searchable fields | tokens | posting bytes |",
        "|---|---:|---:|",
    ]
    for name, value in build["posting_field_variants"].items():
        lines.append(f"| {name} | {value['tokens']:,} | {value['bytes']:,} |")
    lines += [
        "",
        "## Queries",
        "",
        "| query | candidates | warm ranges/bytes | lex | postings | index | records | exact |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["benchmark"]["queries"]:
        stage = row["stages"]
        lines.append(
            f"| {row['name']} | {row['candidate_count']:,} | {row['range_reads']} / {row['bytes_transferred']:,} | "
            f"{stage['lexicon']['reads']} | {stage['postings']['reads']} | "
            f"{stage['record_index']['reads']} | {stage['records']['reads']} | {row['top_k_exact']} |"
        )
    lines += [
        "",
        "## Semantics",
        "",
        "The directory is counted for cold queries and assumed cached for warm queries. Prefix postings are not precomputed: matching exact-token postings are adjacent and fetched as one contiguous span per clause.",
        "",
        f"The latency-oriented reader coalesces record-index gaps up to {report['benchmark']['range_profile']['record_index_max_gap']:,} bytes and record gaps up to {report['benchmark']['range_profile']['records_max_gap']:,} bytes. Transferred-byte totals include that deliberate overfetch.",
        "",
        "Result projections contain ID, display name, primary category, locality/region/country, coordinates, and quantized confidence. Other Overture properties are intentionally excluded and may be hydrated separately.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--block-entries", type=int, default=256)
    parser.add_argument("--record-index-gap", type=int, default=64 * 1024)
    parser.add_argument("--record-gap", type=int, default=256 * 1024)
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.record_index_gap < 0 or args.record_gap < 0:
        parser.error("range gaps cannot be negative")
    places = load_places(args.input, args.limit)
    ordered, build = build_artifact(places, args.artifact, args.block_entries)
    measured = benchmark(
        ordered,
        args.artifact,
        args.record_index_gap,
        args.record_gap,
    )
    report = {
        "schema_version": 1,
        "input": str(args.input),
        "architecture": {
            "partition_target_places": 1_000_000,
            "prefix_strategy": "lexicon range plus contiguous exact-token posting span",
            "record_projection": "id, name, category, locality, region, country, coordinates, quantized confidence",
            "proposed_planet_object_shape_at_75m": {
                "shard_objects": math.ceil(75_000_000 / 1_000_000),
                "manifest_objects": 1,
                "measured_by_this_experiment": False,
                "note": "The compact experiment builds only one spatial shard. A one-object range-readable global head is an unmeasured repack target; the separate head model produced 4,088 objects and 25.1 MB for its 1M sample.",
            },
        },
        "build": build,
        "benchmark": measured,
        "linear_shape_extrapolation": {
            "warning": "compact spatial-shard bytes only; excludes the separately modeled global head and is not a forecast because California token and record distributions are not globally representative",
            "seventy_five_million_places_bytes": round(
                build["bytes_per_place"] * 75_000_000
            ),
            "two_releases_bytes": round(build["bytes_per_place"] * 150_000_000),
        },
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2) + "\n")
    args.markdown_out.write_text(markdown(report) + "\n")
    print(json.dumps({"build": build, "summary": measured["summary"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
