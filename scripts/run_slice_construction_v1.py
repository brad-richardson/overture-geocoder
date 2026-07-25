#!/usr/bin/env python3
"""Drive construction-v1 end to end on a REAL Overture slice, no credentials.

Differs from tests/test_construction_v1_hosted.py in the one way that matters:
the map input is produced by the real S3 projection from a real inventory, not
by a hand-written fixture. Everything after that is the hosted CLI.

Phases: derive-contract -> run-map -> plan-reduce -> run-reduce (every reduce
job the plan dispatches) -> run-head -> finalize (filesystem remote).

Both families run the same five phases through the same hosted CLI, each with
its own projector, transform, proof, encoder and verifier. Two documented
asymmetries, both of them properties of the pipeline rather than of this script:

* `run-head` is a no-op for addresses (there is no global address head), so the
  address head.json is `{"family", "head": null, "note"}` and finalize takes no
  `--head`. Every head assertion below is therefore family-guarded.
* reduce ownership differs. A Places job owns a shuffle-bucket RANGE (#160); an
  address job owns a contiguous PARTITION range, because the address shuffle is
  deliberately deferred (docs/plans/2026-07-24-construction-v1-follow-ups.md).
  Both are dispatched here by `--batch-index`/`--output-dir`, which is exactly
  how the hosted reduce matrix dispatches them, so this loop exercises the real
  dispatch path for each family rather than the legacy per-partition one.

    # Places, Monaco: 38,182 places
    python scripts/build_slice_inventory_v1.py --release 2026-07-22.0 \\
      --bbox 7.36 43.71 7.47 43.78 --output slice/inventory.json
    python scripts/run_slice_construction_v1.py --inventory slice/inventory.json \\
      --task-index 33 --release 2026-07-22.0 --work slice/work

    # Addresses, Seattle: 104,928 addresses across two level-8 cells
    python scripts/build_slice_inventory_v1.py --family addresses \\
      --release 2026-07-22.0 --bbox -122.34 47.59 -122.30 47.63 \\
      --output slice/address-inventory.json
    python scripts/run_slice_construction_v1.py --family addresses \\
      --inventory slice/address-inventory.json --task-index 54 \\
      --release 2026-07-22.0 --work slice/address-work
"""
import argparse
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location(
    "slice_construction_staging", ROOT / "scripts/construction_staging_v1.py"
)
assert _SPEC and _SPEC.loader
STAGING = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(STAGING)
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
parser.add_argument("--family", choices=("addresses", "places"), default="places")
# The intermediate store's transport. Default ON with a filesystem staging root,
# because the artifact-carried store is the planet blocker and the point of this
# loop is that the transport is exercised on every change with no credentials.
# --no-staging runs the legacy single-store shape for an A/B byte comparison.
parser.add_argument("--no-staging", action="store_true",
                    help="Run the legacy artifact-shaped transport: one local "
                         "store shared by every phase, no staging mirror.")
args = parser.parse_args()

WORK = args.work
RELEASE = args.release
FAMILY = args.family
TASK_INDEX = args.task_index
ADDRESSES = FAMILY == "addresses"
# The hosted workflow derives the map task ID from the family key and the task
# index; `construction_v1_control.py` builds `addresses-map-NNN` for the address
# matrix. Anything matching `marker_key`'s charset works, and the harness only
# needs the ID to be family-distinct so the two families never collide in one
# store.
TASK_ID = f"{FAMILY}-map-{TASK_INDEX:03d}"
PREFIX = "address" if ADDRESSES else "places"
TRANSFORM = BIN / f"{PREFIX}-transform-v1"
PROOF = BIN / f"{PREFIX}-proof-directory"
ENCODER = BIN / f"{PREFIX}-serving-encode-v1"
VERIFIER = BIN / f"{PREFIX}-serving-verify-v1"
# The store class prefix each family writes under, asserted on by the CI smoke.
MAP_CLASS = "map/address" if ADDRESSES else "map/places-v1"


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
STAGED = not args.no_staging
# R2 staging, filesystem-backed. One run-scoped, family-scoped prefix inside it,
# derived by the hosted CLI from the contract's request_sha256 exactly as a hosted
# job derives it -- so this exercises the real key layout, not a stand-in.
staging = WORK / "staging"
markers = WORK / "markers"; markers.mkdir(exist_ok=True)


