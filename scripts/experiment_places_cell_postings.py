#!/usr/bin/env python3
"""Model cell-local heavy postings plus the packed global top-k head.

This offline spike extends the locality/head experiment with deterministic
cell-and-term posting buckets. Located queries use complete cell-local postings
when every clause is eligible; all other queries retain the complete global
fallback. No Cloudflare resources are read or written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from experiment_places_compact_index import Place, load_places, normalize  # noqa: E402
from experiment_places_kv_r2_pages import (  # noqa: E402
    CASES,
    FIELD_BITS,
    PAGE_HEADER_BYTES,
    Clause,
    Page,
    QueryCase,
    derived_key,
    posting_pages,
    posting_payload_size,
)
from experiment_places_locality_head import (  # noqa: E402
    HEAD_PREFIX_LENGTHS,
    LocalityHeadIndex,
)


def merge_docs(
    target: dict[int, tuple[int, int]],
    source: dict[int, tuple[int, int]],
) -> None:
    for doc_id, (mask, rank) in source.items():
        old_mask, old_rank = target.get(doc_id, (0, rank))
        target[doc_id] = (old_mask | mask, max(old_rank, rank))


def heavy_local_mappings(
    exact: dict[str, dict[int, tuple[int, int]]],
    direct_prefixes: dict[str, dict[int, tuple[int, int]]],
    minimum_candidates: int,
) -> dict[str, dict[int, tuple[int, int]]]:
    """Return complete mappings for globally heavy exact terms and prefixes."""

    mappings = {
        f"e:{term}": docs
        for term, docs in exact.items()
        if len(docs) >= minimum_candidates
    }
    for prefix, docs in direct_prefixes.items():
        if len(docs) >= minimum_candidates:
            mappings[f"p:{prefix}"] = docs

    approximate: dict[str, int] = defaultdict(int)
    for term, docs in exact.items():
        for length in HEAD_PREFIX_LENGTHS:
            if length > 4 and len(term) >= length:
                approximate[term[:length]] += len(docs)
    eligible = {
        prefix for prefix, count in approximate.items() if count >= minimum_candidates
    }
    long_prefixes: dict[str, dict[int, tuple[int, int]]] = defaultdict(dict)
    for term, docs in exact.items():
        prefixes = {
            term[:length]
            for length in HEAD_PREFIX_LENGTHS
            if length > 4 and len(term) >= length and term[:length] in eligible
        }
        for prefix in prefixes:
            merge_docs(long_prefixes[prefix], docs)
    for prefix, docs in long_prefixes.items():
        if len(docs) >= minimum_candidates:
            mappings[f"p:{prefix}"] = docs
    return mappings


@dataclass(frozen=True)
class LocalPostingEntry:
    key: str
    cell: str
    inline_bytes: int
    overflow: tuple[Page, ...]


class CellPostingStore:
    """Pack complete term postings into deterministic per-cell hash buckets."""

    def __init__(
        self,
        release: str,
        mappings: dict[str, dict[int, tuple[int, int]]],
        cells: list[str],
        target: int = 64 * 1024,
        bucket_count: int = 64,
    ):
        self.mappings = mappings
        self.target = target
        capacity = target - PAGE_HEADER_BYTES
        entries: list[LocalPostingEntry] = []
        for key, docs in mappings.items():
            grouped: dict[str, dict[int, tuple[int, int]]] = defaultdict(dict)
            for doc_id, value in docs.items():
                grouped[cells[doc_id]][doc_id] = value
            for cell, local_docs in grouped.items():
                payload = posting_payload_size(local_docs)
                if payload > capacity // 2:
                    overflow = tuple(
                        posting_pages(
                            release,
                            "cell-post-overflow",
                            f"{cell}:{key}",
                            local_docs,
                            target,
                        )
                    )
                    inline = 32
                else:
                    overflow = ()
                    inline = 16 + payload
                entries.append(LocalPostingEntry(key, cell, inline, overflow))

        entries_by_cell: dict[str, list[LocalPostingEntry]] = defaultdict(list)
        for entry in entries:
            entries_by_cell[entry.cell].append(entry)
        self.bucket_counts: dict[str, int] = {}
        payloads: dict[tuple[str, int], int] = {}
        for cell, cell_entries in entries_by_cell.items():
            count = max(1, bucket_count)
            while True:
                cell_payloads: dict[int, int] = defaultdict(int)
                for entry in cell_entries:
                    cell_payloads[self._hash_bucket(entry.key, count)] += (
                        entry.inline_bytes
                    )
                if all(payload <= capacity for payload in cell_payloads.values()):
                    break
                count *= 2
            self.bucket_counts[cell] = count
            for bucket, payload in cell_payloads.items():
                payloads[(cell, bucket)] = payload

        roots = {
            pair: Page(
                derived_key(release, "cell-post-bucket", f"{pair[0]}:{pair[1]}", 0),
                PAGE_HEADER_BYTES + payload,
                payload,
                0,
            )
            for pair, payload in payloads.items()
        }
        root_counts: dict[tuple[str, int], int] = defaultdict(int)
        self.pages_by_pair: dict[tuple[str, str], list[Page]] = {}
        overflow_pages: dict[str, Page] = {}
        for entry in entries:
            pair = (entry.cell, self._bucket(entry.cell, entry.key))
            root_counts[pair] += 1
            self.pages_by_pair[(entry.cell, entry.key)] = [roots[pair], *entry.overflow]
            for page in entry.overflow:
                overflow_pages[page.key] = page
        self.roots = [
            Page(page.key, page.size, page.payload_bytes, root_counts[pair])
            for pair, page in roots.items()
        ]
        self.overflow = list(overflow_pages.values())
        self.entry_count = len(entries)

    @staticmethod
    def _hash_bucket(key: str, count: int) -> int:
        return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") % count

    def _bucket(self, cell: str, key: str) -> int:
        return self._hash_bucket(key, self.bucket_counts[cell])

    def pages(self, cell: str, key: str) -> list[Page]:
        return self.pages_by_pair.get((cell, key), [])

    def all_pages(self) -> list[Page]:
        return [*self.roots, *self.overflow]


def clause_key(clause: Clause) -> str | None:
    value = normalize(clause.value)
    if clause.prefix and len(value) not in HEAD_PREFIX_LENGTHS:
        return None
    return f"{'p' if clause.prefix else 'e'}:{value}"


class CellPostingIndex(LocalityHeadIndex):
    def __init__(
        self,
        places: list[Place],
        release: str = "fixture-current",
        cell_posting_minimum_candidates: int = 64,
        cell_posting_target: int = 64 * 1024,
        cell_posting_bucket_count: int = 64,
        **kwargs: Any,
    ):
        super().__init__(places, release=release, **kwargs)
        self.cell_posting_minimum_candidates = cell_posting_minimum_candidates
        self.local_mappings = heavy_local_mappings(
            self.base.exact,
            self.base.prefixes,
            cell_posting_minimum_candidates,
        )
        self.cell_posting_store = CellPostingStore(
            release,
            self.local_mappings,
            self.cells,
            cell_posting_target,
            cell_posting_bucket_count,
        )

    def query(
        self,
        case: QueryCase,
        *,
        cell: str | None = None,
        use_global_head: bool = False,
        limit: int = 10,
    ) -> dict[str, Any]:
        if cell is None or use_global_head:
            return super().query(
                case, cell=cell, use_global_head=use_global_head, limit=limit
            )

        resolved: list[tuple[Clause, str, dict[int, tuple[int, int]]]] = []
        for clause in case.clauses:
            key = clause_key(clause)
            mapping = self.local_mappings.get(key or "")
            if key is None or mapping is None:
                fallback = super().query(case, cell=cell, limit=limit)
                fallback["mode"] = "cell_global_fallback"
                fallback["local_clause_count"] = 0
                return fallback
            resolved.append((clause, key, mapping))

        fetched: dict[str, Page] = {}
        candidates: set[int] | None = None
        for clause, key, mapping in resolved:
            docs = {
                doc: value for doc, value in mapping.items() if self.cells[doc] == cell
            }
            if clause.field:
                bit = FIELD_BITS[clause.field]
                docs = {doc: value for doc, value in docs.items() if value[0] & bit}
            for page in self.cell_posting_store.pages(cell, key):
                fetched[page.key] = page
            doc_ids = set(docs)
            candidates = doc_ids if candidates is None else candidates & doc_ids
        candidates = candidates or set()
        docs = self.ranked(candidates, limit)
        for doc in docs:
            page = self.doc_to_result_page[doc]
            fetched[page.key] = page

        oracle = self.oracle_candidates(case, cell)
        oracle_top = self.ranked(oracle, limit)
        return {
            "mode": "cell_local_postings",
            "coverage": "complete candidate traversal within the routed cell",
            "cell": cell,
            "candidate_count": len(candidates),
            "result_ids": [self.places[doc].place_id for doc in docs],
            "oracle_ids": [self.places[doc].place_id for doc in oracle_top],
            "top_k_exact": docs == oracle_top,
            "complete_candidate_recall": candidates == oracle,
            "operations": len(fetched),
            "bytes_transferred": sum(page.size for page in fetched.values()),
            "local_clause_count": len(resolved),
            "global_clause_candidate_counts": [
                len(mapping) for _, _, mapping in resolved
            ],
        }

    def inventory(self) -> dict[str, Any]:
        inventory = super().inventory()
        pages = self.cell_posting_store.all_pages()
        inventory["components"]["cell_local_postings"] = {
            "objects": len(pages),
            "bytes": sum(page.size for page in pages),
            "term_cell_entries": self.cell_posting_store.entry_count,
            "keys": len(self.local_mappings),
            "configured_hash_buckets": sum(
                self.cell_posting_store.bucket_counts.values()
            ),
            "minimum_hash_buckets_per_cell": min(
                self.cell_posting_store.bucket_counts.values(), default=0
            ),
            "maximum_hash_buckets_per_cell": max(
                self.cell_posting_store.bucket_counts.values(), default=0
            ),
        }
        inventory["objects"] += len(pages)
        inventory["bytes"] += sum(page.size for page in pages)
        inventory["bytes_per_place"] = inventory["bytes"] / len(self.places)
        return inventory


def build_report(
    places: list[Place],
    head_minimum_candidates: int = 64,
    cell_minimum_candidates: int = 1800,
) -> dict[str, Any]:
    index = CellPostingIndex(
        places,
        head_minimum_candidates=head_minimum_candidates,
        cell_posting_minimum_candidates=cell_minimum_candidates,
    )
    rows = []
    for case in CASES:
        fallback = index.query(case)
        cell = index.preferred_cell(case)
        located = index.query(case, cell=cell) if cell else None
        head = index.query(case, use_global_head=True)
        rows.append(
            {
                "name": case.name,
                "fallback": fallback,
                "located": located,
                "global_head": head if head["mode"] == "global_head" else None,
            }
        )
    located = [row["located"] for row in rows if row["located"]]
    local = [row for row in located if row["mode"] == "cell_local_postings"]
    inventory = index.inventory()
    return {
        "schema_version": 1,
        "input_places": len(places),
        "architecture": {
            "cell_degrees": index.cell_degrees,
            "head_minimum_candidates": head_minimum_candidates,
            "cell_minimum_candidates": cell_minimum_candidates,
            "cell_posting_target": index.cell_posting_store.target,
            "located_routing_model": "optimistic: cell containing the globally highest-ranked match",
            "fallback": "complete global postings whenever a clause lacks a cell-local mapping",
        },
        "inventory": inventory,
        "queries": rows,
        "summary": {
            "located_query_count": len(located),
            "cell_local_query_count": len(local),
            "cell_local_complete_recall": bool(local)
            and all(row["complete_candidate_recall"] for row in local),
            "located_max_operations": max(
                (row["operations"] for row in located), default=0
            ),
            "located_max_bytes": max(
                (row["bytes_transferred"] for row in located), default=0
            ),
            "located_gate_pass": bool(located)
            and all(
                row["operations"] <= 3 and row["bytes_transferred"] <= 256 * 1024
                for row in located
            ),
        },
    }


def markdown(report: dict[str, Any]) -> str:
    inventory = report["inventory"]
    summary = report["summary"]
    lines = [
        "# Places cell-local postings spike",
        "",
        f"- Input: {report['input_places']:,} Places",
        f"- Total modeled bytes: {inventory['bytes']:,} ({inventory['bytes_per_place']:.1f} B/place)",
        f"- Located gate: {summary['located_gate_pass']} (max {summary['located_max_operations']} ops / {summary['located_max_bytes']:,} B)",
        f"- Cell-local complete recall: {summary['cell_local_complete_recall']}",
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
        "| query | located mode | fallback ops/bytes | located ops/bytes | head ops/bytes |",
        "|---|---|---:|---:|---:|",
    ]
    for row in report["queries"]:
        fallback = row["fallback"]
        located = row["located"]
        head = row["global_head"]
        lines.append(
            f"| {row['name']} | {located['mode'] if located else 'n/a'} | "
            f"{fallback['operations']} / {fallback['bytes_transferred']:,} | "
            f"{located['operations']} / {located['bytes_transferred']:,}"
            if located
            else f"| {row['name']} | n/a | {fallback['operations']} / {fallback['bytes_transferred']:,} | n/a"
        )
        if located:
            head_text = (
                "n/a"
                if not head
                else f"{head['operations']} / {head['bytes_transferred']:,}"
            )
            lines[-1] += f" | {head_text} |"
        else:
            lines[-1] += " | n/a |"
    lines += [
        "",
        "## Semantics",
        "",
        "Cell-local mappings contain every matching document within an eligible routed cell; they are not top-k approximations. Ineligible clauses use the complete global fallback.",
        "",
        "The global head remains top-k-only under static rank. Located numbers use the same optimistic preferred-cell model as the preceding experiment.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument("--head-minimum-candidates", type=int, default=64)
    parser.add_argument("--cell-minimum-candidates", type=int, default=1800)
    args = parser.parse_args(argv)
    if args.head_minimum_candidates <= 0:
        parser.error("--head-minimum-candidates must be positive")
    if args.cell_minimum_candidates <= 0:
        parser.error("--cell-minimum-candidates must be positive")
    report = build_report(
        load_places(args.input),
        args.head_minimum_candidates,
        args.cell_minimum_candidates,
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2) + "\n")
    args.markdown_out.write_text(markdown(report) + "\n")
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
