from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reduce = _load("experiment_address_reduce")
compression = _load("experiment_address_compression")
partition = _load("address_partition")
shard = _load("build_address_shard")


def _record(index: int, *, number: str = "10"):
    return {
        "id": str(uuid.UUID(int=index)),
        "lon": -71.0999,
        "lat": 42.4801,
        "source_object_index": 3,
        "source_row_group": 12,
        "source_row_index": index,
        "country": "US",
        "postal_city": "Stoneham",
        "postcode": "02180",
        "street": "Main Street",
        "number": number,
        "unit": "",
        "address_levels": ["MA", "Middlesex", "Stoneham"],
    }


def _write_reduce(
    path: Path,
    records: list[dict],
    *,
    release: str = "2026-06-17.0",
    family: str = "addresses",
) -> None:
    records = sorted(records, key=reduce.record_key)
    payloads = [reduce.encode_record(item) for item in records]
    first_key = reduce.record_key(records[0])
    key = reduce.key_prefix_payload(first_key)
    sparse = (
        reduce.encode_uvarint(0)
        + reduce.encode_uvarint(0)
        + reduce.encode_uvarint(len(key))
        + key
    )
    record_payload = b"".join(
        len(item).to_bytes(4, "little") + item for item in payloads
    )
    header = {
        "format": reduce.FORMAT_VERSION,
        "records": len(records),
        "distinct_lookup_keys": len({reduce.record_key(item)[:8] for item in records}),
        "sparse_stride": 256,
        "sparse_bytes": len(sparse),
        "record_bytes": len(record_payload),
        "source": {"release": release, "family": family},
        "fragment_sha256": ["a" * 64],
        "fields": [],
    }
    with path.open("wb") as output:
        reduce.write_envelope(output, reduce.ARTIFACT_MAGIC, header)
        output.write(sparse)
        output.write(record_payload)


def _plan(rows: int) -> dict:
    return partition.build_plan(
        {
            "schema": partition.COUNT_SCHEMA,
            "overture_release": "2026-06-17.0",
            "counts": [{"country": "us", "bucket": 0, "rows": rows}],
        },
        maximum_hash_bits=2,
        row_cap=100,
    )


def test_builds_only_worker_readable_page_pair(tmp_path):
    records = [_record(1), _record(2), _record(3, number="11")]
    source = tmp_path / "source.aidx"
    _write_reduce(source, records)
    report = shard.build_shard(
        source,
        tmp_path / "out",
        _plan(len(records)),
        identifier="a-us",
        page_rows=2,
    )
    assert report["verification"] == {
        "rows": 3,
        "sorted": True,
        "partition_membership": True,
    }
    assert report["page_format"]["variant"] == "useful_gzip"
    index = tmp_path / "out" / "a-us.aidx"
    data = tmp_path / "out" / "a-us.adat"
    assert index.is_file() and data.is_file()
    assert not (tmp_path / "out" / "bare.bin").exists()
    key = reduce.record_key(records[0])[:8]
    candidates = compression.indexed_lookup(
        data,
        index,
        key,
        useful=True,
        compressed=True,
        max_index_bytes=1_000_000,
        max_page_bytes=1_000_000,
    )
    assert [item["id"] for item in candidates] == [records[0]["id"], records[1]["id"]]


def test_rejects_reduce_count_that_differs_from_plan(tmp_path):
    source = tmp_path / "source.aidx"
    _write_reduce(source, [_record(1)])
    with pytest.raises(ValueError, match="rows differ"):
        shard.build_shard(
            source,
            tmp_path / "out",
            _plan(2),
            identifier="a-us",
        )


@pytest.mark.parametrize(
    ("release", "family"),
    [("2026-05-20.0", "addresses"), ("2026-06-17.0", "places")],
)
def test_rejects_reduce_artifact_from_another_source(tmp_path, release, family):
    source = tmp_path / "source.aidx"
    _write_reduce(source, [_record(1)], release=release, family=family)
    with pytest.raises(ValueError, match="source differs"):
        shard.build_shard(
            source,
            tmp_path / "out",
            _plan(1),
            identifier="a-us",
        )


def test_refuses_to_overwrite_either_member_of_a_serving_pair(tmp_path):
    source = tmp_path / "source.aidx"
    _write_reduce(source, [_record(1)])
    output = tmp_path / "out"
    output.mkdir()
    stale = output / "a-us.adat"
    stale.write_bytes(b"stale")
    with pytest.raises(ValueError, match="create-only"):
        shard.build_shard(source, output, _plan(1), identifier="a-us")
    assert stale.read_bytes() == b"stale"
    assert not (output / "a-us.aidx").exists()
