"""Unit tests for the family-release-slice scale report helper."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import slice_scale_report as ssr  # noqa: E402


def _region(name, rows, artifacts, byte_count, bbox=(-80.5, 38.0, -66.9, 47.5)):
    return {
        "name": name,
        "bbox": list(bbox),
        "rows": rows,
        "artifacts": artifacts,
        "bytes": byte_count,
    }


def _input(**overrides):
    document = {
        "schema": ssr.INPUT_SCHEMA,
        "slice_version": "slice-2026-07-18.0",
        "release": "2026-06-17.0",
        "verify_seconds": 42,
        "families": [
            {
                "family": "places",
                "manifest_digest": "a" * 64,
                "build_seconds": 1200,
                "publish_seconds": 60,
                "regions": [
                    _region("us-northeast", 4_133_950, 3, 481_000_000),
                    _region("dc-metro", 151_187, 1, 17_600_000,
                            bbox=(-77.3, 38.7, -76.7, 39.1)),
                ],
            },
            {
                "family": "addresses",
                "manifest_digest": "b" * 64,
                "build_seconds": 900,
                "publish_seconds": 40,
                "regions": [
                    _region("us-northeast", 5_000_000, 4, 600_000_000),
                    _region("dc-metro", 200_000, 1, 24_000_000,
                            bbox=(-77.3, 38.7, -76.7, 39.1)),
                ],
            },
        ],
    }
    document.update(overrides)
    return document


def test_scale_report_totals_and_structure():
    report = ssr.build_scale_report(_input())
    assert report["schema"] == ssr.REPORT_SCHEMA
    assert report["promotion_eligible"] is False
    assert report["verify_seconds"] == 42
    assert report["totals"]["families"] == 2
    assert report["totals"]["artifacts"] == 3 + 1 + 4 + 1
    assert report["totals"]["rows"] == 4_133_950 + 151_187 + 5_000_000 + 200_000
    families = {family["family"]: family for family in report["families"]}
    assert families["places"]["region_count"] == 2
    assert families["places"]["wall_seconds"] == {"build": 1200, "publish": 60}


def test_places_projects_to_conus_and_planet_from_largest_region():
    report = ssr.build_scale_report(_input())
    places = next(f for f in report["families"] if f["family"] == "places")
    extrapolation = places["planet_extrapolation"]
    # The largest measured region (US-NE) sets the bytes/row coefficient.
    assert extrapolation["basis_region"] == "us-northeast"
    coefficient = 481_000_000 / 4_133_950
    assert extrapolation["bytes_per_row"] == round(coefficient, 4)
    projections = extrapolation["projections"]
    assert set(projections) == {"conus", "planet"}
    assert projections["planet"]["reference_rows"] == 75_631_061
    assert projections["planet"]["projected_bytes"] == round(coefficient * 75_631_061)
    # A bigger reference row count must project more bytes.
    assert (
        projections["planet"]["projected_bytes"]
        > projections["conus"]["projected_bytes"]
    )


def test_addresses_projects_only_to_all_us():
    report = ssr.build_scale_report(_input())
    addresses = next(f for f in report["families"] if f["family"] == "addresses")
    assert set(addresses["planet_extrapolation"]["projections"]) == {"all_us"}


def test_rejects_wrong_schema():
    with pytest.raises(ValueError, match="input schema"):
        ssr.build_scale_report({"schema": "nope", "families": []})


def test_rejects_missing_verify_seconds():
    document = _input()
    del document["verify_seconds"]
    with pytest.raises(ValueError, match="verify_seconds"):
        ssr.build_scale_report(document)


def test_rejects_duplicate_family():
    document = _input()
    document["families"].append(document["families"][0])
    with pytest.raises(ValueError, match="duplicate family"):
        ssr.build_scale_report(document)


def test_rejects_non_positive_region_rows():
    document = _input()
    document["families"][0]["regions"][0]["rows"] = 0
    with pytest.raises(ValueError, match="rows"):
        ssr.build_scale_report(document)


def test_markdown_renders_rows_and_projection_lines():
    report = ssr.build_scale_report(_input())
    markdown = ssr.render_markdown(report)
    assert "slice-2026-07-18.0" in markdown
    assert "verify 42s (slice-wide)" in markdown
    assert "us-northeast" in markdown
    assert "places → planet" in markdown
    assert "addresses → all_us" in markdown


def test_report_is_deterministic():
    first = ssr.build_scale_report(_input())
    second = ssr.build_scale_report(_input())
    assert first == second
