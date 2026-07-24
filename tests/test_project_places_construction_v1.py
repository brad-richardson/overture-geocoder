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
                "id": "00000000-0000-0000-0000-000000000001",
                "names": {"primary": "Cafe", "common": [("ja", "東京")]},
                "brand": {"names": {"primary": "Brand"}},
                "categories": {"primary": "restaurant"},
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
                "categories": {"primary": None},
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
        "operating_status",
        "geometry",
        "source_object_index",
        "source_row_group",
        "source_row_index",
    ]
    assert batch["common_names"].to_pylist() == [["東京"], []]
    assert batch["category"].to_pylist() == ["restaurant", "library"]
    assert batch["locality"].to_pylist() == ["Town", ""]
    assert batch["source_object_index"].to_pylist() == [3, 3]
    assert batch["source_row_group"].to_pylist() == [4, 4]
    assert batch["source_row_index"].to_pylist() == [5, 6]
    assert batch.schema.field("source_row_index").type == pa.int32()
