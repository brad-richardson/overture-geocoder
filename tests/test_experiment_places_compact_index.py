"""Hermetic tests for the compact Places index experiment."""

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT = Path(__file__).parent.parent / "scripts" / "experiment_places_compact_index.py"
spec = importlib.util.spec_from_file_location("experiment_places_compact_index", SCRIPT)
experiment = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = experiment
spec.loader.exec_module(experiment)


def places():
    rows = [
        {
            "id": "a",
            "name": "Golden Gate Cafe",
            "category": "coffee shop",
            "city": "San Francisco",
            "region": "CA",
            "country": "US",
            "confidence": 0.9,
        },
        {
            "id": "b",
            "name": "Golden Hotel",
            "category": "hotel",
            "city": "San Francisco",
            "region": "CA",
            "country": "US",
            "confidence": 0.8,
        },
        {
            "id": "c",
            "name": "Harbor Cafe",
            "category": "coffee shop",
            "city": "Oakland",
            "region": "CA",
            "country": "US",
            "confidence": 0.7,
        },
    ]
    return [experiment.place_from_row(row, index) for index, row in enumerate(rows, 1)]


def test_varint_round_trip():
    for value in (0, 1, 127, 128, 16_384, 2**32):
        encoded = experiment.encode_varint(value)
        assert experiment.decode_varint(encoded) == (value, len(encoded))


def test_record_round_trip_uses_only_result_fields():
    place = places()[0]
    result = experiment.decode_record(experiment.encode_record(place))
    assert result["id"] == "a"
    assert result["name"] == "Golden Gate Cafe"
    assert result["category"] == "coffee shop"
    assert abs(result["confidence"] - 0.9) < 1e-6


def test_exact_prefix_and_field_postings(tmp_path):
    artifact = tmp_path / "places.pcix"
    experiment.build_artifact(places(), artifact, block_entries=2)

    index = experiment.CompactIndex(artifact)
    results, io = index.search("gold gat", limit=10)
    assert [row["id"] for row in results] == ["a"]
    assert io["unique_bytes"] < artifact.stat().st_size
    assert io["range_reads"] > 0

    index = experiment.CompactIndex(artifact)
    results, _ = index.search("hotel", limit=10)
    assert [row["id"] for row in results] == ["b"]

    index = experiment.CompactIndex(artifact)
    results, _ = index.search("category:hotel", limit=10)
    assert [row["id"] for row in results] == ["b"]

    index = experiment.CompactIndex(artifact)
    results, _ = index.search("name:hotel", limit=10)
    assert [row["id"] for row in results] == ["b"]


def test_context_is_searchable_without_becoming_result_payload_noise(tmp_path):
    artifact = tmp_path / "places.pcix"
    experiment.build_artifact(places(), artifact)
    index = experiment.CompactIndex(artifact)
    results, _ = index.search("oak harb")
    assert [row["id"] for row in results] == ["c"]


def test_benchmark_reports_sqlite_overlap_and_range_bytes(tmp_path):
    artifact = tmp_path / "places.pcix"
    source = places()
    experiment.build_artifact(source, artifact)
    report = experiment.benchmark(source, artifact, ["golden", "coffee shop"], 10)
    assert report["summary"]["query_count"] == 2
    assert report["summary"]["mean_sqlite_top_k_recall"] == 1.0
    assert report["summary"]["query_bytes_touched_max"] < artifact.stat().st_size


def test_cli_writes_reproducible_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
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
    artifact = tmp_path / "places.pcix"
    json_out = tmp_path / "report.json"
    markdown_out = tmp_path / "report.md"
    result = experiment.main(
        [
            str(source),
            "--artifact",
            str(artifact),
            "--json-out",
            str(json_out),
            "--markdown-out",
            str(markdown_out),
            "--query-count",
            "2",
        ]
    )
    assert result == 0
    report = json.loads(json_out.read_text())
    assert report["schema_version"] == 1
    assert report["build"]["places"] == 2
    markdown = markdown_out.read_text()
    assert "range-read architecture spike" in markdown
    assert "Existing unproven trie baseline: not available" in markdown
