#!/usr/bin/env python3
"""Local construction-v1 Address map, genesis plan, and selective reducer.

Feature rows remain in Arrow, Rust, and DuckDB. Python coordinates bounded
stages and validates compact manifests/proof directories only.
"""

from __future__ import annotations

import argparse
import hashlib
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

# --------------------------------------------------------------------------- #
# The spatial cell scheme, shared with Places
# --------------------------------------------------------------------------- #
# The address family's FORWARD key is `(country, route_hash)` and is not spatial.
# Nothing below changes that: `address_key_hash`, `route_hash`, `hash_bucket`,
# TOTAL_ORDER, SERVING_ORDER, the pack layout, and the genesis partition plan are
# all untouched. These constants exist for ONE additive artifact -- the per-address
# records artifact -- whose only consumer is a future spatial reverse index.
# `partition_cell` here is a column on that artifact and never a routing key for
# `.aidx`/`.adat`.
#
# The scheme has to be bit-identical to the Places one, because one reverse
# pipeline is meant to serve both families off the same cell keys and the same
# bucket ranges. The authoritative definition of the cell is `route()` in
# `crates/geocoder-construction/src/bin/places_transform_v1.rs`; the shuffle is
# `shuffle_bucket`/`shuffle_bucket_sql` in `scripts/places_construction_v1.py`.
# Both are MIRRORED here rather than imported, so this module keeps no dependency
# on the Places plane -- and `tests/test_address_records_artifact.py` cross-checks
# every mirror against its original (the Rust binary for the cell, the Places
# module for the shuffle) so the two cannot drift silently.
CELL_GRID = 256
# Addresses carry only E7 fixed-point coordinates (`address-transform-v1` emits
# `longitude_e7`/`latitude_e7` as non-null Int32), so the cell is derived in EXACT
# INTEGER arithmetic over those, not in floating point. 360 degrees is 3.6e9 E7
# units and 180 degrees is 1.8e9, so a cell is a whole number of E7 units in both
# axes, so for any E7 input the integer floor equals the float floor `route()`
# computes. The parity test pins exactly that -- `route_e7(e7) == route(e7 / 1e7)`
# -- against the real binary at every one of the 257 x and 257 y boundaries and
# one E7 unit either side.
#
# What that does NOT claim: it is not "no rounding to reason about". There is a
# real quantization seam. An address whose RAW f64 position sits within about
# 5e-8 degrees of a cell boundary can round, at E7, to the other side of it, so
# its cell differs from the one the raw f64 would have produced. That is ~5 mm on
# the ground, it is inherent in the transform emitting E7 (which the serving
# encoder needs anyway), and the reverse design accepts it as a residual: the
# encoder, the verifier and the worker all key off the SAME E7 value, so nothing
# disagrees with anything -- the record is simply in the neighbouring cell, and a
# reverse query for a point 5 mm away reads that cell too.
LONGITUDE_E7_ORIGIN = 1_800_000_000
LATITUDE_E7_ORIGIN = 900_000_000
LONGITUDE_E7_PER_CELL = 2 * LONGITUDE_E7_ORIGIN // CELL_GRID  # 14,062,500
LATITUDE_E7_PER_CELL = 2 * LATITUDE_E7_ORIGIN // CELL_GRID  # 7,031,250
# Knuth multiplicative hash, floor(2^32 / phi), odd. Takes the HIGH bits: the
# partition key is `(y << 8) | x`, so the LOW bits of the product depend only on
# x and every cell in a longitude column would land in one bucket.
SHUFFLE_BUCKET_BITS = 8
SHUFFLE_MULTIPLIER = 2_654_435_761

