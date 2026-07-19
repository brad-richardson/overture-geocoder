from __future__ import annotations

import importlib.util
import sys
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


partition = _load("address_partition")
collection = _load("build_address_collection")


def _plan():
    counts = {
        "schema": partition.COUNT_SCHEMA,
        "overture_release": "2026-06-17.0",
        "counts": [
            {"country": "ca", "bucket": 0, "rows": 10},
            {"country": "us", "bucket": 0, "rows": 60},
            {"country": "us", "bucket": 1, "rows": 40},
        ],
    }
    return partition.build_plan(counts, maximum_hash_bits=2, row_cap=75)


def _artifacts(tmp_path: Path, plan: dict):
    result = {}
    for item in plan["partitions"]:
        if not item["rows"]:
            continue
        index = tmp_path / f"{item['id']}.idx"
        data = tmp_path / f"{item['id']}.bin"
        index.write_bytes(f"index:{item['id']}".encode())
        data.write_bytes(f"data:{item['id']}".encode())
        result[item["id"]] = (index, data)
    return result


def test_builds_complete_collection_with_explicit_empty_ranges(tmp_path):
    plan = _plan()
    result = collection.build_collection(plan, _artifacts(tmp_path, plan))
    assert result["schema_version"] == 2
    assert result["coverage"] == [-180.0, -90.0, 180.0, 90.0]
    assert result["partition"]["scheme"] == partition.PARTITION_SCHEME
    assert set(result["items"]) == {
        item["id"] for item in plan["partitions"] if item["rows"]
    }
    assert {item["id"] for item in result["empty_ranges"]} == {
        item["id"] for item in plan["partitions"] if not item["rows"]
    }
    for identity, item in result["items"].items():
        assert item["index_href"] == f"families/addresses/shards/{identity}.aidx"
        assert item["data_href"] == f"families/addresses/shards/{identity}.adat"
        assert len(item["index_sha256"]) == 64
        assert len(item["data_sha256"]) == 64


def test_rejects_missing_or_extra_artifacts(tmp_path):
    plan = _plan()
    artifacts = _artifacts(tmp_path, plan)
    artifacts.pop(next(iter(artifacts)))
    with pytest.raises(ValueError, match="differ from non-empty"):
        collection.build_collection(plan, artifacts)


def test_rejects_plan_gap_or_noncanonical_leaf(tmp_path):
    plan = _plan()
    plan["partitions"][0]["hash_start"] = 1
    with pytest.raises(ValueError, match="inconsistent"):
        collection.build_collection(plan, {})


def test_artifact_argument_is_strict_and_unique():
    with pytest.raises(ValueError, match="must be"):
        collection.parse_artifacts(["bad"])
    with pytest.raises(ValueError, match="duplicate"):
        collection.parse_artifacts(["a-us=x=y", "a-us=x=z"])
