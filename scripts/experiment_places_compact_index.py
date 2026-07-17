#!/usr/bin/env python3
"""Build and benchmark a range-readable compact Places inverted index.

This is an offline architecture experiment, not a production shard builder.
The single-file artifact contains a small JSON directory followed by four
independently range-readable components:

* front-coded lexicon blocks keyed by ``field-token``;
* delta-varint postings carrying a quantized static rank byte;
* fixed-width record offsets; and
* compact result records (ID, display fields, coordinates, confidence).

The reader deliberately uses seek/read ranges and accounts for their union.
It never loads the complete artifact.  SQLite FTS5 built from the same rows is
used only as a retrieval comparator; agreement is not relevance ground truth.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sqlite3
import struct
import sys
import tempfile
import time
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


MAGIC = b"PCIX0001"
PREAMBLE = struct.Struct("<8sI")
FIELDS = ("name", "brand", "category", "context")
FIELD_IDS = {name: number for number, name in enumerate(FIELDS)}
FIELD_WEIGHTS = {"name": 8, "brand": 6, "category": 3, "context": 1}
TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
TOKENIZER_VERSION = "nfkd-latin-fold-cjk-bigram-v2"


def _is_latin(character: str) -> bool:
    return "LATIN" in unicodedata.name(character, "")


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0x3040 <= codepoint <= 0x30FF
        or 0x31F0 <= codepoint <= 0x31FF
        or 0xAC00 <= codepoint <= 0xD7AF
    )


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    folded = []
    last_base = ""
    for character in text:
        if unicodedata.combining(character):
            # Preserve Japanese voicing marks (dakuten/handakuten) and other
            # non-Latin distinctions, while retaining the established
            # accent-insensitive behavior for Latin names such as Café.
            if last_base and _is_latin(last_base):
                continue
        else:
            last_base = character
        folded.append(character)
    normalized = unicodedata.normalize("NFC", "".join(folded))
    return " ".join(TOKEN_RE.findall(normalized))


def tokens(value: Any) -> tuple[str, ...]:
    result = []
    for token in normalize(value).split():
        result.append(token)
        start = 0
        while start < len(token):
            if not _is_cjk(token[start]):
                start += 1
                continue
            end = start + 1
            while end < len(token) and _is_cjk(token[end]):
                end += 1
            run = token[start:end]
            if len(run) == 1:
                result.append(run)
            else:
                result.extend(run[index : index + 2] for index in range(len(run) - 1))
            start = end
    return tuple(dict.fromkeys(result))


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint cannot encode a negative value")
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            break
    raise ValueError("invalid or truncated varint")


def pack_text(value: str) -> bytes:
    raw = value.encode("utf-8")
    return encode_varint(len(raw)) + raw


def unpack_text(data: bytes, offset: int) -> tuple[str, int]:
    length, offset = decode_varint(data, offset)
    end = offset + length
    if end > len(data):
        raise ValueError("truncated text")
    return data[offset:end].decode("utf-8"), end


@dataclass(frozen=True)
class Place:
    place_id: str
    name: str
    brand: str
    category: str
    locality: str
    region: str
    country: str
    lat: float
    lon: float
    confidence: float
    # Alternate/common names (e.g. an English "Tokyo Tower" for a primary
    # "東京タワー"), indexed into the name field but never displayed. Defaulted
    # so existing positional/keyword constructions stay valid.
    alt_names: str = ""

    def field_text(self) -> dict[str, str]:
        # Index the primary name plus any alternate/common names so a query in
        # another language matches, while the displayed projection (see
        # encode_projection) keeps only the primary name.
        return {
            "name": " ".join(part for part in (self.name, self.alt_names) if part),
            "brand": self.brand,
            "category": self.category,
            "context": " ".join((self.locality, self.region, self.country)),
        }


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def place_from_row(row: dict[str, Any], row_number: int) -> Place:
    return Place(
        place_id=str(row.get("gers_id") or row.get("id") or f"__row_{row_number}"),
        name=str(row.get("primary_name") or row.get("name") or "").strip(),
        brand=str(row.get("brand_name") or "").strip(),
        category=str(
            row.get("category_primary")
            or row.get("category")
            or row.get("basic_category")
            or ""
        ).strip(),
        locality=str(row.get("locality") or row.get("city") or "").strip(),
        region=str(row.get("region") or "").strip(),
        country=str(row.get("country") or "").strip(),
        lat=_float(row.get("lat")),
        lon=_float(row.get("lon")),
        confidence=min(1.0, max(0.0, _float(row.get("confidence"), 0.5))),
        alt_names=str(row.get("alt_names") or "").strip(),
    )


def iter_rows(path: Path) -> Iterator[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as src:
            yield from csv.DictReader(src)
        return
    if suffix in {".jsonl", ".ndjson"}:
        with path.open(encoding="utf-8") as src:
            for line in src:
                if line.strip():
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("JSONL rows must be objects")
                    yield value
        return
    if suffix == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError("JSON input must be an array")
        yield from value
        return
    if suffix == ".parquet":
        try:
            import duckdb  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Parquet input requires duckdb") from exc
        connection = duckdb.connect()
        try:
            cursor = connection.execute("SELECT * FROM read_parquet(?)", [str(path)])
            columns = [item[0] for item in cursor.description]
            while rows := cursor.fetchmany(10_000):
                for row in rows:
                    yield dict(zip(columns, row))
        finally:
            connection.close()
        return
    raise ValueError(f"unsupported input extension: {suffix}")


def load_places(path: Path, limit: int | None = None) -> list[Place]:
    result = []
    for number, row in enumerate(iter_rows(path), 1):
        place = place_from_row(row, number)
        if place.name:
            result.append(place)
            if limit is not None and len(result) >= limit:
                break
    if not result:
        raise ValueError("input contains no named Places")
    return result


def encode_record(place: Place) -> bytes:
    fixed = struct.pack("<fff", place.lat, place.lon, place.confidence)
    try:
        packed_id = b"\x01" + uuid.UUID(place.place_id).bytes
    except (ValueError, AttributeError):
        packed_id = b"\x00" + pack_text(place.place_id)
    values = (
        place.name,
        place.brand,
        place.category,
        place.locality,
        place.region,
        place.country,
    )
    return fixed + packed_id + b"".join(pack_text(value) for value in values)


def decode_record(data: bytes) -> dict[str, Any]:
    if len(data) < 12:
        raise ValueError("truncated record")
    lat, lon, confidence = struct.unpack_from("<fff", data)
    offset = 12
    binary_id = data[offset]
    offset += 1
    if binary_id == 1:
        if offset + 16 > len(data):
            raise ValueError("truncated UUID")
        place_id = str(uuid.UUID(bytes=data[offset : offset + 16]))
        offset += 16
    elif binary_id == 0:
        place_id, offset = unpack_text(data, offset)
    else:
        raise ValueError("unknown record ID encoding")
    values = []
    for _ in range(6):
        value, offset = unpack_text(data, offset)
        values.append(value)
    return dict(
        zip(("name", "brand", "category", "locality", "region", "country"), values),
        id=place_id,
        lat=lat,
        lon=lon,
        confidence=confidence,
    )


def common_prefix(left: bytes, right: bytes) -> int:
    length = min(len(left), len(right))
    index = 0
    while index < length and left[index] == right[index]:
        index += 1
    return index


def build_artifact(
    places: list[Place], output: Path, block_entries: int = 128
) -> dict[str, Any]:
    started = time.perf_counter()
    posting_map: dict[tuple[int, str], list[int]] = defaultdict(list)
    for doc_id, place in enumerate(places):
        for field, text in place.field_text().items():
            for token in tokens(text):
                posting_map[(FIELD_IDS[field], token)].append(doc_id)

    keys = sorted(posting_map, key=lambda item: (item[0], item[1].encode("utf-8")))
    postings = bytearray()
    entries = []
    for field_id, token in keys:
        start = len(postings)
        previous = 0
        docs = posting_map[(field_id, token)]
        for index, doc_id in enumerate(docs):
            delta = doc_id if index == 0 else doc_id - previous
            postings += encode_varint(delta)
            postings.append(round(places[doc_id].confidence * 255))
            previous = doc_id
        entries.append((field_id, token, start, len(postings) - start, len(docs)))

    lexicon = bytearray()
    blocks = []
    for first in range(0, len(entries), block_entries):
        group = entries[first : first + block_entries]
        block_start = len(lexicon)
        lexicon += encode_varint(len(group))
        previous = b""
        for field_id, token, posting_offset, posting_length, count in group:
            key = bytes((field_id,)) + token.encode("utf-8")
            shared = common_prefix(previous, key)
            suffix = key[shared:]
            lexicon += encode_varint(shared) + encode_varint(len(suffix)) + suffix
            lexicon += encode_varint(posting_offset)
            lexicon += encode_varint(posting_length)
            lexicon += encode_varint(count)
            previous = key
        blocks.append(
            {
                "first": (bytes((group[0][0],)) + group[0][1].encode()).hex(),
                "last": (bytes((group[-1][0],)) + group[-1][1].encode()).hex(),
                "offset": block_start,
                "length": len(lexicon) - block_start,
                "entries": len(group),
            }
        )

    records = bytearray()
    offsets = [0]
    for place in places:
        records += encode_record(place)
        offsets.append(len(records))
    offset_bytes = b"".join(struct.pack("<Q", value) for value in offsets)

    component_data = {
        "lexicon": bytes(lexicon),
        "postings": bytes(postings),
        "record_offsets": offset_bytes,
        "records": bytes(records),
    }
    directory = {
        "schema_version": 1,
        "record_count": len(places),
        "field_ids": FIELD_IDS,
        "field_weights": FIELD_WEIGHTS,
        "lexicon_blocks": blocks,
        "components": {
            name: {"length": len(data)} for name, data in component_data.items()
        },
    }
    directory_bytes = json.dumps(
        directory, sort_keys=True, separators=(",", ":")
    ).encode()
    cursor = PREAMBLE.size + len(directory_bytes)
    for name in component_data:
        directory["components"][name]["offset"] = cursor
        cursor += len(component_data[name])
    # Offsets add digits to JSON, so stabilize directory length and offsets.
    for _ in range(8):
        directory_bytes = json.dumps(
            directory, sort_keys=True, separators=(",", ":")
        ).encode()
        cursor = PREAMBLE.size + len(directory_bytes)
        changed = False
        for name, data in component_data.items():
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
        for data in component_data.values():
            dst.write(data)

    elapsed = time.perf_counter() - started
    return {
        "build_seconds": elapsed,
        "places": len(places),
        "tokens": len(entries),
        "artifact_bytes": output.stat().st_size,
        "bytes_per_place": output.stat().st_size / len(places),
        "components": {
            "directory": PREAMBLE.size + len(directory_bytes),
            **{name: len(data) for name, data in component_data.items()},
        },
    }


class RangeReader:
    def __init__(self, path: Path):
        self.path = path
        self.ranges: list[tuple[int, int]] = []

    def read(self, offset: int, length: int) -> bytes:
        if length < 0 or offset < 0:
            raise ValueError("invalid range")
        with self.path.open("rb") as src:
            src.seek(offset)
            data = src.read(length)
        if len(data) != length:
            raise ValueError("short artifact range read")
        self.ranges.append((offset, offset + length))
        return data

    @property
    def unique_bytes(self) -> int:
        if not self.ranges:
            return 0
        total = 0
        start, end = sorted(self.ranges)[0]
        for next_start, next_end in sorted(self.ranges)[1:]:
            if next_start > end:
                total += end - start
                start, end = next_start, next_end
            else:
                end = max(end, next_end)
        return total + end - start

    def coalesced_plan(self, max_gap: int = 1024) -> tuple[int, int]:
        """Return optimistic request/byte counts after merging nearby ranges.

        This is a lower-bound plan computed after all offsets are known. A
        real reader still needs staged directory, lexicon, postings, and
        record requests, so the raw count remains the conservative metric.
        """
        if not self.ranges:
            return 0, 0
        requests = 1
        bytes_read = 0
        start, end = sorted(self.ranges)[0]
        for next_start, next_end in sorted(self.ranges)[1:]:
            if next_start > end + max_gap:
                bytes_read += end - start
                requests += 1
                start, end = next_start, next_end
            else:
                end = max(end, next_end)
        return requests, bytes_read + end - start


def decode_lexicon_block(data: bytes) -> list[tuple[int, str, int, int, int]]:
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
            (key[0], key[1:].decode(), posting_offset, posting_length, posting_count)
        )
        previous = key
    return result


def decode_postings(data: bytes, count: int) -> list[tuple[int, int]]:
    result = []
    offset = 0
    previous = 0
    for index in range(count):
        delta, offset = decode_varint(data, offset)
        doc_id = delta if index == 0 else previous + delta
        if offset >= len(data):
            raise ValueError("truncated posting rank")
        result.append((doc_id, data[offset]))
        offset += 1
        previous = doc_id
    if offset != len(data):
        raise ValueError("posting length mismatch")
    return result


class CompactIndex:
    def __init__(self, path: Path):
        self.reader = RangeReader(path)
        raw = self.reader.read(0, PREAMBLE.size)
        magic, length = PREAMBLE.unpack(raw)
        if magic != MAGIC:
            raise ValueError("not a Places compact index")
        self.directory = json.loads(self.reader.read(PREAMBLE.size, length))
        self._lexicon_cache: dict[
            tuple[int, int], list[tuple[int, str, int, int, int]]
        ] = {}

    def _component(self, name: str) -> tuple[int, int]:
        value = self.directory["components"][name]
        return value["offset"], value["length"]

    def lexicon_matches(
        self, field: str, token: str, prefix: bool
    ) -> list[tuple[int, str, int, int, int]]:
        target = bytes((FIELD_IDS[field],)) + token.encode()
        matches = []
        lexicon_base, _ = self._component("lexicon")
        for block in self.directory["lexicon_blocks"]:
            first, last = bytes.fromhex(block["first"]), bytes.fromhex(block["last"])
            relevant = first <= target <= last
            if prefix:
                upper = target + b"\xff"
                relevant = last >= target and first <= upper
            if not relevant:
                continue
            cache_key = (block["offset"], block["length"])
            decoded = self._lexicon_cache.get(cache_key)
            if decoded is None:
                data = self.reader.read(lexicon_base + block["offset"], block["length"])
                decoded = decode_lexicon_block(data)
                self._lexicon_cache[cache_key] = decoded
            for entry in decoded:
                entry_key = bytes((entry[0],)) + entry[1].encode()
                if entry_key == target or (prefix and entry_key.startswith(target)):
                    matches.append(entry)
        return matches

    def _postings(self, entry: tuple[int, str, int, int, int]) -> list[tuple[int, int]]:
        base, _ = self._component("postings")
        data = self.reader.read(base + entry[2], entry[3])
        return decode_postings(data, entry[4])

    def _record(self, doc_id: int) -> dict[str, Any]:
        offset_base, _ = self._component("record_offsets")
        pair = self.reader.read(offset_base + doc_id * 8, 16)
        start, end = struct.unpack("<QQ", pair)
        record_base, _ = self._component("records")
        return decode_record(self.reader.read(record_base + start, end - start))

    def search(
        self, query: str, limit: int = 10, prefix: bool = True
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        before_bytes = self.reader.unique_bytes
        before_ranges = len(self.reader.ranges)
        clauses = []
        for raw in query.strip().split():
            field = None
            value = raw
            if ":" in raw:
                candidate, value = raw.split(":", 1)
                if normalize(candidate) in FIELD_IDS:
                    field = normalize(candidate)
            for token in tokens(value):
                clauses.append((field, token))
        if not clauses:
            return [], {"unique_bytes": 0, "range_reads": 0, "candidates": 0}

        candidates: dict[int, tuple[int, int]] | None = None
        for requested_field, token in clauses:
            clause: dict[int, tuple[int, int]] = {}
            fields = (requested_field,) if requested_field else FIELDS
            for field in fields:
                for entry in self.lexicon_matches(field, token, prefix):
                    exact_bonus = 2 if entry[1] == token else 0
                    score = FIELD_WEIGHTS[field] + exact_bonus
                    for doc_id, rank in self._postings(entry):
                        prior_score, prior_rank = clause.get(doc_id, (0, rank))
                        clause[doc_id] = (prior_score + score, max(prior_rank, rank))
            if candidates is None:
                candidates = clause
            else:
                candidates = {
                    doc_id: (old[0] + clause[doc_id][0], max(old[1], clause[doc_id][1]))
                    for doc_id, old in candidates.items()
                    if doc_id in clause
                }
            if not candidates:
                break

        candidates = candidates or {}
        best = sorted(
            candidates, key=lambda doc: (-candidates[doc][0], -candidates[doc][1], doc)
        )[:limit]
        results = [self._record(doc_id) for doc_id in best]
        coalesced_reads, coalesced_bytes = self.reader.coalesced_plan()
        return results, {
            "unique_bytes": self.reader.unique_bytes - before_bytes,
            "range_reads": len(self.reader.ranges) - before_ranges,
            "candidates": len(candidates),
            "optimistic_coalesced_1k_range_reads": coalesced_reads,
            "optimistic_coalesced_1k_bytes": coalesced_bytes,
        }


def sqlite_comparator(places: list[Place]) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE places(id TEXT, name TEXT, brand TEXT, category TEXT, context TEXT, confidence REAL);
        CREATE VIRTUAL TABLE places_fts USING fts5(name, brand, category, context, content='places', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2', prefix='2 3 4');
        CREATE TRIGGER places_ai AFTER INSERT ON places BEGIN
          INSERT INTO places_fts(rowid,name,brand,category,context) VALUES(new.rowid,new.name,new.brand,new.category,new.context);
        END;
    """)
    connection.executemany(
        "INSERT INTO places VALUES(?,?,?,?,?,?)",
        (
            (
                p.place_id,
                p.name,
                p.brand,
                p.category,
                p.field_text()["context"],
                p.confidence,
            )
            for p in places
        ),
    )
    return connection


