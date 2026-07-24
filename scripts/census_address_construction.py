#!/usr/bin/env python3
"""Bounded transformed-row census for one pinned Address projection task."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SPIKE = load("address_spike", ROOT / "scripts/spike_address_construction.py")
CONSTRUCTION = load("address_construction", ROOT / "scripts/address_construction_v1.py")


def run(args: argparse.Namespace) -> dict:
    import duckdb
    import pyarrow.ipc as ipc
    import pyarrow.parquet as pq

    if duckdb.__version__ != "1.5.1":
        raise RuntimeError(f"DuckDB 1.5.1 is required; found {duckdb.__version__}")
    parquet = pq.ParquetFile(args.input)
    metadata = parquet.schema_arrow.metadata or {}
    source_inventory = json.loads(metadata[b"overture.source_inventory_json"])
    identity = {
        key.decode(): value.decode()
        for key, value in metadata.items()
        if key.startswith(b"overture.address_")
        or key
        in {
            b"overture.schema_fingerprint_sha256",
            b"overture.source_inventory_sha256",
        }
    }
    limits = CONSTRUCTION.Limits(
        max_input_rows=args.max_rows,
        max_rss_bytes=args.max_rss_bytes,
        max_scratch_bytes=args.max_scratch_bytes,
        wall_seconds=args.wall_seconds,
        duckdb_memory_limit=args.memory_limit,
        duckdb_threads=args.threads,
    )
    with tempfile.TemporaryDirectory(prefix="address-census-", dir=args.scratch_root) as name:
        workspace = Path(name)
        hydrated = workspace / "hydrated.arrow"
        transformed = workspace / "transformed.arrow"
        transform_report = workspace / "transform.json"
        source_limits = workspace / "source-limits.json"
        source_limits.write_text(
            json.dumps(
                {
                    "objects": [
                        {"records": item["records"], "row_groups": item["row_groups"]}
                        for item in source_inventory["objects"]
                    ]
                },
                sort_keys=True,
            )
        )
        hydration = SPIKE.hydrate_parquet(args.input, hydrated, 65_536)
        transform, transform_evidence = CONSTRUCTION.transform(
            args.binary,
            hydrated,
            source_limits,
            transformed,
            transform_report,
            limits,
            [workspace],
        )
        database = workspace / "census.duckdb"
        spill = workspace / "spill"
        spill.mkdir()
        connection = duckdb.connect(str(database))
        connection.execute(f"SET memory_limit = '{args.memory_limit}'")
        connection.execute(f"SET threads = {args.threads}")
        connection.execute(f"SET temp_directory = '{spill}'")
        started = time.monotonic()
        with transformed.open("rb") as source:
            reader = ipc.open_stream(source)
            connection.register("transformed", reader)
            with CONSTRUCTION.StageWatchdog([workspace], limits, connection) as watchdog:
                connection.execute("CREATE TABLE rows AS SELECT * FROM transformed")
                bucket = connection.execute(
                    "SELECT country, maximum_bucket, count(*)::UBIGINT records "
                    "FROM rows GROUP BY ALL ORDER BY records DESC, country, maximum_bucket LIMIT 1"
                ).fetchone()
                keys = ", ".join(f"normalized_key_{index}" for index in range(8))
                duplicate = connection.execute(
                    f"SELECT {keys}, count(*)::UBIGINT records FROM rows GROUP BY {keys} "
                    f"ORDER BY records DESC, {keys} LIMIT 1"
                ).fetchone()
        evidence = watchdog.evidence()
        evidence["duckdb_census_seconds"] = time.monotonic() - started
        connection.close()
        report = {
            "schema": "overture-address-construction-transformed-census-v1",
            "identity": identity,
            "input": {
                "bytes": args.input.stat().st_size,
                "sha256": CONSTRUCTION.sha256_file(args.input),
                "rows": parquet.metadata.num_rows,
            },
            "hydration": hydration,
            "transform": transform,
            "transform_evidence": transform_evidence,
            "census_evidence": evidence,
            "maximum_bucket_group": {
                "country": bucket[0],
                "maximum_bucket": bucket[1],
                "records": bucket[2],
            },
            "maximum_exact_normalized_key": {
                "normalized_key": list(duplicate[:8]),
                "records": duplicate[8],
            },
        }
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--max-rows", type=int, default=4_100_000)
    parser.add_argument("--max-rss-bytes", type=int, default=4_294_967_296)
    parser.add_argument("--max-scratch-bytes", type=int, default=17_179_869_184)
    parser.add_argument("--wall-seconds", type=float, default=900)
    parser.add_argument("--memory-limit", default="3GB")
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    args.scratch_root.mkdir(parents=True, exist_ok=True)
    print(json.dumps(run(args), sort_keys=True))


if __name__ == "__main__":
    main()