ADDRESS_RECORDS_SCHEMA = "overture-address-map-address-records-v1"
ADDRESS_RECORDS_DIRECTORY_SCHEMA = "overture-address-map-address-records-directory-v1"
# The display projection the structured forward endpoint returns, so a reverse hit
# and a structured forward hit for the same feature render identically and reverse
# needs no secondary lookup. `address_levels` is carried whole rather than as two
# extracted admin levels because that is exactly what `address_serving_encode_v1`
# writes into the serving payload.
ADDRESS_RECORDS_DISPLAY_COLUMNS = (
    "country",
    "display_country",
    "postal_city",
    "postcode",
    "street",
    "number",
    "unit",
    "address_levels",
)
# Every admitted row is one record, identified by feature ID *and* source locator.
# Two admitted address rows can legitimately share a feature ID (the serving order
# breaks ties on the locator triple for exactly that reason), so this artifact is
# per-ROW with locator identity and never a GROUP BY on the ID -- aggregating
# would silently collapse two real addresses into one and reverse would lose one.
ADDRESS_RECORDS_ORDER = (
    "partition_cell, feature_id, source_object_index, source_row_group, "
    "source_row_index"
)


def route_e7(longitude_e7: int, latitude_e7: int) -> tuple[int, str]:
    """`(partition_key, partition_cell)` for E7 coordinates.

    Python mirror of `route()` in places_transform_v1.rs. Out-of-range coordinates
    clamp into the edge cell exactly as the Rust does; callers that must not
    tolerate them check `unroutable_e7` first.
    """
    x = min(
        CELL_GRID - 1,
        max(0, longitude_e7 + LONGITUDE_E7_ORIGIN) // LONGITUDE_E7_PER_CELL,
    )
    y = min(
        CELL_GRID - 1,
        max(0, latitude_e7 + LATITUDE_E7_ORIGIN) // LATITUDE_E7_PER_CELL,
    )
    return (y << 8) | x, f"{y:02x}{x:02x}"


def unroutable_e7(longitude_e7: int, latitude_e7: int) -> bool:
    """Whether E7 coordinates fall outside the world bounds.

    DEFENCE IN DEPTH, not a live filter. `address-transform-v1` already rejects an
    out-of-world coordinate as `invalid_geometry` (`parse_point`, main.rs:206-210
    checks `is_finite` and the +-180/+-90 ranges), so no admitted row should ever
    reach this. It is checked anyway because `Int32` E7 can REPRESENT +-214.7
    degrees, the check lives in a different language and repository layer from this
    one, and the consequence of a gap is silent: clamping would file an address at
    the antimeridian or a pole and reverse would confidently return it.

    Because the Rust check makes this unreachable, failing closed on it cannot
    abort a planet run over one bad source row -- the transform would have rejected
    that row first. Do not remove the Rust check on the grounds that this one
    exists: that WOULD turn a single bad row into a planet-map abort.
    """
    return (
        abs(longitude_e7) > LONGITUDE_E7_ORIGIN
        or abs(latitude_e7) > LATITUDE_E7_ORIGIN
    )


def route_e7_sql() -> tuple[str, str]:
    """DuckDB expressions for `(partition_key, partition_cell)`, mirroring route_e7.

    ``greatest(0, ...)`` then ``least(255, ...)`` reproduces the Rust
    ``clamp(0, 255)`` on both sides, and keeps the dividend non-negative so
    DuckDB's truncating ``//`` is a floor.
    """
    x = (
        f"least({CELL_GRID - 1}, greatest(0, longitude_e7::BIGINT + "
        f"{LONGITUDE_E7_ORIGIN}) // {LONGITUDE_E7_PER_CELL})"
    )
    y = (
        f"least({CELL_GRID - 1}, greatest(0, latitude_e7::BIGINT + "
        f"{LATITUDE_E7_ORIGIN}) // {LATITUDE_E7_PER_CELL})"
    )
    return f"(({y}) * 256 + ({x}))::UINTEGER", f"printf('%02x%02x', {y}, {x})"


