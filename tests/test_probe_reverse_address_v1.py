from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest


ROOT = Path(__file__).parent.parent
SPEC = importlib.util.spec_from_file_location(
    "probe_reverse_address_v1", ROOT / "scripts" / "probe_reverse_address_v1.py"
)
assert SPEC and SPEC.loader
PROBE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PROBE
SPEC.loader.exec_module(PROBE)

REQUEST = "a" * 64


@pytest.fixture(scope="session")
def binaries() -> tuple[Path, Path]:
    subprocess.run(
        [
            "cargo",
            "build",
            "--manifest-path",
            str(ROOT / "crates" / "Cargo.toml"),
            "-p",
            "geocoder-construction",
            "--bin",
            "reverse-encode-v1",
            "--bin",
            "reverse-verify-v1",
            "--release",
        ],
        check=True,
    )
    target = ROOT / "crates" / "target" / "release"
    return target / "reverse-encode-v1", target / "reverse-verify-v1"


def identity(seed: str, size: int = 1) -> dict:
    return {
        "key": f"map/address/records/sha256/{seed * 64}.parquet",
        "bytes": size,
        "sha256": seed * 64,
    }


def test_densest_cell_uses_ascending_cell_tie_break():
    assert PROBE.select_densest_cell({"c329": 7, "8123": 8, "8122": 8}) == (
        "8122",
        8,
    )


def test_range_counts_reconcile_and_include_empty_ranges():
    plan = {
        "expected_records": 9,
        "packs": [
            {"shuffle_bucket": 0, "records": 2, "object": identity("1", 10)},
            {"shuffle_bucket": 15, "records": 3, "object": identity("2", 20)},
            {"shuffle_bucket": 16, "records": 4, "object": identity("3", 30)},
        ],
    }
    ranges = PROBE.range_counts(plan)
    assert len(ranges) == 16
    assert ranges[0]["records"] == 5
    assert ranges[0]["source_packs"] == 2
    assert ranges[0]["source_bytes"] == 30
    assert ranges[1]["records"] == 4
    assert ranges[2]["records"] == 0


def test_projection_uses_worse_rate_and_exact_three_halves_headroom():
    ranges = [
        {
            "range_id": 0,
            "bucket_start": 0,
            "bucket_end": 15,
            "records": 100,
            "source_packs": 1,
            "source_bytes": 10,
        }
    ]
    result = PROBE.conservative_projection(
        ranges=ranges,
        probe_records=10,
        probe_bytes=1_000,
        cap_bytes=15_000,
    )
    assert result["basis"] == "global_densest_cell"
    assert result["headroom_numerator"] == 3
    assert result["headroom_denominator"] == 2
    assert result["maximum_range"]["projected_output_bytes"] == 15_000
    assert result["within_cap"] is True
    assert (
        PROBE.conservative_projection(
            ranges=ranges,
            probe_records=10,
            probe_bytes=1_001,
            cap_bytes=15_000,
        )["within_cap"]
        is False
    )


def _row(cell: str, index: int) -> dict:
    return {
        "feature_id": index.to_bytes(16, "big"),
        "partition_cell": cell,
        "longitude_e7": 0,
        "latitude_e7": 0,
        "display_country": "United States",
        "postal_city": "Seattle",
        "postcode": f"981{index:02d}",
        "street": "Pine Street",
        "number": str(index),
        "unit": "",
        "address_levels": ["Washington"],
        "source_object_index": 0,
        "source_row_group": 0,
        "source_row_index": index,
    }


def _pack(
    *,
    staging_store,
    root: Path,
    task_id: str,
    cell: str,
    rows: list[dict],
) -> dict:
    bucket = PROBE.R2.cell_bucket(cell)
    parquet = root / f"{task_id}.parquet"
    pq.write_table(
        pa.Table.from_pylist(rows, schema=PROBE.R2.R1.input_schema("addresses")),
        parquet,
    )
    object_identity = staging_store.put_content(
        parquet, "map/address/records", ".parquet"
    )
    directory = {
        "schema": PROBE.ADDRESS.ADDRESS_RECORDS_DIRECTORY_SCHEMA,
        "shuffle_bucket": bucket,
        "records": len(rows),
        "cells": [{"partition_cell": cell, "records": len(rows)}],
        "row_groups": [{"records": len(rows)}],
    }
    directory_path = root / f"{task_id}.json"
    directory_path.write_text(json.dumps(directory, sort_keys=True) + "\n")
    directory_identity = staging_store.put_content(
        directory_path, "map/address/record-directories", ".json"
    )
    return {
        "task_id": task_id,
        "pack_id": bucket,
        "shuffle_bucket": bucket,
        "records": len(rows),
        "object": object_identity,
        "directory_object": directory_identity,
    }


