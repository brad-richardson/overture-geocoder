"""Hermetic tests for the range-readable compact spatial shard."""

import importlib.util
import json
import sys
from pathlib import Path


SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "experiment_places_compact_shard.py"
spec = importlib.util.spec_from_file_location("experiment_places_compact_shard", SCRIPT)
experiment = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = experiment
spec.loader.exec_module(experiment)


def places():
    compact = sys.modules["experiment_places_compact_index"]
    rows = [
        {
            "id": "a",
            "name": "Golden Gate Cafe",
            "category": "coffee shop",
            "city": "San Francisco",
            "region": "CA",
            "country": "US",
            "confidence": 0.9,
            "lat": 37.77,
            "lon": -122.42,
        },
        {
            "id": "b",
            "name": "Golden Hotel",
            "category": "hotel",
            "city": "San Francisco",
            "region": "CA",
            "country": "US",
            "confidence": 0.8,
            "lat": 37.78,
            "lon": -122.42,
        },
        {
            "id": "c",
            "name": "Gateway Harbor Cafe",
            "category": "coffee shop",
            "city": "Oakland",
            "region": "CA",
            "country": "US",
            "confidence": 0.7,
            "lat": 37.80,
            "lon": -122.27,
        },
    ]
    return [compact.place_from_row(row, number) for number, row in enumerate(rows, 1)]


def test_projection_round_trip_omits_non_result_brand():
    place = places()[0]
    decoded = experiment.decode_projection(experiment.encode_projection(place))
    assert decoded["id"] == "a"
    assert decoded["name"] == "Golden Gate Cafe"
    assert decoded["category"] == "coffee shop"
    assert "brand" not in decoded


def test_exact_prefix_fielded_and_multi_clause_recall(tmp_path):
    artifact = tmp_path / "places.pcsh"
    ordered, _ = experiment.build_artifact(places(), artifact, block_entries=2)
    cases = (
        experiment.QueryCase("exact", (experiment.Clause("hotel"),), "typical"),
        experiment.QueryCase(
            "prefix", (experiment.Clause("gat", prefix=True),), "typical"
        ),
        experiment.QueryCase(
            "fielded", (experiment.Clause("hotel", field="category"),), "typical"
        ),
        experiment.QueryCase(
            "multi",
            (experiment.Clause("golden"), experiment.Clause("gat", prefix=True)),
            "typical",
        ),
    )
    for case in cases:
        shard = experiment.CompactShard(artifact)
        result = shard.query(case)
        expected, ids = experiment.oracle(ordered, case)
        assert set(result["candidate_doc_ids"]) == expected
        assert result["result_ids"] == ids


def test_equal_sized_wrong_candidate_set_is_not_complete(tmp_path, monkeypatch):
    artifact = tmp_path / "places.pcsh"
    ordered, _ = experiment.build_artifact(places(), artifact, block_entries=2)
    original_query = experiment.CompactShard.query

    def corrupt_query(self, case, **kwargs):
        result = original_query(self, case, **kwargs)
        if result["candidate_doc_ids"]:
            result["candidate_doc_ids"] = [999_999] + result["candidate_doc_ids"][1:]
        return result

    monkeypatch.setattr(experiment.CompactShard, "query", corrupt_query)
    report = experiment.benchmark(ordered, artifact)
    assert report["summary"]["complete_candidate_recall"] is False


def test_prefix_uses_one_contiguous_posting_read(tmp_path):
    artifact = tmp_path / "places.pcsh"
    experiment.build_artifact(places(), artifact, block_entries=1)
    shard = experiment.CompactShard(artifact)
    result = shard.query(
        experiment.QueryCase("gat", (experiment.Clause("gat", prefix=True),), "typical")
    )
    assert result["candidate_count"] == 2
    assert result["stages"]["postings"]["reads"] == 1


def test_artifact_components_are_contiguous_and_range_readable(tmp_path):
    artifact = tmp_path / "places.pcsh"
    _, build = experiment.build_artifact(places(), artifact)
    shard = experiment.CompactShard(artifact)
    components = shard.directory["components"]
    names = ("lexicon", "postings", "record_index", "records")
    for left, right in zip(names, names[1:]):
        assert (
            components[left]["offset"] + components[left]["length"]
            == components[right]["offset"]
        )
    assert build["artifact_bytes"] == artifact.stat().st_size
    variants = build["projection_variants"]
    assert (
        variants["locator_only"]["artifact_bytes_if_substituted"]
        < variants["name_only"]["artifact_bytes_if_substituted"]
    )
    assert (
        variants["name_only"]["artifact_bytes_if_substituted"]
        < variants["search_response"]["artifact_bytes_if_substituted"]
    )
    postings = build["posting_field_variants"]
    assert postings["name_only"]["bytes"] < postings["all_fields"]["bytes"]


def test_cli_writes_reports(tmp_path):
    source = tmp_path / "places.json"
    source.write_text(
        json.dumps(
            [
                {
                    "id": "a",
                    "name": "Golden Gate Cafe",
                    "category": "cafe",
                    "confidence": 0.9,
                },
                {
                    "id": "b",
                    "name": "Harbor Hotel",
                    "category": "hotel",
                    "confidence": 0.8,
                },
            ]
        )
    )
    artifact = tmp_path / "places.pcsh"
    json_out = tmp_path / "report.json"
    markdown_out = tmp_path / "report.md"
    assert (
        experiment.main(
            [
                str(source),
                "--artifact",
                str(artifact),
                "--json-out",
                str(json_out),
                "--markdown-out",
                str(markdown_out),
            ]
        )
        == 0
    )
    report = json.loads(json_out.read_text())
    shape = report["architecture"]["proposed_planet_object_shape_at_75m"]
    assert shape["shard_objects"] == 75
    assert shape["measured_by_this_experiment"] is False
    assert report["benchmark"]["summary"]["complete_candidate_recall"] is True
    assert "compact spatial-shard" in markdown_out.read_text()
