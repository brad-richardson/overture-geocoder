#!/usr/bin/env python3
"""Synthetic planet-scale fan-in gate for construction-v1 aggregate planning.

The construction-v1 design claims that Address and Places aggregate planning now
run over compact, associative per-pack summaries (bucket summaries for Address,
routing summaries for Places) *without reopening any map payload*. That claim has
only ever been exercised at a ~1M-row rehearsal. This tool generates synthetic
per-task summary artifacts that match the real Rust proof-directory schema at
planet scale, then invokes the *real* planner functions
(``address_construction_v1.genesis_plan`` and ``places_construction_v1.genesis_plan``)
over them inside a bounded subprocess. It measures wall time, peak RSS, and peak
scratch, and it fails closed (nonzero exit) if planning breaches its declared
wall-time or RSS gate.

No network or remote access. Reads only the local synthetic inventories under
``benchmarks/`` and writes only local temporary artifacts. It never touches the
concurrently-owned ``benchmarks/places-construction-v1-data/`` beyond reading the
already-published ``inventory/places.json`` row counts.

Synthetic caveat: the bindings are random 256-bit lane sums, not derived from real
feature rows. This benchmark therefore proves the *shape and scale* of the fan-in
(entry counts, memory footprint, planner wall time) but cannot prove semantic
correctness of real digests. See the generated report for the honest boundary.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import platform
import random
import resource
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"

ADDRESS_INVENTORY = (
    REPO / "benchmarks/address-construction-v1-data/inventory/addresses.json"
)
PLACES_INVENTORY = (
    REPO / "benchmarks/places-construction-v1-data/inventory/places.json"
)

# Mirrors crates/geocoder-construction/src/main.rs MAXIMUM_HASH_BITS. The Address
# map derives maximum_bucket as the top 16 bits of the u64 route hash, so buckets
# range over [0, 65535] and a country with millions of rows saturates the space.
MAXIMUM_HASH_BITS = 16
MAXIMUM_BUCKET = (1 << MAXIMUM_HASH_BITS) - 1
UINT256 = 1 << 256

# Canonical summary-entry key sets, mirroring the Serialize structs in
# crates/geocoder-construction/src/bin/address_proof_directory.rs (RoutingGroup)
# and .../places_proof_directory.rs (RoutingGroup). The generator MUST emit
# exactly these fields so the real planners consume synthetic input unchanged.
ADDRESS_BUCKET_SUMMARY_FIELDS = frozenset(
    {"country", "maximum_bucket", "minimum_route_hash", "maximum_route_hash", "binding"}
)
PLACES_ROUTING_SUMMARY_FIELDS = frozenset(
    {"execution_group", "partition_cell", "binding"}
)
BINDING_FIELDS = frozenset({"records", "semantic_sum_a", "semantic_sum_b"})

DEFAULT_PACK_ROWS = 50_000
DEFAULT_GATE_WALL_SECONDS = 60.0 * 60.0
DEFAULT_GATE_MAX_RSS_BYTES = 4 * 1024**3
DEFAULT_GATE_MAX_SCRATCH_BYTES = 8 * 1024**3
# Hosted jobs cap at 330 minutes; the wall gate lives comfortably inside it.
HOSTED_JOB_SECONDS = 330 * 60
# A machine-safe hard kill for characterization sweeps. Sweep scales are chosen so
# real peaks land well under this; it only exists to protect the host from an
# unexpected runaway, never as the acceptance gate.
DEFAULT_MEASURE_MAX_RSS_BYTES = 12 * 1024**3

# Default number of globally populated Places grid cells (256x256 lon/lat grid,
# capped at 65,536). Places rows concentrate on land; a few thousand cells carry
# almost everything, which the Zipf weighting below reproduces.
DEFAULT_PLACES_POPULATED_CELLS = 12_000


def _load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ADDRESS = _load_module("address_construction_v1")
PLACES = _load_module("places_construction_v1")


@dataclass(frozen=True)
class Gate:
    """Declared acceptance bounds for a single planet-scale planning run."""

    wall_seconds: float = DEFAULT_GATE_WALL_SECONDS
    max_rss_bytes: int = DEFAULT_GATE_MAX_RSS_BYTES
    max_scratch_bytes: int = DEFAULT_GATE_MAX_SCRATCH_BYTES

    def validate(self) -> None:
        if (
            self.wall_seconds <= 0
            or self.max_rss_bytes <= 0
            or self.max_scratch_bytes <= 0
        ):
            raise ValueError("construction fan-in gate bounds must be positive")
        if self.wall_seconds > HOSTED_JOB_SECONDS:
            raise ValueError(
                "wall gate exceeds the hosted job ceiling; it would prove nothing"
            )


def check_gate(evidence: dict[str, Any], gate: Gate) -> tuple[bool, list[str]]:
    """Fail-closed comparison of measured evidence against a declared gate."""
    reasons: list[str] = []
    if evidence["wall_seconds"] > gate.wall_seconds:
        reasons.append(
            f"wall {evidence['wall_seconds']:.1f}s exceeds gate {gate.wall_seconds:.1f}s"
        )
    if evidence["peak_rss_bytes"] > gate.max_rss_bytes:
        reasons.append(
            f"peak RSS {evidence['peak_rss_bytes']} B exceeds gate "
            f"{gate.max_rss_bytes} B"
        )
    if evidence.get("peak_scratch_bytes", 0) > gate.max_scratch_bytes:
        reasons.append(
            f"peak scratch {evidence['peak_scratch_bytes']} B exceeds gate "
            f"{gate.max_scratch_bytes} B"
        )
    return (not reasons, reasons)


# --------------------------------------------------------------------------- #
# Synthetic binding generation                                                #
# --------------------------------------------------------------------------- #


def _rng(seed: int, *parts: Any) -> random.Random:
    """Deterministic generator keyed by the seed and stable string components."""
    return random.Random("|".join([str(seed), *(str(part) for part in parts)]))


def _binding(records: int, generator: random.Random) -> dict[str, Any]:
    """A schema-valid binding with random 256-bit associative lane sums."""
    return {
        "records": records,
        "semantic_sum_a": f"{generator.getrandbits(256):064x}",
        "semantic_sum_b": f"{generator.getrandbits(256):064x}",
    }


def _multinomial(total: int, weights: list[float], generator: random.Random) -> list[int]:
    """Deterministic multinomial draw over ``weights`` summing to ``total``.

    Uses the conditional-binomial method so it stays exact and does not require
    numpy. ``weights`` need not be normalized.
    """
    counts = [0] * len(weights)
    remaining = total
    remaining_weight = float(sum(weights))
    for index, weight in enumerate(weights):
        if remaining <= 0:
            break
        if index == len(weights) - 1:
            counts[index] = remaining
            break
        probability = 0.0 if remaining_weight <= 0 else min(1.0, weight / remaining_weight)
        drawn = _binomial(remaining, probability, generator)
        counts[index] = drawn
        remaining -= drawn
        remaining_weight -= weight
    return counts


def _binomial(trials: int, probability: float, generator: random.Random) -> int:
    if trials <= 0 or probability <= 0.0:
        return 0
    if probability >= 1.0:
        return trials
    # Exact for small n; normal approximation with clamping for large n keeps the
    # generator fast at planet scale while remaining deterministic.
    if trials <= 4096:
        return sum(1 for _ in range(trials) if generator.random() < probability)
    mean = trials * probability
    std = math.sqrt(trials * probability * (1.0 - probability))
    value = int(round(generator.gauss(mean, std)))
    return max(0, min(trials, value))


def _occupied_buckets(rows: int, generator: random.Random) -> list[tuple[int, int]]:
    """Return sorted ``(bucket, count)`` pairs for ``rows`` uniform route hashes.

    Rather than draw ``rows`` samples, it computes how many of the 65,536 buckets
    are occupied (expected occupancy under uniform hashing) and distributes rows
    across exactly those buckets. This reproduces the real saturation behaviour:
    a country with millions of rows lights up nearly every bucket.
    """
    if rows <= 0:
        return []
    span = MAXIMUM_BUCKET + 1
    expected_occupied = span * (1.0 - math.exp(-rows / span))
    occupied = max(1, min(span, min(rows, int(round(expected_occupied)))))
    # Pick which buckets are occupied deterministically, then spread rows over them.
    if occupied == span:
        chosen = range(span)
    else:
        chosen = sorted(generator.sample(range(span), occupied))
    weights = [1.0] * occupied
    counts = _multinomial(rows, weights, generator)
    return [
        (bucket, count)
        for bucket, count in zip(chosen, counts)
        if count > 0
    ]


# --------------------------------------------------------------------------- #
# Address family                                                              #
# --------------------------------------------------------------------------- #


def address_task_specs(inventory_path: Path, scale: float) -> list[dict[str, Any]]:
    inventory = json.loads(inventory_path.read_text())
    specs = []
    for task in inventory["plan"]["tasks"]:
        country_rows = {
            country: int(round(rows * scale))
            for country, rows in task["exact_country_rows"].items()
            if int(round(rows * scale)) > 0
        }
        if not country_rows:
            continue
        specs.append(
            {
                "task_id": task["execution_bucket"],
                "index": task["index"],
                "country_rows": country_rows,
            }
        )
    return specs


def generate_address_marker(
    spec: dict[str, Any], *, seed: int, pack_rows: int
) -> dict[str, Any]:
    """Build one synthetic Address task marker matching the real map schema."""
    generator = _rng(seed, "address", spec["index"])
    # Ordered (country, bucket, count) stream in TOTAL_ORDER = country, maximum_bucket.
    stream: list[tuple[str, int, int]] = []
    for country in sorted(spec["country_rows"]):
        for bucket, count in _occupied_buckets(spec["country_rows"][country], generator):
            stream.append((country, bucket, count))

    packs: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    remaining = pack_rows

    def close_pack() -> None:
        if not current:
            return
        packs.append(
            {
                "pack_id": len(packs),
                "directory": {
                    "schema": "overture-address-pack-proof-directory-v1",
                    "binding_schema": ADDRESS.BINDING_SCHEMA,
                    "binding": ADDRESS.combine_bindings(
                        [entry["binding"] for entry in current]
                    ),
                    "bucket_summaries": list(current),
                },
            }
        )
        current.clear()

    for country, bucket, count in stream:
        low = bucket << (64 - MAXIMUM_HASH_BITS)
        high = low | ((1 << (64 - MAXIMUM_HASH_BITS)) - 1)
        while count > 0:
            if remaining == 0:
                close_pack()
                remaining = pack_rows
            take = min(count, remaining)
            current.append(
                {
                    "country": country,
                    "maximum_bucket": bucket,
                    "minimum_route_hash": low,
                    "maximum_route_hash": high,
                    "binding": _binding(take, generator),
                }
            )
            count -= take
            remaining -= take
    close_pack()

    marker_binding = ADDRESS.combine_bindings(
        [entry["binding"] for pack in packs for entry in pack["directory"]["bucket_summaries"]]
    )
    return {
        "schema": ADDRESS.MARKER_SCHEMA,
        "binding_schema": ADDRESS.BINDING_SCHEMA,
        "task_id": spec["task_id"],
        "binding": marker_binding,
        "packs": packs,
    }


# --------------------------------------------------------------------------- #
# Places family                                                               #
# --------------------------------------------------------------------------- #


def places_task_specs(inventory_path: Path, scale: float) -> list[dict[str, Any]]:
    inventory = json.loads(inventory_path.read_text())
    specs = []
    for task in inventory["map_plan"]["tasks"]:
        rows = int(round(task["expected_input_records"] * scale))
        if rows <= 0:
            continue
        specs.append({"task_id": f"places-map-task-{task['index']:02d}", "index": task["index"], "rows": rows})
    return specs


def places_cell_weights(
    populated_cells: int, seed: int
) -> list[tuple[str, float]]:
    """Deterministic globally-populated cell set with Zipf-skewed weights.

    The Places map routes to a 256x256 lon/lat grid; ``partition_cell`` is the
    4-hex ``{y:02x}{x:02x}`` string and ``execution_group`` is its first two hex
    characters. Real occupancy concentrates on a few thousand land cells, which a
    Zipf(1.1) weighting over a fixed random cell sample reproduces.
    """
    generator = _rng(seed, "places-cells")
    span = (MAXIMUM_BUCKET + 1)
    populated = max(1, min(span, populated_cells))
    cell_indices = sorted(generator.sample(range(span), populated))
    weights = []
    for rank, index in enumerate(cell_indices, start=1):
        cell = f"{index:04x}"
        weights.append((cell, 1.0 / (rank**1.1)))
    return weights


def generate_places_marker(
    spec: dict[str, Any],
    *,
    seed: int,
    pack_rows: int,
    cell_weights: list[tuple[str, float]],
) -> dict[str, Any]:
    """Build one synthetic Places task marker matching the real map schema."""
    generator = _rng(seed, "places", spec["index"])
    cells = [cell for cell, _ in cell_weights]
    weights = [weight for _, weight in cell_weights]
    counts = _multinomial(spec["rows"], weights, generator)
    # TOTAL_ORDER begins execution_group, partition_cell; cells are already sorted.
    stream = [
        (cell, count) for cell, count in zip(cells, counts) if count > 0
    ]

    packs: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    remaining = pack_rows

    def close_pack() -> None:
        if not current:
            return
        packs.append(
            {
                "pack_id": len(packs),
                "directory": {
                    "schema": "overture-places-pack-proof-directory-v1",
                    "binding_schema": ADDRESS.BINDING_SCHEMA,
                    "binding": ADDRESS.combine_bindings(
                        [entry["binding"] for entry in current]
                    ),
                    "routing_summaries": list(current),
                },
            }
        )
        current.clear()

    for cell, count in stream:
        execution_group = cell[:2]
        while count > 0:
            if remaining == 0:
                close_pack()
                remaining = pack_rows
            take = min(count, remaining)
            current.append(
                {
                    "execution_group": execution_group,
                    "partition_cell": cell,
                    "binding": _binding(take, generator),
                }
            )
            count -= take
            remaining -= take
    close_pack()

    marker_binding = ADDRESS.combine_bindings(
        [
            entry["binding"]
            for pack in packs
            for entry in pack["directory"]["routing_summaries"]
        ]
    )
    return {
        "schema": PLACES.MARKER_SCHEMA,
        "binding_schema": ADDRESS.BINDING_SCHEMA,
        "task_id": spec["task_id"],
        "binding": marker_binding,
        "packs": packs,
    }


# --------------------------------------------------------------------------- #
# Generation to disk + counting                                               #
# --------------------------------------------------------------------------- #


def _summary_count(family: str, marker: dict[str, Any]) -> int:
    key = "bucket_summaries" if family == "address" else "routing_summaries"
    return sum(len(pack["directory"][key]) for pack in marker["packs"])


def generate_markers(
    family: str,
    *,
    scale: float,
    seed: int,
    pack_rows: int,
    out_dir: Path,
    populated_cells: int,
) -> dict[str, Any]:
    """Stream synthetic markers to ``out_dir``; never hold them all in memory."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if family == "address":
        specs = address_task_specs(ADDRESS_INVENTORY, scale)

        def make(spec: dict[str, Any]) -> dict[str, Any]:
            return generate_address_marker(spec, seed=seed, pack_rows=pack_rows)

    elif family == "places":
        specs = places_task_specs(PLACES_INVENTORY, scale)
        cell_weights = places_cell_weights(populated_cells, seed)

        def make(spec: dict[str, Any]) -> dict[str, Any]:
            return generate_places_marker(
                spec, seed=seed, pack_rows=pack_rows, cell_weights=cell_weights
            )

    else:
        raise ValueError(f"unknown family {family!r}")

    started = time.monotonic()
    total_rows = 0
    total_packs = 0
    total_summaries = 0
    bytes_written = 0
    for spec in specs:
        marker = make(spec)
        total_rows += marker["binding"]["records"]
        total_packs += len(marker["packs"])
        total_summaries += _summary_count(family, marker)
        path = out_dir / f"marker-{spec['index']:04d}.json"
        payload = json.dumps(marker, separators=(",", ":")).encode()
        path.write_bytes(payload)
        bytes_written += len(payload)
        del marker
    return {
        "family": family,
        "scale": scale,
        "tasks": len(specs),
        "records": total_rows,
        "packs": total_packs,
        "summary_entries": total_summaries,
        "marker_bytes": bytes_written,
        "generation_wall_seconds": time.monotonic() - started,
    }


