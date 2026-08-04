"""Tests for the paged KV/R2 Places index experiment."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "experiment_places_kv_r2_pages.py"
spec = importlib.util.spec_from_file_location("experiment_places_kv_r2_pages", SCRIPT)
experiment = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = experiment
spec.loader.exec_module(experiment)


def fixture_places():
    compact = sys.modules["experiment_places_compact_index"]
    rows = [
        {
            "id": "a",
            "name": "Starbucks",
            "brand": "Starbucks",
            "category": "coffee shop",
            "city": "San Francisco",
            "region": "CA",
            "country": "US",
            "confidence": 0.9,
        },
        {
            "id": "b",
            "name": "Golden Gate Hotel",
            "category": "hotel",
            "city": "San Francisco",
            "region": "CA",
            "country": "US",
            "confidence": 0.8,
        },
        {
            "id": "c",
            "name": "Golden Cafe",
            "category": "cafe",
            "city": "Oakland",
            "region": "CA",
            "country": "US",
            "confidence": 0.7,
        },
    ]
    return [compact.place_from_row(row, number) for number, row in enumerate(rows, 1)]


def test_direct_keys_are_release_scoped_and_deterministic():
    one = experiment.derived_key("r1", "post-exact", "starbucks", 0)
    assert one == experiment.derived_key("r1", "post-exact", "starbucks", 0)
    assert one != experiment.derived_key("r2", "post-exact", "starbucks", 0)
    assert "starbucks" not in one


def test_all_page_targets_are_hard_caps_and_recall_is_complete():
    for target in (16 * 1024, 64 * 1024, 256 * 1024):
        index = experiment.PageIndex(fixture_places(), "r1", target, target, target)
        assert index.inventory()["max_object_bytes"] <= target
        result = index.query(
            experiment.QueryCase(
                "gold", (experiment.Clause("gol", prefix=True),), "typical"
            )
        )
        assert result["complete_fixture_recall"] is True
        assert result["candidate_count"] == 2


def test_field_mask_filters_category_without_losing_candidates():
    index = experiment.PageIndex(fixture_places(), "r1", 16_384, 65_536, 16_384)
    case = experiment.QueryCase(
        "hotel", (experiment.Clause("hotel", field="category"),), "typical"
    )
    result = index.query(case)
    assert result["candidate_count"] == 1
    assert result["complete_fixture_recall"] is True


def test_synthetic_overflow_is_fully_traversed_and_exposes_failed_small_pages():
    small = experiment.synthetic_overflow(16 * 1024, 64 * 1024, count=500_000)
    hybrid = experiment.synthetic_overflow(256 * 1024, 64 * 1024, count=500_000)
    assert small["decoded_posting_count"] == 500_000
    assert hybrid["decoded_posting_count"] == 500_000
    assert small["worst_gate_pass"] is False
    assert hybrid["worst_gate_pass"] is True
    assert hybrid["posting_pages"] < small["posting_pages"]
    scattered = experiment.synthetic_overflow(
        256 * 1024, 256 * 1024, count=500_000, scattered_results=True
    )
    assert scattered["full_traversal"] is True
    assert scattered["worst_gate_pass"] is False


def test_cost_model_applies_included_usage_and_hard_ceiling():
    low = experiment.monthly_cost(1_000_000, 3, 0.01, 120)
    high = experiment.monthly_cost(50_000_000, 3, 0.01, 320)
    assert low["workers_usd"] == 5
    assert low["under_30_usd"] is True
    assert high["under_30_usd"] is False
    assert "CPU" in high["excluded"]
    publication = experiment.monthly_cost(
        1_000_000, 2, 0, 120, class_a_writes=2_000_000
    )
    assert publication["r2_class_a_usd"] == 4.5


def test_configuration_selection_reports_when_every_layout_fails():
    def candidate(typical, worst, overflow, operations, size):
        return {
            "warm_gates": {
                "typical_pass": typical,
                "worst_pass": worst,
                "typical_max_operations": operations,
                "worst_max_operations": operations,
            },
            "synthetic_high_fanout_warm": {
                "worst_gate_pass": overflow,
                "total_operations": operations,
            },
            "inventory": {"published_bytes": size},
        }

    selected, passed = experiment.select_configuration(
        {
            "less_bad": candidate(False, True, True, 4, 200),
            "more_bad": candidate(False, False, True, 5, 100),
        }
    )
    assert selected == "less_bad"
    assert passed is False


@pytest.mark.slow  # ~6s: drives the experiment CLI across every configuration.
def test_cli_writes_reports(tmp_path):
    source = tmp_path / "places.jsonl"
    source.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"id": "a", "name": "Starbucks", "category": "coffee shop"},
                {"id": "b", "name": "Hotel", "category": "hotel"},
            ]
        )
    )
    json_out = tmp_path / "report.json"
    md_out = tmp_path / "report.md"
    assert (
        experiment.main(
            [str(source), "--json-out", str(json_out), "--markdown-out", str(md_out)]
        )
        == 0
    )
    report = json.loads(json_out.read_text())
    assert set(report["configurations"]) == {
        "uniform_16k",
        "uniform_64k",
        "uniform_256k",
        "hybrid_16k_256k_64k",
    }
    assert report["selected_configuration"] in report["configurations"]
    assert report["selection_gate_passed"] is True
    assert report["cost_model"]["publication_object_sensitivity"]
    assert (
        report["configurations"][report["selected_configuration"]][
            "synthetic_scattered_results_warm"
        ]["worst_gate_pass"]
        is False
    )
    assert "documented prices" in md_out.read_text()


def test_cli_can_build_one_configuration(tmp_path):
    source = tmp_path / "places.jsonl"
    source.write_text(json.dumps({"id": "a", "name": "Starbucks"}) + "\n")
    json_out = tmp_path / "report.json"
    md_out = tmp_path / "report.md"
    assert (
        experiment.main(
            [
                str(source),
                "--json-out",
                str(json_out),
                "--markdown-out",
                str(md_out),
                "--configuration",
                "uniform_256k",
            ]
        )
        == 0
    )
    report = json.loads(json_out.read_text())
    assert list(report["configurations"]) == ["uniform_256k"]
