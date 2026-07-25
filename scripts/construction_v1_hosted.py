#!/usr/bin/env python3
"""Construction-v1 native hosted adapter.

This is the missing piece between ``construction_v1_control.py`` (fail-closed
admission and the typed confirmation) and the vectorized Address/Places data
planes (``address_construction_v1.py`` / ``places_construction_v1.py``). It:

* derives a deterministic contract + runtime from the reviewed request, so no
  hosted job depends on a file that was never produced;
* wraps each phase (admit / map / plan / reduce / head / finalize) as a CLI
  subcommand driving the real data plane against a single content-addressed
  store. With ``--staging-root`` / ``--staging-bucket`` that store lives in a
  run-scoped R2 staging prefix and the local directory is only a cache, so the
  63.5 GB planet Places store no longer travels between jobs as an artifact
  (``scripts/construction_staging_v1.py``, ``docs/plans/2026-07-24-r2-staging-design.md``);
  without them it is the legacy artifact-carried directory;
* publishes only the final slice create-only through the backend-neutral
  ``construction_v1_remote`` primitives (create-only, per-upload HEAD, marker
  written last, one exact-prefix listing + one streaming read per object); and
* keeps an honest runner-minute ledger and fails closed *before* provisioning
  the next phase when prior + projected minutes exceed the confirmation cap.

Every subcommand runs with no network. The end-to-end execute sequence is
proven against a tmpdir store in tests/test_construction_v1_hosted.py, which is
what stops a planet attempt from burning runner-minutes on a file-not-found.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import importlib.util
import json
import math
import platform
import sys
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

# P0-3: the reduce matrix is capped at 256 entries by the workflow, and the
# admission cost model budgets at most ``max_reducers_per_family`` reducer jobs.
# When a planet family produces MORE partitions than that (planet addresses are
# >=474 partitions at the 1M-row cap; planet Places exceeds it too once every
# spatial cell is enumerated), we do NOT weaken the partition plan -- partition
# sizing is a pre-genesis contract. Instead each reduce matrix JOB processes a
# contiguous BATCH of partitions serially, so the job COUNT stays under the cap
# while per-partition markers and serving outputs are unchanged.
REDUCE_MATRIX_CAP = 256

# The reduce matrix runs under this per-JOB timeout (construction-v1.yml reduce
# job timeout-minutes). A batch job reduces its partitions serially, so its wall
# time must fit here.
REDUCE_JOB_TIMEOUT_MINUTES = 330
# Fraction of the job timeout usable for reduce work; the remainder is margin for
# checkout, cargo build, and artifact IO on the runner.
REDUCE_TIMEOUT_MARGIN_FRACTION = 0.5
# Measured per-partition reduce wall time (minutes), with a safety factor over a
# real census-task measurement, used BOTH to bound batch_size under the job
# timeout AND to project honest reduce runner-minutes. Places: the densest CJK
# census tasks (86 densest_spatial + 87 token_fanout, release 2026-06-17.0,
# planet HOSTED_LIMITS) reduce a near-1M-term-row plain partition in ~7-10s and
# the worst subdivided/high-amplification partition in 14.53s (~0.24 min) end to
# end (directory select + serving encode + Rust verify). 1.0 min is a >4x margin
# covering hotter cells, R2 latency, and slower hosted runners than the bradflix
# measurement host. Addresses: no fresh planet reduce measurement in this change;
# 2.0 min is a deliberately conservative upper bound (address reduce is a
# directory select + encode + verify over <=1M rows, comparable-or-lighter).
MEASURED_REDUCE_MINUTES_PER_PARTITION = {"places": 1.0, "addresses": 2.0}


def _timeout_max_batch(per_partition_minutes: float, *, job_timeout: int, margin: float) -> int:
    """Largest number of partitions one reduce job can serially fit in its timeout."""
    usable = job_timeout * margin
    return max(1, int(usable // per_partition_minutes))


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ADDRESS = _load("construction_v1_hosted_address", "scripts/address_construction_v1.py")
PLACES = _load("construction_v1_hosted_places", "scripts/places_construction_v1.py")
REMOTE = _load("construction_v1_hosted_remote", "scripts/construction_v1_remote.py")
CONTROL = _load("construction_v1_hosted_control", "scripts/construction_v1_control.py")
STAGING = _load("construction_v1_hosted_staging", "scripts/construction_staging_v1.py")

FAMILIES = ("addresses", "places")

# The map phase's per-RECORD artifact, per family. ONE publication seam covers
# both: same fail-closed gate at finalize, same create-only verified publication,
# same manifest listing, same result keys. Only the marker key, the published
# sub-prefix, and the schema string differ.
#
# Both families are in this table, so a missing per-record set is a GAP for either
# of them -- never "correct". (This replaced a places-only set, whose comment said
# a missing addresses set was correct because the address shuffle had not been
# ported. That reasoning was superseded: addresses now emit a spatially keyed
# records artifact for the reverse index WITHOUT the forward shuffle port, which
# is still deliberately deferred.)
#
# Publishing them is not decoration. The map store otherwise travels only as a
# GitHub artifact with 7-day retention, so eight days after a planet run the
# per-record packs would be gone and a spatial reverse index would cost exactly
# the full planet map re-run these artifacts exist to avoid.
PER_RECORD_ARTIFACTS: dict[str, dict[str, str]] = {
    "addresses": {
        "marker_key": "address_records",
        "prefix": "records",
        "schema": ADDRESS.ADDRESS_RECORDS_SCHEMA,
    },
    "places": {
        "marker_key": "positions",
        "prefix": "positions",
        "schema": PLACES.POSITIONS_SCHEMA,
    },
}


def _per_record_artifact(marker: dict[str, Any], family: str) -> dict[str, Any] | None:
    """One marker's per-record artifact, or None when it carries none."""
    spec = PER_RECORD_ARTIFACTS.get(family)
    if spec is None:
        return None
    value = marker.get(spec["marker_key"])
    return value if isinstance(value, dict) else None

# Conservative bounded hosted limits. They stay well under a 330-minute job and
# are overridable per run through the contract so a rehearsal can shrink them.
HOSTED_LIMITS: dict[str, dict[str, Any]] = {
    "addresses": {
        "max_input_rows": 4_000_000,
        "max_pack_rows": 1_000_000,
        "parquet_row_group_rows": 65_536,
        "max_rss_bytes": 12 * 1024**3,
        "max_scratch_bytes": 24 * 1024**3,
        "max_output_bytes": 8 * 1024**3,
        "max_serving_bytes": 2 * 1024**3,
        "wall_seconds": 18_000,
        "duckdb_memory_limit": "8GB",
        "duckdb_threads": 4,
        "allow_unpinned_duckdb": False,
    },
    "places": {
        "max_input_rows": 4_000_000,
        "max_pack_rows": 1_500_000,
        "parquet_row_group_rows": 65_536,
        "max_rss_bytes": 12 * 1024**3,
        "max_scratch_bytes": 24 * 1024**3,
        "max_output_bytes": 8 * 1024**3,
        "wall_seconds": 18_000,
        "allow_unpinned_duckdb": False,
        # P1-2: DuckDB fit for the 16GiB hosted runner. The Places dense-task
        # scratch measured ~5.6GiB, so 1GB/2-thread defaults (places_construction
        # _v1.Limits) would spill pathologically; pin the same runner-fit values
        # the Address family already uses (address_construction_v1.py:517-518).
        "duckdb_memory_limit": "8GB",
        "duckdb_threads": 4,
        # P0-2: the planet Places inventory has 89 map tasks
        # (benchmarks/places-construction-v1-data/inventory/places.json
        # map_plan.task_count). adaptive_genesis_plan, the head phase, and the
        # global-head builder all fan in EVERY map marker at once and fail closed
        # above max_fan_in_tasks (places_construction_v1.py:628,1060,1202); the
        # dataclass default is 16, far below 89. 128 admits all 89 with headroom.
        # max_fan_in_packs is the per-batch DuckDB read stride over the fanned-in
        # packs (places_construction_v1.py:647), not a total-pack cap; 256 bounds
        # each planning read while keeping the number of INSERT batches small.
        "max_fan_in_tasks": 128,
        "max_fan_in_packs": 256,
        # Raised from 1,000,000 / 200,000 after the 2026-07-22.0 growth test
        # (docs/plans/2026-07-24-growth-test-and-path-to-planet.md, appendix A).
        # Two independent reasons, one per cap:
        #
        #   term_rows      A hash prefix cannot split a SINGLE token, so the
        #                  largest (cell, token) pair is a hard floor no depth
        #                  can lower -- measured at 742,392 rows for
        #                  ('b2e3','jp'), 74.2% of the old cap. 2,000,000 takes
        #                  that margin from 1.35x to 2.69x.
        #   distinct_tokens  Cell a1d5 breached the old 200,000 cap outright at
        #                  201,568 after five weeks of drift.
        #
        # The byte cap stays: at the worst observed 178 bytes/row, 2,000,000
        # rows is 356 MB against 512 MiB. 3,000,000 (534 MB) would exceed it, so
        # 2,000,000 is the largest row cap this byte cap admits.
        #
        # Raising these REDUCES partition count because fewer partitions are
        # forced to subdivide; the budget floor is the 16,633 populated cells,
        # not the splits. The committed plan lands at 16,888 partitions.
        #
        # Caveat on the term_rows rationale above: the map-side combiner landed
        # in the same change and takes that 742,392-row group to 1,078, so the
        # indivisible floor is no longer what binds. 2,000,000 still buys real
        # margin -- the largest post-combiner partition is 944,978 rows, which
        # would sit at 94% of the OLD cap -- but the 'jp' figure is the
        # pre-combiner motivation, not the current one.
        #
        # These exceed three declared hard caps in the frozen evidence spec
        # (partition_term_rows_hard_cap 1000000,
        # partition_distinct_tokens_hard_cap 250000,
        # partition_estimated_uncompressed_bytes_hard_cap 268435456), and that
        # divergence is DELIBERATE and one-directional:
        #
        #   spec v2 -> rehearse_places_construction_v1.spec_partition_caps()
        #           -> P.Limits -> adaptive_genesis_plan (subdivide, or fail at
        #              maximum depth)
        #
        # so the spec's caps bind the REHEARSAL, which is what they were written
        # for -- the spec is frozen against release 2026-06-17.0 and a 12-task
        # candidate universe. The hosted planet build runs the raised caps above
        # instead, on the measured justification in this comment.
        # tests/test_construction_v1_preflight.py pins both sides, so neither can
        # move without a test naming the other. Closing the gap means a places
        # evidence spec v3 plus a rehearsal re-run (the spec's relaxation policy
        # is "none"), not an edit here or there; see
        # docs/plans/2026-07-24-construction-v1-follow-ups.md.
        "partition_term_rows": 2_000_000,
        "partition_estimated_bytes": 512 * 1024**2,
        "partition_distinct_tokens": 400_000,
    },
}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text())


