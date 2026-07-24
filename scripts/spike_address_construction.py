#!/usr/bin/env python3
"""Bounded local harness for the Address Rust/Arrow/DuckDB construction spike.

Python owns process coordination and the temporary Arrow hydration boundary. It
does not convert feature batches to Python rows, sort them, or write Parquet.
"""

from __future__ import annotations

import argparse
import heapq
import hashlib
import json
import os
import platform
import pickle
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


REQUIRED_DUCKDB_VERSION = "1.5.1"
CONSTRUCTION_INGEST_BATCH_ROWS = 65_536


class PhaseRss:
    """Sample only this evidence process during one named in-process phase."""

    def __init__(self) -> None:
        import psutil

        self.process = psutil.Process(os.getpid())
        self.started_rss_bytes = self.process.memory_info().rss
        self.peak_rss_bytes = self.started_rss_bytes
        self.ended_rss_bytes = self.started_rss_bytes
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        import psutil

        while not self.stop.wait(0.005):
            try:
                self.peak_rss_bytes = max(
                    self.peak_rss_bytes, self.process.memory_info().rss
                )
            except psutil.Error:
                return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        self.thread.join()
        self.ended_rss_bytes = self.process.memory_info().rss
        self.peak_rss_bytes = max(self.peak_rss_bytes, self.ended_rss_bytes)

    def evidence(self) -> dict:
        return {
            "started_rss_bytes": self.started_rss_bytes,
            "peak_rss_bytes": self.peak_rss_bytes,
            "ended_rss_bytes": self.ended_rss_bytes,
            "retained_rss_bytes": self.ended_rss_bytes - self.started_rss_bytes,
        }


def current_rss_bytes() -> int:
    import psutil

    return psutil.Process(os.getpid()).memory_info().rss
