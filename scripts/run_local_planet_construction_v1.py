#!/usr/bin/env python3
"""Drive a full construction-v1 Places run locally, across every map task.

``run_slice_construction_v1.py`` runs all five phases for ONE task, which is
the correctness gate. The hosted workflow runs the planet by fanning map across
runners. Neither can answer the question a workstation is actually good for:
does the HEAD MERGE complete at planet scale?

That question is memory-shaped, not time-shaped. v4's head merge died at 79% on
8.5 GiB of DuckDB spill and needed a scoped resume at 13.69 GB. A 62 GB machine
does not have that problem, so a head build that cannot finish on a runner can
finish here -- and this is the driver that lets it.

It reuses the hosted CLI phase-for-phase, so what runs here is the same data
plane a hosted job runs, not a reimplementation. Map tasks fan out across a
process pool; plan/reduce/head then run exactly once over all markers.

This is a STAGING harness. Local runs are experimentation -- evidence intended
for promotion still comes from the sanctioned path, whose evidence specs carry
sha256 pins.

Example, planet Places against a mirrored release:

    OVERTURE_SOURCE_MIRROR=/home/brad/dev/overture-local/mirror \\
    python scripts/run_local_planet_construction_v1.py \\
        --inventory /home/brad/dev/overture-local/work/inventory-places-2026-07-22.0.json \\
        --release 2026-07-22.0 --work /home/brad/dev/overture-local/planet \\
        --map-workers 4 --stop-after head
"""
from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

_SPEC = importlib.util.spec_from_file_location(
    "planet_construction_control", ROOT / "scripts/construction_v1_control.py"
)
CONTROL = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(CONTROL)

VENV = sys.executable
BIN = ROOT / "crates/target/release"
TRANSFORM = BIN / "places-transform-v1"
PROOF = BIN / "places-proof-directory"
ENCODER = BIN / "places-serving-encode-v1"
VERIFIER = BIN / "places-serving-verify-v1"

# Production head sharding: 12 bits => 4096 shards, sized so the planet token
# universe stays under the encoder's index-entry cap. The slice harness
# deliberately picks its own smaller value; a planet run must not.
PLANET_HEAD_SHARD_BITS = 12

PHASES = ("map", "reduce", "head")


def run(*argv, capture=False):
    argv = [str(a) for a in argv]
    result = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(
            f"FAILED: {' '.join(argv[:5])}\n"
            f"{result.stdout[-3000:]}\n{result.stderr[-4000:]}"
        )
    return result.stdout


def hosted(*argv):
    return run(VENV, "scripts/construction_v1_hosted.py", *argv)


def human(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.1f}s"
    return f"{seconds / 60:.1f}m"


