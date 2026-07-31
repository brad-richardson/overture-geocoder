"""Synthetic duplicate-UUID coverage gate.

Real 2026-06-17.0 Places census data carries zero duplicate UUIDs, so duplicate
coverage cannot be closed by a planet gate (see
``scripts/validate_places_planet_readiness.py``, which now records it
informationally). This fail-closed pytest is that gate: it drives a checked-in
synthetic fixture whose feature ids repeat across several source rows through the
real map -> plan -> reduce pipeline and asserts every copy survives as a distinct
serving candidate keyed by its source locator (multiplicity preserved through
provenance).
"""

from __future__ import annotations

import importlib.util
import json
import struct
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path

import pytest


pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pytest.importorskip("pyarrow.compute")

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/places_duplicate_uuid.json"


def geometry(row: dict) -> bytes:
    longitude, latitude = row.get("point", [0.0, 0.0])
    return b"\x01" + struct.pack("<Idd", 1, longitude, latitude)


def write_fixture(path: Path, rows: list[dict], *, row_group_size: int = 4) -> None:
    def values(name: str, default):
        return [row.get(name, default) for row in rows]

    table = pa.Table.from_arrays(
        [
            pa.array(values("id", None), type=pa.string()),
            pa.array(values("primary_name", ""), type=pa.string()),
            pa.array(values("common_names", []), type=pa.list_(pa.string())),
            pa.array(values("brand_name", ""), type=pa.string()),
            pa.array(values("category", ""), type=pa.string()),
            pa.array(values("locality", ""), type=pa.string()),
            pa.array(values("region", ""), type=pa.string()),
            pa.array(values("country", ""), type=pa.string()),
            pa.array(values("confidence", 0.5), type=pa.float64()),
            pa.array(values("operating_status", "open"), type=pa.string()),
            pa.array([geometry(row) for row in rows], type=pa.binary()),
            pa.array(values("source_object_index", 0), type=pa.int32()),
            pa.array(values("source_row_group", 0), type=pa.int32()),
            pa.array(values("source_row_index", 0), type=pa.int32()),
        ],
        names=[
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
        ],
    )
    pq.write_table(table, path, row_group_size=row_group_size)


def decode_serving(path: Path, mode: str) -> list[dict]:
    data = path.read_bytes()
    # 0003 adds the prominence_rank byte; the producer emits only the current
    # generation, while the worker still decodes 0002.
    assert data[:8] == (b"PLRV0003" if mode == "routed" else b"PLHD0003")
    expected = struct.unpack_from("<Q", data, 8)[0]
    index_offset = struct.unpack_from("<Q", data, 16)[0]
    position = 32
    output: list[dict] = []

    def text(entry: bytes, at: int) -> tuple[str, int]:
        length = struct.unpack_from("<H", entry, at)[0]
        at += 2
        return entry[at : at + length].decode(), at + length

    while position < index_offset:
        length = struct.unpack_from("<I", data, position)[0]
        position += 4
        entry = data[position : position + length]
        position += length
        at = 0
        token, at = text(entry, at)
        cell = None
        if mode == "routed":
            cell, at = text(entry, at)
        # 0003 layout: field_mask, confidence_rank, prominence_rank.
        _mask, _rank, _prominence = struct.unpack_from("<BBB", entry, at)
        at += 3
        identifier = str(uuid.UUID(bytes=entry[at : at + 16]))
        at += 16
        _lon, _lat, object_index, row_group, row_index = struct.unpack_from(
            "<ddIIQ", entry, at
        )
        output.append(
            {
                "token": token,
                "cell": cell,
                "id": identifier,
                "object": object_index,
                "row_group": row_group,
                "row_index": row_index,
            }
        )
    assert len(output) == expected
    return output


@pytest.fixture(scope="module")
def construction_binaries() -> dict[str, Path]:
    names = [
        "places-transform-v1",
        "places-proof-directory",
        "places-serving-encode-v1",
        "places-serving-verify-v1",
    ]
    subprocess.run(
        ["cargo", "build", "-p", "geocoder-construction", "--bins"],
        cwd=ROOT / "crates",
        check=True,
    )
    return {name: ROOT / "crates/target/debug" / name for name in names}


@pytest.fixture(scope="module")
def construction_module():
    spec = importlib.util.spec_from_file_location(
        "places_construction_v1_dupgate", ROOT / "scripts/places_construction_v1.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_duplicate_uuid_multiplicity_survives_map_plan_reduce(
    tmp_path, construction_binaries, construction_module
):
    module = construction_module
    fixture = json.loads(FIXTURE.read_text())
    rows = fixture["rows"]
    shared_token = fixture["shared_token"]

    # Expected multiplicity per feature is the number of source rows carrying that
    # id — the fixture is the single source of truth.
    expected_multiplicity = Counter(row["id"] for row in rows)
    assert any(count > 1 for count in expected_multiplicity.values()), (
        "fixture must contain duplicate UUIDs to be a meaningful gate"
    )

    source = tmp_path / "source.parquet"
    write_fixture(source, rows, row_group_size=4)
    parquet = pq.ParquetFile(source)
    source_limits = tmp_path / "source-limits.json"
    source_limits.write_text(
        json.dumps(
            {
                "objects": [
                    {
                        "records": len(rows),
                        "row_groups": parquet.metadata.num_row_groups,
                    }
                ]
            }
        )
    )
    limits = module.Limits(
        max_input_rows=100,
        max_pack_rows=100,
        parquet_row_group_rows=64,
        max_rss_bytes=2 * 1024**3,
        max_scratch_bytes=2 * 1024**3,
        max_output_bytes=512 * 1024**2,
        wall_seconds=120,
        allow_unpinned_duckdb=True,
    )
    store = module.A.LocalObjectStore(tmp_path / "store")
    marker = module.map_task(
        input_path=source,
        source_limits=source_limits,
        store=store,
        scratch_root=tmp_path / "scratch",
        request_sha256="ab" * 32,
        task_id="dup-a",
        transform_binary=construction_binaries["places-transform-v1"],
        proof_binary=construction_binaries["places-proof-directory"],
        limits=limits,
    )
    # One emitted term row per (source row, distinct token): every duplicate copy
    # is retained through the pack proofs, not collapsed.
    assert marker["binding"]["records"] > 0

    plan = module.genesis_plan([marker], row_cap=10_000)
    reductions = [
        module.reduce_partition(
            partition=partition,
            plan=plan,
            markers=[marker],
            store=store,
            scratch_root=tmp_path / "reduce",
            encoder_binary=construction_binaries["places-serving-encode-v1"],
            verifier_binary=construction_binaries["places-serving-verify-v1"],
            limits=limits,
        )
        for partition in plan["partitions"]
    ]

    serving: list[dict] = []
    for reduction in reductions:
        serving.extend(
            decode_serving(store.path(reduction["routed_object"]["key"]), "routed")
        )

    # The shared token must carry every source-row copy of every duplicate id,
    # each distinguished by its source locator.
    shared = [entry for entry in serving if entry["token"] == shared_token]
    observed_multiplicity = Counter(entry["id"] for entry in shared)
    assert observed_multiplicity == expected_multiplicity

    # Copies are distinct provenance rows, not deduplicated: the (id, locator)
    # tuples for the shared token are all unique and one per source row.
    locators = {
        (entry["id"], entry["object"], entry["row_group"], entry["row_index"])
        for entry in shared
    }
    assert len(locators) == len(rows)
