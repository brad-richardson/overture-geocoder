"""Adversarial tests for bounded transportation snapshot name clusters."""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import benchmark_transport_components as benchmark  # noqa: E402


def segment(
    segment_id: str,
    primary: str | None,
    connectors: list[str] | list[tuple[str, float]],
    *,
    common: dict[str, str] | None = None,
    rules: list[dict] | None = None,
    core_seed: bool = True,
    bbox: dict | None = None,
) -> dict:
    connector_records = [
        {"connector_id": value, "at": index / max(1, len(connectors) - 1)}
        if isinstance(value, str)
        else {"connector_id": value[0], "at": value[1]}
        for index, value in enumerate(connectors)
    ]
    return {
        "id": segment_id,
        "names": {"primary": primary, "common": common or {}, "rules": rules or []},
        "class": "residential",
        "connectors": connector_records,
        "sources": [{
            "property": "", "dataset": "example/transport", "license": "ODbL",
            "record_id": segment_id, "update_time": "2026-01-01", "confidence": 0.9,
        }],
        "version": 2,
        "bbox": bbox or {"xmin": 0.1, "ymin": 0.1, "xmax": 0.2, "ymax": 0.2},
        "representative_lon": 0.15,
        "representative_lat": 0.15,
        "core_seed": core_seed,
    }


def fixture_segments() -> list[dict]:
    return [
        segment("a", "Main St.", ["1", "2"], common={"en": "Main Street"}),
        segment("b", "Main St.", ["2", "3"]),
        segment("c", "Main St.", ["4", "5"]),
        segment(
            "d", "Broadway", ["2", "6"],
            rules=[{
                "variant": "alternate", "language": "en", "value": "Main Street",
                "between": [0.2, 0.8], "side": "left",
                "perspectives": [{"mode": "accepted", "countries": ["US"]}],
            }],
        ),
    ]


def test_scoped_rules_are_excluded_and_common_aliases_are_separate():
    segments = fixture_segments()
    clusters = benchmark.build_snapshot_name_clusters(
        segments, benchmark.PINNED_RELEASE, (0, 0, 1, 1)
    )
    assert not any(
        cluster["primary_name_normalized"] == "main street" for cluster in clusters
    )
    main = [
        cluster for cluster in clusters
        if cluster["primary_name_normalized"] == "main st."
    ]
    assert sorted(cluster["segment_ids"] for cluster in main) == [["a", "b"], ["c"]]
    aliases = benchmark.build_alias_lookup(segments, clusters)
    assert aliases["main street"] == [
        next(cluster["snapshot_cluster_id"] for cluster in main if "a" in cluster["segment_ids"])
    ]
    rules = benchmark.rule_assertion_summary(segments)
    assert rules["excluded_from_topology_grouping"] is True
    assert rules["assertions"] == rules["with_between"] == rules["with_side"] == 1


def test_snapshot_cluster_id_is_release_and_membership_scoped():
    segments = fixture_segments()
    left = benchmark.build_snapshot_name_clusters(
        segments, benchmark.PINNED_RELEASE, (0, 0, 1, 1)
    )
    reordered = benchmark.build_snapshot_name_clusters(
        list(reversed(copy.deepcopy(segments))), benchmark.PINNED_RELEASE, (0, 0, 1, 1)
    )
    other_release = benchmark.build_snapshot_name_clusters(
        segments, "2026-07-15.0", (0, 0, 1, 1)
    )
    assert sorted(item["snapshot_cluster_id"] for item in left) == sorted(
        item["snapshot_cluster_id"] for item in reordered
    )
    assert {item["snapshot_cluster_id"] for item in left}.isdisjoint(
        item["snapshot_cluster_id"] for item in other_release
    )
    assert all(item["overture_release"] == benchmark.PINNED_RELEASE for item in left)


def test_frontier_halo_support_and_core_only_reporting():
    segments = [
        segment("core", "Boundary Road", ["a", "b"]),
        segment(
            "support", "Boundary Road", ["b", "c"], core_seed=False,
            bbox={"xmin": 0.9, "ymin": 0.1, "xmax": 1.0, "ymax": 0.2},
        ),
        segment("outside", "Other", ["x", "y"], core_seed=False),
    ]
    clusters = benchmark.build_snapshot_name_clusters(segments, "r1", (0, 0, 1, 1))
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["core_seed_segment_count"] == 1
    assert cluster["halo_support_segment_count"] == 1
    assert cluster["frontier"] is True
    assert cluster["frontier_open_connector_proxy_count"] > 0


def test_core_boundary_crossing_marks_frontier_without_halo_support():
    crossing = segment("crossing", "Boundary Road", ["a", "b"])
    crossing["boundary_crossing"] = True
    clusters = benchmark.build_snapshot_name_clusters([crossing], "r1", (0, 0, 1, 1))
    assert clusters[0]["frontier"] is True


def test_official_boundary_identity_is_fully_pinned():
    assert benchmark.BOUNDARY_DIVISION_ID == "5df2793f-5a0a-4fcf-bd3c-7edb8cc495d8"
    assert benchmark.BOUNDARY_DIVISION_VERSION == 6
    assert benchmark.BOUNDARY_AREA_ID == "78cd3b93-9cd5-4023-9e31-4194be70701b"
    assert benchmark.BOUNDARY_AREA_VERSION == 10
    assert "official dataset polygon" in benchmark.BOUNDARY_LABEL
    assert "experimental Boston-area bbox" not in benchmark.__doc__
    assert "not legal authority" in benchmark.__doc__


def test_markdown_uses_polygon_not_core_bbox_contract():
    source = Path(benchmark.__file__).read_text()
    assert "Polygon scan bbox" in source
    assert "Core bbox" not in source
    assert "not a claim of legal boundary authority" in source