def store_for(phase: str) -> Path:
    """This phase's LOCAL store directory.

    With staging on, every phase gets its OWN EMPTY directory, which is the whole
    claim under test: a hosted phase runs on a fresh runner with no store artifact
    and must fetch only the keys its markers name. Sharing one directory would let
    a consumer read map output straight off local disk and prove nothing.
    """
    return WORK / (f"store-{phase}" if STAGED else "store")


def staging_argv() -> tuple:
    return ("--staging-root", staging) if STAGED else ()

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
    # Namespaced per family so the two slices never publish into one another's
    # create-only prefixes. Changing these changes `request_sha256`, so a work
    # directory from an older harness revision fails closed with a digest
    # mismatch rather than resuming into a different contract.
    "namespaces": {
        "immutable_root": f"construction-v1/slice-{FAMILY}",
        "slice": f"construction-v1/slice-{FAMILY}/slice/slice-1/",
        "markers": f"construction-v1/slice-{FAMILY}/markers/",
    },
}) + "\n")
contract = WORK / "contract.json"
hosted("derive-contract", "--request", request, "--output", contract,
       "--runtime", WORK / "runtime.json", "--allow-unpinned-duckdb")
REQUEST_SHA256 = json.loads(contract.read_text())["request_sha256"]
# Derived by the same function the hosted CLI uses, so the harness reports the
# prefix a hosted job would actually write rather than a look-alike.
STAGING_PREFIX = STAGING.staging_prefix(REQUEST_SHA256, FAMILY)
print(f"  ok {time.time()-t:.1f}s")
if STAGED:
    print(f"  intermediate store transport: R2 staging (filesystem backend) at "
          f"{STAGING_PREFIX}")
    print("  every phase gets its own EMPTY local store, so each one fetches only "
          "the keys its markers name")

# --- projection (the part no test covers) ---------------------------------
t = phase(f"project task {TASK_INDEX} from S3")
projected = WORK / "projected.parquet"
report = WORK / "projection.json"
if ADDRESSES:
    # The address family has its own S3 projector keyed on its own canonical
    # row-group inventory; there is no Places code path in it and vice versa.
    # Same flags and caps the hosted address map job uses.
    run(VENV, "scripts/experiment_hosted_rowgroups.py",
        "--release", RELEASE, "--family", "addresses",
        "--inventory-report", args.inventory, "--task-index", TASK_INDEX,
        "--output", projected, "--json-out", report,
        "--target-rowgroup-uncompressed-bytes", 400_000_000,
        "--max-rows", 4_000_000, "--max-groups", 72,
        "--max-output-bytes", 500_000_000)
    records = json.loads(report.read_text())["output"]["rows"]
else:
    run(VENV, "scripts/project_places_construction_v1.py",
        "--inventory", args.inventory,
        "--evidence-spec", ROOT / "benchmarks/places-construction-v1-evidence-spec-v2.json",
        "--task-index", TASK_INDEX, "--output", projected, "--report", report,
        "--max-rows", 4_000_000, "--max-groups", 72,
        "--max-selected-compressed-bytes", 536_870_912,
        "--max-selected-uncompressed-bytes", 1_000_000_000,
        "--max-output-bytes", 500_000_000)
    records = json.loads(report.read_text())["output"]["records"]
hosted("source-limits", "--report", report, "--family", FAMILY,
       "--output", WORK / "source-limits.json")
print(f"  projected {records:,} real Overture {FAMILY}  {time.time()-t:.1f}s")

# --- map ------------------------------------------------------------------
t = phase("run-map")
hosted("run-map", "--contract", contract, "--store-root", store_for("map"),
       *staging_argv(),
       "--family", FAMILY, "--task-id", TASK_ID, "--input", projected,
       "--source-limits", WORK / "source-limits.json",
       "--transform-binary", TRANSFORM,
       "--proof-binary", PROOF,
       "--scratch-dir", WORK / "map-scratch",
       "--marker-out", markers / f"{TASK_ID}.json",
       "--output", WORK / "map.json")
