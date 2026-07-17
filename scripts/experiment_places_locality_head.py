#!/usr/bin/env python3
"""Model cell-clustered result pages plus a packed global top-k head index.

This is an offline architecture spike. It reuses the complete posting lists from
the KV/R2 page experiment, clusters result records by coarse spatial cell and
static rank, and adds an explicitly top-k-only global head for common exact and
prefix terms. No Cloudflare resources are read or written.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from experiment_places_compact_index import (  # noqa: E402
    Place,
    encode_record,
    encode_varint,
    load_places,
    normalize,
    tokens,
)
from experiment_places_kv_r2_pages import (  # noqa: E402
    CASES,
    Clause,  # noqa: F401 - re-exported for experiment consumers and tests
    Page,
    PageIndex,
    QueryCase,
    derived_key,
    item_pages,
)


HEAD_PREFIX_LENGTHS = tuple(range(2, 9))
# Famous pair keys use the first this-many distinct name/brand tokens per
# famous place (tokenizer emission order), bounding pair fanout to 28/place.
FAMOUS_PAIR_TOKEN_LIMIT = 8


def spatial_cell(place: Place, degrees: float) -> str:
    if not (-90 <= place.lat <= 90 and -180 <= place.lon <= 180):
        return "unknown"
    y = math.floor((place.lat + 90.0) / degrees)
    x = math.floor((place.lon + 180.0) / degrees)
    return f"{y:04x}-{x:04x}"


def ordered_places(places: list[Place], degrees: float) -> list[Place]:
    return sorted(
        places,
        key=lambda place: (
            spatial_cell(place, degrees),
            -round(place.confidence * 255),
            place.place_id,
        ),
    )


def place_terms(place: Place) -> set[str]:
    return {
        token
        for field_text in place.field_text().values()
        for token in tokens(field_text)
    }


class PackedHeadStore:
    """Pack top-k result projections into deterministic hash-bucket objects."""

    def __init__(
        self,
        release: str,
        places: list[Place],
        heads: dict[str, list[int]],
        target: int,
        bucket_count: int,
    ):
        self.heads = heads
        self.pages_by_key: dict[str, Page] = {}
        capacity = target - 24
        entry_sizes: dict[str, int] = {}
        for key, doc_ids in heads.items():
            records = b"".join(
                encode_varint(len(record)) + record
                for record in (encode_record(places[doc]) for doc in doc_ids)
            )
            entry_sizes[key] = 16 + len(key.encode()) + len(records)
        oversized = [key for key, size in entry_sizes.items() if size > capacity]
        if oversized:
            raise ValueError(
                f"global-head entry {oversized[0]!r} exceeds the object target"
            )

        self.bucket_count = max(1, bucket_count)
        while True:
            buckets: dict[int, list[str]] = defaultdict(list)
            for key in heads:
                bucket = (
                    int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
                    % self.bucket_count
                )
                buckets[bucket].append(key)
            bucket_payloads = {
                bucket: sum(entry_sizes[key] for key in keys)
                for bucket, keys in buckets.items()
            }
            if all(payload <= capacity for payload in bucket_payloads.values()):
                break
            self.bucket_count *= 2

        self.pages: list[Page] = []
        for bucket, keys in sorted(buckets.items()):
            payload = bucket_payloads[bucket]
            page = Page(
                derived_key(release, "global-head-bucket", str(bucket), 0),
                24 + payload,
                payload,
                len(keys),
            )
            self.pages.append(page)
            for key in keys:
                self.pages_by_key[key] = page

    def page(self, key: str) -> Page | None:
        return self.pages_by_key.get(key)


def push_top(heap: list[tuple[int, int]], doc_id: int, rank: int, limit: int) -> None:
    value = (rank, -doc_id)
    if len(heap) < limit:
        heapq.heappush(heap, value)
    elif value > heap[0]:
        heapq.heapreplace(heap, value)


def famous_docs(places: list[Place], famous_cap: int) -> list[int]:
    """Deterministically select the famous set F.

    Top ``famous_cap`` places by (-quantized confidence, stable serving order).
    A hard cap rather than a confidence floor keeps the added bytes bounded
    regardless of the confidence distribution.
    """
    if famous_cap <= 0:
        return []
    return sorted(
        range(len(places)),
        key=lambda doc: (-round(places[doc].confidence * 255), doc),
    )[:famous_cap]


def famous_name_brand_tokens(place: Place) -> tuple[str, ...]:
    """Distinct name/brand tokens of one place, in tokenizer emission order.

    Category/context tokens are deliberately excluded: they would reintroduce
    the density this admission path exists to bypass.
    """
    field_text = place.field_text()
    return tuple(
        dict.fromkeys(
            token
            for value in (field_text["name"], field_text["brand"])
            for token in tokens(value)
        )
    )


def build_heads(
    places: list[Place],
    exact: dict[str, dict[int, tuple[int, int]]],
    minimum_candidates: int,
    limit: int,
    famous_cap: int = 0,
) -> dict[str, list[int]]:
    heads: dict[str, list[int]] = {}
    admitted_tokens: set[str] = set()
    pair_keys: dict[str, tuple[str, str]] = {}
    for doc in famous_docs(places, famous_cap):
        name_brand = famous_name_brand_tokens(places[doc])
        admitted_tokens.update(name_brand)
        pair_tokens = name_brand[:FAMOUS_PAIR_TOKEN_LIMIT]
        for first_index in range(len(pair_tokens)):
            for second_index in range(first_index + 1, len(pair_tokens)):
                low, high = sorted(
                    (pair_tokens[first_index], pair_tokens[second_index])
                )
                pair_keys[f"e2:{low} {high}"] = (low, high)
    for key in sorted(pair_keys):
        low, high = pair_keys[key]
        low_docs = exact.get(low)
        high_docs = exact.get(high)
        if not low_docs or not high_docs:
            continue
        if len(high_docs) < len(low_docs):
            low_docs, high_docs = high_docs, low_docs
        shared = [doc for doc in low_docs if doc in high_docs]
        if not shared:
            continue
        heads[key] = sorted(
            shared, key=lambda doc: (-max(low_docs[doc][1], high_docs[doc][1]), doc)
        )[:limit]
    for token, docs in exact.items():
        if len(docs) < minimum_candidates and token not in admitted_tokens:
            continue
        best = sorted(docs, key=lambda doc: (-docs[doc][1], doc))[:limit]
        heads[f"e:{token}"] = best

    approximate_counts: dict[str, int] = defaultdict(int)
    for token, docs in exact.items():
        for length in HEAD_PREFIX_LENGTHS:
            if len(token) >= length:
                approximate_counts[token[:length]] += len(docs)
    heavy_prefixes = {
        prefix
        for prefix, count in approximate_counts.items()
        if count >= minimum_candidates
    }
    prefix_heaps: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for doc_id, place in enumerate(places):
        rank = round(place.confidence * 255)
        prefixes = {
            token[:length]
            for token in place_terms(place)
            for length in HEAD_PREFIX_LENGTHS
            if len(token) >= length and token[:length] in heavy_prefixes
        }
        for prefix in prefixes:
            push_top(prefix_heaps[prefix], doc_id, rank, limit)
    for prefix, heap in prefix_heaps.items():
        if approximate_counts[prefix] >= minimum_candidates:
            heads[f"p:{prefix}"] = [
                -neg_doc for _, neg_doc in sorted(heap, reverse=True)
            ]
    return heads


class LocalityHeadIndex:
    def __init__(
        self,
        places: list[Place],
        release: str = "fixture-current",
        cell_degrees: float = 0.25,
        lexical_target: int = 16 * 1024,
        posting_target: int = 256 * 1024,
        result_target: int = 64 * 1024,
        head_target: int = 64 * 1024,
        head_bucket_count: int = 4096,
        head_minimum_candidates: int = 64,
        head_limit: int = 10,
        head_famous_cap: int = 0,
    ):
        self.cell_degrees = cell_degrees
        self.places = ordered_places(places, cell_degrees)
        self.cells = [spatial_cell(place, cell_degrees) for place in self.places]
        self.base = PageIndex(
            self.places, release, lexical_target, posting_target, result_target
        )
        grouped_records: dict[str, list[tuple[int, bytes]]] = defaultdict(list)
        for doc_id, place in enumerate(self.places):
            grouped_records[self.cells[doc_id]].append((doc_id, encode_record(place)))
        self.result_pages: list[Page] = []
        self.doc_to_result_page: dict[int, Page] = {}
        for cell, rows in sorted(grouped_records.items()):
            pages, local_mapping = item_pages(
                release,
                "cell-result",
                cell,
                (record for _, record in rows),
                result_target,
            )
            self.result_pages.extend(pages)
            for local_id, (doc_id, _) in enumerate(rows):
                self.doc_to_result_page[doc_id] = pages[local_mapping[local_id]]
        self.record_sizes = [len(encode_record(place)) for place in self.places]
        self.heads = build_heads(
            self.places,
            self.base.exact,
            head_minimum_candidates,
            head_limit,
            head_famous_cap,
        )
        self.head_store = PackedHeadStore(
            release, self.places, self.heads, head_target, head_bucket_count
        )
        self.head_limit = head_limit

    def oracle_candidates(self, case: QueryCase, cell: str | None = None) -> set[int]:
        candidates: set[int] | None = None
        for clause in case.clauses:
            docs = self.base.brute_clause(clause)
            candidates = docs if candidates is None else candidates & docs
        result = candidates or set()
        if cell is not None:
            result = {doc for doc in result if self.cells[doc] == cell}
        return result

    def ranked(self, docs: set[int], limit: int = 10) -> list[int]:
        return sorted(
            docs,
            key=lambda doc: (-round(self.places[doc].confidence * 255), doc),
        )[:limit]

    def preferred_cell(self, case: QueryCase) -> str | None:
        ranked = self.ranked(self.oracle_candidates(case), 1)
        return self.cells[ranked[0]] if ranked else None

    def query(
        self,
        case: QueryCase,
        *,
        cell: str | None = None,
        use_global_head: bool = False,
        limit: int = 10,
    ) -> dict[str, Any]:
        oracle = self.oracle_candidates(case, cell)
        oracle_top = self.ranked(oracle, limit)
        if use_global_head and cell is None and len(case.clauses) == 1:
            clause = case.clauses[0]
            value = normalize(clause.value)
            key = f"{'p' if clause.prefix else 'e'}:{value}"
            page = self.head_store.page(key) if clause.field is None else None
            if page is not None:
                docs = self.heads[key][:limit]
                return {
                    "mode": "global_head",
                    "coverage": "exact top-k under static rank; candidate tail intentionally omitted",
                    "cell": None,
                    "candidate_count": len(oracle),
                    "result_ids": [self.places[doc].place_id for doc in docs],
                    "oracle_ids": [self.places[doc].place_id for doc in oracle_top],
                    "top_k_exact": docs == oracle_top,
                    "complete_candidate_recall": len(docs) == len(oracle),
                    "operations": 1,
                    "bytes_transferred": page.size,
                }

        candidates: set[int] | None = None
        fetched: dict[str, Page] = {}
        for clause in case.clauses:
            docs, pages, _, _ = self.base.resolve_clause(clause)
            clause_docs = set(docs)
            candidates = clause_docs if candidates is None else candidates & clause_docs
            for page in pages:
                fetched[page.key] = page
        candidates = candidates or set()
        if cell is not None:
            candidates = {doc for doc in candidates if self.cells[doc] == cell}
        docs = self.ranked(candidates, limit)
        for doc in docs:
            page = self.doc_to_result_page[doc]
            fetched[page.key] = page
        return {
            "mode": "cell_routed" if cell is not None else "full_fallback",
            "coverage": "complete candidate traversal",
            "cell": cell,
            "candidate_count": len(candidates),
            "result_ids": [self.places[doc].place_id for doc in docs],
            "oracle_ids": [self.places[doc].place_id for doc in oracle_top],
            "top_k_exact": docs == oracle_top,
            "complete_candidate_recall": candidates == oracle,
            "operations": len(fetched),
            "bytes_transferred": sum(page.size for page in fetched.values()),
        }

    def inventory(self) -> dict[str, Any]:
        non_result_pages = (
            [page for pages in self.base.lexical_pages.values() for page in pages]
            + self.base.exact_store.all_pages()
            + self.base.prefix_store.all_pages()
        )
        components = {
            "lexical_and_postings": {
                "objects": len(non_result_pages),
                "bytes": sum(page.size for page in non_result_pages),
            },
            "cell_results": {
                "objects": len(self.result_pages),
                "bytes": sum(page.size for page in self.result_pages),
            },
            "global_head": {
                "objects": len(self.head_store.pages),
                "bytes": sum(page.size for page in self.head_store.pages),
                "keys": len(self.heads),
                "hash_buckets": self.head_store.bucket_count,
            },
        }
        return {
            "cells": len(set(self.cells)),
            "components": components,
            "objects": sum(value["objects"] for value in components.values()),
            "bytes": sum(value["bytes"] for value in components.values()),
            "bytes_per_place": sum(value["bytes"] for value in components.values())
            / len(self.places),
        }


def build_report(
    places: list[Place],
    head_minimum_candidates: int = 64,
    head_famous_cap: int = 0,
) -> dict[str, Any]:
    index = LocalityHeadIndex(
        places,
        head_minimum_candidates=head_minimum_candidates,
        head_famous_cap=head_famous_cap,
    )
    cases = []
    for case in CASES:
        fallback = index.query(case)
        preferred_cell = index.preferred_cell(case)
        located = index.query(case, cell=preferred_cell) if preferred_cell else None
        head = index.query(case, use_global_head=True)
        cases.append(
            {
                "name": case.name,
                "fallback": fallback,
                "located": located,
                "global_head": head if head["mode"] == "global_head" else None,
            }
        )
    inventory = index.inventory()
    located_rows = [row["located"] for row in cases if row["located"]]
    head_rows = [row["global_head"] for row in cases if row["global_head"]]
    return {
        "schema_version": 1,
        "input_places": len(places),
        "architecture": {
            "cell_degrees": index.cell_degrees,
            "result_order": "spatial cell, descending quantized confidence, stable ID",
            "global_head_semantics": "top-k exact under static rank for eligible single exact/prefix clauses; never presented as complete candidate recall",
            "head_minimum_candidates": head_minimum_candidates,
            "head_famous_cap": head_famous_cap,
            "head_limit": index.head_limit,
            "located_routing_model": "optimistic: route to the cell containing the globally highest-ranked matching result",
        },
        "inventory": inventory,
        "queries": cases,
        "summary": {
            "located_query_count": len(located_rows),
            "located_top_k_exact": all(row["top_k_exact"] for row in located_rows),
            "located_complete_candidate_recall": all(
                row["complete_candidate_recall"] for row in located_rows
            ),
            "located_max_operations": max(
                (row["operations"] for row in located_rows), default=0
            ),
            "located_max_bytes": max(
                (row["bytes_transferred"] for row in located_rows), default=0
            ),
            "global_head_query_count": len(head_rows),
            "global_head_top_k_exact": all(row["top_k_exact"] for row in head_rows),
            "global_head_max_operations": max(
                (row["operations"] for row in head_rows), default=0
            ),
            "global_head_max_bytes": max(
                (row["bytes_transferred"] for row in head_rows), default=0
            ),
            "located_gate_pass": bool(located_rows)
            and all(
                row["operations"] <= 3 and row["bytes_transferred"] <= 256 * 1024
                for row in located_rows
            ),
            "global_head_gate_pass": bool(head_rows)
            and all(
                row["operations"] <= 1 and row["bytes_transferred"] <= 64 * 1024
                for row in head_rows
            ),
        },
    }


def markdown(report: dict[str, Any]) -> str:
    inventory = report["inventory"]
    summary = report["summary"]
    lines = [
        "# Places locality + global-head spike",
        "",
        f"- Input: {report['input_places']:,} Places",
        f"- Spatial cells: {inventory['cells']:,}",
        f"- Total modeled bytes: {inventory['bytes']:,} ({inventory['bytes_per_place']:.1f} B/place)",
        f"- Located gate: {summary['located_gate_pass']} (max {summary['located_max_operations']} ops / {summary['located_max_bytes']:,} B)",
        f"- Global-head gate: {summary['global_head_gate_pass']} (max {summary['global_head_max_operations']} op / {summary['global_head_max_bytes']:,} B)",
        "",
        "## Inventory",
        "",
        "| component | objects | bytes |",
        "|---|---:|---:|",
    ]
    for name, value in inventory["components"].items():
        lines.append(f"| {name} | {value['objects']:,} | {value['bytes']:,} |")
    lines += [
        "",
        "## Queries",
        "",
        "| query | fallback ops/bytes | located ops/bytes | head ops/bytes | top-k exact |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in report["queries"]:
        fallback = row["fallback"]
        located = row["located"]
        head = row["global_head"]
        located_text = (
            "n/a"
            if not located
            else f"{located['operations']} / {located['bytes_transferred']:,}"
        )
        head_text = (
            "n/a"
            if not head
            else f"{head['operations']} / {head['bytes_transferred']:,}"
        )
        exact = (located["top_k_exact"] if located else True) and (
            head["top_k_exact"] if head else True
        )
        lines.append(
            f"| {row['name']} | {fallback['operations']} / {fallback['bytes_transferred']:,} | "
            f"{located_text} | {head_text} | {exact} |"
        )
    lines += [
        "",
        "## Semantics",
        "",
        "Located-query numbers use an optimistic router: the preferred cell is the cell containing the globally highest-ranked match. Located queries still traverse complete postings, then filter to that cell before ranking and hydrating cell-clustered result pages.",
        "",
        "The global head is authoritative only for top-k under the experiment's static confidence ranking. It intentionally omits the candidate tail and is never reported as complete candidate recall. Ineligible queries use the complete fallback.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument("--head-minimum-candidates", type=int, default=64)
    parser.add_argument("--head-famous-cap", type=int, default=0)
    args = parser.parse_args(argv)
    if args.head_minimum_candidates <= 0:
        parser.error("--head-minimum-candidates must be positive")
    if args.head_famous_cap < 0:
        parser.error("--head-famous-cap cannot be negative")
    report = build_report(
        load_places(args.input), args.head_minimum_candidates, args.head_famous_cap
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2) + "\n")
    args.markdown_out.write_text(markdown(report) + "\n")
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