def sqlite_search(
    connection: sqlite3.Connection, query: str, limit: int = 10
) -> list[str]:
    query_tokens = normalize(query).split()
    if not query_tokens:
        return []
    match = " AND ".join(
        f'{{name brand category context}} : "{token}"*' for token in query_tokens
    )
    rows = connection.execute(
        """SELECT p.id FROM places_fts JOIN places p ON p.rowid=places_fts.rowid
           WHERE places_fts MATCH ? ORDER BY bm25(places_fts,8.0,6.0,3.0,1.0)-5.0*p.confidence LIMIT ?""",
        (match, limit),
    ).fetchall()
    return [row[0] for row in rows]


def generated_queries(places: list[Place], count: int) -> list[str]:
    queries = []
    seen = set()
    ranked = sorted(
        enumerate(places), key=lambda item: (-item[1].confidence, item[1].name, item[0])
    )
    for _, place in ranked:
        normalized = normalize(place.name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        queries.append(place.name)
        words = normalized.split()
        if len(words[-1]) >= 4:
            queries.append(" ".join(words[:-1] + [words[-1][:3]]))
        if len(queries) >= count:
            return queries[:count]
    return queries[:count]


def load_case_queries(path: Path | None) -> list[str]:
    if path is None:
        return []
    value = json.loads(path.read_text())
    rows = value.get("cases", []) if isinstance(value, dict) else value
    return [
        str(row["query"]) for row in rows if isinstance(row, dict) and row.get("query")
    ]


def benchmark(
    places: list[Place], artifact: Path, queries: list[str], top_k: int
) -> dict[str, Any]:
    sqlite_db = sqlite_comparator(places)
    cases = []
    try:
        for query in queries:
            # Model a cold independent query. This includes the two directory
            # ranges and avoids cross-query cache overlap making later queries
            # appear to touch zero bytes.
            compact = CompactIndex(artifact)
            directory_bytes = compact.reader.unique_bytes
            directory_reads = len(compact.reader.ranges)
            compact_results, io = compact.search(query, top_k)
            io["unique_bytes"] += directory_bytes
            io["range_reads"] += directory_reads
            compact_ids = [row["id"] for row in compact_results]
            sqlite_ids = sqlite_search(sqlite_db, query, top_k)
            overlap = len(set(compact_ids) & set(sqlite_ids))
            cases.append(
                {
                    "query": query,
                    "compact_ids": compact_ids,
                    "sqlite_ids": sqlite_ids,
                    "sqlite_top_k_recall": overlap / len(sqlite_ids)
                    if sqlite_ids
                    else None,
                    **io,
                }
            )
    finally:
        sqlite_db.close()
    scored = [
        case["sqlite_top_k_recall"]
        for case in cases
        if case["sqlite_top_k_recall"] is not None
    ]
    touched = sorted(case["unique_bytes"] for case in cases)
    return {
        "queries": cases,
        "summary": {
            "query_count": len(cases),
            "sqlite_nonempty_queries": len(scored),
            "mean_sqlite_top_k_recall": sum(scored) / len(scored) if scored else None,
            "min_sqlite_top_k_recall": min(scored) if scored else None,
            "query_bytes_touched_p50": touched[len(touched) // 2] if touched else 0,
            "query_bytes_touched_max": max(touched, default=0),
            "range_reads_max": max((case["range_reads"] for case in cases), default=0),
            "optimistic_coalesced_1k_range_reads_max": max(
                (case["optimistic_coalesced_1k_range_reads"] for case in cases),
                default=0,
            ),
            "optimistic_coalesced_1k_bytes_max": max(
                (case["optimistic_coalesced_1k_bytes"] for case in cases), default=0
            ),
        },
    }


def markdown(report: dict[str, Any]) -> str:
    build = report["build"]
    comparison = report["comparison"]
    trie_bytes = comparison.get("existing_trie_bytes")
    trie_baseline = (
        f"{trie_bytes:,} bytes" if trie_bytes is not None else "not available"
    )
    lines = [
        "# Places compact-index experiment",
        "",
        "This is a bounded range-read architecture spike, not a production size forecast.",
        "",
        f"- Input: `{report['input']}` ({build['places']:,} named Places)",
        f"- Artifact: {build['artifact_bytes']:,} bytes ({build['bytes_per_place']:.1f} bytes/place)",
        f"- Build: {build['build_seconds']:.3f} seconds; {build['tokens']:,} field/token keys",
        f"- SQLite FTS comparator: {comparison['sqlite_bytes']:,} bytes",
        f"- Existing unproven trie baseline: {trie_baseline}",
        f"- Linear shape only: {report['linear_shape_extrapolation']['one_million_places_bytes']:,} bytes at 1M Places; {report['linear_shape_extrapolation']['seventy_five_million_places_bytes']:,} bytes at 75M",
    ]
    historical = comparison.get("existing_fixture_baselines", {})
    if historical:
        lines += [
            f"- Historical full region SQLite: {historical.get('region', 0):,} bytes",
            f"- Historical prefix/minimal SQLite: {historical.get('prefix_only', 0):,} / {historical.get('minimal', 0):,} bytes",
        ]
    lines += [
        "",
        "## Components",
        "",
        "| component | bytes | bytes/place |",
        "|---|---:|---:|",
    ]
    for name, size in build["components"].items():
        lines.append(f"| {name} | {size:,} | {size / build['places']:.1f} |")
    summary = report["benchmark"]["summary"]
    lines += [
        "",
        "## Retrieval comparison",
        "",
        "Recall below is overlap with SQLite's top-k, not human-labelled relevance.",
        "",
        f"- Mean SQLite top-k recall: {summary['mean_sqlite_top_k_recall']}",
        f"- Minimum SQLite top-k recall: {summary['min_sqlite_top_k_recall']}",
        f"- Query bytes touched p50/max: {summary['query_bytes_touched_p50']:,} / {summary['query_bytes_touched_max']:,}",
        f"- Maximum simulated range reads: {summary['range_reads_max']}",
        f"- Optimistic 1 KiB-gap coalesced max ranges/bytes: {summary['optimistic_coalesced_1k_range_reads_max']} / {summary['optimistic_coalesced_1k_bytes_max']:,}",
        "",
        "| query | recall | candidates | bytes touched | raw ranges | coalesced ranges/bytes |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for case in report["benchmark"]["queries"]:
        recall = (
            "n/a"
            if case["sqlite_top_k_recall"] is None
            else f"{case['sqlite_top_k_recall']:.2f}"
        )
        lines.append(
            f"| {case['query']} | {recall} | {case['candidates']} | {case['unique_bytes']:,} | {case['range_reads']} | {case['optimistic_coalesced_1k_range_reads']} / {case['optimistic_coalesced_1k_bytes']:,} |"
        )
    lines += [
        "",
        "## Interpretation",
        "",
        f"**Verdict: {report['verdict']}**",
        "",
        "The old 68.7 B/place radix trie was keyed only by normalized full primary name and returned only ID plus coordinates. It had no token search, aliases, category/context fields, ranking, display names, or independently addressable blocks; it is therefore a size floor, not a like-for-like competitor.",
        "",
        "The new artifact proves the richer storage layout can be range-addressed, but does not yet prove it should replace SQLite. Raw ranges are conservative seek/read calls; the 1 KiB coalesced column is an optimistic lower bound calculated only after every offset is known and may overfetch bytes. A real remote reader needs staged directory, lexicon, postings, and record requests. A high range-count or low SQLite overlap is a failure signal, even if total bytes are attractive. The extrapolations above are deliberately linear shape calculations only; token fanout, language coverage, and source distributions can change bytes/place materially.",
        "",
    ]
    return "\n".join(lines)


def sqlite_file_size(places: list[Place]) -> int:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "places.db"
        connection = sqlite3.connect(path)
        connection.executescript("""
            CREATE TABLE places(id TEXT, name TEXT, brand TEXT, category TEXT, context TEXT, confidence REAL);
            CREATE VIRTUAL TABLE places_fts USING fts5(name,brand,category,context,content='places',content_rowid='rowid',tokenize='unicode61 remove_diacritics 2',prefix='2 3 4');
        """)
        connection.executemany(
            "INSERT INTO places VALUES(?,?,?,?,?,?)",
            (
                (
                    p.place_id,
                    p.name,
                    p.brand,
                    p.category,
                    p.field_text()["context"],
                    p.confidence,
                )
                for p in places
            ),
        )
        connection.execute("INSERT INTO places_fts(places_fts) VALUES('rebuild')")
        connection.commit()
        connection.execute("VACUUM")
        connection.close()
        return path.stat().st_size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--query-count", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--block-entries", type=int, default=128)
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")

    places = load_places(args.input, args.limit)
    build = build_artifact(places, args.artifact, args.block_entries)
    queries = load_case_queries(args.cases)
    queries += generated_queries(places, max(0, args.query_count - len(queries)))
    queries = list(dict.fromkeys(queries))[: args.query_count]
    result = benchmark(places, args.artifact, queries, args.top_k)
    trie = Path("exports/experiment/trie/places-bbox.trie")
    comparison = {
        "sqlite_bytes": sqlite_file_size(places),
        "existing_trie_bytes": trie.stat().st_size if trie.is_file() else None,
        "existing_trie_provenance": "commit 5b85a52 scripts/experiment_places_addresses.py; full-primary-name radix prefix only",
    }
    metrics_path = Path("exports/experiment/metrics.json")
    if metrics_path.is_file() and args.input == Path(
        "exports/experiment/places-raw.parquet"
    ):
        historical = json.loads(metrics_path.read_text())["places"]["shards"]
        comparison["existing_fixture_baselines"] = {
            name: historical[name]["size_bytes"]
            for name in (
                "region",
                "tiered_index",
                "tiered_detail",
                "prefix_only",
                "minimal",
                "trie",
            )
        }
    report = {
        "schema_version": 1,
        "input": str(args.input),
        "limitations": [
            "bounded fixture; no global byte forecast",
            "SQLite top-k overlap is not labelled relevance",
            "range count is simulated and does not include HTTP/R2 latency",
            "no multilingual aliases unless present in flattened input fields",
        ],
        "build": build,
        "comparison": comparison,
        "benchmark": result,
        "verdict": "do not replace SQLite yet; byte shape is promising, but remote range fanout and top-k ranking divergence need another design iteration",
        "linear_shape_extrapolation": {
            "warning": "not a forecast; fixture density, token fanout, languages, and field coverage are unrepresentative",
            "one_million_places_bytes": round(build["bytes_per_place"] * 1_000_000),
            "seventy_five_million_places_bytes": round(
                build["bytes_per_place"] * 75_000_000
            ),
        },
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2) + "\n")
    args.markdown_out.write_text(markdown(report) + "\n")
    print(
        json.dumps(
            {"artifact": str(args.artifact), **build, **result["summary"]}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
