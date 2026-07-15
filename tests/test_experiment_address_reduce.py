from __future__ import annotations

import importlib.util
import struct
import uuid
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "experiment_address_reduce.py"
SPEC = importlib.util.spec_from_file_location("experiment_address_reduce", SCRIPT)
assert SPEC and SPEC.loader
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


def sample_record(*, feature_id: str, number: str = "10", unit: str = ""):
    return {
        "id": feature_id,
        "street": " Main\tStreet ",
        "number": number,
        "unit": unit,
        "postcode": "02180",
        "postal_city": "Stoneham",
        "country": "US",
        "address_levels": ["MA", "Middlesex", "Stoneham"],
        "lon": -71.0999,
        "lat": 42.4801,
        "source_row_group": 12,
        "source_row_index": 345,
    }


def test_record_round_trip_keeps_raw_levels_and_source_locator():
    record = sample_record(feature_id=str(uuid.UUID(int=1)), unit="A")
    decoded = experiment.decode_record(experiment.encode_record(record))

    assert decoded["key"][:8] == (
        "us",
        "ma",
        "stoneham",
        "stoneham",
        "02180",
        "main street",
        "10",
        "a",
    )
    assert decoded["address_levels"] == record["address_levels"]
    assert decoded["source_row_group"] == 12
    assert decoded["source_row_index"] == 345
    assert abs(decoded["lon"] - record["lon"]) < 0.0000001


def test_point_decoder_accepts_little_and_big_endian_wkb():
    little = b"\x01" + struct.pack("<Idd", 1, -71.0, 42.0)
    big = b"\x00" + struct.pack(">Idd", 1, -71.0, 42.0)
    assert experiment.point_coordinates(little) == (-71.0, 42.0)
    assert experiment.point_coordinates(big) == (-71.0, 42.0)
    assert experiment.point_coordinates(b"bad") is None


def write_fragment(path: Path, records: list[dict], source_digest: str, index: int):
    encoded = sorted(
        ((experiment.record_key(record), experiment.encode_record(record)) for record in records),
        key=lambda item: item[0],
    )
    with path.open("wb") as output:
        experiment.write_envelope(
            output,
            experiment.FRAGMENT_MAGIC,
            {
                "format": 1,
                "source_inventory_sha256": source_digest,
                "records": len(encoded),
                "fragment_index": index,
                "sorted_by": "test",
            },
        )
        for _, payload in encoded:
            output.write(struct.pack("<I", len(payload)))
            output.write(payload)
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "records": len(encoded),
        "sha256": experiment.sha256_file(path),
    }


def test_streaming_reduce_merges_fragments_and_exact_lookup_crosses_sparse_blocks(tmp_path):
    digest = "a" * 64
    duplicate_ids = [str(uuid.UUID(int=index)) for index in range(1, 6)]
    fragments = [
        write_fragment(
            tmp_path / "a.bin",
            [sample_record(feature_id=duplicate_ids[0]), sample_record(feature_id=duplicate_ids[2])],
            digest,
            0,
        ),
        write_fragment(
            tmp_path / "b.bin",
            [
                sample_record(feature_id=duplicate_ids[1]),
                sample_record(feature_id=duplicate_ids[3]),
                sample_record(feature_id=duplicate_ids[4]),
                sample_record(feature_id=str(uuid.UUID(int=10)), number="11"),
            ],
            digest,
            1,
        ),
    ]
    output = tmp_path / "address.aidx"
    source = {
        "source_inventory_sha256": digest,
        "source_uri": "s3://example/source.parquet",
        "source_etag": "etag",
        "release": "2026-06-17.0",
        "family": "addresses",
    }

    report = experiment.build_artifact(
        fragments,
        output,
        source=source,
        sparse_stride=2,
        max_artifact_bytes=1_000_000,
        max_workspace_bytes=2_000_000,
        input_bytes=0,
    )

    with experiment.AddressReduceArtifact(output) as artifact:
        results = artifact.lookup(
            ("US", "MA", "Stoneham", "Stoneham", "02180", "Main Street", "10", "")
        )
        verification = artifact.verify(report["verification_groups"])

    assert {item["id"] for item in results} == set(duplicate_ids)
    assert report["rows"] == 6
    assert report["maximum_candidate_fanout"] == 5
    assert verification == {
        "full_sorted_scan": True,
        "record_count_match": True,
        "exact_candidate_sets": 2,
    }


def test_fragment_reader_fails_on_wrong_source_inventory(tmp_path):
    fragment = write_fragment(
        tmp_path / "fragment.bin",
        [sample_record(feature_id=str(uuid.UUID(int=1)))],
        "b" * 64,
        0,
    )
    source = {
        "source_inventory_sha256": "a" * 64,
        "source_uri": "s3://example/source.parquet",
        "source_etag": "etag",
        "release": "2026-06-17.0",
        "family": "addresses",
    }
    try:
        experiment.build_artifact(
            [fragment],
            tmp_path / "out.bin",
            source=source,
            sparse_stride=4,
            max_artifact_bytes=1_000_000,
            max_workspace_bytes=2_000_000,
            input_bytes=0,
        )
    except ValueError as error:
        assert "inventories differ" in str(error)
    else:
        raise AssertionError("source mismatch was accepted")
