#!/usr/bin/env python3
"""Repack the packed global head into one range-readable object and measure it.

The locality-head spike published the global top-k head as ~4,088 deterministic
64 KiB hash-bucket objects (25.1 MB on its 1M California sample). Every lookup
reads a whole bucket, so a hit transfers all the unrelated keys that hash to the
same bucket. This experiment reuses that machinery by import
(``ordered_places`` + ``build_heads`` reproduce the exact head; ``PackedHeadStore``
reproduces the exact bucket baseline) and repacks the same heads into ONE object:

* a sorted internal key index (term -> entry offset/length); followed by
* the concatenated per-key head entries (framed top-k result projections).

It then models reads/bytes per query for three readers on the same query set the
original head experiment used (exact ``starbucks``, long prefix ``starbu*``, the
ineligible cases that never probe the head, and an eligible-shaped miss):

  (i)   the 4,088-object bucket baseline (one whole-bucket read per lookup);
  (ii)  the single object with a resident/cached key index (one entry read); and
  (iii) the single object cold (key-index read + entry read).

This is an offline architecture spike. No Cloudflare resource is read or written.
All linearizations are labelled diagnostics; no latency is claimed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from experiment_places_compact_index import (  # noqa: E402
    Place,
    decode_record,
    decode_varint,
    encode_record,
    encode_varint,
    load_places,
    normalize,
)
from experiment_places_compact_shard import posting_map  # noqa: E402
from experiment_places_kv_r2_pages import CASES, Clause  # noqa: E402
from experiment_places_locality_head import (  # noqa: E402
    PackedHeadStore,
    build_heads,
    famous_pair_token_key,
    ordered_places,
)


MAGIC = b"PHRP0001"
PREAMBLE = struct.Struct("<8sI")

# Match the locality-head defaults so the reproduced head is identical to the
# object the locality-head spike published.
CELL_DEGREES = 0.25
HEAD_TARGET = 64 * 1024
HEAD_BUCKET_COUNT = 4096
HEAD_MINIMUM_CANDIDATES = 64
HEAD_LIMIT = 10
# Famous-unique admission is off by default so the historical spike objects
# stay byte-for-byte reproducible (a cap of 0 also omits the famous provenance
# fields from the directory); callers opt in with an explicit cap.
HEAD_FAMOUS_CAP = 0
HEAD_ADMISSION_MARKER = "famous-unique-v1"
HEAD_KEY_FAMILIES = ("e", "e2", "p")
# Mirrors of the Rust reader's hard caps (crates/geocoder-worker/src/
# places_pages.rs: MAX_HEAD_KEYS, MAX_HEAD_INDEX_BYTES, MAX_HEAD_ENTRY_BYTES,
# MAX_TOKEN_BYTES). The builder fails a build that the reader would reject so
# an over-cap famous configuration surfaces at build time, not as a serve-time
# outage of every head-eligible query.
READER_MAX_HEAD_KEYS = 100_000
READER_MAX_HEAD_INDEX_BYTES = 1024 * 1024
READER_MAX_HEAD_ENTRY_BYTES = 128 * 1024
READER_MAX_KEY_BYTES = 4096


def head_key(clause: Clause) -> str | None:
    """Return the head lookup key for an eligible single exact/prefix clause."""
    if clause.field is not None:
        return None
    value = normalize(clause.value)
    if not value:
        return None
    return f"{'p' if clause.prefix else 'e'}:{value}"


def famous_pair_key(clauses: tuple[Clause, ...]) -> str | None:
    """Return the ``e2:`` famous-pair key for two exact unfielded clauses.

    The two normalized tokens are joined in ascending order, matching the
    builder's ``a < b`` pair emission. Queries with any other shape — or two
    identical tokens, which the builder never emits — return ``None``. The Rust
    Worker reader constructs the identical key; the smoke's producer-oracle
    equality enforces the lockstep.
    """
    if len(clauses) != 2 or any(
        clause.field is not None or clause.prefix for clause in clauses
    ):
        return None
    low, high = sorted(normalize(clause.value) for clause in clauses)
    if not low or low == high:
        return None
    return famous_pair_token_key(low, high)


# --------------------------------------------------------------------------
# Pure encoding logic (hermetic; unit-tested without any I/O).
# --------------------------------------------------------------------------


def encode_head_entry(records: list[bytes]) -> bytes:
    """Frame concatenated top-k result projections for one head key.

    This is byte-identical to the per-key payload PackedHeadStore packs into its
    bucket objects, so the repack stores the same head content, only relaid out.
    """
    return b"".join(encode_varint(len(record)) + record for record in records)


def decode_head_entry(data: bytes) -> list[dict[str, Any]]:
    """Decode a framed head entry back into result-projection dicts."""
    results = []
    offset = 0
    while offset < len(data):
        length, offset = decode_varint(data, offset)
        end = offset + length
        if end > len(data):
            raise ValueError("truncated head record")
        results.append(decode_record(data[offset:end]))
        offset = end
    return results


def encode_key_index(entries: list[tuple[str, int, int]]) -> bytes:
    """Encode a sorted term -> (offset, length) index.

    Entries must be sorted by key. Each is varint(key_len) + key +
    varint(offset) + varint(length). The whole index is small enough to keep
    resident; a cold reader fetches it once and decodes it in full.
    """
    previous = ""
    out = bytearray()
    for key, offset, length in entries:
        if key < previous:
            raise ValueError("key index entries must be sorted")
        raw = key.encode("utf-8")
        out += encode_varint(len(raw)) + raw
        out += encode_varint(offset)
        out += encode_varint(length)
        previous = key
    return bytes(out)


def decode_key_index(data: bytes) -> dict[str, tuple[int, int]]:
    """Decode the sorted key index into a resident term -> (offset, length) map."""
    index: dict[str, tuple[int, int]] = {}
    offset = 0
    while offset < len(data):
        key_len, offset = decode_varint(data, offset)
        end = offset + key_len
        if end > len(data):
            raise ValueError("truncated key")
        key = data[offset:end].decode("utf-8")
        offset = end
        entry_offset, offset = decode_varint(data, offset)
        entry_length, offset = decode_varint(data, offset)
        index[key] = (entry_offset, entry_length)
    return index


# --------------------------------------------------------------------------
# Head reproduction + single-object repack.
# --------------------------------------------------------------------------


def build_heads_and_baseline(
    places: list[Place],
    *,
    release: str = "fixture-current",
    cell_degrees: float = CELL_DEGREES,
    head_target: int = HEAD_TARGET,
    head_bucket_count: int = HEAD_BUCKET_COUNT,
    head_minimum_candidates: int = HEAD_MINIMUM_CANDIDATES,
    head_limit: int = HEAD_LIMIT,
    head_famous_cap: int = HEAD_FAMOUS_CAP,
    preserve_input_order: bool = False,
) -> tuple[list[Place], dict[str, list[int]], PackedHeadStore]:
    """Reproduce the locality-head object exactly, without its cell/posting tiers.

    ``ordered_places`` + ``posting_map`` + ``build_heads`` reproduce
    ``LocalityHeadIndex.heads`` and ``PackedHeadStore`` reproduces
    ``LocalityHeadIndex.head_store`` bit-for-bit (see the equality test), while
    skipping the expensive base page index the repack does not need.
    """
    # A multi-shard caller may already have established the serving order
    # (shard index, then local document ID). Preserve it so packed-head and
    # fallback top-k use the same deterministic tie-breaker.
    ordered = (
        list(places) if preserve_input_order else ordered_places(places, cell_degrees)
    )
    exact = posting_map(ordered)
    heads = build_heads(
        ordered, exact, head_minimum_candidates, head_limit, head_famous_cap
    )
    baseline = PackedHeadStore(release, ordered, heads, head_target, head_bucket_count)
    return ordered, heads, baseline


def key_family(key: str) -> str:
    """Return the key-family prefix (``e``, ``e2``, or ``p``) for one head key."""
    return key.split(":", 1)[0]


def build_repack_object(
    ordered: list[Place],
    heads: dict[str, list[int]],
    output: Path,
    *,
    head_famous_cap: int = HEAD_FAMOUS_CAP,
) -> dict[str, Any]:
    """Write the single range-readable head object and return its metadata."""
    started = time.perf_counter()
    entries_blob = bytearray()
    key_entries: list[tuple[str, int, int]] = []
    entry_sizes: list[int] = []
    family_key_counts = dict.fromkeys(HEAD_KEY_FAMILIES, 0)
    family_entry_bytes = dict.fromkeys(HEAD_KEY_FAMILIES, 0)
    for key in sorted(heads):
        records = [encode_record(ordered[doc]) for doc in heads[key]]
        entry = encode_head_entry(records)
        key_entries.append((key, len(entries_blob), len(entry)))
        entry_sizes.append(len(entry))
        entries_blob += entry
        family = key_family(key)
        family_key_counts[family] += 1
        family_entry_bytes[family] += len(entry)

    key_index = encode_key_index(key_entries)
    oversized_keys = [
        key
        for key, _, _ in key_entries
        if len(key.encode("utf-8")) > READER_MAX_KEY_BYTES
    ]
    if (
        len(key_entries) > READER_MAX_HEAD_KEYS
        or len(key_index) > READER_MAX_HEAD_INDEX_BYTES
        or (entry_sizes and max(entry_sizes) > READER_MAX_HEAD_ENTRY_BYTES)
        or oversized_keys
    ):
        raise ValueError(
            "packed head exceeds the reader's hard caps "
            f"(keys={len(key_entries)}, key_index_bytes={len(key_index)}, "
            f"max_entry_bytes={max(entry_sizes, default=0)}, "
            f"oversized_keys={len(oversized_keys)}); "
            "shrink head_famous_cap or raise the reader caps in lockstep"
        )
    components = {"key_index": bytes(key_index), "entries": bytes(entries_blob)}
    directory = {
        "schema_version": 1,
        "magic": MAGIC.decode(),
        "key_count": len(key_entries),
        "head_limit": HEAD_LIMIT,
        "components": {
            name: {"length": len(data)} for name, data in components.items()
        },
    }
    if head_famous_cap > 0:
        # Additive famous provenance, only on famous-enabled builds so a cap-0
        # build stays byte-identical to the historical spike objects and the
        # marker actually distinguishes famous-admitted objects.
        directory["head_famous_cap"] = head_famous_cap
        directory["e2_key_count"] = family_key_counts["e2"]
        directory["admission"] = HEAD_ADMISSION_MARKER
    # Stabilize the JSON directory length and the component offsets it stores.
    for _ in range(8):
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
        raise RuntimeError("head object directory offsets did not stabilize")
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
    distribution = {
        "count": len(entry_sizes),
        "min": min(entry_sizes) if entry_sizes else 0,
        "max": max(entry_sizes) if entry_sizes else 0,
        "mean": statistics.mean(entry_sizes) if entry_sizes else 0,
        "median": statistics.median(entry_sizes) if entry_sizes else 0,
        "p90": (
            statistics.quantiles(entry_sizes, n=10)[8]
            if len(entry_sizes) >= 10
            else max(entry_sizes, default=0)
        ),
        "total": sum(entry_sizes),
    }
    return {
        "build_seconds": elapsed,
        "object_bytes": size,
        "objects": 1,
        "preamble_bytes": PREAMBLE.size,
        "directory_bytes": len(directory_bytes),
        "key_index_bytes": len(key_index),
        "entries_bytes": len(entries_blob),
        "key_count": len(key_entries),
        "head_famous_cap": head_famous_cap,
        "key_counts_by_family": family_key_counts,
        "entry_bytes_by_family": family_entry_bytes,
        "entry_size_distribution": distribution,
    }


class RepackHead:
    """Range reader over the single repacked head object."""

    def __init__(self, path: Path):
        self.path = path
        preamble = self._read(0, PREAMBLE.size)
        magic, length = PREAMBLE.unpack(preamble)
        if magic != MAGIC:
            raise ValueError("not a repacked Places head")
        self.directory = json.loads(self._read(PREAMBLE.size, length))
        self.directory_bytes = PREAMBLE.size + length
        self._resident_index: dict[str, tuple[int, int]] | None = None

    def _read(self, offset: int, length: int) -> bytes:
        with self.path.open("rb") as src:
            src.seek(offset)
            data = src.read(length)
        if len(data) != length:
            raise ValueError("short head range read")
        return data

    def component(self, name: str) -> tuple[int, int]:
        entry = self.directory["components"][name]
        return entry["offset"], entry["length"]

    def load_resident_index(self) -> dict[str, tuple[int, int]]:
        offset, length = self.component("key_index")
        self._resident_index = decode_key_index(self._read(offset, length))
        return self._resident_index

    def query_resident(self, key: str) -> dict[str, Any]:
        """(ii) Key index assumed resident/cached; one entry read on a hit."""
        if self._resident_index is None:
            self.load_resident_index()
        assert self._resident_index is not None
        entries_base, _ = self.component("entries")
        located = self._resident_index.get(key)
        if located is None:
            return {"hit": False, "reads": 0, "bytes": 0, "result_ids": []}
        offset, length = located
        data = self._read(entries_base + offset, length)
        return {
            "hit": True,
            "reads": 1,
            "bytes": length,
            "result_ids": [row["id"] for row in decode_head_entry(data)],
        }

    def query_cold(self, key: str) -> dict[str, Any]:
        """(iii) Cold: read the whole key index, then the entry."""
        index_offset, index_length = self.component("key_index")
        index = decode_key_index(self._read(index_offset, index_length))
        entries_base, _ = self.component("entries")
        located = index.get(key)
        if located is None:
            return {
                "hit": False,
                "reads": 1,
                "bytes": index_length,
                "result_ids": [],
            }
        offset, length = located
        data = self._read(entries_base + offset, length)
        return {
            "hit": True,
            "reads": 2,
            "bytes": index_length + length,
            "result_ids": [row["id"] for row in decode_head_entry(data)],
        }


class BucketBaseline:
    """(i) Model the 4,088-object bucket head: one whole-bucket read per lookup."""

    def __init__(self, store: PackedHeadStore):
        self.store = store
        self.page_by_bucket: dict[int, Any] = {}
        self.bucket_sizes: list[int] = [page.size for page in store.pages]
        for key, page in store.pages_by_key.items():
            self.page_by_bucket[self._bucket(key)] = page

    def _bucket(self, key: str) -> int:
        return (
            int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
            % self.store.bucket_count
        )

    def lookup(self, key: str) -> dict[str, Any]:
        page = self.page_by_bucket.get(self._bucket(key))
        hit = key in self.store.pages_by_key
        if page is None:
            # No bucket object exists for that hash slot.
            return {"hit": hit, "reads": 0, "bytes": 0}
        return {"hit": hit, "reads": 1, "bytes": page.size}


@dataclass(frozen=True)
class HeadQuery:
    name: str
    clause: Clause
    kind: str  # "hit", "eligible_miss", or "ineligible"


def query_set(heads: dict[str, list[int]]) -> list[HeadQuery]:
    """Reproduce the head experiment's query set plus one eligible-shaped miss."""
    queries: list[HeadQuery] = []
    for case in CASES:
        if len(case.clauses) != 1:
            queries.append(HeadQuery(case.name, case.clauses[0], "ineligible"))
            continue
        clause = case.clauses[0]
        key = head_key(clause)
        if key is None:
            queries.append(HeadQuery(case.name, clause, "ineligible"))
        elif key in heads:
            queries.append(HeadQuery(case.name, clause, "hit"))
        else:
            queries.append(HeadQuery(case.name, clause, "eligible_miss"))
    # A deterministic eligible-shaped exact term that is absent from the head.
    miss_clause = Clause("zzunlikelyheadtoken")
    assert head_key(miss_clause) not in heads
    queries.append(HeadQuery("absent_exact_miss", miss_clause, "eligible_miss"))
    return queries


