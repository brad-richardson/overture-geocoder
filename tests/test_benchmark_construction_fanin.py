"""Fast invariants for the construction-v1 aggregate fan-in benchmark.

These tests never run at planet scale. They validate that the synthetic
generator emits summary entries matching the real Rust proof-directory schema,
that the real planners consume that synthetic input and reconcile, and that the
acceptance gate fails closed.
"""

from __future__ import annotations

import importlib.util
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