def store_for(work: Path, phase: str) -> Path:
    """This phase's LOCAL store directory -- one per phase, never shared.

    A hosted phase runs on a fresh runner with an empty store and hydrates only
    the keys its markers name, and several guards are written against exactly
    that shape. In particular the head phase's disk check measures the whole
    store root as its "hydrated candidate cache". Share one directory across
    phases and head is charged for map's and reduce's output too: on this
    planet run that was 110.5 GB (serve 39 + map 38 + reduce 28) against an
    18.25 GB cap, so head failed on disk it never touched. That is a harness
    bug, not a pipeline bug, and per-phase roots are the fix.
    """
    root = work / f"store-{phase}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def map_one(task_index: int, work: Path, inventory: Path, contract: Path,
            store_root: Path, staging: Path, markers: Path,
            evidence_spec: Path) -> dict:
    """Project one map task and run the transform over it.

    Each task derives its OWN source-limits from its OWN projection report:
    the bound is per source object and a report only covers the objects that
    task read, so sharing one task's limits across the planet would reject
    real rows.
    """
    task_id = f"places-map-{task_index:03d}"
    scratch = work / "map-scratch" / task_id
    scratch.mkdir(parents=True, exist_ok=True)
    projected = scratch / "projected.parquet"
    report = scratch / "report.json"
    limits = scratch / "source-limits.json"

    started = time.time()
    run(VENV, "scripts/project_places_construction_v1.py",
        "--inventory", inventory,
        "--evidence-spec", evidence_spec,
        "--task-index", task_index, "--output", projected, "--report", report,
        "--max-rows", 4_000_000, "--max-groups", 72,
        "--max-selected-compressed-bytes", 536_870_912,
        "--max-selected-uncompressed-bytes", 1_000_000_000,
        "--max-output-bytes", 500_000_000)
    records = json.loads(report.read_text())["output"]["records"]
    hosted("source-limits", "--report", report, "--family", "places",
           "--output", limits)
    hosted("run-map", "--contract", contract, "--store-root", store_root,
           "--staging-root", staging,
           "--family", "places", "--task-id", task_id, "--input", projected,
           "--source-limits", limits,
           "--transform-binary", TRANSFORM, "--proof-binary", PROOF,
           "--scratch-dir", scratch / "map",
           "--marker-out", markers / f"{task_id}.json",
           "--output", scratch / "map.json")
    # The projected parquet is the bulk of working space and nothing downstream
    # reads it -- reduce and head consume the STAGED packs. Planet Places is 88
    # of these, so keeping them all would cost far more disk than the run needs.
    projected.unlink(missing_ok=True)
    return {"task_index": task_index, "records": records,
            "seconds": time.time() - started}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--evidence-spec", type=Path,
                        default=ROOT / "benchmarks/places-construction-v1-evidence-spec-v4.json")
    # Each map worker holds a projection plus a transform. Four is deliberately
    # conservative: the point of running here is head-merge headroom, and an
    # OOM during map would waste the run that headroom exists to enable.
    parser.add_argument("--map-workers", type=int, default=4)
    parser.add_argument("--reduce-workers", type=int, default=4)
    parser.add_argument("--shard-bits", type=int, default=PLANET_HEAD_SHARD_BITS)
    parser.add_argument("--stop-after", choices=PHASES, default="head")
    parser.add_argument("--max-reduce-jobs", type=int, default=None)
    # The reason a workstation run needs this at all. HOSTED_MAX_SCRATCH_BYTES
    # is 17 GiB, sized against a GitHub runner's ~25.6 GB free-disk floor, and
    # the head merge's DuckDB temp cap is derived from it as a fraction --
    # 4.2 GiB in practice. On planet Places the merge exhausts that and dies
    # with `failed to offload data block ... set by the max_temp_directory_size
    # setting`, which is the v4 failure. It is a CONFIGURED bound, not this
    # machine's: raising it is the entire point of staging here, and on 1.1 TB
    # of free disk it is safe. Left unset the run uses the hosted limits, so a
    # local run reproduces the hosted failure by default rather than silently
    # diverging from it.
    parser.add_argument("--max-scratch-gib", type=int, default=None,
                        help="LOCAL ONLY: raise the contract's places "
                             "max_scratch_bytes (hosted default 17 GiB). Keep "
                             "it below free disk -- above that the guard can no "
                             "longer fire before ENOSPC. Recorded in the run "
                             "summary so a local result is never mistaken for a "
                             "hosted-shaped one.")
    parser.add_argument("--tasks", default=None,
                        help="Comma/dash task selection (e.g. 0-9,20). "
                             "Default: every task in the inventory.")
    args = parser.parse_args(argv)

    if not TRANSFORM.exists():
        raise SystemExit(
            f"missing {TRANSFORM}; build with "
            "`cargo build --release -p geocoder-construction --bins`"
        )

    inventory = json.loads(args.inventory.read_text())
    task_count = len(inventory["map_plan"]["tasks"])
    if args.tasks:
        selected = []
        for part in args.tasks.split(","):
            if "-" in part:
                lo, hi = part.split("-", 1)
                selected.extend(range(int(lo), int(hi) + 1))
            else:
                selected.append(int(part))
        tasks = sorted(set(selected))
        if tasks and (tasks[0] < 0 or tasks[-1] >= task_count):
            raise SystemExit(f"--tasks outside 0..{task_count - 1}")
    else:
        tasks = list(range(task_count))

    work = args.work
    work.mkdir(parents=True, exist_ok=True)
    markers = work / "markers"
    markers.mkdir(exist_ok=True)
    staging = work / "staging"

    mirror = os.environ.get("OVERTURE_SOURCE_MIRROR")
    print(f"release      {args.release}")
    print(f"inventory    {inventory['inventory_sha256'][:16]}... "
          f"{inventory['totals']['records']:,} records")
    print(f"tasks        {len(tasks)} of {task_count}")
    print(f"source       {mirror or 'S3 (anonymous)'}")
    print(f"head shards  2^{args.shard_bits} = {1 << args.shard_bits}")
    print(f"work         {work}", flush=True)

    # --- contract ---------------------------------------------------------
    request = work / "request.json"
    request.write_text(json.dumps({
        "schema": "overture-construction-v1-request-v1",
        "release": args.release,
        "families": {"addresses": {}, "places": {}},
        "versions": {"duckdb": "1.5.1", "pyarrow": "25.0.0", "numpy": "2.3.5",
                     "python": "3.11", "rustc": "local"},
        "caps": {"max_remote_operations": CONTROL.CAPS["max_remote_operations"],
                 "max_reducers_per_family": CONTROL.CAPS["max_reducers_per_family"],
                 "max_remote_write_bytes": 1_000_000_000_000},
        # Namespaced away from both the slice harness and any hosted run, so a
        # local experiment can never publish into a real prefix.
        "namespaces": {
            "immutable_root": "construction-v1/local-planet-places",
            "slice": "construction-v1/local-planet-places/slice/slice-1/",
            "markers": "construction-v1/local-planet-places/markers/",
        },
    }) + "\n")
    contract = work / "contract.json"
    hosted("derive-contract", "--request", request, "--output", contract,
           "--runtime", work / "runtime.json", "--allow-unpinned-duckdb")
    if args.max_scratch_gib is not None:
        # Patched AFTER derivation rather than threaded through the request:
        # the request digest is what binds a run to its reviewed shape, and a
        # local disk allowance must not change it. The limits are not part of
        # that digest.
        document = json.loads(contract.read_text())
        raised = args.max_scratch_gib * 1024**3
        hosted_default = document["limits"]["places"]["max_scratch_bytes"]
        document["limits"]["places"]["max_scratch_bytes"] = raised
        contract.write_text(json.dumps(document, indent=2) + "\n")
        print(f"limits       max_scratch_bytes {hosted_default / 1024**3:.0f} GiB "
              f"-> {args.max_scratch_gib} GiB (LOCAL override)", flush=True)

    # --- map --------------------------------------------------------------
    started = time.time()
    pending = [t for t in tasks
               if not (markers / f"places-map-{t:03d}.json").exists()]
    if len(pending) != len(tasks):
        print(f"\n=== run-map: {len(tasks) - len(pending)} already complete, "
              f"resuming {len(pending)} ===", flush=True)
    else:
        print(f"\n=== run-map x{len(pending)} "
              f"({args.map_workers} workers) ===", flush=True)
    done = 0
    total_records = 0
    with concurrent.futures.ProcessPoolExecutor(args.map_workers) as pool:
        futures = {
            pool.submit(map_one, index, work, args.inventory, contract,
                        store_for(work, "map"), staging, markers,
                        args.evidence_spec): index
            for index in pending
        }
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                result = future.result()
            except Exception as error:  # noqa: BLE001 - report and keep going
                print(f"  task {index:03d} FAILED: {error}", flush=True)
                raise
            done += 1
            total_records += result["records"]
            rate = (time.time() - started) / done
            print(f"  [{done}/{len(pending)}] task {index:03d} "
                  f"{result['records']:>9,} records {human(result['seconds'])} "
                  f"(eta {human(rate * (len(pending) - done))})", flush=True)
    map_seconds = time.time() - started
    print(f"  map complete: {total_records:,} records in {human(map_seconds)}")
    if args.stop_after == "map":
        return 0

    # --- plan-reduce ------------------------------------------------------
    started = time.time()
    print("\n=== plan-reduce ===", flush=True)
    plan = work / "plan.json"
    plan_argv = ["plan-reduce", "--contract", contract,
                 "--store-root", store_for(work, "plan"),
                 "--staging-root", staging, "--family", "places",
                 "--markers-dir", markers, "--scratch-dir", work / "plan-scratch",
                 "--output", plan, "--matrix-out", work / "reduce-matrix.json",
                 "--staging-report", work / "plan-staging.json"]
    if args.max_reduce_jobs is not None:
        plan_argv += ["--max-reduce-jobs", args.max_reduce_jobs]
    hosted(*plan_argv)
    plan_document = json.loads(plan.read_text())
    execution = plan_document["reduce_execution"]
    partitions = plan_document["partitions"]
    plan_staged = json.loads((work / "plan-staging.json").read_text())
    print(f"  {len(partitions):,} partitions in {execution['job_count']} jobs "
          f"{human(time.time() - started)}")
    print(f"  staging peak resident "
          f"{plan_staged['staged_peak_resident_bytes'] / 1e9:.2f} GB", flush=True)

    # --- reduce -----------------------------------------------------------
    started = time.time()
    batches = execution["batches"]
    print(f"\n=== run-reduce x{len(batches)} "
          f"({args.reduce_workers} workers) ===", flush=True)
    reductions = work / "reductions"
    reductions.mkdir(exist_ok=True)

    def reduce_one(batch):
        index = batch["batch_index"]
        hosted("run-reduce", "--contract", contract,
               "--store-root", store_for(work, "reduce"),
               "--staging-root", staging, "--family", "places", "--plan", plan,
               "--markers-dir", markers, "--batch-index", index,
               "--encoder-binary", ENCODER, "--verifier-binary", VERIFIER,
               "--scratch-dir", work / f"reduce-scratch-{index}",
               "--staging-report", work / f"reduce-staging-{index}.json",
               "--output-dir", reductions)
        return index

    completed = 0
    with concurrent.futures.ThreadPoolExecutor(args.reduce_workers) as pool:
        for index in pool.map(reduce_one, batches):
            completed += 1
            rate = (time.time() - started) / completed
            print(f"  [{completed}/{len(batches)}] batch {index} "
                  f"(eta {human(rate * (len(batches) - completed))})", flush=True)
    print(f"  reduce complete in {human(time.time() - started)}")
    if args.stop_after == "reduce":
        return 0

    # --- head -------------------------------------------------------------
    # The reason this harness exists. Peak resident and spill are what the
    # runner could not survive, so they are what gets reported.
    started = time.time()
    print(f"\n=== run-head (2^{args.shard_bits} shards) ===", flush=True)
    head = work / "head.json"
    hosted("run-head", "--contract", contract,
           "--store-root", store_for(work, "head"),
           "--staging-root", staging,
           "--staging-report", work / "head-staging.json",
           "--family", "places", "--markers-dir", markers,
           "--encoder-binary", ENCODER, "--verifier-binary", VERIFIER,
           "--scratch-dir", work / "head-scratch",
           "--shard-bits", str(args.shard_bits),
           "--output", head)
    head_seconds = time.time() - started
    result = json.loads(head.read_text())
    staged = json.loads((work / "head-staging.json").read_text())
    print(f"  head complete in {human(head_seconds)}")
    print(f"  populated shards {result['populated_shards']:,} "
          f"of {result['shard_count']:,}")
    print(f"  candidate rows   {result.get('input_candidate_rows', 0):,}")
    print(f"  staged published {staged.get('staged_bytes_published', 0) / 1e9:.2f} GB")
    print(f"  peak resident    "
          f"{staged.get('staged_peak_resident_bytes', 0) / 1e9:.2f} GB")

    summary = {
        "release": args.release,
        "inventory_sha256": inventory["inventory_sha256"],
        "source_mirror": mirror,
        "max_scratch_gib_override": args.max_scratch_gib,
        "tasks": len(tasks),
        "records": total_records,
        "map_seconds": round(map_seconds, 1),
        "partitions": len(partitions),
        "head_seconds": round(head_seconds, 1),
        "head": result,
        "head_staging": staged,
    }
    (work / "local-planet-summary.json").write_text(
        json.dumps(summary, indent=2) + "\n")
    print(f"\nSummary written to {work / 'local-planet-summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
