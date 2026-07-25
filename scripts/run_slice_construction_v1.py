#!/usr/bin/env python3
"""Drive construction-v1 end to end on a REAL Overture slice covering Monaco.

Differs from tests/test_construction_v1_hosted.py in the one way that matters:
the map input is produced by the real S3 projection from a real inventory, not
by a hand-written fixture. Everything after that is the hosted CLI.

Phases: derive-contract -> run-map -> plan-reduce -> run-reduce (all partitions)
-> run-head -> finalize (filesystem remote, no credentials).
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = sys.executable
BIN = ROOT / "crates/target/release"

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--inventory", type=Path, required=True,
                    help="Inventory built by build_slice_inventory_v1.py")
parser.add_argument("--task-index", type=int, required=True)
parser.add_argument("--release", required=True)
parser.add_argument("--work", type=Path, required=True)
# The default plan gives every populated bucket its own job, which on a slice
# this small means one partition per job -- the DEGENERATE shape. Lowering the
# job cap widens the bucket stride, so a job owns several partitions and the fast
# loop actually exercises multi-partition ranges.
parser.add_argument("--max-reduce-jobs", type=int, default=None,
                    help="Cap reduce jobs, widening the bucket stride "
                         "(use 1 to force one job over the whole bucket space).")
args = parser.parse_args()

WORK = args.work
RELEASE = args.release
TASK_INDEX = args.task_index
TASK_ID = f"places-map-{TASK_INDEX:03d}"


def run(*argv, capture=False):
    argv = [str(a) for a in argv]
    result = subprocess.run(argv, cwd=ROOT, text=True,
                            capture_output=True)
    if result.returncode:
        print(f"\nFAILED: {' '.join(argv[:4])}\n{result.stdout[-3000:]}\n{result.stderr[-4000:]}")
        raise SystemExit(1)
    return result.stdout


def hosted(*argv):
    return run(VENV, "scripts/construction_v1_hosted.py", *argv)


def phase(name):
    print(f"\n=== {name} ===", flush=True)
    return time.time()


WORK.mkdir(parents=True, exist_ok=True)
store = WORK / "store"
markers = WORK / "markers"; markers.mkdir(exist_ok=True)

# --- contract -------------------------------------------------------------
t = phase("derive-contract")
request = WORK / "request.json"
request.write_text(json.dumps({
    "schema": "overture-construction-v1-request-v1",
    "release": RELEASE,
    "families": {"addresses": {}, "places": {}},
    "versions": {"duckdb": "1.5.1", "pyarrow": "25.0.0", "numpy": "2.3.5",
                 "python": "3.12.3", "rustc": "local"},
    "caps": {"max_remote_operations": 100000,
             "max_remote_write_bytes": 1_000_000_000_000},
    "namespaces": {
        "immutable_root": "construction-v1/monaco",
        "slice": "construction-v1/monaco/slice/monaco-1/",
        "markers": "construction-v1/monaco/markers/",
    },
}) + "\n")
contract = WORK / "contract.json"
hosted("derive-contract", "--request", request, "--output", contract,
       "--runtime", WORK / "runtime.json", "--allow-unpinned-duckdb")
print(f"  ok {time.time()-t:.1f}s")

# --- projection (the part no test covers) ---------------------------------
t = phase(f"project task {TASK_INDEX} from S3")
projected = WORK / "projected.parquet"
report = WORK / "projection.json"
run(VENV, "scripts/project_places_construction_v1.py",
    "--inventory", args.inventory,
    "--evidence-spec", ROOT / "benchmarks/places-construction-v1-evidence-spec-v2.json",
    "--task-index", TASK_INDEX, "--output", projected, "--report", report,
    "--max-rows", 4_000_000, "--max-groups", 72,
    "--max-selected-compressed-bytes", 536_870_912,
    "--max-selected-uncompressed-bytes", 1_000_000_000,
    "--max-output-bytes", 500_000_000)
records = json.loads(report.read_text())["output"]["records"]
hosted("source-limits", "--report", report, "--family", "places",
       "--output", WORK / "source-limits.json")
print(f"  projected {records:,} real Overture places  {time.time()-t:.1f}s")

# --- map ------------------------------------------------------------------
t = phase("run-map")
hosted("run-map", "--contract", contract, "--store-root", store,
       "--family", "places", "--task-id", TASK_ID, "--input", projected,
       "--source-limits", WORK / "source-limits.json",
       "--transform-binary", BIN / "places-transform-v1",
       "--proof-binary", BIN / "places-proof-directory",
       "--scratch-dir", WORK / "map-scratch",
       "--marker-out", markers / f"{TASK_ID}.json")
marker = json.loads((markers / f"{TASK_ID}.json").read_text())
comb = marker["combiner"]
print(f"  term rows {comb['input_rows']:,} -> {comb['retained_rows']:,} retained "
      f"({comb['discarded']['records']:,} combined away)  {time.time()-t:.1f}s")

# --- plan -----------------------------------------------------------------
t = phase("plan-reduce")
plan = WORK / "plan.json"
plan_argv = ["plan-reduce", "--contract", contract, "--store-root", store,
             "--family", "places", "--markers-dir", markers,
             "--scratch-dir", WORK / "plan-scratch", "--output", plan,
             "--matrix-out", WORK / "reduce-matrix.json"]
if args.max_reduce_jobs is not None:
    plan_argv += ["--max-reduce-jobs", args.max_reduce_jobs]
hosted(*plan_argv)
plan_document = json.loads(plan.read_text())
partitions = plan_document["partitions"]
execution = plan_document["reduce_execution"]
print(f"  {len(partitions)} partitions in {execution['job_count']} bucket-range jobs "
      f"(stride {execution['bucket_stride']} of {execution['bucket_count']} buckets)"
      f"  {time.time()-t:.1f}s")

# --- reduce ---------------------------------------------------------------
# One job per BUCKET RANGE, exactly as the hosted matrix dispatches it: the job
# opens each map fragment in its range once and emits every partition whose cell
# hashes into the range.
t = phase(f"run-reduce x{execution['job_count']} bucket ranges")
reductions = WORK / "reductions"; reductions.mkdir(exist_ok=True)
for batch in execution["batches"]:
    hosted("run-reduce", "--contract", contract, "--store-root", store,
           "--family", "places", "--plan", plan, "--markers-dir", markers,
           "--batch-index", batch["batch_index"],
           "--encoder-binary", BIN / "places-serving-encode-v1",
           "--verifier-binary", BIN / "places-serving-verify-v1",
           "--scratch-dir", WORK / f"reduce-scratch-{batch['batch_index']}",
           "--output-dir", reductions)
elapsed = time.time() - t
print(f"  {len(partitions)} partitions in {elapsed:.1f}s "
      f"({elapsed/max(1,len(partitions)):.2f}s/partition)")

# --- head -----------------------------------------------------------------
t = phase("run-head")
head = WORK / "head.json"
hosted("run-head", "--contract", contract, "--store-root", store,
       "--family", "places", "--markers-dir", markers,
       "--encoder-binary", BIN / "places-serving-encode-v1",
       "--verifier-binary", BIN / "places-serving-verify-v1",
       "--scratch-dir", WORK / "head-scratch", "--shard-bits", "4",
       "--output", head)
head_result = json.loads(head.read_text())
print(f"  shards {head_result['shard_count']}  {time.time()-t:.1f}s")

# --- finalize -------------------------------------------------------------
t = phase("finalize (filesystem remote)")
final = WORK / "final.json"
hosted("finalize", "--contract", contract, "--store-root", store,
       "--family", "places", "--plan", plan, "--reductions-dir", reductions,
       "--head", head, "--remote-root", WORK / "remote",
       "--work-root", WORK / "final-work", "--output", final)
result = json.loads(final.read_text())
print(f"  reconciles={result['reconciles']} marker_written_last={result['marker_written_last']}"
      f"  {time.time()-t:.1f}s")

# Break the store down by artifact class. A single total invites a linear
# extrapolation to planet scale, which is wrong twice over: fixed per-artifact
# overhead dominates at this size, and a small slice combines far less than the
# planet does (fewer rows share a (cell, token) group). Report the parts.
def tree_bytes(path):
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())

classes = {
    d.name + "/" + s.name: tree_bytes(s)
    for d in sorted(store.iterdir()) if d.is_dir()
    for s in sorted(d.iterdir()) if s.is_dir()
}
print("\nstore by artifact class:")
for name, size in sorted(classes.items(), key=lambda kv: -kv[1]):
    if size:
        print(f"  {size/1e6:8.2f} MB  {name}")
print(f"  {tree_bytes(store)/1e6:8.2f} MB  TOTAL for {records:,} places")
print(f"  {tree_bytes(WORK / 'remote')/1e6:8.2f} MB  published slice")
print(
    "\nNOTE: do not extrapolate these linearly to planet scale. Only the map/\n"
    "class is what the inter-phase transport carries out of map, and a slice this\n"
    "small under-combines relative to the planet."
)
# The head keys are here because `reconciles` alone is not evidence: for places
# it is a hardcoded literal in the finalize adapter, and an EMPTY head (zero
# populated shards, zero records) satisfies every other check. A caller that
# wants to know the run produced something must be able to assert on counts.
summary = {"records": records, "partitions": len(partitions),
           "reduce_seconds_per_partition": round(elapsed/max(1,len(partitions)), 3),
           "store_bytes_by_class": classes,
           "reconciles": result["reconciles"],
           "head_shard_count": head_result["shard_count"],
           "head_populated_shards": head_result["populated_shards"],
           "head_total_records": head_result["total_records"],
           # How the bucket space was cut. batch_size 1 is the degenerate shape;
           # a run asserting anything about multi-partition ranges must be able
           # to see that it got them.
           "reduce_job_count": execution["job_count"],
           "reduce_partitions_per_job": execution["batch_size"],
           "reduce_bucket_stride": execution["bucket_stride"]}
# Written as a file as well as printed: stdout can interleave with stderr, so
# `tail -1` of a merged stream is not a reliable machine-readable surface.
(WORK / "summary.json").write_text(json.dumps(summary, sort_keys=True) + "\n")
print(json.dumps(summary, sort_keys=True))