def measure(
    heads: dict[str, list[int]],
    baseline: BucketBaseline,
    reader: RepackHead,
    object_meta: dict[str, Any],
) -> dict[str, Any]:
    reader.load_resident_index()
    rows = []
    for query in query_set(heads):
        key = head_key(query.clause)
        row: dict[str, Any] = {
            "name": query.name,
            "kind": query.kind,
            "value": query.clause.value,
            "prefix": query.clause.prefix,
            "field": query.clause.field,
            "head_key": key,
        }
        if query.kind == "ineligible":
            # The head is never probed; the query goes to the posting fallback.
            row["note"] = "not a single unfielded clause; head not consulted"
            rows.append(row)
            continue
        assert key is not None
        candidates = len(heads.get(key, []))
        base = baseline.lookup(key)
        resident = reader.query_resident(key)
        cold = reader.query_cold(key)
        row.update(
            {
                "head_candidates": candidates,
                "baseline_4090obj": {"reads": base["reads"], "bytes": base["bytes"]},
                "single_resident": {
                    "reads": resident["reads"],
                    "bytes": resident["bytes"],
                },
                "single_cold": {"reads": cold["reads"], "bytes": cold["bytes"]},
                "results_agree_resident_vs_cold": (
                    resident["result_ids"] == cold["result_ids"]
                ),
            }
        )
        if query.kind == "hit":
            row["overfetch_ratio_baseline_vs_resident"] = (
                base["bytes"] / resident["bytes"] if resident["bytes"] else None
            )
        rows.append(row)

    hits = [r for r in rows if r["kind"] == "hit"]
    return {
        "queries": rows,
        "summary": {
            "hit_query_count": len(hits),
            "baseline_max_reads": max(
                (r["baseline_4090obj"]["reads"] for r in hits), default=0
            ),
            "baseline_max_bytes": max(
                (r["baseline_4090obj"]["bytes"] for r in hits), default=0
            ),
            "resident_max_reads": max(
                (r["single_resident"]["reads"] for r in hits), default=0
            ),
            "resident_max_bytes": max(
                (r["single_resident"]["bytes"] for r in hits), default=0
            ),
            "cold_max_reads": max((r["single_cold"]["reads"] for r in hits), default=0),
            "cold_max_bytes": max((r["single_cold"]["bytes"] for r in hits), default=0),
            "mean_overfetch_ratio_baseline_vs_resident": (
                statistics.mean(
                    [r["overfetch_ratio_baseline_vs_resident"] for r in hits]
                )
                if hits
                else None
            ),
        },
    }


