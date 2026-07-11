"""Focused tests for boundary-aware H3 country cell classification."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("h3")
pytest.importorskip("shapely")

from shapely.geometry import Point, Polygon, box

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import build_country_h3_index as country_h3


def cell_at(lat, lon, resolution=3):
    if hasattr(country_h3.h3, "latlng_to_cell"):
        return country_h3.h3.latlng_to_cell(lat, lon, resolution)
    return country_h3.h3.geo_to_h3(lat, lon, resolution)


def classify(cell, countries, resolution=3):
    return country_h3.build_h3_index(
        countries,
        resolution=resolution,
        simplify_tol=0,
        candidate_cells=[cell],
    )


def test_fully_covered_cell_is_interior():
    cell = cell_at(35.68, 139.69)
    cell_polygon = country_h3._cell_polygon(cell)

    interior, boundary, candidates = classify(
        cell, {"JP": cell_polygon.buffer(0.25)}
    )

    assert interior == {cell: "JP"}
    assert boundary == {}
    assert candidates[cell] == {"JP"}


def test_cell_crossing_country_border_has_both_candidates():
    cell = cell_at(49.0, -123.0)
    cell_polygon = country_h3._cell_polygon(cell)
    min_x, min_y, max_x, max_y = cell_polygon.bounds
    split_x = cell_polygon.centroid.x
    margin = 1
    countries = {
        "CA": box(min_x - margin, min_y - margin, split_x, max_y + margin),
        "US": box(split_x, min_y - margin, max_x + margin, max_y + margin),
    }

    interior, boundary, candidates = classify(cell, countries)

    assert interior == {}
    assert boundary[cell]["candidates"] == ["CA", "US"]
    assert candidates[cell] == {"CA", "US"}


@pytest.mark.parametrize("shape", ["coast", "hole"])
def test_partial_single_country_cell_is_boundary(shape):
    cell = cell_at(37.77, -122.42)
    cell_polygon = country_h3._cell_polygon(cell)
    if shape == "coast":
        min_x, min_y, max_x, max_y = cell_polygon.bounds
        country = cell_polygon.buffer(0.1).intersection(
            box(min_x - 1, min_y - 1, cell_polygon.centroid.x, max_y + 1)
        )
    else:
        country = cell_polygon.buffer(0.1).difference(
            Point(cell_polygon.centroid).buffer(0.01)
        )

    interior, boundary, candidates = classify(cell, {"US": country})

    assert interior == {}
    assert boundary[cell]["candidates"] == ["US"]
    assert candidates[cell] == {"US"}


def test_dateline_crossing_cell_uses_small_unwrapped_polygon():
    cell = cell_at(0, 179.9)
    cell_polygon = country_h3._cell_polygon(cell)
    assert cell_polygon.bounds[2] - cell_polygon.bounds[0] < 10

    # Supply a conventional [-180, 180] ring that crosses the antimeridian.
    country = Polygon([
        (179.0, -2.0),
        (-179.0, -2.0),
        (-179.0, 2.0),
        (179.0, 2.0),
        (179.0, -2.0),
    ])
    interior, boundary, candidates = classify(cell, {"DL": country})

    assert interior == {cell: "DL"}
    assert boundary == {}
    assert candidates[cell] == {"DL"}


def test_low_resolution_global_cell_counts_are_stable():
    assert len(country_h3._cells_at_resolution(2)) == 5_882
    assert len(country_h3._cells_at_resolution(3)) == 41_162


def test_merge_country_components_fails_instead_of_dropping_parts(monkeypatch):
    def fail_union(_geoms):
        raise ValueError("synthetic union failure")

    monkeypatch.setattr(country_h3, "unary_union", fail_union)
    with pytest.raises(RuntimeError, match="Failed to merge all 2 components for XX"):
        country_h3._merge_country_components({
            "XX": [box(0, 0, 1, 1), box(2, 2, 3, 3)],
        })


def test_main_fails_closed_when_release_input_cannot_load(monkeypatch, tmp_path):
    def fail_load(_path, _countries):
        raise RuntimeError("synthetic source failure")

    monkeypatch.setattr(country_h3, "load_country_geoms", fail_load)
    expected = tmp_path / "expected.json"
    expected.write_text('["US"]')
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_country_h3_index.py",
            "--parquet",
            "missing.parquet",
            "--expected-country-count",
            "1",
            "--expected-country-codes",
            str(expected),
            "--overture-release",
            "2026-06-17.0",
            "--output",
            str(tmp_path / "router.json"),
        ],
    )
    with pytest.raises(RuntimeError, match="synthetic source failure"):
        country_h3.main()
    assert not (tmp_path / "router.json").exists()


def test_demo_fixtures_require_explicit_flag_and_write_manifest(monkeypatch, tmp_path):
    output = tmp_path / "router.json"
    manifest = tmp_path / "router-manifest.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_country_h3_index.py",
            "--demo-fixtures",
            "--resolution",
            "0",
            "--output",
            str(output),
            "--manifest",
            str(manifest),
        ],
    )
    country_h3.main()
    metadata = __import__("json").loads(manifest.read_text())
    assert output.exists()
    assert metadata["source"]["mode"] == "demo-fixtures"
    assert metadata["completeness"]["country_count"] == 4
    assert metadata["completeness"]["decoded_component_count"] == 4
    assert metadata["artifacts"][0]["sha256"]
    assert metadata["artifacts"][0]["size_bytes"] == output.stat().st_size


def test_release_build_requires_expected_country_manifest(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_country_h3_index.py",
            "--parquet",
            str(tmp_path / "input.parquet"),
            "--overture-release",
            "2026-06-17.0",
        ],
    )
    with pytest.raises(ValueError, match="expected-country-count"):
        country_h3.main()


def test_s3_release_provenance_matches_standard_path():
    country_h3._validate_release_provenance(
        "s3://bucket/release/2026-06-17.0/theme=divisions/type=division_area/*",
        "2026-06-17.0",
    )


def test_s3_release_provenance_rejects_mismatch():
    with pytest.raises(ValueError, match="does not match"):
        country_h3._validate_release_provenance(
            "s3://bucket/release/2026-06-17.0/theme=divisions/type=division_area/*",
            "2026-07-01.0",
        )