def test_halo_comparison_detects_support_connectivity_change():
    core_bbox = (0.0, 0.0, 1.0, 1.0)
    segments = [
        segment("a", "Main", ["a0", "h1"], bbox={"xmin": 0.1, "ymin": 0.1, "xmax": 0.2, "ymax": 0.2}),
        segment("b", "Main", ["h2", "b0"], bbox={"xmin": 0.8, "ymin": 0.1, "xmax": 0.9, "ymax": 0.2}),
        segment(
            "halo", "Main", ["h1", "h2"], core_seed=False,
            bbox={"xmin": 1.001, "ymin": 0.1, "xmax": 1.004, "ymax": 0.2},
        ),
    ]
    result = benchmark.compare_halo_snapshots(segments, "r1", core_bbox, 0.005)
    assert result["inner_core_touching_snapshot_clusters"] == 2
    assert result["outer_core_touching_snapshot_clusters"] == 1
    assert result["core_named_segments_with_changed_core_peer_membership"] == 2


def test_connector_validation_counts_interior_duplicates_and_missing_features():
    segments = [segment("a", "Main", [
        ("endpoint", 0.0), ("interior", 0.5), ("same-at", 0.5),
        ("duplicate", 1.0), ("duplicate", 1.0), ("invalid", 1.2),
    ])]
    result = benchmark.connector_validation(
        segments, {"endpoint", "interior", "same-at", "duplicate"}
    )
    assert result["endpoint_connector_references"] == 3
    assert result["interior_connector_references"] == 2
    assert result["invalid_at_references"] == 1
    assert result["duplicate_connector_id_references_within_segment"] == 1
    assert result["duplicate_at_references_within_segment"] == 2
    assert result["referenced_connector_ids_missing_from_bounded_connector_extract"] == 1


def test_shared_interior_connector_is_a_topology_edge():
    segments = [
        segment("a", "Main", [("left", 0.0), ("shared", 0.4)]),
        segment("b", "Main", [("shared", 0.6), ("right", 1.0)]),
    ]
    clusters = benchmark.build_snapshot_name_clusters(segments, "r1", (0, 0, 1, 1))
    assert len(clusters) == 1
    assert clusters[0]["segment_ids"] == ["a", "b"]


def valid_args() -> argparse.Namespace:
    return argparse.Namespace(
        release=benchmark.PINNED_RELEASE, bbox=[0, 0, 1, 1], halo=0.1,
        max_transport_rows=10, max_transport_bytes=10, max_connector_rows=10,
        max_connector_bytes=10, max_address_rows=10, max_crossing_segments=10,
        max_crossing_tile_memberships=10,
        max_crossing_candidate_pairs=10, crossing_tile_degrees=0.1,
        remote_time_cap_seconds=10,
        json_out=None, markdown_out=None,
    )


@pytest.mark.parametrize("field", [
    "max_transport_rows", "max_transport_bytes", "max_connector_rows",
    "max_connector_bytes", "max_address_rows", "max_crossing_segments",
    "max_crossing_tile_memberships",
    "max_crossing_candidate_pairs",
])
def test_guards_must_be_positive_before_remote_work(field: str):
    args = valid_args()
    setattr(args, field, 0)
    with pytest.raises(ValueError, match="must be positive"):
        benchmark.run(args)


def test_address_guard_has_non_overridable_250k_ceiling():
    args = valid_args()
    args.max_address_rows = benchmark.ADDRESS_HARD_MAX_ROWS + 1
    with pytest.raises(ValueError, match="hard cap"):
        benchmark.run(args)


def test_address_hash_sample_plan_avoids_cap_edge_and_handles_empty():
    plan = benchmark.address_hash_sample_plan(309_946, 100_000)
    assert plan["sampled"] is True
    assert plan["target_rows"] == 95_000
    assert 0 < plan["hash_threshold"] < plan["hash_space"]
    empty = benchmark.address_hash_sample_plan(0, 100_000)
    assert empty["sampled"] is False
    assert empty["hash_threshold"] == 2**32


def test_serialized_estimate_deduplicates_segment_metadata():
    segments = fixture_segments()
    clusters = benchmark.build_snapshot_name_clusters(
        segments, benchmark.PINNED_RELEASE, (0, 0, 1, 1)
    )
    estimate = benchmark.serialized_snapshot_estimate(
        segments, clusters, benchmark.build_alias_lookup(segments, clusters)
    )
    assert estimate["segment_records"] == 4
    assert estimate["snapshot_cluster_records"] == 3
    assert estimate["total_bytes"] > estimate["snapshot_cluster_records_bytes"]
    assert estimate["hot_cluster_lookup_bytes"] < estimate["total_bytes"]
    assert estimate["detail_segment_index_bytes"] == estimate["deduplicated_segment_index_bytes"]


def test_provenance_coverage_includes_full_source_shape_and_warning():
    coverage = benchmark.provenance_coverage(fixture_segments())
    assert "between" in coverage["all_source_field_populated_records"]
    assert coverage["source_property_records"] == {"<root>": 4}
    assert "not calibrated" in coverage["confidence_interpretation"]


def test_name_normalization_preserves_punctuation_and_unicode():
    assert benchmark.normalize_name("  Rue  de l'Église  ") == "rue de l'église"
    assert benchmark.normalize_name("Main-St.") != benchmark.normalize_name("Main St")


def test_committable_report_sanitizer_removes_row_examples_recursively():
    report = {
        "summary": {"rows": 2, "examples": [{"id": "secret"}]},
        "missing_reference_examples": ["connector-id"],
        "nested": [{"repeated_name_examples": [{"name": "Main"}]}],
    }
    assert benchmark.sanitize_aggregate_report(report) == {
        "summary": {"rows": 2}, "nested": [{}],
    }
