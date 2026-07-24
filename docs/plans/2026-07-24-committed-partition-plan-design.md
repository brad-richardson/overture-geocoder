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

Effective caps, read from the harvested plan's own `limits` (these are the
**contract** values, which differ from the `places_construction_v1.Limits`
dataclass defaults — the defaults say 256 MiB / 250,000 tokens):

| cap | value |
|---|---|
| `term_rows` | 1,000,000 |
| `estimated_uncompressed_bytes` | 536,870,912 (512 MiB) |
| `distinct_tokens` | 200,000 |
| `maximum_depth` | 8 |

Measured distribution over the harvested plan:

| statistic | value |
|---|---|
| depth histogram | **16,428** at depth 0, **1,324** at depth 1, **64** at depth 2 |
| deepest subdivision | **2**, against a cap of 8 |
| populated cells | 16,511, of which only **83** subdivide at all |
| peak utilisation of any cap | **0.998** (one partition at 998,196 rows) |
| p99 / p50 utilisation | 0.497 / **0.0007** |
| mean partition | 31,141 rows — 3.1% of the row cap |

The distribution is extremely skewed: half the partitions sit at under a
thousandth of a cap while one is at 99.8%. Partition count is driven by the
number of populated cells, not by subdivision.

## The headroom budget, measured

`MEASURED_REDUCE_MINUTES_PER_PARTITION` is **flat**: 1.0 min/partition for
places, 2.0 for addresses. Projected reduce minutes are therefore
`partitions x 1.0`, so **partition count is budget**.

Subdividing one partition one level adds 15 partitions and 15 projected minutes.
Remaining budget is `40,000 - 18,248 = 21,752` minutes.

Measured cost of a threshold policy over the harvested plan — pre-split every
partition above the given fraction of any cap:

| threshold | splits | added partitions/minutes | total partitions | fits budget |
|---|---|---|---|---|
| >90% | 16 | +240 | 18,056 | yes |
| >75% | 53 | +795 | 18,611 | yes |
| **>50%** | **175** | **+2,625** | **20,441** | **yes** |
| >35% | 324 | +4,860 | 22,676 | yes |
| >25% | 535 | +8,025 | 25,841 | yes |
| >10% | 1,589 | +23,835 | 41,651 | **no** |

Because the prefix is a hash, a pre-split partition's children each hold roughly
`u/16` of its load. So a >50% policy costs **+15% partition count** and buys
16x headroom on the 175 hot partitions, while everything left untouched is
already below half a cap and must double before it breaks. That is years of
growth, not months, for a very cheap price.

Uniform subdivision remains impossible: `17,816 x 16 = 285,056` partitions is
**7x over the ledger cap**. So the rule still holds —

> Buffer must be threshold-driven, never uniform.

— but the affordable thresholds are far more generous than the flat cost model
suggested before measuring. **Recommend >50%, with >35% comfortably affordable
if more margin is wanted.**

**Superseded finding:** an earlier draft made a row-aware reduce cost model a
prerequisite, on the assumption that headroom was capped near 1,450 splits. The
measured distribution dissolves that: >50% needs only 175 splits. A row-aware
cost model is still worth doing — a 31k-row partition does not take a minute —
but it is now an independent efficiency item, not a blocker, and it cannot be
calibrated until a reduce phase actually completes.

## Design

### 1. The committed artifact

`scripts/places_partition_plan_v1.json`, pinned by SHA into the request identity
exactly as the inventory and evidence spec already are. **Seeded in this branch
from the harvested plan.**

Only the branches where subdivision happened are stored — a cell absent from the
tree is depth 0. That is what makes this cheap:

| | |
|---|---|
| cells in the tree | **83** (of 16,511 populated) |
| leaf branches | 1,388 |
| file size | **10,154 bytes**, 93 lines, one cell per line |
| partitions reconstructed | **17,816 — exactly matches the derived plan** |

The whole planet partition tree is ten kilobytes and diffs cleanly. Contents:

- `schema`, `plan_version`, `generated_from` (Overture release, source run,
  term rows, partition count)
- `partition_contract`: scheme kind and the caps the plan was generated against
- `headroom`: policy and threshold used (the seed used none)
- `cells`: `cell -> ["depth:prefixhex", ...]`

Measured stats stay provenance, never truth. The build must not trust them —
see the gate below.

### 2. The offline generator

One script, runnable on a single large machine:

    python scripts/generate_places_partition_plan.py \
      --release 2026-06-17.0 --inventory ... --caps ... \
      --headroom-fraction 0.5 --output scripts/places_partition_plan_v1.json

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

1. ~~Row-aware reduce cost model.~~ **Demoted** — the measured distribution shows
   generous thresholds already fit the budget. Independent efficiency item, and
   uncalibratable until a reduce phase completes.
2. **Harvest plan v1. Done.** Extracted `plan/plan.json` (9,906,835 bytes) from
   the 63 GB `cv1-plan` artifact by reading its ZIP64 central directory over HTTP
   range requests and inflating one member — about 2 MB transferred instead of
   63 GB (`scratchpad/harvest.py`). Reduced to the 10 KB committed tree above.
3. **Build the generator and reproduce v1 from it. Done** —
   `scripts/generate_places_partition_plan.py`, with `--check` verifying the
   committed plan byte for byte. The committed v1 is the generator's own output.

   Note what that proof does and does not cover. `cells` records only
   subdivided branches (1,388 of 17,816 partitions, 7.8%); the other 16,428 are
   implicit depth-0 partitions determined by the *populated-cell set*, which
   lives in the data. A generator that dropped or invented depth-0 cells would
   leave `cells` untouched. The plan therefore records `populated_cells`
   alongside `partitions`, so a byte-for-byte `--check` pins the whole partition
   set via the identity `populated_cells - subdivided_cells + branches =
   partitions`.
4. Map-side assignment plus the fail-closed gate.
5. R2 staging for partition-keyed fragments; reduce reads selectively.

Step 3 can start now and needs a machine that can hold the term universe —
which is the point of the generator being an offline script.

## Harvest note

The extraction technique is reusable and worth keeping: GitHub artifact blobs
honour HTTP Range, so any single file can be pulled from a huge artifact by
reading the ZIP64 end-of-central-directory locator from the tail, then the
central directory, then just that member's compressed extent. For this artifact
the central directory was 178,259 bytes covering 1,092 entries, and
`plan/plan.json` happened to be the first entry at offset 0.