def build_report(
    places: list[Place],
    output: Path,
    *,
    input_path: str,
    input_sha256: str | None,
    head_famous_cap: int = HEAD_FAMOUS_CAP,
) -> dict[str, Any]:
    started = time.perf_counter()
    ordered, heads, store = build_heads_and_baseline(
        places, head_famous_cap=head_famous_cap
    )
    object_meta = build_repack_object(
        ordered, heads, output, head_famous_cap=head_famous_cap
    )
    baseline = BucketBaseline(store)
    reader = RepackHead(output)
    measured = measure(heads, baseline, reader, object_meta)

    baseline_total_bytes = sum(page.size for page in store.pages)
    bucket_sizes = baseline.bucket_sizes
    return {
        "schema_version": 1,
        "input": input_path,
        "input_sha256": input_sha256,
        "wall_seconds": time.perf_counter() - started,
        "input_places": len(places),
        "head_parameters": {
            "cell_degrees": CELL_DEGREES,
            "head_target_bytes": HEAD_TARGET,
            "head_bucket_count_seed": HEAD_BUCKET_COUNT,
            "head_minimum_candidates": HEAD_MINIMUM_CANDIDATES,
            "head_limit": HEAD_LIMIT,
            "head_famous_cap": head_famous_cap,
        },
        "baseline_bucket_head": {
            "objects": len(store.pages),
            "bytes": baseline_total_bytes,
            "bytes_per_place": baseline_total_bytes / len(places),
            "final_bucket_count": store.bucket_count,
            "keys": len(heads),
            "bucket_size_distribution": {
                "min": min(bucket_sizes) if bucket_sizes else 0,
                "max": max(bucket_sizes) if bucket_sizes else 0,
                "mean": statistics.mean(bucket_sizes) if bucket_sizes else 0,
                "median": statistics.median(bucket_sizes) if bucket_sizes else 0,
            },
        },
        "single_object_head": object_meta,
        "reads_model": {
            "baseline_4090obj": "hash key -> one bucket object; a hit or a miss both read the whole bucket (all keys hashing there)",
            "single_resident": "release manifest resolves the object layout and the key index is cached; a hit is one entry read, a miss is zero reads",
            "single_cold": "read the whole key index once, then the entry; a miss stops after the index read. Excludes the object's own 12-byte preamble + "
            f"{object_meta['directory_bytes']}-byte JSON directory, assumed resolved from the resident edge-cached release manifest",
        },
        "benchmark": measured,
    }