marker = json.loads((markers / f"{TASK_ID}.json").read_text())
map_staged = json.loads((WORK / "map.json").read_text())
map_summary: dict = {}
if ADDRESSES:
    transform_report = marker["transform"]
    map_summary = {
        "admitted_rows": transform_report["admitted_rows"],
        "rejected_rows": transform_report["rejected_rows"],
        "map_packs": len(marker["packs"]),
    }
    print(f"  admitted {map_summary['admitted_rows']:,} / rejected "
          f"{map_summary['rejected_rows']:,} -> {map_summary['map_packs']} packs"
          f"  {time.time()-t:.1f}s")
    # Guarded exactly as the hosted CLI guards it: a marker written before the
    # artifact existed has no such key, and the diagnosis for that is finalize's
    # fail-closed gate, not a KeyError here.
    art = marker.get("address_records")
    if isinstance(art, dict):
        cells = sorted({
            cell["partition_cell"]
            for pack in art["packs"] for cell in pack["directory"]["cells"]
        })
        map_summary["address_records_rows"] = art["records"]
        map_summary["address_records_packs"] = len(art["packs"])
        map_summary["address_records_bytes"] = art["output_bytes"]
        map_summary["address_records_cells"] = cells
        map_summary["address_records_null_island"] = art["null_island_records"]
        print(f"  address-records {art['records']:,} rows in "
              f"{len(art['packs'])} bucket packs ({art['output_bytes']/1e6:.2f} MB), "
              f"cells {cells}, buckets {[p['shuffle_bucket'] for p in art['packs']]}, "
              f"null-island {art['null_island_records']}, "
              f"unroutable {art['unroutable_records']}")
else:
    comb = marker["combiner"]
    map_summary = {
        "term_rows_in": comb["input_rows"],
        "term_rows_retained": comb["retained_rows"],
        "term_rows_combined_away": comb["discarded"]["records"],
    }
    print(f"  term rows {comb['input_rows']:,} -> {comb['retained_rows']:,} retained "
          f"({comb['discarded']['records']:,} combined away)  {time.time()-t:.1f}s")

# --- plan -----------------------------------------------------------------
t = phase("plan-reduce")
plan = WORK / "plan.json"
plan_argv = ["plan-reduce", "--contract", contract, "--store-root", store_for("plan"),
             *staging_argv(),
             "--family", FAMILY, "--markers-dir", markers,
             "--scratch-dir", WORK / "plan-scratch", "--output", plan,
             *(("--staging-report", WORK / "plan-staging.json") if STAGED else ()),
             "--matrix-out", WORK / "reduce-matrix.json"]
if args.max_reduce_jobs is not None:
    plan_argv += ["--max-reduce-jobs", args.max_reduce_jobs]
hosted(*plan_argv)
plan_document = json.loads(plan.read_text())
partitions = plan_document["partitions"]
execution = plan_document["reduce_execution"]
# Places records a bucket stride (ownership "shuffle-bucket-range"); addresses
# record a partition batch size and carry no bucket keys at all, so reading them
# unguarded would KeyError on the address path.
if execution.get("bucket_stride") is not None:
    print(f"  {len(partitions)} partitions in {execution['job_count']} bucket-range jobs "
          f"(stride {execution['bucket_stride']} of {execution['bucket_count']} buckets)"
          f"  {time.time()-t:.1f}s")
else:
    print(f"  {len(partitions)} partitions in {execution['job_count']} "
          f"{execution['ownership']} jobs (batch size {execution['batch_size']})"
          f"  {time.time()-t:.1f}s")
plan_staged = json.loads((WORK / "plan-staging.json").read_text()) if STAGED else {}
if STAGED:
    # The plan phase reads pack BODIES, so its PEAK resident bytes -- not its total
    # -- decides whether the job fits a runner. It used to hydrate every pack
    # eagerly, which made peak == total and put the whole planet term store on this
    # one runner: the very job run 30113308268 died on.
    print(f"  staging: hydrated {plan_staged['staged_bytes_hydrated']/1e6:.2f} MB, "
          f"peak resident {plan_staged['staged_peak_resident_bytes']/1e6:.2f} MB, "
          f"released {plan_staged['staged_objects_released']} objects")

# --- reduce ---------------------------------------------------------------
# One job per reduce BATCH, exactly as the hosted matrix dispatches it. For
# places a batch is a bucket RANGE and the job opens each map fragment in its
# range once, emitting every partition whose cell hashes into the range; for
# addresses a batch is a contiguous partition range. Either way this is
# --batch-index/--output-dir, never the legacy one-job-per-partition path.
t = phase(f"run-reduce x{execution['job_count']} {execution['ownership']} jobs")
reductions = WORK / "reductions"; reductions.mkdir(exist_ok=True)
for batch in execution["batches"]:
    hosted("run-reduce", "--contract", contract, "--store-root", store_for("reduce"),
           *staging_argv(),
           "--family", FAMILY, "--plan", plan, "--markers-dir", markers,
           "--batch-index", batch["batch_index"],
           # Addresses re-prove every fetched row group inside reduce, so the
           # reducer needs the proof binary as well as the encoder/verifier.
           *(("--proof-binary", PROOF) if ADDRESSES else ()),
           "--encoder-binary", ENCODER,
           "--verifier-binary", VERIFIER,
           "--scratch-dir", WORK / f"reduce-scratch-{batch['batch_index']}",
           *(("--staging-report",
              WORK / f"reduce-staging-{batch['batch_index']}.json") if STAGED else ()),
           "--output-dir", reductions)