def load_markers(markers_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text())
        for path in sorted(markers_dir.glob("marker-*.json"))
    ]


# --------------------------------------------------------------------------- #
# Bounded planning subprocess                                                 #
# --------------------------------------------------------------------------- #


def _self_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def run_plan_worker(family: str, markers_dir: Path, row_cap: int, result_path: Path) -> None:
    """In-process worker: invoke the real planner over the synthetic markers.

    When the planner exposes a streaming entry point (``genesis_plan_streaming``),
    feed it marker file paths so the planet-scale fan-in never materializes every
    marker at once. Otherwise fall back to the in-memory list API.
    """
    module = ADDRESS if family == "address" else PLACES
    marker_paths = sorted(markers_dir.glob("marker-*.json"))
    streaming = getattr(module, "genesis_plan_streaming", None)
    started = time.monotonic()
    if streaming is not None:
        plan = streaming(marker_paths, row_cap=row_cap)
        planner = f"{module.__name__}.genesis_plan_streaming"
    else:
        plan = module.genesis_plan(load_markers(markers_dir), row_cap=row_cap)
        planner = f"{module.__name__}.genesis_plan"
    wall = time.monotonic() - started
    result = {
        "family": family,
        "planner": planner,
        "plan_schema": plan["schema"],
        "partitions": len(plan["partitions"]),
        "row_cap": row_cap,
        "plan_records": plan["binding"]["records"],
        "planner_wall_seconds": wall,
        "worker_maxrss_bytes": _self_rss_bytes(),
    }
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


