from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


pa = pytest.importorskip("pyarrow")
ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "project_places_construction_v1_test",
    ROOT / "scripts/project_places_construction_v1.py",
)
assert SPEC and SPEC.loader
projection = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = projection
SPEC.loader.exec_module(projection)


def test_flatten_batch_is_columnar_and_preserves_nested_values():
    schema = pa.schema(
        [
            ("id", pa.string()),
            (
                "names",
                pa.struct(
                    [
                        ("primary", pa.string()),
                        ("common", pa.map_(pa.string(), pa.string())),
                    ]
                ),
            ),
            (
                "brand",
                pa.struct([("names", pa.struct([("primary", pa.string())]))]),
            ),
            (
                "categories",
                pa.struct(
                    [
                        ("primary", pa.string()),
                        ("alternate", pa.list_(pa.string())),
                    ]
                ),
            ),
            ("basic_category", pa.string()),
            (
                "addresses",
                pa.list_(
                    pa.struct(
                        [
                            ("locality", pa.string()),
                            ("region", pa.string()),
                            ("country", pa.string()),
                        ]
                    )
                ),
            ),
            ("confidence", pa.float64()),
            ("operating_status", pa.string()),
            ("geometry", pa.binary()),
        ]
    )
    table = pa.Table.from_pylist(
        [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "names": {"primary": "Cafe", "common": [("ja", "東京")]},
                "brand": {"names": {"primary": "Brand"}},
                "categories": {"primary": "restaurant", "alternate": ["cafe"]},
                "basic_category": "food",
                "addresses": [
                    {"locality": "Town", "region": "Region", "country": "XX"}
                ],
                "confidence": 0.75,
                "operating_status": "open",
                "geometry": b"point",
            },
            {
                "id": "00000000-0000-0000-0000-000000000002",
                "names": {"primary": "Library", "common": []},
                "brand": None,
                "categories": {"primary": None, "alternate": None},
                "basic_category": "library",
                "addresses": [],
                "confidence": 1.0,
                "operating_status": None,
                "geometry": b"point2",
            },
        ],
        schema=schema,
    )
    batch = projection.flatten_batch(
        table.to_batches()[0], object_index=3, row_group=4, row_offset=5
    )
    assert batch.schema.names == [
        "id",
        "primary_name",
        "common_names",
        "brand_name",
        "category",
        "locality",
        "region",
        "country",
        "confidence",
        "prominence_rank",
        "operating_status",
        "geometry",
        "source_object_index",
        "source_row_group",
        "source_row_index",
    ]
    assert batch["common_names"].to_pylist() == [["東京"], []]
    # A restaurant is a commodity type and scores zero however its alternates
    # read; `library` is a real, weak prominence signal. See
    # scripts/places_type_prior_v1.py.
    assert batch["prominence_rank"].to_pylist() == [0, round(0.40 * 255)]
    assert batch.schema.field("prominence_rank").type == pa.uint8()
    assert batch["category"].to_pylist() == ["restaurant", "library"]
    assert batch["locality"].to_pylist() == ["Town", ""]
    assert batch["source_object_index"].to_pylist() == [3, 3]
    assert batch["source_row_group"].to_pylist() == [4, 4]
    assert batch["source_row_index"].to_pylist() == [5, 6]
    assert batch.schema.field("source_row_index").type == pa.int32()