def _family_module(family: str):
    return ADDRESS if family == "addresses" else PLACES


def _limits_for(contract: dict[str, Any], family: str):
    module = _family_module(family)
    values = dict(contract["limits"][family])
    fields = {field.name for field in dataclasses.fields(module.Limits)}
    filtered = {key: value for key, value in values.items() if key in fields}
    return module.Limits(**filtered)


def _store(args: argparse.Namespace, *, request_sha256: str | None = None):
    """The phase's object store: local-only, or local cache over R2 staging.

    Without staging flags this is byte-for-byte the previous behaviour -- one
    content-addressed directory the workflow carries job -> job as an artifact.

    With them the local directory becomes a CACHE and the record of truth is the
    run-scoped R2 staging prefix, so a phase runs with an EMPTY local store and
    fetches only the keys its markers name. That is the whole point: the 63.5 GB
    map store stops travelling between phases. Consumers DISCOVER their objects by
    deriving `staging_prefix(request_sha256, family)` + the key already recorded in
    the marker/plan/reduction JSON -- there is no listing and no side manifest.
    """
    local = ADDRESS.LocalObjectStore(Path(args.store_root))
    store_root = getattr(args, "staging_root", None)
    bucket = getattr(args, "staging_bucket", None)
    endpoint_url = getattr(args, "staging_endpoint_url", None)
    if store_root is None and not bucket and not endpoint_url:
        return local
    if request_sha256 is None:
        raise SystemExit(
            "R2 staging needs the contract's request_sha256 to derive its "
            "run-scoped prefix; pass --contract"
        )
    backend = STAGING.staging_backend(
        store_root=store_root, bucket=bucket, endpoint_url=endpoint_url
    )
    return STAGING.StagedObjectStore(
        local, backend, STAGING.staging_prefix(request_sha256, args.family)
    )


def _staging_evidence(store: Any) -> dict[str, Any]:
    """Staging transport counters, or {} for a local-only store.

    Reported so a run that was SUPPOSED to fetch its inputs from staging and
    silently found them all locally is visible: `staged_objects_hydrated == 0` in
    a consumer phase means the local cache was not empty, which is the failure
    mode this whole increment exists to remove.
    """
    evidence = getattr(store, "evidence", None)
    return evidence() if callable(evidence) else {}


def _write_staging_report(args: argparse.Namespace, store: Any) -> None:
    """Persist the staging counters when --staging-report was given.

    stdout interleaves with stderr under `/usr/bin/time -v`, so a phase whose only
    machine-readable surface is a printed line is not assertable. The reduce and
    plan phases have no result FILE of their own, so they get one here.
    """
    report = getattr(args, "staging_report", None)
    if report:
        write_json(report, _staging_evidence(store))


def _add_staging_arguments(parser: argparse.ArgumentParser) -> None:
    """Flags selecting the R2 staging transport for the intermediate store.

    Mutually exclusive backends on purpose: `--staging-root` is a local directory
    (credential-free, used by the slice harness and the tests), `--staging-bucket`
    plus `--staging-endpoint-url` is real R2. Omitting all three keeps the legacy
    artifact-carried store, so this is opt-in per phase rather than a flag day.
    """
    parser.add_argument(
        "--staging-root",
        default=None,
        help="Filesystem staging root (credential-free): the intermediate store "
             "is mirrored here create-only and hydrated from here on read.",
    )
    parser.add_argument(
        "--staging-bucket",
        default=None,
        help="R2 bucket holding the run-scoped staging prefix for the "
             "intermediate store.",
    )
    parser.add_argument(
        "--staging-endpoint-url",
        default=None,
        help="R2 S3-compatible endpoint URL for --staging-bucket.",
    )
    parser.add_argument(
        "--staging-report",
        type=Path,
        default=None,
        help="Write this phase's staging transport counters here, including the "
             "peak resident hydrated bytes that decide whether it fits a runner.",
    )


def _reduce_marker_key(family: str, index: int) -> str:
    return f"reduce/{family}/tasks/{index:04d}/complete.json"


def _head_marker_key() -> str:
    return "head/places/complete.json"