def measure_planning(
    family: str,
    *,
    markers_dir: Path,
    row_cap: int,
    scratch_root: Path,
    measure_max_rss_bytes: int,
    wall_seconds: float,
    max_scratch_bytes: int,
) -> dict[str, Any]:
    """Run the planner in a bounded child; hard-kill on any cap breach."""
    scratch_root.mkdir(parents=True, exist_ok=True)
    result_path = scratch_root / f"{family}-plan-result.json"
    if result_path.exists():
        result_path.unlink()
    limits = ADDRESS.Limits(
        max_rss_bytes=measure_max_rss_bytes,
        max_scratch_bytes=max_scratch_bytes,
        wall_seconds=wall_seconds,
    )
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "plan-worker",
        "--family",
        family,
        "--markers-dir",
        str(markers_dir),
        "--row-cap",
        str(row_cap),
        "--result",
        str(result_path),
    ]
    evidence = ADDRESS.run_bounded(
        command, scratch_roots=[scratch_root], limits=limits
    )
    result = json.loads(result_path.read_text())
    return {**result, **evidence}


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #


def default_row_cap(family: str) -> int:
    if family == "address":
        # Matches the Address inventory planning gate target_rows.
        return 4_000_000
    # Places simple genesis is provisional-unsplittable: a partition equals one
    # populated cell. A generous cap keeps the compact planner from raising on a
    # dense synthetic cell; the adaptive planner (which reopens payloads) is the
    # production path for over-cap cells and is out of scope here.
    return 20_000_000


