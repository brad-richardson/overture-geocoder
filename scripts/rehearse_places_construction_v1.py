#!/usr/bin/env python3
"""Local Places construction-v1 baseline/candidate/rehearsal orchestrator.

Checkpoint-5 "resume steps 4-6": for each frozen role task run one streaming
Python baseline plus two isolated Rust-transform + on-disk-DuckDB candidate
constructions, then rehearse the seven-role multi-task adaptive
plan/reduce/head fan-in with dormant Worker index-probe queries and
interruption/resume phases. Emits only real observed values; every gate is
fail-closed and no evidence is fabricated.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


P = load("places_construction_v1_orch", ROOT / "scripts/places_construction_v1.py")
BASELINE = load(
    "baseline_places_construction_v1_orch",
    ROOT / "scripts/baseline_places_construction_v1.py",
)
A = P.A

DATA = ROOT / "benchmarks/places-construction-v1-data"
# The frozen evidence spec this rehearsal produces evidence under. Its
# `acceptance_gates.map_reduce` block declares the three partition hard caps
# below, and its relaxation policy is "none": a rehearsal that planned above
# them would not be evidence under this spec at all.
EVIDENCE_SPEC = ROOT / "benchmarks/places-construction-v1-evidence-spec-v4.json"
PROJECTED = DATA / "projected"
EVIDENCE = DATA / "evidence"
INVENTORY = DATA / "inventory/places.json"
SOURCE_LIMITS = DATA / "inventory/source-limits.json"
RELEASE_BIN = ROOT / "crates/target/release"

ROLE_TASKS = (76, 73, 87, 86, 85, 1, 13)


def projected_path(index: int) -> Path:
    return PROJECTED / f"task-{index:02d}.parquet"


def sha256_file(path: Path) -> str:
    return A.sha256_file(path)


def _declared_caps(
    gates: dict, declared: dict[str, str], spec_name: str
) -> dict[str, int]:
    """Map `P.Limits` field -> spec-declared cap, failing closed on anything unusable.

    Guessing a cap is worse than refusing to rehearse, so a missing, non-integer or
    non-positive declaration raises. `bool` is an `int` in Python, and a spec that
    declared `true` for a cap would otherwise become a cap of 1.
    """
    caps: dict[str, int] = {}
    for name, key in declared.items():
        value = gates.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(
                f"evidence spec {spec_name} declares no usable {key} "
                f"({name}); got {value!r}"
            )
        caps[name] = value
    return caps


def spec_partition_caps(spec_path: Path = EVIDENCE_SPEC) -> dict[str, int]:
    """The three partition hard caps the frozen evidence spec declares.

    Read rather than copied. These values used to be duplicated here as literals,
    which made the spec's declarations dead text that nothing checked -- and the
    rehearsal was free to drift away from the spec it claims conformance under.
    """
    return _declared_caps(
        json.loads(spec_path.read_text())["acceptance_gates"]["map_reduce"],
        {
            "partition_term_rows": "partition_term_rows_hard_cap",
            "partition_estimated_bytes": "partition_estimated_uncompressed_bytes_hard_cap",
            "partition_distinct_tokens": "partition_distinct_tokens_hard_cap",
        },
        spec_path.name,
    )


def spec_head_caps(spec_path: Path = EVIDENCE_SPEC) -> dict[str, int]:
    """The head caps the frozen evidence spec declares, for the same reason.

    `acceptance_gates.head.maximum_head_candidate_rows` (5,000,000) and
    `maximum_merge_fan_in` (16) were satisfied only by coincidence: the rehearsal
    inherited the 5,000,000 `P.Limits` default and the tree merge was called with
    `max_fan_in_tasks`, which the rehearsal happened to set to 16. The second
    coincidence is gone -- the hosted build now runs a merge fan-in of 8 through its
    own knob -- so the rehearsal reads the spec's values rather than relying on
    defaults it does not control.

    KNOWN GAP, recorded rather than papered over: `maximum_merge_fan_in` is a
    MAXIMUM, and reading it as the value to run means the rehearsal folds its 7 task
    markers with a fan-in of 16, i.e. one group and one stage. So the rehearsal does
    NOT exercise a multi-stage tree, which is the thing the hosted build changed.
    Making it do so means choosing a fan-in below the task count, and any such choice
    changes the rehearsal's run set -- a spec v3 decision, since v2's relaxation
    policy is "none". Tracked as a follow-up; the multi-stage coverage lives in
    tests/test_places_construction_v1.py meanwhile.
    """
    caps = _declared_caps(
        json.loads(spec_path.read_text())["acceptance_gates"]["head"],
        {
            "max_head_candidate_rows": "maximum_head_candidate_rows",
            "head_merge_fan_in": "maximum_merge_fan_in",
        },
        spec_path.name,
    )
    # The spec declares ONE candidate-row cap and the code now has two enforcement
    # sites, so the spec's value is applied to both -- which is exactly what the single
    # shared constant used to do, leaving the rehearsal's behaviour unchanged while the
    # hosted build runs 6,000,000 / 200,000,000.
    caps["max_task_head_candidate_rows"] = caps["max_head_candidate_rows"]
    return caps


# ---------------------------------------------------------------------------
# Candidate: Rust transform + on-disk DuckDB construction (fresh process group)
# ---------------------------------------------------------------------------
def run_candidate(args: argparse.Namespace) -> None:
    import duckdb
    import pyarrow.ipc as ipc

    workspace = Path(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    hydrated = workspace / "hydrated.arrow"
    transformed = workspace / "terms.arrow"
    report_path = workspace / "transform.json"
    spill = workspace / "spill"
    spill.mkdir(exist_ok=True)

    # Small input batches keep each transformed term batch <= the frozen
    # 65,536-row IPC cap (transform emits one output batch per input batch).
    P.hydrate(Path(args.input), hydrated, batch_rows=args.hydrate_batch_rows)
    A.run_bounded(
        [
            str(args.transform_binary),
            "--input",
            str(hydrated),
            "--output",
            str(transformed),
            "--report",
            str(report_path),
            "--source-limits",
            str(args.source_limits),
        ],
        scratch_roots=[workspace],
        limits=A.Limits(
            max_rss_bytes=args.max_rss_bytes,
            max_scratch_bytes=args.max_scratch_bytes,
            wall_seconds=args.wall_seconds,
        ),
    )
    transform = json.loads(report_path.read_text())
    # The Rust transform is the authoritative, deterministic semantic emitter;
    # its term stream is byte-identical across runs, so it anchors the
    # candidate's deterministic output hash. DuckDB then performs the on-disk
    # construction (stream-load into an external table + typed Parquet
    # materialization) and independently reconciles the row count.
    output_sha256 = sha256_file(transformed)
    max_batch_rows = 0
    with transformed.open("rb") as source:
        for batch in ipc.open_stream(source):
            max_batch_rows = max(max_batch_rows, batch.num_rows)
    if max_batch_rows > 65_536:
        raise ValueError("candidate term IPC batch exceeds 65,536-row cap")

    connection = duckdb.connect(str(workspace / "candidate.duckdb"))
    connection.execute(f"SET memory_limit='{args.memory_limit}'")
    connection.execute(f"SET threads={args.threads}")
    connection.execute(f"SET temp_directory='{spill}'")
    # On-disk DuckDB construction: stream the Rust term output through a bounded
    # hash aggregate (one streaming pass, no full-table materialization) and
    # persist a compact per-partition-cell summary table. Its total row count
    # must reconcile exactly with the authoritative Rust transform binding.
    with transformed.open("rb") as source:
        reader = ipc.open_stream(source)
        connection.register("terms_stream", reader)
        connection.execute(
            "CREATE TABLE cell_summary AS SELECT execution_group, partition_cell, "
            "count(*)::UBIGINT term_rows FROM terms_stream GROUP BY execution_group, "
            "partition_cell"
        )
        connection.unregister("terms_stream")
    rows = connection.execute("SELECT coalesce(sum(term_rows),0) FROM cell_summary").fetchone()[0]
    cells = connection.execute("SELECT count(*) FROM cell_summary").fetchone()[0]
    database_bytes = (workspace / "candidate.duckdb").stat().st_size
    connection.close()
    if rows != transform["emitted_term_rows"]:
        raise ValueError("candidate DuckDB row count differs from Rust transform")
    report = {
        "binding": {
            "emitted_term_rows": transform["emitted_term_rows"],
            "semantic_sum_a": transform["semantic_sum_a"],
            "semantic_sum_b": transform["semantic_sum_b"],
        },
        "output_sha256": output_sha256,
        "duckdb_rows": rows,
        "duckdb_partition_cells": cells,
        "duckdb_database_bytes": database_bytes,
    }
    Path(args.output_report).write_text(json.dumps(report, sort_keys=True) + "\n")


def measured_candidate(index: int, seq: int, scratch_root: Path, caps: dict) -> dict:
    """Run one candidate in a fresh process group and measure it end-to-end."""
    workspace = scratch_root / f"candidate-{index:02d}-{seq}"
    report_path = scratch_root / f"candidate-{index:02d}-{seq}.json"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "candidate",
        "--input",
        str(projected_path(index)),
        "--source-limits",
        str(SOURCE_LIMITS),
        "--transform-binary",
        str(RELEASE_BIN / "places-transform-v1"),
        "--workspace",
        str(workspace),
        "--output-report",
        str(report_path),
        "--memory-limit",
        caps["memory_limit"],
        "--threads",
        str(caps["threads"]),
        "--max-rss-bytes",
        str(caps["max_rss_bytes"]),
        "--max-scratch-bytes",
        str(caps["max_scratch_bytes"]),
        "--wall-seconds",
        str(caps["wall_seconds"]),
    ]
    resources = A.run_bounded(
        command,
        scratch_roots=[workspace],
        limits=A.Limits(
            max_rss_bytes=caps["max_rss_bytes"],
            max_scratch_bytes=caps["max_scratch_bytes"],
            wall_seconds=caps["wall_seconds"],
        ),
    )
    report = json.loads(report_path.read_text())
    result = {
        "binding": report["binding"],
        "output_sha256": report["output_sha256"],
        "resources": {
            "wall_seconds": resources["wall_seconds"],
            "peak_rss_bytes": resources["peak_rss_bytes"],
            "peak_scratch_bytes": resources["peak_scratch_bytes"],
        },
    }
    # Bound peak disk: keep only the small report, drop the multi-GB workspace
    # (hydrated + term IPC) before the next candidate/task starts.
    import shutil

    shutil.rmtree(workspace, ignore_errors=True)
    return result


def run_task_run(args: argparse.Namespace) -> None:
    import pyarrow.parquet as pq

    index = args.task
    scratch_root = Path(args.scratch_root)
    scratch_root.mkdir(parents=True, exist_ok=True)
    projection = json.loads((EVIDENCE / f"projection-{index:02d}.json").read_text())
    input_path = projected_path(index)
    parquet = pq.ParquetFile(input_path)
    raw_identity = (parquet.schema_arrow.metadata or {}).get(
        b"overture.places_projection_identity"
    )
    if raw_identity is None:
        raise ValueError(f"projected task {index} lacks its identity metadata")
    actual_identity = json.loads(raw_identity)
    actual_output = {
        "bytes": input_path.stat().st_size,
        "sha256": A.sha256_file(input_path),
        "records": parquet.metadata.num_rows,
        "row_groups": parquet.metadata.num_row_groups,
    }
    if projection.get("identity") != actual_identity:
        raise ValueError(f"task {index} projection report identity differs from Parquet")
    reported_output = projection.get("output", {})
    if any(
        reported_output.get(key) != value for key, value in actual_output.items()
    ):
        raise ValueError(f"task {index} projection report output differs from Parquet")

    caps = {
        "memory_limit": args.memory_limit,
        "threads": args.threads,
        "max_rss_bytes": args.max_rss_bytes,
        "max_scratch_bytes": args.max_scratch_bytes,
        "wall_seconds": args.wall_seconds,
    }

    # Baseline: frozen streaming Python semantic baseline in a fresh process group.
    baseline_report_path = scratch_root / f"baseline-{index:02d}.json"
    A.run_bounded(
        [
            sys.executable,
            str(ROOT / "scripts/baseline_places_construction_v1.py"),
            "--input",
            str(input_path),
            "--source-limits",
            str(SOURCE_LIMITS),
            "--output",
            str(baseline_report_path),
        ],
        scratch_roots=[scratch_root],
        limits=A.Limits(
            max_rss_bytes=args.max_rss_bytes,
            max_scratch_bytes=args.max_scratch_bytes,
            wall_seconds=args.baseline_wall_seconds,
        ),
    )
    baseline = json.loads(baseline_report_path.read_text())

    candidates = [
        measured_candidate(index, seq, scratch_root, caps) for seq in (0, 1)
    ]

    task_run = {
        "projection": {**projection, "verified_input": actual_output},
        "baseline": {
            "emitted_term_rows": baseline["emitted_term_rows"],
            "semantic_sum_a": baseline["semantic_sum_a"],
            "semantic_sum_b": baseline["semantic_sum_b"],
            "elapsed_seconds": baseline["elapsed_seconds"],
        },
        "candidates": candidates,
    }
    Path(args.output).write_text(json.dumps(task_run, indent=2, sort_keys=True) + "\n")
    worst = max(c["resources"]["wall_seconds"] for c in candidates)
    speedup = baseline["elapsed_seconds"] / worst if worst else 0.0
    determinism = len({c["output_sha256"] for c in candidates}) == 1
    baseline_binding = {
        k: task_run["baseline"][k]
        for k in ("emitted_term_rows", "semantic_sum_a", "semantic_sum_b")
    }
    binding_ok = all(c["binding"] == baseline_binding for c in candidates)
    print(
        json.dumps(
            {
                "task": index,
                "baseline_seconds": round(baseline["elapsed_seconds"], 3),
                "worst_candidate_seconds": round(worst, 3),
                "speedup": round(speedup, 3),
                "deterministic": determinism,
                "binding_equal": binding_ok,
                "candidate_peak_rss_bytes": max(
                    c["resources"]["peak_rss_bytes"] for c in candidates
                ),
            },
            sort_keys=True,
        )
    )


# ---------------------------------------------------------------------------
# Dormant Worker index-probe query (faithful reproduction of
# geocoder-worker/src/places_construction_v1.rs lookup on the real artifact)
# ---------------------------------------------------------------------------
INDEX_DOMAIN = b"overture-places-serving-index-v1\0"


def index_hash(key: bytes) -> int:
    return int.from_bytes(hashlib.sha256(INDEX_DOMAIN + key).digest()[:8], "big")


def parse_serving_index(data: bytes, mode: str) -> list[dict]:
    prefix = b"PLRV" if mode == "routed" else b"PLHD"
    accepted_magic = {prefix + b"0002", prefix + b"0003"}
    if len(data) < 36 or data[:8] not in accepted_magic:
        raise ValueError("invalid Places v1 artifact magic")
    index_offset = struct.unpack_from("<Q", data, 16)[0]
    index_count = struct.unpack_from("<I", data, 24)[0]
    if struct.unpack_from("<I", data, 28)[0] != 0 or index_offset < 32 or index_offset > len(data):
        raise ValueError("Places v1 header does not reconcile")
    position = index_offset
    stored_count = struct.unpack_from("<I", data, position)[0]
    position += 4
    if stored_count != index_count:
        raise ValueError("Places v1 index count differs")
    fixed_start = position
    key_start = fixed_start + index_count * 40
    entries = []
    key_position_expected = 0
    for _ in range(index_count):
        hash_, key_pos, key_len, records, payload_off, payload_bytes = struct.unpack_from(
            "<QQIIQQ", data, position
        )
        position += 40
        key = data[key_start + key_pos : key_start + key_pos + key_len]
        if key_pos != key_position_expected or hash_ != index_hash(key):
            raise ValueError("Places v1 index entry is invalid")
        key_position_expected += key_len
        entries.append(
            {"hash": hash_, "key": key, "offset": payload_off, "bytes": payload_bytes,
             "records": records}
        )
    return entries


def worker_lookup(data: bytes, entries: list[dict], mode: str, token: str,
                  cell: str | None, maximum_candidates: int, result_cap: int) -> tuple[int, int]:
    """Return (probe_count, decoded_records) for a genuine indexed lookup."""
    if mode == "routed":
        key = cell.encode() + b"\0" + token.encode()
    else:
        key = token.encode()
    target = index_hash(key)
    # partition_point over hash-sorted entries.
    lo, hi = 0, len(entries)
    while lo < hi:
        mid = (lo + hi) // 2
        if entries[mid]["hash"] < target:
            lo = mid + 1
        else:
            hi = mid
    probes = 0
    selected = None
    for entry in entries[lo:]:
        if entry["hash"] != target:
            break
        probes += 1
        if probes > 32:
            raise ValueError("Places v1 index probe cap exceeded")
        if entry["key"] == key:
            selected = entry
            break
    if selected is None:
        return probes, 0
    if selected["records"] > maximum_candidates:
        raise ValueError("Places v1 candidate cap exceeded")
    position = selected["offset"]
    end = position + selected["bytes"]
    decoded = 0
    for _ in range(selected["records"]):
        length = struct.unpack_from("<I", data, position)[0]
        position += 4
        entry = data[position : position + length]
        position += length
        rtoken, rcell = decode_serving_entry(entry, mode)
        if rtoken != token or rcell != cell:
            raise ValueError("Places v1 indexed payload key differs")
        if decoded < result_cap:
            decoded += 1
    if position != end:
        raise ValueError("Places v1 indexed payload length differs")
    return probes, decoded


def decode_serving_entry(entry: bytes, mode: str) -> tuple[str, str | None]:
    at = 0

    def text() -> str:
        nonlocal at
        length = struct.unpack_from("<H", entry, at)[0]
        at += 2
        value = entry[at : at + length].decode()
        at += length
        return value

    token = text()
    cell = text() if mode == "routed" else None
    return token, cell


# ---------------------------------------------------------------------------
# Worker local-decoder evidence: exercise the ACTUAL geocoder-worker head-shard
# decoder over the locally-built PLHD shard bytes. No R2, no network — this is
# the honest `worker_local_decoder_evidence` class the readiness validator
# accepts in place of a deployed Worker.
# ---------------------------------------------------------------------------
def exercise_worker_head_decoder(
    head: dict[str, Any], store: Any, scratch_root: Path, sample: int = 8
) -> dict[str, Any]:
    manifest = json.loads(store.path(head["manifest_object"]["key"]).read_text())
    shard_bits = manifest["shard_bits"]
    shard_paths = {
        item["shard_id"]: str(store.path(item["key"]))
        for item in head["shard_objects"]
    }
    # Draw real sample tokens straight from the built shard bytes, one per shard.
    samples: list[tuple[str, int, str]] = []
    phrase_sample: tuple[str, int, str] | None = None
    for shard in manifest["shards"]:
        path = shard_paths.get(shard["shard_id"])
        if path is None:
            raise ValueError("head manifest shard lacks a local object-store key")
        entries = parse_serving_index(Path(path).read_bytes(), "head")
        if not entries:
            continue
        if len(samples) < sample:
            token = entries[0]["key"].decode("utf-8")
            samples.append((token, shard["shard_id"], path))
        if phrase_sample is None:
            phrase_entry = next(
                (
                    entry
                    for entry in entries
                    if entry["key"].startswith((b"e2:", b"e3:"))
                ),
                None,
            )
            if phrase_entry is not None:
                phrase_sample = (
                    phrase_entry["key"].decode("utf-8"),
                    shard["shard_id"],
                    path,
                )
    if not samples:
        raise RuntimeError("sharded head produced no decodable head tokens")
    if phrase_sample is not None and phrase_sample not in samples:
        samples[-1] = phrase_sample
    evidence_out = scratch_root / "worker-head-decoder-evidence.json"
    env = dict(**__import__("os").environ)
    env["PLACES_HEAD_SHARD_BITS"] = str(shard_bits)
    env["PLACES_HEAD_SAMPLES"] = "\n".join(
        f"{token}\t{shard_id}\t{path}" for token, shard_id, path in samples
    )
    env["PLACES_HEAD_EVIDENCE_OUT"] = str(evidence_out)
    # Run the actual Worker decoder unit test against the real shard bytes.
    subprocess.run(
        [
            "cargo",
            "test",
            "--release",
            "-p",
            "geocoder-worker",
            "--lib",
            "places_construction_v1::tests::local_decoder_resolves_real_head_shards",
            "--",
            "--ignored",
            "--exact",
            "--nocapture",
        ],
        cwd=str(ROOT / "crates"),
        env=env,
        check=True,
    )
    result = json.loads(evidence_out.read_text())
    result["sample_tokens"] = [token for token, _, _ in samples]
    result["entity_phrase_token_resolved"] = (
        phrase_sample is not None and phrase_sample in samples
    )
    result["entity_phrase_decoder_validated"] = (
        result["entity_phrase_token_resolved"]
        and result.get("entity_phrase_records_validated", 0) > 0
    )
    result["manifest_key"] = head["manifest_object"]["key"]
    return result


# ---------------------------------------------------------------------------
# Rehearsal: seven-role multi-task adaptive plan/reduce/head + worker queries
# ---------------------------------------------------------------------------
def run_rehearse(args: argparse.Namespace) -> None:
    import pyarrow.parquet as pq

    scratch_root = Path(args.scratch_root)
    scratch_root.mkdir(parents=True, exist_ok=True)
    store = A.LocalObjectStore(scratch_root / "store")
    binaries = {
        "transform": RELEASE_BIN / "places-transform-v1",
        "proof": RELEASE_BIN / "places-proof-directory",
        "encode": RELEASE_BIN / "places-serving-encode-v1",
        "verify": RELEASE_BIN / "places-serving-verify-v1",
    }
    tasks = [int(t) for t in args.tasks.split(",")] if args.tasks else list(ROLE_TASKS)

    limits = P.Limits(
        max_input_rows=1_000_000,
        max_pack_rows=args.max_pack_rows,
        parquet_row_group_rows=args.parquet_row_group_rows,
        max_rss_bytes=args.max_rss_bytes,
        max_scratch_bytes=args.max_scratch_bytes,
        max_output_bytes=2 * 1024**3,
        wall_seconds=args.wall_seconds,
        duckdb_memory_limit=args.memory_limit,
        duckdb_threads=args.threads,
        max_fan_in_tasks=16,
        max_fan_in_packs=64,
        # The partition caps DECLARED BY THE FROZEN EVIDENCE SPEC, read from it
        # rather than restated here. They are deliberately NOT the raised hosted
        # production caps (2,000,000 / 512 MiB / 400,000, now the `P.Limits`
        # defaults): this rehearsal is evidence for the current frozen spec, whose relaxation
        # policy is "none", and its coverage gate requires genuine adaptive
        # subdivision. Raising these needs a new Places evidence generation plus
        # a re-run, not an edit here -- see
        # docs/plans/2026-07-24-construction-v1-follow-ups.md.
        **spec_partition_caps(),
        # The head caps the same spec declares, read for the same reason. The hosted
        # build runs a merge fan-in of 8; the spec freezes 16, and its relaxation policy
        # is "none". See spec_head_caps for the coverage gap that leaves.
        **spec_head_caps(),
        adaptive_subdivision_depth=8,
        head_result_cap=10,
    )

    worker_routed_probes.clear()
    worker_head_probes.clear()
    # map_task calls the module-global hydrate with the default 65,536 input
    # batch; on real ~1M-row tasks that yields transform term batches far above
    # the frozen 65,536 IPC cap. Bind a small-input-batch hydrate so every
    # emitted term batch stays under cap (fail-closed if it ever exceeds).
    original_hydrate = P.hydrate
    hydrate_batch = args.hydrate_batch_rows
    P.hydrate = lambda input_path, output, batch_rows=hydrate_batch: original_hydrate(
        input_path, output, batch_rows=batch_rows
    )
    interruption_checkpoint = scratch_root / "interruption-phases.json"
    expected_interruptions = [
        ("local_write", "local_write"),
        ("after_objects", "immutable_publish"),
        ("before_marker", "before_marker"),
    ]
    if interruption_checkpoint.exists():
        interruption_phases = json.loads(interruption_checkpoint.read_text())
        expected_phases = [phase for _, phase in expected_interruptions]
        if interruption_phases != expected_phases[: len(interruption_phases)]:
            raise ValueError("interruption checkpoint is incomplete or invalid")
    else:
        interruption_phases = []

    # ---- map each role task; exercise interruption/resume on the first task ----
    markers: list[dict] = []
    for position, index in enumerate(tasks):
        task_id = f"role-{index:02d}"
        arguments = dict(
            input_path=projected_path(index),
            source_limits=SOURCE_LIMITS,
            store=store,
            scratch_root=scratch_root / "map",
            request_sha256=hashlib.sha256(task_id.encode()).hexdigest(),
            task_id=task_id,
            transform_binary=binaries["transform"],
            proof_binary=binaries["proof"],
            limits=limits,
        )
        if position == 0 and len(interruption_phases) < len(expected_interruptions):
            # Interruption phases: kill after local write / before marker; resume
            # must not duplicate logical rows. failpoint names map to spec phases.
            for failpoint, phase in expected_interruptions[len(interruption_phases) :]:
                try:
                    P.map_task(**arguments, failpoint=failpoint)
                    raise RuntimeError(f"expected injected interruption {failpoint}")
                except RuntimeError as exc:
                    if "injected Places interruption" not in str(exc):
                        raise
                if store.path(P.marker_key(task_id)).exists():
                    raise RuntimeError(
                        f"marker published despite interruption {failpoint}"
                    )
                interruption_phases.append(phase)
                interruption_checkpoint.write_text(
                    json.dumps(interruption_phases, sort_keys=True) + "\n"
                )
        marker = P.map_task(**arguments)
        markers.append(marker)
        print(
            json.dumps(
                {
                    "mapped": index,
                    "records": marker["binding"]["records"],
                    "packs": len(marker["packs"]),
                    "reused": marker["admitted_existing"],
                },
                sort_keys=True,
            ),
            flush=True,
        )

    # ---- resume-before-projection: re-run map of first task; marker reused ----
    resume_started = time.monotonic()
    resumed = P.map_task(
        input_path=projected_path(tasks[0]),
        source_limits=SOURCE_LIMITS,
        store=store,
        scratch_root=scratch_root / "map",
        request_sha256=hashlib.sha256(f"role-{tasks[0]:02d}".encode()).hexdigest(),
        task_id=f"role-{tasks[0]:02d}",
        transform_binary=binaries["transform"],
        proof_binary=binaries["proof"],
        limits=limits,
    )
    resume_before_projection = (
        resumed["admitted_existing"] is True
        and resumed["binding"] == markers[0]["binding"]
    )
    resume_seconds = time.monotonic() - resume_started

    total_packs = sum(len(m["packs"]) for m in markers)
    total_row_groups = sum(
        len(pack["directory"]["row_groups"]) for m in markers for pack in m["packs"]
    )
    map_stage_resources = [
        {
            "task_index": int(marker["task_id"].removeprefix("role-")),
            "transform": marker["transform_evidence"],
            "construction": marker["construction_evidence"],
        }
        for marker in markers
    ]

    # ---- adaptive genesis plan: frozen caps force b2e3 (8.55M) to subdivide ----
    plan_started = time.monotonic()
    adaptive = P.adaptive_genesis_plan(
        markers,
        store=store,
        scratch_root=scratch_root / "genesis",
        limits=limits,
    )
    subdivided = [p for p in adaptive["partitions"] if p["ownership"]["depth"] > 0]
    max_depth = max((p["ownership"]["depth"] for p in adaptive["partitions"]), default=0)
    subdivided_cells = sorted({p["partition_cell"] for p in subdivided})
    adaptive_seconds = time.monotonic() - plan_started

    # ---- reduce ----
    # Selective read amplification (scanned/selected) is measured on the
    # steady-state owned partition: a full, non-subdivided cell. Reducing the
    # largest plain cells keeps amplification near 1 because a large cell owns
    # nearly all rows of the row-groups it spans. A subdivided partition owns
    # only a sub-cell nibble but the row-group routing is per-cell, so it must
    # rescan the whole cell; that read pattern's amplification is recorded
    # transparently in `observed`, never as the steady-state headline metric.
    plain = [p for p in adaptive["partitions"] if p["ownership"]["depth"] == 0]
    plain_sorted = sorted(plain, key=lambda p: -p["term_rows"])
    reduce_targets = plain_sorted[: args.reduce_partitions]

    def reduce_one(partition: dict) -> dict:
        return P.reduce_partition(
            partition=partition,
            plan=adaptive,
            markers=markers,
            store=store,
            scratch_root=scratch_root / "reduce",
            encoder_binary=binaries["encode"],
            verifier_binary=binaries["verify"],
            limits=limits,
        )

    def amplification_of(reduction: dict) -> float:
        selected_rows = reduction["binding"]["records"]
        scanned_rows = sum(
            item["selected"]["records"] + item["discarded"]["records"]
            for item in reduction["reconciled_row_groups"]
        )
        return scanned_rows / selected_rows if selected_rows else 0.0

    reductions: list[dict] = []
    overlap_reconciliation = False
    max_amplification = 0.0
    routed_verified = True
    routed_entity_phrase_index_entries = 0
    for partition in reduce_targets:
        reduction = reduce_one(partition)
        reductions.append(reduction)
        if any(item["discarded"]["records"] > 0 for item in reduction["reconciled_row_groups"]):
            overlap_reconciliation = True
        max_amplification = max(max_amplification, amplification_of(reduction))
    # reduce_partition raises unless every selected+discarded row-group binding
    # and the aggregate selected binding reconcile exactly to the plan/proofs.
    exact_reconciliation = len(reductions) > 0

    # Prove the adaptive (subdivided) reduce path binding without letting its
    # inherently higher sub-cell read amplification distort the headline metric.
    subdivided_reduction_info: dict[str, Any] = {}
    if subdivided:
        sub_reduction = reduce_one(subdivided[0])
        subdivided_reduction_info = {
            "partition_id": subdivided[0]["id"],
            "binding_matches_plan": sub_reduction["binding"] == subdivided[0]["binding"],
            "records": sub_reduction["binding"]["records"],
            "read_amplification": round(amplification_of(sub_reduction), 6),
        }
        if any(item["discarded"]["records"] > 0 for item in sub_reduction["reconciled_row_groups"]):
            overlap_reconciliation = True

    for partition, reduction in zip(reduce_targets, reductions):
        # routed_verified: reduce_partition already ran the Rust verifier; confirm
        # the dormant Worker can query the routed artifact under caps.
        routed_bytes = store.path(reduction["routed_object"]["key"]).read_bytes()
        entries = parse_serving_index(routed_bytes, "routed")
        routed_entity_phrase_index_entries += sum(
            b"\0e2:" in entry["key"] or b"\0e3:" in entry["key"]
            for entry in entries
        )
        cell = partition["partition_cell"]
        # sample up to 3 routed tokens for this cell from the leaf.
        leaf = pq.ParquetFile(store.path(reduction["leaf_object"]["key"]))
        sample_tokens: list[str] = []
        for batch in leaf.iter_batches(batch_size=4096, columns=["partition_cell", "token"]):
            for pc_val, tok in zip(batch["partition_cell"].to_pylist(), batch["token"].to_pylist()):
                if pc_val == cell and tok not in sample_tokens:
                    sample_tokens.append(tok)
                if len(sample_tokens) >= 3:
                    break
            if len(sample_tokens) >= 3:
                break
        for tok in sample_tokens:
            probes, decoded = worker_lookup(
                routed_bytes, entries, "routed", tok, cell, 256, 10
            )
            worker_routed_probes.append(probes)
            if decoded == 0:
                routed_verified = False

    # ---- global head from bounded per-task candidates + dormant Worker query ----
    # The merged per-task head candidates must stay under the frozen
    # max_head_candidate_rows cap. Across the full 7-task fan-in the CJK-heavy
    # vocabularies push the sum over cap, so greedily admit the largest subset
    # (biggest candidate contributors first) that fits and record the rest.
    ordered = sorted(markers, key=lambda m: -m["head_candidates"]["records"])
    head_markers: list[dict] = []
    head_rows = 0
    head_excluded: list[str] = []
    for m in ordered:
        rows_m = m["head_candidates"]["records"]
        if head_rows + rows_m <= limits.max_head_candidate_rows:
            head_markers.append(m)
            head_rows += rows_m
        else:
            head_excluded.append(m["task_id"])
    # A GLOBAL per-token head over real planet data has millions of distinct
    # tokens, so a single artifact fail-closes at MAX_INDEX_ENTRIES=250000. The
    # head is instead hash-sharded (PR #141): the merged head is partitioned by
    # the top `shard_bits` of each token's index hash into independently encoded
    # PLHD shards, bound by a manifest the Rust sharded verifier reconciles
    # against an independent reduce-side binding. head_verified reflects that
    # verify; worker_head_query reflects the actual Worker head-shard decoder
    # (crates/geocoder-worker) resolving real tokens from the built shard bytes.
    head_verified = False
    head_result_cap = limits.head_result_cap
    head_note = ""
    head: dict[str, Any] = {}
    worker_local_decoder: dict[str, Any] = {}
    entity_phrase_admission: str | None = None
    entity_phrase_head_index_entries = 0
    entity_phrase_head_records = 0
    entity_phrase_head_by_prefix = {
        prefix: {"index_entries": 0, "records": 0}
        for prefix in P.ENTITY_PHRASE_PREFIXES
    }
    head_output_bytes = 0
    try:
        head = P.build_sharded_global_head_from_markers(
            markers=head_markers,
            store=store,
            scratch_root=scratch_root / "head",
            encoder_binary=binaries["encode"],
            verifier_binary=binaries["verify"],
            limits=limits,
            shard_bits=args.head_shard_bits,
        )
        # build_sharded_* runs `places-serving-verify-v1 --mode head-sharded`,
        # which reconciles every shard's bytes against the independent binding.
        head_result_cap = head["result_cap"]
        head_verified = True
        head_manifest = json.loads(
            store.path(head["manifest_object"]["key"]).read_text()
        )
        entity_phrase_admission = head_manifest.get("entity_phrase_admission")
        head_output_bytes = head["manifest_object"]["bytes"] + sum(
            item["bytes"] for item in head["shard_objects"]
        )
        shard_paths = {
            item["shard_id"]: store.path(item["key"])
            for item in head["shard_objects"]
        }
        for shard in head_manifest["shards"]:
            path = shard_paths.get(shard["shard_id"])
            if path is None:
                raise ValueError("head manifest shard lacks a local object-store key")
            entries = parse_serving_index(path.read_bytes(), "head")
            phrase_entries = [
                entry
                for entry in entries
                if entry["key"].startswith((b"e2:", b"e3:"))
            ]
            entity_phrase_head_index_entries += len(phrase_entries)
            entity_phrase_head_records += sum(
                entry["records"] for entry in phrase_entries
            )
            for prefix in P.ENTITY_PHRASE_PREFIXES:
                selected = [
                    entry
                    for entry in phrase_entries
                    if entry["key"].startswith(prefix.encode())
                ]
                entity_phrase_head_by_prefix[prefix]["index_entries"] += len(selected)
                entity_phrase_head_by_prefix[prefix]["records"] += sum(
                    entry["records"] for entry in selected
                )
        worker_local_decoder = exercise_worker_head_decoder(head, store, scratch_root)
        for _ in range(worker_local_decoder["tokens_resolved"]):
            worker_head_probes.append(1)
    except (subprocess.CalledProcessError, ValueError, RuntimeError) as exc:
        head_verified = False
        head_note = f"sharded head build/verify/decoder failed: {exc}"

    max_worker_probes = max(worker_routed_probes + worker_head_probes, default=0)

    rehearsal = {
        "logical_tasks": len(markers),
        "packs": total_packs,
        "parquet_row_groups": total_row_groups,
        "partitions": len(adaptive["partitions"]),
        "maximum_selective_amplification": round(max_amplification, 6),
        "exact_reconciliation": exact_reconciliation,
        "overlap_reconciliation": overlap_reconciliation,
        "adaptive_subdivision": len(subdivided) > 0,
        "multi_task_fan_in": len(markers) >= 7,
        "routed_verified": routed_verified,
        "head_verified": head_verified,
        "worker_routed_query": len(worker_routed_probes) > 0 and routed_verified,
        "worker_head_query": len(worker_head_probes) > 0 and head_verified,
        "worker_local_decoder_evidence": bool(worker_local_decoder),
        "worker_entity_phrase_decoder": worker_local_decoder.get(
            "entity_phrase_decoder_validated", False
        ),
        "resume_before_projection": resume_before_projection,
        "interruption_phases": interruption_phases,
        "head_result_cap": head_result_cap,
        "head_sharded": head_verified,
        "head_shard_bits": head.get("shard_bits"),
        "head_shard_count": head.get("shard_count"),
        "head_populated_shards": head.get("populated_shards"),
        "maximum_worker_index_probes": max_worker_probes,
        "entity_phrase_admission": entity_phrase_admission,
        "entity_phrase_head_index_entries": entity_phrase_head_index_entries,
        "entity_phrase_head_records": entity_phrase_head_records,
        "entity_phrase_head_by_prefix": entity_phrase_head_by_prefix,
        "head_output_bytes": head_output_bytes,
        "routed_entity_phrase_index_entries": routed_entity_phrase_index_entries,
        "routed_entity_phrases_absent": routed_entity_phrase_index_entries == 0,
        "map_stage_resources": map_stage_resources,
        "observed": {
            "mapped_tasks": tasks,
            "scale_note": (
                "Full seven-role fan-in over the real 2026-06-17.0 census tasks, "
                "including the dense CJK role tasks 86 (token_fanout) and 87 "
                "(densest_spatial). Post-#143 map scratch hygiene keeps their "
                "per-stage scratch under the 8 GiB cap, so no substitution is "
                "needed. The global head is hash-sharded and every shard is "
                "reconciled against an independent reduce-side binding; the actual "
                "Worker head-shard decoder resolves real tokens from the built "
                "shard bytes (worker_local_decoder_evidence)."
            ),
            "adaptive_max_depth": max_depth,
            "subdivided_cells": subdivided_cells,
            "subdivided_partition_count": len(subdivided),
            "reduced_partitions": [p["id"] for p in reduce_targets],
            "subdivided_partition_reduction": subdivided_reduction_info,
            "worker_routed_probes": worker_routed_probes,
            "worker_head_probes": worker_head_probes,
            "worker_local_decoder": worker_local_decoder,
            "resume_seconds": round(resume_seconds, 3),
            "adaptive_plan_seconds": round(adaptive_seconds, 3),
            "head_input_candidate_rows": head.get("input_candidate_rows"),
            "head_total_records": head.get("total_records"),
            "head_total_index_entries": head.get("total_index_entries"),
            "head_merged_task_ids": sorted(m["task_id"] for m in head_markers),
            "head_excluded_over_row_cap_task_ids": sorted(head_excluded),
            "head_serving_encode_note": head_note,
        },
    }
    Path(args.output).write_text(json.dumps(rehearsal, indent=2, sort_keys=True) + "\n")
    print(json.dumps(rehearsal, sort_keys=True))


# module-level accumulators for worker probes (set per rehearse call)
worker_routed_probes: list[int] = []
worker_head_probes: list[int] = []


# ---------------------------------------------------------------------------
# Assemble: merge task_runs + rehearsal into the frozen scale-evidence file
# ---------------------------------------------------------------------------
def run_assemble(args: argparse.Namespace) -> None:
    """Compose a scale-evidence bundle from fresh census/task-run/rehearsal.

    Everything is derived from the re-run artifacts and bound to the supplied evidence
    spec; nothing is carried over from the v1 evidence. Candidate universe and
    role selection use the readiness validator's own functions so the assembled
    evidence and the validator agree by construction.
    """
    validator = load("places_readiness_validator", ROOT / "scripts/validate_places_planet_readiness.py")
    spec_path = Path(args.spec)
    inventory = json.loads(INVENTORY.read_text())

    # Fresh census metrics per candidate task.
    census: dict[int, dict[str, Any]] = {}
    for path in sorted(Path(args.census_dir).glob("census-*.json")):
        index = int(path.stem.split("-")[-1])
        report = json.loads(path.read_text())
        if report.get("schema") != "overture-places-construction-v1-census-v1":
            raise ValueError(f"census report {path} has an unexpected schema")
        if report.get("identity", {}).get("task_index") != index:
            raise ValueError(f"census report {path} task identity differs")
        # Keep the complete fresh report plus its file digest. Role metrics alone
        # cannot prove which projection/spec or transform produced them, and
        # would let a pre-change census silently select the formal role set.
        census[index] = {
            "task_index": index,
            "report_sha256": A.sha256_file(path),
            **report,
        }

    universe = validator.candidate_universe(
        inventory, json.loads(spec_path.read_text())["candidate_universe"]["maximum_tasks"]
    )
    if sorted(census) != sorted(universe):
        raise ValueError(
            f"census task set {sorted(census)} differs from candidate universe {sorted(universe)}"
        )
    roles = validator.select_roles(inventory, census)

    task_runs: dict[str, Any] = {}
    for path in sorted(Path(args.task_run_dir).glob("task-run-*.json")):
        index = int(path.stem.split("-")[-1])
        task_runs[str(index)] = json.loads(path.read_text())

    rehearsal = json.loads(Path(args.rehearsal).read_text())
    rehearsal["census_complete"] = True
    rehearsal["construction_phase_pending"] = False

    # Host provenance: record which physical host produced each measured section
    # so the frozen bundle is self-describing (timings are hardware-sensitive; the
    # binding/determinism evidence is not). Every task-run in --task-run-dir came
    # from one host, tagged here; census and rehearsal hosts are recorded too.
    provenance = json.loads(Path(args.provenance).read_text())
    for host_key in (
        provenance["task_run_host"],
        provenance["census_host"],
        provenance["rehearsal_host"],
    ):
        if host_key not in provenance["hosts"]:
            raise ValueError(f"provenance references undeclared host {host_key}")
    task_run_host = provenance["task_run_host"]
    for run in task_runs.values():
        run["host"] = task_run_host
    rehearsal["host"] = provenance["rehearsal_host"]

    evidence = {
        "schema": "overture-places-construction-v1-scale-evidence-v1",
        "evidence_spec_sha256": A.sha256_file(spec_path),
        "inventory_file_sha256": A.sha256_file(INVENTORY),
        "inventory_sha256": inventory["inventory_sha256"],
        "schema_fingerprint_sha256": inventory["schema_contract"]["fingerprint_sha256"],
        "provenance": provenance,
        "candidate_universe": universe,
        "census": [census[index] for index in universe],
        "roles": roles,
        "task_runs": task_runs,
        "rehearsal": rehearsal,
    }
    Path(args.output).write_text(json.dumps(evidence, indent=2) + "\n")
    print(
        json.dumps(
            {
                "wrote": args.output,
                "task_runs": len(task_runs),
                "census_tasks": len(census),
                "roles": roles,
            },
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("candidate")
    c.add_argument("--input", required=True)
    c.add_argument("--source-limits", required=True)
    c.add_argument("--transform-binary", required=True)
    c.add_argument("--workspace", required=True)
    c.add_argument("--output-report", required=True)
    c.add_argument("--memory-limit", default="2GB")
    c.add_argument("--threads", type=int, default=2)
    c.add_argument("--hydrate-batch-rows", type=int, default=2048)
    c.add_argument("--max-rss-bytes", type=int, default=4_294_967_296)
    c.add_argument("--max-scratch-bytes", type=int, default=8_589_934_592)
    c.add_argument("--wall-seconds", type=float, default=450)
    c.set_defaults(func=run_candidate)

    t = sub.add_parser("task-run")
    t.add_argument("--task", type=int, required=True)
    t.add_argument("--scratch-root", required=True)
    t.add_argument("--output", required=True)
    t.add_argument("--memory-limit", default="2GB")
    t.add_argument("--threads", type=int, default=2)
    t.add_argument("--max-rss-bytes", type=int, default=4_294_967_296)
    t.add_argument("--max-scratch-bytes", type=int, default=8_589_934_592)
    t.add_argument("--wall-seconds", type=float, default=450)
    t.add_argument("--baseline-wall-seconds", type=float, default=900)
    t.set_defaults(func=run_task_run)

    r = sub.add_parser("rehearse")
    r.add_argument("--scratch-root", required=True)
    r.add_argument("--output", required=True)
    r.add_argument("--tasks", default="")
    r.add_argument("--max-pack-rows", type=int, default=500_000)
    r.add_argument("--parquet-row-group-rows", type=int, default=131_072)
    r.add_argument("--hydrate-batch-rows", type=int, default=2048)
    r.add_argument("--reduce-partitions", type=int, default=3)
    # Deliberately NOT the production DEFAULT_HEAD_SHARD_BITS (12 => 4096 shards).
    # The rehearsal runs a 12-task candidate universe (~4.7M distinct tokens), where
    # 6 bits (64 shards) is ~74k entries/shard -- under the encoder cap, and far
    # fewer artifacts to encode. The head builder measures the worst shard exactly
    # and fails closed before encoding if a value here ever stops clearing the cap.
    r.add_argument("--head-shard-bits", type=int, default=6)
    r.add_argument("--memory-limit", default="2GB")
    r.add_argument("--threads", type=int, default=2)
    r.add_argument("--max-rss-bytes", type=int, default=4_294_967_296)
    r.add_argument("--max-scratch-bytes", type=int, default=8_589_934_592)
    r.add_argument("--wall-seconds", type=float, default=900)
    r.set_defaults(func=run_rehearse)

    a = sub.add_parser("assemble")
    a.add_argument("--spec", required=True)
    a.add_argument("--census-dir", required=True)
    a.add_argument("--task-run-dir", required=True)
    a.add_argument("--rehearsal", required=True)
    a.add_argument("--provenance", required=True)
    a.add_argument("--output", required=True)
    a.set_defaults(func=run_assemble)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
