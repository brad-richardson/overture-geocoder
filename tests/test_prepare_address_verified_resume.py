from __future__ import annotations

import importlib.util
import json
import struct
import sys
import uuid
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reduce = load("address_reduce_for_verified_resume", ROOT / "scripts" / "experiment_address_reduce.py")
resume = load(
    "prepare_address_verified_resume",
    ROOT / "scripts" / "prepare_address_verified_resume.py",
)


def record(feature_id: str, number: str) -> dict:
    return {
        "id": feature_id,
        "street": "Main Street",
        "number": number,
        "unit": "",
        "postcode": "02180",
        "postal_city": "Stoneham",
        "country": "US",
        "address_levels": ["MA", "Middlesex", "Stoneham"],
        "lon": -71.1,
        "lat": 42.48,
        "source_object_index": 0,
        "source_row_group": 0,
        "source_row_index": int(number),
    }


def write_fragment(path: Path, source_digest: str, index: int) -> dict:
    rows = [record(str(uuid.UUID(int=index + 1)), str(index + 1))]
    header = {
        "format": reduce.FORMAT_VERSION,
        "source_inventory_sha256": source_digest,
        "records": len(rows),
        "fragment_index": index,
        "partition_id": reduce.SPIKE_PARTITION_ID,
        "sorted_by": "country/general/specific/postal_city/postcode/street/number/unit/id",
    }
    with path.open("wb") as output:
        reduce.write_envelope(output, reduce.FRAGMENT_MAGIC, header)
        for item in rows:
            payload = reduce.encode_record(item)
            output.write(struct.pack("<I", len(payload)))
            output.write(payload)
    return {
        "index": index,
        "partition_id": reduce.SPIKE_PARTITION_ID,
        "path": str(path),
        "bytes": path.stat().st_size,
        "records": len(rows),
        "sha256": reduce.sha256_file(path),
    }


def fixture(tmp_path: Path) -> tuple[Path, list[dict]]:
    source_digest = "a" * 64
    fragments = [
        write_fragment(tmp_path / f"fragment-{index:04d}.bin", source_digest, index)
        for index in range(2)
    ]
    report = {
        "schema": resume.MAP_SCHEMA,
        "map_fragments": {
            "source": {
                "source_inventory_sha256": source_digest,
                "source_uri": "s3://example/addresses.parquet",
                "source_etag": "etag",
                "release": "2026-06-17.0",
                "family": "addresses",
            },
            "fragments": fragments,
        },
    }
    path = tmp_path / "map.json"
    path.write_text(json.dumps(report))
    return path, fragments


def reduce_args(tmp_path: Path, map_report: Path, name: str, **overrides) -> Namespace:
    values = {
        "map_report": map_report,
        "restore_report": None,
        "expected_report": None,
        "output": tmp_path / f"{name}.aidx",
        "json_out": tmp_path / f"{name}.json",
        "work_dir": tmp_path,
        "sparse_stride": 2,
        "max_artifact_bytes": 1_000_000,
        "max_workspace_bytes": 2_000_000,
    }
    values.update(overrides)
    return Namespace(**values)


def test_restored_real_fragments_reduce_to_exact_local_oracle(tmp_path):
    map_report, fragments = fixture(tmp_path)
    local_args = reduce_args(tmp_path, map_report, "local")
    local = resume.reduce_fragments(local_args)
    restore_report = tmp_path / "restore.json"
    restore_report.write_text(
        json.dumps(
            {
                "schema": resume.STORE_SCHEMA,
                "artifacts": [
                    {**fragment, "verified": True, "status": "remote_verified"}
                    for fragment in fragments
                ],
            }
        )
    )
    remote_args = reduce_args(
        tmp_path,
        map_report,
        "remote",
        restore_report=restore_report,
        expected_report=local_args.json_out,
    )
    remote = resume.reduce_fragments(remote_args)

    assert remote["restored"] is True
    assert remote["local_oracle_match"] is True
    assert remote["reduce"]["sha256"] == local["reduce"]["sha256"]
    assert remote["reduce"]["verification"]["full_sorted_scan"] is True


def test_restore_report_rejects_identity_mismatch(tmp_path):
    map_report_path, fragments = fixture(tmp_path)
    map_report = json.loads(map_report_path.read_text())
    restore = tmp_path / "restore.json"
    restore.write_text(
        json.dumps(
            {
                "schema": resume.STORE_SCHEMA,
                "artifacts": [
                    {
                        **fragment,
                        "sha256": "0" * 64,
                        "verified": True,
                    }
                    for fragment in fragments
                ],
            }
        )
    )

    try:
        resume.restored_paths(map_report, restore)
    except ValueError as error:
        assert "identity differs" in str(error)
    else:
        raise AssertionError("a mismatched restored fragment was accepted")