def test_probe_encodes_only_authenticated_densest_cell_without_staging_writes(
    binaries, tmp_path
):
    staging_root = tmp_path / "staging"
    prefix = (
        staging_root
        / PROBE.STAGING.staging_prefix(REQUEST, "addresses")
    )
    staging_store = PROBE.ADDRESS.LocalObjectStore(prefix)
    packs = [
        _pack(
            staging_store=staging_store,
            root=tmp_path,
            task_id="addresses-map-a",
            cell="8080",
            rows=[_row("8080", 1), _row("8080", 2)],
        ),
        _pack(
            staging_store=staging_store,
            root=tmp_path,
            task_id="addresses-map-b",
            cell="8080",
            rows=[_row("8080", 3), _row("8080", 4)],
        ),
        _pack(
            staging_store=staging_store,
            root=tmp_path,
            task_id="addresses-map-c",
            cell="c328",
            rows=[_row("c328", 5), _row("c328", 6), _row("c328", 7)],
        ),
    ]
    plan = {
        "schema": PROBE.R2.PLAN_SCHEMA,
        "family": "addresses",
        "request_sha256": REQUEST,
        "shuffle_bucket_bits": PROBE.R2.SHUFFLE_BUCKET_BITS,
        "task_ids": sorted(pack["task_id"] for pack in packs),
        "expected_records": 7,
        "packs": sorted(
            packs, key=lambda pack: (pack["shuffle_bucket"], pack["task_id"])
        ),
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(PROBE.R2.canonical_json(plan) + b"\n")
    plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    before = sorted(
        (path.relative_to(staging_root), path.read_bytes())
        for path in staging_root.rglob("*")
        if path.is_file()
    )
    encoder, verifier = binaries
    output = tmp_path / "out" / "probe.json"
    defaults = PROBE.ADDRESS.Limits()
    args = argparse.Namespace(
        plan=plan_path,
        expected_plan_sha256=plan_sha256,
        store_root=tmp_path / "cache",
        staging_root=staging_root,
        staging_bucket=None,
        staging_endpoint_url=None,
        scratch_dir=tmp_path / "scratch",
        encoder_binary=encoder,
        verifier_binary=verifier,
        output=output,
        max_rss_bytes=defaults.max_rss_bytes,
        max_scratch_bytes=defaults.max_scratch_bytes,
        max_output_bytes=PROBE.OUTPUT_CAP_BYTES,
        wall_seconds=defaults.wall_seconds,
        duckdb_memory_limit=defaults.duckdb_memory_limit,
        duckdb_threads=defaults.duckdb_threads,
        required_duckdb_version=defaults.required_duckdb_version,
        allow_unpinned_duckdb=True,
    )
    result = PROBE.run_probe(args)
    after = sorted(
        (path.relative_to(staging_root), path.read_bytes())
        for path in staging_root.rglob("*")
        if path.is_file()
    )

    assert before == after
    assert result["read_only"] is True
    assert result["directory_scan"]["records"] == 7
    assert result["densest_cell"]["partition_cell"] == "8080"
    assert result["densest_cell"]["records"] == 4
    assert result["densest_cell"]["loaded_records"] == 4
    assert result["densest_cell"]["encoded_records"] == 4
    assert result["densest_cell"]["source_packs"] == 2
    assert set(result["densest_cell"]["dictionary_cardinalities"]) == set(
        PROBE.DICTIONARY_FIELDS
    )
    assert (
        result["densest_cell"]["framing"]["header_bytes"]
        + result["densest_cell"]["framing"]["dictionary_bytes"]
        + result["densest_cell"]["framing"]["payload_bytes"]
        + result["densest_cell"]["framing"]["index_bytes"]
        == result["densest_cell"]["framing"]["total_bytes"]
    )
    assert output.is_file()
