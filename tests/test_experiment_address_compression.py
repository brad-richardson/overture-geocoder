from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
REDUCE_SPEC = importlib.util.spec_from_file_location("experiment_address_reduce", SCRIPTS / "experiment_address_reduce.py")
assert REDUCE_SPEC and REDUCE_SPEC.loader
reduce = importlib.util.module_from_spec(REDUCE_SPEC)
sys.modules["experiment_address_reduce"] = reduce
REDUCE_SPEC.loader.exec_module(reduce)
COMPRESSION_SPEC = importlib.util.spec_from_file_location("experiment_address_compression", SCRIPTS / "experiment_address_compression.py")
assert COMPRESSION_SPEC and COMPRESSION_SPEC.loader
compression = importlib.util.module_from_spec(COMPRESSION_SPEC)
COMPRESSION_SPEC.loader.exec_module(compression)


def record(index: int, *, number: str = "10"):
    feature_id = str(uuid.UUID(int=index))
    return {
        "key": ("us", "ma", "stoneham", "stoneham", "02180", "main street", number, "", feature_id),
        "id": feature_id, "lon": -71.0999, "lat": 42.4801,
        "source_row_group": 12, "source_row_index": index,
        "country": "US", "postal_city": "Stoneham", "postcode": "02180",
        "street": "Main Street", "number": number, "unit": "",
        "address_levels": ["MA", "Middlesex", "Stoneham"],
    }


def write_baseline(path: Path):
    records = [record(1), record(2), record(3, number="11")]
    payloads = [reduce.encode_record({**item, "address_levels": item["address_levels"]}) for item in records]
    sparse = reduce.encode_uvarint(0) + reduce.encode_uvarint(0)
    key = reduce.key_prefix_payload(records[0]["key"])
    sparse += reduce.encode_uvarint(len(key)) + key
    record_payload = b"".join(len(item).to_bytes(4, "little") + item for item in payloads)
    header = {
        "format": 1, "records": len(records), "distinct_lookup_keys": 2,
        "sparse_stride": 256, "sparse_bytes": len(sparse), "record_bytes": len(record_payload),
        "source": {}, "fragment_sha256": ["a" * 64], "fields": [],
    }
    with path.open("wb") as output:
        reduce.write_envelope(output, reduce.ARTIFACT_MAGIC, header)
        output.write(sparse)
        output.write(record_payload)


def test_page_formats_round_trip_fidelity_and_tradeoffs():
    records = [record(1), record(2), record(3, number="11")]
    for useful in (False, True):
        payload = compression.encode_page(records, useful=useful)
        decoded = compression.decode_page(payload, useful=useful)
        assert [item["key"] for item in decoded] == [item["key"] for item in records]
        assert [item["id"] for item in decoded] == [item["id"] for item in records]
        if useful:
            assert [item["address_levels"] for item in decoded] == [item["address_levels"] for item in records]
            assert [item["street"] for item in decoded] == [item["street"] for item in records]
        else:
            assert "address_levels" not in decoded[0]


def test_full_experiment_preserves_oracle_and_measures_every_variant(tmp_path):
    baseline = tmp_path / "baseline.aidx"
    write_baseline(baseline)
    report = compression.run(baseline, tmp_path / "variants", page_rows=2, planning_rows=1000)

    assert report["input"]["records"] == 3
    assert report["oracle"]["distinct_lookup_keys"] == 2
    assert report["oracle"]["maximum_candidate_fanout"] == 2
    assert set(report["variants"]) == set(compression.VARIANTS)
    assert all(item["full_decode_digest_match"] for item in report["variants"].values())
    assert "drops display casing" in report["variants"]["bare"]["accuracy"]
    assert "lossless" in report["variants"]["useful_gzip"]["accuracy"]