elapsed = time.time() - t
print(f"  {len(partitions)} partitions in {elapsed:.1f}s "
      f"({elapsed/max(1,len(partitions)):.2f}s/partition)")
reduce_staged: dict = {}
if STAGED:
    reports = [json.loads((WORK / f"reduce-staging-{b['batch_index']}.json").read_text())
               for b in execution["batches"]]
    reduce_staged = {
        "staged_bytes_hydrated": sum(r["staged_bytes_hydrated"] for r in reports),
        "staged_objects_hydrated": sum(r["staged_objects_hydrated"] for r in reports),
        "staged_peak_resident_bytes": max(r["staged_peak_resident_bytes"] for r in reports),
        # Summed, not maxed: this is "did the reducer evict at all", and zero is the
        # signature of the defect it replaced (the address reducer held every pack it
        # opened until the process exited, so peak == total by construction).
        "staged_objects_released": sum(r["staged_objects_released"] for r in reports),
    }
    print(f"  staging: hydrated {reduce_staged['staged_bytes_hydrated']/1e6:.2f} MB "
          f"in {reduce_staged['staged_objects_hydrated']} objects across "
          f"{len(reports)} jobs, worst job peak resident "
          f"{reduce_staged['staged_peak_resident_bytes']/1e6:.2f} MB, released "
          f"{reduce_staged['staged_objects_released']} objects")

# --- head -----------------------------------------------------------------
t = phase("run-head")
head = WORK / "head.json"
head_args = () if ADDRESSES else (
    "--encoder-binary", ENCODER, "--verifier-binary", VERIFIER,
    "--scratch-dir", WORK / "head-scratch", "--shard-bits", "4",
)
hosted("run-head", "--contract", contract, "--store-root", store_for("head"),
       *staging_argv(),
       *(("--staging-report", WORK / "head-staging.json") if STAGED else ()),
       "--family", FAMILY, "--markers-dir", markers, *head_args,
       "--output", head)
head_result = json.loads(head.read_text())
head_staged = json.loads((WORK / "head-staging.json").read_text()) if STAGED else {}
if STAGED and head_staged["staged_bytes_hydrated"]:
    # Head hydrates EVERY task's head-candidate pack and hands them all to one
    # read_parquet, so its peak EQUALS its total: measured, not bounded. Recorded
    # so the planet figure is a number rather than an assumption.
    print(f"  staging: hydrated {head_staged['staged_bytes_hydrated']/1e6:.2f} MB, "
          f"peak resident {head_staged['staged_peak_resident_bytes']/1e6:.2f} MB "
          "(unbatched by design -- see the follow-up)")
# Addresses have no global head phase; run-head writes {"head": null} for them,
# so every head key below is guarded rather than assumed.
if head_result.get("head", "absent") is None:
    print(f"  no global head phase for {FAMILY}  {time.time()-t:.1f}s")
else:
    print(f"  shards {head_result['shard_count']}  {time.time()-t:.1f}s")

# --- finalize -------------------------------------------------------------
t = phase("finalize (filesystem remote)")
final = WORK / "final.json"
hosted("finalize", "--contract", contract, "--store-root", store_for("finalize"),
       *staging_argv(),
       "--family", FAMILY, "--plan", plan, "--reductions-dir", reductions,
       # Threaded for BOTH families. Finalize publishes the map phase's
       # per-record artifact from the markers, and for a family that carries one
       # it fails closed without this flag rather than silently publishing a
       # slice whose per-record packs expire with the map artifact retention.
       "--markers-dir", markers,
       # The address head result carries `head: null`; passing it would make
       # finalize read shard fields that do not exist. Matches the hosted
       # workflow, which only threads --head for places.
       *(() if ADDRESSES else ("--head", head)),
       "--remote-root", WORK / "remote",
       "--work-root", WORK / "final-work", "--output", final)
result = json.loads(final.read_text())
print(f"  reconciles={result['reconciles']} marker_written_last={result['marker_written_last']}"
      f"  {time.time()-t:.1f}s")
