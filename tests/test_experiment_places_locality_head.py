"""Tests for cell-clustered results and the packed global head."""

import importlib.util
import json
import sys
from pathlib import Path


SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "experiment_places_locality_head.py"
spec = importlib.util.spec_from_file_location("experiment_places_locality_head", SCRIPT)
experiment = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = experiment
spec.loader.exec_module(experiment)


def places():
    compact = sys.modules["experiment_places_compact_index"]
    rows = [
        {
            "id": f"sf-{index}",
            "name": "Starbucks",
            "category": "cafe",
            "lat": 37.77 + index / 10000,
            "lon": -122.42,
            "confidence": 1 - index / 100,
        }
        for index in range(12)
    ] + [
        {
            "id": f"la-{index}",
            "name": "Starbucks",
            "category": "cafe",
            "lat": 34.05,
            "lon": -118.24,
            "confidence": 0.7 - index / 100,
        }
        for index in range(4)
    ]
    return [compact.place_from_row(row, number) for number, row in enumerate(rows, 1)]


def test_spatial_order_clusters_high_ranked_cell_results():
    index = experiment.LocalityHeadIndex(
        places(), cell_degrees=0.25, head_minimum_candidates=2, head_bucket_count=16
    )
    case = experiment.QueryCase(
        "starbucks", (experiment.Clause("starbucks"),), "typical"
    )
    cell = index.preferred_cell(case)
    result = index.query(case, cell=cell)
    assert result["mode"] == "cell_routed"
    assert result["complete_candidate_recall"] is True
    assert result["top_k_exact"] is True
    assert result["operations"] <= 2


def test_global_head_is_exact_top_k_but_explicitly_not_candidate_complete():
    index = experiment.LocalityHeadIndex(
        places(), head_minimum_candidates=2, head_bucket_count=16
    )
    case = experiment.QueryCase(
        "starbucks", (experiment.Clause("starbucks"),), "typical"
    )
    result = index.query(case, use_global_head=True)
    assert result["mode"] == "global_head"
    assert result["top_k_exact"] is True
    assert result["complete_candidate_recall"] is False
    assert result["operations"] == 1
    assert "tail intentionally omitted" in result["coverage"]


def test_global_head_store_grows_hash_space_to_respect_object_target():
    index = experiment.LocalityHeadIndex(
        places(), head_minimum_candidates=2, head_bucket_count=1, head_target=700
    )
    assert index.head_store.bucket_count > 1
    assert max(page.size for page in index.head_store.pages) <= 700


def test_fielded_and_multi_clause_queries_fall_back_to_complete_postings():
    index = experiment.LocalityHeadIndex(
        places(), head_minimum_candidates=2, head_bucket_count=16
    )
    fielded = experiment.QueryCase(
        "cafe", (experiment.Clause("cafe", field="category"),), "typical"
    )
    result = index.query(fielded, use_global_head=True)
    assert result["mode"] == "full_fallback"
    assert result["complete_candidate_recall"] is True


def test_cli_writes_reports(tmp_path):
    source = tmp_path / "places.jsonl"
    source.write_text(
        "\n".join(
            json.dumps(
                {
                    "id": place.place_id,
                    "name": place.name,
                    "category": place.category,
                    "lat": place.lat,
                    "lon": place.lon,
                    "confidence": place.confidence,
                }
            )
            for place in places()
        )
    )
    json_out = tmp_path / "report.json"
    markdown_out = tmp_path / "report.md"
    assert (
        experiment.main(
            [
                str(source),
                "--json-out",
                str(json_out),
                "--markdown-out",
                str(markdown_out),
                "--head-minimum-candidates",
                "2",
            ]
        )
        == 0
    )
    report = json.loads(json_out.read_text())
    assert report["inventory"]["components"]["global_head"]["bytes"] > 0
    assert "global-head" in markdown_out.read_text()