def shuffle_bucket(partition_key: int, bits: int = SHUFFLE_BUCKET_BITS) -> int:
    """Python mirror of `shuffle_bucket_sql`, and of the Places implementation."""
    return ((partition_key * SHUFFLE_MULTIPLIER) % 4294967296) >> (32 - bits)


def shuffle_bucket_sql(
    key_expression: str = "partition_key", bits: int = SHUFFLE_BUCKET_BITS
) -> str:
    return (
        f"(((({key_expression})::UBIGINT * {SHUFFLE_MULTIPLIER}) % 4294967296) "
        f">> {32 - bits})::UINTEGER"
    )


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
    # Buckets the per-address records artifact is shuffled into. Same meaning and
    # same default as the Places `shuffle_bucket_bits`, so a reverse consumer can
    # own one bucket RANGE across both families. It bounds nothing about the
    # forward address packs.
    shuffle_bucket_bits: int = SHUFFLE_BUCKET_BITS

    def validate(self) -> None:
        numeric = (
            self.max_input_rows,
            self.max_pack_rows,
            self.parquet_row_group_rows,
            self.max_rss_bytes,
            self.max_scratch_bytes,
            self.max_output_bytes,
            self.max_serving_bytes,
            self.shuffle_bucket_bits,
        )
        if any(value <= 0 for value in numeric) or self.wall_seconds <= 0:
            raise ValueError("construction limits must be positive")
        if self.duckdb_threads <= 0:
            raise ValueError("DuckDB threads must be positive")
        # The shuffle takes `bits` HIGH bits of a 32-bit product, so anything above
        # 32 silently produces bucket 0 for every cell.
        if self.shuffle_bucket_bits > 24:
            raise ValueError("address records shuffle bucket bits are out of range")


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
            existing = target.read_bytes()
            if existing != payload:
                # Name the key and BOTH payloads. A marker is a create-only
                # completion claim, so this means one task slot was completed twice
                # with different results -- and the two most common causes are told
                # apart only by the payloads: a store reused across two producer
                # revisions (the fields differ) versus two jobs claiming one slot
                # (the artifact identity differs). The bare message forced that
                # diagnosis to be done by re-running with a patched exception.
                raise ValueError(
                    f"existing completion marker differs at {key}: this slot was "
                    "already completed with a different result. A marker is "
                    f"create-only. Existing {len(existing)} bytes "
                    f"sha256={hashlib.sha256(existing).hexdigest()}, new "
                    f"{len(payload)} bytes "
                    f"sha256={hashlib.sha256(payload).hexdigest()}. Existing "
                    f"payload: {existing.decode('utf-8', 'replace').strip()[:512]}"
                ) from None
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


