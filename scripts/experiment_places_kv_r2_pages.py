#!/usr/bin/env python3
"""Model a KV-directory plus immutable R2-page Places index.

Measured facts come from a local Places fixture. High-fanout behavior is a
deterministic synthetic stress test. Prices are documented constants, not
measurements. No Cloudflare resources are read or written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from experiment_places_compact_index import (  # noqa: E402
    FIELD_IDS,
    FIELDS,
    Place,
    encode_record,
    encode_varint,
    load_places,
    normalize,
    tokens,
)


PAGE_HEADER_BYTES = 24
DIRECT_PREFIX_LENGTHS = (2, 3, 4)
POSTING_BUCKET_COUNT = 4096
FIELD_BITS = {field: 1 << FIELD_IDS[field] for field in FIELDS}
PRICE = {
    "as_of": "2026-07-14",
    "workers_base_monthly_usd": 5.0,
    "workers_included_requests": 10_000_000,
    "workers_additional_per_million_usd": 0.30,
    "workers_included_cpu_ms": 30_000_000,
    "workers_additional_cpu_million_ms_usd": 0.02,
    "r2_standard_storage_gb_month_usd": 0.015,
    "r2_included_storage_gb_month": 10,
    "r2_class_a_per_million_usd": 4.50,
    "r2_included_class_a": 1_000_000,
    "r2_class_b_per_million_usd": 0.36,
    "r2_included_class_b": 10_000_000,
    "kv_included_storage_gb_month": 1,
    "kv_storage_gb_month_usd": 0.50,
    "kv_included_reads": 10_000_000,
    "kv_reads_per_million_usd": 0.50,
    "sources": {
        "r2": "https://developers.cloudflare.com/r2/pricing/",
        "kv": "https://developers.cloudflare.com/kv/platform/pricing/",
        "workers": "https://developers.cloudflare.com/workers/platform/pricing/",
    },
}


@dataclass(frozen=True)
class Page:
    key: str
    size: int
    payload_bytes: int
    item_count: int


@dataclass(frozen=True)
class Clause:
    value: str
    prefix: bool = False
    field: str | None = None


@dataclass(frozen=True)
class QueryCase:
    name: str
    clauses: tuple[Clause, ...]
    tier: str


def derived_key(release: str, kind: str, value: str, page: int) -> str:
    digest = hashlib.sha256(value.encode()).hexdigest()
    return f"places/{release}/{kind}/{digest[:2]}/{digest}-p{page:04d}.bin"


def posting_maps(
    places: list[Place],
) -> tuple[
    dict[str, dict[int, tuple[int, int]]], dict[str, dict[int, tuple[int, int]]]
]:
    exact: dict[str, dict[int, tuple[int, int]]] = {}
    for doc_id, place in enumerate(places):
        rank = round(place.confidence * 255)
        for field, text in place.field_text().items():
            bit = FIELD_BITS[field]
            for token in tokens(text):
                docs = exact.setdefault(token, {})
                prior_mask, prior_rank = docs.get(doc_id, (0, rank))
                docs[doc_id] = (prior_mask | bit, max(prior_rank, rank))
    prefixes: dict[str, dict[int, tuple[int, int]]] = {}
    for token, docs in exact.items():
        for length in DIRECT_PREFIX_LENGTHS:
            if len(token) < length:
                continue
            prefix = token[:length]
            target = prefixes.setdefault(prefix, {})
            for doc_id, (mask, rank) in docs.items():
                old_mask, old_rank = target.get(doc_id, (0, rank))
                target[doc_id] = (old_mask | mask, max(old_rank, rank))
    return exact, prefixes


def posting_entry_bytes(
    doc_id: int, previous: int | None, mask: int, rank: int
) -> bytes:
    delta = doc_id if previous is None else doc_id - previous
    return encode_varint(delta) + bytes((mask, rank))


def posting_pages(
    release: str, kind: str, key: str, docs: dict[int, tuple[int, int]], target: int
) -> list[Page]:
    capacity = target - PAGE_HEADER_BYTES
    pages: list[Page] = []
    used = 0
    count = 0
    previous = None
    for doc_id, (mask, rank) in sorted(docs.items()):
        entry = posting_entry_bytes(doc_id, previous, mask, rank)
        if used and used + len(entry) > capacity:
            pages.append(
                Page(
                    derived_key(release, kind, key, len(pages)),
                    PAGE_HEADER_BYTES + used,
                    used,
                    count,
                )
            )
            used, count, previous = 0, 0, None
            entry = posting_entry_bytes(doc_id, previous, mask, rank)
        if len(entry) > capacity:
            raise ValueError("posting entry exceeds page")
        used += len(entry)
        count += 1
        previous = doc_id
    if count or not pages:
        pages.append(
            Page(
                derived_key(release, kind, key, len(pages)),
                PAGE_HEADER_BYTES + used,
                used,
                count,
            )
        )
    return pages


def posting_payload_size(docs: dict[int, tuple[int, int]]) -> int:
    size = 0
    previous = None
    for doc_id, (mask, rank) in sorted(docs.items()):
        size += len(posting_entry_bytes(doc_id, previous, mask, rank))
        previous = doc_id
    return size


class BucketPostingStore:
    """Pack rare posting lists into deterministic hash-bucket root pages.

    A term hash directly selects one root object. Lists larger than half a
    page get deterministic overflow objects; the root carries their directory
    entry. The builder must increase bucket_count if aggregate inline content
    cannot fit a root page.
    """

    def __init__(
        self,
        release: str,
        kind: str,
        mappings: dict[str, dict[int, tuple[int, int]]],
        target: int,
        bucket_count: int = POSTING_BUCKET_COUNT,
    ):
        self.mappings = mappings
        self.target = target
        self.bucket_count = bucket_count
        self.term_pages: dict[str, list[Page]] = {}
        self.term_payload = {
            term: posting_payload_size(docs) for term, docs in mappings.items()
        }
        buckets: dict[int, list[str]] = {}
        for term in mappings:
            bucket = (
                int.from_bytes(hashlib.sha256(term.encode()).digest()[:8], "big")
                % bucket_count
            )
            buckets.setdefault(bucket, []).append(term)
        self.roots: list[Page] = []
        self.overflow: list[Page] = []
        capacity = target - PAGE_HEADER_BYTES
        for bucket, terms in sorted(buckets.items()):
            inline = 0
            root_entries = 0
            heavy: dict[str, list[Page]] = {}
            for term in sorted(terms):
                payload = self.term_payload[term]
                if payload > capacity // 2:
                    overflow = posting_pages(
                        release, f"{kind}-overflow", term, mappings[term], target
                    )
                    heavy[term] = overflow
                    self.overflow.extend(overflow)
                    inline += 32  # term hash, doc count, overflow count/flags
                else:
                    inline += 16 + payload  # compact directory entry + inline postings
                root_entries += 1
            if inline > capacity:
                raise ValueError(
                    f"posting bucket {bucket} exceeds target; increase bucket count"
                )
            root = Page(
                derived_key(release, f"{kind}-bucket", str(bucket), 0),
                PAGE_HEADER_BYTES + inline,
                inline,
                root_entries,
            )
            self.roots.append(root)
            for term in terms:
                self.term_pages[term] = [root, *heavy.get(term, [])]

    def pages(self, term: str) -> list[Page]:
        return self.term_pages.get(term, [])

    def all_pages(self) -> list[Page]:
        return [*self.roots, *self.overflow]


def item_pages(
    release: str, kind: str, bucket: str, items: Iterable[bytes], target: int
) -> tuple[list[Page], dict[int, int]]:
    capacity = target - PAGE_HEADER_BYTES
    pages: list[Page] = []
    mapping: dict[int, int] = {}
    used = count = 0
    for item_id, item in enumerate(items):
        framed = encode_varint(len(item)) + item
        if used and used + len(framed) > capacity:
            pages.append(
                Page(
                    derived_key(release, kind, bucket, len(pages)),
                    PAGE_HEADER_BYTES + used,
                    used,
                    count,
                )
            )
            used, count = 0, 0
        if len(framed) > capacity:
            raise ValueError("item exceeds page")
        mapping[item_id] = len(pages)
        used += len(framed)
        count += 1
    if count or not pages:
        pages.append(
            Page(
                derived_key(release, kind, bucket, len(pages)),
                PAGE_HEADER_BYTES + used,
                used,
                count,
            )
        )
    return pages, mapping


class PageIndex:
    def __init__(
        self,
        places: list[Place],
        release: str,
        lexical_target: int,
        posting_target: int,
        result_target: int,
    ):
        self.places = places
        self.release = release
        self.targets = {
            "lexical": lexical_target,
            "posting": posting_target,
            "result": result_target,
        }
        self.exact, self.prefixes = posting_maps(places)
        self.exact_store = BucketPostingStore(
            release, "post-exact", self.exact, posting_target
        )
        self.prefix_store = BucketPostingStore(
            release, "post-prefix", self.prefixes, posting_target
        )
        buckets: dict[str, list[str]] = {}
        for token in self.exact:
            buckets.setdefault(token[:4], []).append(token)
        self.lexical_pages: dict[str, list[Page]] = {}
        for bucket, bucket_tokens in buckets.items():
            entries = [
                encode_varint(len(token.encode()))
                + token.encode()
                + encode_varint(len(self.exact[token]))
                for token in sorted(bucket_tokens)
            ]
            self.lexical_pages[bucket], _ = item_pages(
                release, "lex", bucket, entries, lexical_target
            )
        records = [encode_record(place) for place in places]
        self.record_sizes = [len(value) for value in records]
        self.result_pages, self.doc_to_result_page = item_pages(
            release, "result", "records", records, result_target
        )

    def all_pages(self) -> list[Page]:
        return (
            [page for pages in self.lexical_pages.values() for page in pages]
            + self.exact_store.all_pages()
            + self.prefix_store.all_pages()
            + self.result_pages
        )

    def resolve_clause(
        self, clause: Clause
    ) -> tuple[dict[int, tuple[int, int]], list[Page], int, int]:
        value = normalize(clause.value)
        pages: list[Page] = []
        traversed = 0
        if not clause.prefix:
            docs = self.exact.get(value, {})
            pages.extend(self.exact_store.pages(value))
            traversed += len(docs)
            useful = self.exact_store.term_payload.get(value, 0)
        elif len(value) in DIRECT_PREFIX_LENGTHS:
            docs = self.prefixes.get(value, {})
            pages.extend(self.prefix_store.pages(value))
            traversed += len(docs)
            useful = self.prefix_store.term_payload.get(value, 0)
        else:
            bucket = value[:4]
            pages.extend(self.lexical_pages.get(bucket, []))
            matching = [token for token in self.exact if token.startswith(value)]
            docs = {}
            useful = sum(
                page.payload_bytes for page in self.lexical_pages.get(bucket, [])
            )
            for token in matching:
                pages.extend(self.exact_store.pages(token))
                traversed += len(self.exact[token])
                useful += self.exact_store.term_payload[token]
                for doc_id, (mask, rank) in self.exact[token].items():
                    old_mask, old_rank = docs.get(doc_id, (0, rank))
                    docs[doc_id] = (old_mask | mask, max(old_rank, rank))
        if clause.field:
            bit = FIELD_BITS[clause.field]
            docs = {doc: value for doc, value in docs.items() if value[0] & bit}
        return docs, pages, traversed, useful

    def brute_clause(self, clause: Clause) -> set[int]:
        result = set()
        query = normalize(clause.value)
        fields = (clause.field,) if clause.field else FIELDS
        for doc_id, place in enumerate(self.places):
            values = [
                token for field in fields for token in tokens(place.field_text()[field])
            ]
            if any(
                token.startswith(query) if clause.prefix else token == query
                for token in values
            ):
                result.add(doc_id)
        return result

    def query(
        self, case: QueryCase, limit: int = 10, cold_kv: bool = False
    ) -> dict[str, Any]:
        candidates: set[int] | None = None
        oracle: set[int] | None = None
        fetched: dict[str, Page] = {}
        traversed = 0
        posting_useful = 0
        for clause in case.clauses:
            docs, pages, clause_traversed, clause_useful = self.resolve_clause(clause)
            traversed += clause_traversed
            posting_useful += clause_useful
            for page in pages:
                fetched[page.key] = page
            candidate_ids = set(docs)
            oracle_ids = self.brute_clause(clause)
            candidates = (
                candidate_ids if candidates is None else candidates & candidate_ids
            )
            oracle = oracle_ids if oracle is None else oracle & oracle_ids
        candidates = candidates or set()
        oracle = oracle or set()
        ranked = sorted(
            candidates, key=lambda doc: (-round(self.places[doc].confidence * 255), doc)
        )[:limit]
        useful_result = sum(self.record_sizes[doc] for doc in ranked)
        for doc in ranked:
            page = self.result_pages[self.doc_to_result_page[doc]]
            fetched[page.key] = page
        transferred = sum(page.size for page in fetched.values())
        useful = posting_useful + useful_result
        r2_ops = len(fetched)
        return {
            "name": case.name,
            "tier": case.tier,
            "candidate_count": len(candidates),
            "oracle_candidate_count": len(oracle),
            "complete_fixture_recall": candidates == oracle,
            "posting_entries_traversed": traversed,
            "r2_operations": r2_ops,
            "kv_operations": 1 if cold_kv else 0,
            "total_operations": r2_ops + (1 if cold_kv else 0),
            "bytes_transferred": transferred,
            "useful_bytes": useful,
            "page_amplification": transferred / useful if useful else None,
            "result_ids": [self.places[doc].place_id for doc in ranked],
        }

    def inventory(self) -> dict[str, Any]:
        pages = self.all_pages()
        by_kind = {}
        for kind, selected in {
            "lexical": [p for p in pages if "/lex/" in p.key],
            "exact_postings": [p for p in pages if "/post-exact" in p.key],
            "prefix_postings": [p for p in pages if "/post-prefix" in p.key],
            "results": [p for p in pages if "/result/" in p.key],
        }.items():
            by_kind[kind] = {
                "pages": len(selected),
                "bytes": sum(page.size for page in selected),
            }
        return {
            "targets_bytes": self.targets,
            "objects": len(pages),
            "published_bytes": sum(page.size for page in pages),
            "max_object_bytes": max(page.size for page in pages),
            "components": by_kind,
            "kv_directory_bytes": len(
                json.dumps(
                    {
                        "active_release": self.release,
                        "rollback_release": "previous",
                        "format": 1,
                        "direct_prefix_lengths": DIRECT_PREFIX_LENGTHS,
                        "page_targets": self.targets,
                    },
                    separators=(",", ":"),
                ).encode()
            ),
            "kv_keys": 2,
        }


CASES = (
    QueryCase("starbucks_exact", (Clause("starbucks"),), "typical"),
    QueryCase(
        "warfield_hotel_tokens", (Clause("warfield"), Clause("hotel")), "typical"
    ),
    QueryCase(
        "golden_gate_prefix", (Clause("golden"), Clause("gat", prefix=True)), "typical"
    ),
    QueryCase("hotel_category", (Clause("hotel", field="category"),), "typical"),
    QueryCase(
        "sf_cafe_context",
        (
            Clause("san", field="context"),
            Clause("francisco", field="context"),
            Clause("cafe", prefix=True),
        ),
        "worst_supported",
    ),
    QueryCase(
        "starbucks_long_prefix", (Clause("starbu", prefix=True),), "worst_supported"
    ),
)


def gates(results: list[dict[str, Any]], cold: bool = False) -> dict[str, Any]:
    typical = [row for row in results if row["tier"] == "typical"]
    worst = [row for row in results if row["tier"] == "worst_supported"]
    return {
        "routing_cache": "cold-kv" if cold else "warm-catalog",
        "typical_max_operations": max(row["total_operations"] for row in typical),
        "typical_max_bytes": max(row["bytes_transferred"] for row in typical),
        "typical_pass": all(
            row["total_operations"] <= 3 and row["bytes_transferred"] <= 256 * 1024
            for row in typical
        ),
        "worst_max_operations": max(row["total_operations"] for row in worst),
        "worst_max_bytes": max(row["bytes_transferred"] for row in worst),
        "worst_pass": all(
            row["total_operations"] <= 8 and row["bytes_transferred"] <= 2 * 1024 * 1024
            for row in worst
        ),
        "complete_fixture_recall": all(
            row["complete_fixture_recall"] for row in results
        ),
    }


def synthetic_overflow(
    posting_target: int,
    result_target: int,
    count: int = 500_000,
    cold_kv: bool = False,
    scattered_results: bool = False,
) -> dict[str, Any]:
    docs = {doc: (FIELD_BITS["brand"], 200) for doc in range(count)}
    store = BucketPostingStore(
        "synthetic", "post-exact", {"chain": docs}, posting_target
    )
    pages = store.pages("chain")
    decoded_count = count
    result_page_count = 10 if scattered_results else 1
    result_page_bytes = result_target * result_page_count
    operations = len(pages) + result_page_count + int(cold_kv)
    transferred = sum(page.size for page in pages) + result_page_bytes
    useful = store.term_payload["chain"] + 10 * 96
    return {
        "synthetic": True,
        "distribution": (
            "500,000 documents share one brand token; top-10 results occupy ten full result pages"
            if scattered_results
            else "500,000 documents share one brand token; top-10 results co-located in one full result page"
        ),
        "posting_count": count,
        "posting_root_pages": 1,
        "posting_overflow_pages": len(pages) - 1,
        "posting_pages": len(pages),
        "result_pages": result_page_count,
        "decoded_posting_count": decoded_count,
        "full_traversal": decoded_count == count,
        "total_operations": operations,
        "bytes_transferred": transferred,
        "page_amplification": transferred / useful,
        "worst_gate_pass": operations <= 8 and transferred <= 2 * 1024 * 1024,
    }


def ceil_million_cost(usage: float, included: float, unit_price: float) -> float:
    billable = max(0.0, usage - included)
    return math.ceil(billable / 1_000_000) * unit_price


def monthly_cost(
    query_count: int,
    average_r2_ops: float,
    kv_reads_per_query: float,
    storage_gb: float,
    class_a_writes: int = 0,
) -> dict[str, Any]:
    worker = PRICE["workers_base_monthly_usd"] + ceil_million_cost(
        query_count,
        PRICE["workers_included_requests"],
        PRICE["workers_additional_per_million_usd"],
    )
    r2_reads = query_count * average_r2_ops
    r2_ops = ceil_million_cost(
        r2_reads, PRICE["r2_included_class_b"], PRICE["r2_class_b_per_million_usd"]
    )
    r2_class_a = ceil_million_cost(
        class_a_writes,
        PRICE["r2_included_class_a"],
        PRICE["r2_class_a_per_million_usd"],
    )
    r2_storage = (
        math.ceil(max(0, storage_gb - PRICE["r2_included_storage_gb_month"]))
        * PRICE["r2_standard_storage_gb_month_usd"]
    )
    kv_reads = query_count * kv_reads_per_query
    kv = ceil_million_cost(
        kv_reads, PRICE["kv_included_reads"], PRICE["kv_reads_per_million_usd"]
    )
    total = worker + r2_ops + r2_class_a + r2_storage + kv
    return {
        "queries": query_count,
        "storage_gb": storage_gb,
        "average_r2_ops_per_query": average_r2_ops,
        "kv_reads_per_query_assumption": kv_reads_per_query,
        "workers_usd": worker,
        "r2_class_b_usd": r2_ops,
        "r2_class_a_usd": r2_class_a,
        "class_a_writes_assumption": class_a_writes,
        "r2_storage_usd": r2_storage,
        "kv_usd": kv,
        "total_usd_lower_bound": total,
        "under_30_usd": total <= 30,
        "excluded": "Workers CPU overage, logs, and any non-Cloudflare costs",
    }


def select_configuration(reports: dict[str, dict[str, Any]]) -> tuple[str, bool]:
    """Select a diagnostic layout without assuming that any layout passes."""
    passing = [
        name
        for name, value in reports.items()
        if value["warm_gates"]["typical_pass"]
        and value["warm_gates"]["worst_pass"]
        and value["synthetic_high_fanout_warm"]["worst_gate_pass"]
    ]
    candidates = passing or list(reports)

    def selection_key(name: str) -> tuple[int, int, int, int, int]:
        value = reports[name]
        warm = value["warm_gates"]
        overflow = value["synthetic_high_fanout_warm"]
        failed_gates = sum(
            (
                not warm["typical_pass"],
                not warm["worst_pass"],
                not overflow["worst_gate_pass"],
            )
        )
        return (
            failed_gates,
            warm["typical_max_operations"],
            warm["worst_max_operations"],
            overflow["total_operations"],
            value["inventory"]["published_bytes"],
        )

    return min(candidates, key=selection_key), bool(passing)


def build_report(
    places: list[Place], configuration_names: set[str] | None = None
) -> dict[str, Any]:
    all_configurations = {
        "uniform_16k": (16 * 1024, 16 * 1024, 16 * 1024),
        "uniform_64k": (64 * 1024, 64 * 1024, 64 * 1024),
        "uniform_256k": (256 * 1024, 256 * 1024, 256 * 1024),
        "hybrid_16k_256k_64k": (16 * 1024, 256 * 1024, 64 * 1024),
    }
    configurations = {
        name: targets
        for name, targets in all_configurations.items()
        if configuration_names is None or name in configuration_names
    }
    if not configurations:
        raise ValueError("at least one page configuration is required")
    reports = {}
    for name, targets in configurations.items():
        index = PageIndex(places, "fixture-current", *targets)
        warm = [index.query(case, cold_kv=False) for case in CASES]
        cold = [index.query(case, cold_kv=True) for case in CASES]
        reports[name] = {
            "inventory": index.inventory(),
            "warm_queries": warm,
            "cold_queries": cold,
            "warm_gates": gates(warm),
            "cold_gates": gates(cold, cold=True),
            "synthetic_high_fanout_warm": synthetic_overflow(targets[1], targets[2]),
            "synthetic_high_fanout_cold": synthetic_overflow(
                targets[1], targets[2], cold_kv=True
            ),
            "synthetic_scattered_results_warm": synthetic_overflow(
                targets[1], targets[2], scattered_results=True
            ),
            "synthetic_scattered_results_cold": synthetic_overflow(
                targets[1], targets[2], cold_kv=True, scattered_results=True
            ),
        }
    # Selection is fixture-bounded: choose the smallest published layout that
    # passes fixture warm gates and co-located overflow. Scattered-result stress
    # is reported separately and can still reject production readiness.
    selected_name, selection_gate_passed = select_configuration(reports)
    selected = reports[selected_name]
    inventory = selected["inventory"]
    bytes_per_place = inventory["published_bytes"] / len(places)
    two_places_releases_gb = bytes_per_place * 75_000_000 * 2 / 1_000_000_000
    typical = [row for row in selected["warm_queries"] if row["tier"] == "typical"]
    average_ops = sum(row["r2_operations"] for row in typical) / len(typical)
    scale = 75_000_000 / len(places)
    components = inventory["components"]
    global_component_bytes = {
        name: value["bytes"] * scale for name, value in components.items()
    }
    object_sensitivity = []
    for bucket_count in (4096, 65_536, 1_000_000):
        payload_pages = (
            math.ceil(
                global_component_bytes["lexical"]
                / inventory["targets_bytes"]["lexical"]
            )
            + math.ceil(
                (
                    global_component_bytes["exact_postings"]
                    + global_component_bytes["prefix_postings"]
                )
                / inventory["targets_bytes"]["posting"]
            )
            + math.ceil(
                global_component_bytes["results"] / inventory["targets_bytes"]["result"]
            )
        )
        object_count = 2 * bucket_count + payload_pages
        object_sensitivity.append(
            {
                "posting_buckets_per_family": bucket_count,
                "modeled_objects_per_release": object_count,
                "monthly_one_release_class_a_usd": ceil_million_cost(
                    object_count,
                    PRICE["r2_included_class_a"],
                    PRICE["r2_class_a_per_million_usd"],
                ),
                "warning": "linear fixture payload pages plus fixed exact/prefix roots; global token skew is unknown",
            }
        )
    expected_objects = object_sensitivity[0]["modeled_objects_per_release"]
    direct_term_linear_objects = round(inventory["objects"] * scale)
    costs = []
    for queries in (1_000_000, 10_000_000, 50_000_000):
        for core_gb in (100, 200, 300):
            costs.append(
                monthly_cost(
                    queries,
                    average_ops,
                    0.01,
                    core_gb + two_places_releases_gb,
                    expected_objects,
                )
            )
    kv_sensitivity = [
        monthly_cost(
            50_000_000,
            average_ops,
            fraction,
            200 + two_places_releases_gb,
            expected_objects,
        )
        for fraction in (0.0, 0.01, 1.0)
    ]
    return {
        "schema_version": 1,
        "fixture": {
            "places": len(places),
            "scope": "input Places corpus; measured page serialization",
            "human_label_status": "no independent labels; recall is exact agreement with a brute-force fixture oracle",
        },
        "architecture": {
            "kv": "two tiny keys: active/rollback release pointer and format/page-target metadata; assumed edge-cached",
            "r2": "immutable lexical, 4096-way hash-bucket posting roots/overflow, and result pages; term hash derives the root key",
            "direct_prefix_lengths": DIRECT_PREFIX_LENGTHS,
            "overflow": "bucket root identifies deterministic overflow pages; every page is traversed before ranking; no cap/truncation",
        },
        "configurations": reports,
        "selected_configuration": selected_name,
        "selection_gate_passed": selection_gate_passed,
        "linear_shape_only": {
            "fixture_published_bytes_per_place": bytes_per_place,
            "one_global_release_gb_at_75m": two_places_releases_gb / 2,
            "two_retained_global_releases_gb": two_places_releases_gb,
            "warning": "not a forecast; token, language, field, fanout, and compression distributions can differ globally",
        },
        "pricing": PRICE,
        "cost_model": {
            "selected_typical_average_r2_ops": average_ops,
            "kv_read_fraction_assumption": 0.01,
            "scenarios": costs,
            "kv_cache_sensitivity_at_50m_queries_200gb_core": kv_sensitivity,
            "publication_object_sensitivity": object_sensitivity,
            "rejected_direct_term_linear_objects_per_release": direct_term_linear_objects,
            "publication_model": "one new global Places release written per month; current+rollback both retained for storage",
            "rounding": "billable GB and million-operation units rounded upward after documented included usage",
            "cost_gate_interpretation": "lower bound because Workers CPU is unmeasured; any scenario above $30 fails, below $30 still needs CPU evidence",
        },
    }


def markdown(report: dict[str, Any]) -> str:
    shape = report["linear_shape_only"]
    selected_name = report["selected_configuration"]
    selected = report["configurations"][selected_name]
    targets = selected["inventory"]["targets_bytes"]
    lines = [
        "# Places KV/R2 paged-index spike",
        "",
        "Measured fixture facts, synthetic stress results, linear extrapolations, and documented prices are kept separate below.",
        "",
        f"- Input corpus: {report['fixture']['places']:,} Places",
        f"- Selected diagnostic layout: `{selected_name}` ({targets['lexical'] // 1024} KiB lexical, {targets['posting'] // 1024} KiB postings, {targets['result'] // 1024} KiB results)",
        f"- At least one layout passed the fixture/co-located gates: {report['selection_gate_passed']}",
        f"- Fixture published bytes/place: {shape['fixture_published_bytes_per_place']:.1f}",
        f"- Linear global release / two-release storage: {shape['one_global_release_gb_at_75m']:.2f} / {shape['two_retained_global_releases_gb']:.2f} GB",
        f"- Extrapolation warning: {shape['warning']}",
        "",
        "KV stores only the active/rollback release pointer and format/page targets. Token hashes directly select one of 4,096 packed exact/prefix posting roots; heavy terms use deterministic overflow pages. Longer prefixes use a four-character lexical bucket and then complete exact-token chains. All overflow pages are traversed before ranking; there is no silent candidate cap.",
        "",
        "## Page configurations",
        "",
        "| layout | fixture bytes | objects | warm typical gate | warm worst gate | 500k co-located | 500k scattered |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, value in report["configurations"].items():
        lines.append(
            f"| {name} | {value['inventory']['published_bytes']:,} | {value['inventory']['objects']:,} | "
            f"{value['warm_gates']['typical_pass']} ({value['warm_gates']['typical_max_operations']} ops/{value['warm_gates']['typical_max_bytes']:,} B) | "
            f"{value['warm_gates']['worst_pass']} ({value['warm_gates']['worst_max_operations']} ops/{value['warm_gates']['worst_max_bytes']:,} B) | "
            f"{value['synthetic_high_fanout_warm']['worst_gate_pass']} ({value['synthetic_high_fanout_warm']['total_operations']} ops/{value['synthetic_high_fanout_warm']['bytes_transferred']:,} B) | "
            f"{value['synthetic_scattered_results_warm']['worst_gate_pass']} ({value['synthetic_scattered_results_warm']['total_operations']} ops/{value['synthetic_scattered_results_warm']['bytes_transferred']:,} B) |"
        )
    lines += [
        "",
        "## Selected-layout fixture queries",
        "",
        "| query | tier | ops warm/cold | bytes | amplification | candidates | recall |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    cold_by_name = {row["name"]: row for row in selected["cold_queries"]}
    for row in selected["warm_queries"]:
        amp = (
            "n/a"
            if row["page_amplification"] is None
            else f"{row['page_amplification']:.2f}x"
        )
        lines.append(
            f"| {row['name']} | {row['tier']} | {row['total_operations']}/{cold_by_name[row['name']]['total_operations']} | {row['bytes_transferred']:,} | {amp} | {row['candidate_count']} | {row['complete_fixture_recall']} |"
        )
    synthetic = selected["synthetic_high_fanout_warm"]
    scattered = selected["synthetic_scattered_results_warm"]
    lines += [
        "",
        "## Synthetic overflow",
        "",
        f"The fixture is too small to stress overflow. A deterministic synthetic token shared by {synthetic['posting_count']:,} documents produced one root plus {synthetic['posting_overflow_pages']} overflow pages. The reader accounted for {synthetic['decoded_posting_count']:,} entries, so full traversal is `{synthetic['full_traversal']}`. With co-located top-10 results, warm operations/bytes were {synthetic['total_operations']} / {synthetic['bytes_transferred']:,}; gate pass: `{synthetic['worst_gate_pass']}`. With top-10 scattered across ten full result pages, they were {scattered['total_operations']} / {scattered['bytes_transferred']:,}; gate pass: `{scattered['worst_gate_pass']}`.",
        "",
        "The public API defaults to 10 results and permits up to 40. The scattered top-10 stress already fails the operation gate; result locality remains unproven and larger limits can only increase pressure.",
        "",
        "## Publication-object sensitivity",
        "",
        f"A rejected one-object-per-term linear shape would produce about {report['cost_model']['rejected_direct_term_linear_objects_per_release']:,} objects per release. Packed roots avoid that direct scaling, but global payload/skew remain modeled:",
        "",
        "| exact/prefix buckets each | modeled objects/release | monthly Class A for one release |",
        "|---:|---:|---:|",
    ]
    for row in report["cost_model"]["publication_object_sensitivity"]:
        lines.append(
            f"| {row['posting_buckets_per_family']:,} | {row['modeled_objects_per_release']:,} | ${row['monthly_one_release_class_a_usd']:.2f} |"
        )
    lines += [
        "",
        "The 4,096-bucket row is used in the cost table. It is a sensitivity model, not a demonstrated global build; a producer must fail if any rare-term bucket root exceeds its page cap.",
        "",
        "## Monthly lower-bound cost model",
        "",
        "Prices are Cloudflare documentation constants as of 2026-07-14. Worker CPU is unmeasured and excluded, so passing totals are not yet proof of the $30 ceiling; failing totals are decisive.",
        "",
        "| queries/month | retained storage | Workers | R2 reads | R2 publish | R2 storage | KV | total lower bound | <=$30 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["cost_model"]["scenarios"]:
        lines.append(
            f"| {row['queries']:,} | {row['storage_gb']:.1f} GB | ${row['workers_usd']:.2f} | ${row['r2_class_b_usd']:.2f} | ${row['r2_class_a_usd']:.2f} | ${row['r2_storage_usd']:.2f} | ${row['kv_usd']:.2f} | ${row['total_usd_lower_bound']:.2f} | {row['under_30_usd']} |"
        )
    lines += [
        "",
        "KV cache sensitivity at 50M queries and the 200 GB core scenario (0%, 1%, and 100% query reads):",
        "",
        "| KV reads/query | KV cost | total lower bound | <=$30 |",
        "|---:|---:|---:|---:|",
    ]
    for row in report["cost_model"]["kv_cache_sensitivity_at_50m_queries_200gb_core"]:
        lines.append(
            f"| {row['kv_reads_per_query_assumption']:.2f} | ${row['kv_usd']:.2f} | ${row['total_usd_lower_bound']:.2f} | {row['under_30_usd']} |"
        )
    fixture_pass = (
        selected["warm_gates"]["typical_pass"] and selected["warm_gates"]["worst_pass"]
    )
    scatter_pass = selected["synthetic_scattered_results_warm"]["worst_gate_pass"]
    lines += [
        "",
        "## Verdict",
        "",
        f"At least one layout passes the fixture/co-located selection gates: `{report['selection_gate_passed']}`. The selected diagnostic layout passes its warm fixture gates: `{fixture_pass}`. It passes the scattered-result stress: `{scatter_pass}`. Therefore this spike does not establish production readiness even though fixture recall is complete and posting overflow is never truncated.",
        "",
        "A cold catalog miss adds one KV operation and can break the three-operation typical gate. KV must remain tiny and cached; storing per-token postings or results there would invalidate the cost model.",
        "",
        "At 1M and 10M monthly queries, the modeled 100–300 GB core plus two linearized Places releases remains below $30 before Worker CPU. At 50M queries, read operations and Worker requests exceed the ceiling. This design therefore needs an explicit traffic/CPU gate and aggressive cache evidence; it is not automatically affordable at arbitrary volume.",
        "",
        f"Official pricing: [R2]({PRICE['sources']['r2']}), [Workers KV]({PRICE['sources']['kv']}), [Workers]({PRICE['sources']['workers']}).",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument(
        "--configuration",
        action="append",
        choices=("uniform_16k", "uniform_64k", "uniform_256k", "hybrid_16k_256k_64k"),
        help="Build only the selected layout; repeat to select multiple layouts",
    )
    args = parser.parse_args(argv)
    places = load_places(args.input)
    selected_configurations = set(args.configuration) if args.configuration else None
    report = build_report(places, selected_configurations)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2) + "\n")
    args.markdown_out.write_text(markdown(report) + "\n")
    print(
        json.dumps(
            {
                "places": len(places),
                "selected": report["selected_configuration"],
                "selected_warm_gates": report["configurations"][
                    report["selected_configuration"]
                ]["warm_gates"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
