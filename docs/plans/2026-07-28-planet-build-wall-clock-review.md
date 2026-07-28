# Planet build wall-clock review

Date: 2026-07-28

Scope: construction-v1 Address and Places planet attempts through GitHub Actions
run `30322532358`

Change scope: analysis only; this document is the only file created by this
review.

## Addendum, 2026-07-28, after finalize-only run `30323929757`

Written the same day, after the sections below were drafted. Where they
disagree, this addendum wins.

1. **The critical finding below is resolved.** PR
   [#185](https://github.com/brad-richardson/overture-geocoder/pull/185)
   overlaid the four reviewed finalizer-transport files onto the request-pinned
   producer checkout with byte comparison and import-time API assertions, and
   run
   [`30323929757`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30323929757)
   executed it: planet Places finalize completed in 56 minutes 50 seconds
   (admission 24m56s at 5 workers, upload 27m02s at 16, verification 4m24s),
   all 40,931 members verified, marker written last. Wave 0 items 1-4 are done;
   items 5 (head substage timing) and 6 (post-#184 cold control) remain.
2. **Re-anchor all baselines and percentage gates on that run before executing
   this plan.** Finalize is now a measured 57 minutes, not a failed
   257-minute lower bound. The stitched Places baseline and every percentage
   target derived from it are stale as written.
3. **Item 7 (single-write final objects) is promoted onto the Places critical
   path.** Measured finalize is about 3x the 15-20 minute Track A budget, and
   the dominant cost is structural: two hydration passes over all 40,929
   staged members (81,858 logical GETs) plus a full 51.8 GB re-upload. At the
   Track B target, 57 minutes of finalize is roughly a third of the whole
   budget. It is not an "if #184 is insufficient" fallback; #184 was measured
   and is insufficient for the gate.
4. **Adopted direction: staged A-to-B.** Track C is not planned. Execute Waves
   1-3 as written (they are shared by both tracks), then add the three
   B-specific pieces: the reviewed 16-way request-schema extension (8-way
   probe first, 16 accepted only on a further >=25% improvement), the fast
   marker fan-in with head running concurrently with plan/reduce, and the
   16-range head matrix only if single-runner radix plus four workers cannot
   reach 80-100 minutes. Wave 5 is not planned unless Wave 4 measurably
   misses its gates.
5. **Head-before-reduce changes the recovery model.** The current head-only
   resume authenticates reducer completion before running head. Starting head
   from a map-marker fan-in invalidates that assumption; the resume and
   authentication semantics must be respecified as part of that change, not
   discovered during a failed recovery.
6. **Pair the post-change cold control with the next real Overture release
   ingest** rather than paying a dedicated planet run purely for measurement.
   Note that concurrency changes do not reduce runner-minutes, only wall
   clock; the levers that cut paid minutes are shared binaries, early marker
   discovery, the Address reducer cap, selective planning, and head radix.
7. **Reverse R1 remains the primary workstream** per the operating policy;
   this plan proceeds as the secondary track within the two-active-PR budget.
   Also preserve `final-work/result.json` as a run artifact (small, from the
   publication result doc) in the first wave.

## Outcome

A reduction of at least 50% is realistic without changing the frozen Address
key or the 4,096-shard Places serving layout. A 75% reduction is also a
defensible engineering target, but it requires changing the execution topology,
not merely tuning the current serial phase DAG.

This review therefore keeps two distinct paths:

- **Track A, lower risk:** retain the current durable phase barriers and target
  three hours for Address / six hours for Places.
- **Track C, approximately two hours:** use 16-way ownership, incremental
  ingestion/planning, concurrent reduce and head sealing, and direct final
  writes. This targets 90-97 minutes for Address and 105-120 minutes for Places.

An Actions-only Track B between them can establish most of the topology before
introducing long-lived ownership workers.

| family | stitched measured cold baseline | Track A budget | strict 75% ceiling | Track C stretch |
|---|---:|---:|---:|---:|
| Address | about **387 min / 6 h 27 min** | **169-180 min / 2 h 49-3 h** | **97 min** | **90-97 min / 1 h 30-1 h 37** |
| Places | at least **729 min / 12 h 9 min before finalization**; at least 986 min / 16 h 26 min including the failed legacy finalizer | **340-360 min / 5 h 40-6 h** | **182 min** against upstream alone | **105-120 min / 1 h 45-2 h** |

The Address baseline stitches the fresh map from run
[`30207544725`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30207544725)
to the successful plan/reduce/finalize phases from run
[`30215529919`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30215529919).
The Places baseline stitches the successful fresh map/plan/reduce from run
[`30226086949`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30226086949),
the successful head from run
[`30288619536`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30288619536),
and only the 257 minutes paid before the old finalizer failed in run
[`30305749838`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30305749838).
It is therefore a lower bound on the old full build, not a claim that a successful
legacy finalizer would have ended there.

Track A is not delivered by one speculative knob. It is an itemized, measurable
combination:

1. fix the control-plane/producer checkout split so merged PR
   [#184](https://github.com/brad-richardson/overture-geocoder/pull/184)
   actually runs, then reset the R2 baseline;
2. raise map/reduce matrix concurrency from 4 to 8 after an R2 saturation probe;
3. build request-pinned Rust binaries once, not in every map and reducer job;
4. run about 60 larger Address reducer jobs instead of 117;
5. remove the second full Places planning scan for unsplit cells;
6. replace the Places head's 16 full-file shard scans with a two-level radix
   partition and parallelize the 4,096 independent shard tails;
7. make final serving objects single-write, so finalize validates metadata and
   commits manifests/marker rather than moving the same bytes again.

The Places head is the largest uncertainty. A six-hour target is not credible if
the 207-minute head remains serial and otherwise unchanged. It is credible if
the head reaches an 80-100 minute budget.

Track C goes further: it removes the workflow dependency that makes head wait
for reduce, divides head ownership into 16 independent coarse hash ranges, and
lets reducer/head owners ingest immutable map fragments while the remaining maps
are still running. Its budget is a target to prove, not a claim that the current
code would finish in two hours after changing one runner label.

There was also an immediate run blocker: run `30322532358` was dispatched from
merged #184, but its finalizer checked out the request-pinned producer commit
`63b7f71` before executing. That tree does not contain #184's R2/finalizer
changes, and the workflow has no runtime-file overlay. The run was cancelled
after 20 minutes 37 seconds in the old finalizer step. It avoided another
four-hour wait but produced no #184 measurement.

## What was reviewed

I reviewed:

- all construction-v1 workflow-dispatch attempts since 2026-07-24 through the
  public GitHub Actions API, including every job and step timestamp;
- the current workflow, hosted control plane, both family implementations, R2
  staging/publication code, and their tests;
- commits from `#140` through `#184`;
- `CLAUDE.md`, the current construction-v1 state, the dated scale/probe/design
  reports, local Claude memories/transcripts, and the active Codex history that
  drove the Address and Places recovery attempts;
- the current ARM ID/division workflow and representative successful
  1-hour-52-minute run `29624600543` as a topology comparison;
- the exact successful planet artifacts and counters preserved in the state
  document and agent history.

Raw Actions log archives require authenticated Actions access and the injected
`gh` token in this environment was invalid. Public job/step timestamps were
available. Exact terminal exceptions not visible in those timestamps were
cross-checked against the preserved Codex transcript and the current state
document. No failure cause below is inferred merely from a red job.

## Attempt history

| run | result | wall-clock evidence | what it established |
|---|---|---:|---|
| [`30113307034`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30113307034), [`30113308268`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30113308268) | failed/cancelled | about 2.5-3.2 h | The 63.5 GB Places store was copied through Actions artifacts and then downloaded whole per consumer. The implied fan-out was tens of TB; reducers could not start safely. |
| [`30207544725`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30207544725) | cancelled after Address map | Address map 173 min; plan 8 min | All 127 fresh maps passed. Reduce then lacked `control/contract.json`; this was workflow transport, not reducer work. |
| [`30215529919`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30215529919) | **Address success** | total 290.7 min; reused map 75.6, plan 9.0, reduce 137.9, finalize 67.2 | First marker-last planet Address slice. Reducers hydrated 292.66 GiB across 117 jobs. |
| [`30226086949`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30226086949) | Places head failed | map 248.2, plan 60.8, reduce 213.3, head 4.9 | All 89 fresh maps and 128 reducers passed. Head exhausted a 4.2 GiB DuckDB temp share. |
| [`30263207263`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30263207263) | replayed upstream; head failed | reused map 46.1, plan 104.8, reduce 143.8, head 6.0 | The compatibility shim changed the wrong dynamically loaded module. It also demonstrated the cost of replaying already-good phases. |
| [`30288619536`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30288619536) | head passed; finalize path failed | head 206.4 min; finalize failed in 43 s | Head-only recovery worked. The planet head produced all 4,096 shards. Finalize looked for `headdl/head/head.json` instead of `headdl/head.json`. |
| [`30305749838`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30305749838) | finalize-only failed | finalize 257.8 min | Serial pre-publication admission was still running when one streamed R2 GET body timed out. No final object or marker was written. |
| [`30322532358`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30322532358) | cancelled | finalizer step ran 20.6 min | Intended as the first #184 recovery, but the finalizer checked out `63b7f71`; #184's helper changes were absent from the executing tree. It was cancelled before producing a measurement. |

The recovery paths were valuable: they avoided another 5-12 hours of replay.
They are currently bespoke head-only and finalize-only branches, though, rather
than a general phase checkpoint model.

## Critical finding: the cancelled recovery did not execute PR #184

The canonical request is SHA
`88b7f17149fd5d75bf64720f0640d2cbe8aeb5ead750d279c1881f9bd5332614`
and pins:

```json
{"producer_commit":"63b7f71398eb4f50cfbe937314eb2abc6cb342bd"}
```

The finalize job's first source step is:

```yaml
- name: Check out the request-pinned producer
  uses: actions/checkout@...
  with:
    ref: ${{ needs.admit.outputs.producer_commit }}
```

It then directly runs that checkout's
`scripts/construction_v1_hosted.py finalize`. Between checkout and execution
there is no copy, `git show`, artifact download, or compatibility shim for:

- `scripts/construction_v1_hosted.py`;
- `scripts/construction_v1_remote.py`;
- `scripts/construction_staging_v1.py`;
- `scripts/r2_verified_store.py`.

Those are exactly the four runtime files #184 changed. Comparing `63b7f71` with
`a4fb387` shows 309 additions and 94 deletions in those files. The pinned
`r2_verified_store.py` still performs:

```text
HEAD -> create-only PUT -> full verified GET read-back -> trailing HEAD
```

and has no `download_with_info`, content-addressed one-GET hydration, or
stream-body retry. The pinned finalizer has no `admission_concurrency`,
measured-object upload concurrency, or phase progress callback.

This explains why a workflow-only recovery patch can merge cleanly yet fail to
change the expensive job. Before cancellation, run `30322532358` was on the
same path toward four-hour serial admission and the same un-retried
`ReadTimeoutError`.

Immediate choices, both requiring an explicit reviewed compatibility decision:

1. prepare a new request pinned to the new producer tree, accepting that the old
   request's staging namespace is not reusable; or
2. split the semantic producer pin from a separately authenticated runtime
   adapter pin, and carry an exact #184 overlay into the old request's job.

For option 2, do not loosely "use latest main." Admission should publish a
runtime manifest binding the workflow SHA, base producer SHA, the four overlay
file SHA-256 values, dependency lock SHA, and an explicit compatibility reason.
The job should verify that manifest before copying/executing anything. Slice
tests must prove that the overlay changes transport behavior without changing
the admitted plan/reduction/head bytes.

Until that is wired, #184 is merged source but not executed data-plane behavior.

## Measured phase anatomy

### Address

The cold baseline is:

| phase | measured wall | supporting measurement |
|---|---:|---|
| fresh map | 172.8 min | 127 jobs, 681.4 runner-min, median 315 s, p90 409 s |
| plan | 9.0 min | successful run |
| reduce | 137.9 min | 117 jobs, 543.6 runner-min, median 255 s, p90 385 s |
| head | 0.5 min | expected no-op |
| finalize | 67.2 min | 10,931 exact-set objects, marker last |
| **stitched cold total** | **about 387 min** | phase sum |

The resume map is especially revealing. Its useful `admit-task`/map command work
summed to only 827 seconds across 127 tasks, while the jobs consumed 17,751
seconds. Every job checked out, installed Python, reclaimed disk, and rebuilt
Rust before discovering its durable marker. Task 0's command took five seconds;
the complete map matrix took 75.6 minutes of wall time.

Address reduction is safe but repetitive. The 117 jobs hydrated
314,240,107,255 bytes (292.66 GiB); the largest job hydrated 6.52 GB and peak
resident cache was 4.46 GB. Median total job duration was only 255 seconds, so
there is ample timeout headroom for larger batches.

### Places

The successfully measured upstream path is:

| phase | measured wall | supporting measurement |
|---|---:|---|
| fresh map | 248.2 min | 89 jobs, 952.5 runner-min, median 566 s, p90 1,068 s, max 1,516 s |
| plan | 60.8 min | first cold plan; replay was 104.8 min |
| reduce | 213.3 min | 128 jobs, 841.5 runner-min, median 418 s, p90 517 s |
| head | 206.4 min | 62,573,648 candidates to 30,841,082 rows / 13,971,501 tokens / 4,096 shards |
| **upstream subtotal** | **728.7 min / 12 h 9 min** | excludes finalization entirely |
| old finalize paid before failure | **257.8 min** | no final objects uploaded |

Fresh Places map job time included:

- 45,246 seconds in the actual project/map/publish step;
- 6,495 seconds reclaiming disk;
- 3,093 seconds rebuilding the same Rust binaries;
- about 1,583 seconds in Python setup and package installation.

Places reducer time included:

- 42,563 seconds in the actual reducer;
- 4,554 seconds rebuilding the same Rust binaries;
- about 2,247 seconds in Python setup and package installation.

Those repeated fixed costs are worth removing, but compute and R2 work still
dominate. Setup cleanup alone cannot halve Places.

## Track A phase budgets

These are acceptance budgets, not forecasts to silently turn into contract
constants.

| phase | Address target | Places target | mechanism |
|---|---:|---:|---|
| map | 85 min | 115 min | concurrency 8, one binary build, PR #184 staging write proof |
| plan | 8 min | 30 min | Address unchanged; selective Places binding pass |
| reduce | 60 min | 95 min | concurrency 8; Address cap near 60 jobs; one binary build |
| head | 1 min | 80 min | Address no-op; radix sharding plus bounded shard workers |
| finalize | 15 min | 20 min | direct final-key producer writes, or PR #184 if its measured result is already this low |
| **total** | **169 min / 2 h 49 min** | **340 min / 5 h 40 min** | stretch budget |

The less aggressive release gate should be **three hours for Address and six
hours for Places**. The stretch figures leave 11-20 minutes for orchestration
variance while still satisfying those gates.

The arithmetic is grounded:

- Address historical job work divided by eight is 85.2 map minutes and 68.0
  reducer minutes before removing duplicate reducer reads and repeated builds.
- Places historical job work divided by eight is 119.1 map minutes and 105.2
  reducer minutes before PR #184 removes body read-backs and before shared
  binaries.
- Places planning can remove almost exactly half of its R2 input.
- The head and finalizer targets require structural work; they are not justified
  by dividing the old number by two.

After the checkout split is fixed, run one cold post-#184 control and enforce
the tighter of:

```text
Address target = min(180 minutes, 50% of the post-#184 cold control)
Places target  = min(360 minutes, 50% of the post-#184 cold control)
```

This prevents merged #184 improvements from being counted once in the baseline
and again as future savings.

## Execution tracks and the 75% path

The phase budgets above are Track A. They deliberately preserve the current
map -> plan -> reduce -> head -> finalize barriers. That is the shortest safe
path to a repeatable build, but those barriers leave too much work on the
critical path for a two-hour Places objective.

### What the ID/division build actually demonstrates

The closest repository precedent is successful
[run `29624600543`](https://github.com/brad-richardson/overture-geocoder/actions/runs/29624600543),
which completed the ID/division workflow in 1 hour 52 minutes on
`ubuntu-24.04-arm`:

| work | elapsed shape |
|---|---:|
| division `rebuild-shards` | 9.5 min, concurrent with ID staging |
| four registry staging ranges | 42-58 min, concurrent |
| seven release-type staging jobs | 0.6-53 min, concurrent with the registry ranges |
| global dictionary | 1.2 min |
| four independent ID hash-range builds | 26-31 min, concurrent |
| ID post/finalize/post-finalize | 21.8 min |

The useful lesson is not that ARM makes a planet build take two hours. It is
that independent ownership is established early, disjoint work starts
together, and the critical path sees the slowest owner rather than the sum of
all owners. Construction-v1 currently does the opposite at two important
points: all reducers wait for a global planner, and Places head waits for every
reducer even though it consumes map candidates rather than reducer output.

The ID data shape and proof contract are different, so its elapsed time is not a
row-count extrapolation. Its DAG topology is the precedent to copy.

### Track A: existing durable phase DAG

Track A is the itemized plan in the next section:

- at most eight static map/reduce jobs at once;
- complete each phase before starting the next;
- keep R2 objects as the durable boundary;
- optimize scans, setup, head internals, and final publication.

Expected result: Address in 169-180 minutes and Places in 340-360 minutes.

Advantages:

- no worker service, task queue, lease, or long-lived process to operate;
- the current marker-last recovery model remains almost unchanged;
- each optimization is independently measurable and revertible.

Limit: even perfect implementation leaves about 340 minutes of sequential
Places phase budgets before orchestration margin. It cannot reach two hours.

### Track B: Actions-only 16-way DAG

Track B keeps GitHub Actions as the scheduler but removes avoidable
serialization:

1. raise map/reduce concurrency to 16 under a newly reviewed request;
2. publish one authenticated binary bundle per architecture;
3. create a fast authenticated marker fan-in separate from the expensive
   Places planner;
4. start Places head from that marker fan-in, concurrently with planning and
   reduce;
5. split head into 16 coarse index-hash jobs, each owning 256 of the unchanged
   4,096 serving shards;
6. use direct final-key writes and a metadata-only final commit.

Historical runner work gives useful lower bounds:

- Address map work / 16 is 42.6 minutes and reducer work / 16 is 34.0 minutes
  before shared binaries and larger reducer batches.
- Places map work / 16 is 59.5 minutes and reducer work / 16 is 52.6 minutes
  before shared binaries and #184 transport savings.

Allowing for waves and stragglers, Track B should be treated as a
**95-110-minute Address / 150-180-minute Places experiment**, not a promise.
It can satisfy the mathematical 75% Places gate at the low end and can put
Address near its 97-minute gate. It is unlikely to make both families
comfortably repeatable in two hours because map-to-reducer hydration and
planning still begin after the last map finishes.

Its main risks are account-wide Actions concurrency, source S3 and R2
saturation, and having 16 independent head jobs reconcile one lexically ordered
published manifest. It is still much less operationally invasive than a worker
service.

### Track C: streaming ownership workers

Track C is the credible approximately two-hour architecture. "Streaming" here
does not mean an uncheckpointed TCP pipe from one Actions runner to another. It
means that immutable, content-addressed fragments remain the recovery boundary
while long-lived owners consume them as soon as their marker appears.

#### 1. Establish coarse owners before map starts

Use 16 coarse ownership ranges:

- Places reduce owners cover disjoint ranges of the existing shuffle buckets;
- Places head owners cover the top four bits of the serving index hash, with
  256 final serving shards per owner;
- Address owners cover contiguous plan/range work without changing the frozen
  Address forward key or introducing a new map shuffle.

Each map marker names exact immutable fragment hashes plus its task binding.
An owner records a durable receipt keyed by
`(request, owner, map_task_id, object_sha256)` and ignores an identical replay.
Conflicting replays fail closed.

Run the 127 Address or 89 Places map tasks through a work-conserving queue on
the admitted worker pool rather than provisioning a fresh environment per task.
Each process takes the next task after writing the previous task's durable
marker, so stragglers do not leave fast workers idle. Runtime, binaries, source
connections, and safe content-addressed cache entries remain warm while the
task-level proof and replay boundary stay unchanged.

#### 2. Ingest while maps are still running

Places reducer owners download, verify, and ingest their fixed shuffle-bucket
fragments as map markers arrive. Head owners do the same for their coarse head
candidates. Address has no map shuffle to name stable reducer ownership that
early: its owners incrementally fold directories/bindings and may prefetch
content-addressed packs, but authoritative row selection waits for the sealed
range plan. They may maintain a local DuckDB/spill database, but their receipt
set and input identities are durable in R2.

An owner may seal only after it has the exact admitted map-task set. Missing,
extra, duplicated, or conflicting receipts are terminal. A crash loses local
ingest work, not correctness: the replacement replays the immutable receipts.
An optional content-addressed owner checkpoint can reduce that replay cost.

This overlaps today's reducer hydration/ingest and head hydration/tree merge
with the 50-60 minute map wave instead of adding both afterward.

#### 3. Fold planning summaries incrementally

The map markers already carry the counts, routing summaries, and additive
bindings the planner folds. Aggregate those summaries as each authenticated
marker arrives. On the final marker:

- Address should need only a deterministic final range cut;
- Places should bind unsplit cells directly and inspect only the small hot-cell
  set that needs subdivision.

The critical-path planning tail becomes a 3-5 minute seal rather than a new
30-105 minute phase. The resulting plan bytes and binding still have to compare
exactly with the reference planner before this becomes authoritative.

#### 4. Pre-shard Places head candidates in Rust

Current head derives `__shard` with a Python SHA-256 UDF over the global merged
file, then scans the resulting pre-sharded file 16 times. The measured Python
hash path costs roughly 25.7 seconds per million rows, or 21-29 minutes at the
planet row count, before the repeated scans.

After each map has computed its exact per-token top 10, pass that much smaller
candidate stream through a Rust coarse sharder. It writes up to 16
content-addressed candidate fragments keyed by the top four serving-index-hash
bits. No token can cross owners. At 89 map tasks this changes the head-candidate
object ceiling from 89 to at most 1,424, so the request's exact object-count
projection and cap must change with it; the final serving object count does not.
Each owner then:

1. performs the associative top-10 merge for its token set;
2. partitions once into its 256 final shard IDs;
3. runs bounded `ordered Arrow -> Rust encode -> verify -> stage` workers;
4. emits an independently bound partial manifest.

A small fan-in orders the 16 partial manifests using the existing published
lexical shard order and checks counts, dual-lane sums, exact shard IDs, and
object hashes. The serving format and 4,096-shard routing layout do not change.

This removes the single 207-minute head runner and makes the slowest coarse
owner the head wall clock. It also lets head sealing run alongside reducers,
because both depend on map completion but not on one another.

#### 5. Pipeline processes inside each owner

There are two useful bounded pipelines:

```text
source/range reader -> Arrow batches -> Rust transform -> DuckDB ingest/spill

DuckDB sorted export -> Rust encoder -> verifier -> create-only uploader
```

Use a bounded disk-backed queue or a pipe that is simultaneously teed to a
content-addressed spool. Do not create an all-or-nothing anonymous pipe across
the entire phase. The global sorts/top-N operations still require a seal; they
cannot emit authoritative final objects from the first input batch.

On a standard four-core runner, keep DuckDB at one or two threads and at most
two encoder/verifier tails active. On a 32-core worker, assign explicit CPU,
RSS, and scratch budgets to the reader, ingest, and tail pools. The whole owner,
not each child independently, must enforce the admitted aggregate cap.

The immediate gain is overlap and removal of `projected.parquet ->
hydrated.arrow -> transformed.arrow` idle boundaries. The larger gain is in
the final tail: the current 4,096-shard head loop explicitly waits for each
Arrow export, encoder, store write, and disk sweep before starting the next.

The deeper variant is one Rust owner binary that performs coarse sharding,
external merge, encoding, binding, and upload around bounded Arrow batches.
That could remove Python process launch/materialization and the Python head hash
entirely, but it also replaces DuckDB's already-proven spill/sort behavior. Do
not begin with a whole-pipeline rewrite. First make the Rust coarse sharder and
tail worker byte-identical around DuckDB; replace a measured owner stage only if
profiling shows at least 15-20 minutes of remaining coordinator/materialization
wall.

#### 6. Use a scheduler that can keep owners alive

Three implementation choices, in increasing operational scope:

1. a bounded set of long-running Actions jobs that poll the authenticated marker
   directory;
2. one ephemeral 16-32 vCPU larger/self-hosted runner with local NVMe and a
   process pool;
3. a small batch/queue service in `us-west-2`, colocated with the
   `overturemaps-us-west-2` source bucket, with R2 as the durable checkpoint and
   publication store.

The single high-core runner is the simplest Track C prototype. GitHub's current
larger-runner table offers 16 vCPU / 64 GB / 600 GB and 32 vCPU / 128 GB /
1.2 TB profiles on both x64 and arm64:
[larger-runner reference](https://docs.github.com/en/actions/reference/runners/larger-runners).
The measured local 20-core Places map completed its core path in about 50
minutes, so the compute shape is plausible. The tradeoff is paid capacity and
a larger single-host failure domain. Durable per-task markers remain mandatory.

A batch service gives better retries and elastic ownership but adds credential,
image-provenance, lease, cleanup, and cost controls. It should not be the first
prototype.

#### Track C critical-path budget

These ranges overlap; they are not a list to sum naively.

| critical-path component | Address | Places |
|---|---:|---:|
| authenticated runtime/bootstrap | 3-5 min | 3-5 min |
| map wave, shared runtime and sufficient SSD | 45-50 min | 50-60 min |
| incremental plan seal | 2-3 min | 3-5 min |
| post-map reducer tail after overlapped ingest | 23-27 min | 30-35 min |
| head tail, concurrent with reducer tail | no-op | 20-30 min |
| exact-set/final marker | 5-7 min | 5-10 min |
| orchestration margin | 5 min | 5 min |
| **engineering target** | **90-97 min** | **105-120 min** |

The Address range has less margin: 97 minutes is its literal 75% ceiling against
the stitched 387-minute baseline. The Places two-hour target is more aggressive
than 75% against its 729-minute upstream-only lower bound.

Do not accept the budget based on component microbenchmarks alone. The release
gate is one operator-authorized cold planet request with:

```text
Address <= 97 minutes
Places  <= 120 minutes
```

The Places gate can be relaxed to 182 minutes only if the stated objective is
strictly "75% reduction" rather than "approximately two hours."

### ARM is an axis, not the architecture

ARM is included in both Track B and Track C as a benchmark candidate. It is not
included as assumed savings.

Standard public `ubuntu-24.04` and `ubuntu-24.04-arm` runners both provide 4
vCPU, 16 GB RAM, and 14 GB SSD:
[GitHub-hosted runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
Changing the label therefore fixes neither the disk cleanup tax nor phase
serialization. The repository's ID workflow proves ARM can run substantial
DuckDB/R2 work, not that this construction workload is faster on ARM.

Construction-v1 also cannot change labels as-is:

- the Python 3.11 ARM64 NumPy, PyArrow, and psutil hashes are already present in
  `.github/requirements-hosted-rowgroup.txt`;
- the
  [DuckDB 1.5.1 files](https://pypi.org/project/duckdb/1.5.1/#files)
  include a Python 3.11 ARM64 wheel, but its SHA-256 is not in that file;
  the missing wheel digest is
  `553c273a6a8f140adaa6da6a6135c7f95bdc8c2e5f95252fcdf9832d758e2141`;
- the
  [`unicodedata2` 17.0.0 files](https://pypi.org/project/unicodedata2/17.0.0/#files)
  include no Linux ARM64 CPython 3.11 wheel, while the workflow requires
  binary-only installation.

The execute path does not import `unicodedata2`; it is used by the independent
Places baseline/test implementation. The least invasive ARM enablement is
therefore to split execute-time and validation-time requirements, keep the
Unicode baseline in its own canonical validation job, and add the exact DuckDB
ARM wheel hash. Alternatives are a separately built and authenticated
`unicodedata2` ARM wheel or moving the runtime to Python 3.12, both larger
runtime-contract changes.

Then run the same dense Address and Places tasks on x64 and ARM64 and require:

- exact equality of every published serving object and semantic binding;
- stage-by-stage wall, CPU, RSS, scratch, S3, and R2 timing;
- no queue-time regression;
- an architecture-specific binary manifest bound into the request.

If ARM wins materially, use it. If DuckDB wins on x64 while Rust encoding wins
on ARM, mixed per-phase architecture is possible, but it requires two
authenticated binary bundles and exact cross-architecture byte evidence.
Architecture alone should carry **zero minutes** in the budget until that probe.

### Track tradeoffs

| track | likely wall target | main benefit | main cost/risk |
|---|---:|---|---|
| A: eight-way, existing barriers | Address 3 h; Places 6 h | smallest correctness and operations change | cannot reach 2 h Places |
| B: 16-way Actions, parallel head/reduce | Address 1 h 35-1 h 50; Places 2 h 30-3 h | no new worker service; proves coarse ownership | Actions/R2 saturation; still waits at map/plan barriers |
| C: incremental ownership workers | Address 1 h 30-1 h 37; Places 1 h 45-2 h | overlaps ingest/planning/head with map; ID-like critical path | leases, aggregate bounds, checkpoint replay, paid/high-core capacity |
| cross-request semantic map cache | near-zero map on an exact repeat | faster iteration and transport-only upgrades | no cold-build saving; cache identity/admission becomes security-critical |
| release-delta compiler, later | potentially minutes for small releases | reuses unchanged immutable serving objects | does not improve the first cold build; exact deletion/change detection is a new product |

The map cache would key entries by the exact source object/range identities,
projection identity, producer/binary hashes, semantic spec, and every limit that
can affect bytes. A request still re-admits and binds the cached marker; a
transport-only runtime change can reuse it only when the semantic producer
identity is explicitly unchanged. This is valuable for development and exact
repeats but carries zero credit in the cold two-hour budget.

The last row is worth a separate design after cold builds are stable. A
release-delta compiler can compare authenticated source identities with the
previous release and rebuild only affected Address ranges, Places cells/tokens,
and head shards while reusing unchanged content-addressed objects. It could beat
two hours routinely, but only with an exact update/delete inventory and a
fail-closed cold-rebuild fallback. Counting precomputation outside the build
timer would merely move work and is not an optimization.

## Itemized optimization plan

### 0. Wire merged PR #184 into the pinned data-plane job, then measure

Status: merged as `a028df7`, but not present in the tree run `30322532358`
executed. That cancelled run is not the measurement.

PR #184:

- hydrates a content-addressed staging object with one GET rather than HEAD+GET;
- replaces staging upload's full GET read-back with stored-byte HEAD proof;
- retries interrupted streaming GET bodies;
- admits five Places members concurrently under the contract/disk bound;
- uploads at 16 workers after measuring the actual 209 MB maximum;
- verifies stored metadata at 16 workers.

This should improve cold map, reduce, and head too, because all three publish
staged objects. The old planet head staged 4,098 objects / 5.14 GB serially and
read every new object back. Do not describe #184 as a finalize-only win.

Gate:

- prove in the job log, before credentials are used for publication, the base
  producer commit plus every executed runtime-helper digest;
- preserve admission/upload/verification durations separately;
- record object count, bytes, concurrency, retry count, and marker time;
- use the measurement as the new control rather than a projection.

### 1. Raise matrix concurrency from 4 to 8

Priority: highest low-risk cold-build lever.

The workflow and confirmation parser already allow `MAX_PARALLEL=1..8`.
The canonical request generator currently emits 4, however, and the typed
confirmation binds that value. Eight-way planet execution therefore needs a new
reviewed request/package (and a new request-derived namespace); it is not an
authorized reinterpretation of the current request. Historical total runner
time makes the ideal savings large:

| phase | current wall | estimated 8-way wall | estimated saving |
|---|---:|---:|---:|
| Address map | 173 min | 90-100 min | 73-83 min |
| Address reduce | 138 min | 75-90 min before larger batches | 48-63 min |
| Places map | 248 min | 130-145 min | 103-118 min |
| Places reduce | 213 min | 110-125 min | 88-103 min |

The estimates allow for stragglers rather than assuming perfect halving. Map
task balance is already acceptable; the Places max was 25.3 minutes against a
45-minute task bound. Redesigning the map matrix is not the first move.

Probe:

1. run eight representative dense map tasks together against the real staging
   backend through a dedicated benchmark or a newly reviewed request;
2. compare task compute, R2 GET/PUT/HEAD latency, retries, and runner RSS/disk to
   four-way evidence;
3. run the preserved Europe path at eight-way;
4. accept only if no R2 throttling appears and phase wall improves by at least
   35%.

Eight is the Track A ceiling, not the Track B/C ceiling. After the eight-way
probe succeeds, a separate change may extend the request schema, parser, typed
confirmation, canonical caps, and saturation tests to 16. The current parser
correctly rejects 16; do not bypass that rejection or reinterpret an existing
request. Accept 16-way execution only if its dense-task probe improves wall by
at least another 25% over eight-way without material retry/throttle growth.

### 2. Build and authenticate Rust binaries once per architecture

Priority: low risk and useful for cold and resume paths.

Every map and reducer job builds identical release binaries from the same
producer commit and `Cargo.lock`. Measured compile cost was:

| family | map compile runner-min | reduce compile runner-min | wall represented at concurrency 4 / 8 |
|---|---:|---:|---:|
| Address | 74.1 | 68.2 | about 35.6 / 17.8 min |
| Places | 51.6 | 75.9 | about 31.9 / 15.9 min |

Add one build job per target triple. Package only the required binaries with a
manifest binding:

- producer commit;
- `Cargo.lock` SHA-256;
- target triple and Rust version;
- each binary's SHA-256.

Every consumer verifies the manifest and binary digests before execution.
Build it in parallel with nondependent admission work where possible.

Gate: slice bytes and construction evidence must be byte-identical to a local
build, and a deliberately substituted binary must fail before data access.

### 3. Put marker discovery before expensive resume work

Priority: highest recovery lever; no cold-build saving.

Immediate change: in each matrix job, run `admit-task` before disk reclamation
and Cargo build. On the Address resume this removes about 225 runner-minutes of
just those two steps.

Better change: add one credentialed checkpoint-inventory job after secret-free
admission. It authenticates all durable map/reducer/head markers and emits
matrices containing only missing work. A completed map should not provision 127
runners merely to say "already complete."

For general reducer resume, the durable per-partition marker must reference a
content-addressed complete reduction record, not only the serving artifact.
That record needs the binding/evidence finalize currently receives through a
seven-day Actions artifact. Once that exists:

- retain and authenticate the plan in durable staging;
- form reduce matrices only from batches with missing partitions;
- retain and authenticate the head result;
- start at the first incomplete phase without special head-only/finalize-only
  workflow branches.

Targets:

- complete-map resume discovery under 5 minutes;
- complete-reducer resume discovery under 10 minutes;
- zero Cargo builds and zero disk-reclamation steps for skipped tasks.

### 4. Cut Address reducer fan-out near 60 jobs

Priority: high for Address; uses an existing dispatch knob.

The current 117 jobs are short and duplicate pack hydration across job
boundaries. Lowering `max_reduce_jobs` makes larger contiguous partition batches;
`_batch_retention` already keeps a pack until its last use within a batch.

Probe `max_reduce_jobs=60` on the preserved Europe inputs, then on an authorized
non-promoting planet control. Require:

- identical plan partition set, reduction bindings, serving hashes, and final
  manifest;
- per-job scratch/RSS within the existing caps;
- no job near the timeout-derived batch ceiling;
- total hydration at most 220 GiB versus 292.66 GiB;
- eight-way reduce wall at most 75 minutes.

If hydration does not fall enough, implement the already-described
range-owning Address reducer. It can open each pack once for a contiguous hash
range without changing the frozen Address map shuffle, partition key, or serving
format.

Do not apply the same job-cap change blindly to Places. Places already owns
shuffle-bucket ranges and its 128-job cap is part of the resident-input safety
shape.

### 5. Remove the second full Places planning read

Priority: high, narrow, and supported by exact planet data.

`adaptive_genesis_plan` currently:

1. reads all packs into bounded DuckDB planning state;
2. plans subdivisions;
3. re-fetches every pack to compute per-partition additive bindings.

For the successful planet plan:

| measure | current | selective second pass |
|---|---:|---:|
| packs | 10,119 twice | 10,119 once + 10 again |
| object reads | 20,238 | 10,129 |
| hydrated bytes | 59.77 GB | about 31.30 GB |
| cells requiring subdivision | 6 of 16,511 | same |
| split-cell packs | all packs reread | 10 packs / 1.41 GB reread |

The marker's pack directory already carries exact per-cell routing summaries and
bindings. For an unsplit cell, that binding is the partition binding. Only split
cells need row-level token-hash routing from pack bodies.

Implementation:

- accumulate directory bindings by cell while doing the first pass;
- after subdivision, assign directory bindings directly to depth-0 partitions;
- construct the set of packs whose routing summaries mention a split cell;
- re-fetch only those packs and apply the existing `_partition_mask` logic;
- retain the final whole-plan binding reconciliation.

Target: at least 45% fewer planning bytes and requests, plan wall at most 30-45
minutes, and byte-identical plan output.

### 6. Make the Places head a measured, bounded parallel pipeline

Priority: required for the Places 50% goal.

The existing 207-minute job has only coarse whole-job timing. Add timestamps and
bytes/rows around:

1. candidate hydration and every merge-tree level;
2. counts;
3. shard-id derivation;
4. each shard COPY batch;
5. independent binding;
6. per-shard sort, encode, stage proof;
7. final sharded verification.

Two code shapes are already visible without that profile.

#### 6a. Replace 16 full scans with radix partitioning

`_write_head_shards` materializes `pre-sharded.parquet`, then runs 16 queries:

```text
read all pre-sharded rows
filter shard range 0-255
...
read all pre-sharded rows
filter shard range 3840-4095
```

That is 16 full scans to keep only one sixteenth per scan. Preserve the memory
bound with a two-level layout:

1. partition once into 16 coarse `shard_id // 256` groups;
2. read each coarse group once and partition it into its 256 final shards.

The fan-out work drops from 16 full scans to two row passes, an 87.5% reduction
in that stage's full-file scan equivalents. Published bytes remain governed by
the later explicit `ORDER BY HEAD_ORDER`, so workspace parquet container
differences need not change `.plhd` bytes.

#### 6b. Parallelize independent shard tails

The current loop performs sort, encode, R2 stage, and scratch sweep serially
4,096 times. After coarse partitioning, give four worker processes disjoint
coarse groups. Each worker uses its own DuckDB connection and scratch directory;
the parent combines digests/counts and emits manifest entries in the existing
lexicographic order.

Start with four workers, not 16. Enforce:

```text
workers × measured worst sort/encoder/staging residency
    + shared shard workspace
    <= existing RSS and scratch caps
```

If a single runner cannot safely reach the target, split the head into:

- one merge/coarse-shard job;
- 16 matrix jobs, one per coarse range;
- one deterministic fan-in/verification job.

That architecture adds durable intermediate objects but removes the measured
207-minute (330-minute timeout-bounded) single-job long pole and gives exact
range-level resume.

Targets:

- radix stage at least 60% faster than the current shard-write stage;
- shard-tail throughput at least 2.5x faster with four workers;
- total planet head at most 80-100 minutes;
- all 4,096 shard hashes, counts, dual-lane sums, and routing manifest bytes
  identical, unless a manifest format change is separately reviewed.

The pure-Python independent binding is only about four minutes at the measured
30.8 million output rows. Moving it to Rust may be tidy, but it is not the
first-order head optimization.

### 7. Write final immutable objects once

Priority: architecture lever; use if #184 does not already put finalize inside
the 15-20 minute budget.

Today a final object is:

1. written to R2 staging by its producer;
2. fetched and hashed during final admission;
3. fetched again and uploaded to a final key;
4. metadata-verified;
5. included in the exact-set listing;
6. made discoverable only when the completion marker lands.

The actual Places exact set is about 40,931 objects / 51.8 GB. Even after #184
parallelizes it, the finalizer still moves roughly two staging reads plus one
upload of that set.

The contract's final slice namespace is known at admission. Split producer
outputs into:

- internal-only objects, which stay in run-scoped staging: Places term packs,
  candidate packs, and any still-consumed build intermediates;
- final immutable objects, which the producing map/reduce/head phase writes
  create-only under their final content-addressed names: per-record packs,
  routed/Address serving objects, head shards, and the head routing manifest.

Finalize then:

- authenticates the complete producer records and bindings;
- HEAD-proves final object length, store ETag, and SHA metadata in parallel;
- writes the two small manifests;
- verifies exact-prefix equality;
- writes the completion marker last.

Partial output is non-promoting because no reader/catalog may accept a slice
without its marker. Add explicit cleanup for unmarked namespaces before taking
this path.

This eliminates the bulk final copy and one duplicate staged write for
final-only objects while retaining create-only and marker-last semantics.

As an interim bridge, Cloudflare R2 supports S3 `CopyObject` and source
conditions. Its destination `if-none-match` extension is currently beta, so use
it only after a live one-object proof of source identity, destination
create-only behavior, metadata preservation, conflict behavior, and immediate
read-after-copy:

- [R2 S3 API compatibility](https://developers.cloudflare.com/r2/api/s3/api/)
- [R2 CopyObject extensions](https://developers.cloudflare.com/r2/api/s3/extensions/)

Direct producer writes are the cleaner steady state because no copy operation is
needed at all.

### 8. Remove runner-image tax after the data-path work

Standard public Linux GitHub-hosted runners are currently 4 vCPU, 16 GB RAM, and
14 GB SSD for both x64 and arm64. ARM is therefore not a capacity increase and
should be treated as a byte-identity/performance benchmark, not assumed savings:
[GitHub-hosted runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).

The Places map matrix spent 108.3 runner-minutes deleting preinstalled tools to
create disk headroom. Options:

- a larger GitHub runner with enough native SSD;
- an ephemeral self-hosted image with exact Python/Rust dependencies and
  sufficient NVMe;
- a whole-phase 16-32 vCPU worker with durable R2 checkpoints.

The local 20-core planet Places map completed its core data path in about 50
minutes, versus 248 minutes hosted at four-way concurrency, so a high-core
ephemeral runner is a credible Track C prototype. It also consolidates setup and
local shuffle I/O. Its costs are operational: image provenance, ephemeral
credentialing, isolation, durable checkpoints, and recovery from host loss.
Benchmark it after the #184 baseline and exact-byte architecture probe; the
two-hour goal should not wait for Track A to miss its six-hour gate.

## Recommended sequence

Waves 0-3 are the reusable Track A foundation. The Track B/C probes should
branch after Wave 0 rather than waiting for a six-hour build to prove that
serial barriers are serial.

### Wave 0: reset the baseline

1. Do not treat run `30322532358` as #184 evidence; it executed the pinned old
   helpers.
2. Choose a new request or an authenticated, hash-bound compatibility overlay.
3. Log and assert the executed helper hashes.
4. Run the real #184 finalize-only recovery and record its phase breakdown.
5. Add phase-internal timing, especially to Places head.
6. Run one post-#184 cold control before claiming percentage improvement.

Request sequencing matters. Changes to `places_construction_v1.py`,
`address_construction_v1.py`, `Cargo.lock`, canonical caps, or the producer
commit create a different request identity and staging namespace. Land and prove
the independent PRs on slice/Europe evidence first, then prepare one reviewed
cold-control request containing the accepted set. Do not pay for a new planet
namespace after every individual optimization.

### Wave 1: low-risk parallelism and duplication

1. Request-pinned binary artifact.
2. Early map marker check plus central checkpoint inventory design.
3. Eight-way R2 saturation probe.
4. Address `max_reduce_jobs=60` probe.
5. Selective Places planning binding pass.

Expected gate after Wave 1:

- Address at or below three hours, or close enough that direct final writes are
  the only remaining long pole;
- Places map + plan + reduce at or below four hours total.

### Wave 2: Places head

1. Profile the existing head by substage.
2. Implement two-level radix sharding.
3. Add four bounded shard-tail workers.
4. Move to a 16-range head matrix only if one-runner evidence cannot reach
   80-100 minutes safely.

### Wave 3: single-write publication

1. Prove the namespace/marker discovery rule in tests.
2. Write final-only producer objects directly to final immutable keys.
3. Make finalize metadata/exact-set only.
4. Add abandoned unmarked-namespace cleanup.

### Wave 4: Actions-only 75% probe

1. Split the hosted runtime requirements and run an x64/ARM64 exact-byte and
   stage-timing comparison.
2. Choose the measured architecture; publish and authenticate its binary bundle.
3. Prove eight-way, then 16-way, S3/R2 saturation with dense tasks.
4. Add the fast exact marker fan-in.
5. Implement the 16-way Rust coarse head sharder and partial-manifest fan-in.
6. Let head run concurrently with plan/reduce.
7. Run a preserved-input head/reduce replay before any new planet source map.

Stop here if the measured cold build already reaches Address <=97 minutes and
Places <=120 minutes. A worker service would then add complexity without
serving the wall-clock objective.

### Wave 5: streaming-owner prototype

1. Specify owner identity, exact expected task set, receipt, conflict, seal,
   lease, restart, and cleanup semantics.
2. Mutation-test duplicate, missing, conflicting, delayed, and out-of-order map
   markers.
3. Prototype one coarse Places head owner and one reducer owner against the
   preserved planet marker/object set.
4. Run the bounded process pipelines on one measured 16-32 vCPU ephemeral
   worker; prove aggregate RSS/scratch limits under forced child overlap.
5. Prove restart from receipts/checkpoints produces the exact Track B bytes.
6. Run Monaco/Seattle, Europe, and then one operator-authorized cold planet
   request against the <=97 / <=120 minute gates.

## Changes not recommended for this target

- Do not port an Address map shuffle or change Address forward keys. The current
  output is already hash-clustered and that decision is frozen.
- Do not reduce the 4,096 Places head shards. Serving fetch size, not encoder
  capacity alone, fixes that layout.
- Do not lower Places reducer count by analogy with Address without a residency
  probe.
- Do not redesign map task boundaries first. Existing skew is acceptable and
  concurrency offers a much cheaper test.
- Do not count ARM as a win before a representative byte-identical benchmark;
  the standard runner has the same nominal CPU/RAM/storage.
- Do not stream uncheckpointed records directly between Actions runners. Use
  local bounded process pipelines and immutable inter-runner fragments; a lost
  socket must never force a whole planet restart or erase input provenance.
- Do not claim a two-hour build by moving normalization into an unmeasured
  prerequisite. A reusable release cache is valuable only when its construction
  is reported separately and its reuse identity is exact.
- Do not remove independent byte/binding verification to save a pass. Change
  where proof happens or use store-computed proof, but keep admission,
  create-only writes, exact-set equality, and marker-last publication.
- Do not prioritize repacking tiny manifests/directories. Persistent clients and
  concurrency have made bulk data movement and repeated scans the larger terms.

## Verification and stopping policy

Each optimization should be one concern/PR and follow the repository ladder:

1. focused invariant and mutation tests;
2. Monaco Places / Seattle Address exact-byte slice;
3. preserved Europe timing/RSS/disk/fan-out comparison;
4. one-object live R2 proof for changed remote semantics;
5. operator-authorized non-promoting planet run.

Stop or revert a lever if:

- any published object hash, binding, shard routing, or marker order changes
  without an explicitly reviewed format change;
- eight-way concurrency produces R2 throttling/retry growth that erases at least
  half of the expected wall saving;
- 16-way concurrency fails to improve at least 25% over eight-way or causes
  source/R2 tail latency to dominate the slowest wave;
- Address 60-job peak approaches the existing scratch/RSS guard;
- head parallelism cannot state and enforce an aggregate memory/disk bound;
- an ownership worker can seal without the exact admitted map-task receipt set,
  or a duplicate/conflicting receipt is not detected before output;
- crash/restart from durable owner receipts does not reproduce the reference
  plan, shard objects, bindings, and lexical manifest bytes;
- a resume path trusts an Actions artifact or marker without authenticating it
  to the exact request/producer.

## Source index

- [Current construction-v1 state](construction-v1-state.md)
- [Planet probe findings](2026-07-26-planet-probe-findings.md)
- [Growth test and path to planet](2026-07-24-growth-test-and-path-to-planet.md)
- [R2 staging design](2026-07-24-r2-staging-design.md)
- [Construction-v1 follow-ups](2026-07-24-construction-v1-follow-ups.md)
- [Construction-v1 one-way doors](2026-07-23-construction-v1-one-way-doors.md)
- `.github/workflows/construction-v1.yml`
- `.github/workflows/rebuild-r2-shards.yml`
- `scripts/places_construction_v1.py`
- `scripts/construction_v1_hosted.py`
- `scripts/construction_staging_v1.py`
- `scripts/r2_verified_store.py`