PACK_SCHEMA = "overture-address-construction-pack-v1"
TOTAL_ORDER = (
    "country, maximum_bucket, normalized_key_0, normalized_key_1, "
    "normalized_key_2, normalized_key_3, normalized_key_4, normalized_key_5, "
    "normalized_key_6, normalized_key_7, feature_id, source_object_index, "
    "source_row_group, source_row_index"
)
PROJECTED_COLUMNS = (
    "id",
    "street",
    "number",
    "unit",
    "postcode",
    "postal_city",
    "address_levels",
    "country",
    "geometry",
    "source_object_index",
    "source_row_group",
    "source_row_index",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def hydrate_parquet(input_path: Path, output_path: Path, batch_rows: int) -> dict:
    import pyarrow.ipc as ipc
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(input_path)
    missing = sorted(set(PROJECTED_COLUMNS) - set(parquet.schema_arrow.names))
    if missing:
        raise ValueError(f"projected Address input is missing columns: {missing}")
    writer = None
    started = time.monotonic()
    rows = 0
    batches = 0
    with output_path.open("wb") as destination:
        try:
            for batch in parquet.iter_batches(
                batch_size=batch_rows,
                columns=list(PROJECTED_COLUMNS),
                use_threads=True,
            ):
                if writer is None:
                    writer = ipc.new_stream(destination, batch.schema)
                writer.write_batch(batch)
                rows += batch.num_rows
                batches += 1
            if writer is None:
                raise ValueError("projected Address input is empty")
        finally:
            if writer is not None:
                writer.close()
    return {
        "seconds": time.monotonic() - started,
        "rows": rows,
        "batches": batches,
        "ipc_bytes": output_path.stat().st_size,
    }


def hydrate_experiment_parquet(input_path: Path, output_path: Path) -> dict:
    """Columnarly adapt the checked-in real sample to the source boundary.

    This exists only because ``exports/experiment/addresses-raw.parquet``
    predates the projected global schema. No feature is materialized as a
    Python tuple, dictionary, or object.
    """

    import numpy as np
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.ipc as ipc
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(input_path)
    required = {
        "gers_id",
        "number",
        "street",
        "unit",
        "postcode",
        "city",
        "state",
        "country",
        "lon",
        "lat",
    }
    missing = sorted(required - set(parquet.schema_arrow.names))
    if missing:
        raise ValueError(f"experiment Address input is missing columns: {missing}")
    writer = None
    rows = 0
    started = time.monotonic()
    point_type = np.dtype(
        {
            "names": ["order", "kind", "lon", "lat"],
            "formats": ["u1", "<u4", "<f8", "<f8"],
            "offsets": [0, 1, 5, 13],
            "itemsize": 21,
        }
    )
    with output_path.open("wb") as destination:
        try:
            for group_index in range(parquet.metadata.num_row_groups):
                source = parquet.read_row_group(group_index, columns=sorted(required))
                count = source.num_rows
                points = np.empty(count, dtype=point_type)
                points["order"] = 1
                points["kind"] = 1
                points["lon"] = source["lon"].combine_chunks().to_numpy(
                    zero_copy_only=False
                )
                points["lat"] = source["lat"].combine_chunks().to_numpy(
                    zero_copy_only=False
                )
                offsets = np.arange(count + 1, dtype=np.int32) * 21
                geometry = pa.Array.from_buffers(
                    pa.binary(),
                    count,
                    [None, pa.py_buffer(offsets), pa.py_buffer(points)],
                )
                state = source["state"].combine_chunks()
                city = source["city"].combine_chunks()
                joined = pa.concat_arrays([state, city])
                indexes = np.empty(count * 2, dtype=np.int64)
                indexes[0::2] = np.arange(count, dtype=np.int64)
                indexes[1::2] = np.arange(count, dtype=np.int64) + count
                level_values = pc.take(joined, pa.array(indexes))
                level_offsets = pa.array(np.arange(count + 1, dtype=np.int32) * 2)
                levels = pa.ListArray.from_arrays(level_offsets, level_values)
                table = pa.Table.from_arrays(
                    [
                        source["gers_id"].combine_chunks(),
                        source["street"].combine_chunks(),
                        source["number"].combine_chunks(),
                        source["unit"].combine_chunks(),
                        source["postcode"].combine_chunks(),
                        source["city"].combine_chunks(),
                        levels,
                        source["country"].combine_chunks(),
                        geometry,
                        pa.array(np.zeros(count, dtype=np.int32)),
                        pa.array(np.full(count, group_index, dtype=np.int32)),
                        pa.array(np.arange(rows, rows + count, dtype=np.int32)),
                    ],
                    names=list(PROJECTED_COLUMNS),
                )
                if writer is None:
                    writer = ipc.new_stream(destination, table.schema)
                writer.write_table(table)
                rows += count
            if writer is None:
                raise ValueError("experiment Address input is empty")
        finally:
            if writer is not None:
                writer.close()
    return {
        "kind": "columnar-experiment-adapter-v1",
        "seconds": time.monotonic() - started,
        "rows": rows,
        "batches": parquet.metadata.num_row_groups,
        "ipc_bytes": output_path.stat().st_size,
    }


def run_legacy_baseline(
    input_path: Path,
    output_path: Path,
    diagnostics_path: Path | None = None,
    chunk_rows: int = 250_000,
    max_open_chunks: int = 16,
) -> dict:
    """Measure the disposable Python-row design on the same frozen IPC input."""

    import pyarrow as pa
    import pyarrow.ipc as ipc
    import pyarrow.parquet as pq

    script_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(script_dir))
    try:
        import pack_schemas_v1 as pack_schemas
        from experiment_address_reduce import strict_batch_records
    finally:
        sys.path.pop(0)
    started = time.monotonic()
    rss = {"start": current_rss_bytes()}

    def sample(name: str) -> None:
        rss[name] = current_rss_bytes()
        if diagnostics_path is not None:
            diagnostics_path.write_text(json.dumps(rss, indent=2, sort_keys=True) + "\n")

    sample("baseline_entered")
    if chunk_rows <= 0 or max_open_chunks <= 0:
        raise ValueError("baseline external-sort bounds must be positive")
    pending = []
    chunk_paths: list[Path] = []
    rejections = None
    rows = 0
    peak_pending_records = 0
    peak_spill_bytes = 0

    with tempfile.TemporaryDirectory(
        prefix="legacy-baseline-sort-", dir=output_path.parent
    ) as scratch_name:
        scratch = Path(scratch_name)

        def spill(records: list) -> None:
            nonlocal peak_spill_bytes
            records.sort(key=lambda item: (item[0], item[1]))
            path = scratch / f"chunk-{len(chunk_paths):03d}.pickle"
            with path.open("wb") as destination:
                for record in records:
                    pickle.dump(record, destination, protocol=pickle.HIGHEST_PROTOCOL)
            chunk_paths.append(path)
            peak_spill_bytes = sum(item.stat().st_size for item in chunk_paths)

        with input_path.open("rb") as source:
            for input_batch in ipc.open_stream(source):
                batch_records, batch_rejections = strict_batch_records(input_batch)
                pending.extend(batch_records)
                peak_pending_records = max(peak_pending_records, len(pending))
                while len(pending) >= chunk_rows:
                    spill(pending[:chunk_rows])
                    pending = pending[chunk_rows:]
                if rejections is None:
                    rejections = batch_rejections
                else:
                    for reason, count in batch_rejections.items():
                        rejections[reason] += count
        if pending:
            spill(pending)
            pending = []
        if not chunk_paths or len(chunk_paths) > max_open_chunks:
            raise ValueError("baseline external sort exceeded its open-chunk cap")
        sample("sorted_chunks_closed")

        def read_chunk(path: Path):
            with path.open("rb") as source:
                while True:
                    try:
                        yield pickle.load(source)
                    except EOFError:
                        return

        batch_rows = 65_536
        peak_batch_rows = 0
        output_rows = []
        streams = [read_chunk(path) for path in chunk_paths]
        with pq.ParquetWriter(
            output_path,
            pack_schemas.shuffle_schema(),
            compression="zstd",
            compression_level=6,
            version="2.6",
        ) as writer:
            for _, payload in heapq.merge(*streams):
                output_rows.append(
                    pack_schemas.payload_to_shuffle_row(payload, maximum_hash_bits=16)
                )
                rows += 1
                if len(output_rows) == batch_rows:
                    table = pa.Table.from_pylist(
                        output_rows, schema=pack_schemas.shuffle_schema()
                    )
                    writer.write_table(table, row_group_size=batch_rows)
                    peak_batch_rows = max(peak_batch_rows, len(output_rows))
                    output_rows = []
            if output_rows:
                table = pa.Table.from_pylist(
                    output_rows, schema=pack_schemas.shuffle_schema()
                )
                writer.write_table(table, row_group_size=batch_rows)
                peak_batch_rows = max(peak_batch_rows, len(output_rows))
    sample("bounded_rows_written")
    sample("parquet_written")
    return {
        "kind": "legacy-python-row-single-pack-reference-v1",
        "seconds": time.monotonic() - started,
        "rows": rows,
        "rejections": rejections,
        "bytes": output_path.stat().st_size,
        "sha256": sha256_file(output_path),
        "rss_snapshots_bytes": rss,
        "row_materialization": {
            "kind": "python-row-external-sort-merge-v1",
            "sort_chunk_rows": chunk_rows,
            "chunks": len(chunk_paths),
            "max_open_chunks": max_open_chunks,
            "peak_pending_records": peak_pending_records,
            "peak_spill_bytes": peak_spill_bytes,
            "maximum_batch_rows": batch_rows,
            "peak_batch_rows": peak_batch_rows,
            "full_python_row_table_materialized": False,
        },
        "limitations": [
            "omits legacy spill merge, summaries, manifests, and multi-pack planning",
            "included only as disposable same-input performance evidence",
        ],
    }


