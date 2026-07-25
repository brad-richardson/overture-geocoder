# construction-v1: current state and the next increment

**Living document. Read this before touching construction-v1.** Undated on
purpose — the dated docs in this directory are point-in-time analyses; this one
is the current picture and supersedes them where they disagree.

Last updated 2026-07-25.

## The one-paragraph version

The pipeline works end to end on real data at small scale, and until 2026-07-25
it could not run at planet scale for exactly one reason: **intermediate data
moved between phases as GitHub Actions artifacts.** The 63.5 GB Places store got
downloaded by every reduce batch, re-uploaded by each, downloaded again whole by
head and by finalize. It does not fit on a runner, so reduce never started. That
transport is now **replaced**: map writes the store into a run-scoped R2 staging
prefix through the `ObjectStore` seam, every inter-job artifact carries markers
and JSON only, and each consumer fetches by key exactly the objects its markers
name — the same shape `build_id_index.py` already uses. **For PLACES** what
remains before a planet dispatch is operational rather than structural: a scoped
staging-only R2 token, and row-group range reads to tighten read amplification
further. **For ADDRESSES the reduce phase is bounded too** as of 2026-07-25: it now
releases each hydrated pack at its last use and enforces the result against
`max_scratch_bytes`, so a job holds one hash SLICE of packs — one per map task holding
its country, single-digit GB at planet scale — instead of everything it ever opened.
What remains for addresses is object amplification, not a partition that will not fit:
tunable today with `--max-reduce-jobs` (which lowers R2 reads but NOT peak) and to be
closed by a range-owning reducer. Two earlier claims here ("there is no shuffle to
narrow it", "an address reduce partition would still not fit a runner") were WRONG, and
so was a first draft of the correction ("peak flat at one pack") — see the family
caveat below for all three and for the peak-resident law.

## Read the history this way

Four sessions in one week each approached "planet scale" confidently, rewrote a
large part of the pipeline, and stalled. The common thread is **designing at
planet scale against phases that had never run.** Before 2026-07-25, `reduce`,
`head`, and `finalize` had never executed in CI even once — every green
construction-v1 run was a dry-run doing `admit` plus three map tasks.

The counter-measure now exists and should be the default working mode:

```bash
python scripts/build_slice_inventory_v1.py --release 2026-07-22.0 \
  --bbox 7.36 43.71 7.47 43.78 --output slice/inventory.json
python scripts/run_slice_construction_v1.py --inventory slice/inventory.json \
  --task-index 33 --release 2026-07-22.0 --work slice/work
```

That is Monaco, 38,182 real Overture places, all five phases, **~13 seconds**,
no credentials. Use it for every change. If a change cannot be demonstrated on
this loop, it is probably a design document rather than a change.

This exact loop also runs in CI on every relevant PR as `.github/workflows/slice-smoke.yml`.

## What has landed

- **Caps raised** to `partition_term_rows` 2,000,000 and `distinct_tokens`
  400,000 (`construction_v1_hosted.HOSTED_LIMITS`).
- **Map-side combiner** — keeps only the top `maximum_serving_candidates` (256)
  rows per `(partition_cell, token)`. Exact, because top-N under a total order
  is decomposable. Removes 46% of planet term rows.
- **Map-side shuffle** — `pack_id` is now a hash bucket of `partition_key`, so a
  fragment holds a set of cells completely and nothing else. **This is the piece
  the whole architecture turns on.**
- **Per-place positions artifact** — map emits one row per admitted place
  RECORD, pre-combiner, self-sufficient, on the same shuffle as the term packs,
  and finalize publishes it durably. 2026-07-25 decision, detailed under "Query
  surface" below. This is what makes a spatial reverse index additive instead of
  a planet map re-run.
- **Slice harness** — `build_slice_inventory_v1.py`, `run_slice_construction_v1.py`.
- **Partition plan v2** (`scripts/places_partition_plan_v1.json`) — regenerated
  from 2026-07-22.0 post-combiner. Consumed only by `predict-reduce`.
- **Reduce reads by bucket range** — a Places reduce job owns an inclusive
  `[bucket_start, bucket_end]` (the `build_id_index.py --prefix-start/--prefix-end`
  convention), derives its fragment set from the map markers, opens each fragment
  **once**, and emits every partition whose cell hashes into the range. The
  planner cuts the bucket space into contiguous strides rather than cutting the
  partition list; `plan-reduce` records the ranges and the workflow matrix key is
  unchanged. Outputs are byte-identical to the per-partition reducer, which is
  still there for the single-partition CLI path and the rehearsal.
- **The reducer's published bytes are bound to the plan.** Every binding in the
  reducer is computed pyarrow-side while ingesting, but the artifacts come out
  DuckDB-side through a `WHERE` on the partition tag. Nothing connected the two,
  so a wrong predicate published the wrong partition's rows and *every* check —
  including finalize's reconciliation, which sums those same pyarrow bindings —
  accepted it. Now the predicate's result set is measured in SQL against the
  partition's identity (cell, ownership prefix, planned row count) and the leaf
  that is about to be stored is digested back to the plan binding on both lanes,
  with the serving stream derived from that proven leaf rather than from a second
  unproven predicate.
- **Places finalize actually reconciles.** `reconciles: true` for places was a
  literal, checked only against the summed binding. It is now
  `places_construction_v1.validate_complete_reduction`: the partition id set must
  match the plan exactly, and each reduction's binding must equal the binding the
  plan recorded for *that* partition. A duplicate covering a missing partition of
  equal size, or two partitions carrying each other's rows, keeps the sum correct
  and is now rejected. `Limits`'s partition-cap defaults are also the hosted
  production caps now, and `predict-reduce`'s addresses branch is floored by the
  per-country bisection the planner performs (474 -> 725 on the planet inventory).
- **Reduce is watched** — `StageWatchdog` now wraps the reducer's Python +
  pyarrow + DuckDB ingest and its serving encode, with the same caps and the
  same fail-closed semantics as the two `map_task` stages. It was the one phase
  nothing bounded.
- **The intermediate store lives in R2 staging, for BOTH families**
  (2026-07-25). This was "the next increment" and it has landed; details below.

## The store transport (2026-07-25) — what the old "next increment" became

`scripts/construction_staging_v1.py` adds `StagedObjectStore`: the exact
four-method surface the construction code already uses
(`path`/`put_content`/`read_json`/`write_marker_last`, defined by
`address_construction_v1.LocalObjectStore`), mirroring every object into a
run-scoped R2 staging prefix through the existing `ObjectStore` seam
(`scripts/r2_verified_store.py`: `FilesystemStore` credential-free, `S3Store` for
real R2, both create-only and content-addressed).

**Staging key layout**, chosen to stay inside the convention
`.github/workflows/r2-cleanup.yml` can guard and expire:

```
staging/global-v2/<request_sha256>/construction-v1/<family>/<construction store key>
```

Run-scoped (`request_sha256`), so concurrent or retried dispatches never collide
and a resumed run finds its own objects already present — create-only plus
content-addressing is what makes resume free. Family-scoped below that, so the
two families never write into one another's create-only key space. The construction
key shape underneath is **unchanged** (`{class}/sha256/{digest}{suffix}`), because
markers already record those keys. A prefix outside
`staging/global-v2/<64-lowercase-hex>/…` is refused in code, since r2-cleanup's
phase-2 guard is literally `^staging/global-v2/[0-9a-f]{64}/$`.

**How consumers discover objects: they derive, they never list.** A phase already
holds the keys it needs — in the map markers, the plan, and the reduction JSON —
so it computes `staging_prefix(request_sha256, family) + key` and fetches that.
No LIST, no side manifest, no directory object, therefore no listing cost and no
listing race. `--store-root` becomes an EMPTY local cache on every phase after
map.

**Fail-closed, everywhere, with failing tests for each case**
(`tests/test_construction_staging_v1.py`, plus CLI-level cases in
`tests/test_construction_v1_hosted.py`):

- an absent staged object aborts. **There is no fallback to the artifact path** —
  a silent fallback is how a partial store becomes a wrong slice;
- a short object aborts, and a same-length object with different bytes aborts.
  The digest is IN the key, so verification needs no side table;
- a key that is NOT content-addressed can never be hydrated as an object
  (`path()` refuses it) because nothing would verify it. Markers travel through
  `read_json` and are verified against the store's recorded `sha256` metadata; a
  staged marker with no such metadata aborts rather than being trusted;
- rewriting a marker with different bytes aborts (create-only, first writer wins);
  rewriting it with identical bytes is a verified no-op.

**Resume became real, and that needed one more fix.** `admit-task` reads the
marker through the same store, so a fresh dispatch now genuinely skips completed
map tasks — which it never did before, because a fresh runner had no local store
and `--remote-root` was not wired. But the artifacts carry markers only, so a task
that skipped and emitted no marker would have silently dropped itself from
`plan-reduce`'s fan-in. `admit-task --marker-out` republishes the completed task's
marker on the skip path, fails closed if completion was observed without a
readable payload, and the workflow asserts the file is non-empty.

**What the workflow carries now.** `cv1-map-*`, `cv1-plan`, `cv1-reduce-*` and
`cv1-head` are markers, plan, reductions, head result and ledger fragments —
single-digit MB. The store directory is in none of them, and
`tests/test_construction_v1_workflow_contract.py` asserts it, because adding it
back is the one regression that reopens the blocker. The finalize job's local
publish tree was renamed `staging/` → `publish/`, since `staging/` now
unambiguously means the R2 intermediate prefix.

**Credentials.** Map, plan, reduce, head and finalize now all receive
`CLOUDFLARE_ACCOUNT_ID`/`R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY` — the SAME
workflow secrets finalize already used, by owner decision, so the transport was
not blocked on a token change. That is a real widening of blast radius (~89
parallel map jobs on a public repo instead of one finalize job) and it is recorded
as such; issuing a scoped staging-only token is the tracked next step. The
workflow is still `workflow_dispatch`-only and never runs on `pull_request`, and
every credentialed step is execute-mode-gated.

**What each phase actually downloads, stated honestly.** "The store stops
travelling between phases" is true of the *artifact*; it is not true that no phase
reads pack bodies. Three do, and their bound is the **peak resident hydrated bytes**
— the high-water mark of fan-in on the runner at once, not the total streamed
through. Every phase reports both (`--staging-report`, and in the slice summary):

| phase | reads | bound |
|---|---|---|
| map | nothing (it writes) | — |
| plan (places) | **every pack body, twice** | `max_fan_in_packs` (256) packs at once in the DuckDB pass, then ONE at a time in the binding pass, released after each. ~390 MB planet, against ~34 GB eager |
| plan (addresses) | marker JSON only | zero pack bytes |
| reduce (places) | the fragments in its own bucket range | ~1 GB post-combiner (the bucket-range measurement above) |
| reduce (addresses) | packs from essentially every map task | **unbounded — see the family caveat below** |
| head | EVERY task's head-candidate pack, in one `read_parquet` | **MEASURED, not bounded** — 7.35 MB from one Monaco task, order 10 GB at 89 planet tasks. `run-head --staging-report` records it and the job now carries the 25 GB free-disk floor; batching it is a restructure, tracked as a follow-up |
| finalize | exactly the published set, **twice** (hash, then upload) | **ONE OBJECT at a time** — hydrate, verify, upload, `release()`. Peak resident = **exactly the largest single published object**, measured as such in both families (12.09 MB on Monaco, 29.17 MB on Seattle — the latter is one 29.17 MB serving artifact). NOT the partition cap: that bounds only the routed lane, and at `--shard-bits 4` the biggest object is a head shard at ~625–690 MB. It still writes the whole slice into its local `publish/` tree for the R2 mirror step, so the phase's disk floor is *that* tree plus one object, not twice the slice |

Two eager-hydration defects of the same class, both now fixed and both now
asserted. The plan phase's eager `[store.path(k) for k in packs]` defeated its own
`max_fan_in_packs` batching completely and would have put the whole planet term
store on the one job run 30113308268 died on. **Finalize had the identical defect,
in the last phase of the run**: it built its whole exact set as a list of
`store.path(...)` results — every published object hydrated before the first
upload, with no `release()` anywhere in the phase — which is 13–18 GB at planet
scale (a ~10–11 GB head payload plus 3.3–6.7 GB of positions packs, before the
routed lane). Both now hydrate and evict, and both slice-smoke jobs assert
`peak_resident < bytes_hydrated` and `objects_released > 0`, which are false under
eager hydration.

**The RAM bound, which used to appear nowhere.** `publish_exact_set` read EVERY
artifact's full bytes into one `payloads` dict during admission and consumed them
from that dict in the upload loop, so the entire published set was resident in
process memory as well as on disk — the same 13–18 GB, on a 16 GB runner, at the
very end of a multi-hour run. It now computes each identity by STREAMING the file
(`file_identity`, 1 MiB chunks) and re-reads each payload inside the upload loop
where it is needed. **Peak RAM is exactly the largest single published object** —
measured as such, in both families — which makes it predictable rather than merely
bounded: 1.6 MB while publishing a 12.6 MB set of 24 objects, against 12.6 MB
(100% of the set) before. It is **not** the partition cap (512 MiB estimated
uncompressed); that bounds only the routed lane, and at `--shard-bits 4` the
largest published object is a head shard at **~625–690 MB** (a 10–11 GB planet head
payload over 16 shards), which exceeds it. Comfortable on a 16 GB runner either
way, and the two bounds interact favourably with #169: at 4,096 head shards the
largest head shard drops to roughly 2.7 MB, shrinking this bound by ~250x.

The two-phase contract is unchanged: every identity is admitted, gated and sorted
before any upload, and the marker is committed last. Splitting one read into two
did lose one invariant that used to hold by construction — *the admitted identity
and the uploaded payload are the same bytes* — so the upload loop now re-hashes the
payload it is about to publish and compares it to the pre-admitted digest. Nothing
else covered it: a `local_member` (the two manifests) is digest-verified on neither
read, a staged member's re-hydration hits `StagedObjectStore.path()`'s
`if path.is_file(): return path` short-circuit so the second read is not
digest-checked, and the per-upload HEAD compares only `bytes`, so a same-length
swap passed. The failure it prevents is the expensive kind: a marker committed
recording the GOOD identity over BAD bytes, caught only by
`verify_whole_slice_once` — which runs after the marker.

The cost of that fix, stated: finalize now reads each published object from
staging **twice** — once to hash it into the admitted set (where the
content-addressed and provenance gates run), once to read the payload it uploads.
That is bounded extra GET volume, not extra residency, and it is the price of
keeping "the whole admitted set is fixed before any upload" while holding one
object. Planning admission from the identities the producing phases already
recorded would make it one read; tracked as a follow-up.

**Evidence, on the fast loop, no credentials.** Both slice-smoke jobs run the
whole transport with a filesystem staging backend and each phase on its own empty
store:

| | Monaco Places | Seattle addresses |
|---|---|---|
| harness wall time | 13.3 s | 12.2 s |
| records | 38,182 | 104,928 |
| map published to staging | 18 objects / 25.53 MB | 7 objects / 90.73 MB |
| plan hydrated / peak resident | 32.88 MB / **16.44 MB** (8 released) | 0 / 0 |
| reduce hydrated / worst job peak | 16.44 MB / **8.30 MB** | 10.89 MB / 10.89 MB |
| head hydrated / peak resident | 7.35 MB / 7.35 MB (unbatched) | 0 / 0 (no head phase) |
| finalize hydrated from staging | 56 objects / 71.88 MB (28 objects, twice) | 10 objects / 64.29 MB (5 objects, twice) |
| finalize peak resident / released | **12.09 MB** / 56 objects | **29.17 MB** / 10 objects |
| serving objects published | 20 (4 `.plrv` + 16 `.plhd`) | 1 `.av1` |

Finalize's `hydrated` doubled and its `peak resident` fell, in the same change:
28 → 56 hydrations is the same 28 objects fetched once per pass, and 35.94 MB →
12.09 MB peak is the whole point — before, `peak == hydrated` because nothing was
ever released. The published byte set is unchanged by it: `set_sha256`
`48c895cd…` for Places (31 files, 35,953,036 bytes) and `73a0e76f…` for
addresses (8 files, 32,147,481 bytes), identical before and after.

The published slice is **byte-identical** to a `--no-staging` run of the same
slice — same content-addressed names, same sizes; 31 files for Places, 8 for
addresses.

Two byte totals in `store_bytes_by_class` do wobble, and neither is about
transport:

- `map/` drifts a few bytes run to run in **both** modes, because the pack proof
  directories inline per-run wall-time and RSS evidence;
- `serve/` differs by 96 bytes between a staged and a `--no-staging` run because
  the head **manifest** embeds each shard's absolute local path, which contains
  the `--store-root` directory name. That manifest is not part of the published
  slice (`_artifact_keys` collects `shard_objects`, not `manifest_object`), which
  is why the published trees are still identical. It is a real wart — a digest
  that depends on a runner's directory layout — and is tracked as a follow-up.

**The Places routed serving objects now actually get published.** They did not:
`_artifact_keys` read `reduction["artifact"]`, which only the ADDRESS reducer sets,
so a Places finalize published head shards, positions packs and two manifests and
dropped every `.plrv` — a planet Places execute would have served no routed
payloads at all, and `objects`, `reconciles` and `positions_objects` were all
non-zero. The serving key is now a per-family table
(`REDUCTION_SERVING_OBJECTS`: places → `routed_object`, addresses → `artifact`) and
a reduction naming none is fatal. `leaf_object` stays unpublished on purpose: it is
the head phase's input, it holds TERM rows, and the positions packs are the durable
per-record artifact. The published serving set is asserted to COVER the reduction
set — Monaco publishes 4 `.plrv` (one per partition, names matching the reducer's
recorded digests exactly) plus 16 `.plhd`, and the slice grew from 11.86 MB to
35.95 MB, which is the routed payload that was previously being lost.

**The head half had the identical defect and is closed the same way.**
`head.get("shard_objects", [])` was the same permissive get one line down: a places
slice with ZERO `.plhd` shards published cleanly, exit 0, `reconciles: true`,
satisfying the routed gate — including the shape where head.json claims
`shard_count: 16` while the tree holds none. `--head` is now REQUIRED for every
family in `HEAD_FAMILIES`, `shard_objects` must be non-empty, and its length must
equal the `populated_shards` the head phase itself reported. Every serving identity
(routed, head shard, address artifact) must carry `key`/`sha256`/`bytes` or abort
naming itself, so a truthy-but-malformed entry can no longer reach the publication
verification and die in a traceback. The workflow gate is now the same EQUALITY the
slice jobs assert (`serving == reductions + populated_shards`) rather than a lower
bound, since `-ge` accepted exactly the shard-free slice above.

**Two consequences worth knowing before a dispatch**, both recorded as follow-ups
rather than changed here: a staging prefix written before this fix cannot be resumed
for reduce (the reduce marker payload changed from `artifact: null` to the real
identity, and markers are create-only), so such a prefix must be **abandoned for a
fresh `request_sha256`** — no planet execute has run, so this is local only. And
`leaf_object` is still staged with a full readback while nothing on the hosted path
reads it: 16.44 MB on Monaco, 46% of the serving volume.

**Publication is verified against identity now, not just against itself.**
`verify_whole_slice_once` derives what it expects from the same files it
publishes, and the reconciliation compares bindings out of the reduction JSON, so
a local store carrying wrong bytes under a right key published them and reported
`reconciles: true`. Finalize now compares every file to the digest in its
content-addressed key AND to the size/digest its producing phase recorded, before
anything is uploaded. Head shard entries carry `sha256`/`bytes` for that reason.

### The transport unblocks PLACES at planet scale. It does not unblock addresses.

Be precise about this, because two green slice jobs read as if both families were
done and they are not:

- **Places** is transport-clear end to end. Map writes to staging, plan is bounded
  and evicts, a reduce job owns a shuffle-bucket range and fetches only the
  fragments in it (~1 GB post-combiner), head reads bounded candidates, finalize
  reads exactly the published set.
- **Addresses** are transport-clear for map, plan, and finalize. Reduce is now
  bounded too, and the cost is object amplification rather than a partition that
  cannot fit a runner. Two earlier claims here were WRONG and are corrected below,
  because both were driving work in the wrong direction.

#### Correction 1: address map output IS hash-clustered. There is a shuffle-like sort.

This doc previously said "there is no shuffle to narrow it". That is false.
`pack_id` is `row_number() OVER (ORDER BY {TOTAL_ORDER})` integer-divided by
`max_pack_rows` (`scripts/address_construction_v1.py:1009-1010`), and `TOTAL_ORDER`
begins `country, maximum_bucket, ...`
(`scripts/spike_address_construction.py:77-82`), where `maximum_bucket` is the top
`MAXIMUM_HASH_BITS = 16` bits of `route_hash`
(`crates/geocoder-construction/src/main.rs:26,422`). So a map task's packs — and the
row groups inside them — are **contiguously clustered by `(country, route_hash>>48)`**.

Be precise about what that is and is not. It is an INTRA-TASK sort, not an
inter-task shuffle: each task orders only its own rows, so a partition's hash range
still appears in every task that holds rows for that country. The consequence is a
clean split of the cost:

- **Row-group SELECTION is already tight.** Each row group's `routing_groups`
  summary is narrow because its rows are hash-contiguous, so a partition reads few
  groups. Measured 1.55 of 52 groups per partition at 93 partitions (~3.25x byte
  amplification projected to planet); independently reproduced on a planet-shaped
  Seattle slice at 2.62 of 53 groups per partition over 32 partitions (4.9%).
  There is very little left to win here.
- **Whole-object HYDRATION was the entire problem.** `StagedObjectStore.path()`
  fetches a WHOLE pack to read one row group, so the amplification is
  objects-not-bytes: measured 24x on the shaped run, planet-projected 12.7x
  (~0.63 TB of R2 reads) at today's reduce batch size of 3.

#### Correction 2: the hazard was an unbounded store, not a partition that will not fit.

This doc previously said "at planet scale an address reduce partition would still
not fit a runner". That is not supported, but the *reason* matters and an earlier
draft of this section got it wrong in the other direction — see the law below.

The real hazard was that the hydrated store sat outside every declared cap.
`reduce_partition` ran its `StageWatchdog([workspace], limits, connection)` over the
temporary WORKSPACE only, not over `--store-root`, and nothing else bounded the local
cache. Combined with a reducer that never released anything, a job's disk use grew
monotonically with everything it had ever opened, which is exactly what
`reduce_staged_peak_resident_bytes == reduce_staged_bytes_hydrated` (10,886,477 for
both on the Seattle slice) was reporting.

**`release()` on the reduce path is what bounds it**, and it is now called: each pack
is evicted at its last use, except those the later partitions of the same reducer job
still need (`retain_keys`, computed by `construction_v1_hosted._batch_retention`).

#### The peak-resident LAW. Do not read the single-task table as the general case.

An earlier version of this section claimed peak was "flat at one pack independently
of batch size", from the table below. That table is measured on a **single map task**,
which makes its packs one GLOBAL sort, so a partition touches at most 2 of them
(measured: distinct packs per partition mean 1.41, max 2). The sort is **intra-task**,
so that is not the planet shape and the "one pack" figure does not generalise.

Re-measured with the same slice split across N round-robin map tasks, at fixed
batch 8 — peak scales with the **task count**, exactly:

| map tasks | distinct packs per partition (mean) | peak resident | peak, in largest packs |
| --- | --- | --- | --- |
| 1 | 1.41 | 887,478 | 1.00 |
| 2 | 2.38 | 1,788,821 | 2.00 |
| 4 | 4.38 | 3,598,884 | 4.00 |
| 8 | 8.25 | 7,220,808 | 7.99 |
| 16 | 16.00 | 11,955,459 | 15.99 (== the whole pack set) |

**The law: `peak ≈ (map tasks holding this partition's country) × pack bytes`, and it
is batch-INDEPENDENT for batch ≥ 2.** (At 8 tasks the peak is 7,220,808 at batch 4, 8
and 32 alike; only batch 1 falls to one pack, because a one-partition job retains
nothing — and it pays 264 hydrations instead of 16 to do so.) The mechanism: at any
moment a job holds one *hash slice* of packs, one per task, and drops it when the
range advances past it. The 16-task row is the degenerate end — each task emits a
single pack, so there is no slice to drop and peak equals the entire pack set.

Planet-sized from `benchmarks/address-construction-v1-data/inventory/addresses.json`
(`plan.task_count` 127) and 103.8 forward-pack bytes/row measured on the slice at
production `parquet_row_group_rows`:

- a 1,000,000-row pack (the `max_pack_rows` cap) is ~104 MB; all forward packs are
  ~49 GB over 473.6 M records;
- the US is the worst country, held by **39** tasks by `exact_country_rows` →
  **~4.05 GB** resident;
- 77 tasks carry mixed-or-unknown-country rows (39.2 M rows, ~509 k each → one
  sub-1M pack of ~53 MB). If every one of them is also selected, **~8.1 GB**.

So: **~127 packs worst case, single-digit GB, batch-independent** — which is what the
older "~4.2 GB, dominated by the number of TASKS holding a country" figure in this doc
was reaching for (the 39-task derivation lands within 4% of it). The `~5.3 GB at
batch_size 24` half of that pair was wrong in kind, not just in value: peak does not
grow with batch size above batch 1. Both are replaced by the derivation above.

Because that bound is emergent from a cache policy rather than enforced,
`reduce_partition` now also checks hydrated resident bytes against
`limits.max_scratch_bytes` after every fetch, and the hydrated cache is a
`StageWatchdog` root. Lowering `--max-reduce-jobs`, which this doc recommends for R2
reads, does NOT lower peak — so without that check an under-provisioned runner meets
the law as ENOSPC mid-reduce, on a plan the plan phase certified.

**Operators: `max_scratch_bytes` changed meaning for the address reduce stage.** It
governed the temporary WORKSPACE alone; it now governs workspace **+ the hydrated pack
cache**. Nothing else moved, and no default changed, but the same number is now being
asked to cover strictly more. It fits: at planet scale the cache is ~8.1 GB worst case
and the workspace adds ~1.1 GB, so ~9.2 GB against the address cap of 24 GiB
(25.77 GB) — 2.8x headroom. The two guards split the work by window: `check_resident`
runs on every fetch and is what covers the hydration peak, while the watchdog is
entered only around the export and therefore sees only the packs retained across it.

What the fix DOES buy, on the single-task table (kept because it is the before/after
on identical inputs — **single map task, not the general case**):

| reducer jobs | batch size | peak resident, before | peak resident, after | object amplification |
| --- | --- | --- | --- | --- |
| 32 | 1 | 1,774,192 | 887,478 | 3.36x |
| 8 | 4 | 2,660,313 | 887,478 | 1.53x |
| 4 | 8 | 3,653,171 | 887,478 | 1.23x |
| 1 | 32 | 11,622,309 (== total) | 887,478 | 1.00x |

Before the fix, peak grew with batch size until it reached the entire pack set. After,
it is the law above — one hash slice, independent of batch size. Object amplification
is unchanged by the fix and falls with batch size, which is why `--max-reduce-jobs` is
now a dispatch input on `construction-v1.yml` (planet: batch 3 = 12.7x / ~0.63 TB,
batch 12 = 4.2x / ~0.21 TB). Reduce output is byte-identical before and after, at
every batch size.

A green address slice still means "the transport moves address map output durably"
rather than "a planet address build has been executed" — no planet execute has run
for either family. But address reduce is no longer the blocker it was described as.

## The next increment, precisely

0. **A range-owning address reducer** — a port of
   `places_construction_v1._reduce_ingest` / `reduce_bucket_range`, where one job
   opens each pack once for a whole contiguous hash range and emits every partition
   in it. That is the load-bearing fix for the remaining object amplification; it is
   queued as separate work. **The address map shuffle will NOT be ported** —
   decided, see the DEFERRED section of the follow-ups doc for the reasoning. Until
   the range-owning reducer lands, `--max-reduce-jobs` on the dispatch is the lever:
   a lower job cap means larger batches means fewer pack reads. The forward
   partition key stays FROZEN either way.
1. Scoped **staging-only** R2 token for the map/reduce/plan/head jobs, leaving
   the broad key with finalize where the promotion discipline lives. This is now
   the only credential item between here and a planet dispatch.
2. **Row-group range reads** in the reducer, with the read-amplification gate the
   frozen evidence spec already declares (`selective_read_amplification_max` 4.0,
   enforced today only by the rehearsal validator). `path()` currently hydrates a
   WHOLE object; that is sufficient because the map-side shuffle makes a fragment
   hold a complete set of cells and nothing else, so a bucket-range reduce job
   already fetches only its own fragments. Range reads tighten it further and are
   step 2 of `2026-07-24-r2-staging-design.md` §7.
3. Estimate R2 Class B request volume before the first planet dispatch rather
   than discovering it in a bill.

## Numbers worth not re-deriving

Measured on release `2026-07-22.0` (planet Places), 2026-07-24/25:

| | |
|---|---|
| place records / term rows | 74,223,561 / 533,964,455 |
| term rows after the combiner | 286,494,538 (46.3% removed) |
| populated cells | 16,633 |
| largest single cell | **9,958,516 rows (Tokyo, `b2e3`)** |
| largest indivisible `(cell, token)` | 742,392 pre-combiner → **1,078** after |
| store, planet | 63.5 GB pre-combiner, ~34 GB after |
| max consumer input at 256 buckets | **~1 GB post-combiner** |

**The floor on any consumer is the largest single cell**, because a cell never
splits across buckets. More buckets do not help: 4096 buckets moves the max from
1,134 MB to 1,001 MB while multiplying objects from 22.5k to 360k. 256 is right.

If a cell ever outgrows a runner, the fix is shuffling by
`(cell, top token-hash nibble)` — that splits the cell 16 ways while keeping
each `(cell, token)` group intact, and it is already the subdivision scheme.

## Decisions with reasons, so they are not relitigated

- **Keep the 256x256 equirectangular `partition_cell`.** It is a level-8 quadkey
  in plate carrée. Cells vary 11x in ground area, which looks like a defect, but
  the largest cells are Tokyo, São Paulo, Osaka, Taipei, Mexico City — all
  mid-latitude, all full-size. **The skew is population density, not geometry**,
  so H3/S2/equal-area tiling would not help. Only adaptive subdivision does, and
  the scheme already has it. Changing the cell scheme is also client-visible:
  the serving key is `cell\0token` in routed mode.
- **The shuffle bucket must be a hash, never a spatial index.** H3/S2/quadkeys
  preserve locality, which is the opposite of what load balancing needs — dense
  neighbours would clump into one consumer.
- **Take the HIGH bits of the multiplicative hash.** `partition_key` is
  `(y << 8) | x`, so low bits depend only on `x` and every cell in a longitude
  column lands in one bucket — a pole-to-pole meridian strip per consumer. Cell
  *counts* stay perfectly uniform under that bug; only a data-weighted or
  one-axis-at-a-time test catches it. Both exist now.
- **The committed partition plan is probably scaffolding.** It, the headroom
  policy, and the fail-closed cap gate all exist to avoid a global planning
  barrier. A cell-keyed shuffle removes that barrier: a consumer holding a cell's
  complete data can decide subdivision locally. Expect to retire most of it.
  Bucket-range reduce deliberately did NOT retire it: the committed plan's
  partition count is still `predict-reduce`'s structural floor (PR #155), and the
  bucket-range prediction spreads that floored count uniformly over the bucket
  space — uniform being the right model, not a convenience, because the bucket is
  a multiplicative hash of the cell so per-bucket cell COUNTS are uniform by
  construction. Retiring the committed plan is a separate change with its own
  argument to make.

## Addresses

Not started, deliberately — Places first, proven. The map-side shuffle port that
used to be "then port" is now CANCELLED (2026-07-25); the address fix is reduce-side.
Three things HAVE been done for both families at once, because doing them per family
would have built the same machinery twice:

- **the R2 staging transport** (2026-07-25). It is transport, not routing: the
  address family's forward packs are unchanged, and every marker records the same
  keys it always did. Addresses are the bigger half of the build-time transport
  problem (473,576,753 records / 33.2 GB selected against Places' 74.2M / 10.6 GB),
  so excluding them would have left the wall standing for the larger family.
  **This is NOT the deferred address shuffle port** — `address_key_hash`,
  `route_hash`, `hash_bucket`, TOTAL_ORDER, SERVING_ORDER, the forward pack layout
  and the genesis partition plan are untouched, and the address forward partition
  key is FROZEN;
- **a bounded address reducer** (2026-07-25). `reduce_partition` releases each
  hydrated pack at its last use, retaining only what the later partitions of the same
  reducer job still need, and enforces resident bytes against `max_scratch_bytes`.
  Bounded means one hash slice — one pack per map task holding the partition's country,
  batch-independent — NOT one pack; see the peak-resident law above. Reduce-side only:
  no map change, no partition-key change, and reduce output is byte-identical;
- the per-record map artifact and its one publication seam, below.

- **The address map now emits a per-address, spatially keyed records artifact**
  (2026-07-25). `overture-address-map-address-records-v1`: one row per admitted
  address, `partition_cell`/`partition_key` derived from the E7 coordinates with
  the SAME 256x256 scheme and the SAME high-bits shuffle as Places, plus the
  display projection the structured forward endpoint returns. One pack per
  present shuffle bucket, a proof directory with per-row-group and per-cell
  counts, and a fail-closed equality against the admitted row count at map time
  and on resume. This is the one address thing that could not be added after the
  planet address map without re-running it
  (`docs/plans/2026-07-25-reverse-v2-design.md`). It is purely ADDITIVE: the
  forward packs are byte-identical, and it is not the deferred forward-shuffle
  port.
  - **Address map markers written before 2026-07-25 cannot resume.** Markers are
    write-once, so an older marker is intact and self-consistent but carries no
    records artifact, and both `validate_marker` and `finalize` fail closed on it
    by design — the alternative is one run silently mixing tasks that have the
    artifact with tasks that do not, which is the failure the artifact exists to
    prevent. Remediation is to delete that task's marker and re-run its map task;
    the forward packs are content-addressed, so the re-run republishes identical
    bytes and only adds the records packs. No planet address map has run, so today
    this affects local work directories only.
  - Both families now publish their map-phase per-record packs durably through
    **one** finalize seam (`PER_RECORD_ARTIFACTS` in `construction_v1_hosted.py`),
    under `families/places/positions/` and `families/addresses/records/`.
    Without that they would expire with the 7-day GitHub artifact retention, and
    a reverse index would cost the planet map re-run they exist to avoid.
- **There is now an address slice harness** (2026-07-25). `--family addresses` on
  both slice scripts runs 104,928 real Overture addresses (Seattle,
  `--bbox -122.34 47.59 -122.30 47.63`) through all five phases in ~9 s with no
  credentials. Use it for every address change, exactly as Places uses Monaco.
  The slice deliberately straddles the level-8 cell boundary at
  longitude -122.34375, so it covers cells `c328` and `c329` — two different
  shuffle buckets.

- **The key is sound; do not change it.** `address_key_hash` is FNV-1a over 8
  normalized fields, so `route_hash` is effectively unique per address. There is
  **no indivisible group**, unlike Places' `(cell, token)`. Subdivide to any
  depth and each split halves the load. `hash_bucket` already takes high bits.
- **The shuffle is worth porting** and is easier: the natural key is
  `(country, top-K bits of route_hash)` — the partition key at fixed
  granularity, already uniform, no hash-of-a-hash needed.
- Addresses are the **bigger BUILD-TIME** transport problem: 473,576,753
  records / 33.2 GB selected, against Places' 74.2M / 10.6 GB. This is about
  moving bytes between build phases and says nothing about query latency --
  see "Query surface" below before inferring anything about lookups.
- 8,023 of 8,704 row groups are already single-country, so the source is
  largely country-clustered already.
- **Measure country skew first** to choose K — the inventory records no
  per-country row counts, so it is currently unknown.
- `MEASURED_REDUCE_MINUTES_PER_PARTITION["addresses"] = 2.0` is genuinely
  uncalibrated and the code says so. The Places 1.0 **is** calibrated.

## Query surface, and what construction-v1 does NOT build

Reverse design now exists: `2026-07-25-reverse-v2-design.md`.

Everything above is about **build time**. It is easy to read the partition-key
discussion as a statement about lookup performance. It is not. Keep these
separate.

**Live today (v1, divisions only).** Measured 2026-07-25 from a dev machine, so
these include RTT to the edge:

| endpoint | cold | warm p50 |
|---|---|---|
| `/search` | 434 ms | **47 ms** |
| `/reverse` | 39 ms | **42 ms** |

The documented gate is a warm median at or under 250 ms per route class
(`docs/api-v2.md`). Both are inside it.

**v2 is unpublished.** Every v2 endpoint returns
`503 release_unavailable`, because construction-v1 has never completed a run.

| family | forward | reverse |
|---|---|---|
| divisions | `/v2/forward` text mode | `/v2/reverse` -- the ONLY reverse that exists |
| places / POI | `/v2/forward` text; no proximity -> packed global head, 1-2 exact tokens; with proximity -> exactly ONE quadkey shard, up to 4 tokens | **rejected by the API** |
| addresses | `/v2/forward` **structured exact only** (`country`+`street`+`number`). Free-text address search is deliberately unadvertised; `types=address` with `q` returns unsupported-capability | **rejected by the API** |

### construction-v1 builds FORWARD indexes only

`docs/api-v2.md` states it directly: *"Reverse currently serves divisions only.
`poi` and `address` are rejected until their spatial reverse indexes exist."*

- The Places serving key is `cell\0token` in routed mode -- forward.
- The address serving index is keyed by `route_hash` and ordered by
  route/key/source -- exact forward lookup.
- **Neither can serve a reverse query at any key.** The address key being
  non-spatial is therefore irrelevant to reverse; a reverse index would be a
  separate spatial structure, as the divisions reverse shards already are
  (`build_shards.py --reverse`).

**This is a deliberate, documented scoping decision, not an oversight.** An
earlier draft of this section claimed it was recorded nowhere; that was wrong.
Three contract docs say it:

- `docs/address-structured-endpoint-contract.md`: *"This exact-key view is not
  the future general-search layout. Free-form street forward search and
  nearest-address reverse search should be separate secondary indexes (likely
  country/postcode/locality and **spatial ownership** respectively) behind the
  same v2 API. Forcing all three access patterns into this hash layout would
  make each of them worse."*
- `docs/global-v2-build-readiness.md`: *"The first address capability is exact
  structured forward lookup. Free-text address forward and address reverse
  require separate secondary indexes later."*
- `docs/v2-release-catalog-contract.md`: the address family advertises only
  `structured_forward`; *"General address forward/reverse can be enabled only by
  a later build whose artifacts and Worker support those operations."*

So the capability machinery is already built for this: the catalog advertises
per-operation, and the API returns a clean unsupported/503 rather than
pretending. What is missing is the secondary index itself -- no design, no
build, and it is not on the current path.

**It is cheaper than it sounds, and today's work helps rather than hinders:**

- Addresses already carry coordinates through the map phase
  (`crates/geocoder-construction/src/main.rs`, the `address-transform-v1`
  binary, references longitude/latitude throughout). The input data for a
  spatial index is already flowing; nothing new needs extracting.
- Places already has the spatial machinery -- `partition_cell` plus the
  cell-keyed shuffle. An address reverse index wants **the same spatial
  partitioning**, so the shuffle built for Places is directly reusable.
- The realistic shape is a SECOND serving index off the same map output, keyed
  spatially instead of by `route_hash` -- not a second pipeline.

The remaining real work is a nearest-neighbour structure within a cell and
Worker support, which the divisions reverse shards
(`build_shards.py --reverse`) already demonstrate at smaller scale.

### DECIDED 2026-07-25: map emits a per-place positions artifact

**It is emitted.** Map writes per-place positions packs alongside the term
fragments, so a spatial reverse index can be built later without re-running the
planet map phase. The reasoning below is kept because it is the reason, not
because the question is open.

What it is, precisely:

- one row per admitted **place RECORD**, not per distinct place. The key is
  `feature_id` PLUS the source locator (`source_object_index`,
  `source_row_group`, `source_row_index`) -- the same identity the serving path
  uses. **Do not group by `feature_id`.** The frozen evidence spec requires every
  copy of a repeated id to survive as a distinct candidate keyed by its
  provenance (`tests/test_places_duplicate_uuid_gate.py`), so collapsing by id
  would violate the contract AND abort a planet map job on data the contract
  declares valid. Consumers that want one row per place dedupe by their own
  policy; the artifact does not choose one for them;
- **self-sufficient**: position plus `primary_name`, `brand_name`, `category`,
  `locality`, `region`, `country`, `confidence_rank`. Forced by the reverse-v2
  design -- the ID index returns no names and `/v2/features/:gers_id` is being
  removed, so a positions-only row cannot render a reverse hit at all;
- derived **pre-combiner**, by a projection with a `DISTINCT` that removes only
  the per-token fan-out, which is the entire point (see below). No aggregation,
  so no coordinate or name is ever synthesized across rows;
- bucketed by the **same shuffle** as the term packs, one pack per present
  bucket, ordered `(shuffle_bucket, partition_cell, feature_id, locator)` --
  total, hence deterministic;
- named `map/places-v1/positions` with `map/places-v1/position-directories`
  beside it, distinct from the term `packs`/`directories` so nothing can confuse
  the two;
- **published by finalize** under `families/places/positions/`, listed in the
  family and slice manifests and covered by the single whole-slice verification.
  This is not optional: the store otherwise travels only as a GitHub artifact
  with `retention-days: 7`, so an unpublished artifact expires a week after a
  planet run and reverse costs the map re-run this whole thing exists to avoid.
  Nor is it optional by omission -- `finalize --markers-dir` is REQUIRED for every
  family in `POSITIONS_FAMILIES` (places today), and a marker set where any task
  carries no positions is rejected, so a wiring mistake cannot quietly ship a
  slice without per-place records;
- fails closed if the row count is not exactly `admitted_features`, if a cell
  lands in the wrong bucket, or if a resumed marker's positions artifact is
  absent or does not reconcile. The slice-smoke CI job asserts
  `positions_objects > 0` and `positions_records > 0` from the harness summary,
  so a run that emits but fails to publish it is a red build.

Measured on the Monaco slice (38,182 projected, 38,172 admitted): **38,172 rows
in 4 packs, 1,718,410 bytes** -- 45.0 compressed bytes per record (63.5
uncompressed), against 16.4 MB of term packs for the same task, so **10.5%** of
the term packs. Straight-line planet projection is ~3.3 GB compressed / ~4.7 GB
uncompressed; treat ~6.7 GB as the conservative bound, since a 38k-record slice
repeats names far more than the planet does and therefore compresses better.
Finalize published 8 objects (4 packs + 4 directories), 1.72 MB, verified.

The positions packs get a content hash and a row-group directory (per-row-group
and per-cell record counts, plus the assertion that every cell in a pack hashes
to that pack's bucket). They deliberately do **not** get the term packs' exact
two-lane binding: that binding is computed from `semantic_digest_a/_b` on TERM
rows, and a positions row carries no such digest. Synthesizing one would produce
a proof frame that looks exact and binds nothing that exists.

**Resume note:** a `RESUME_FROM` of a run that predates this artifact aborts its
map jobs by design -- an admitted marker with no positions is rejected rather
than resumed, so one run can never mix tasks that have positions with tasks that
do not. The error names the marker key to delete for a re-map.

Nothing downstream of map CONSUMES it yet, and the boundary is structural: the
positions packs live under `marker["positions"]["packs"]` and nowhere else, while
`bucket_range_fragments` (the reducer's term-fragment selector), head and the
genesis plan read `marker["packs"]`. The two key spaces are disjoint --
`map/places-v1/positions/` versus `map/places-v1/packs/` -- so a positions pack
cannot be mistaken for a term fragment. **Keep it that way**: putting positions
into the top-level `packs` list would make the reducer ingest them as term rows.
Only finalize touches them, to publish them.

The sequencing argument that produced the decision:

This was the only part of reverse that is expensive to add late, so it was a
sequencing decision rather than a design one.

Reverse must be built from **per-place records** (one row per place with a
position), not from term rows:

- term rows are ~7.19 per place -- wrong shape and 7x the volume; and
- the combiner keeps only the top `maximum_serving_candidates` per
  `(cell, token)`, so a place whose tokens are all generic and all in saturated
  groups can be dropped from the term store entirely. Harmless for forward
  search, **silently missing from reverse**.

Term rows and head candidates (top-N per token) are both the wrong shape, and
nothing else in map enumerated places. So adding reverse without this artifact
would have meant **re-running the planet map phase** -- not redesigning it, but
re-running it, plus a re-publish.

**The insurance is cheap.** The minimal form was estimated at ~32 bytes and
~2.4 GB planet-wide; the self-sufficient form actually built measures 45.0
compressed bytes per record, so a few GB against a ~34 GB term store. It rides
the same shuffle and the same staging.

- Emit it -> reverse is purely additive later. New encoder, new index, no map
  re-run.
- Skip it -> the price of reverse is one full planet map re-run.

**Everything else about reverse is additive.** Inventory, projection, the
transform, contract/admission/ledger, the proof frame, the cell scheme, the
cell-keyed shuffle, and R2 staging all carry over unchanged -- reverse wants
exactly the spatial grouping the shuffle already produces.

### Reverse for Places vs addresses is NOT symmetric

Both need a new index, but they start from very different places, and the two
should almost certainly be designed together rather than separately.

First, a distinction that is easy to miss: **divisions reverse is containment**
(which admin polygon contains this point) and is already served by
`build_shards.py --reverse`. **POI and address reverse are nearest-neighbour**
over points. The existing reverse shards are not a template you can reuse
directly -- it is a different query.

**Places has a substantial head start.**

- Coarse spatial routing already exists. The serving layout is keyed
  `cell\0token` and routes to exactly one world-quadkey shard, so given a
  lat/lon you compute the cell with `route()` and know which shard to open. No
  new partitioning needed.
- `places_serving_encode_v1.rs` already reads `longitude`/`latitude` into the
  serving payload, so positions are present in the served records.
- What is missing is only a WITHIN-cell spatial structure: records inside a
  shard are ordered by token, not by position.

**Addresses start further back.** `(country, route_hash)` is not spatial at all,
so given a lat/lon there is no way to know which shard to open. The coordinates
do flow through `address-transform-v1`, but the partitioning has to be built.
The Places cell scheme plus the cell-keyed shuffle is what it should reuse.

**Both hit the same wall in dense cells.** Mean is 4,462 places per cell, which
a linear within-cell scan handles trivially. The tail does not:

| cell | places (approx) |
|---|---|
| `b2e3` Tokyo | **~1,384,000** |
| `5e5e` São Paulo | ~816,000 |
| `b1e0` Osaka | ~738,000 |

So a sub-cell spatial subdivision is required for the dense tail. Note the
existing subdivision is by **token hash**, which is useless for this -- it
deliberately scatters neighbours. A spatial sub-key (a finer quadkey inside the
cell) would be needed.

**Conclusion: design one spatial reverse index that serves both families.** Same
cell scheme, same sub-cell spatial structure, two datasets. Doing them
separately would build the same dense-cell machinery twice.

### Expected forward performance (projection, not measurement)

Nothing below is built, so this is reasoning from the read pattern only:

| query | reads | expectation |
|---|---|---|
| POI forward with proximity | exactly 1 quadkey shard | same shape as today's `/search`; warm tens of ms |
| POI forward without proximity | packed global head only | fastest path, small and cacheable |
| address structured exact | hash -> 1 partition, range read | point lookup, cheapest of the three |

All three are bounded single-shard reads *by contract*, so warm latency is a low
risk. The real risk is **cold-shard size** -- a first touch is the 434 ms case
above, and the densest cell (Tokyo) is the largest shard in the system.

### `/v2/features/:gers_id` is slated for removal

Owner decision, 2026-07-25. Do not build on it, do not extend it, and exclude it
from capability analyses. Removing it is its own change: it touches the worker,
`docs/api-v2.md`, and tests.

## Open follow-ups

See `2026-07-24-construction-v1-follow-ups.md`. The one that can still bite:

1. Three evidence-spec `*_hard_cap` values are dead declarations the build now
   exceeds. Enforce them or delete them.

(The reduce `StageWatchdog` gap is closed — see "What has landed".)