print(f"  per-record artifact published: {result['positions_objects']} objects, "
      f"{result['positions_records']:,} records, {result['positions_bytes']/1e6:.2f} MB "
      f"(verified as part of the whole-slice check)")
# The serving payload count, reported separately from `objects`: a places finalize
# used to publish head shards, positions packs and two manifests while silently
# dropping EVERY routed `.plrv`, and every other number here stayed non-zero.
print(f"  serving objects published: {result['serving_objects']} "
      f"(>= {result['reduction_serving_objects']} reductions x "
      f"{result['serving_object_key']}, plus any head shards)")

# Break the store down by artifact class. A single total invites a linear
# extrapolation to planet scale, which is wrong twice over: fixed per-artifact
# overhead dominates at this size, and a small slice combines far less than the
# planet does (fewer rows share a (cell, token) group). Report the parts.
def tree_bytes(path):
    return sum(p.stat().st_size for p in path.rglob("*")
               if p.is_file() and not p.name.endswith(".metadata.json"))


# With staging on, the COMPLETE store lives in the staging tree -- each phase's
# local directory holds only the subset that phase wrote or hydrated -- so the
# class report is read from there. Keys and byte totals are unchanged either way:
# a staging key is `<prefix>/<class>/sha256/...` and the objects are byte-identical
# to a --no-staging run, so these numbers stay comparable with history. The
# FilesystemStore's `.metadata.json` sidecars are excluded for the same reason.
if STAGED:
    class_root = staging / STAGING_PREFIX
else:
    class_root = store_for("map")
classes = {
    d.name + "/" + s.name: tree_bytes(s)
    for d in sorted(class_root.iterdir()) if d.is_dir()
    for s in sorted(d.iterdir()) if s.is_dir()
}
print("\nstore by artifact class:")
for name, size in sorted(classes.items(), key=lambda kv: -kv[1]):
    if size:
        print(f"  {size/1e6:8.2f} MB  {name}")
print(f"  {tree_bytes(class_root)/1e6:8.2f} MB  TOTAL for {records:,} {FAMILY}")
print(f"  {tree_bytes(WORK / 'remote')/1e6:8.2f} MB  published slice")
if STAGED:
    print(f"\nR2 staging transport (filesystem backend, prefix {STAGING_PREFIX}):")
    print(f"  map      published {map_staged['staged_objects_published']:3d} objects "
          f"({map_staged['staged_bytes_published']/1e6:8.2f} MB), hydrated "
          f"{map_staged['staged_objects_hydrated']:3d}")
    print(f"  finalize published {result['staged_objects_published']:3d} objects "
          f"({result['staged_bytes_published']/1e6:8.2f} MB), hydrated "
          f"{result['staged_objects_hydrated']:3d} "
          f"({result['staged_bytes_hydrated']/1e6:.2f} MB) into an EMPTY local store")
    print(f"           peak resident {result['staged_peak_resident_bytes']/1e6:.2f} MB, "
          f"released {result['staged_objects_released']} objects "
          f"({result['staged_bytes_released']/1e6:.2f} MB) -- one object at a time")
print(
    "\nNOTE: do not extrapolate these linearly to planet scale. Only the map/\n"
    "class is what the inter-phase transport carries out of map, and a slice this\n"
    "small under-combines relative to the planet."
)
# The head keys are here because `reconciles` alone is not evidence: it is a real
# validator result for both families now (#166), but an EMPTY head (zero populated
# shards, zero records) reconciles perfectly and satisfies every other check. A
# caller that wants to know the run produced something must assert on counts.
summary = {"family": FAMILY,
           "records": records, "partitions": len(partitions),
           "reduce_seconds_per_partition": round(elapsed/max(1,len(partitions)), 3),
           "store_bytes_by_class": classes,
           "map_store_class": MAP_CLASS,
           "reconciles": result["reconciles"],
           # How the reduce space was cut. batch_size 1 is the degenerate shape;
           # a run asserting anything about multi-partition ranges must be able
           # to see that it got them.
           "reduce_ownership": execution["ownership"],
           "reduce_job_count": execution["job_count"],
           "reduce_partitions_per_job": execution["batch_size"],
           # Same reasoning for the per-record artifact: it is the insurance
           # against a planet map re-run, and a run that emitted it but failed to
           # PUBLISH it looks identical from every other key here. The key names
           # stay `positions_*` for both families because they are already the
           # published shape of the finalize result; the mechanism is generic.
           # The serving payload set. `positions_objects` and `objects` were both
           # non-zero while places published no `.plrv` at all, so the count of
           # SERVING objects and the reduction count it must cover are separate,
           # assertable keys.
           "serving_objects": result["serving_objects"],
           "reduction_serving_objects": result["reduction_serving_objects"],
           "serving_object_key": result["serving_object_key"],
           "positions_objects": result["positions_objects"],
           "positions_records": result["positions_records"],
           "positions_bytes": result["positions_bytes"],
           **map_summary}
