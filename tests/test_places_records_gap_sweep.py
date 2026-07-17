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


# Same-rank filler docs interleaved between adjacent chain branches. Each filler
# projection is ~59 bytes, so adjacent served chain records sit about
# FILLER_PER_GAP * 59 B (~17.7 KiB) apart in the rank-laid-out records blob:
# within the 64Ki/256Ki coalesce buckets but beyond 0/4Ki/16Ki, so the sweep
# genuinely discriminates between the swept thresholds.
CHAIN_BRANCHES = 12
FILLER_PER_GAP = 300


def modeled_places() -> list[Place]:
    """A rank-band-scattered same-brand chain inside dense equal-rank filler.

    The chain branches share one confidence with a large equal-rank filler
    population. The rank layout's in-band tiebreak is doc id, every place shares
    one spatial cell, and doc order within the cell follows place_id, so the
    interleaved ids ({block}-a-chain < {block}-b-filler < {block+1}-a-chain)
    separate adjacent served chain records by FILLER_PER_GAP filler records
    (~17.7 KiB) in the records blob. This is a scaled-down analogue of the real
    7-Eleven scatter, sized so the swept gap thresholds straddle the actual
    inter-record gaps instead of trivially coalescing at every setting. Filler
    tokens are unique, so no filler matches the chain clauses and the postings
    stage stays small.
    """
    places: list[Place] = []
    for block in range(CHAIN_BRANCHES):
        places.append(
            _place(
                place_id=f"{block:04}-a-chain",
                name=f"Seven Eleven Store {block}",
                brand="Seven Eleven",
                category="convenience store",
                lat=35.01,
                lon=139.01,
                confidence=0.90,
            )
        )
        for index in range(FILLER_PER_GAP):
            places.append(
                _place(
                    place_id=f"{block:04}-b-{index:04}",
                    name=f"Unique Landmark {block:04}x{index:04}",
                    brand="",
                    category="shop",
                    lat=35.01,
                    lon=139.01,
                    confidence=0.90,
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
    """The modeled chain_name case genuinely needs coalescing, and the sweep
    discriminates between the swept thresholds: the ~17.7 KiB inter-record gaps
    split at 0/4Ki/16Ki (busting the read gate) and coalesce at 64Ki/256Ki."""
    result = _sweep(tmp_path)
    by_gap = {entry["gap"]: entry for entry in result["table"]}
    chain_at_zero = next(
        row for row in by_gap[0]["rows"] if row["case"] == "chain_name"
    )
    assert chain_at_zero["candidate_count"] > 10
    assert chain_at_zero["records_reads"] > 1
    # Sub-threshold gaps bust the read gate; the sweep is not vacuous.
    for gap in (0, 4 * 1024, 16 * 1024):
        assert by_gap[gap]["max_cold_reads"] > MAX_COLD_READS
    for gap in (64 * 1024, 256 * 1024):
        assert by_gap[gap]["max_cold_reads"] <= MAX_COLD_READS


def test_configured_gap_is_smallest_in_the_min_byte_passing_tier(tmp_path):
    """The producer/reader constant (mirrored in the Worker) keeps every modeled
    case within the cold gate and is the smallest swept gap in the sweep's
    minimum-byte passing tier.

    For the binding chain case the other stages already consume the 7 non-record
    reads of a two-clause query, so the records/record_index stages must each
    coalesce to a single physical read. Every gap large enough to do that
    fetches the same rank-local span (identical bytes), so within the passing
    tier the smallest gap wins: it bounds worst-case dead-gap overfetch on
    shapes the model does not cover while still coalescing the modeled scatter.
    The post-merge credentialed smoke confirms the choice on real data."""
    result = _sweep(tmp_path)
    table = result["table"]
    assert shard.RECORD_INDEX_COALESCE_GAP == shard.RECORDS_COALESCE_GAP
    configured = shard.RECORDS_COALESCE_GAP
    assert configured in {entry["gap"] for entry in table}
    entry = next(item for item in table if item["gap"] == configured)
    assert entry["max_cold_reads"] <= MAX_COLD_READS
    assert entry["max_cold_bytes"] <= MAX_COLD_BYTES
    min_bytes = _min_passing_bytes(table)
    assert entry["max_cold_bytes"] == min_bytes
    tier_gaps = [
        item["gap"]
        for item in _passing(table)
        if item["max_cold_bytes"] == min_bytes
    ]
    assert configured == min(tier_gaps)
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
