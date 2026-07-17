#!/usr/bin/env python3
"""Offline coalesce-gap sweep for the rank-laid-out compact Places records stage.

With records laid out in global serving-rank order (see
`experiment_places_compact_shard.build_artifact`), this models the records /
record_index coalesce gaps over a representative case set that includes a
chain_name-shaped query (many similar-confidence same-brand branches whose
served window scatters through a dense equal-rank band). It sweeps the gap
thresholds {256Ki, 64Ki, 16Ki, 4Ki, 0}, reports max physical reads and bytes
per setting, and pins that the constants chosen in
`experiment_places_compact_shard` (mirrored in the Worker) keep every modeled
case within the routed cold gate: <= 8 physical reads and <= 512 KiB.

Run as a script to print the sweep table for the PR body:
    python tests/test_places_records_gap_sweep.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(module_name: str):
    script = SCRIPTS / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


shard = _load("experiment_places_compact_shard")
compact = _load("experiment_places_compact_index")
Place = compact.Place
Clause = shard.Clause
QueryCase = shard.QueryCase

# Gate that the routed shard read chain must clear cold (docs/plans spec).
MAX_COLD_READS = 8
MAX_COLD_BYTES = 512 * 1024
# Sweep from the loosest legacy setting down to no coalescing at all.
GAP_SWEEP = (256 * 1024, 64 * 1024, 16 * 1024, 4 * 1024, 0)


def _place(
    place_id: str,
    name: str,
    brand: str,
    category: str,
    lat: float,
    lon: float,
    confidence: float,
) -> Place:
    return Place(
        place_id=place_id,
        name=name,
        brand=brand,
        category=category,
        locality="City",
        region="RG",
        country="US",
        lat=lat,
        lon=lon,
        confidence=confidence,
    )


def modeled_places() -> list[Place]:
    """A representative shard: a scattered same-brand chain plus dense filler.

    The chain branches share a brand and a confidence, so in global rank order
    their served window lands inside a wide band of equal-rank filler records —
    the exact scatter that made the real 7-Eleven records stage span most of the
    component. Filler tokens are unique, so no filler clause matches the chain
    and the postings stage stays small.
    """
    places: list[Place] = []
    # 30 chain branches spread across a wide bbox (distinct spatial cells) at one
    # shared confidence, so the served top-10 are the 10 lowest-doc branches and
    # they scatter through the equal-rank filler band.
    for index in range(30):
        lat = 35.0 + (index % 6) * 0.4
        lon = 139.0 + (index // 6) * 0.4
        places.append(
            _place(
                place_id=f"chain-{index:03}",
                name=f"Seven Eleven Store {index}",
                brand="Seven Eleven",
                category="convenience store",
                lat=lat,
                lon=lon,
                confidence=0.90,
            )
        )
    # Dense equal-rank filler sharing the chain's confidence so it interleaves
    # with the chain in rank order, plus a spread of other ranks for realism.
    for index in range(1200):
        lat = 35.0 + (index % 30) * 0.12
        lon = 139.0 + (index // 30) * 0.12
        confidence = 0.90 if index % 2 == 0 else 0.50 + (index % 40) / 100.0
        places.append(
            _place(
                place_id=f"filler-{index:04}",
                name=f"Unique Landmark {index:04}",
                brand="",
                category="shop",
                lat=lat,
                lon=lon,
                confidence=confidence,
            )
        )
    return places


# The modeled case set. chain_name is the stress case; the others confirm no
# ordinary shape regresses at the chosen gap.
def modeled_cases() -> tuple[QueryCase, ...]:
    return (
        QueryCase("chain_name", (Clause("seven"), Clause("eleven")), "relevance"),
        QueryCase("brand_exact", (Clause("seven"),), "typical"),
        QueryCase("category_fielded", (Clause("shop", field="category"),), "typical"),
        QueryCase("name_prefix", (Clause("uniq", prefix=True),), "worst_supported"),
    )


def _sweep(tmp_path: Path) -> dict:
    artifact = tmp_path / "sweep.pcsh"
    ordered, _ = shard.build_artifact(modeled_places(), artifact)
    cases = modeled_cases()
    table: list[dict] = []
    for gap in GAP_SWEEP:
        rows = []
        for case in cases:
            reader = shard.CompactShard(artifact)
            result = reader.query(case, index_gap=gap, record_gap=gap)
            rows.append(
                {
                    "case": case.name,
                    "cold_reads": result["cold_range_reads"],
                    "cold_bytes": result["cold_bytes_transferred"],
                    "records_reads": result["stages"]["records"]["reads"],
                    "records_bytes": result["stages"]["records"]["bytes"],
                    "candidate_count": result["candidate_count"],
                }
            )
        table.append(
            {
                "gap": gap,
                "max_cold_reads": max(row["cold_reads"] for row in rows),
                "max_cold_bytes": max(row["cold_bytes"] for row in rows),
                "rows": rows,
            }
        )
    return {"ordered": ordered, "table": table}


def _passing(table: list[dict]) -> list[dict]:
    return [
        entry
        for entry in table
        if entry["max_cold_reads"] <= MAX_COLD_READS
        and entry["max_cold_bytes"] <= MAX_COLD_BYTES
    ]


def _min_passing_bytes(table: list[dict]) -> int:
    passing = _passing(table)
    assert passing, "no swept gap keeps every modeled case within the cold gate"
    return min(entry["max_cold_bytes"] for entry in passing)


def test_chain_name_scatter_is_bounded_only_by_coalescing(tmp_path):
    """The modeled chain_name case genuinely needs coalescing: gap 0 blows the
    read gate (both record_index and records split ~10 ways) and a nonzero gap
    is required to fit it."""
    result = _sweep(tmp_path)
    by_gap = {entry["gap"]: entry for entry in result["table"]}
    chain_at_zero = next(
        row for row in by_gap[0]["rows"] if row["case"] == "chain_name"
    )
    assert chain_at_zero["candidate_count"] > 10
    assert chain_at_zero["records_reads"] > 1
    # Without coalescing the scattered served window busts the read gate.
    assert by_gap[0]["max_cold_reads"] > MAX_COLD_READS


def test_configured_gap_is_in_the_min_byte_passing_tier(tmp_path):
    """The producer/reader constant (mirrored in the Worker) keeps every modeled
    case within the cold gate and sits at the minimum-byte tier of the sweep.

    For the binding chain case the other stages already consume the 7 non-record
    reads of a two-clause query, so the records/record_index stages must each
    coalesce to a single physical read. Every gap that achieves that fetches the
    same rank-local span, so modeled bytes are flat across the passing tier and
    the gap acts as a read-gate guardrail; the constant is chosen conservatively
    within that tier and the post-merge credentialed smoke confirms real data."""
    result = _sweep(tmp_path)
    table = result["table"]
    assert shard.RECORD_INDEX_COALESCE_GAP == shard.RECORDS_COALESCE_GAP
    configured = shard.RECORDS_COALESCE_GAP
    assert configured in {entry["gap"] for entry in table}
    entry = next(item for item in table if item["gap"] == configured)
    assert entry["max_cold_reads"] <= MAX_COLD_READS
    assert entry["max_cold_bytes"] <= MAX_COLD_BYTES
    assert entry["max_cold_bytes"] == _min_passing_bytes(table)
    chain_row = next(row for row in entry["rows"] if row["case"] == "chain_name")
    assert chain_row["cold_reads"] <= MAX_COLD_READS
    assert chain_row["cold_bytes"] <= MAX_COLD_BYTES


def _format_table(table: list[dict]) -> str:
    lines = [
        "| gap | max cold reads | max cold bytes | chain reads | chain records reads | chain records bytes |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for entry in table:
        chain = next(row for row in entry["rows"] if row["case"] == "chain_name")
        lines.append(
            f"| {entry['gap']:,} | {entry['max_cold_reads']} | "
            f"{entry['max_cold_bytes']:,} | {chain['cold_reads']} | "
            f"{chain['records_reads']} | {chain['records_bytes']:,} |"
        )
    return "\n".join(lines)


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        result = _sweep(Path(tmp))
        print(_format_table(result["table"]))
        print()
        min_bytes = _min_passing_bytes(result["table"])
        passing = sorted(
            entry["gap"] for entry in _passing(result["table"])
        )
        print(f"gaps passing the cold gate: {[f'{g:,}' for g in passing]}")
        print(f"minimum passing max-bytes tier: {min_bytes:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
