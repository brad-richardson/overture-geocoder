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

