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
    assert result["measurement_status"] == "complete"
    assert result["directory_scan"]["records"] == 7
    assert result["densest_cell"]["partition_cell"] == "8080"
    assert result["densest_cell"]["records"] == 4
    assert result["densest_cell"]["loaded_records"] == 4
    assert result["densest_cell"]["encoded_records"] == 4
    assert result["densest_cell"]["source_packs"] == 2
    assert set(result["densest_cell"]["dictionary_cardinalities"]) == set(
        PROBE.DICTIONARY_FIELDS
    )
    dictionary = result["densest_cell"]["pre_encoding_dictionary"]
    assert dictionary["format"] == "ARDX0001"
    assert dictionary["total_bytes"] == result["densest_cell"]["framing"][
        "dictionary_bytes"
    ]
    assert dictionary["exceeds_serving_cap"] is False
    assert set(dictionary["fields"]) == set(PROBE.DICTIONARY_FIELDS)
    assert all(
        set(metrics)
        == {
            "cardinality",
            "utf8_value_bytes",
            "encoded_entry_bytes",
            "max_utf8_value_bytes",
        }
        for metrics in dictionary["fields"].values()
    )
    assert (
        result["densest_cell"]["framing"]["header_bytes"]
        + result["densest_cell"]["framing"]["dictionary_bytes"]
        + result["densest_cell"]["framing"]["payload_bytes"]
        + result["densest_cell"]["framing"]["index_bytes"]
        == result["densest_cell"]["framing"]["total_bytes"]
    )
    assert output.is_file()


def test_probe_retains_cardinalities_and_read_only_evidence_when_encoder_fails(
    monkeypatch, tmp_path
):
    staging_root = tmp_path / "staging"
    prefix = (
        staging_root
        / PROBE.STAGING.staging_prefix(REQUEST, "addresses")
    )
    staging_store = PROBE.ADDRESS.LocalObjectStore(prefix)
    rows = [_row("8080", index) for index in (1, 2, 3)]
    rows[1]["postal_city"] = "Tacóma"
    rows[2]["postcode"] = rows[1]["postcode"]
    rows[1]["street"] = "Pike Street"
    rows[1]["unit"] = "A"
    rows[0]["address_levels"] = ["Washington", "King"]
    rows[1]["address_levels"] = ["Washington", "Pierce"]
    pack = _pack(
        staging_store=staging_store,
        root=tmp_path,
        task_id="addresses-map-a",
        cell="8080",
        rows=rows,
    )
    plan = {
        "schema": PROBE.R2.PLAN_SCHEMA,
        "family": "addresses",
        "request_sha256": REQUEST,
        "shuffle_bucket_bits": PROBE.R2.SHUFFLE_BUCKET_BITS,
        "task_ids": [pack["task_id"]],
        "expected_records": len(rows),
        "packs": [pack],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(PROBE.R2.canonical_json(plan) + b"\n")
    plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    before = sorted(
        (path.relative_to(staging_root), path.read_bytes())
        for path in staging_root.rglob("*")
        if path.is_file()
    )
    commands = []

    def fail_encoder(command, *, scratch_roots, limits):
        commands.append(command)
        raise subprocess.CalledProcessError(7, command)

    monkeypatch.setattr(PROBE.ADDRESS, "run_bounded", fail_encoder)
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
        encoder_binary=tmp_path / "encoder",
        verifier_binary=tmp_path / "verifier",
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
    assert len(commands) == 1
    assert output.is_file()
    assert json.loads(output.read_text()) == result
    assert result["read_only"] is True
    assert result["measurement_status"] == "encoder_failed"
    assert result["densest_cell"]["dictionary_cardinalities"] == {
        "display_country": 1,
        "postal_city": 2,
        "postcode": 2,
        "street": 2,
        "number": 3,
        "unit": 2,
        "address_levels": 3,
    }
    dictionary = result["densest_cell"]["pre_encoding_dictionary"]
    assert dictionary == {
        "format": "ARDX0001",
        "header_bytes": 12,
        "field_header_bytes": 28,
        "encoded_entry_bytes": 113,
        "total_bytes": 153,
        "serving_cap_bytes": 8 * 1024 * 1024,
        "exceeds_serving_cap": False,
        "fields": {
            "display_country": {
                "cardinality": 1,
                "utf8_value_bytes": 13,
                "encoded_entry_bytes": 15,
                "max_utf8_value_bytes": 13,
            },
            "postal_city": {
                "cardinality": 2,
                "utf8_value_bytes": 14,
                "encoded_entry_bytes": 18,
                "max_utf8_value_bytes": 7,
            },
            "postcode": {
                "cardinality": 2,
                "utf8_value_bytes": 10,
                "encoded_entry_bytes": 14,
                "max_utf8_value_bytes": 5,
            },
            "street": {
                "cardinality": 2,
                "utf8_value_bytes": 22,
                "encoded_entry_bytes": 26,
                "max_utf8_value_bytes": 11,
            },
            "number": {
                "cardinality": 3,
                "utf8_value_bytes": 3,
                "encoded_entry_bytes": 9,
                "max_utf8_value_bytes": 1,
            },
            "unit": {
                "cardinality": 2,
                "utf8_value_bytes": 1,
                "encoded_entry_bytes": 5,
                "max_utf8_value_bytes": 1,
            },
            "address_levels": {
                "cardinality": 3,
                "utf8_value_bytes": 20,
                "encoded_entry_bytes": 26,
                "max_utf8_value_bytes": 10,
            },
        },
    }
    assert result["densest_cell"]["encoded_records"] is None
    assert result["densest_cell"]["framing"] is None
    assert result["projection"] is None
    assert result["resources"]["encoder"]["exit_code"] == 7
    assert result["resources"]["verifier"]["status"] == "not_run"
    assert result["resources"]["staging"]["staged_objects_published"] == 0
    assert result["execute_gate"]["status"] == "blocked"


def test_main_reports_blocked_encoder_evidence_without_assuming_a_shard(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.setattr(
        PROBE,
        "run_probe",
        lambda _args: {
            "request_sha256": REQUEST,
            "plan_sha256": "b" * 64,
            "measurement_status": "encoder_failed",
            "densest_cell": {
                "partition_cell": "5e5e",
                "records": 6_489_932,
                "dictionary_cardinalities": {
                    field: index
                    for index, field in enumerate(PROBE.DICTIONARY_FIELDS, 1)
                },
                "pre_encoding_dictionary": {
                    "total_bytes": 9_000_000,
                    "exceeds_serving_cap": True,
                },
            },
            "resources": {"encoder": {"exit_code": 1}},
            "execute_gate": {"status": "blocked"},
        },
    )

    status = PROBE.main(
        [
            "--plan",
            str(tmp_path / "plan.json"),
            "--expected-plan-sha256",
            "b" * 64,
            "--store-root",
            str(tmp_path / "cache"),
            "--staging-root",
            str(tmp_path / "staging"),
            "--scratch-dir",
            str(tmp_path / "scratch"),
            "--encoder-binary",
            str(tmp_path / "encoder"),
            "--verifier-binary",
            str(tmp_path / "verifier"),
            "--output",
            str(tmp_path / "out.json"),
        ]
    )

    summary = json.loads(capsys.readouterr().out)
    assert status == 2
    assert summary["measurement_status"] == "encoder_failed"
    assert summary["encoder_exit_code"] == 1
    assert summary["projected_ardx0001_dictionary_bytes"] == 9_000_000
    assert summary["ardx0001_dictionary_exceeds_serving_cap"] is True
    assert "dictionary_bytes" not in summary