def address_records_directory(
    parquet_path: Path, *, bucket: int, bits: int
) -> dict[str, Any]:
    """Row-group directory for one address-records pack, and its cell invariant.

    The forward packs get an exact two-lane binding from the Rust proof binary,
    which reads `semantic_digest_a`/`_b` off each row. Those digests bind the
    FORWARD payload -- the normalized keys, the locator, the display fields -- and
    this artifact's payload is a different projection with a derived spatial column,
    so reusing them would produce a proof frame that looks exact while binding
    something else. A records pack is therefore bound by its content hash
    (`put_content`) plus this directory: per-row-group record counts and per-cell
    record counts, which is what a reverse consumer needs to size a cell's shard
    and pick its sub-cell depth WITHOUT reading any data, and to check it read every
    row it was promised.

    It also enforces the property the shuffle exists for: every cell in the pack
    hashes to the pack's own bucket, so a cell is never split across buckets and
    one consumer holds a cell's complete data.
    """
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(parquet_path)
    row_groups = []
    totals: dict[str, int] = {}
    null_island = 0
    for index in range(parquet.metadata.num_row_groups):
        batch = parquet.read_row_group(
            index, columns=["partition_cell", "longitude_e7", "latitude_e7"]
        )
        counts: dict[str, int] = {}
        for cell in batch.column("partition_cell").to_pylist():
            if cell is None:
                raise ValueError("address records row carries no partition cell")
            counts[cell] = counts.get(cell, 0) + 1
            totals[cell] = totals.get(cell, 0) + 1
        longitudes = batch.column("longitude_e7").to_pylist()
        latitudes = batch.column("latitude_e7").to_pylist()
        for longitude, latitude in zip(longitudes, latitudes, strict=True):
            if longitude is None or latitude is None:
                raise ValueError("address records row carries no coordinate")
            if unroutable_e7(longitude, latitude):
                raise ValueError("address records row carries an unroutable coordinate")
            if longitude == 0 and latitude == 0:
                null_island += 1
        row_groups.append(
            {
                "index": index,
                "records": batch.num_rows,
                "cells": [
                    {"partition_cell": cell, "records": counts[cell]}
                    for cell in sorted(counts)
                ],
            }
        )
    for cell in totals:
        if shuffle_bucket(cell_partition_key(cell), bits) != bucket:
            raise ValueError("address records cell landed in the wrong shuffle bucket")
    return {
        "schema": ADDRESS_RECORDS_DIRECTORY_SCHEMA,
        "shuffle_bucket": bucket,
        "records": parquet.metadata.num_rows,
        # (0, 0) is a real cell (`8080`) and a well-known source defect, so it is
        # COUNTED and kept, never dropped: dropping it would break the equality
        # against the admitted count that this artifact's value rests on.
        "null_island_records": null_island,
        "row_groups": row_groups,
        "cells": [
            {"partition_cell": cell, "records": totals[cell]} for cell in sorted(totals)
        ],
    }


def cell_partition_key(cell: str) -> int:
    """(y<<8)|x for a `{y:02x}{x:02x}` partition cell, matching route_e7.

    Mirrors `places_construction_v1.cell_partition_key`; cross-checked against it.
    """
    if len(cell) != 4:
        raise ValueError(f"address partition cell is malformed: {cell!r}")
    return (int(cell[:2], 16) << 8) | int(cell[2:], 16)