# Places-only keys stay Places-only rather than being emitted as nulls, so a
# consumer's `jq -e` on them is a real assertion and not vacuously true. The
# address family has no global head and no shuffle-bucket reduce ownership.
if execution.get("bucket_stride") is not None:
    summary["reduce_bucket_stride"] = execution["bucket_stride"]
    summary["reduce_bucket_count"] = execution["bucket_count"]
# Staging keys are emitted only when staging is on, for the same reason: a `jq -e`
# on them must be a real assertion. `map_staged_objects_published > 0` proves the
# map phase's intermediate output actually left for staging, and
# `finalize_staged_objects_hydrated > 0` proves a downstream phase read it back
# from there rather than off a store it inherited on local disk -- which together
# are the whole claim of this transport.
if STAGED:
    summary["staging_prefix"] = STAGING_PREFIX
    summary["map_staged_objects_published"] = map_staged["staged_objects_published"]
    summary["map_staged_bytes_published"] = map_staged["staged_bytes_published"]
    summary["finalize_staged_objects_hydrated"] = result["staged_objects_hydrated"]
    summary["finalize_staged_bytes_hydrated"] = result["staged_bytes_hydrated"]
    # Finalize used to hydrate the WHOLE published set before its first upload --
    # 13-18 GB at planet scale on the last job of a multi-hour run. It now hydrates
    # one object, verifies it, uploads it and evicts it, so peak-below-total is the
    # assertable form of that, exactly as it is for the plan phase, and the smoke
    # job checks both keys.
    summary["finalize_staged_peak_resident_bytes"] = result[
        "staged_peak_resident_bytes"
    ]
    summary["finalize_staged_objects_released"] = result["staged_objects_released"]
    # The plan phase is the one that used to hydrate its whole fan-in eagerly.
    # Peak-below-total is the assertable form of "batched and evicted", and the
    # smoke job checks it, so a regression to eager hydration is a red build.
    summary["plan_staged_bytes_hydrated"] = plan_staged["staged_bytes_hydrated"]
    summary["plan_staged_peak_resident_bytes"] = plan_staged["staged_peak_resident_bytes"]
    summary["plan_staged_objects_released"] = plan_staged["staged_objects_released"]
    summary["reduce_staged_bytes_hydrated"] = reduce_staged["staged_bytes_hydrated"]
    summary["reduce_staged_objects_hydrated"] = reduce_staged["staged_objects_hydrated"]
    summary["reduce_staged_peak_resident_bytes"] = reduce_staged[
        "staged_peak_resident_bytes"
    ]
    # The tripwire for the reduce phase, and the reason it is a separate key from
    # peak/total: on a slice whose whole map output is ONE pack, peak necessarily
    # EQUALS total no matter how promptly the reducer evicts, so peak-below-total is
    # not assertable there. A zero release count is, and zero is exactly what the
    # address reducer reported before it called `release()` at all.
    summary["reduce_staged_objects_released"] = reduce_staged["staged_objects_released"]
    # Head is MEASURED, not bounded: it hands every head-candidate pack to one
    # read_parquet, so peak == total by construction. Recorded so the planet figure
    # comes from a run rather than an estimate.
    summary["head_staged_bytes_hydrated"] = head_staged["staged_bytes_hydrated"]
    summary["head_staged_peak_resident_bytes"] = head_staged[
        "staged_peak_resident_bytes"
    ]
if head_result.get("head", "absent") is not None:
    summary["head_shard_count"] = head_result["shard_count"]
    summary["head_populated_shards"] = head_result["populated_shards"]
    summary["head_total_records"] = head_result["total_records"]
# Written as a file as well as printed: stdout can interleave with stderr, so
# `tail -1` of a merged stream is not a reliable machine-readable surface.
(WORK / "summary.json").write_text(json.dumps(summary, sort_keys=True) + "\n")
print(json.dumps(summary, sort_keys=True))