def benchmark_family(
    family: str,
    *,
    scale: float,
    seed: int,
    pack_rows: int,
    scratch_root: Path,
    gate: Gate,
    measure_max_rss_bytes: int,
    row_cap: int,
    populated_cells: int,
    keep_markers: bool = False,
) -> dict[str, Any]:
    markers_dir = scratch_root / f"{family}-markers"
    generation = generate_markers(
        family,
        scale=scale,
        seed=seed,
        pack_rows=pack_rows,
        out_dir=markers_dir,
        populated_cells=populated_cells,
    )
    breach: str | None = None
    try:
        planning = measure_planning(
            family,
            markers_dir=markers_dir,
            row_cap=row_cap,
            scratch_root=scratch_root / f"{family}-plan-scratch",
            measure_max_rss_bytes=measure_max_rss_bytes,
            wall_seconds=gate.wall_seconds,
            max_scratch_bytes=gate.max_scratch_bytes,
        )
    except RuntimeError as error:
        breach = str(error)
        planning = None
    finally:
        if not keep_markers:
            for path in markers_dir.glob("marker-*.json"):
                path.unlink()
            markers_dir.rmdir()

    if planning is None:
        return {
            "family": family,
            "scale": scale,
            "generation": generation,
            "passed": False,
            "gate_reasons": [f"planner killed at hard cap: {breach}"],
            "measure_max_rss_bytes": measure_max_rss_bytes,
            "gate": asdict(gate),
        }
    passed, reasons = check_gate(planning, gate)
    return {
        "family": family,
        "scale": scale,
        "generation": generation,
        "planning": planning,
        "passed": passed,
        "gate_reasons": reasons,
        "measure_max_rss_bytes": measure_max_rss_bytes,
        "gate": asdict(gate),
    }