def emit_address_records(
    connection: Any,
    *,
    source_table: str,
    workspace: Path,
    store: LocalObjectStore,
    limits: Limits,
    admitted_rows: int,
) -> dict[str, Any]:
    """Emit `overture-address-map-address-records-v1` from the transform table.

    One row per ADMITTED address -- feature ID, source locator, `partition_cell`,
    `partition_key`, the E7 coordinates, and the display projection the structured
    forward endpoint returns -- bucketed by the SAME shuffle Places uses, one pack
    per PRESENT bucket.

    Why it exists, and why here rather than later: the address map's forward packs
    are keyed by a ROW COUNTER (`row_number() // max_pack_rows`) and carry no
    spatial column at all, so no spatial index can ever be built from them. Adding
    a spatially keyed artifact after the planet address map has run means re-running
    the planet address map. Emitting it now makes an address reverse index purely
    additive (docs/plans/2026-07-25-reverse-v2-design.md, section 3).

    Why it is additive: it reads the already-materialised transform table and
    writes new objects. `address_key_hash`, `route_hash`, `hash_bucket`,
    TOTAL_ORDER, SERVING_ORDER, the forward pack layout and the genesis partition
    plan are all untouched, and every downstream phase reads `marker["packs"]`,
    never this. It is not the deferred forward-shuffle port
    (2026-07-24-construction-v1-follow-ups.md) and must not be used to start it.

    Why per-ROW and not a GROUP BY on the feature ID: two admitted rows can share
    a feature ID -- SERVING_ORDER breaks ties on the locator triple precisely
    because they can -- so aggregating would collapse two real addresses into one
    and reverse would silently lose one. The record count is therefore an EQUALITY
    against the transform's admitted rows, not an upper bound.
    """
    import pyarrow.parquet as pq

    bits = limits.shuffle_bucket_bits
    key_sql, cell_sql = route_e7_sql()
    # Fail closed BEFORE writing anything: an out-of-world coordinate has no
    # truthful cell, and the clamp that keeps the SQL total would file the address
    # at the antimeridian or a pole. Counted, named, and fatal -- never dropped.
    # Unreachable in practice, because `address-transform-v1` already rejects such
    # a row as `invalid_geometry` (see `unroutable_e7`); this is the second lock on
    # the same door, in a different layer.
    unroutable = connection.execute(
        f"SELECT count(*) FROM {source_table} WHERE "
        f"abs(longitude_e7::BIGINT) > {LONGITUDE_E7_ORIGIN} OR "
        f"abs(latitude_e7::BIGINT) > {LATITUDE_E7_ORIGIN}"
    ).fetchone()[0]
    if unroutable:
        raise ValueError(
            f"{unroutable} admitted address rows carry coordinates outside the world "
            "bounds and cannot be assigned a partition cell"
        )
    display = ", ".join(ADDRESS_RECORDS_DISPLAY_COLUMNS)
    # `partition_key` is deliberately NOT a column: it is `cell_partition_key` of
    # `partition_cell`, a pure 4-hex-character decode, so shipping it would be ~4
    # redundant bytes per row -- about 1.9 GB planet-wide -- for a value any reader
    # can derive. The Places positions packs dropped it for the same reason. It is
    # still computed here because the shuffle hashes it.
    projection = (
        f"SELECT feature_id, {cell_sql} AS partition_cell, "
        "longitude_e7, latitude_e7, source_object_index, source_row_group, "
        f"source_row_index, {display} FROM {source_table}"
    )
    # One tagged copy, then one file per present bucket, exactly as the Places
    # positions packs do. `COPY ... PARTITION_BY` cannot be used because DuckDB
    # does not preserve row order within a partition, and these packs are sorted.
    tagged = workspace / "address-records-tagged.parquet"
    connection.execute(
        # The bucket is derived from the same E7 columns the projection carries, so
        # the key never has to be materialised as a column to be hashed.
        f"COPY (SELECT *, {shuffle_bucket_sql(key_sql, bits)} AS records_bucket "
        f"FROM ({projection})) "
        f"TO '{tagged}' (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6, "
        f"ROW_GROUP_SIZE {limits.parquet_row_group_rows}, PARQUET_VERSION V2)"
    )
    records = pq.ParquetFile(tagged).metadata.num_rows
    if records != admitted_rows:
        raise ValueError("address records differ from the admitted row count")
    source = f"read_parquet('{tagged}')"
    present = [
        int(row[0])
        for row in connection.execute(
            f"SELECT DISTINCT records_bucket FROM {source} ORDER BY records_bucket"
        ).fetchall()
    ]
    packs = []
    output_bytes = 0
    null_island = 0
    for bucket in present:
        pack = workspace / f"address-records-{bucket:06d}.parquet"
        connection.execute(
            f"COPY (SELECT * EXCLUDE(records_bucket) FROM {source} "
            f"WHERE records_bucket = {bucket} ORDER BY {ADDRESS_RECORDS_ORDER}) "
            f"TO '{pack}' (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6, "
            f"ROW_GROUP_SIZE {limits.parquet_row_group_rows}, PARQUET_VERSION V2, "
            "PRESERVE_ORDER true)"
        )
        if pack.stat().st_size > limits.max_output_bytes:
            raise ValueError("address records pack exceeds its output cap")
        # Bound the AGGREGATE too, not just each pack. The forward path caps the
        # sum of its packs, and a per-pack-only cap is satisfied by any number of
        # just-under-cap packs -- so with 256 buckets the artifact could be 256x
        # the bound the cap appears to state.
        if output_bytes + pack.stat().st_size > limits.max_output_bytes:
            raise ValueError("address records output exceeded its hard cap in total")
        directory_value = address_records_directory(pack, bucket=bucket, bits=bits)
        directory_path = workspace / f"address-records-{bucket:06d}.directory.json"
        directory_path.write_text(json.dumps(directory_value, sort_keys=True) + "\n")
        pack_object = store.put_content(pack, "map/address/records", ".parquet")
        directory_object = store.put_content(
            directory_path, "map/address/record-directories", ".json"
        )
        null_island += directory_value["null_island_records"]
        output_bytes += pack_object["bytes"] + directory_object["bytes"]
        packs.append(
            {
                "pack_id": bucket,
                "shuffle_bucket": bucket,
                "records": directory_value["records"],
                "object": pack_object,
                "directory_object": directory_object,
                "directory": directory_value,
            }
        )
        pack.unlink(missing_ok=True)
        directory_path.unlink(missing_ok=True)
    tagged.unlink(missing_ok=True)
    if sum(item["records"] for item in packs) != records:
        raise ValueError("address records packs do not reconstruct the record count")
    return {
        "schema": ADDRESS_RECORDS_SCHEMA,
        "records": records,
        "admitted_rows": admitted_rows,
        "shuffle_bucket_bits": bits,
        "unroutable_records": 0,
        "null_island_records": null_island,
        "output_bytes": output_bytes,
        "packs": packs,
    }


