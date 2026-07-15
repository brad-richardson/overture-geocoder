"""Tests for cell-local heavy posting fragments."""

import importlib.util
import json
import sys
from pathlib import Path


SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "experiment_places_cell_postings.py"
spec = importlib.util.spec_from_file_location("experiment_places_cell_postings", SCRIPT)
experiment = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = experiment
spec.loader.exec_module(experiment)


def places():
    compact = sys.modules["experiment_places_compact_index"]
    rows = [
        {
            "id": f"sf-{index}",
            "name": "Starbucks Reserve",
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


def test_located_exact_and_long_prefix_use_complete_local_postings():
    index = experiment.CellPostingIndex(
        places(),
        cell_posting_minimum_candidates=2,
        head_minimum_candidates=2,
        cell_posting_bucket_count=16,
        head_bucket_count=16,
    )
    for clause in (
        experiment.Clause("starbucks"),
        experiment.Clause("starbu", prefix=True),
    ):
        case = experiment.QueryCase("query", (clause,), "typical")
        result = index.query(case, cell=index.preferred_cell(case))
        assert result["mode"] == "cell_local_postings"
        assert result["complete_candidate_recall"] is True
        assert result["top_k_exact"] is True
        assert result["operations"] <= 2


def test_multi_clause_local_postings_intersect_with_complete_recall():
    index = experiment.CellPostingIndex(
        places(),
        cell_posting_minimum_candidates=2,
        head_minimum_candidates=2,
        cell_posting_bucket_count=16,
        head_bucket_count=16,
    )
    case = experiment.QueryCase(
        "reserve cafe",
        (experiment.Clause("reserve"), experiment.Clause("cafe", field="category")),
        "typical",
    )
    result = index.query(case, cell=index.preferred_cell(case))
    assert result["mode"] == "cell_local_postings"
    assert result["complete_candidate_recall"] is True
    assert result["operations"] <= 3


def test_ineligible_clause_uses_complete_global_fallback():
    index = experiment.CellPostingIndex(
        places(),
        cell_posting_minimum_candidates=20,
        head_minimum_candidates=20,
    )
    case = experiment.QueryCase("reserve", (experiment.Clause("reserve"),), "typical")
    result = index.query(case, cell=index.preferred_cell(case))
    assert result["mode"] == "cell_global_fallback"
    assert result["complete_candidate_recall"] is True


def test_hash_fanout_is_sized_independently_per_cell():
    cells = ["dense"] * 20 + ["sparse"]
    mappings = {
        f"e:term-{number}": {
            **{doc: (1, 200) for doc in range(20)},
            20: (1, 200),
        }
        for number in range(24)
    }
    store = experiment.CellPostingStore(
        "fixture", mappings, cells, target=128, bucket_count=1
    )
    assert store.bucket_counts["dense"] >= store.bucket_counts["sparse"]
    assert max(page.size for page in store.all_pages()) <= 128


def test_cli_writes_cell_posting_inventory(tmp_path):
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
                "--cell-minimum-candidates",
                "2",
            ]
        )
        == 0
    )
    report = json.loads(json_out.read_text())
    component = report["inventory"]["components"]["cell_local_postings"]
    assert component["bytes"] > 0
    assert component["term_cell_entries"] > component["keys"]
    assert "cell-local" in markdown_out.read_text()