def markdown(report: dict[str, Any]) -> str:
    baseline = report["baseline_bucket_head"]
    single = report["single_object_head"]
    dist = single["entry_size_distribution"]
    summary = report["benchmark"]["summary"]
    overfetch = summary["mean_overfetch_ratio_baseline_vs_resident"]
    lines = [
        "# Places global-head single-object repack spike",
        "",
        f"- Input: `{report['input']}` ({report['input_places']:,} Places)",
    ]
    if report["input_sha256"]:
        lines.append(f"- Input SHA-256: `{report['input_sha256']}`")
    lines += [
        f"- Baseline packed head: {baseline['objects']:,} objects / {baseline['bytes']:,} bytes "
        f"({baseline['bytes_per_place']:.2f} B/place), {baseline['keys']:,} keys",
        f"- Single-object repack: 1 object / {single['object_bytes']:,} bytes "
        f"(key index {single['key_index_bytes']:,} B + entries {single['entries_bytes']:,} B "
        f"+ directory {single['directory_bytes']:,} B)",
        f"- Entry sizes: min {dist['min']:,} / median {dist['median']:.0f} / mean {dist['mean']:.0f} "
        f"/ p90 {dist['p90']:.0f} / max {dist['max']:,} bytes",
        (
            f"- Mean hit overfetch, bucket vs resident-entry: {overfetch:.1f}x"
            if overfetch
            else "- Mean hit overfetch: n/a"
        ),
        "",
        "## Provenance and measured vs modeled",
        "",
        "This re-extracts the California 1M sample with the deterministic extractor "
        "(`ORDER BY id` before `LIMIT`), so it supersedes the earlier locality-head "
        "spike's non-deterministic sample (which pinned ~4,088 head objects / 25.1 MB). "
        f"The re-extracted sample reproduces {baseline['objects']:,} head objects / "
        f"{baseline['bytes'] / 1e6:.1f} MB via the same `build_heads` + `PackedHeadStore` "
        "machinery.",
        "",
        "- **Measured** (real bytes on disk): the baseline object inventory, the single "
        "object and its component sizes, and the entry-size distribution.",
        "- **Modeled** (range-read accounting, no network, no latency): the per-query "
        "reads/bytes below.",
        "",
        "## Reads / bytes per query (diagnostic model; no latency measured)",
        "",
        f"| query | kind | head candidates | (i) bucket baseline ({baseline['objects']:,} obj) | (ii) single resident | (iii) single cold |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in report["benchmark"]["queries"]:
        if row["kind"] == "ineligible":
            lines.append(
                f"| {row['name']} | ineligible | n/a | head not consulted | head not consulted | head not consulted |"
            )
            continue
        base = row["baseline_4090obj"]
        res = row["single_resident"]
        cold = row["single_cold"]
        lines.append(
            f"| {row['name']} | {row['kind']} | {row['head_candidates']} | "
            f"{base['reads']} / {base['bytes']:,} B | "
            f"{res['reads']} / {res['bytes']:,} B | "
            f"{cold['reads']} / {cold['bytes']:,} B |"
        )
    lines += [
        "",
        "## Reads model",
        "",
        f"- (i) baseline: {report['reads_model']['baseline_4090obj']}",
        f"- (ii) resident: {report['reads_model']['single_resident']}",
        f"- (iii) cold: {report['reads_model']['single_cold']}",
        "",
        "## Implications for the shared-reader prototype",
        "",
        "These are reads/bytes shapes only; nothing was measured over a network and no latency is claimed.",
        "",
        f"- The bucket baseline reads a whole {baseline['bucket_size_distribution']['mean']:.0f} B-average bucket "
        f"(up to {baseline['bucket_size_distribution']['max']:,} B) for every hit, carrying unrelated co-hashed keys. "
        "The single-object repack with a resident index reads only the matched entry, so a hit transfers the entry "
        f"(median {dist['median']:.0f} B) at 1 read.",
        "- A cold reader pays one key-index read before the entry. The whole key index is "
        f"{single['key_index_bytes']:,} B; if that is too large to fetch cold on the first query, a block/offset "
        "directory over the key index would let a cold hit read only one index block plus the entry, still 2 reads.",
        "- On a miss, the bucket baseline still reads a whole bucket, while a resident-index reader answers a miss "
        "with zero object reads and a cold reader stops after the index read. The single object collapses the head "
        "from thousands of objects to one, which the shared reader can range-read exactly like a shard.",
        "",
        "## Reproduction",
        "",
        "```bash",
        "python scripts/factory_extract_places.py \\",
        "  --release 2026-06-17.0 --limit 1000000 --output exports/places-ca-1m.parquet",
        "",
        "python scripts/experiment_places_head_repack.py \\",
        "  exports/places-ca-1m.parquet \\",
        "  --object-out artifacts/places-ca-head.repack \\",
        "  --json-out benchmarks/places-head-repack-report.json \\",
        "  --markdown-out benchmarks/places-head-repack-report.md",
        "```",
        "",
        f"Head reproduction + baseline + repack ran in ~{report['wall_seconds']:.0f} s wall "
        "at ~1.9 GiB peak RSS (`/usr/bin/time -l`), one core.",
        "",
    ]
    return "\n".join(lines)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--object-out", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--head-famous-cap", type=int, default=HEAD_FAMOUS_CAP)
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive")
    if args.head_famous_cap < 0:
        parser.error("--head-famous-cap cannot be negative")
    places = load_places(args.input, args.limit)
    input_sha = sha256_file(args.input) if args.input.is_file() else None
    report = build_report(
        places,
        args.object_out,
        input_path=str(args.input),
        input_sha256=input_sha,
        head_famous_cap=args.head_famous_cap,
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2) + "\n")
    args.markdown_out.write_text(markdown(report) + "\n")
    print(
        json.dumps(
            {
                "summary": report["benchmark"]["summary"],
                "single_object_head": report["single_object_head"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