def run_transform(binary: Path, input_path: Path, output_path: Path, report: Path) -> dict:
    import psutil

    started = time.monotonic()
    process = subprocess.Popen(
        [
            str(binary),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--report",
            str(report),
        ],
    )
    peak_rss_bytes = 0
    observed = psutil.Process(process.pid)
    while process.poll() is None:
        try:
            peak_rss_bytes = max(peak_rss_bytes, observed.memory_info().rss)
        except psutil.Error:
            pass
        time.sleep(0.002)
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, process.args)
    value = json.loads(report.read_text())
    value["wall_seconds"] = time.monotonic() - started
    value["peak_rss_bytes"] = peak_rss_bytes
    value["output_ipc_bytes"] = output_path.stat().st_size
    return value


def construct_packs(
    transformed_path: Path,
    output_dir: Path,
    *,
    memory_limit: str,
    threads: int,
    allow_unpinned_duckdb: bool,
) -> dict:
    import duckdb
    import pyarrow.ipc as ipc

    if duckdb.__version__ != REQUIRED_DUCKDB_VERSION and not allow_unpinned_duckdb:
        raise RuntimeError(
            f"DuckDB {REQUIRED_DUCKDB_VERSION} is required; found {duckdb.__version__}. "
            "Use --allow-unpinned-duckdb only for local spike evidence."
        )
    output_dir.mkdir(parents=True, exist_ok=False)
    database_path = output_dir / "construction.duckdb"
    scratch = output_dir / "duckdb-spill"
    scratch.mkdir()
    connection = duckdb.connect(str(database_path))
    connection.execute(f"SET memory_limit = '{memory_limit}'")
    connection.execute(f"SET threads = {threads}")
    connection.execute(f"SET temp_directory = '{scratch}'")
    started = time.monotonic()
    ingestion_batches = 0
    maximum_ingestion_batch_rows = 0
    with transformed_path.open("rb") as source:
        reader = ipc.open_stream(source)
        with PhaseRss() as ingestion_rss:
            for batch in reader:
                if batch.num_rows > CONSTRUCTION_INGEST_BATCH_ROWS:
                    raise ValueError("transformed Arrow batch exceeds ingestion cap")
                connection.register("transformed_batch", batch)
                try:
                    if ingestion_batches == 0:
                        connection.execute(
                            "CREATE TABLE address_rows AS "
                            "SELECT * FROM transformed_batch WHERE false"
                        )
                    connection.execute(
                        "INSERT INTO address_rows SELECT * FROM transformed_batch"
                    )
                finally:
                    connection.unregister("transformed_batch")
                ingestion_batches += 1
                maximum_ingestion_batch_rows = max(
                    maximum_ingestion_batch_rows, batch.num_rows
                )
                del batch
    if ingestion_batches == 0:
        raise ValueError("transformed Arrow stream contained no record batches")
    materialize_seconds = time.monotonic() - started

    pack_path = output_dir / "pack-000000.parquet"
    summary_path = output_dir / "summary.parquet"
    export_started = time.monotonic()
    connection.execute("SET threads = 1")
    connection.execute(
        f"""
        COPY (
            SELECT * FROM address_rows ORDER BY {TOTAL_ORDER}
        ) TO '{pack_path}' (
            FORMAT PARQUET,
            COMPRESSION ZSTD,
            COMPRESSION_LEVEL 6,
            ROW_GROUP_SIZE 65536,
            PARQUET_VERSION V2,
            PRESERVE_ORDER true
        )
        """
    )
    connection.execute(
        f"""
        COPY (
            SELECT country, maximum_bucket, count(*)::UBIGINT AS records
            FROM address_rows
            GROUP BY country, maximum_bucket
            ORDER BY country, maximum_bucket
        ) TO '{summary_path}' (
            FORMAT PARQUET,
            COMPRESSION ZSTD,
            COMPRESSION_LEVEL 6,
            ROW_GROUP_SIZE 65536,
            PARQUET_VERSION V2,
            PRESERVE_ORDER true
        )
        """
    )
    export_seconds = time.monotonic() - export_started
    row_count = connection.execute("SELECT count(*) FROM address_rows").fetchone()[0]
    connection.close()
    return {
        "schema": PACK_SCHEMA,
        "duckdb_version": duckdb.__version__,
        "required_duckdb_version": REQUIRED_DUCKDB_VERSION,
        "pinned_runtime": duckdb.__version__ == REQUIRED_DUCKDB_VERSION,
        "materialize_threads": threads,
        "export_threads": 1,
        "materialize_seconds": materialize_seconds,
        "bounded_ingestion": {
            "kind": "arrow-record-batch-to-on-disk-duckdb-v1",
            "maximum_batch_rows": CONSTRUCTION_INGEST_BATCH_ROWS,
            "batches": ingestion_batches,
            "observed_maximum_batch_rows": maximum_ingestion_batch_rows,
            "full_table_read_all": False,
            "phase_rss": ingestion_rss.evidence(),
        },
        "export_seconds": export_seconds,
        "rows": row_count,
        "total_order": TOTAL_ORDER,
        "pack": {
            "path": pack_path.name,
            "bytes": pack_path.stat().st_size,
            "sha256": sha256_file(pack_path),
        },
        "summary": {
            "path": summary_path.name,
            "bytes": summary_path.stat().st_size,
            "sha256": sha256_file(summary_path),
        },
        "database_bytes": database_path.stat().st_size,
        "scratch_bytes_after_close": sum(
            path.stat().st_size for path in scratch.rglob("*") if path.is_file()
        ),
    }


