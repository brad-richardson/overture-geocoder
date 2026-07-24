#!/usr/bin/env python3
"""Local construction-v1 Address map, genesis plan, and selective reducer.

Feature rows remain in Arrow, Rust, and DuckDB. Python coordinates bounded
stages and validates compact manifests/proof directories only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from stat import S_ISREG
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
SPIKE_SPEC = importlib.util.spec_from_file_location(
    "spike_address_construction", SCRIPT_DIR / "spike_address_construction.py"
)
assert SPIKE_SPEC and SPIKE_SPEC.loader
SPIKE = importlib.util.module_from_spec(SPIKE_SPEC)
SPIKE_SPEC.loader.exec_module(SPIKE)

MARKER_SCHEMA = "overture-address-construction-task-marker-v1"
PLAN_SCHEMA = "overture-address-genesis-partition-plan-v1"
REDUCE_SCHEMA = "overture-address-selective-reduce-v1"
BINDING_SCHEMA = "sha256-add-mod-2^256-two-domain-v1"
TOTAL_ORDER = SPIKE.TOTAL_ORDER
SERVING_ORDER = (
    "route_hash, normalized_key_0, normalized_key_1, normalized_key_2, "
    "normalized_key_3, normalized_key_4, normalized_key_5, normalized_key_6, "
    "normalized_key_7, feature_id, source_object_index, source_row_group, "
    "source_row_index"
)
UINT256 = 1 << 256


@dataclass(frozen=True)
class Limits:
    max_input_rows: int = 100_000
    max_pack_rows: int = 50_000
    parquet_row_group_rows: int = 8_192
    max_rss_bytes: int = 2 * 1024**3
    max_scratch_bytes: int = 4 * 1024**3
    max_output_bytes: int = 2 * 1024**3
    max_serving_bytes: int = 512 * 1024**2
    wall_seconds: float = 300.0
    duckdb_memory_limit: str = "1GB"
    duckdb_threads: int = 2
    required_duckdb_version: str = "1.5.1"
    allow_unpinned_duckdb: bool = False

    def validate(self) -> None:
        numeric = (
            self.max_input_rows,
            self.max_pack_rows,
            self.parquet_row_group_rows,
            self.max_rss_bytes,
            self.max_scratch_bytes,
            self.max_output_bytes,
            self.max_serving_bytes,
        )
        if any(value <= 0 for value in numeric) or self.wall_seconds <= 0:
            raise ValueError("construction limits must be positive")
        if self.duckdb_threads <= 0:
            raise ValueError("DuckDB threads must be positive")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def require_duckdb_runtime(duckdb, limits: Limits) -> None:
    if (
        duckdb.__version__ != limits.required_duckdb_version
        and not limits.allow_unpinned_duckdb
    ):
        raise RuntimeError(
            f"DuckDB {limits.required_duckdb_version} is required; found "
            f"{duckdb.__version__}"
        )


def sha256_file(path: Path) -> str:
    return SPIKE.sha256_file(path)


def zero_binding() -> dict[str, Any]:
    return {
        "records": 0,
        "semantic_sum_a": "0" * 64,
        "semantic_sum_b": "0" * 64,
    }


def validate_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "records",
        "semantic_sum_a",
        "semantic_sum_b",
    }:
        raise ValueError("semantic binding has the wrong shape")
    if type(value["records"]) is not int or value["records"] < 0:
        raise ValueError("semantic binding count is invalid")
    for name in ("semantic_sum_a", "semantic_sum_b"):
        digest = value[name]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("semantic binding lane is invalid")
    return value


def combine_bindings(values: list[dict[str, Any]]) -> dict[str, Any]:
    result = zero_binding()
    for value in values:
        validate_binding(value)
        result["records"] += value["records"]
        for name in ("semantic_sum_a", "semantic_sum_b"):
            result[name] = f"{(int(result[name], 16) + int(value[name], 16)) % UINT256:064x}"
    return result


class StageWatchdog:
    def __init__(self, roots: list[Path], limits: Limits, connection=None):
        self.roots = roots
        self.limits = limits
        self.connection = connection
        self.started = time.monotonic()
        self.peak_rss_bytes = 0
        self.peak_disk_bytes = 0
        self.failure: str | None = None
        self.finished = False
        self.process = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    @staticmethod
    def disk_bytes(roots: list[Path]) -> int:
        # Scratch churns continuously while the guarded stage runs: DuckDB
        # writes and unlinks spill blocks under the same workspace, and packs
        # are unlinked after upload. A path can therefore vanish between being
        # listed and being measured. That race is routine, so skip the entry
        # instead of letting the error reach the monitor loop, where it would
        # read as the watchdog failing to observe and abort a healthy stage.
        # One stat per path also avoids a second is_file() syscall.
        total = 0
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                try:
                    info = path.stat()
                except OSError:
                    continue
                if S_ISREG(info.st_mode):
                    total += info.st_size
        return total

    def _observe(self) -> None:
        self.peak_rss_bytes = max(self.peak_rss_bytes, self.process.memory_info().rss)
        self.peak_disk_bytes = max(self.peak_disk_bytes, self.disk_bytes(self.roots))
        if self.peak_rss_bytes > self.limits.max_rss_bytes:
            self.failure = "whole-stage RSS exceeded its hard cap"
        elif self.peak_disk_bytes > self.limits.max_scratch_bytes:
            self.failure = "whole-stage scratch exceeded its hard cap"
        elif time.monotonic() - self.started > self.limits.wall_seconds:
            self.failure = "whole-stage wall time exceeded its hard cap"

    def _abort(self) -> None:
        # Always called after self.failure is recorded, so a fault raised here
        # still surfaces through __exit__ rather than replacing the real cause.
        if self.connection is not None:
            self.connection.interrupt()

    def _run(self) -> None:
        # These caps are the only bound on the stage, so a monitor thread that
        # stops observing must fail the stage rather than let it run unguarded.
        # Exceptions raised here would otherwise die inside the daemon thread,
        # leaving __exit__ to report success and evidence() to report zero peaks.
        try:
            self._observe()
            while not self.failure and not self.stop.wait(0.01):
                self._observe()
        except BaseException as error:  # noqa: BLE001 - fail closed on any fault
            self.failure = f"stage watchdog stopped observing: {error!r}"
            self._abort()
            return
        if self.failure:
            self._abort()
            return
        self.finished = True

    def __enter__(self):
        import psutil

        # Import and attach on the caller's thread so a missing dependency or an
        # unreadable process fails loudly at the call site instead of silently
        # disabling the guard from inside the daemon thread.
        self.process = psutil.Process(os.getpid())
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        self.thread.join()
        self.peak_disk_bytes = max(
            self.peak_disk_bytes, self.disk_bytes(self.roots)
        )
        if self.failure is None and not self.finished:
            self.failure = "stage watchdog exited without recording an observation"
        if exc_type is None and self.failure:
            raise RuntimeError(self.failure)

    def evidence(self) -> dict[str, Any]:
        return {
            "peak_rss_bytes": self.peak_rss_bytes,
            "peak_scratch_and_output_bytes": self.peak_disk_bytes,
            "wall_seconds": time.monotonic() - self.started,
        }


def run_bounded(
    command: list[str], *, scratch_roots: list[Path], limits: Limits
) -> dict[str, Any]:
    import psutil

    started = time.monotonic()
    process = subprocess.Popen(command, start_new_session=os.name == "posix")
    observed = psutil.Process(process.pid)
    peak_rss = 0
    peak_disk = 0
    failure = None
    while process.poll() is None:
        try:
            processes = [observed]
            try:
                processes.extend(observed.children(recursive=True))
            except (psutil.Error, OSError):
                # Sandboxed macOS denies the process-table sysctl. The direct
                # child remains measurable and is still isolated in a process
                # group; hosted Linux measures the complete descendant tree.
                pass
            peak_rss = max(
                peak_rss,
                sum(item.memory_info().rss for item in processes if item.is_running()),
            )
        except (psutil.Error, OSError):
            pass
        peak_disk = max(peak_disk, StageWatchdog.disk_bytes(scratch_roots))
        elapsed = time.monotonic() - started
        if peak_rss > limits.max_rss_bytes:
            failure = "child RSS exceeded its hard cap"
        elif peak_disk > limits.max_scratch_bytes:
            failure = "child scratch exceeded its hard cap"
        elif elapsed > limits.wall_seconds:
            failure = "child wall time exceeded its hard cap"
        if failure:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()
            raise RuntimeError(failure)
        time.sleep(0.005)
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, process.args)
    return {
        "peak_rss_bytes": peak_rss,
        "peak_scratch_bytes": peak_disk,
        "wall_seconds": time.monotonic() - started,
    }


class LocalObjectStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        if key.startswith("/") or ".." in Path(key).parts:
            raise ValueError("object key escapes the local store")
        return self.root / key

    def put_content(self, source: Path, prefix: str, suffix: str) -> dict[str, Any]:
        digest = sha256_file(source)
        key = f"{prefix}/sha256/{digest}{suffix}"
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=f".{target.name}.", suffix=".pending", dir=target.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.stat().st_size != source.stat().st_size or sha256_file(target) != digest:
                raise ValueError("existing immutable object differs from its identity")
        finally:
            temporary.unlink(missing_ok=True)
        if target.stat().st_size != source.stat().st_size or sha256_file(target) != digest:
            raise ValueError("published immutable object differs from its identity")
        return {"key": key, "bytes": source.stat().st_size, "sha256": digest}

    def read_json(self, key: str) -> dict[str, Any] | None:
        path = self.path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def write_marker_last(self, key: str, value: dict[str, Any]) -> None:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = canonical_json(value) + b"\n"
        with tempfile.NamedTemporaryFile(
            prefix=f".{target.name}.", suffix=".complete", dir=target.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != payload:
                raise ValueError("existing completion marker differs")
        finally:
            temporary.unlink(missing_ok=True)


def write_arrow_query(connection, sql: str, output: Path, batch_rows: int) -> int:
    import pyarrow.ipc as ipc

    reader = connection.execute(sql).fetch_record_batch(batch_rows)
    rows = 0
    with output.open("wb") as destination:
        writer = ipc.new_stream(destination, reader.schema)
        try:
            for batch in reader:
                writer.write_batch(batch)
                rows += batch.num_rows
        finally:
            writer.close()
    return rows


def parquet_layout(path: Path) -> dict[str, Any]:
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(path)
    return {
        "row_groups": [
            {"index": index, "records": parquet.metadata.row_group(index).num_rows}
            for index in range(parquet.metadata.num_row_groups)
        ]
    }


def proof_directory(
    binary: Path,
    arrow_path: Path,
    layout: dict[str, Any],
    output: Path,
    limits: Limits,
    roots: list[Path],
) -> tuple[dict[str, Any], dict[str, Any]]:
    layout_path = output.with_suffix(".layout.json")
    layout_path.write_text(json.dumps(layout, sort_keys=True) + "\n")
    evidence = run_bounded(
        [
            str(binary),
            "--input",
            str(arrow_path),
            "--layout",
            str(layout_path),
            "--output",
            str(output),
        ],
        scratch_roots=roots,
        limits=limits,
    )
    directory = json.loads(output.read_text())
    if directory.get("schema") != "overture-address-pack-proof-directory-v1":
        raise ValueError("Rust proof directory has the wrong schema")
    validate_binding(directory["binding"])
    return directory, evidence


def transform(
    binary: Path,
    hydrated: Path,
    source_limits: Path,
    transformed: Path,
    report_path: Path,
    limits: Limits,
    roots: list[Path],
) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = run_bounded(
        [
            str(binary),
            "--input",
            str(hydrated),
            "--output",
            str(transformed),
            "--report",
            str(report_path),
            "--source-limits",
            str(source_limits),
        ],
        scratch_roots=roots,
        limits=limits,
    )
    report = json.loads(report_path.read_text())
    if report["input_rows"] != report["admitted_rows"] + report["rejected_rows"]:
        raise ValueError("transform accounting does not reconcile")
    return report, evidence


def marker_key(task_id: str) -> str:
    if not task_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in task_id):
        raise ValueError("task ID is not a safe canonical component")
    return f"map/address/tasks/{task_id}/complete.json"


def validate_marker(
    marker: dict[str, Any], *, request_sha256: str, task_id: str, store: LocalObjectStore
) -> dict[str, Any]:
    if (
        marker.get("schema") != MARKER_SCHEMA
        or marker.get("request_sha256") != request_sha256
        or marker.get("task_id") != task_id
        or marker.get("binding_schema") != BINDING_SCHEMA
        or not marker.get("packs")
    ):
        raise ValueError("Address construction marker identity is invalid")
    validate_binding(marker.get("binding"))
    for pack in marker["packs"]:
        for identity in (pack["object"], pack["directory_object"]):
            path = store.path(identity["key"])
            if (
                not path.is_file()
                or path.stat().st_size != identity["bytes"]
                or sha256_file(path) != identity["sha256"]
            ):
                raise ValueError("Address marker immutable object is missing or changed")
        stored_directory = json.loads(
            store.path(pack["directory_object"]["key"]).read_text()
        )
        if stored_directory != pack["directory"]:
            raise ValueError("Address marker embeds a different proof directory")
        validate_binding(pack["directory"]["binding"])
    if combine_bindings(
        [pack["directory"]["binding"] for pack in marker["packs"]]
    ) != marker["binding"]:
        raise ValueError("Address marker pack bindings do not reconcile")
    return marker


def map_task(
    *,
    input_path: Path,
    source_limits: Path,
    store: LocalObjectStore,
    scratch_root: Path,
    request_sha256: str,
    task_id: str,
    transform_binary: Path,
    directory_binary: Path,
    limits: Limits,
    failpoint: str | None = None,
) -> dict[str, Any]:
    import duckdb
    import pyarrow.ipc as ipc
    import pyarrow.parquet as pq

    limits.validate()
    require_duckdb_runtime(duckdb, limits)
    if (
        len(request_sha256) != 64
        or any(character not in "0123456789abcdef" for character in request_sha256)
    ):
        raise ValueError("request SHA-256 is not canonical")
    existing = store.read_json(marker_key(task_id))
    if existing is not None:
        result = validate_marker(
            existing, request_sha256=request_sha256, task_id=task_id, store=store
        )
        return {**result, "admitted_existing": True}
    parquet = pq.ParquetFile(input_path)
    if parquet.metadata.num_rows > limits.max_input_rows:
        raise ValueError("Address input exceeds its hard row cap")
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"address-{task_id}-", dir=scratch_root) as name:
        workspace = Path(name)
        hydrated = workspace / "hydrated.arrow"
        transformed = workspace / "transformed.arrow"
        transform_report_path = workspace / "transform.json"
        hydration_report = workspace / "hydration.json"
        hydration_evidence = run_bounded(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "hydrate",
                "--input",
                str(input_path),
                "--output",
                str(hydrated),
                "--report",
                str(hydration_report),
                "--batch-rows",
                "65536",
            ],
            scratch_roots=[workspace],
            limits=limits,
        )
        hydration = json.loads(hydration_report.read_text())
        transform_report, transform_evidence = transform(
            transform_binary,
            hydrated,
            source_limits,
            transformed,
            transform_report_path,
            limits,
            [workspace],
        )
        # Fail closed on ANY invalid_source_locator: a correctly report-derived
        # source-limits bound never rejects a real projected locator, so a
        # non-zero count means the limits or projected locators are inconsistent
        # (the silent wrong-bytes class the old row_groups:1 defect exposed).
        if transform_report.get("rejections_by_precedence", {}).get(
            "invalid_source_locator", 0
        ):
            raise ValueError(
                "address transform rejected projected locators as invalid_source_locator; "
                "source-limits and projected row-group/row indices are inconsistent"
            )
        database_path = workspace / "construction.duckdb"
        duckdb_scratch = workspace / "duckdb-spill"
        duckdb_scratch.mkdir()
        connection = duckdb.connect(str(database_path))
        connection.execute(f"SET memory_limit = '{limits.duckdb_memory_limit}'")
        connection.execute(f"SET threads = {limits.duckdb_threads}")
        connection.execute(f"SET temp_directory = '{duckdb_scratch}'")
        with transformed.open("rb") as source:
            table = ipc.open_stream(source).read_all()
            connection.register("transformed_arrow", table)
            connection.execute(
                f"""
                CREATE TABLE packed AS
                SELECT *, ((row_number() OVER (ORDER BY {TOTAL_ORDER}) - 1)
                    // {limits.max_pack_rows})::UINTEGER AS pack_id
                FROM transformed_arrow
                """
            )
            connection.unregister("transformed_arrow")
        pack_count = connection.execute(
            "SELECT coalesce(max(pack_id) + 1, 0)::UINTEGER FROM packed"
        ).fetchone()[0]
        packs = []
        with StageWatchdog([workspace], limits, connection) as watchdog:
            connection.execute("SET threads = 1")
            for pack_id in range(pack_count):
                pack = workspace / f"pack-{pack_id:06d}.parquet"
                connection.execute(
                    f"""
                    COPY (
                        SELECT * EXCLUDE (pack_id) FROM packed
                        WHERE pack_id = {pack_id}
                        ORDER BY {TOTAL_ORDER}
                    ) TO '{pack}' (
                        FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6,
                        ROW_GROUP_SIZE {limits.parquet_row_group_rows},
                        PARQUET_VERSION V2, PRESERVE_ORDER true
                    )
                    """
                )
                ordered = workspace / f"pack-{pack_id:06d}.arrow"
                rows = write_arrow_query(
                    connection,
                    f"SELECT * EXCLUDE (pack_id) FROM packed WHERE pack_id = {pack_id} ORDER BY {TOTAL_ORDER}",
                    ordered,
                    65_536,
                )
                layout = parquet_layout(pack)
                if rows != sum(item["records"] for item in layout["row_groups"]):
                    raise ValueError("pack Arrow/Parquet row counts differ")
                directory_path = workspace / f"pack-{pack_id:06d}.directory.json"
                directory, directory_evidence = proof_directory(
                    directory_binary,
                    ordered,
                    layout,
                    directory_path,
                    limits,
                    [workspace],
                )
                pack_object = store.put_content(pack, "map/address/packs", ".parquet")
                directory_object = store.put_content(
                    directory_path, "map/address/directories", ".json"
                )
                packs.append(
                    {
                        "pack_id": pack_id,
                        "object": pack_object,
                        "directory_object": directory_object,
                        "directory": directory,
                        "proof_evidence": directory_evidence,
                    }
                )
        construction_evidence = watchdog.evidence()
        connection.close()
        if failpoint in {"after_objects", "before_marker"}:
            raise RuntimeError(f"injected interruption: {failpoint}")
        binding = combine_bindings([item["directory"]["binding"] for item in packs])
        if binding["records"] != transform_report["admitted_rows"]:
            raise ValueError("pack binding count differs from transform admission")
        if (
            binding["semantic_sum_a"] != transform_report["semantic_sum_a"]
            or binding["semantic_sum_b"] != transform_report["semantic_sum_b"]
        ):
            raise ValueError("pack binding differs from transform binding")
        output_bytes = sum(
            item[field]["bytes"]
            for item in packs
            for field in ("object", "directory_object")
        )
        if output_bytes > limits.max_output_bytes:
            raise ValueError("Address map output exceeded its hard cap")
        marker = {
            "schema": MARKER_SCHEMA,
            "binding_schema": BINDING_SCHEMA,
            "request_sha256": request_sha256,
            "task_id": task_id,
            "input": {
                "sha256": sha256_file(input_path),
                "bytes": input_path.stat().st_size,
                "records": parquet.metadata.num_rows,
            },
            "source_limits_sha256": sha256_file(source_limits),
            "limits": asdict(limits),
            "runtime": {
                "duckdb": duckdb.__version__,
                "pinned_duckdb": duckdb.__version__
                == limits.required_duckdb_version,
                "transform_sha256": sha256_file(transform_binary),
                "directory_sha256": sha256_file(directory_binary),
            },
            "hydration": hydration,
            "hydration_evidence": hydration_evidence,
            "transform": transform_report,
            "transform_evidence": transform_evidence,
            "construction_evidence": construction_evidence,
            "pack_plan": {
                "kind": "sorted-fixed-row-count-v1",
                "max_pack_rows": limits.max_pack_rows,
                "packs": pack_count,
            },
            "packs": packs,
            "binding": binding,
            "output_bytes": output_bytes,
        }
        store.write_marker_last(marker_key(task_id), marker)
        return {**marker, "admitted_existing": False}


def _accumulate_bucket_summaries(
    marker: dict[str, Any], summaries: dict[tuple[str, int], dict[str, Any]]
) -> None:
    """Fold one marker's pack bucket summaries into the running aggregate.

    The aggregate holds one binding per ``(country, maximum_bucket)`` identity —
    bounded by ~200 countries x 65,536 buckets — instead of materializing every
    per-pack binding. Because ``combine_bindings`` is an associative, commutative
    modular sum, folding entries in one at a time yields the identical binding a
    single combine over the full list would produce.
    """
    for pack in marker["packs"]:
        for item in pack["directory"]["bucket_summaries"]:
            key = (item["country"], item["maximum_bucket"])
            current = summaries.get(key)
            summaries[key] = combine_bindings(
                [item["binding"]] if current is None else [current, item["binding"]]
            )


def _plan_from_summaries(
    summaries: dict[tuple[str, int], dict[str, Any]],
    expected: dict[str, Any],
    row_cap: int,
) -> dict[str, Any]:
    """Bisect the bounded per-(country, bucket) aggregate into a genesis plan.

    ``summaries`` and ``expected`` fully determine the output, so this is shared
    verbatim by the in-memory and streaming entry points.
    """
    partitions = []

    def emit(country: str, prefix: int, bits: int, entries: list[tuple[int, dict]]) -> None:
        binding = combine_bindings([item[1] for item in entries])
        if binding["records"] <= row_cap:
            remaining = 64 - bits
            start = prefix << remaining if bits else 0
            end = start + (1 << remaining) - 1
            partitions.append(
                {
                    "id": f"a-{country}" if bits == 0 else f"a-{country}-h-{prefix:0{bits}b}",
                    "country": country,
                    "hash_bits": bits,
                    "hash_prefix": f"{prefix:0{bits}b}" if bits else "",
                    "hash_start": start,
                    "hash_end": end,
                    "binding": binding,
                }
            )
            return
        if bits == 16:
            raise ValueError("Address genesis partition exceeds cap at construction ceiling")
        for bit in (0, 1):
            child_prefix = (prefix << 1) | bit
            shift = 16 - (bits + 1)
            child = [
                item for item in entries if item[0] >> shift == child_prefix
            ]
            if child:
                emit(country, child_prefix, bits + 1, child)

    countries = sorted({country for country, _ in summaries})
    for country in countries:
        emit(
            country,
            0,
            0,
            sorted(
                ((bucket, binding) for (name, bucket), binding in summaries.items() if name == country)
            ),
        )
    total = combine_bindings([partition["binding"] for partition in partitions])
    if total != expected:
        raise ValueError("genesis partition bindings do not cover map output exactly")
    return {
        "schema": PLAN_SCHEMA,
        "maximum_hash_bits": 16,
        "row_cap": row_cap,
        "partitions": partitions,
        "binding": total,
    }


def genesis_plan(markers: list[dict[str, Any]], *, row_cap: int) -> dict[str, Any]:
    if row_cap <= 0:
        raise ValueError("partition row cap must be positive")
    summaries: dict[tuple[str, int], dict[str, Any]] = {}
    expected = zero_binding()
    for marker in markers:
        _accumulate_bucket_summaries(marker, summaries)
        expected = combine_bindings([expected, marker["binding"]])
    return _plan_from_summaries(summaries, expected, row_cap)


def genesis_plan_streaming(marker_paths, *, row_cap: int) -> dict[str, Any]:
    """Plan from marker files read one at a time, never holding them all at once.

    Produces output byte-identical to ``genesis_plan`` on the same markers, but
    only the bounded per-(country, bucket) aggregate and a single decoded marker
    live in memory at any moment. This is the planet-scale entry point; the
    in-memory ``genesis_plan`` stays for small inputs and existing callers.
    """
    if row_cap <= 0:
        raise ValueError("partition row cap must be positive")
    summaries: dict[tuple[str, int], dict[str, Any]] = {}
    expected = zero_binding()
    for path in marker_paths:
        marker = json.loads(Path(path).read_text())
        _accumulate_bucket_summaries(marker, summaries)
        expected = combine_bindings([expected, marker["binding"]])
        del marker
    return _plan_from_summaries(summaries, expected, row_cap)


def export_filter(
    connection,
    table: str,
    predicate: str,
    output: Path,
    order: str,
) -> int:
    return write_arrow_query(
        connection,
        f"SELECT * FROM {table} WHERE {predicate} ORDER BY {order}",
        output,
        65_536,
    )


def binding_for_arrow(
    directory_binary: Path,
    path: Path,
    records: int,
    output: Path,
    limits: Limits,
    roots: list[Path],
) -> tuple[dict[str, Any], dict[str, Any]]:
    layout = {"row_groups": [] if records == 0 else [{"index": 0, "records": records}]}
    return proof_directory(directory_binary, path, layout, output, limits, roots)


def reduce_partition(
    *,
    partition: dict[str, Any],
    markers: list[dict[str, Any]],
    store: LocalObjectStore,
    scratch_root: Path,
    directory_binary: Path,
    encoder_binary: Path,
    verifier_binary: Path,
    limits: Limits,
    query: list[str] | None = None,
) -> dict[str, Any]:
    import duckdb
    import pyarrow.ipc as ipc
    import pyarrow.parquet as pq

    require_duckdb_runtime(duckdb, limits)

    selected_groups = []
    for marker in markers:
        for pack in marker["packs"]:
            for group in pack["directory"]["row_groups"]:
                if any(
                    route["country"] == partition["country"]
                    and route["maximum_route_hash"] >= partition["hash_start"]
                    and route["minimum_route_hash"] <= partition["hash_end"]
                    for route in group["routing_groups"]
                ):
                    selected_groups.append((pack, group))
    if not selected_groups:
        raise ValueError("partition selected no map row groups")
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"reduce-{partition['id']}-", dir=scratch_root) as name:
        workspace = Path(name)
        fetched = workspace / "fetched.arrow"
        expected_groups = []
        rows = 0
        schema = None
        with fetched.open("wb") as destination:
            writer = None
            try:
                for fetch_index, (pack, group) in enumerate(selected_groups):
                    pack_path = store.path(pack["object"]["key"])
                    if sha256_file(pack_path) != pack["object"]["sha256"]:
                        raise ValueError("selective reducer pack SHA differs")
                    table = pq.ParquetFile(pack_path).read_row_group(group["index"])
                    schema = table.schema
                    if writer is None:
                        writer = ipc.new_stream(destination, schema)
                    writer.write_table(table)
                    rows += table.num_rows
                    expected_groups.append(
                        {"index": fetch_index, "records": table.num_rows}
                    )
            finally:
                if writer is not None:
                    writer.close()
        actual_directory, fetched_evidence = proof_directory(
            directory_binary,
            fetched,
            {"row_groups": expected_groups},
            workspace / "fetched-directory.json",
            limits,
            [workspace],
        )
        if len(actual_directory["row_groups"]) != len(selected_groups):
            raise ValueError("selective row-group directory count differs")
        for actual, (_, expected) in zip(
            actual_directory["row_groups"], selected_groups, strict=True
        ):
            if actual["binding"] != expected["binding"] or actual["routing_groups"] != expected[
                "routing_groups"
            ]:
                raise ValueError("selected row group differs from its map proof")
        connection = duckdb.connect(str(workspace / "reduce.duckdb"))
        connection.execute(f"SET memory_limit = '{limits.duckdb_memory_limit}'")
        connection.execute(f"SET threads = {limits.duckdb_threads}")
        connection.execute(f"SET temp_directory = '{workspace / 'duckdb-spill'}'")
        with fetched.open("rb") as source:
            table = ipc.open_stream(source).read_all()
            connection.register("fetched_arrow", table)
            connection.execute("CREATE TABLE fetched AS SELECT * FROM fetched_arrow")
            connection.unregister("fetched_arrow")
        predicate = (
            f"country = '{partition['country']}' AND route_hash BETWEEN "
            f"{partition['hash_start']}::UBIGINT AND {partition['hash_end']}::UBIGINT"
        )
        selected = workspace / "selected.arrow"
        discarded = workspace / "discarded.arrow"
        with StageWatchdog([workspace], limits, connection) as watchdog:
            selected_rows = export_filter(
                connection, "fetched", predicate, selected, SERVING_ORDER
            )
            discarded_rows = export_filter(
                connection, "fetched", f"NOT ({predicate})", discarded, TOTAL_ORDER
            )
        reduce_evidence = watchdog.evidence()
        connection.close()
        if selected_rows + discarded_rows != rows:
            raise ValueError("selected and discarded row counts do not reconcile")
        selected_directory, selected_evidence = binding_for_arrow(
            directory_binary,
            selected,
            selected_rows,
            workspace / "selected-directory.json",
            limits,
            [workspace],
        )
        discarded_directory, discarded_evidence = binding_for_arrow(
            directory_binary,
            discarded,
            discarded_rows,
            workspace / "discarded-directory.json",
            limits,
            [workspace],
        )
        if combine_bindings(
            [selected_directory["binding"], discarded_directory["binding"]]
        ) != actual_directory["binding"]:
            raise ValueError("selected/discarded bindings do not reconcile fetched rows")
        if selected_directory["binding"] != partition["binding"]:
            raise ValueError("selected binding differs from genesis partition ownership")
        artifact = workspace / f"{partition['id']}.av1"
        encode_evidence = run_bounded(
            [
                str(encoder_binary),
                "--input",
                str(selected),
                "--output",
                str(artifact),
                "--max-output-bytes",
                str(limits.max_serving_bytes),
            ],
            scratch_roots=[workspace],
            limits=limits,
        )
        query_path = workspace / "query.json"
        verifier_report = workspace / "verification.json"
        command = [
            str(verifier_binary),
            "--input",
            str(artifact),
            "--output",
            str(verifier_report),
            "--max-input-bytes",
            str(limits.max_serving_bytes),
        ]
        if query is not None:
            query_path.write_text(json.dumps(query) + "\n")
            command.extend(["--query-json", str(query_path)])
        verify_evidence = run_bounded(
            command, scratch_roots=[workspace], limits=limits
        )
        verification = json.loads(verifier_report.read_text())
        if verification["binding"] != partition["binding"]:
            raise ValueError("serving verifier binding differs from partition")
        artifact_object = store.put_content(
            artifact, "reduce/address/artifacts", ".av1"
        )
        return {
            "schema": REDUCE_SCHEMA,
            "partition": partition,
            "selected_row_groups": len(selected_groups),
            "fetched_binding": actual_directory["binding"],
            "selected_binding": selected_directory["binding"],
            "discarded_binding": discarded_directory["binding"],
            "artifact": artifact_object,
            "verification": verification,
            "evidence": {
                "fetched": fetched_evidence,
                "reduce": reduce_evidence,
                "selected": selected_evidence,
                "discarded": discarded_evidence,
                "encode": encode_evidence,
                "verify": verify_evidence,
            },
        }


def validate_complete_reduction(
    plan: dict[str, Any], reductions: list[dict[str, Any]]
) -> dict[str, Any]:
    expected_ids = [partition["id"] for partition in plan["partitions"]]
    actual_ids = [item["partition"]["id"] for item in reductions]
    if len(actual_ids) != len(set(actual_ids)) or sorted(actual_ids) != sorted(expected_ids):
        raise ValueError("reduction has missing, extra, or duplicate partitions")
    binding = combine_bindings([item["selected_binding"] for item in reductions])
    if binding != plan["binding"]:
        raise ValueError("complete reduction binding differs from genesis plan")
    return {"partitions": len(reductions), "binding": binding, "reconciles": True}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    subcommands = value.add_subparsers(dest="command", required=True)
    hydrate = subcommands.add_parser("hydrate")
    hydrate.add_argument("--input", type=Path, required=True)
    hydrate.add_argument("--output", type=Path, required=True)
    hydrate.add_argument("--report", type=Path, required=True)
    hydrate.add_argument("--batch-rows", type=int, required=True)
    subcommands.add_parser("version")
    return value


if __name__ == "__main__":
    arguments = parser().parse_args()
    if arguments.command == "version":
        print("global-v2-construction-v1")
    elif arguments.command == "hydrate":
        hydration = SPIKE.hydrate_parquet(
            arguments.input, arguments.output, arguments.batch_rows
        )
        arguments.report.write_text(
            json.dumps(hydration, sort_keys=True, indent=2) + "\n"
        )
