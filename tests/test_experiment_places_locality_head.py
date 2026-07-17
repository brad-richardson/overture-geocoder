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


def famous_places():
    compact = sys.modules["experiment_places_compact_index"]
    rows = [
        {
            "id": f"dense-{index:02}",
            "name": "Tokyo Grand Cafe",
            "category": "cafe",
            "lat": 35.68,
            "lon": 139.75,
            "confidence": 0.95 - index / 1000,
        }
        for index in range(11)
    ]
    rows.append(
        {
            "id": "famous-tower",
            "name": "Tokyo Tower",
            "category": "landmark",
            "lat": 35.6586,
            "lon": 139.7454,
            "confidence": 0.93,
        }
    )
    rows.append(
        {
            "id": "obscure-shack",
            "name": "Obscure Shack",
            "category": "shed",
            "lat": 35.60,
            "lon": 139.70,
            "confidence": 0.05,
        }
    )
    return [compact.place_from_row(row, number) for number, row in enumerate(rows, 1)]


def index_for(famous_cap):
    return experiment.LocalityHeadIndex(
        famous_places(),
        head_minimum_candidates=2,
        head_bucket_count=16,
        head_famous_cap=famous_cap,
    )


def test_famous_cap_zero_preserves_density_gated_admission():
    heads = index_for(0).heads
    assert "e:tower" not in heads
    assert not any(key.startswith("e2:") for key in heads)


def test_rare_prominent_token_is_admitted_and_rare_obscure_is_not():
    index = index_for(12)
    heads = index.heads
    assert [index.places[doc].place_id for doc in heads["e:tower"]] == [
        "famous-tower"
    ]
    # The obscure place is outside the famous cap, so its unique tokens stay
    # below the density floor and are not admitted.
    assert "e:obscure" not in heads
    assert "e:shack" not in heads


def test_famous_pair_entry_is_exact_intersection_and_dense_entries_unchanged():
    index = index_for(12)
    heads = index.heads
    baseline = index_for(0).heads
    # The famous pair serves the AND directly even though the famous place is
    # squeezed out of the dense token's per-token top-10.
    assert [index.places[doc].place_id for doc in heads["e2:tokyo tower"]] == [
        "famous-tower"
    ]
    assert "famous-tower" not in {
        index.places[doc].place_id for doc in heads["e:tokyo"]
    }
    # Entries admitted without the famous cap are identical with it enabled.
    for key, docs in baseline.items():
        assert heads[key] == docs


def test_pair_generation_uses_first_eight_tokens_and_sorted_keys():
    compact = sys.modules["experiment_places_compact_index"]
    rows = [
        {
            "id": "many-tokens",
            "name": "Alpha Beta Gamma Delta Epsilon Zeta Eta Theta Iota Kappa",
            "category": "cafe",
            "lat": 0.0,
            "lon": 0.0,
            "confidence": 0.99,
        },
        {
            "id": "other",
            "name": "Unrelated",
            "category": "cafe",
            "lat": 0.1,
            "lon": 0.1,
            "confidence": 0.5,
        },
    ]
    places_list = [
        compact.place_from_row(row, number) for number, row in enumerate(rows, 1)
    ]
    index = experiment.LocalityHeadIndex(
        places_list,
        head_minimum_candidates=2,
        head_bucket_count=16,
        head_famous_cap=1,
    )
    pair_keys = sorted(key for key in index.heads if key.startswith("e2:"))
    # First 8 distinct tokens -> exactly C(8, 2) = 28 unordered pairs.
    assert len(pair_keys) == 28
    excluded = {"iota", "kappa"}
    for key in pair_keys:
        low, high = key[len("e2:") :].split(" ")
        assert low < high
        assert excluded.isdisjoint({low, high})


def test_famous_head_build_is_deterministic():
    first = index_for(12).heads
    second = index_for(12).heads
    assert first == second
    assert list(first) == list(second)


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