def run_isolated_construction(
    transformed_path: Path,
    output_dir: Path,
    *,
    memory_limit: str,
    threads: int,
    allow_unpinned_duckdb: bool,
    max_rss_bytes: int = 4_294_967_296,
    max_scratch_bytes: int = 17_179_869_184,
    wall_seconds: float = 900,
) -> dict:
    """Run one DuckDB construction in a fresh, bounded process group."""

    script_dir = str(Path(__file__).resolve().parent)
    sys.path.insert(0, script_dir)
    try:
        import address_construction_v1 as construction
    finally:
        sys.path.pop(0)

    report_path = output_dir.parent / f".{output_dir.name}.child-report.json"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "construct-child",
        "--transformed",
        str(transformed_path),
        "--output-dir",
        str(output_dir),
        "--report",
        str(report_path),
        "--memory-limit",
        memory_limit,
        "--threads",
        str(threads),
    ]
    if allow_unpinned_duckdb:
        command.append("--allow-unpinned-duckdb")
    resource_evidence = construction.run_bounded(
        command,
        scratch_roots=[output_dir],
        limits=construction.Limits(
            max_rss_bytes=max_rss_bytes,
            max_scratch_bytes=max_scratch_bytes,
            wall_seconds=wall_seconds,
        ),
    )
    report = json.loads(report_path.read_text())
    report_path.unlink()
    if report["scratch_bytes_after_close"] != 0:
        raise ValueError("isolated DuckDB construction retained scratch after close")
    report["isolated_process"] = {
        **report.pop("child_process"),
        **resource_evidence,
        "exited_before_next_run": True,
    }
    return report


