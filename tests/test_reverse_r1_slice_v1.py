"""Contract tests for `scripts/reverse_r1_slice_v1.py` (reverse R1-c).

The driver's real subject is a completed slice work tree; these tests pin its
CONTRACT on synthetic packs instead, where every failure mode can be staged
deliberately:

* a pack without its per-cell directory fails closed;
* a directory that names a cell the data does not carry fails closed;
* the slice-completeness invariant (shard records == artifact records ==
  transform admitted rows) fails closed when the marker lies;
* a successful run writes a summary whose keys are pinned, so the CI smoke's
  `jq -e` assertions cannot be silently vacuous.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import random
import subprocess
import sys
from pathlib import Path

import pytest


pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pytest.importorskip("duckdb")

ROOT = Path(__file__).parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


DRIVER = _load("reverse_r1_slice_driver", "scripts/reverse_r1_slice_v1.py")
REVERSE = _load("reverse_r1_slice_shard", "scripts/reverse_shard_v1.py")


@pytest.fixture(scope="module")
def binaries() -> dict[str, Path]:
    subprocess.run(
        [
            "cargo",
            "build",
            "-p",
            "geocoder-construction",
            "--bin",
            "reverse-encode-v1",
            "--bin",
            "reverse-verify-v1",
        ],
        cwd=ROOT / "crates",
        check=True,
    )
    target = ROOT / "crates/target/debug"
    return {
        "encode": target / "reverse-encode-v1",
        "verify": target / "reverse-verify-v1",
    }


def places_rows(count: int, cell: str, seed: int = 3) -> list[dict]:
    generator = random.Random(seed)
    y, x = REVERSE.cell_yx(cell)
    rows = []
    for index in range(count):
        longitude_e7 = (
            x * REVERSE.LONGITUDE_E7_PER_CELL
            - REVERSE.LONGITUDE_E7_ORIGIN
            + generator.randrange(REVERSE.LONGITUDE_E7_PER_CELL)
        )
        latitude_e7 = (
            y * REVERSE.LATITUDE_E7_PER_CELL
            - REVERSE.LATITUDE_E7_ORIGIN
            + generator.randrange(REVERSE.LATITUDE_E7_PER_CELL)
        )
        rows.append(
            {
                "feature_id": index.to_bytes(16, "big"),
                "partition_cell": cell,
                "longitude": longitude_e7 / 1e7,
                "latitude": latitude_e7 / 1e7,
                "primary_name": f"Place {index}",
                "brand_name": "",
                "category": "eat_and_drink",
                "locality": "Monaco",
                "region": "",
                "country": "MC",
                "confidence_rank": index % 256,
                "source_object_index": 9,
                "source_row_group": 66,
                "source_row_index": index,
            }
        )
    return rows


PLACES_PACK_SCHEMA = pa.schema(
    [
        ("feature_id", pa.binary()),
        ("partition_cell", pa.string()),
        ("longitude", pa.float64()),
        ("latitude", pa.float64()),
        ("primary_name", pa.string()),
        ("brand_name", pa.string()),
        ("category", pa.string()),
        ("locality", pa.string()),
        ("region", pa.string()),
        ("country", pa.string()),
        ("confidence_rank", pa.uint8()),
        ("source_object_index", pa.uint32()),
        ("source_row_group", pa.uint32()),
        ("source_row_index", pa.uint64()),
    ]
)


def build_work_tree(
    root: Path, rows_by_cell: dict[str, list[dict]]
) -> tuple[Path, dict]:
    """A minimal completed-slice work tree: one positions pack, one marker.

    The directory is derived honestly from `rows_by_cell`; tests that need a
    lying marker mutate the returned marker document and rewrite it.
    """
    work = root / "work"
    store = work / "store-map"
    markers = work / "markers"
    markers.mkdir(parents=True)
    rows = [row for cell in sorted(rows_by_cell) for row in rows_by_cell[cell]]
    pack_path = root / "pack.parquet"
    pq.write_table(
        pa.Table.from_pylist(rows, schema=PLACES_PACK_SCHEMA),
        pack_path,
        row_group_size=8,
    )
    digest = hashlib.sha256(pack_path.read_bytes()).hexdigest()
    key = f"map/places-v1/positions/sha256/{digest}.parquet"
    target = store / key
    target.parent.mkdir(parents=True)
    target.write_bytes(pack_path.read_bytes())
    directory = {
        "schema": "overture-places-map-positions-directory-v1",
        "shuffle_bucket": 0,
        "records": len(rows),
        "row_groups": [],
        "cells": [
            {"partition_cell": cell, "records": len(rows_by_cell[cell])}
            for cell in sorted(rows_by_cell)
        ],
    }
    marker = {
        "task_id": "places-map-000",
        "transform": {"admitted_features": len(rows)},
        "positions": {
            "schema": "overture-places-map-positions-v1",
            "records": len(rows),
            "packs": [
                {
                    "pack_id": 0,
                    "shuffle_bucket": 0,
                    "records": len(rows),
                    "object": {
                        "key": key,
                        "bytes": pack_path.stat().st_size,
                        "sha256": digest,
                    },
                    "directory": directory,
                }
            ],
        },
    }
    (markers / "places-map-000.json").write_text(json.dumps(marker))
    return work, marker


def write_marker(work: Path, marker: dict) -> None:
    (work / "markers" / "places-map-000.json").write_text(json.dumps(marker))


def run_driver(work: Path, binaries: dict[str, Path], output: Path) -> dict:
    DRIVER.main(
        [
            "--work",
            str(work),
            "--family",
            "places",
            "--output",
            str(output),
            "--encode-binary",
            str(binaries["encode"]),
            "--verify-binary",
            str(binaries["verify"]),
        ]
    )
    return json.loads((output / "summary.json").read_text())


def test_summary_keys_are_pinned(binaries, tmp_path):
    """The CI smoke asserts on these keys with `jq -e`; a renamed or dropped
    key must fail HERE rather than turn a workflow assertion vacuous."""
    rows_by_cell = {"be85": places_rows(20, "be85"), "bf85": places_rows(7, "bf85")}
    work, _ = build_work_tree(tmp_path, rows_by_cell)
    summary = run_driver(work, binaries, tmp_path / "out")
    assert set(summary) == {
        "schema",
        "family",
        "cells",
        "shards",
        "records",
        "artifact_records",
        "admitted_rows",
        "bytes",
        "payload_bytes",
        "mean_record_bytes",
        "model_record_bytes",
        "per_cell",
    }
    assert summary["schema"] == "overture-reverse-r1-slice-summary-v1"
    assert summary["family"] == "places"
    assert summary["cells"] == ["be85", "bf85"]
    assert summary["shards"] == 2
    assert summary["records"] == summary["artifact_records"] == 27
    assert summary["admitted_rows"] == 27
    assert summary["model_record_bytes"] == 112
    assert summary["mean_record_bytes"] > 0
    for cell, entry in summary["per_cell"].items():
        assert set(entry) == {
            "records",
            "sub_cell_level",
            "leaves",
            "shard_bytes",
            "payload_bytes",
        }
        # 27 records is far under the density target, so every ceiling gives 0.
        assert entry["sub_cell_level"] == 0
        assert entry["records"] == len(rows_by_cell[cell])
    plrx = sorted(path.name for path in (tmp_path / "out").glob("*.plrx"))
    assert plrx == ["places-be85.plrx", "places-bf85.plrx"]


def test_missing_directory_fails_closed(binaries, tmp_path):
    work, marker = build_work_tree(tmp_path, {"be85": places_rows(4, "be85")})
    broken = copy.deepcopy(marker)
    del broken["positions"]["packs"][0]["directory"]
    write_marker(work, broken)
    with pytest.raises(SystemExit, match="no per-cell directory"):
        run_driver(work, binaries, tmp_path / "out")


def test_cell_mismatch_fails_closed(binaries, tmp_path):
    """A directory that promises a cell the pack does not carry: the per-cell
    SELECT comes back short and the driver must not encode a partial shard."""
    work, marker = build_work_tree(tmp_path, {"be85": places_rows(4, "be85")})
    lying = copy.deepcopy(marker)
    lying["positions"]["packs"][0]["directory"]["cells"] = [
        {"partition_cell": "be86", "records": 4}
    ]
    write_marker(work, lying)
    with pytest.raises(SystemExit, match="carries 0 rows"):
        run_driver(work, binaries, tmp_path / "out")


def test_artifact_record_mismatch_fails_closed(binaries, tmp_path):
    work, marker = build_work_tree(tmp_path, {"be85": places_rows(4, "be85")})
    lying = copy.deepcopy(marker)
    lying["positions"]["records"] = 5
    write_marker(work, lying)
    with pytest.raises(SystemExit, match="directories sum to"):
        run_driver(work, binaries, tmp_path / "out")


def test_completeness_mismatch_against_admitted_rows_fails_closed(binaries, tmp_path):
    """The R1 form of the finalizer invariant: every shard encodes and
    verifies, and the run must STILL fail when the transform admitted more
    rows than the artifact carries."""
    work, marker = build_work_tree(tmp_path, {"be85": places_rows(4, "be85")})
    lying = copy.deepcopy(marker)
    lying["transform"]["admitted_features"] = 5
    write_marker(work, lying)
    with pytest.raises(SystemExit, match="completeness violated"):
        run_driver(work, binaries, tmp_path / "out")


def test_changed_pack_bytes_fail_closed(binaries, tmp_path):
    work, marker = build_work_tree(tmp_path, {"be85": places_rows(4, "be85")})
    key = marker["positions"]["packs"][0]["object"]["key"]
    path = work / "store-map" / key
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(SystemExit, match="differs from its marker identity"):
        run_driver(work, binaries, tmp_path / "out")