# --------------------------------------------------------------------------- #
# derive-contract
# --------------------------------------------------------------------------- #
def cmd_derive_contract(args: argparse.Namespace) -> int:
    request = read_json(args.request)
    request_sha256 = hashlib.sha256(CONTROL.canonical(request)).hexdigest()
    limits = {family: dict(HOSTED_LIMITS[family]) for family in FAMILIES}
    if args.allow_unpinned_duckdb:
        for family in FAMILIES:
            limits[family]["allow_unpinned_duckdb"] = True
    if args.map_input_rows_cap is not None:
        for family in FAMILIES:
            limits[family]["max_input_rows"] = args.map_input_rows_cap
    contract = {
        "schema": "construction-v1-contract-v1",
        "request_sha256": request_sha256,
        "release": request.get("release"),
        "families": request.get("families", {}),
        "namespaces": request.get("namespaces", {}),
        "caps": request.get("caps", {}),
        "limits": limits,
    }
    write_json(args.output, contract)

    pinned = request.get("versions", {})
    runtime = {
        "schema": "construction-v1-runtime-v1",
        "request_sha256": request_sha256,
        "python": platform.python_version(),
        "pinned": {key: pinned.get(key) for key in ("python", "duckdb", "pyarrow", "numpy", "rustc")},
        "strict_versions": bool(args.strict_versions),
    }
    if args.strict_versions:
        try:
            import duckdb  # noqa: WPS433

            if duckdb.__version__ != pinned.get("duckdb"):
                raise SystemExit(
                    f"runner duckdb {duckdb.__version__} != pinned {pinned.get('duckdb')}"
                )
            runtime["duckdb"] = duckdb.__version__
        except ImportError as error:
            raise SystemExit("duckdb is required in strict-version mode") from error
    write_json(args.runtime, runtime)
    print(json.dumps({"request_sha256": request_sha256, "contract": str(args.output)}, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# admit-task
# --------------------------------------------------------------------------- #
def _remote_marker_completed(remote_root: str, key: str, contract: dict[str, Any]) -> bool:
    """Read-only HEAD of a durable create-only marker in the remote store.

    P1-4 fail-closed direction: a definitively ABSENT marker means "not
    completed" (the task re-runs, which is safe under create-only writes). Any
    HEAD transport error ABORTS instead of re-running, so a flaky remote can
    never silently drop create-only discipline by masquerading as absence.
    """
    budget = REMOTE.Budget(
        max_operations=int(contract.get("caps", {}).get("max_remote_operations", 100_000)),
        max_write_bytes=0,
        max_read_bytes=int(contract.get("caps", {}).get("max_remote_write_bytes", 1_000_000_000_000)),
    )
    remote = REMOTE.FilesystemRemote(Path(remote_root), budget)
    try:
        info = remote.head(key)
    except Exception as error:  # noqa: BLE001 - abort on any transport failure
        raise SystemExit(
            f"remote marker HEAD failed for {key}; aborting rather than re-running "
            f"to preserve create-only discipline: {error}"
        )
    return info is not None


def cmd_admit_task(args: argparse.Namespace) -> int:
    # The contract is optional for a purely local marker check and REQUIRED once
    # staging is in play, because the staging prefix is derived from its
    # request_sha256. _store fails closed on the second case.
    admit_contract = read_json(args.contract) if args.contract else {}
    store = _store(args, request_sha256=admit_contract.get("request_sha256"))
    if args.phase == "map":
        key = _family_module(args.family).marker_key(args.task_id)
    elif args.phase == "reduce":
        key = _reduce_marker_key(args.family, args.index)
    elif args.phase == "head":
        key = _head_marker_key()
    else:
        raise SystemExit(f"unknown phase {args.phase}")
    # Within a single run the local content-addressed store records completion.
    # With R2 staging that store is a local CACHE over the run-scoped staging
    # prefix, so `read_json` also consults staging and a fresh resume dispatch
    # sees genuinely completed tasks with no local store at all -- which is the
    # durable record the --remote-root HEAD path was standing in for.
    existing = store.read_json(key)
    local_completed = existing is not None
    remote_completed = False
    if args.remote_root:
        remote_key = f"{args.remote_marker_prefix.rstrip('/')}/{key}" if args.remote_marker_prefix else key
        remote_completed = _remote_marker_completed(
            args.remote_root, remote_key, admit_contract
        )
    completed = local_completed or remote_completed
    result = {
        "phase": args.phase,
        "family": args.family,
        "marker_key": key,
        "completed": completed,
        "local_completed": local_completed,
        "remote_completed": remote_completed,
    }
    # A SKIPPED task must still contribute its marker to the fan-in, or the plan
    # phase silently plans without it. That was harmless while the store travelled
    # as an artifact -- a fresh runner had no local store, so nothing was ever
    # skipped -- but the staged marker makes resume real, so the skip path now has
    # to materialise the marker it is skipping on.
    if args.marker_out and completed:
        if existing is None:
            raise SystemExit(
                f"admit-task --marker-out was requested for {key} but the marker's "
                "payload is not readable (completion was observed only through a "
                "HEAD). Refusing to report a skip that would drop this task from "
                "the fan-in."
            )
        write_json(args.marker_out, existing)
        result["marker_out"] = str(args.marker_out)
    if args.output:
        write_json(args.output, result)
    print(json.dumps(result, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# run-map
# --------------------------------------------------------------------------- #
def cmd_run_map(args: argparse.Namespace) -> int:
    contract = read_json(args.contract)
    store = _store(args, request_sha256=contract["request_sha256"])
    limits = _limits_for(contract, args.family)
    request_sha256 = contract["request_sha256"]
    common = dict(
        input_path=Path(args.input),
        source_limits=Path(args.source_limits),
        store=store,
        scratch_root=Path(args.scratch_dir),
        request_sha256=request_sha256,
        task_id=args.task_id,
        transform_binary=Path(args.transform_binary),
        limits=limits,
    )
    if args.family == "addresses":
        marker = ADDRESS.map_task(directory_binary=Path(args.proof_binary), **common)
    else:
        marker = PLACES.map_task(proof_binary=Path(args.proof_binary), **common)
    write_json(args.marker_out, marker)
    summary = {
        "task_id": args.task_id,
        "family": args.family,
        "admitted_existing": marker.get("admitted_existing"),
        "records": marker["binding"]["records"],
        "packs": len(marker["packs"]),
    }
    # Both families emit a per-RECORD artifact from map alongside the serving
    # packs -- Places per place, addresses per address, each on the same 256x256
    # cell scheme and the same shuffle. Report it under ONE key pair regardless of
    # family so a log consumer needs no per-family branch; nothing downstream of
    # map consumes them yet.
    per_record = _per_record_artifact(marker, args.family)
    if per_record is not None:
        summary["positions_packs"] = len(per_record["packs"])
        summary["positions_records"] = per_record["records"]
    summary.update(_staging_evidence(store))
    if args.output:
        write_json(args.output, summary)
    print(json.dumps(summary, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# plan-reduce
# --------------------------------------------------------------------------- #
def _load_markers(markers_dir: str) -> list[dict[str, Any]]:
    paths = sorted(Path(markers_dir).glob("*.json"))
    if not paths:
        raise SystemExit(f"no map markers found under {markers_dir}")
    return [json.loads(path.read_text()) for path in paths]


def _reduce_batches(
    partition_count: int, *, job_cap: int, timeout_max_batch: int | None = None
) -> tuple[int, list[dict[str, int]]]:
    """Split ``partition_count`` partitions into at most ``job_cap`` contiguous
    batches. ``batch_size`` is the smallest stride that keeps the job count under
    the cap, so a family with more partitions than the cap still fits one matrix.

    ``timeout_max_batch`` (derived from the MEASURED per-partition reduce time and
    the job timeout) fails closed when the batch a legal matrix would require
    cannot serially fit one job's timeout -- batching must never silently produce
    a job that times out.
    """
    if partition_count <= 0:
        raise SystemExit("plan-reduce produced no partitions")
    if job_cap <= 0:
        raise SystemExit("reduce job cap must be positive")
    batch_size = max(1, math.ceil(partition_count / job_cap))
    if timeout_max_batch is not None and batch_size > timeout_max_batch:
        raise SystemExit(
            f"reduce needs batch_size {batch_size} to fit {job_cap} jobs, but the "
            f"measured per-partition reduce time only admits {timeout_max_batch} "
            f"partitions per {REDUCE_JOB_TIMEOUT_MINUTES}-min job; failing closed"
        )
    batches = [
        {
            "batch_index": index,
            "partition_start": start,
            "partition_count": min(batch_size, partition_count - start),
        }
        for index, start in enumerate(range(0, partition_count, batch_size))
    ]
    if len(batches) > job_cap:  # pragma: no cover - arithmetic guarantees this
        raise SystemExit("reduce batching failed to fit the job cap")
    return batch_size, batches


def _reduce_bucket_ranges(
    partitions_per_bucket: list[int],
    *,
    job_cap: int,
    timeout_max_batch: int | None = None,
) -> tuple[int, list[dict[str, int]]]:
    """Split the SHUFFLE-BUCKET space into at most ``job_cap`` contiguous ranges.

    Places reduce jobs own a bucket range, not a partition range, because the map
    fragments are keyed by bucket: a range consumer's input is the fragments in
    its range, read once each. Ranges are contiguous strides over the whole
    bucket space, inclusive on both ends, mirroring
    ``build_id_index.py --prefix-start/--prefix-end``.

    The returned cover is TOTAL and DISJOINT over ``0..len(partitions_per_bucket)-1``
    whether or not every bucket is populated, which is what makes ownership exact:
    a cell hashes to one bucket, a bucket lies in one range. Ranges with no
    partitions are returned with ``partition_count`` 0 so the cover stays visible;
    the caller drops them from the dispatch matrix because they would read nothing.

    ``partition_start`` is valid because a Places plan is ORDERED by shuffle
    bucket, so a bucket range is also a contiguous partition range.

    A bucket is indivisible -- a cell never splits across buckets -- so unlike
    partition batching there is no smaller stride to fall back on. When even a
    single-bucket range holds more partitions than the job timeout admits, this
    fails closed rather than dispatching a job that cannot finish.
    """
    bucket_count = len(partitions_per_bucket)
    if bucket_count <= 0:
        raise SystemExit("reduce bucket space must be non-empty")
    if job_cap <= 0:
        raise SystemExit("reduce job cap must be positive")
    stride = max(1, math.ceil(bucket_count / job_cap))
    ranges: list[dict[str, int]] = []
    partition_start = 0
    for index, start in enumerate(range(0, bucket_count, stride)):
        end = min(start + stride, bucket_count) - 1
        count = sum(partitions_per_bucket[start : end + 1])
        if timeout_max_batch is not None and count > timeout_max_batch:
            raise SystemExit(
                f"reduce bucket range [{start},{end}] holds {count} partitions, but "
                f"the measured per-partition reduce time only admits "
                f"{timeout_max_batch} per {REDUCE_JOB_TIMEOUT_MINUTES}-min job"
                + (
                    "; a bucket is indivisible, so raise the job cap or the bucket "
                    "count instead"
                    if stride == 1
                    else "; raise the job cap to shorten the stride"
                )
            )
        ranges.append(
            {
                "batch_index": index,
                "bucket_start": start,
                "bucket_end": end,
                "partition_start": partition_start,
                "partition_count": count,
            }
        )
        partition_start += count
    if len(ranges) > job_cap:  # pragma: no cover - arithmetic guarantees this
        raise SystemExit("reduce bucket ranges failed to fit the job cap")
    return stride, ranges


def _places_partitions_per_bucket(
    partitions: list[dict[str, Any]], bits: int
) -> list[int]:
    """Per-bucket partition counts, asserting the plan is bucket-ordered."""
    counts = [0] * (1 << bits)
    previous = -1
    for partition in partitions:
        bucket = PLACES.partition_shuffle_bucket(partition, bits)
        if bucket < previous:
            raise SystemExit(
                "places plan partitions are not ordered by shuffle bucket, so a "
                "bucket range would not be a contiguous partition range; re-run "
                "plan-reduce with a current plan"
            )
        previous = bucket
        counts[bucket] += 1
    return counts


def _places_reduce_execution(
    partitions: list[dict[str, Any]],
    *,
    bits: int,
    job_cap: int,
    timeout_max_batch: int | None,
    fragment_buckets: set[int] | None = None,
) -> tuple[int, list[dict[str, int]], dict[str, Any]]:
    """Bucket-range reduce execution for a Places plan."""
    if not partitions:
        raise SystemExit("plan-reduce produced no partitions")
    counts = _places_partitions_per_bucket(partitions, bits)
    stride, cover = _reduce_bucket_ranges(
        counts, job_cap=job_cap, timeout_max_batch=timeout_max_batch
    )
    dispatched = [dict(item) for item in cover if item["partition_count"]]
    for index, item in enumerate(dispatched):
        item["batch_index"] = index
    if sum(item["partition_count"] for item in dispatched) != len(partitions):
        raise SystemExit("reduce bucket ranges do not cover every partition once")
    # Ranges with no partitions are NOT dispatched, so a bucket that holds map
    # data but no plan partition would be silently skipped -- and the in-job
    # "fragments but no partitions" guard would never run to catch it. Check it
    # here, where the map markers are still in hand.
    if fragment_buckets is not None:
        covered = {
            bucket
            for item in dispatched
            for bucket in range(item["bucket_start"], item["bucket_end"] + 1)
        }
        orphans = sorted(bucket for bucket in fragment_buckets if bucket not in covered)
        if orphans:
            raise SystemExit(
                f"map fragments occupy shuffle buckets {orphans} that no dispatched "
                "reduce range covers; the plan is missing those cells"
            )
    batch_size = max(item["partition_count"] for item in dispatched)
    details = {
        "shuffle_bucket_bits": bits,
        "bucket_count": 1 << bits,
        "bucket_stride": stride,
        "bucket_ranges": len(cover),
        "populated_bucket_ranges": len(dispatched),
    }
    return batch_size, dispatched, details


def cmd_plan_reduce(args: argparse.Namespace) -> int:
    contract = read_json(args.contract)
    store = _store(args, request_sha256=contract["request_sha256"])
    limits = _limits_for(contract, args.family)
    markers = _load_markers(args.markers_dir)
    if args.family == "addresses":
        row_cap = args.row_cap or limits.max_pack_rows
        plan = ADDRESS.genesis_plan(markers, row_cap=row_cap)
    else:
        plan = PLACES.adaptive_genesis_plan(
            markers, store=store, scratch_root=Path(args.scratch_dir), limits=limits
        )

    partition_count = len(plan["partitions"])
    # The cap on reducer JOBS is the tighter of the workflow matrix cap and the
    # admitted ``max_reducers_per_family`` budget the cost model is built on.
    max_reducers = int(contract.get("caps", {}).get("max_reducers_per_family", REDUCE_MATRIX_CAP))
    job_cap = min(REDUCE_MATRIX_CAP, max_reducers)
    if args.max_reduce_jobs is not None:
        job_cap = min(job_cap, args.max_reduce_jobs)
    per_partition = (
        args.reduce_minutes_per_partition
        if args.reduce_minutes_per_partition is not None
        else MEASURED_REDUCE_MINUTES_PER_PARTITION[args.family]
    )
    timeout_max_batch = _timeout_max_batch(
        per_partition, job_timeout=args.job_timeout_minutes, margin=args.timeout_margin
    )
    # Places reduce jobs own a bucket RANGE; the address family still batches
    # contiguous partition indexes, because it has no shuffle yet (deferred, see
    # docs/plans/2026-07-24-construction-v1-follow-ups.md).
    details: dict[str, Any] = {}
    if args.family == "places":
        batch_size, batches, details = _places_reduce_execution(
            plan["partitions"],
            bits=int(limits.shuffle_bucket_bits),
            job_cap=job_cap,
            timeout_max_batch=timeout_max_batch,
            fragment_buckets={
                int(pack["shuffle_bucket"])
                for marker in markers
                for pack in marker["packs"]
            },
        )
    else:
        batch_size, batches = _reduce_batches(
            partition_count, job_cap=job_cap, timeout_max_batch=timeout_max_batch
        )
    job_count = len(batches)
    per_job_minutes = math.ceil(batch_size * per_partition)
    timing = {
        "measured_reduce_minutes_per_partition": per_partition,
        "job_timeout_minutes": args.job_timeout_minutes,
        "timeout_margin": args.timeout_margin,
        "timeout_max_batch": timeout_max_batch,
        "per_job_minutes": per_job_minutes,
    }
    plan["reduce_execution"] = {
        "schema": (
            "construction-v1-reduce-execution-v2"
            if args.family == "places"
            else "construction-v1-reduce-execution-v1"
        ),
        "ownership": "shuffle-bucket-range" if args.family == "places" else "partition-batch",
        "partition_count": partition_count,
        "batch_size": batch_size,
        "job_count": job_count,
        "job_cap": job_cap,
        "matrix_cap": REDUCE_MATRIX_CAP,
        "timing_assumption": timing,
        "batches": batches,
        **details,
    }
    write_json(args.output, plan)

    # The matrix has ONE entry per reducer JOB (batch), never per partition, so a
    # family with more partitions than the cap still dispatches a legal matrix.
    matrix = {"include": batches}
    if args.matrix_out:
        write_json(args.matrix_out, matrix)

    # P0-3 + measured economics: gate the ledger on the honest total reduce
    # minutes (partitions x measured per-partition time, batching-invariant),
    # plus the fixed head/finalize tails. Fails closed here -- before the reduce
    # matrix is provisioned -- when the projection exceeds cap.
    ledger_check: dict[str, Any] | None = None
    if args.ledger is not None:
        ledger = read_json(args.ledger)
        prior = int(ledger.get("prior_runner_minutes", 0))
        spent = sum(int(item["runner_minutes"]) for item in ledger.get("phases", []))
        cap = int(ledger["max_total_runner_minutes"])
        projected_reduce = math.ceil(partition_count * per_partition)
        projected = prior + spent + projected_reduce + int(args.tail_minutes)
        ledger_check = {
            "prior_runner_minutes": prior,
            "spent_runner_minutes": spent,
            "reduce_job_count": job_count,
            "measured_reduce_minutes_per_partition": per_partition,
            "projected_reduce_minutes": projected_reduce,
            "tail_minutes": int(args.tail_minutes),
            "projected_runner_minutes": projected,
            "max_total_runner_minutes": cap,
            "within_cap": projected <= cap,
        }
        if projected > cap:
            raise SystemExit(
                f"projected {projected} runner minutes exceed cap {cap} "
                f"(prior={prior} spent={spent} reduce={partition_count}x"
                f"{per_partition}min tail={args.tail_minutes}); "
                "failing closed before provisioning the reduce matrix"
            )

    summary = {
        "family": args.family,
        "partitions": partition_count,
        "reduce_batch_size": batch_size,
        "reduce_job_count": job_count,
        "reduce_job_cap": job_cap,
        "timing_assumption": timing,
        "binding": plan["binding"],
    }
    if ledger_check is not None:
        summary["ledger_check"] = ledger_check
    # The plan phase reads pack BODIES, so its resident high-water mark is the
    # number that decides whether this job fits a runner. Reported, not assumed.
    summary.update(_staging_evidence(store))
    _write_staging_report(args, store)
    print(json.dumps(summary, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# source-limits (per-object transform bound derived from the projection report)
# --------------------------------------------------------------------------- #
def cmd_source_limits(args: argparse.Namespace) -> int:
    """Emit the transform's per-object source-limits from a projection report.

    The transform rejects any locator whose ``source_row_group >= groups`` or
    ``source_row_index >= records`` for its object (main.rs). Projected rows keep
    their ORIGINAL row-group index (0..N) and per-group row offset, so a single
    ``row_groups:1`` bound would reject nearly every real row. This derives a
    correct, safe UPPER bound (object total rows / total row groups) per object,
    indexed by object_index, with (1,1) placeholders for unread objects.
    """
    report = read_json(args.report)
    per_object: dict[int, tuple[int, int]] = {}
    if args.family == "addresses":
        for source in report["sources"]:
            per_object[int(source["source_object_index"])] = (
                int(source["parquet_rows"]),
                int(source["parquet_row_groups"]),
            )
    else:
        identity = report["identity"]
        selected = sorted({int(item["object_index"]) for item in identity["ranges"]})
        objects = identity["objects"]
        if len(selected) != len(objects):
            raise SystemExit("places projection report object/range mismatch")
        for object_index, obj in zip(selected, objects):
            per_object[object_index] = (int(obj["records"]), int(obj["row_group_count"]))
    if not per_object:
        raise SystemExit("projection report exposes no source objects")
    limits = []
    for object_index in range(max(per_object) + 1):
        records, groups = per_object.get(object_index, (1, 1))
        limits.append({"records": records, "row_groups": groups})
    payload = {"objects": limits}
    write_json(args.output, payload)
    print(json.dumps({"objects": len(limits), "read_objects": sorted(per_object)}, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# predict-reduce (dry-run capacity certification, no network)
# --------------------------------------------------------------------------- #
def _inventory_total_records(inventory: dict[str, Any]) -> int:
    totals = inventory.get("totals") or {}
    if "records" in totals:
        return int(totals["records"])
    plan = inventory.get("map_plan") or inventory.get("plan") or {}
    tasks = plan.get("tasks") or []
    if not tasks:
        raise SystemExit("inventory has neither totals.records nor map-plan tasks")
    return sum(int(task.get("rows", task.get("expected_input_records", 0))) for task in tasks)


COMMITTED_PLACES_PARTITION_PLAN = ROOT / "scripts/places_partition_plan_v1.json"


def _committed_plan_partitions(path: Path | None) -> tuple[int, str]:
    """Partition count recorded by the committed Places partition plan.

    Fails closed on a missing or malformed plan rather than silently falling
    back to the row-derived estimate, which is a lower bound (see the caller).
    """
    plan_path = path or COMMITTED_PLACES_PARTITION_PLAN
    plan = read_json(plan_path)
    recorded = plan.get("generated_from", {}).get("partitions")
    if not isinstance(recorded, int) or recorded < 1:
        raise SystemExit(f"committed partition plan {plan_path} records no partition count")
    return recorded, f"committed plan {plan_path.name} partitions"


def _address_uniform_partition_estimate(
    inventory: dict[str, Any], row_cap: int
) -> tuple[int, str]:
    """Address genesis partition count MODELLED from the plan's shape, not its size.

    `ceil(total_records / row_cap)` is not an upper bound on the address partition
    count, for the same class of reason it was not one for Places (see the places
    branch below): the plan is not a flat division of rows.
    `address_construction_v1._plan_from_summaries` bisects EACH COUNTRY
    independently over `hash_bucket`, so two effects push the true count above the
    total-row figure and neither is visible in a total row count:

    * every country with any rows contributes at least one partition;
    * a country over the cap splits by HALVING, so its leaf count is a power of
      two rather than `ceil(country_rows / row_cap)`.

    This is an ESTIMATE, not a bound in either direction, and its precondition is
    that `route_hash` spreads a country's rows evenly across `hash_bucket`:

    * it can OVERSHOOT under even spreading, because the real planner stops
      bisecting a subtree as soon as that subtree fits: 4,000,003 rows spread over
      8 even buckets at a 1,000,000 cap is model 8, real planner 5;
    * it can UNDERSHOOT under in-country bucket skew, because each split isolates
      light siblings that are already under the cap and each of those becomes a
      leaf: a heavy adjacent pair plus ten scattered light buckets, 2,000,000 rows
      total, is model 2, real planner 6.

    Both figures are from running `address_construction_v1._plan_from_summaries`
    itself on those inputs, not from reasoning about it.

    `route_hash` IS uniform by construction (FNV-1a over 8 normalized fields,
    high bits taken), which is why on the real planet inventory the model lands at
    725 against a simulated real 725 -- but that is the model being well
    conditioned on this data, not a guarantee.

    The genuine per-country LOWER bound is `sum(ceil(country_rows / row_cap))` =
    493 on the planet inventory, still above the 474 the total-row division gives.
    It is recorded here for context and deliberately not used: it is a weaker
    prediction than the uniform model on the data this actually runs against, and
    the point of the prediction is to stop under-provisioning the reduce matrix.

    Fails closed when the inventory carries no per-country row counts, matching
    `_committed_plan_partitions`: a silent fallback to the total-row figure is
    exactly the optimism this exists to remove.
    """
    per_country = inventory.get("exact_country_rows")
    if not isinstance(per_country, dict) or not per_country:
        raise SystemExit(
            "address inventory records no exact_country_rows, so the per-country "
            "partition estimate cannot be derived; rebuild the inventory with "
            "scripts/inventory_address_rowgroups.py"
        )
    attributed = sum(int(rows) for rows in per_country.values())
    if attributed <= 0:
        raise SystemExit("address inventory exact_country_rows are all zero")
    total = _inventory_total_records(inventory)
    # Rows in mixed-or-unknown-country row groups are not attributed to any
    # country. Spread them proportionally rather than dropping them: dropping
    # them would make the estimate optimistic again by exactly their share.
    scale = max(1.0, total / attributed)
    # address_construction_v1 refuses to subdivide past 16 hash bits.
    maximum_leaves = 1 << 16
    estimate = 0
    for rows in per_country.values():
        leaves = 1
        scaled = int(rows) * scale
        while scaled / leaves > row_cap and leaves < maximum_leaves:
            leaves *= 2
        estimate += leaves
    return estimate, (
        f"per-country uniform hash-bisection estimate over {len(per_country)} "
        f"inventory countries (x{scale:.4f} for unattributed rows)"
    )


def cmd_predict_reduce(args: argparse.Namespace) -> int:
    """Predict the reduce partition/batch/minute demand from committed inventory
    statistics, with no map run and no network, so a dry-run fails closed when a
    real execute could not fit the matrix, reducer, or minute budget."""
    contract = read_json(args.contract)
    limits = _limits_for(contract, args.family)
    inventory = read_json(args.inventory)
    total_records = _inventory_total_records(inventory)

    if args.family == "addresses":
        row_cap = args.row_cap or limits.max_pack_rows
        # Each genesis partition holds at most ``row_cap`` records, so the total-row
        # division is a lower bound -- but NOT an upper one, and it was the only
        # term here, which is the same optimism the places branch had (PR #155).
        # Take the larger of it and the per-country uniform bisection ESTIMATE of
        # the plan the address planner actually builds (see that function for what
        # the estimate is and is not).
        by_rows = math.ceil(total_records / row_cap)
        modelled, model_basis = _address_uniform_partition_estimate(inventory, row_cap)
        predicted_partitions = max(by_rows, modelled)
        basis = (
            f"max(ceil({total_records} records / {row_cap} row cap) = {by_rows}, "
            f"{model_basis} = {modelled})"
        )
    else:
        # Places reduces TERM rows (one output term batch per input feature batch,
        # up to MAX_TERMS_PER_FEATURE terms/feature); use that conservative upper
        # bound against the per-partition term-row cap.
        terms_per_feature = PLACES.MAX_TERMS_PER_FEATURE
        term_rows = total_records * terms_per_feature
        by_rows = math.ceil(term_rows / limits.partition_term_rows)
        # The term-row division alone is NOT an upper bound, and treating it as
        # one made this gate 14x optimistic. Places partition count is floored by
        # the number of POPULATED SPATIAL CELLS -- one partition per cell before
        # any subdivision -- and that floor is completely independent of the row
        # cap. Dividing by a larger cap therefore shrinks the estimate while the
        # truth does not move: at the 2,000,000 cap this predicted 1,211
        # partitions against a real 16,888.
        #
        # The committed partition plan records the real structural count, so use
        # it as the floor. max() keeps the gate conservative in both directions:
        # if a release genuinely needs more subdivision than the committed tree
        # has, the row-derived figure still wins.
        floor, floor_basis = _committed_plan_partitions(
            getattr(args, "partition_plan", None)
        )
        predicted_partitions = max(by_rows, floor)
        basis = (
            f"max(ceil({total_records} features x {terms_per_feature} terms / "
            f"{limits.partition_term_rows} term-row cap) = {by_rows}, "
            f"{floor_basis} = {floor})"
        )
    predicted_partitions = max(1, predicted_partitions)

    max_reducers = int(contract.get("caps", {}).get("max_reducers_per_family", REDUCE_MATRIX_CAP))
    job_cap = min(REDUCE_MATRIX_CAP, max_reducers)
    if args.max_reduce_jobs is not None:
        job_cap = min(job_cap, args.max_reduce_jobs)
    per_partition = (
        args.reduce_minutes_per_partition
        if args.reduce_minutes_per_partition is not None
        else MEASURED_REDUCE_MINUTES_PER_PARTITION[args.family]
    )
    timeout_max_batch = _timeout_max_batch(
        per_partition, job_timeout=args.job_timeout_minutes, margin=args.timeout_margin
    )
    details: dict[str, Any] = {}
    if args.family == "places":
        # Places reduce jobs own bucket ranges, so predict the same way plan-reduce
        # decides: spread the predicted partitions over the bucket space and cover
        # it in contiguous strides. Uniform spreading is the right model rather
        # than a convenience -- the bucket is a multiplicative hash of the cell,
        # so cell COUNTS per bucket are uniform by construction (only the
        # data-weighted distribution is skewed, and reduce is planned per
        # partition). The structural partition floor from the committed plan is
        # unchanged and still supplies `predicted_partitions`.
        bits = int(limits.shuffle_bucket_bits)
        buckets = 1 << bits
        base, extra = divmod(predicted_partitions, buckets)
        counts = [base + (1 if index < extra else 0) for index in range(buckets)]
        stride, cover = _reduce_bucket_ranges(
            counts, job_cap=job_cap, timeout_max_batch=timeout_max_batch
        )
        populated = [item for item in cover if item["partition_count"]]
        if not populated:
            raise SystemExit("predicted reduce has no populated bucket ranges")
        batch_size = max(item["partition_count"] for item in populated)
        batches = populated
        details = {
            "shuffle_bucket_bits": bits,
            "bucket_count": buckets,
            "bucket_stride": stride,
            "bucket_ranges": len(cover),
            "populated_bucket_ranges": len(populated),
            "reduce_ownership": "shuffle-bucket-range",
        }
    else:
        batch_size, batches = _reduce_batches(
            predicted_partitions, job_cap=job_cap, timeout_max_batch=timeout_max_batch
        )
    job_count = len(batches)

    projected_reduce = math.ceil(predicted_partitions * per_partition)
    result: dict[str, Any] = {
        "family": args.family,
        "total_records": total_records,
        "predicted_partitions": predicted_partitions,
        "prediction_basis": basis,
        "reduce_batch_size": batch_size,
        "reduce_job_count": job_count,
        "reduce_job_cap": job_cap,
        "matrix_cap": REDUCE_MATRIX_CAP,
        "timing_assumption": {
            "measured_reduce_minutes_per_partition": per_partition,
            "job_timeout_minutes": args.job_timeout_minutes,
            "timeout_margin": args.timeout_margin,
            "timeout_max_batch": timeout_max_batch,
            "per_job_minutes": math.ceil(batch_size * per_partition),
        },
        "projected_reduce_minutes": projected_reduce,
        "fits_matrix_cap": job_count <= REDUCE_MATRIX_CAP,
        "fits_reducer_cap": job_count <= max_reducers,
        **details,
    }
    if job_count > REDUCE_MATRIX_CAP or job_count > max_reducers:
        raise SystemExit(
            f"predicted reduce needs {job_count} jobs; exceeds matrix cap "
            f"{REDUCE_MATRIX_CAP} / reducer cap {max_reducers}"
        )
    if args.ledger is not None:
        ledger = read_json(args.ledger)
        prior = int(ledger.get("prior_runner_minutes", 0))
        spent = sum(int(item["runner_minutes"]) for item in ledger.get("phases", []))
        cap = int(ledger["max_total_runner_minutes"])
        projected = prior + spent + projected_reduce + int(args.tail_minutes)
        result.update({
            "prior_runner_minutes": prior,
            "spent_runner_minutes": spent,
            "tail_minutes": int(args.tail_minutes),
            "projected_runner_minutes": projected,
            "max_total_runner_minutes": cap,
            "within_cap": projected <= cap,
        })
        if projected > cap:
            raise SystemExit(
                f"predicted {projected} runner minutes exceed cap {cap} "
                f"(prior={prior} spent={spent} reduce={predicted_partitions}x"
                f"{per_partition}min tail={args.tail_minutes})"
            )
    if args.output:
        write_json(args.output, result)
    print(json.dumps(result, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# run-reduce
# --------------------------------------------------------------------------- #
def _reduce_one_partition(
    args: argparse.Namespace,
    *,
    store: Any,
    limits: Any,
    plan: dict[str, Any],
    markers: list[dict[str, Any]],
    partition_index: int,
) -> dict[str, Any]:
    partition = plan["partitions"][partition_index]
    if args.family == "addresses":
        reduction = ADDRESS.reduce_partition(
            partition=partition,
            markers=markers,
            store=store,
            scratch_root=Path(args.scratch_dir),
            directory_binary=Path(args.proof_binary),
            encoder_binary=Path(args.encoder_binary),
            verifier_binary=Path(args.verifier_binary),
            limits=limits,
        )
    else:
        reduction = PLACES.reduce_partition(
            partition=partition,
            plan=plan,
            markers=markers,
            store=store,
            scratch_root=Path(args.scratch_dir),
            encoder_binary=Path(args.encoder_binary),
            verifier_binary=Path(args.verifier_binary),
            limits=limits,
        )
    # A per-partition completion marker in the LOCAL store: it records completion
    # within this run (carried job->job as an artifact). A fresh RESUME dispatch
    # has no local store and instead relies on the durable create-only marker in
    # the REMOTE store, which admit-task consults read-only via --remote-root.
    # Batching is an execution grouping only: the marker and serving artifact are
    # byte-identical to the unbatched plan.
    store.write_marker_last(
        _reduce_marker_key(args.family, partition_index),
        {"partition_index": partition_index,
         "artifact": reduction.get(REDUCTION_SERVING_OBJECTS[args.family])},
    )
    return reduction


def _reduce_bucket_range(
    args: argparse.Namespace,
    *,
    store: Any,
    limits: Any,
    plan: dict[str, Any],
    markers: list[dict[str, Any]],
    bucket_start: int,
    bucket_end: int,
    expected_partitions: list[int] | None = None,
) -> dict[str, Any]:
    """Reduce one INCLUSIVE shuffle-bucket range, writing one file per partition.

    The range owns every partition whose cell hashes into it, and reads each map
    fragment in the range exactly once. Output paths are still keyed by PLAN
    partition index, so finalize, the marker keys, and the published object set
    are unchanged by how the ranges were cut.

    ``expected_partitions`` is the partition range the PLAN assigned to this job.
    It is checked before a single marker is written, because a completion marker
    is a create-only OWNERSHIP CLAIM: once published to the run-scoped staging
    prefix it can never be replaced, so a marker written for a partition this job
    did not own leaves the run unresumable. Checking after publication -- which is
    where this check used to live -- also hid the real diagnosis, since the first
    thing to fail was an opaque "existing completion marker differs" from the
    colliding marker rather than the ownership message below.
    """
    if args.family != "places":
        raise SystemExit("bucket-range reduce is Places-only; addresses batch by partition")
    if args.output_dir is None:
        raise SystemExit("bucket-range reduce requires --output-dir")
    result = PLACES.reduce_bucket_range(
        bucket_start=bucket_start,
        bucket_end=bucket_end,
        plan=plan,
        markers=markers,
        store=store,
        scratch_root=Path(args.scratch_dir),
        encoder_binary=Path(args.encoder_binary),
        verifier_binary=Path(args.verifier_binary),
        limits=limits,
    )
    emitted = result["partition_indexes"]
    if expected_partitions is not None and emitted != expected_partitions:
        assigned, produced = set(expected_partitions), set(emitted)
        raise SystemExit(
            "reduce bucket range emitted partitions the plan did not assign to "
            "this batch; the plan and the reducer disagree about ownership. "
            f"Reducer emitted {len(emitted)}, plan assigned "
            f"{len(expected_partitions)}; emitted-but-unassigned "
            f"{sorted(produced - assigned)[:8]}, assigned-but-missing "
            f"{sorted(assigned - produced)[:8]}"
        )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for partition_index, reduction in zip(
        result["partition_indexes"], result["reductions"], strict=True
    ):
        write_json(output_dir / f"{partition_index:04d}.json", reduction)
        store.write_marker_last(
            _reduce_marker_key(args.family, partition_index),
            {"partition_index": partition_index,
             "artifact": reduction.get(REDUCTION_SERVING_OBJECTS[args.family])},
        )
    return result


def cmd_run_reduce(args: argparse.Namespace) -> int:
    contract = read_json(args.contract)
    store = _store(args, request_sha256=contract["request_sha256"])
    limits = _limits_for(contract, args.family)
    plan = read_json(args.plan)
    markers = _load_markers(args.markers_dir)

    # Bucket-range mode (Places): --bucket-start/--bucket-end directly, mirroring
    # build_id_index.py --prefix-start/--prefix-end.
    if args.bucket_start is not None or args.bucket_end is not None:
        if args.bucket_start is None or args.bucket_end is None:
            raise SystemExit("--bucket-start and --bucket-end must be given together")
        result = _reduce_bucket_range(
            args, store=store, limits=limits, plan=plan, markers=markers,
            bucket_start=args.bucket_start, bucket_end=args.bucket_end,
        )
        print(json.dumps({"family": args.family, "bucket_start": result["bucket_start"],
                          "bucket_end": result["bucket_end"],
                          "partitions": result["partition_indexes"],
                          "fragments_opened": result["fragments_opened"],
                          **_staging_evidence(store)}, sort_keys=True))
        _write_staging_report(args, store)
        return 0

    # Batch mode: one reducer JOB processes a contiguous partition range serially
    # and writes one output per partition into --output-dir (0000.json ...). This
    # is what keeps the reduce matrix under the cap for planet-scale families.
    if args.batch_index is not None or args.output_dir is not None:
        if args.output_dir is None:
            raise SystemExit("batch reduce requires --output-dir")
        batches = plan.get("reduce_execution", {}).get("batches")
        if not batches:
            raise SystemExit("plan has no reduce_execution batches; re-run plan-reduce")
        if args.batch_index is None or not 0 <= args.batch_index < len(batches):
            raise SystemExit("reduce --batch-index is outside the plan")
        batch = batches[args.batch_index]
        # A Places plan records each job as a bucket range; --batch-index selects
        # one, so the workflow matrix stays a single key and the dispatch shape is
        # unchanged. Addresses keep the partition-batch path.
        if batch.get("bucket_start") is not None:
            result = _reduce_bucket_range(
                args, store=store, limits=limits, plan=plan, markers=markers,
                bucket_start=batch["bucket_start"], bucket_end=batch["bucket_end"],
                # Checked INSIDE, before any marker is published -- see the
                # docstring. A create-only marker for a partition this job does not
                # own cannot be taken back.
                expected_partitions=list(
                    range(batch["partition_start"],
                          batch["partition_start"] + batch["partition_count"])
                ),
            )
            print(json.dumps({"family": args.family, "batch_index": args.batch_index,
                              "bucket_start": result["bucket_start"],
                              "bucket_end": result["bucket_end"],
                              "partition_start": batch["partition_start"],
                              "partition_count": batch["partition_count"],
                              "partitions": result["partition_indexes"],
                              "fragments_opened": result["fragments_opened"],
                              **_staging_evidence(store)},
                             sort_keys=True))
            _write_staging_report(args, store)
            return 0
        start = batch["partition_start"]
        count = batch["partition_count"]
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        processed = []
        for partition_index in range(start, start + count):
            reduction = _reduce_one_partition(
                args, store=store, limits=limits, plan=plan,
                markers=markers, partition_index=partition_index,
            )
            write_json(output_dir / f"{partition_index:04d}.json", reduction)
            processed.append(partition_index)
        result = {"family": args.family, "batch_index": args.batch_index,
                  "partition_start": start, "partition_count": count,
                  "partitions": processed, **_staging_evidence(store)}
        print(json.dumps(result, sort_keys=True))
        _write_staging_report(args, store)
        return 0

    # Legacy single-partition mode (one job per partition).
    if args.partition_index is None or args.output is None:
        raise SystemExit("single-partition reduce requires --partition-index and --output")
    reduction = _reduce_one_partition(
        args, store=store, limits=limits, plan=plan,
        markers=markers, partition_index=args.partition_index,
    )
    write_json(args.output, reduction)
    print(json.dumps({"family": args.family, "partition_index": args.partition_index,
                      "artifact": reduction.get(
                          REDUCTION_SERVING_OBJECTS[args.family]),
                      **_staging_evidence(store)}, sort_keys=True))
    _write_staging_report(args, store)
    return 0


# --------------------------------------------------------------------------- #
# run-head (Places only)
# --------------------------------------------------------------------------- #
def cmd_run_head(args: argparse.Namespace) -> int:
    contract = read_json(args.contract)
    store = _store(args, request_sha256=contract["request_sha256"])
    if args.family != "places":
        write_json(args.output, {"family": args.family, "head": None, "note": "no global head phase"})
        summary: dict[str, Any] = {"family": args.family, "head": None}
    else:
        limits = _limits_for(contract, "places")
        markers = _load_markers(args.markers_dir)
        result = PLACES.build_sharded_global_head_from_markers(
            markers=markers,
            store=store,
            scratch_root=Path(args.scratch_dir),
            encoder_binary=Path(args.encoder_binary),
            verifier_binary=Path(args.verifier_binary),
            limits=limits,
            shard_bits=args.shard_bits,
        )
        store.write_marker_last(_head_marker_key(), {"shard_count": result["shard_count"],
                                                     "total_records": result["total_records"]})
        write_json(args.output, {**result, **_staging_evidence(store)})
        summary = {"family": "places", "shard_count": result["shard_count"],
                   "populated_shards": result["populated_shards"]}
    # ONE report site for both branches. The head phase hydrates EVERY task's
    # head-candidate pack, and `build_sharded_global_head_from_markers` hands them
    # all to a single `read_parquet([...])`, so unlike plan it cannot batch-and-evict
    # without a restructure. Measured rather than bounded -- see the follow-up.
    _write_staging_report(args, store)
    print(json.dumps({**summary, **_staging_evidence(store)}, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# finalize
# --------------------------------------------------------------------------- #
# The key under which each family's reducer records its SERVING object. A table,
# not `reduction.get("artifact")`, because the two reducers name it differently and
# the mismatch was silent: a Places reduction carries `routed_object` and
# `leaf_object` and NO `artifact`, so `.get("artifact")` returned None, the falsy
# branch skipped it, and a Places finalize published head shards, positions packs
# and two manifests while dropping every `.plrv` serving payload. Every existing
# check still passed -- the reconciliation compares BINDINGS out of the reduction
# JSON and never looks at the published object set.
#
# `leaf_object` is deliberately NOT here. The leaf is a build intermediate: the
# reduce phase proves it against the plan binding and the head phase reads it, and
# it holds TERM rows (~7.19 per place), so publishing it would multiply the served
# volume for something nothing serves. The per-place positions packs are the
# durable per-record artifact, and they are published through their own seam.
REDUCTION_SERVING_OBJECTS: dict[str, str] = {
    "addresses": "artifact",
    "places": "routed_object",
}


# Families whose finalize MUST publish a global head. Places builds a sharded head
# in `run-head`; addresses have no head phase at all and their `run-head` writes
# `{"head": null}`. A table rather than a truthiness test on `head`, because
# `head.get("shard_objects", [])` defaulting to empty is the same permissive-get
# pattern that lost the routed objects -- it published a places slice with ZERO
# `.plhd` shards, exit 0, including the shape where head.json claims
# `shard_count: 16` while the tree holds none.
HEAD_FAMILIES = ("places",)


def _serving_identity(value: Any, *, what: str) -> dict[str, Any]:
    """A publishable object identity, or abort naming what was wrong.

    Requires the three fields finalize's pre-publication verification consumes
    (`key` to locate it, `sha256`/`bytes` to prove it is what its producer
    recorded). A truthy-but-malformed entry used to reach that check and die with a
    raw KeyError/TypeError traceback instead of saying which object was broken.
    """
    if not isinstance(value, dict) or not all(
        isinstance(value.get(field), expected)
        for field, expected in (("key", str), ("sha256", str), ("bytes", int))
    ):
        raise SystemExit(
            f"{what} is not a publishable object identity (need key/sha256/bytes): "
            f"{value!r}"
        )
    return value


def _artifact_keys(family: str, reductions: list[dict[str, Any]], head: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Every serving object the slice must publish, one per reduction plus the head.

    FAIL CLOSED on BOTH halves. The previous `if artifact:` skip is how the Places
    routed objects went missing without a single check firing, and the
    `head.get("shard_objects", [])` default is the identical defect one line down --
    a places slice with no head shards at all published cleanly, reported
    `reconciles: true`, and satisfied the workflow's own gates. A missing object is
    now an abort that names what it expected.
    """
    key = REDUCTION_SERVING_OBJECTS[family]
    objects: list[dict[str, Any]] = []
    for reduction in reductions:
        artifact = reduction.get(key)
        partition = (reduction.get("partition") or {}).get("id", "<unknown>")
        if not artifact:
            raise SystemExit(
                f"{family} reduction for partition {partition} records no "
                f"{key!r}; its serving object cannot be published, so the slice "
                "would be missing that partition's payload entirely"
            )
        objects.append(
            _serving_identity(artifact, what=f"{family} {key} for partition {partition}")
        )
    # One serving object per partition, and at least one overall: the published set
    # must COVER the reduction set, which is the property that was violated.
    if len(objects) != len(reductions) or not objects:
        raise SystemExit(  # pragma: no cover - the loop above guarantees this
            f"{family} serving object set does not cover every reduction"
        )

    if family in HEAD_FAMILIES:
        # `--head` omitted entirely, or pointing at an address-shaped result.
        if not head or head.get("head", "present") is None:
            raise SystemExit(
                f"finalize for the {family} family requires a --head result: its "
                "global head shards are half the serving surface, and publishing "
                "the slice without them would ship routed shards with no head"
            )
        shards = head.get("shard_objects")
        populated = head.get("populated_shards")
        if not isinstance(shards, list) or not shards:
            raise SystemExit(
                f"{family} head result records no shard_objects; a head that "
                "produced no shards is not publishable. head.json says "
                f"shard_count={head.get('shard_count')!r} "
                f"populated_shards={populated!r}"
            )
        # The count the head phase REPORTED must equal the objects it handed over,
        # which is the shape the review published: shard_count 16, zero shards.
        if not isinstance(populated, int) or len(shards) != populated:
            raise SystemExit(
                f"{family} head result lists {len(shards)} shard objects but "
                f"reports populated_shards={populated!r}; the head phase and its "
                "published set disagree"
            )
        objects.extend(
            _serving_identity(shard, what=f"{family} head shard {index}")
            for index, shard in enumerate(shards)
        )
        # The head MANIFEST is a serving object, not build-plane bookkeeping. Head
        # shards are content-addressed, so the manifest is the only thing that maps
        # a shard id to the object that answers it, and the only thing that records
        # `shard_bits`. Publishing the shards without it leaves a reader probing
        # every shard and relying on the Worker's misroute rejection: 16 probes per
        # query at shard_bits=4, 4096 at the designed 12 -- i.e. an unusable
        # dataset. It was collected nowhere (`_artifact_keys` took `shard_objects`
        # and not `manifest_object`), so it fails closed here like everything else.
        objects.append(
            _serving_identity(
                head.get("manifest_object"), what=f"{family} head manifest"
            )
        )
    elif head and head.get("shard_objects"):
        raise SystemExit(
            f"the {family} family has no global head phase, but its head result "
            "carries shard objects; refusing to publish them"
        )
    return objects


def _positions_objects(
    markers: list[dict[str, Any]], family: str
) -> list[dict[str, Any]]:
    """One family's map-phase per-record packs and directories, deduped by key.

    These are NOT serving artifacts, and they are published for one reason: the
    store otherwise travels only as a GitHub artifact with a 7-day retention, so
    eight days after a planet run the per-record packs would be gone and a spatial
    reverse index would cost the full map re-run these artifacts exist to avoid.
    Publishing them puts them in durable storage under the same create-only,
    verified-once treatment as everything else in the slice.

    Family-generic: `PER_RECORD_ARTIFACTS` says which marker key to read, so
    Places positions and address records travel the identical path.

    Deduplicated by key because the keys are content-addressed: two tasks that
    produced byte-identical bytes are one object, and the exact-set publisher
    rejects a repeated key.
    """
    unique: dict[str, dict[str, Any]] = {}
    for marker in markers:
        per_record = _per_record_artifact(marker, family)
        if per_record is None:
            continue
        for pack in per_record["packs"]:
            for identity in (pack["object"], pack["directory_object"]):
                unique[identity["key"]] = identity
    return sorted(unique.values(), key=lambda item: item["key"])


def cmd_finalize(args: argparse.Namespace) -> int:
    contract = read_json(args.contract)
    store = _store(args, request_sha256=contract["request_sha256"])
    plan = read_json(args.plan)
    reductions = [read_json(path) for path in sorted(Path(args.reductions_dir).glob("*.json"))]
    if not reductions:
        raise SystemExit("finalize requires at least one reduction")
    head = read_json(args.head) if args.head and Path(args.head).exists() else None

    # `reconciles` is REPORTED BY THE VALIDATOR for both families now. It used to
    # be a constant on the places branch, checked only against the summed binding,
    # so a duplicated partition standing in for a missing one -- or two partitions
    # that swapped their outputs -- reported `reconciles: true`.
    reconciliation = _family_module(args.family).validate_complete_reduction(
        plan, reductions
    )

    request_sha256 = contract["request_sha256"]
    slice_root = contract["namespaces"]["slice"].rstrip("/")
    artifacts = _artifact_keys(args.family, reductions, head)
    if len({item["key"] for item in artifacts}) != len(artifacts):
        raise SystemExit("duplicate serving artifact key")
    # Map-phase per-RECORD packs, published alongside the serving set so they
    # outlive the 7-day artifact retention. One seam for both families
    # (PER_RECORD_ARTIFACTS): Places positions and address records.
    #
    # FAIL CLOSED when a family that carries them cannot publish them. Skipping
    # publication on a missing --markers-dir would be the durable-publication
    # failure one level up: a workflow wiring mistake would quietly produce a
    # slice with no per-record packs, and the cost of noticing is a full planet
    # map re-run. BOTH families are in the table, so neither gets a free pass.
    per_record_spec = PER_RECORD_ARTIFACTS.get(args.family)
    if per_record_spec is not None and not args.markers_dir:
        raise SystemExit(
            f"finalize --markers-dir is required for the {args.family} family: its "
            f"map phase emits {per_record_spec['schema']} packs, and they must be "
            "published durably rather than expiring with the map artifact retention"
        )
    positions_markers = (
        _load_markers(args.markers_dir)
        if args.markers_dir and per_record_spec is not None
        else []
    )
    # And a marker that carries no per-record artifact is a gap too: it means one
    # map task in this slice predates the artifact, so the published set would be
    # missing exactly that task's records.
    if any(
        _per_record_artifact(marker, args.family) is None
        for marker in positions_markers
    ):
        raise SystemExit(
            f"a {args.family} map marker carries no "
            f"{per_record_spec['marker_key']} artifact; its per-record packs cannot "
            "be published, so the slice would be incomplete. A marker written "
            "before the artifact existed cannot be upgraded in place (markers are "
            "write-once): delete that task's marker and re-run its map task."
        )
    positions = _positions_objects(positions_markers, args.family)
    positions_records = sum(
        _per_record_artifact(marker, args.family)["records"]
        for marker in positions_markers
    )
    if {item["key"] for item in positions} & {item["key"] for item in artifacts}:
        raise SystemExit("per-record object collides with a serving artifact key")

    # Enough for a reader to route a token with no probing: `shard_bits` says how to
    # compute the shard id, and `manifest` names the published object holding the
    # shard_id -> object map (content-addressed, so its name is its digest and
    # `sha256` re-verifies it). Gated on HEAD_FAMILIES rather than on `head` being
    # truthy: an address-shaped `{"head": null}` result is a truthy dict, and reading
    # head fields off it would be a KeyError traceback instead of a null head block.
    head_block = None
    if args.family in HEAD_FAMILIES:
        # `_artifact_keys` above already fails closed on a head family with no head
        # result, no shards, or no manifest object, so every field here exists.
        head_block = {
            "shard_count": head["shard_count"],
            "shard_bits": head["shard_bits"],
            "populated_shards": head["populated_shards"],
            "total_records": head["total_records"],
            "manifest": {
                "object": PurePosixPath(head["manifest_object"]["key"]).name,
                "sha256": head["manifest_object"]["sha256"],
                "bytes": head["manifest_object"]["bytes"],
            },
        }

    family_manifest = {
        "schema": "construction-v1-family-manifest-v1",
        "family": args.family,
        "request_sha256": request_sha256,
        "reconciles": reconciliation["reconciles"],
        "binding": reconciliation["binding"],
        "partitions": reconciliation["partitions"],
        "artifacts": sorted(artifacts, key=lambda item: item["key"]),
        "head": head_block,
        # Listed separately from `artifacts`: these are build-phase per-record
        # packs, not serving objects, and nothing serves them today. The key stays
        # `positions` for both families because it is already the published
        # manifest shape; `schema` says which artifact it actually is.
        "positions": {
            "schema": per_record_spec["schema"],
            "records": positions_records,
            "objects": positions,
        } if positions else None,
    }
    family_manifest_path = Path(args.work_root) / "family-manifest.json"
    write_json(family_manifest_path, family_manifest)

    slice_manifest = {
        "schema": "construction-v1-slice-manifest-v1",
        "request_sha256": request_sha256,
        "family": args.family,
        "family_manifest_sha256": hashlib.sha256(family_manifest_path.read_bytes()).hexdigest(),
        "object_count": len(artifacts) + len(positions),
        "positions_object_count": len(positions),
        "non_promoting": True,
    }
    slice_manifest_path = Path(args.work_root) / "slice-manifest.json"
    write_json(slice_manifest_path, slice_manifest)

    # Create-only publication of the exact final set, marker last.
    budget = REMOTE.Budget(
        max_operations=int(contract["caps"].get("max_remote_operations", 100_000)),
        max_write_bytes=int(contract["caps"].get("max_remote_write_bytes", 1_000_000_000_000)),
        max_read_bytes=int(contract["caps"].get("max_remote_write_bytes", 1_000_000_000_000)),
    )
    remote = REMOTE.FilesystemRemote(Path(args.remote_root), budget)
    # Every published object is hydrated ONE AT A TIME and evicted as soon as the
    # publisher is done reading it, rather than assembling a list of paths -- which
    # hydrated the whole published set onto this runner before the first upload.
    # That is 13-18 GB at planet scale (a ~10-11 GB head payload plus 3.3-6.7 GB of
    # positions packs, before the routed lane), on the last job of a multi-hour run.
    # This is the same defect the plan phase carried (#167): batching that hydrates
    # eagerly is not batching. `release` exists only on the staged store, so a
    # local-only run evicts nothing -- there the local directory IS the store.
    release = getattr(store, "release", None)

    def member(published_key: str, store_key: str) -> Any:
        return REMOTE.Member(
            key=published_key,
            hydrate=lambda: store.path(store_key),
            release=(lambda: release(store_key)) if release is not None else (lambda: None),
        )

    manifests = {
        f"{slice_root}/families/{args.family}/family-manifest.json": family_manifest_path,
        f"{slice_root}/families/{args.family}/slice-manifest.json": slice_manifest_path,
    }
    # The manifests are the only members that are not store objects: finalize wrote
    # them into its own work root a few lines above, so they are already local and
    # nothing may evict them.
    exact_set: list[Any] = [
        REMOTE.local_member(key, path) for key, path in manifests.items()
    ]
    # Recorded identity per published key, so the pre-publication check below can
    # compare the bytes on disk to what the PRODUCING phase wrote down, not just to
    # the digest in the filename.
    recorded: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        source = f"{slice_root}/families/{args.family}/objects/{Path(artifact['key']).name}"
        recorded[source] = artifact
        exact_set.append(member(source, artifact["key"]))
    # Per-family sub-prefix: `positions/` for places, `records/` for addresses --
    # so the published tree names the artifact it holds instead of calling address
    # records "positions". Both go through the same exact-set publisher and the
    # same verify_whole_slice_once below.
    for artifact in positions:
        source = (
            f"{slice_root}/families/{args.family}/{per_record_spec['prefix']}/"
            f"{Path(artifact['key']).name}"
        )
        recorded[source] = artifact
        exact_set.append(member(source, artifact["key"]))

    def verify_identity(source: str, actual: dict[str, Any]) -> None:
        """Pre-publication identity gate, run during admission.

        Every published object is content-addressed, so its NAME states its bytes
        -- and nothing used to check that the bytes at the local key matched. The
        whole-slice verification derives `expected` from the same files it
        publishes, so a local store carrying wrong bytes under a right key
        published them and reported `reconciles: true`: the reconciliation compares
        BINDINGS from the reduction JSON, which a substituted file does not touch.

        This runs as `publish_exact_set`'s admission hook rather than as a pass of
        its own, so the object is hashed once per residency instead of being
        hydrated a third time. It is still strictly before ANY upload -- admission
        completes before the publisher writes a byte -- which is the property that
        makes it a gate and not a report.
        """
        if source in manifests:
            # The only non-content-addressed members, and both were written from
            # memory a few lines above.
            return
        artifact = recorded[source]
        declared = STAGING.content_addressed_digest(artifact["key"])
        if declared is None:
            raise SystemExit(
                f"refusing to publish an object with no verifiable identity: {source}"
            )
        # Two independent checks. `declared` proves the bytes match the digest the
        # store key ASSERTS about them; `artifact` proves they also match the size
        # and digest the PRODUCING phase recorded in its reduction / head result /
        # map marker. Either alone would have caught the pre-planted shard; both
        # together mean the file, its name, and its provenance all agree.
        if actual["sha256"] != declared:
            raise SystemExit(
                f"refusing to publish {source}: the bytes at "
                f"{store.path(artifact['key'])} hash to "
                f"{actual['sha256']} but their content-addressed key declares "
                f"{declared}. The local store is not the store that produced this "
                "slice."
            )
        if actual["sha256"] != artifact["sha256"] or actual["bytes"] != artifact["bytes"]:
            raise SystemExit(
                f"published object {source} differs from the identity its producing "
                "phase recorded"
            )

    marker_key = f"{contract['namespaces']['markers'].rstrip('/')}/finalize/{args.family}.json"
    marker = REMOTE.publish_exact_set(
        remote,
        artifacts=exact_set,
        marker_key=marker_key,
        request_sha256=request_sha256,
        fail_after_upload=args.fail_after_upload,
        verify=verify_identity,
    )
    # The admitted set IS the expected set: `publish_exact_set` computed each
    # identity by streaming the same file it uploaded, and `verify_identity` gated
    # every one of them. Reusing it here (rather than a second dict of identities
    # built from paths held open across the whole phase) is what lets nothing but
    # the marker outlive the upload loop.
    expected = marker["artifacts"]
    verification = REMOTE.verify_whole_slice_once(
        remote, prefix=f"{slice_root}/families/{args.family}/", expected=expected
    )
    result = {
        "family": args.family,
        "reconciles": reconciliation["reconciles"],
        "objects": verification["objects"],
        "bytes": verification["bytes"],
        "marker_key": marker_key,
        "marker_written_last": True,
        "verification": verification,
        "positions_objects": len(positions),
        "positions_records": positions_records,
        "positions_bytes": sum(item["bytes"] for item in positions),
        # Reported so a run that publishes NO serving payload is visible from the
        # result alone. It used to be exactly that for places, and `objects`,
        # `reconciles` and `positions_objects` were all non-zero anyway.
        "serving_objects": len(artifacts),
        "reduction_serving_objects": len(reductions),
        # 1 for a head family, 0 for a family with no head phase. Reported so the
        # exact-set gates can stay one family-generic equality
        # (serving == reductions + populated shards + head manifests) instead of
        # hardcoding a "+1" that would be wrong for addresses.
        "head_manifest_objects": 1 if head_block else 0,
        "serving_object_key": REDUCTION_SERVING_OBJECTS[args.family],
        **_staging_evidence(store),
    }
    write_json(args.output, result)
    print(json.dumps({"family": args.family, "objects": verification["objects"],
                      "reconciles": reconciliation["reconciles"], "marker_written_last": True,
                      "serving_objects": len(artifacts),
                      "positions_objects": len(positions)}, sort_keys=True))
    return 0


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #
def cmd_ledger_append(args: argparse.Namespace) -> int:
    ledger = read_json(args.ledger)
    ledger.setdefault("phases", []).append({"phase": args.phase, "runner_minutes": args.minutes})
    write_json(args.ledger, ledger)
    total = int(ledger.get("prior_runner_minutes", 0)) + sum(int(item["runner_minutes"]) for item in ledger["phases"])
    print(json.dumps({"phase": args.phase, "minutes": args.minutes, "consumed_runner_minutes": total}, sort_keys=True))
    return 0


def cmd_ledger_check(args: argparse.Namespace) -> int:
    ledger = read_json(args.ledger)
    prior = int(ledger.get("prior_runner_minutes", 0))
    spent = sum(int(item["runner_minutes"]) for item in ledger.get("phases", []))
    cap = int(ledger["max_total_runner_minutes"])
    projected = prior + spent + int(args.next_phase_minutes)
    result = {
        "prior_runner_minutes": prior,
        "spent_runner_minutes": spent,
        "next_phase_minutes": int(args.next_phase_minutes),
        "projected_runner_minutes": projected,
        "max_total_runner_minutes": cap,
        "within_cap": projected <= cap,
    }
    if args.output:
        write_json(args.output, result)
    print(json.dumps(result, sort_keys=True))
    if projected > cap:
        raise SystemExit(
            f"projected {projected} runner minutes exceed cap {cap} "
            f"(prior={prior} spent={spent} next={args.next_phase_minutes}); failing closed before the next phase"
        )
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    derive = sub.add_parser("derive-contract")
    derive.add_argument("--request", type=Path, required=True)
    derive.add_argument("--output", type=Path, required=True)
    derive.add_argument("--runtime", type=Path, required=True)
    derive.add_argument("--allow-unpinned-duckdb", action="store_true")
    derive.add_argument("--strict-versions", action="store_true")
    derive.add_argument("--map-input-rows-cap", type=int, default=None)
    derive.set_defaults(func=cmd_derive_contract)

    admit = sub.add_parser("admit-task")
    admit.add_argument("--store-root", required=True)
    admit.add_argument("--family", choices=FAMILIES, required=True)
    admit.add_argument("--phase", choices=["map", "reduce", "head"], required=True)
    admit.add_argument("--task-id", default="")
    admit.add_argument("--index", type=int, default=0)
    admit.add_argument("--remote-root", default=None,
                       help="Optional durable remote store consulted read-only for resume skips.")
    admit.add_argument("--remote-marker-prefix", default=None,
                       help="Namespace prefix under which durable markers live in the remote store.")
    admit.add_argument("--contract", type=Path, default=None)
    admit.add_argument("--marker-out", type=Path, default=None,
                       help="Write the COMPLETED task's marker here, so a skipped "
                            "task still reaches the plan phase's fan-in. Fails "
                            "closed if completion was observed without a payload.")
    admit.add_argument("--output", type=Path)
    _add_staging_arguments(admit)
    admit.set_defaults(func=cmd_admit_task)

    run_map = sub.add_parser("run-map")
    run_map.add_argument("--contract", type=Path, required=True)
    run_map.add_argument("--store-root", required=True)
    run_map.add_argument("--family", choices=FAMILIES, required=True)
    run_map.add_argument("--task-id", required=True)
    run_map.add_argument("--input", required=True)
    run_map.add_argument("--source-limits", required=True)
    run_map.add_argument("--transform-binary", required=True)
    run_map.add_argument("--proof-binary", required=True)
    run_map.add_argument("--scratch-dir", required=True)
    run_map.add_argument("--marker-out", type=Path, required=True)
    run_map.add_argument("--output", type=Path)
    _add_staging_arguments(run_map)
    run_map.set_defaults(func=cmd_run_map)

    plan = sub.add_parser("plan-reduce")
    plan.add_argument("--contract", type=Path, required=True)
    plan.add_argument("--store-root", required=True)
    plan.add_argument("--family", choices=FAMILIES, required=True)
    plan.add_argument("--markers-dir", required=True)
    plan.add_argument("--scratch-dir", default="/tmp/construction-v1-plan-scratch")
    plan.add_argument("--row-cap", type=int, default=None)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--matrix-out", type=Path)
    plan.add_argument("--max-reduce-jobs", type=int, default=None,
                      help="Override the reduce job cap (default min(256, caps.max_reducers_per_family)).")
    plan.add_argument("--ledger", type=Path, default=None,
                      help="If set, fail closed when the reduce projection exceeds the ledger cap.")
    plan.add_argument("--reduce-minutes-per-partition", type=float, default=None,
                      help="Measured per-partition reduce minutes (default: the measured per-family constant).")
    plan.add_argument("--job-timeout-minutes", type=int, default=REDUCE_JOB_TIMEOUT_MINUTES)
    plan.add_argument("--timeout-margin", type=float, default=REDUCE_TIMEOUT_MARGIN_FRACTION)
    plan.add_argument("--tail-minutes", type=int, default=0,
                      help="Fixed head+finalize minutes added to the reduce projection.")
    _add_staging_arguments(plan)
    plan.set_defaults(func=cmd_plan_reduce)

    source_limits = sub.add_parser("source-limits")
    source_limits.add_argument("--report", type=Path, required=True)
    source_limits.add_argument("--family", choices=FAMILIES, required=True)
    source_limits.add_argument("--output", type=Path, required=True)
    source_limits.set_defaults(func=cmd_source_limits)

    predict = sub.add_parser("predict-reduce")
    predict.add_argument(
        "--partition-plan", type=Path, default=None,
        help="Committed Places partition plan supplying the structural partition floor "
             "(default: scripts/places_partition_plan_v1.json).")
    predict.add_argument("--contract", type=Path, required=True)
    predict.add_argument("--family", choices=FAMILIES, required=True)
    predict.add_argument("--inventory", type=Path, required=True)
    predict.add_argument("--row-cap", type=int, default=None)
    predict.add_argument("--max-reduce-jobs", type=int, default=None)
    predict.add_argument("--ledger", type=Path, default=None)
    predict.add_argument("--reduce-minutes-per-partition", type=float, default=None,
                         help="Measured per-partition reduce minutes (default: the measured per-family constant).")
    predict.add_argument("--job-timeout-minutes", type=int, default=REDUCE_JOB_TIMEOUT_MINUTES)
    predict.add_argument("--timeout-margin", type=float, default=REDUCE_TIMEOUT_MARGIN_FRACTION)
    predict.add_argument("--tail-minutes", type=int, default=0)
    predict.add_argument("--output", type=Path)
    predict.set_defaults(func=cmd_predict_reduce)

    reduce = sub.add_parser("run-reduce")
    reduce.add_argument("--contract", type=Path, required=True)
    reduce.add_argument("--store-root", required=True)
    reduce.add_argument("--family", choices=FAMILIES, required=True)
    reduce.add_argument("--plan", type=Path, required=True)
    reduce.add_argument("--markers-dir", required=True)
    reduce.add_argument("--partition-index", type=int, default=None,
                        help="Legacy single-partition mode; one reducer job per partition.")
    reduce.add_argument("--batch-index", type=int, default=None,
                        help="Batch mode: process this batch of the plan -- a shuffle-bucket "
                             "range for places, a contiguous partition range for addresses.")
    reduce.add_argument("--bucket-start", type=int, default=None,
                        help="Places bucket-range mode: first shuffle bucket owned (inclusive).")
    reduce.add_argument("--bucket-end", type=int, default=None,
                        help="Places bucket-range mode: last shuffle bucket owned (inclusive).")
    reduce.add_argument("--proof-binary", default="")
    reduce.add_argument("--encoder-binary", required=True)
    reduce.add_argument("--verifier-binary", required=True)
    reduce.add_argument("--scratch-dir", required=True)
    reduce.add_argument("--output", type=Path, default=None,
                        help="Single-partition output path (legacy mode).")
    reduce.add_argument("--output-dir", type=Path, default=None,
                        help="Batch mode output directory; writes NNNN.json per partition.")
    _add_staging_arguments(reduce)
    reduce.set_defaults(func=cmd_run_reduce)

    head = sub.add_parser("run-head")
    head.add_argument("--contract", type=Path, required=True)
    head.add_argument("--store-root", required=True)
    head.add_argument("--family", choices=FAMILIES, required=True)
    head.add_argument("--markers-dir", required=True)
    head.add_argument("--encoder-binary", default="")
    head.add_argument("--verifier-binary", default="")
    head.add_argument("--scratch-dir", default="/tmp/construction-v1-head-scratch")
    # The default is the ONE committed constant, never a re-typed literal: a 4-bit
    # (16-shard) head is ~6-8x over the encoder's MAX_INDEX_ENTRIES cap at the
    # planet token universe, and that only surfaces after map+reduce+merge are paid
    # for. See scripts/places_construction_v1.py:DEFAULT_HEAD_SHARD_BITS.
    head.add_argument("--shard-bits", type=int, default=PLACES.DEFAULT_HEAD_SHARD_BITS)
    head.add_argument("--output", type=Path, required=True)
    _add_staging_arguments(head)
    head.set_defaults(func=cmd_run_head)

    final = sub.add_parser("finalize")
    final.add_argument("--contract", type=Path, required=True)
    final.add_argument("--store-root", required=True)
    final.add_argument("--family", choices=FAMILIES, required=True)
    final.add_argument("--plan", type=Path, required=True)
    final.add_argument("--reductions-dir", required=True)
    final.add_argument("--head", type=Path, default=None)
    final.add_argument("--markers-dir", default=None,
                      help="Map markers, so the per-record packs are published durably "
                           "instead of expiring with the 7-day artifact retention. "
                           "REQUIRED for every family in PER_RECORD_ARTIFACTS, which is "
                           "both of them (places positions, address records).")
    final.add_argument("--remote-root", required=True)
    final.add_argument("--work-root", required=True)
    final.add_argument("--fail-after-upload", type=int, default=None)
    final.add_argument("--output", type=Path, required=True)
    _add_staging_arguments(final)
    final.set_defaults(func=cmd_finalize)

    append = sub.add_parser("ledger-append")
    append.add_argument("--ledger", type=Path, required=True)
    append.add_argument("--phase", required=True)
    append.add_argument("--minutes", type=int, required=True)
    append.set_defaults(func=cmd_ledger_append)

    check = sub.add_parser("ledger-check")
    check.add_argument("--ledger", type=Path, required=True)
    check.add_argument("--next-phase-minutes", type=int, default=0)
    check.add_argument("--output", type=Path)
    check.set_defaults(func=cmd_ledger_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