def construct_child(args: argparse.Namespace) -> None:
    import psutil

    process = psutil.Process(os.getpid())
    started_rss = process.memory_info().rss
    report = construct_packs(
        args.transformed,
        args.output_dir,
        memory_limit=args.memory_limit,
        threads=args.threads,
        allow_unpinned_duckdb=args.allow_unpinned_duckdb,
    )
    report["child_process"] = {
        "pid": os.getpid(),
        "process_group_id": os.getpgrp(),
        "started_rss_bytes": started_rss,
        "ended_rss_bytes": process.memory_info().rss,
    }
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def run(args: argparse.Namespace) -> dict:
    import pyarrow

    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    output_dir.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="address-spike-", dir=output_dir) as name:
        workspace = Path(name)
        hydrated = workspace / "hydrated.arrow"
        transformed = workspace / "transformed.arrow"
        transform_report = workspace / "transform-report.json"
        with PhaseRss() as hydration_rss:
            if args.experiment_input:
                hydration = hydrate_experiment_parquet(args.input, hydrated)
            else:
                hydration = hydrate_parquet(args.input, hydrated, args.batch_rows)
        hydration["rss"] = hydration_rss.evidence()
        with PhaseRss() as baseline_rss:
            baseline = run_legacy_baseline(
                hydrated,
                workspace / "legacy.parquet",
                output_dir / "phase-rss-diagnostic.json",
            )
        baseline["phase_rss"] = baseline_rss.evidence()
        transform = run_transform(
            args.binary.resolve(), hydrated, transformed, transform_report
        )
        second_transformed = workspace / "transformed-second.arrow"
        second_transform_report = workspace / "transform-report-second.json"
        second_transform = run_transform(
            args.binary.resolve(),
            hydrated,
            second_transformed,
            second_transform_report,
        )
        packs = run_isolated_construction(
            transformed,
            output_dir / "packs",
            memory_limit=args.memory_limit,
            threads=args.threads,
            allow_unpinned_duckdb=args.allow_unpinned_duckdb,
        )
        second_dir = output_dir / "determinism"
        second = run_isolated_construction(
            second_transformed,
            second_dir,
            memory_limit=args.memory_limit,
            threads=args.threads,
            allow_unpinned_duckdb=args.allow_unpinned_duckdb,
        )
        if (
            packs["isolated_process"]["pid"]
            == second["isolated_process"]["pid"]
            or packs["isolated_process"]["process_group_id"]
            == second["isolated_process"]["process_group_id"]
        ):
            raise ValueError("deterministic constructions reused a process group")
        determinism = {
            "logical_report": (
                transform["semantic_sum_a"] == second_transform["semantic_sum_a"]
                and transform["semantic_sum_b"] == second_transform["semantic_sum_b"]
                and transform["rejections_by_precedence"]
                == second_transform["rejections_by_precedence"]
            ),
            "transformed_ipc_sha256": sha256_file(transformed)
            == sha256_file(second_transformed),
            "pack_sha256": packs["pack"]["sha256"] == second["pack"]["sha256"],
            "summary_sha256": packs["summary"]["sha256"]
            == second["summary"]["sha256"],
        }
    report = {
        "schema": "overture-address-construction-spike-evidence-v1",
        "input": {
            "path": str(args.input.resolve()),
            "bytes": args.input.stat().st_size,
            "sha256": sha256_file(args.input),
        },
        "command": " ".join(os.sys.argv),
        "binary": {
            "path": str(args.binary.resolve()),
            "sha256": sha256_file(args.binary),
        },
        "runtime": {
            "python": platform.python_version(),
            "pyarrow": pyarrow.__version__,
            "platform": platform.platform(),
            "rustc": subprocess.run(
                ["rustc", "--version"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
        },
        "hydration": hydration,
        "legacy_baseline": baseline,
        "transform": transform,
        "construction": packs,
        "determinism": determinism,
        "second_construction_process": second["isolated_process"],
        "limitations": [
            "local fixture scale is not evidence for planet-scale RSS or runtime",
            "RSS polling may miss a sub-two-millisecond process peak",
        ],
    }
    report_path = output_dir / "evidence.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--input", type=Path, required=True)
    value.add_argument("--binary", type=Path, required=True)
    value.add_argument("--output-dir", type=Path, required=True)
    value.add_argument("--batch-rows", type=int, default=65_536)
    value.add_argument("--memory-limit", default="2GB")
    value.add_argument("--threads", type=int, default=2)
    value.add_argument("--allow-unpinned-duckdb", action="store_true")
    value.add_argument(
        "--experiment-input",
        action="store_true",
        help="columnarly adapt the checked-in pre-projection experiment sample",
    )
    return value


def child_parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--transformed", type=Path, required=True)
    value.add_argument("--output-dir", type=Path, required=True)
    value.add_argument("--report", type=Path, required=True)
    value.add_argument("--memory-limit", required=True)
    value.add_argument("--threads", type=int, required=True)
    value.add_argument("--allow-unpinned-duckdb", action="store_true")
    return value


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "construct-child":
        construct_child(child_parser().parse_args(sys.argv[2:]))
    else:
        result = run(parser().parse_args())
        print(json.dumps(result, indent=2, sort_keys=True))