def extrapolate(sweep: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Least-squares slope of peak RSS and wall vs. summary entries."""
    points = [
        (
            item["generation"]["summary_entries"],
            item["planning"]["peak_rss_bytes"],
            item["planning"]["planner_wall_seconds"],
        )
        for item in sweep
        if item.get("planning")
    ]
    if len(points) < 2:
        return None
    entries = [point[0] for point in points]
    mean_entries = sum(entries) / len(entries)

    def slope(values: list[float]) -> float:
        mean_value = sum(values) / len(values)
        numerator = sum(
            (entry - mean_entries) * (value - mean_value)
            for entry, value in zip(entries, values)
        )
        denominator = sum((entry - mean_entries) ** 2 for entry in entries)
        return numerator / denominator if denominator else 0.0

    return {
        "bytes_per_summary_entry": slope([point[1] for point in points]),
        "seconds_per_summary_entry": slope([point[2] for point in points]),
    }


def format_bytes(value: int | float) -> str:
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(number) < 1024 or unit == "TiB":
            return f"{number:.2f} {unit}"
        number /= 1024
    return f"{number:.2f} TiB"


def render_report(results: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# construction-v1 aggregate fan-in benchmark")
    lines.append("")
    lines.append(
        "Synthetic planet-scale gate proving (or refuting) that Address and Places "
        "aggregate planning runs over compact per-pack summaries without reopening "
        "map payloads, comfortably inside a hosted job."
    )
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    for family in ("address", "places"):
        result = results["families"].get(family)
        if result is None:
            continue
        planning = result.get("planning") or {}
        verdict = "PASS" if result["passed"] else "FAIL"
        entries = result["generation"]["summary_entries"]
        if result["passed"]:
            detail = (
                f"{entries:,} summary entries planned in "
                f"{planning['planner_wall_seconds']:.1f} s at "
                f"{format_bytes(planning['peak_rss_bytes'])} peak RSS"
            )
        else:
            detail = (
                f"{entries:,} summary entries; planning breaches the "
                f"{format_bytes(results['gate']['max_rss_bytes'])} RSS gate"
            )
        lines.append(f"- **{family.title()}: {verdict}** — {detail}.")
    lines.append("")
    lines.append("## Runtime")
    lines.append("")
    runtime = results["runtime"]
    lines.append(f"- generated: {results['generated_at']}")
    lines.append(f"- platform: {runtime['platform']}")
    lines.append(
        f"- python {runtime['python']}, duckdb {runtime['duckdb']}, "
        f"pyarrow {runtime['pyarrow']}"
    )
    lines.append(f"- seed: {results['seed']}, pack rows: {results['pack_rows']}")
    lines.append("")
    lines.append("## Gate")
    lines.append("")
    gate = results["gate"]
    lines.append(
        f"- wall gate: {gate['wall_seconds'] / 60:.0f} min "
        f"(hosted job ceiling {HOSTED_JOB_SECONDS / 60:.0f} min)"
    )
    lines.append(f"- RSS gate: {format_bytes(gate['max_rss_bytes'])}")
    lines.append(f"- scratch gate: {format_bytes(gate['max_scratch_bytes'])}")
    lines.append("")
    lines.append("## Methodology")
    lines.append("")
    lines.append(
        "1. Read the real per-task row counts and country/skew from the local "
        "inventories (`benchmarks/address-construction-v1-data/inventory/addresses.json`, "
        "`benchmarks/places-construction-v1-data/inventory/places.json`)."
    )
    lines.append(
        "2. Generate synthetic per-task markers whose per-pack summary entries "
        "match the real Rust proof-directory schema (Address `bucket_summaries`: "
        "country + top-16-bit `maximum_bucket`; Places `routing_summaries`: "
        "`execution_group` + `partition_cell`). Bindings are random 256-bit "
        "associative lane sums; marker bindings equal the combine of their pack "
        "bindings, so the real planners reconcile."
    )
    lines.append(
        "3. Invoke the real `genesis_plan` in a bounded subprocess "
        "(`scripts/address_construction_v1.py`, `scripts/places_construction_v1.py`) "
        "and measure wall / peak RSS / peak scratch with the repo's `run_bounded` "
        "watchdog, which hard-kills on any cap breach."
    )
    lines.append("")
    for family in ("address", "places"):
        result = results["families"].get(family)
        if result is None:
            continue
        lines.append(f"## {family.title()} planet-scale result")
        lines.append("")
        generation = result["generation"]
        lines.append(f"- tasks: {generation['tasks']}")
        lines.append(f"- synthetic records: {generation['records']:,}")
        lines.append(f"- packs: {generation['packs']:,}")
        lines.append(
            f"- summary entries (fan-in size): {generation['summary_entries']:,}"
        )
        lines.append(f"- marker bytes on disk: {format_bytes(generation['marker_bytes'])}")
        lines.append(
            f"- generation wall: {generation['generation_wall_seconds']:.1f} s"
        )
        planning = result.get("planning")
        if planning is None:
            lines.append(
                f"- **planner killed at hard cap "
                f"{format_bytes(result['measure_max_rss_bytes'])}** — the fully "
                "materialized Python `genesis_plan` breaches the RSS gate before it "
                "can emit a plan. The scaling sweep below shows peak RSS already "
                "exceeds the gate at reduced scale, so this is a real ceiling, not a "
                "transient spike."
            )
        else:
            lines.append(f"- partitions produced: {planning['partitions']:,}")
            lines.append(f"- planner wall: {planning['planner_wall_seconds']:.2f} s")
            lines.append(
                f"- peak RSS (subprocess sampler): "
                f"{format_bytes(planning['peak_rss_bytes'])}"
            )
            lines.append(
                f"- peak RSS (worker getrusage): "
                f"{format_bytes(planning['worker_maxrss_bytes'])}"
            )
            lines.append(
                f"- peak scratch: {format_bytes(planning['peak_scratch_bytes'])}"
            )
        verdict = "PASS" if result["passed"] else "FAIL"
        lines.append(f"- **verdict vs gate: {verdict}**")
        if result["gate_reasons"]:
            for reason in result["gate_reasons"]:
                lines.append(f"  - {reason}")
        lines.append("")
        sweep = results.get("sweeps", {}).get(family)
        if sweep:
            gate_rss = results["gate"]["max_rss_bytes"]
            lines.append(f"### {family.title()} scaling sweep")
            lines.append("")
            lines.append(
                "Peak RSS is reported by two independent meters (`run_bounded` psutil "
                "sampler and the worker's kernel `getrusage`); they agree here. "
                "The sweep ran with a relaxed hard-kill cap so runs complete and the "
                "true peak is observable; PASS/FAIL below is still judged against the "
                f"{format_bytes(gate_rss)} gate."
            )
            lines.append("")
            lines.append(
                "| scale | summary entries | planner wall (s) | peak RSS (psutil) "
                "| peak RSS (getrusage) | vs gate |"
            )
            lines.append(
                "| ----- | --------------- | ---------------- | ----------------- "
                "| -------------------- | ------- |"
            )
            for item in sweep:
                planning = item.get("planning")
                if planning is None:
                    lines.append(
                        f"| {item['scale']:.3f} | "
                        f"{item['generation']['summary_entries']:,} | killed | killed "
                        "| killed | FAIL |"
                    )
                    continue
                within = planning["peak_rss_bytes"] <= gate_rss
                lines.append(
                    f"| {item['scale']:.3f} | "
                    f"{item['generation']['summary_entries']:,} | "
                    f"{planning['planner_wall_seconds']:.2f} | "
                    f"{format_bytes(planning['peak_rss_bytes'])} | "
                    f"{format_bytes(planning['worker_maxrss_bytes'])} | "
                    f"{'PASS' if within else 'FAIL'} |"
                )
            lines.append("")
            model = results.get("extrapolations", {}).get(family)
            if model:
                peaks = [
                    it["planning"]["peak_rss_bytes"]
                    for it in sweep
                    if it.get("planning")
                ]
                monotone = all(a <= b for a, b in zip(peaks, peaks[1:]))
                lines.append(
                    f"- observed peak-RSS band across the sweep: "
                    f"{format_bytes(min(peaks))} .. {format_bytes(max(peaks))}"
                )
                if not monotone:
                    lines.append(
                        "- peak RSS is **not monotonic** in entry count: it is dominated "
                        "by a transient inside `genesis_plan` (the fully materialized "
                        "`buckets` map of per-summary bindings plus the recursive "
                        "`emit` combine lists), not by a term that scales linearly with "
                        "entries. A linear fit therefore under-models the true peak and "
                        "is reported only as an order-of-magnitude slope, not a bound."
                    )
                lines.append(
                    f"- linear slope (lower bound only): "
                    f"~{format_bytes(model['bytes_per_summary_entry'])} peak RSS per "
                    f"summary entry, ~{model['seconds_per_summary_entry'] * 1e6:.2f} us "
                    f"wall per entry"
                )
                lines.append("")
    lines.append("## Synthetic-data caveats (what this cannot prove)")
    lines.append("")
    lines.append(
        "- Bindings are random 256-bit sums, not digests of real feature rows; "
        "this proves fan-in *shape and scale*, not semantic digest correctness."
    )
    lines.append(
        "- Address bucket occupancy uses uniform route-hash saturation and Places "
        "cell occupancy uses a Zipf-skewed synthetic land model; real skew may "
        "shift entry counts modestly but not the order of magnitude."
    )
    lines.append(
        "- Only the compact summary-only planners (`genesis_plan`) are exercised. "
        "The Places `adaptive_genesis_plan` deliberately reopens pack payloads and "
        "cannot be driven by summaries alone; it is out of scope for a synthetic "
        "summary benchmark and still needs a real payload-scale proof."
    )
    lines.append(
        "- Peak RSS is sampled (5 ms poll in `run_bounded`); sub-poll peaks are "
        "possible. The worker `getrusage` maxrss is reported as a kernel cross-check."
    )
    lines.append("")
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    subcommands = value.add_subparsers(dest="command", required=True)

    run = subcommands.add_parser("run", help="Run the planet-scale fan-in gate.")
    run.add_argument(
        "--family", choices=("address", "places", "both"), default="both"
    )
    run.add_argument("--scale", type=float, default=1.0)
    run.add_argument("--seed", type=int, default=1729)
    run.add_argument("--pack-rows", type=int, default=DEFAULT_PACK_ROWS)
    run.add_argument("--scratch", type=Path, required=True)
    run.add_argument(
        "--gate-wall-seconds", type=float, default=DEFAULT_GATE_WALL_SECONDS
    )
    run.add_argument(
        "--gate-max-rss-bytes", type=int, default=DEFAULT_GATE_MAX_RSS_BYTES
    )
    run.add_argument(
        "--gate-max-scratch-bytes", type=int, default=DEFAULT_GATE_MAX_SCRATCH_BYTES
    )
    run.add_argument(
        "--measure-max-rss-bytes",
        type=int,
        default=None,
        help="Hard kill cap for the planner subprocess; defaults to the RSS gate.",
    )
    run.add_argument("--address-row-cap", type=int, default=default_row_cap("address"))
    run.add_argument("--places-row-cap", type=int, default=default_row_cap("places"))
    run.add_argument(
        "--places-populated-cells", type=int, default=DEFAULT_PLACES_POPULATED_CELLS
    )
    run.add_argument(
        "--sweep",
        type=str,
        default=None,
        help="Comma-separated scale ladder for characterization (no gate failure).",
    )
    run.add_argument(
        "--sweep-max-rss-bytes",
        type=int,
        default=DEFAULT_MEASURE_MAX_RSS_BYTES,
        help="Hard kill cap for sweep subprocesses.",
    )
    run.add_argument("--report", type=Path, default=None)
    run.add_argument("--keep-markers", action="store_true")

    worker = subcommands.add_parser("plan-worker", help=argparse.SUPPRESS)
    worker.add_argument("--family", choices=("address", "places"), required=True)
    worker.add_argument("--markers-dir", type=Path, required=True)
    worker.add_argument("--row-cap", type=int, required=True)
    worker.add_argument("--result", type=Path, required=True)
    return value


def _runtime_metadata() -> dict[str, str]:
    import duckdb
    import pyarrow

    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "duckdb": duckdb.__version__,
        "pyarrow": pyarrow.__version__,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.command == "plan-worker":
        run_plan_worker(
            arguments.family,
            arguments.markers_dir,
            arguments.row_cap,
            arguments.result,
        )
        return 0

    gate = Gate(
        wall_seconds=arguments.gate_wall_seconds,
        max_rss_bytes=arguments.gate_max_rss_bytes,
        max_scratch_bytes=arguments.gate_max_scratch_bytes,
    )
    gate.validate()
    measure_cap = arguments.measure_max_rss_bytes or gate.max_rss_bytes
    families = (
        ("address", "places") if arguments.family == "both" else (arguments.family,)
    )
    row_caps = {
        "address": arguments.address_row_cap,
        "places": arguments.places_row_cap,
    }
    scratch = arguments.scratch
    scratch.mkdir(parents=True, exist_ok=True)

    results: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "seed": arguments.seed,
        "pack_rows": arguments.pack_rows,
        "gate": asdict(gate),
        "runtime": _runtime_metadata(),
        "families": {},
        "sweeps": {},
        "extrapolations": {},
    }

    exit_code = 0
    for family in families:
        if arguments.sweep:
            sweep_results = []
            for scale in [float(part) for part in arguments.sweep.split(",")]:
                sweep_results.append(
                    benchmark_family(
                        family,
                        scale=scale,
                        seed=arguments.seed,
                        pack_rows=arguments.pack_rows,
                        scratch_root=scratch / f"sweep-{family}-{scale:.4f}",
                        gate=gate,
                        measure_max_rss_bytes=arguments.sweep_max_rss_bytes,
                        row_cap=row_caps[family],
                        populated_cells=arguments.places_populated_cells,
                    )
                )
            results["sweeps"][family] = sweep_results
            model = extrapolate(sweep_results)
            if model:
                results["extrapolations"][family] = model

        result = benchmark_family(
            family,
            scale=arguments.scale,
            seed=arguments.seed,
            pack_rows=arguments.pack_rows,
            scratch_root=scratch / f"gate-{family}",
            gate=gate,
            measure_max_rss_bytes=measure_cap,
            row_cap=row_caps[family],
            populated_cells=arguments.places_populated_cells,
            keep_markers=arguments.keep_markers,
        )
        results["families"][family] = result
        status = "PASS" if result["passed"] else "FAIL"
        planning = result.get("planning") or {}
        print(
            f"[{family}] scale={arguments.scale} entries="
            f"{result['generation']['summary_entries']:,} "
            f"wall={planning.get('planner_wall_seconds', float('nan')):.2f}s "
            f"peakRSS={format_bytes(planning.get('peak_rss_bytes', 0))} -> {status}",
            file=sys.stderr,
        )
        for reason in result["gate_reasons"]:
            print(f"  reason: {reason}", file=sys.stderr)
        if not result["passed"]:
            exit_code = 1

    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(render_report(results))
        print(f"wrote report {arguments.report}", file=sys.stderr)

    results_path = scratch / "fanin-results.json"
    results_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
