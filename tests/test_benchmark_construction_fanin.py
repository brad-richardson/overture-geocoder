"""Fast invariants for the construction-v1 aggregate fan-in benchmark.

These tests never run at planet scale. They validate that the synthetic
generator emits summary entries matching the real Rust proof-directory schema,
that the real planners consume that synthetic input and reconcile, and that the
acceptance gate fails closed.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "benchmark_construction_fanin", ROOT / "scripts/benchmark_construction_fanin.py"
)
assert _SPEC and _SPEC.loader
FANIN = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = FANIN
_SPEC.loader.exec_module(FANIN)


def _address_spec(index: int, country_rows: dict[str, int]) -> dict:
    return {
        "task_id": f"address-map-task-{index:03d}",
        "index": index,
        "country_rows": country_rows,
    }


def _places_spec(index: int, rows: int) -> dict:
    return {"task_id": f"places-map-task-{index:02d}", "index": index, "rows": rows}


# --------------------------------------------------------------------------- #
# Schema fidelity                                                             #
# --------------------------------------------------------------------------- #


def test_address_summary_entries_match_real_schema():
    marker = FANIN.generate_address_marker(
        _address_spec(0, {"us": 3_000, "ca": 1_200}), seed=7, pack_rows=500
    )
    assert marker["schema"] == FANIN.ADDRESS.MARKER_SCHEMA
    assert marker["packs"], "expected at least one pack"
    for pack in marker["packs"]:
        directory = pack["directory"]
        assert directory["schema"] == "overture-address-pack-proof-directory-v1"
        for entry in directory["bucket_summaries"]:
            assert set(entry) == FANIN.ADDRESS_BUCKET_SUMMARY_FIELDS
            assert 0 <= entry["maximum_bucket"] <= FANIN.MAXIMUM_BUCKET
            assert entry["minimum_route_hash"] <= entry["maximum_route_hash"]
            assert set(entry["binding"]) == FANIN.BINDING_FIELDS
            for lane in ("semantic_sum_a", "semantic_sum_b"):
                assert len(entry["binding"][lane]) == 64
                int(entry["binding"][lane], 16)  # canonical hex


def test_places_summary_entries_match_real_schema():
    weights = FANIN.places_cell_weights(populated_cells=40, seed=3)
    marker = FANIN.generate_places_marker(
        _places_spec(0, 6_000), seed=7, pack_rows=500, cell_weights=weights
    )
    assert marker["schema"] == FANIN.PLACES.MARKER_SCHEMA
    assert marker["packs"], "expected at least one pack"
    for pack in marker["packs"]:
        directory = pack["directory"]
        assert directory["schema"] == "overture-places-pack-proof-directory-v1"
        for entry in directory["routing_summaries"]:
            assert set(entry) == FANIN.PLACES_ROUTING_SUMMARY_FIELDS
            assert entry["execution_group"] == entry["partition_cell"][:2]
            assert set(entry["binding"]) == FANIN.BINDING_FIELDS


def test_pack_row_cap_is_respected():
    marker = FANIN.generate_address_marker(
        _address_spec(0, {"us": 4_000}), seed=1, pack_rows=250
    )
    for pack in marker["packs"]:
        rows = sum(
            entry["binding"]["records"]
            for entry in pack["directory"]["bucket_summaries"]
        )
        assert rows <= 250


# --------------------------------------------------------------------------- #
# Real planners consume the synthetic markers and reconcile                   #
# --------------------------------------------------------------------------- #


def test_address_genesis_plan_reconciles_synthetic_markers():
    markers = [
        FANIN.generate_address_marker(
            _address_spec(index, {"us": 2_500, "mx": 900}), seed=index, pack_rows=400
        )
        for index in range(4)
    ]
    plan = FANIN.ADDRESS.genesis_plan(markers, row_cap=1_000)
    assert plan["schema"] == FANIN.ADDRESS.PLAN_SCHEMA
    assert plan["partitions"]
    expected = FANIN.ADDRESS.combine_bindings([m["binding"] for m in markers])
    assert plan["binding"] == expected  # genesis_plan raises otherwise; assert too
    assert plan["binding"]["records"] == sum(m["binding"]["records"] for m in markers)


def test_places_genesis_plan_consumes_synthetic_markers():
    weights = FANIN.places_cell_weights(populated_cells=30, seed=5)
    markers = [
        FANIN.generate_places_marker(
            _places_spec(index, 5_000), seed=index, pack_rows=400, cell_weights=weights
        )
        for index in range(3)
    ]
    plan = FANIN.PLACES.genesis_plan(markers, row_cap=10_000_000)
    assert plan["schema"] == FANIN.PLACES.PLAN_SCHEMA
    assert plan["partitions"]
    # One partition per distinct populated cell.
    cells = {
        entry["partition_cell"]
        for marker in markers
        for pack in marker["packs"]
        for entry in pack["directory"]["routing_summaries"]
    }
    assert len(plan["partitions"]) == len(cells)


def test_marker_binding_equals_combined_pack_bindings():
    marker = FANIN.generate_address_marker(
        _address_spec(2, {"br": 3_100}), seed=11, pack_rows=700
    )
    combined = FANIN.ADDRESS.combine_bindings(
        [pack["directory"]["binding"] for pack in marker["packs"]]
    )
    assert combined == marker["binding"]


# --------------------------------------------------------------------------- #
# Streaming genesis planner equivalence                                       #
# --------------------------------------------------------------------------- #


def _reference_genesis_plan(markers, *, row_cap):
    """Verbatim pre-remediation Address ``genesis_plan``.

    Frozen here as the equivalence oracle: it materializes every per-pack binding
    (the ``buckets`` map) exactly as the shipped implementation did before the
    streaming/aggregate refactor. The new in-memory and streaming planners must
    reproduce its output byte for byte.
    """
    ADDRESS = FANIN.ADDRESS
    if row_cap <= 0:
        raise ValueError("partition row cap must be positive")
    buckets: dict = {}
    for marker in markers:
        for pack in marker["packs"]:
            for item in pack["directory"]["bucket_summaries"]:
                buckets.setdefault(
                    (item["country"], item["maximum_bucket"]), []
                ).append(item["binding"])
    summaries = {
        identity: ADDRESS.combine_bindings(bindings)
        for identity, bindings in buckets.items()
    }
    partitions = []

    def emit(country, prefix, bits, entries):
        binding = ADDRESS.combine_bindings([item[1] for item in entries])
        if binding["records"] <= row_cap:
            remaining = 64 - bits
            start = prefix << remaining if bits else 0
            end = start + (1 << remaining) - 1
            partitions.append(
                {
                    "id": f"a-{country}"
                    if bits == 0
                    else f"a-{country}-h-{prefix:0{bits}b}",
                    "country": country,
                    "hash_bits": bits,
                    "hash_prefix": f"{prefix:0{bits}b}" if bits else "",
                    "hash_start": start,
                    "hash_end": end,
                    "binding": binding,
                }
            )
            return
        if bits == 16:
            raise ValueError(
                "Address genesis partition exceeds cap at construction ceiling"
            )
        for bit in (0, 1):
            child_prefix = (prefix << 1) | bit
            shift = 16 - (bits + 1)
            child = [item for item in entries if item[0] >> shift == child_prefix]
            if child:
                emit(country, child_prefix, bits + 1, child)

    countries = sorted({country for country, _ in summaries})
    for country in countries:
        emit(
            country,
            0,
            0,
            sorted(
                (bucket, binding)
                for (name, bucket), binding in summaries.items()
                if name == country
            ),
        )
    total = ADDRESS.combine_bindings([p["binding"] for p in partitions])
    expected = ADDRESS.combine_bindings([m["binding"] for m in markers])
    if total != expected:
        raise ValueError(
            "genesis partition bindings do not cover map output exactly"
        )
    return {
        "schema": ADDRESS.PLAN_SCHEMA,
        "maximum_hash_bits": 16,
        "row_cap": row_cap,
        "partitions": partitions,
        "binding": total,
    }


def _equivalence_markers():
    """Nontrivial synthetic markers: multiple tasks, countries, and bisection."""
    return [
        FANIN.generate_address_marker(
            _address_spec(
                index,
                {"us": 9_000, "br": 6_500, "mx": 2_200, "ca": 700, "de": 40},
            ),
            seed=index,
            pack_rows=500,
        )
        for index in range(6)
    ]


@pytest.fixture(scope="module")
def _generated_equivalence_markers():
    """Generate the synthetic equivalence markers exactly once per module.

    Generation is deterministic (fixed per-index seeds) and costs ~4.6 s, but
    every consumer used to re-run it: five `row_cap` parametrisations plus
    `test_genesis_plan_direct_invariants`, i.e. ~28 s of byte-identical work.
    """
    return _equivalence_markers()


@pytest.fixture
def equivalence_markers(_generated_equivalence_markers):
    """Hand each test its own deep copy of the shared markers.

    The copy costs ~0.26 s against ~4.6 s to regenerate. It is deliberately not
    skipped: the markers are plain nested dicts, so copying preserves exactly
    today's semantics -- every test still mutates only its own object graph and
    no test can observe another's writes -- without anyone having to prove that
    the reference planner, the real planner and the streaming planner all treat
    their input as read-only.
    """
    return copy.deepcopy(_generated_equivalence_markers)


@pytest.mark.slow  # 2-7s per case: plans the same markers at five row caps.
@pytest.mark.parametrize("row_cap", [400, 1_000, 4_000, 25_000, 250_000])
def test_streaming_genesis_plan_is_byte_identical(tmp_path, row_cap, equivalence_markers):
    ADDRESS = FANIN.ADDRESS
    markers = equivalence_markers

    reference = _reference_genesis_plan(markers, row_cap=row_cap)
    in_memory = ADDRESS.genesis_plan(markers, row_cap=row_cap)

    # At tight caps bisection must actually fire so the recursive path is
    # exercised; at generous caps whole countries fit in one partition.
    if row_cap <= 4_000:
        assert any(p["hash_bits"] > 0 for p in reference["partitions"])
    assert ADDRESS.canonical_json(in_memory) == ADDRESS.canonical_json(reference)

    paths = []
    for index, marker in enumerate(markers):
        path = tmp_path / f"marker-{index:04d}.json"
        path.write_text(json.dumps(marker, separators=(",", ":")))
        paths.append(path)
    streamed = ADDRESS.genesis_plan_streaming(paths, row_cap=row_cap)
    assert ADDRESS.canonical_json(streamed) == ADDRESS.canonical_json(reference)


def test_streaming_genesis_plan_matches_on_empty_and_single():
    ADDRESS = FANIN.ADDRESS
    assert ADDRESS.canonical_json(
        ADDRESS.genesis_plan([], row_cap=1_000)
    ) == ADDRESS.canonical_json(_reference_genesis_plan([], row_cap=1_000))
    single = [
        FANIN.generate_address_marker(
            _address_spec(0, {"fr": 3_000}), seed=42, pack_rows=700
        )
    ]
    assert ADDRESS.canonical_json(
        ADDRESS.genesis_plan(single, row_cap=800)
    ) == ADDRESS.canonical_json(_reference_genesis_plan(single, row_cap=800))


@pytest.mark.slow  # ~4s: full plan invariants over the large marker set.
def test_genesis_plan_direct_invariants(equivalence_markers):
    """Prove the plan properties directly, not only via the reference oracle.

    Deterministic bytes, exact binding reconciliation (count + both digest lanes),
    every partition under row_cap, and per-country hash ranges that tile the full
    64-bit space exactly once (no gap, no overlap).
    """
    ADDRESS = FANIN.ADDRESS
    row_cap = 1_000
    markers = equivalence_markers

    first = ADDRESS.genesis_plan(markers, row_cap=row_cap)
    second = ADDRESS.genesis_plan(markers, row_cap=row_cap)
    assert ADDRESS.canonical_json(first) == ADDRESS.canonical_json(second)

    # Exact reconciliation: combined partition bindings equal combined markers.
    expected = ADDRESS.combine_bindings([m["binding"] for m in markers])
    combined = ADDRESS.combine_bindings([p["binding"] for p in first["partitions"]])
    assert combined == expected == first["binding"]
    assert combined["records"] == sum(m["binding"]["records"] for m in markers)

    span = 1 << 64
    by_country: dict[str, list] = {}
    for partition in first["partitions"]:
        assert partition["binding"]["records"] <= row_cap
        assert partition["hash_start"] <= partition["hash_end"]
        by_country.setdefault(partition["country"], []).append(partition)

    for country, parts in by_country.items():
        ranges = sorted((p["hash_start"], p["hash_end"]) for p in parts)
        cursor = 0
        for start, end in ranges:
            assert start == cursor, f"{country} has a gap/overlap at {cursor}"
            cursor = end + 1
        assert cursor == span, f"{country} does not tile the full hash space"


def test_streaming_and_in_memory_reject_nonpositive_row_cap(tmp_path):
    ADDRESS = FANIN.ADDRESS
    with pytest.raises(ValueError, match="row cap must be positive"):
        ADDRESS.genesis_plan([], row_cap=0)
    with pytest.raises(ValueError, match="row cap must be positive"):
        ADDRESS.genesis_plan_streaming([], row_cap=0)


# --------------------------------------------------------------------------- #
# Determinism                                                                 #
# --------------------------------------------------------------------------- #


def test_generation_is_deterministic():
    first = FANIN.generate_address_marker(
        _address_spec(0, {"us": 5_000}), seed=99, pack_rows=300
    )
    second = FANIN.generate_address_marker(
        _address_spec(0, {"us": 5_000}), seed=99, pack_rows=300
    )
    assert first == second


# --------------------------------------------------------------------------- #
# Gate logic fails closed                                                     #
# --------------------------------------------------------------------------- #


def test_check_gate_passes_within_bounds():
    gate = FANIN.Gate(wall_seconds=60, max_rss_bytes=10**9, max_scratch_bytes=10**9)
    evidence = {"wall_seconds": 1.0, "peak_rss_bytes": 10**6, "peak_scratch_bytes": 0}
    passed, reasons = FANIN.check_gate(evidence, gate)
    assert passed and reasons == []


def test_check_gate_fails_on_wall_and_rss():
    gate = FANIN.Gate(wall_seconds=1, max_rss_bytes=10**6, max_scratch_bytes=10**6)
    evidence = {
        "wall_seconds": 5.0,
        "peak_rss_bytes": 10**9,
        "peak_scratch_bytes": 10**9,
    }
    passed, reasons = FANIN.check_gate(evidence, gate)
    assert not passed
    assert len(reasons) == 3


def test_gate_rejects_wall_above_hosted_ceiling():
    gate = FANIN.Gate(wall_seconds=FANIN.HOSTED_JOB_SECONDS + 1)
    with pytest.raises(ValueError):
        gate.validate()


def test_benchmark_family_fails_closed_on_tiny_rss_cap(tmp_path):
    """A hard kill at an unreachable RSS cap must surface as a failed gate."""
    gate = FANIN.Gate(wall_seconds=120, max_rss_bytes=1024, max_scratch_bytes=10**9)
    result = FANIN.benchmark_family(
        "places",
        scale=0.0005,
        seed=1,
        pack_rows=5_000,
        scratch_root=tmp_path,
        gate=gate,
        measure_max_rss_bytes=1024,  # 1 KiB: unreachable, forces a kill
        row_cap=FANIN.default_row_cap("places"),
        populated_cells=200,
    )
    assert result["passed"] is False
    assert result["gate_reasons"]


@pytest.mark.slow  # ~8s: runs the whole benchmark family plus its gate.
def test_benchmark_family_passes_small_scale(tmp_path):
    gate = FANIN.Gate(
        wall_seconds=120, max_rss_bytes=2 * 1024**3, max_scratch_bytes=4 * 1024**3
    )
    result = FANIN.benchmark_family(
        "address",
        scale=0.0005,
        seed=1,
        pack_rows=5_000,
        scratch_root=tmp_path,
        gate=gate,
        measure_max_rss_bytes=2 * 1024**3,
        row_cap=FANIN.default_row_cap("address"),
        populated_cells=200,
    )
    assert result["passed"] is True
    assert result["planning"]["partitions"] > 0