def test_prominence_degrades_to_primary_only_when_alternate_is_absent():
    """A pre-v3 standalone projection shape still degrades safely.

    The current strict legacy inventory requires `categories.alternate`, but
    the lower-level projector remains tolerant for already-materialized and
    hand-authored legacy inputs.
    """
    schema = pa.schema(
        [
            ("id", pa.string()),
            (
                "names",
                pa.struct(
                    [
                        ("primary", pa.string()),
                        ("common", pa.map_(pa.string(), pa.string())),
                    ]
                ),
            ),
            (
                "brand",
                pa.struct([("names", pa.struct([("primary", pa.string())]))]),
            ),
            # No `alternate` field at all -- the pre-change source shape.
            ("categories", pa.struct([("primary", pa.string())])),
            ("basic_category", pa.string()),
            (
                "addresses",
                pa.list_(
                    pa.struct(
                        [
                            ("locality", pa.string()),
                            ("region", pa.string()),
                            ("country", pa.string()),
                        ]
                    )
                ),
            ),
            ("confidence", pa.float64()),
            ("operating_status", pa.string()),
            ("geometry", pa.binary()),
        ]
    )
    table = pa.Table.from_pylist(
        [
            {
                "id": "00000000-0000-0000-0000-000000000003",
                "names": {"primary": "Monument", "common": []},
                "brand": None,
                "categories": {"primary": "monument"},
                "basic_category": "monument",
                "addresses": [],
                "confidence": 0.5,
                "operating_status": "open",
                "geometry": b"point3",
            },
            {
                "id": "00000000-0000-0000-0000-000000000004",
                "names": {"primary": "Cafe", "common": []},
                "brand": None,
                "categories": {"primary": "coffee_shop"},
                "basic_category": "coffee_shop",
                "addresses": [],
                "confidence": 1.0,
                "operating_status": "open",
                "geometry": b"point4",
            },
        ],
        schema=schema,
    )
    batch = projection.flatten_batch(
        table.to_batches()[0], object_index=0, row_group=0, row_offset=0
    )
    # Projects without raising, and the primary category still separates them.
    assert batch["prominence_rank"].to_pylist() == [255, 0]


def test_taxonomy_projection_separates_display_from_search_hierarchy():
    schema = pa.schema(
        [
            ("id", pa.string()),
            (
                "names",
                pa.struct(
                    [
                        ("primary", pa.string()),
                        ("common", pa.map_(pa.string(), pa.string())),
                    ]
                ),
            ),
            (
                "brand",
                pa.struct([("names", pa.struct([("primary", pa.string())]))]),
            ),
            (
                "taxonomy",
                pa.struct(
                    [
                        ("primary", pa.string()),
                        ("hierarchy", pa.list_(pa.string())),
                        ("alternates", pa.list_(pa.string())),
                    ]
                ),
            ),
            ("basic_category", pa.string()),
            (
                "addresses",
                pa.list_(
                    pa.struct(
                        [
                            ("locality", pa.string()),
                            ("region", pa.string()),
                            ("country", pa.string()),
                        ]
                    )
                ),
            ),
            ("confidence", pa.float64()),
            ("operating_status", pa.string()),
            ("geometry", pa.binary()),
        ]
    )
    table = pa.Table.from_pylist(
        [
            {
                "id": "00000000-0000-0000-0000-000000000005",
                "names": {"primary": "Golden Dragon", "common": []},
                "brand": None,
                "taxonomy": {
                    "primary": "cantonese_restaurant",
                    "hierarchy": [
                        "food_and_drink",
                        "restaurant",
                        "asian_restaurant",
                        "cantonese_restaurant",
                    ],
                    "alternates": ["takeout_restaurant"],
                },
                "basic_category": "restaurant",
                "addresses": [],
                "confidence": 1.0,
                "operating_status": "open",
                "geometry": b"point5",
            },
            {
                "id": "00000000-0000-0000-0000-000000000006",
                "names": {"primary": "City Collection", "common": []},
                "brand": None,
                "taxonomy": {
                    "primary": "specialty_museum",
                    "hierarchy": [
                        "arts_and_entertainment",
                        "museum",
                        "specialty_museum",
                    ],
                    "alternates": [],
                },
                "basic_category": "museum",
                "addresses": [],
                "confidence": 0.5,
                "operating_status": "open",
                "geometry": b"point6",
            },
        ],
        schema=schema,
    )
    batch = projection.flatten_batch(
        table.to_batches()[0], object_index=1, row_group=2, row_offset=3
    )
    assert batch["category"].to_pylist() == [
        "cantonese_restaurant",
        "specialty_museum",
    ]
    assert batch["category_terms"].to_pylist() == [
        (
            "cantonese_restaurant restaurant food_and_drink "
            "asian_restaurant takeout_restaurant"
        ),
        "specialty_museum museum arts_and_entertainment",
    ]
    assert batch["prominence_rank"].to_pylist() == [0, round(0.85 * 255)]
