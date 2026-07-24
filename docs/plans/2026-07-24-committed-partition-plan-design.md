# Committed partition plan: design

Date: 2026-07-24. Status: DRAFT for owner review. No code written yet.

The Places reducer plan is currently derived every run by reading every map
task's term rows. This proposes committing the partition tree to the repo
instead, generating it offline when it goes stale, and letting the hosted build
assign partitions locally from it.

## Why this is more than a cache

The partition function is **local**: `(partition_cell, token_hash) -> partition`.
The cell comes from the feature's location and the prefix from the token hash.
Given the tree, a map task can assign every row to its final partition with no
global knowledge of the other tasks.

That removes the barrier. The pipeline stops being

    map -> plan (needs ALL map output) -> reduce (needs ALL map output)

and becomes

    map (partition-aware) -> reduce (needs only its own partitions)

Three consequences, in descending order of value:

1. **Reduce reads its own slice.** Today `cv1-plan` is a 63 GB artifact and all
   128 reduce jobs download it — roughly 8 TB of transfer for one family.
   Partition-keyed map output means a reduce job reads only what it owns.

   This is no longer hypothetical. In run `30113308268`, `places reduce batch 2`
   spent 12 minutes downloading `cv1-plan` and then failed on the first line of
   its run step — the `df -Pk / >= 30000000` guard — because 63 GB of artifact
   leaves under 30 GB free. It failed closed and diagnosably, which is the guard
   working, but **reduce cannot proceed at all in the current design.**
2. **The plan phase's fan-in disappears.** Today it reads 554,814,222 term rows
   to compute three aggregates per cell.
3. **Reduce can start before every map task finishes**, since it no longer waits
   on a global planning barrier.

## Measured baseline (2026-07-24 planet run, places)

From the plan job of run `30113308268`:

- **554,814,222** term rows
- **17,816** partitions, **128** reduce jobs, batch size 140
- projected 18,248 runner-minutes against the 40,000 cap

Current caps (`places_construction_v1.Limits`):

| cap | value |
|---|---|
| `partition_term_rows` | 1,000,000 |
| `partition_estimated_bytes` | 268,435,456 (256 MiB) |
| `partition_distinct_tokens` | 250,000 |
| `adaptive_subdivision_depth` | 8 |

Average partition holds **31,141 rows — 3.1% of the row cap**. Partition count
is driven by the number of populated cells, not by subdivision; only the dense
cells subdivide at all.

## The headroom budget is real and small

`MEASURED_REDUCE_MINUTES_PER_PARTITION` is **flat**: 1.0 min/partition for
places, 2.0 for addresses. Projected reduce minutes are therefore
`partitions x 1.0`, so **partition count is budget**.

Subdividing one partition one level adds 15 partitions and 15 projected minutes.

- Remaining budget: `40,000 - 18,248 = 21,752` minutes
- **Affordable pre-splits: about 1,450 partitions**, one level each

That is enough for meaningful headroom but nowhere near uniform. Subdividing
*everything* one extra level would give `17,816 x 16 = 285,056` partitions and
285,056 projected minutes — **7x over the ledger cap**. So:

> Buffer must be threshold-driven — subdivide only cells within X% of a cap —
> never uniform.

A reasonable starting policy: pre-split any partition above **50%** of any cap,
then report the resulting count and projected minutes, and let the operator
choose the threshold with the numbers in hand rather than guessing.

**Related finding:** the flat cost model is itself the binding constraint, not
real compute. A 31k-row partition does not take a minute. Making
`MEASURED_REDUCE_MINUTES_PER_PARTITION` row-aware (e.g. a floor plus a
per-million-rows term) would free most of the headroom budget and is worth doing
independently — it is what makes generous buffering affordable.

## Design

### 1. The committed artifact

`data/places_partition_plan_v1.json` (or similar), pinned by SHA into the
request identity exactly as the inventory and evidence spec already are.

Contents:

- `schema`, `plan_version`, `generated_from` (Overture release + inventory SHA)
- `partition_contract`: scheme, minimum/maximum level, the caps the plan was
  generated against
- `splits`: only the branches where subdivision happened — a cell with no entry
  is depth 0. Most cells are depth 0, so this is far smaller than enumerating
  all 17,816 partitions. Expect a few hundred KB, and it diffs readably.
- `provenance`: measured rows/bytes/distinct-tokens per split branch at
  generation time, plus the headroom threshold used

Store measured stats as provenance, not as truth. The build must never trust
them — see the gate below.

### 2. The offline generator

One script, runnable on a single large machine:

    python scripts/generate_places_partition_plan.py \
      --release 2026-06-17.0 --inventory ... --caps ... \
      --headroom-fraction 0.5 --output data/places_partition_plan_v1.json

It does what `adaptive_genesis_plan` does today (DuckDB aggregation over the
term universe), plus the headroom pre-split, and reports:

- resulting partition count and projected reduce minutes vs the ledger cap
- how many partitions were pre-split for headroom vs required by caps
- the worst-case partition against each cap, so drift is visible

Running it locally is the point: it needs the whole term universe once, which is
exactly what we do not want the hosted build doing every month.

### 3. Map-side assignment

Map tasks read the committed plan, assign each term row to its partition
locally, and write partition-keyed output. No change to tokenization, admission,
or digest semantics — this is purely where rows land.

### 4. The fail-closed gate (non-negotiable)

A committed plan can go stale: Tokyo grows, a new dense cell appears. A
partition silently exceeding its caps breaks the serving-side bounds
downstream, which is the failure mode this whole system exists to prevent.

Because map already assigns partitions, it can emit **per-partition counts**
almost free. Fanning in counts is a few MB, not 63 GB. A cheap check then
asserts every partition is under every cap:

- under cap: proceed to reduce
- **over cap: fail closed**, naming the offending partitions and their measured
  values — which is exactly the input the generator needs for the next version

So aggregation cost becomes proportional to the **plan**, not the **data**, and
the fail-closed property is preserved rather than traded away.

### 5. Version bump loop

Failure names the partitions. Operator re-runs the generator on a big machine,
commits a new plan version, re-dispatches. With sensible headroom this should
happen on the order of months, not monthly.

## What to keep from the deleted implementation

The retired executor implemented this concept
(`git show 3f9a8e9^:scripts/global_v2_places_plan.py`, `_load_predecessor_splits`
at line 1297). Worth reusing:

- **Exact-identity pinning.** The predecessor was verified by canonical bytes
  *and* SHA-256, and rejected if either differed. Apply the same to the
  committed plan file.
- **Contract compatibility validation.** It rejected a predecessor whose
  `scheme`, `minimum_level`, `maximum_level`, or row cap disagreed with the
  request. A committed plan generated under different caps must be rejected the
  same way, not silently reused.
- **Split-never-merge.** A cell split in an earlier generation stays split.
  Matches one-way-doors section 5.
- **Fail-closed on every mismatch**, including a bootstrap that supplies a
  predecessor and a later generation that omits one.

What **not** to reuse: the old design sourced the predecessor from a *published*
R2 catalog (`catalog.pcat`) plus a family manifest, chained by
`lineage_generation`. That couples planning to publication — generation N needs
a published N-1 — so the first planet build could never benefit, and reviewing a
partition change meant reading a bucket. A committed file is better here:
decoupled from publication, and partition changes show up in a PR diff. That
matters because one-way-doors section 5 requires locking the partition scheme
before genesis, and a diff is a better review surface than an object store.

Keep `lineage_generation` as a monotonic field in the committed file for
sticky-split lineage; drop the published-predecessor plumbing.

## Interaction with R2 staging

Complementary, and this should come first. R2 staging (the ID-index pattern)
fixes *where* intermediate data lives; the committed plan fixes *how much of it
each reducer must read*. Staging alone still has every reducer scanning the
whole term set to find its partitions. Together: map writes partition-keyed
fragments to R2 staging, reduce reads only its own prefixes.

The map-side rollup proposed earlier partly dissolves into this — the
per-partition counts from the gate serve both purposes.

## Risks and open questions

- **Sticky splits only grow.** Over years the tree accumulates depth it may no
  longer need. Already the declared contract, so a known cost — but with
  headroom pre-splitting it grows faster. Worth a periodic regeneration from
  scratch rather than pure inheritance.
- **Distinct-token counting across tasks.** `count(DISTINCT token)` is not
  additive across map tasks. Keying the per-partition counts by
  `(partition, token_hash)` recovers it exactly, but that is a larger rollup
  than plain counts, and it makes the gate use hash-distinct rather than
  string-distinct. Consistent with a hash-prefix partition scheme, but it is a
  semantic change to a cap and should be decided explicitly.
- **New cells absent from the plan.** Needs a defined default (depth 0) plus the
  gate to catch it if that default is over cap.
- **Plan SHA in the request** makes every plan bump a new request identity.
  Correct, but it means a plan bump cannot be hot-fixed into a resumed run.

## Sequencing

1. Make the reduce cost model row-aware. Small, independent, and it sets the
   real headroom budget. Without it, buffering is capped at ~1,450 pre-splits.
2. Harvest plan v1 from the run that just computed it. Run `30113308268` already
   produced the real 17,816-partition tree; it is inside the 63 GB `cv1-plan`
   artifact, so extracting just `plan/plan.json` needs some care but avoids a
   dedicated planet pass.
3. Build the generator, reproduce v1 from it, and diff against the harvested
   tree. Reproducing a known-good answer is the correctness proof.
4. Map-side assignment plus the fail-closed gate.
5. R2 staging for partition-keyed fragments; reduce reads selectively.

Steps 1-3 are independent of the in-flight run and can start now.
