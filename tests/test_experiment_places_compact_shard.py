"""Hermetic tests for the range-readable compact spatial shard."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "experiment_places_compact_shard.py"
spec = importlib.util.spec_from_file_location("experiment_places_compact_shard", SCRIPT)
experiment = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = experiment
spec.loader.exec_module(experiment)

GENERATOR_SCRIPT = Path(__file__).with_name("generate_places_page_fixtures.py")
GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "generate_places_page_fixtures", GENERATOR_SCRIPT
)
assert GENERATOR_SPEC and GENERATOR_SPEC.loader
fixture_generator = importlib.util.module_from_spec(GENERATOR_SPEC)
sys.modules[GENERATOR_SPEC.name] = fixture_generator
GENERATOR_SPEC.loader.exec_module(fixture_generator)


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


def _rank(place) -> int:
    return round(place.confidence * 255)


def scattered_places():
    """Places whose spatial (doc) order deliberately differs from rank order, so
    the records-by-rank layout is a non-identity permutation."""
    compact = sys.modules["experiment_places_compact_index"]
    Place = compact.Place
    # Distinct spatial cells (wide lon spread), confidences NOT monotonic with
    # cell/doc order, so ordered_places (spatial, -rank, id) != rank order.
    rows = [
        ("west", 0.72, 37.0, -122.0),
        ("mid_a", 0.95, 37.0, -100.0),
        ("mid_b", 0.61, 37.0, -100.0),
        ("east", 0.88, 37.0, -70.0),
        ("far", 0.80, 37.0, -40.0),
    ]
    return [
        Place(
            place_id=pid,
            name=f"Cafe {pid}",
            brand="Shared Brand",
            category="cafe",
            locality="Town",
            region="RG",
            country="US",
            lat=lat,
            lon=lon,
            confidence=conf,
        )
        for pid, conf, lat, lon in rows
    ]


def test_records_blob_is_laid_out_in_serving_rank_order(tmp_path):
    artifact = tmp_path / "rank.pcsh"
    ordered, _ = experiment.build_artifact(scattered_places(), artifact)
    # Precondition: this input actually reorders (doc order != rank order).
    rank_order = sorted(range(len(ordered)), key=lambda doc: (-_rank(ordered[doc]), doc))
    assert rank_order != list(range(len(ordered)))

    shard = experiment.CompactShard(artifact)
    base, length = shard.component("record_index")
    index_bytes = shard.reader.read(base, length, "record_index")
    positions = [
        experiment.RECORD_INDEX.unpack_from(index_bytes, doc * experiment.RECORD_INDEX.size)
        for doc in range(len(ordered))
    ]
    # Physical record order (ascending offset) must equal serving-rank order.
    physical_order = sorted(range(len(ordered)), key=lambda doc: positions[doc][0])
    assert physical_order == rank_order
    # Extents tile the records component contiguously with no gaps/overlaps.
    records_len = shard.component("records")[1]
    cursor = 0
    for doc in physical_order:
        offset, size = positions[doc]
        assert offset == cursor
        cursor += size
    assert cursor == records_len


def test_rank_layout_round_trips_and_is_byte_deterministic(tmp_path):
    first = tmp_path / "a.pcsh"
    second = tmp_path / "b.pcsh"
    ordered, _ = experiment.build_artifact(scattered_places(), first)
    experiment.build_artifact(scattered_places(), second)
    assert first.read_bytes() == second.read_bytes()
    # record_index still resolves each doc to its own projection after reorder.
    case = experiment.QueryCase("brand", (experiment.Clause("shared"),), "typical")
    shard = experiment.CompactShard(first)
    result = shard.query(case)
    expected, ids = experiment.oracle(ordered, case)
    assert set(result["candidate_doc_ids"]) == expected
    assert result["result_ids"] == ids
    for row in result["results"]:
        # Confidence in the decoded projection matches the doc the ID belongs to.
        source = next(place for place in ordered if place.place_id == row["id"])
        assert round(row["confidence"] * 255) == _rank(source)


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


def test_split_posting_layout_splits_gap_zero_and_skips_dead_bytes(tmp_path):
    """Multi-entry posting coalescing: non-adjacent matched entries must split
    the gap-0 plan and never fetch the dead bytes between them; adjacent
    entries must merge into one physical read."""
    source = fixture_generator.split_places()
    artifact = tmp_path / "split.pcsh"
    ordered, _ = experiment.build_artifact(
        source, artifact, posting_layout=fixture_generator.split_posting_layout(source)
    )
    probe = experiment.CompactShard(artifact)
    matches = probe.lexicon_matches("shared", True)
    assert [entry.token for entry in matches] == ["shareda", "sharedz"]
    matched_bytes = sum(entry.posting_length for entry in matches)
    span = max(
        entry.posting_offset + entry.posting_length for entry in matches
    ) - min(entry.posting_offset for entry in matches)
    assert matched_bytes < span, "fixture must place a dead gap between matches"

    shard = experiment.CompactShard(artifact)
    case = experiment.QueryCase(
        "split", (experiment.Clause("shared", prefix=True),), "typical"
    )
    result = shard.query(case)
    expected, ids = experiment.oracle(ordered, case)
    assert set(result["candidate_doc_ids"]) == expected
    assert result["result_ids"] == ids
    # The physical plan splits instead of spanning the dead gap...
    assert result["stages"]["postings"]["reads"] == 2
    # ...and byte accounting proves the gap bytes were never fetched.
    assert result["stages"]["postings"]["bytes"] == matched_bytes

    adjacent = experiment.QueryCase(
        "adjacent", (experiment.Clause("adj", prefix=True),), "typical"
    )
    shard = experiment.CompactShard(artifact)
    result = shard.query(adjacent)
    expected, ids = experiment.oracle(ordered, adjacent)
    assert set(result["candidate_doc_ids"]) == expected
    assert result["result_ids"] == ids
    adjacent_matches = probe.lexicon_matches("adj", True)
    assert result["stages"]["postings"]["reads"] == 1
    assert result["stages"]["postings"]["bytes"] == sum(
        entry.posting_length for entry in adjacent_matches
    )


def test_posting_layout_must_permute_the_exact_token_set(tmp_path):
    with pytest.raises(ValueError):
        experiment.build_artifact(
            places(), tmp_path / "bad.pcsh", posting_layout=["only-token"]
        )


def test_clause_candidate_counts_mirror_worker_early_exit(tmp_path):
    """clause_candidate_counts is a diagnostic mirroring the Worker exactly:
    Some(decoded count) for clauses whose postings were read, None for clauses
    skipped by the no-lexicon-match or emptied-intersection early exits."""
    artifact = tmp_path / "places.pcsh"
    experiment.build_artifact(places(), artifact, block_entries=2)

    # Any clause without a lexicon match skips ALL posting reads.
    shard = experiment.CompactShard(artifact)
    result = shard.query(
        experiment.QueryCase(
            "nomatch",
            (experiment.Clause("golden"), experiment.Clause("zzznope")),
            "typical",
        )
    )
    assert result["clause_candidate_counts"] == [None, None]
    assert result["candidate_count"] == 0
    assert result["stages"]["postings"]["reads"] == 0

    # Both clauses decode, the intersection is empty: numeric counts, no
    # sentinel, and the result set is empty.
    shard = experiment.CompactShard(artifact)
    result = shard.query(
        experiment.QueryCase(
            "empty",
            (experiment.Clause("hotel"), experiment.Clause("gateway")),
            "typical",
        )
    )
    assert result["clause_candidate_counts"] == [1, 1]
    assert result["candidate_count"] == 0
    assert result["stages"]["postings"]["reads"] == 2

    # Once the running intersection empties, later clauses are never read.
    shard = experiment.CompactShard(artifact)
    result = shard.query(
        experiment.QueryCase(
            "sentinel",
            (
                experiment.Clause("hotel"),
                experiment.Clause("gateway"),
                experiment.Clause("golden"),
            ),
            "typical",
        )
    )
    assert result["clause_candidate_counts"] == [1, 1, None]
    assert result["candidate_count"] == 0
    assert result["stages"]["postings"]["reads"] == 2


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
