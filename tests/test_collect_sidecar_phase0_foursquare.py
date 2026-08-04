"""Contracts for the public Foursquare/Wikidata Phase 0 collector."""

import importlib.util
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).parent.parent
    / "scripts"
    / "collect_sidecar_phase0_foursquare.py"
)
spec = importlib.util.spec_from_file_location(
    "collect_sidecar_phase0_foursquare", SCRIPT
)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def binding(qid, external_id, *, label="Example", point="Point(2.0 48.0)"):
    value = {
        "item": {"value": f"http://www.wikidata.org/entity/{qid}"},
        "foursquare": {"value": external_id},
        "itemLabel": {"value": label},
    }
    if point is not None:
        value["coord"] = {"value": point}
    return value


def test_snapshot_parser_consolidates_qid_claims_deterministically():
    result = collector.parse_sparql_snapshot({
        "results": {"bindings": [
            binding("Q2", "venue-z", label="Zulu"),
            binding("Q1", "venue-b", label="Bravo"),
            binding("Q1", "venue-a", label="Alpha"),
        ]}
    })
    assert [row["wikidata_qid"] for row in result] == ["Q1", "Q2"]
    assert result[0] == {
        "wikidata_qid": "Q1",
        "names": ["Alpha", "Bravo"],
        "latitude": 48.0,
        "longitude": 2.0,
        "coordinate_candidates": [{"latitude": 48.0, "longitude": 2.0}],
        "external_ids": {"Foursquare": ["venue-a", "venue-b"]},
    }


def test_snapshot_parser_retains_multiple_coordinates_without_choosing_one():
    result = collector.parse_sparql_snapshot({
        "results": {"bindings": [
            binding("Q1", "venue-a", point="Point(2 48)"),
            binding("Q1", "venue-b", point="Point(3 49)"),
        ]}
    })
    assert result[0]["latitude"] is None
    assert result[0]["longitude"] is None
    assert result[0]["coordinate_candidates"] == [
        {"latitude": 48.0, "longitude": 2.0},
        {"latitude": 49.0, "longitude": 3.0},
    ]


def test_public_paths_validate_release_components():
    assert "dataset=Foursquare" in collector.bridge_glob("2026-06-17.0")
    assert "theme=places/type=place" in collector.places_glob("2026-06-17.0")
    with pytest.raises(ValueError):
        collector.bridge_glob("latest")
    with pytest.raises(ValueError):
        collector.places_glob("../2026-06-17.0")


def test_query_contract_is_bounded_and_uses_direct_external_id_property():
    assert "wdt:P1968" in collector.SPARQL_QUERY
    assert "LIMIT 1000" in collector.SPARQL_QUERY
    assert "itemLabel" in collector.SPARQL_QUERY
