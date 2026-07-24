#!/usr/bin/env python3
"""Frozen bounded census for one projected Places construction-v1 task."""

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


P = load(
    "places_construction_census_shared", ROOT / "scripts/places_construction_v1.py"
)


def run(args: argparse.Namespace) -> dict:
    import duckdb
    import pyarrow.ipc as ipc
    import pyarrow.parquet as pq

    if duckdb.__version__ != "1.5.1":
        raise RuntimeError(f"DuckDB 1.5.1 is required; found {duckdb.__version__}")
    parquet = pq.ParquetFile(args.input)
    metadata = parquet.schema_arrow.metadata or {}
    raw_identity = metadata.get(b"overture.places_projection_identity")
    if raw_identity is None:
        raise ValueError("Places census input lacks projection identity")
    identity = json.loads(raw_identity)
    limits = P.Limits(
        max_input_rows=args.max_rows,
        max_rss_bytes=args.max_rss_bytes,
        max_scratch_bytes=args.max_scratch_bytes,
        wall_seconds=args.wall_seconds,
        duckdb_memory_limit=args.memory_limit,
        duckdb_threads=args.threads,
        required_duckdb_version="1.5.1",
    )
    with tempfile.TemporaryDirectory(
        prefix="places-census-", dir=args.scratch_root
    ) as name:
        workspace = Path(name)
        hydrated = workspace / "hydrated.arrow"
        transformed = workspace / "transformed.arrow"
        transform_report = workspace / "transform.json"
        hydration = P.hydrate(args.input, hydrated)
        transform_evidence = P.A.run_bounded(
            [
                str(args.binary),
                "--input",
                str(hydrated),
                "--output",
                str(transformed),
                "--report",
                str(transform_report),
                "--source-limits",
                str(args.source_limits),
            ],
            scratch_roots=[workspace],
            limits=P.A.Limits(
                max_rss_bytes=args.max_rss_bytes,
                max_scratch_bytes=args.max_scratch_bytes,
                wall_seconds=args.wall_seconds,
            ),
        )
        transform = json.loads(transform_report.read_text())
        connection = duckdb.connect(str(workspace / "census.duckdb"))
        connection.execute(f"SET memory_limit='{args.memory_limit}'")
        connection.execute(f"SET threads={args.threads}")
        connection.execute(f"SET temp_directory='{workspace}'")
        started = time.monotonic()
        with transformed.open("rb") as source:
            reader = ipc.open_stream(source)
            connection.register("terms_stream", reader)
            with P.A.StageWatchdog([workspace], limits, connection) as watchdog:
                connection.execute("CREATE TABLE terms AS SELECT * FROM terms_stream")
                spatial = connection.execute(
                    "SELECT partition_cell, count(*)::UBIGINT records FROM terms "
                    "GROUP BY partition_cell ORDER BY records DESC, partition_cell LIMIT 1"
                ).fetchone()
                token = connection.execute(
                    "SELECT token, count(*)::UBIGINT records FROM terms GROUP BY token "
                    "ORDER BY records DESC, token LIMIT 1"
                ).fetchone()
                duplicate = connection.execute(
                    "SELECT feature_id, count(*)::UBIGINT occurrences FROM (SELECT DISTINCT "
                    "feature_id, source_object_index, source_row_group, source_row_index FROM terms) "
                    "GROUP BY feature_id ORDER BY occurrences DESC, feature_id LIMIT 1"
                ).fetchone()
                duplicate_rows = connection.execute(
                    "SELECT coalesce(sum(occurrences),0)::UBIGINT FROM (SELECT count(*) occurrences "
                    "FROM (SELECT DISTINCT feature_id, source_object_index, source_row_group, "
                    "source_row_index FROM terms) GROUP BY feature_id HAVING count(*)>1)"
                ).fetchone()[0]
        evidence = watchdog.evidence()
        evidence["duckdb_census_seconds"] = time.monotonic() - started
        connection.close()
        report = {
            "schema": "overture-places-construction-v1-census-v1",
            "identity": identity,
            "input": {
                "bytes": args.input.stat().st_size,
                "sha256": P.A.sha256_file(args.input),
                "rows": parquet.metadata.num_rows,
            },
            "hydration": hydration,
            "transform": transform,
            "transform_evidence": transform_evidence,
            "census_evidence": evidence,
            "metrics": {
                "maximum_spatial_cell_term_rows": spatial[1],
                "maximum_spatial_cell": spatial[0],
                "term_rows_per_admitted_feature": (
                    transform["emitted_term_rows"] / transform["admitted_features"]
                    if transform["admitted_features"]
                    else 0
                ),
                "multilingual_cjk_features": transform["multilingual_features"]
                + transform["cjk_features"],
                "multilingual_features": transform["multilingual_features"],
                "cjk_features": transform["cjk_features"],
                "maximum_uuid_multiplicity": duplicate[1],
                "duplicate_uuid_rows": duplicate_rows,
                "maximum_token_rows": token[1],
                "maximum_token": token[0],
            },
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--source-limits", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--max-rows", type=int, default=1_000_000)
    parser.add_argument("--max-rss-bytes", type=int, default=4_294_967_296)
    parser.add_argument("--max-scratch-bytes", type=int, default=8_589_934_592)
    parser.add_argument("--wall-seconds", type=float, default=600)
    parser.add_argument("--memory-limit", default="3GB")
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    args.scratch_root.mkdir(parents=True, exist_ok=True)
    print(json.dumps(run(args), sort_keys=True))


if __name__ == "__main__":
    main()
