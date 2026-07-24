#!/usr/bin/env python3
"""Generate the committed Places partition plan.

The hosted build must not re-derive the reducer partition tree every run: doing
so forces a global barrier and makes every reducer read the whole term set. This
script derives the tree once, offline, on a machine big enough to hold the term
universe. The hosted build then assigns partitions locally from the committed
result and only verifies, per partition, that no cap was breached.

Two sources:

  --from-plan   an existing adaptive-genesis plan (the cheap path: reuse a tree a
                previous run already derived, e.g. to seed v1 or to re-buffer an
                existing plan without recomputing it)
  --packs       term-row pack parquets, aggregated with DuckDB (the real path,
                used when the distribution has to be measured from source data)

Both then apply the same optional headroom policy and emit the same format.

Headroom pre-splits any leaf already above ``--headroom-fraction`` of a cap, so
the plan tolerates growth between regenerations. Splitting a leaf is a pure tree
operation - the children's contents are determined at map time - so it needs no
extra measurement, and because the prefix is a hash each child inherits roughly
a sixteenth of its parent's load.

Examples:

    # Reproduce a committed plan exactly (the correctness proof).
    python scripts/generate_places_partition_plan.py --from-plan plan.json \\
      --release 2026-06-17.0 --check scripts/places_partition_plan_v1.json

    # Re-buffer an existing tree with 50% headroom.
    python scripts/generate_places_partition_plan.py --from-plan plan.json \\
      --release 2026-06-17.0 --headroom-fraction 0.5 --output plan-v2.json

    # Measure from source packs.
    python scripts/generate_places_partition_plan.py --packs 'store/**/*.parquet' \\
      --release 2026-07-22.0 --headroom-fraction 0.5 --output plan-v2.json
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "overture-places-partition-plan-v1"
SCHEME_KIND = "token-sha256-nibble-prefix-v1"
MAXIMUM_DEPTH = 8
# Mirrors construction_v1_hosted.MEASURED_REDUCE_MINUTES_PER_PARTITION["places"].
REDUCE_MINUTES_PER_PARTITION = 1.0

DEFAULT_CAPS = {
    "term_rows": 1_000_000,
    "estimated_uncompressed_bytes": 536_870_912,
    "distinct_tokens": 200_000,
    "maximum_depth": MAXIMUM_DEPTH,
}


@dataclass(frozen=True)
class Leaf:
    """One partition: a cell, plus the hash-prefix branch that owns it."""

    cell: str
    depth: int
    prefix: int
    rows: int
    bytes_: int
    tokens: int

    def utilisation(self, caps: dict[str, int]) -> float:
        """Fraction of the tightest cap this leaf consumes."""
        return max(
            self.rows / caps["term_rows"],
            self.bytes_ / caps["estimated_uncompressed_bytes"],
            self.tokens / caps["distinct_tokens"],
        )

    def over(self, caps: dict[str, int]) -> bool:
        return self.utilisation(caps) > 1.0


def prefix_of(token_hash: int, depth: int) -> int:
    """Top ``depth`` nibbles of the token hash.

    Mirrors ``places_construction_v1._prefix_sql``: ``token_hash >> (64 - d*4)``.
    """
    if depth <= 0 or depth > MAXIMUM_DEPTH:
        raise ValueError("Places adaptive prefix depth is invalid")
    return token_hash >> (64 - depth * 4)


def leaves_from_plan(path: Path) -> list[Leaf]:
    """Read leaves out of an adaptive-genesis plan a previous run derived."""
    return leaves_from_plan_payload(json.loads(path.read_text()))


def leaves_from_plan_payload(payload: dict[str, Any]) -> list[Leaf]:
    partitions = payload.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        raise ValueError("plan has no partitions")
    leaves = []
    for partition in partitions:
        ownership = partition["ownership"]
        if ownership.get("kind", SCHEME_KIND) != SCHEME_KIND:
            raise ValueError(f"unexpected ownership kind {ownership.get('kind')!r}")
        leaves.append(
            Leaf(
                cell=partition["partition_cell"],
                depth=int(ownership["depth"]),
                prefix=int(ownership["prefix"]),
                rows=int(partition["term_rows"]),
                bytes_=int(partition["estimated_uncompressed_bytes"]),
                tokens=int(partition["distinct_tokens"]),
            )
        )
    return leaves


def leaves_from_packs(
    paths: list[str], caps: dict[str, int], *, memory_limit: str, threads: int
) -> list[Leaf]:
    """Derive leaves by aggregating term-row packs, subdividing over-cap cells.

    Mirrors ``places_construction_v1.adaptive_genesis_plan``: aggregate per cell,
    and while a cell (or hash-prefix branch of one) exceeds any cap, subdivide it
    one nibble deeper.
    """
    import duckdb

    connection = duckdb.connect()
    connection.execute(f"SET memory_limit='{memory_limit}'")
    connection.execute(f"SET threads={threads}")
    quoted = ", ".join("'" + path.replace("'", "''") + "'" for path in paths)
    byte_expression = "96+" + "+".join(
        f"octet_length(encode({field}))"
        for field in (
            "execution_group",
            "partition_cell",
            "token",
            "primary_name",
            "brand_name",
            "category",
            "locality",
            "region",
            "country",
        )
    )
    connection.execute(
        "CREATE TABLE planning AS SELECT partition_cell, token, token_hash, "
        f"({byte_expression})::UBIGINT estimated_bytes FROM read_parquet([{quoted}])"
    )

    leaves: list[Leaf] = []

    def emit(cell: str, depth: int, prefix: int, rows: int, size: int, tokens: int) -> None:
        leaf = Leaf(cell, depth, prefix, rows, size, tokens)
        if not leaf.over(caps):
            leaves.append(leaf)
            return
        if depth >= caps["maximum_depth"]:
            raise ValueError(
                f"partition {cell} remains over cap at maximum depth {depth}"
            )
        where = f"partition_cell='{cell}'"
        if depth:
            where += f" AND (token_hash >> {64 - depth * 4})={prefix}"
        children = connection.execute(
            f"SELECT (token_hash >> {64 - (depth + 1) * 4})::UBIGINT child, "
            "count(*)::UBIGINT, sum(estimated_bytes)::UBIGINT, "
            f"count(DISTINCT token)::UBIGINT FROM planning WHERE {where} "
            "GROUP BY child ORDER BY child"
        ).fetchall()
        if not children:
            raise ValueError(f"subdivision of {cell} produced no children")
        for child, child_rows, child_size, child_tokens in children:
            emit(cell, depth + 1, int(child), int(child_rows), int(child_size), int(child_tokens))

    cells = connection.execute(
        "SELECT partition_cell, count(*)::UBIGINT, sum(estimated_bytes)::UBIGINT, "
        "count(DISTINCT token)::UBIGINT FROM planning GROUP BY partition_cell "
        "ORDER BY partition_cell"
    ).fetchall()
    for cell, rows, size, tokens in cells:
        emit(cell, 0, 0, int(rows), int(size), int(tokens))
    connection.close()
    return leaves


def apply_headroom(
    leaves: Iterable[Leaf], caps: dict[str, int], fraction: float | None
) -> tuple[list[Leaf], int]:
    """Split every leaf already above ``fraction`` of a cap one nibble deeper.

    The children's measured stats are unknown and deliberately left at zero: the
    committed tree records structure, and the hosted gate measures reality. A
    hash prefix distributes roughly evenly, so each child inherits about a
    sixteenth of the parent's load.
    """
    if fraction is None:
        return sorted(leaves, key=lambda leaf: (leaf.cell, leaf.depth, leaf.prefix)), 0
    result: list[Leaf] = []
    split = 0
    for leaf in leaves:
        if leaf.utilisation(caps) > fraction and leaf.depth < caps["maximum_depth"]:
            split += 1
            for child in range(16):
                result.append(
                    Leaf(leaf.cell, leaf.depth + 1, (leaf.prefix << 4) | child, 0, 0, 0)
                )
        else:
            result.append(leaf)
    return sorted(result, key=lambda leaf: (leaf.cell, leaf.depth, leaf.prefix)), split


def build_tree(leaves: Iterable[Leaf]) -> dict[str, list[str]]:
    """Committed form: only subdivided branches. An absent cell is depth 0."""
    tree: dict[str, list[str]] = {}
    for leaf in leaves:
        if leaf.depth:
            tree.setdefault(leaf.cell, []).append(
                f"{leaf.depth}:{leaf.prefix:0{leaf.depth}x}"
            )
    return {cell: sorted(branches) for cell, branches in sorted(tree.items())}


def render(document: dict[str, Any]) -> bytes:
    """One cell per line so a partition change is readable in a diff."""
    lines = ["{"]
    for key, value in document.items():
        if key == "cells":
            continue
        lines.append(f"  {json.dumps(key)}: {json.dumps(value, sort_keys=True)},")
    lines.append('  "cells": {')
    items = list(document["cells"].items())
    for index, (cell, branches) in enumerate(items):
        comma = "," if index < len(items) - 1 else ""
        lines.append(
            f"    {json.dumps(cell)}: {json.dumps(branches, separators=(',', ':'))}{comma}"
        )
    lines.append("  }")
    lines.append("}")
    return ("\n".join(lines) + "\n").encode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-plan", type=Path, help="an adaptive-genesis plan JSON")
    source.add_argument("--packs", help="glob of term-row pack parquets")
    parser.add_argument("--release", required=True, help="Overture release the plan describes")
    parser.add_argument("--source-run", default=None, help="provenance: run that produced the input")
    parser.add_argument("--plan-version", type=int, default=1)
    parser.add_argument(
        "--headroom-fraction",
        default=None,
        help="pre-split leaves above this fraction of a cap (e.g. 0.5); omit for none",
    )
    parser.add_argument("--caps", type=Path, default=None, help="JSON overriding the caps")
    parser.add_argument("--memory-limit", default="8GB")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--check",
        type=Path,
        default=None,
        help="compare against an existing plan and fail if the bytes differ",
    )
    args = parser.parse_args(argv)

    caps = dict(DEFAULT_CAPS)
    if args.caps:
        caps.update(json.loads(args.caps.read_text()))
    fraction = None if args.headroom_fraction is None else float(args.headroom_fraction)

    if args.from_plan:
        leaves = leaves_from_plan(args.from_plan)
    else:
        paths = sorted(glob.glob(args.packs, recursive=True))
        if not paths:
            raise SystemExit(f"no packs matched {args.packs!r}")
        leaves = leaves_from_packs(
            paths, caps, memory_limit=args.memory_limit, threads=args.threads
        )

    measured = [leaf for leaf in leaves if leaf.rows or leaf.bytes_ or leaf.tokens]
    worst = max((leaf.utilisation(caps) for leaf in measured), default=0.0)
    before = len(leaves)
    leaves, split = apply_headroom(leaves, caps, fraction)
    tree = build_tree(leaves)

    document = {
        "schema": SCHEMA,
        "plan_version": args.plan_version,
        "generated_from": {
            "overture_release": args.release,
            "source_run": args.source_run,
            "partitions": len(leaves),
            "term_rows": sum(leaf.rows for leaf in measured),
        },
        "partition_contract": {"kind": SCHEME_KIND, "caps": caps},
        "headroom": {
            "policy": "threshold" if fraction is not None else "none",
            "threshold": fraction,
            "pre_split_partitions": split,
        },
        "cells": tree,
    }
    payload = render(document)

    projected = len(leaves) * REDUCE_MINUTES_PER_PARTITION
    print(
        f"partitions {before:,} -> {len(leaves):,} "
        f"({split:,} pre-split, {len(tree):,} cells in tree, {len(payload):,} bytes)",
        file=sys.stderr,
    )
    print(
        f"worst measured utilisation {worst:.3f} | "
        f"projected reduce minutes {projected:,.0f}",
        file=sys.stderr,
    )

    if args.check:
        existing = args.check.read_bytes()
        if existing != payload:
            print(
                f"generated plan differs from {args.check}", file=sys.stderr
            )
            return 1
        print(f"matches {args.check} byte for byte", file=sys.stderr)
    if args.output:
        args.output.write_bytes(payload)
    elif not args.check:
        sys.stdout.buffer.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
