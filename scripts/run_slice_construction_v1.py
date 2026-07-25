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
hosted("plan-reduce", "--contract", contract, "--store-root", store,
       "--family", "places", "--markers-dir", markers,
       "--scratch-dir", WORK / "plan-scratch", "--output", plan,
       "--matrix-out", WORK / "reduce-matrix.json")
partitions = json.loads(plan.read_text())["partitions"]
print(f"  {len(partitions)} partitions  {time.time()-t:.1f}s")

# --- reduce ---------------------------------------------------------------
t = phase(f"run-reduce x{len(partitions)}")
reductions = WORK / "reductions"; reductions.mkdir(exist_ok=True)
for index in range(len(partitions)):
    hosted("run-reduce", "--contract", contract, "--store-root", store,
           "--family", "places", "--plan", plan, "--markers-dir", markers,
           "--partition-index", index,
           "--encoder-binary", BIN / "places-serving-encode-v1",
           "--verifier-binary", BIN / "places-serving-verify-v1",
           "--scratch-dir", WORK / f"reduce-scratch-{index}",
           "--output", reductions / f"{index:04d}.json")
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
print(f"  shards {json.loads(head.read_text())['shard_count']}  {time.time()-t:.1f}s")

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
print(json.dumps({"records": records, "partitions": len(partitions),
                  "reduce_seconds_per_partition": round(elapsed/max(1,len(partitions)), 3),
                  "store_bytes_by_class": classes,
                  "reconciles": result["reconciles"]}, sort_keys=True))