def validate_address_records(
    marker: dict[str, Any], store: LocalObjectStore
) -> None:
    """A resumed marker must carry the address records artifact, intact.

    Without this, a marker written before the artifact existed resumes silently and
    one run mixes tasks that have records with tasks that do not -- so the planet
    reverse build would be short by whole map tasks with nothing failing.
    """
    records = marker.get("address_records")
    if (
        not isinstance(records, dict)
        or records.get("schema") != ADDRESS_RECORDS_SCHEMA
    ):
        raise ValueError(
            "Address marker is missing its per-address records artifact. A marker "
            "written before the artifact existed cannot be upgraded in place -- "
            "markers are write-once and this one is intact and self-consistent, it "
            "just predates the artifact. Remediation is to DELETE this task's "
            f"marker ({marker.get('task_id')!r}) from the store and re-run its map "
            "task; the forward packs are content-addressed, so the re-run publishes "
            "the identical bytes and only adds the records packs."
        )
    packs = records.get("packs")
    if not packs:
        raise ValueError("Address records artifact records no packs")
    for pack in packs:
        for identity in (pack["object"], pack["directory_object"]):
            path = store.path(identity["key"])
            if (
                not path.is_file()
                or path.stat().st_size != identity["bytes"]
                or sha256_file(path) != identity["sha256"]
            ):
                raise ValueError(
                    "Address immutable records object is missing or changed"
                )
        stored = json.loads(store.path(pack["directory_object"]["key"]).read_text())
        if stored != pack["directory"]:
            raise ValueError("Address marker embeds a different records directory")
        if pack["directory"]["shuffle_bucket"] != pack["shuffle_bucket"]:
            raise ValueError("Address records pack bucket differs from its directory")
    if sum(pack["records"] for pack in packs) != records["records"]:
        raise ValueError("Address records packs do not reconstruct the record count")
    if records["records"] != marker["transform"]["admitted_rows"]:
        raise ValueError("Address records differ from the admitted row count")


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
    validate_address_records(marker, store)
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
            # Additive, and inside the same watchdog so the records artifact is
            # bounded by the same RSS/scratch/wall caps as the forward packs. It
            # reads `packed` and writes new objects; the forward packs above are
            # already written and are not touched, which is why their bytes are
            # unchanged by this artifact existing.
            address_records = emit_address_records(
                connection,
                source_table="packed",
                workspace=workspace,
                store=store,
                limits=limits,
                admitted_rows=transform_report["admitted_rows"],
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
            # The per-address, spatially keyed records artifact. Deliberately NOT
            # inside "packs": every downstream phase (genesis_plan,
            # _accumulate_bucket_summaries, reduce_partition) iterates
            # marker["packs"], so keeping this separate is what makes the addition
            # invisible to the forward pipeline. Its bytes are reported under its
            # own key for the same reason -- "output_bytes" keeps meaning the
            # forward map output.
            "address_records": address_records,
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


def selected_row_groups(
    markers: list[dict[str, Any]], partition: dict[str, Any]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """The map row groups whose routing summary overlaps ``partition``'s hash range.

    Extracted from ``reduce_partition`` so a BATCH driver can ask which packs a
    partition will need WITHOUT reducing it: that is what lets a job hold a pack for
    the later partitions of its own batch instead of releasing and re-fetching it.
    Selection is unchanged -- one row group per (pack, group) pair, in marker order,
    which is also the order the fetched IPC stream and its proof directory are built
    in, so this must stay order-preserving.
    """
    groups: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for marker in markers:
        for pack in marker["packs"]:
            for group in pack["directory"]["row_groups"]:
                if any(
                    route["country"] == partition["country"]
                    and route["maximum_route_hash"] >= partition["hash_start"]
                    and route["minimum_route_hash"] <= partition["hash_end"]
                    for route in group["routing_groups"]
                ):
                    groups.append((pack, group))
    return groups


def partition_pack_keys(
    markers: list[dict[str, Any]], partition: dict[str, Any]
) -> set[str]:
    """The store keys ``partition`` will hydrate. See ``selected_row_groups``."""
    return {pack["object"]["key"] for pack, _ in selected_row_groups(markers, partition)}


def reduce_partition(
    *,
    partition: dict[str, Any],
    markers: list[dict[str, Any]],
    # Duck-typed on purpose, and annotated to say so: the hosted reduce job passes
    # a `construction_staging_v1.StagedObjectStore` (construction_v1_hosted._store
    # builds it family-agnostically and threads it into ADDRESS.reduce_partition),
    # not the `LocalObjectStore` this used to claim. The annotation mattered because
    # it hid the fact that this function is the consumer the staging transport's
    # `release()` was built for.
    store: Any,
    scratch_root: Path,
    directory_binary: Path,
    encoder_binary: Path,
    verifier_binary: Path,
    limits: Limits,
    query: list[str] | None = None,
    # Pack keys the LATER partitions of this reducer job will need. They are kept in
    # the local cache instead of being released, so a batched job fetches each pack
    # once rather than once per partition. Default None == "nothing follows me",
    # which is the correct behaviour for the single-partition path and for any
    # caller that does not batch.
    retain_keys: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    import duckdb
    import pyarrow.ipc as ipc
    import pyarrow.parquet as pq

    require_duckdb_runtime(duckdb, limits)

    selected_groups = selected_row_groups(markers, partition)
    if not selected_groups:
        raise ValueError("partition selected no map row groups")
    # Pack bodies are RELEASED from the local cache as soon as nothing still to be
    # reduced on this runner needs them, mirroring the places idiom
    # (places_construction_v1.adaptive_genesis_plan:1201,1226-1228). Without it this
    # reducer's PEAK resident bytes equal its TOTAL hydrated bytes -- it finishes
    # holding every pack it ever opened -- which is exactly the
    # whole-fan-in-on-one-runner shape the R2 staging transport exists to avoid, and
    # measurably was: 10,886,477 for both counters on a Seattle slice run.
    #
    # `release` is present only on the staged store; a local-only store has no such
    # method and must never be evicted, because there the local directory IS the
    # store. So the `getattr` guard is the whole idiom rather than defensive style
    # (construction_staging_v1.py:265-296).
    release = getattr(store, "release", None)
    # LAST use, not first. `selected_groups` is a list of (pack, group) pairs and one
    # pack contributes several row groups, so evicting after the first would force a
    # re-hydrate and re-verify of the same object for every remaining group of it.
    # Keyed by object key, so two markers naming the same content-addressed pack
    # collapse to one release at the later index.
    last_use = {
        pack["object"]["key"]: index for index, (pack, _) in enumerate(selected_groups)
    }
    retained = frozenset(retain_keys or ())
    # The hydrated cache is now under a DECLARED cap, not merely under a cache policy.
    # `release()` bounds the peak, but that bound is EMERGENT: it depends on how the
    # plan cut the partitions and on which packs a job's range happens to touch.
    # Peak resident is `(map tasks holding this partition's country) x pack bytes` and
    # is batch-INDEPENDENT above batch 1, so lowering `--max-reduce-jobs` -- which the
    # docs now recommend -- does not reduce it. On a planet address plan that is ~39
    # exact-US tasks (up to ~94 including mixed-country tasks) x ~104 MB, i.e. single-
    # digit GB. Without an enforced cap that lands as ENOSPC mid-reduce with no
    # diagnosis, on a run the plan phase certified.
    #
    # `resident_bytes` exists only on the staged store, where the local directory is a
    # CACHE. On a local-only store the directory IS the map output, so there is
    # nothing to cap and the check is correctly absent -- same discriminator as
    # `release` above.
    def resident_bytes() -> int | None:
        return getattr(store, "resident_bytes", None)

    def check_resident(stage: str) -> None:
        resident = resident_bytes()
        if resident is not None and resident > limits.max_scratch_bytes:
            raise ValueError(
                f"hydrated map packs resident on this runner ({resident} bytes) "
                f"exceed the stage scratch cap ({limits.max_scratch_bytes} bytes) "
                f"while {stage}. A reduce job holds one pack per map task holding "
                "its country; lower the partition count or raise max_scratch_bytes."
            )

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
                    pack_key = pack["object"]["key"]
                    pack_path = store.path(pack_key)
                    # Checked per fetch, not once at the end: the whole point is to
                    # abort with a diagnosis BEFORE the disk fills, and hydration is
                    # the only thing here that grows it.
                    check_resident("hydrating map packs")
                    # Unchanged: every fetch re-verifies the body against the digest
                    # the marker recorded, whether the body was already local or was
                    # re-hydrated after a release.
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
                    # The rows are now in the fetched IPC stream, so unless a LATER
                    # partition of this same job needs it, this pack's bytes are dead
                    # weight on the runner. Content-addressed and still in staging, so
                    # releasing is always safe: a later `path()` re-fetches and
                    # re-verifies it against the digest in its key.
                    if (
                        release is not None
                        and last_use[pack_key] == fetch_index
                        and pack_key not in retained
                    ):
                        release(pack_key)
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
        # The hydrated pack CACHE is a watchdog root too, not just the workspace. It
        # used to be outside every declared cap, so `peak_disk_bytes` under-reported
        # the job's real disk footprint by however much input it was holding -- on the
        # planet shape that is the larger of the two. Only when the store is a cache:
        # on a local-only store this directory is the map output itself, and counting
        # it would fail a legitimate run against a scratch cap it was never meant to
        # bound.
        watchdog_roots = [workspace]
        if release is not None:
            watchdog_roots.append(Path(store.root))
        with StageWatchdog(watchdog_roots, limits, connection) as watchdog:
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
    # Each reduction must carry the binding the PLAN recorded for ITS OWN
    # partition, not merely a binding that sums correctly with the others: two
    # partitions that published each other's rows leave the id set complete and
    # the sum untouched, so the sum below cannot see them. Ported from the places
    # validator, where the same hole was the reason `reconciles` was a literal.
    planned = {partition["id"]: partition["binding"] for partition in plan["partitions"]}
    for item in reductions:
        partition_id = item["partition"]["id"]
        if item["selected_binding"] != planned[partition_id]:
            raise ValueError(
                f"reduction binding for {partition_id} differs from the binding the "
                "genesis plan recorded for that partition"
            )
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
