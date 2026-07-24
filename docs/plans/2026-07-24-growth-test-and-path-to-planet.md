# Growth test against 2026-07-22.0, and the path back to a planet attempt

Date: 2026-07-24. Status: measured findings, owner review requested on §4.

The committed Places partition plan (`scripts/places_partition_plan_v1.json`)
was derived from Overture release `2026-06-17.0`. Release `2026-07-22.0` landed
five weeks later. This runs the whole Places map phase locally against the new
release and asks the question the committed plan exists to answer: **does the
tree we already committed still hold every partition under every cap?**

It does not. And the reason it fails is not the reason we expected.

## 1. What was run

The full hosted map data plane, locally, on a 20-core / 62 GB machine:
projection from S3 (`project_places_construction_v1.py`) → hydrate →
`places-transform-v1` → term rows, for all 88 inventory tasks. This is the real
tokenizer and the real projection, not a model of them. Only the pack/proof/
marker contract was skipped, since the growth question needs the term
distribution and nothing else.

Wall clock ~50 minutes at 6-way parallelism. This is exactly the offline
regeneration workload the generator was designed around, and it is cheap.

| | 2026-06-17.0 (committed) | 2026-07-22.0 | change |
|---|---|---|---|
| place records | 75,642,289 | 74,223,561 | **-1.9%** |
| term rows | 554,814,222 | 533,964,455 | **-3.8%** |
| populated cells | 16,511 | 16,633 | +122 |
| map tasks | 89 | 88 | -1 |

Note the direction: the planet **shrank**. Any failure here is redistribution,
not growth.

## 2. The gate fails closed — by 0.8%

Expanding the committed tree over the new data (a proper prefix tree, verified
non-overlapping, and row-conserving over all 533,964,455 rows):

```
v1 tree over new data: 17,938 partitions
GATE: 1 partition over cap -> FAIL CLOSED
worst utilisation 1.008
```

The single breach is cell `a1d5`, and it breaches on **distinct tokens**, not
rows:

| | v1 | new |
|---|---|---|
| rows | 888,752 | 901,502 (90.2% of cap) |
| bytes | 145,207,492 | 146,518,560 (27.3% of cap) |
| distinct tokens | 198,216 | **201,568 (100.8% of cap)** |
| utilisation | 0.9911 | **1.0078** |

It was at 99.1% of a cap when we committed the plan. A 1.7% drift pushed it
over. Nothing anomalous happened — this is exactly the failure the fail-closed
gate is designed to catch, and it caught it.

The 432 brand-new cells are all negligible (worst utilisation 0.000), so the
depth-0 default for unknown cells is safe. 310 v1 partitions became empty.

## 3. Headroom works — validated against real drift

Applying each headroom policy to the **v1** measurements and then evaluating the
resulting tree against the **new** data:

| policy | partitions | projected reduce min | breaches | worst utilisation |
|---|---|---|---|---|
| none (what we committed) | 17,938 | 17,938 | **1** | 1.008 |
| **>90%** | 18,178 | 18,178 | **0** | 0.890 |
| >75% | 18,733 | 18,733 | 0 | 0.772 |
| >50% | 20,563 | 20,563 | 0 | 0.745 |
| >35% | 22,798 | 22,798 | 0 | 0.745 |

Even the cheapest policy (+240 partitions, +1.3%) would have absorbed five weeks
of real drift. The design doc's recommended >50% is comfortably sufficient.

But note where the table stops improving: **>50% and >35% both floor at 0.745.**
Spending 2,235 extra partitions buys exactly zero additional margin. That floor
is the actual finding.

## 4. The floor: a single (cell, token) pair is indivisible

The worst partition under the >50% policy is `b2e3` at depth 3, prefix `855`:
744,628 rows, of which **742,392 (99.7%) are the single token `'jp'`**.

The partition prefix is `token_hash >> (64 - depth*4)`. One token has one hash.
**No subdivision at any depth can ever split a single token's rows.** The
largest single (cell, token) pair is therefore a hard floor on partition size,
and neither the headroom policy nor `maximum_depth` can move it.

Measured planet-wide on the new release:

| cell | token | rows | % of the 1,000,000 row cap |
|---|---|---|---|
| b2e3 | `jp` | 742,392 | **74.2%** |
| 5e5e | `br` | 735,911 | 73.6% |
| 5e5e | `sp` | 731,873 | 73.2% |
| 93c7 | `th` | 644,649 | 64.5% |
| c97f | `gb` | 564,983 | 56.5% |
| 9b39 | `mx` | 541,181 | 54.1% |

Indivisible pairs already over the cap: **0**. Over half the cap: **6**.

So the scheme still works today, but the irreducible margin is **1.35x**, and it
shrinks as Overture grows. If any single (cell, token) pair ever exceeds
1,000,000 rows, `adaptive_genesis_plan` raises *"remains over cap at maximum
depth"* and no plan can be generated at all — offline or otherwise.

This corrects a claim in the predecessor design doc. It said *"Because the
prefix is a hash, a pre-split partition's children each hold roughly `u/16` of
its load"*, and concluded headroom buys *"years of growth, not months"*. That is
true for the **distinct-token** cap and for the general row distribution. It is
**false for the row cap in token-skewed cells**, which is where the real ceiling
is.

### Why the obvious mitigation does not work

These are administrative-name tokens. Dropping country codes from the term
index (13.9% of all 533M term rows) barely helps, because the next offender is a
region name:

| after excluding `token = lower(country)` | rows |
|---|---|
| 5e5e `sp` (São Paulo) | 731,873 — 73.2% of cap |
| 5e5e `sao` | 488,323 |
| 5e5e `paulo` | 448,225 |

The floor moves from 74.2% to 73.2%. Not a fix.

### The leading candidate: the row cap is over-tight

Across all 17,938 measured partitions, the three caps are used very unevenly:

| cap | value | worst observed | utilisation |
|---|---|---|---|
| `term_rows` | 1,000,000 | 990,791 | **99.1%** |
| `distinct_tokens` | 200,000 | 201,568 | **100.8%** |
| `estimated_uncompressed_bytes` | 536,870,912 | 173,888,811 | **32.4%** |

The byte cap is nowhere near binding. At the ~160 bytes/row observed in the
largest partitions, 512 MiB would permit **~3.3M rows**. Raising `term_rows`
toward 2,000,000 would keep every partition inside the existing byte cap, give
the indivisible floor **2.7x** margin instead of 1.35x, and *reduce* partition
count — which also relieves the runner-minute budget.

**This needs owner review and cannot be settled from these measurements alone.**
The row cap plausibly also bounds reducer memory and wall time, not just output
bytes, and `MEASURED_REDUCE_MINUTES_PER_PARTITION` is still uncalibrated because
no reduce phase has ever completed. The alternative — adding a within-token
split dimension (e.g. by `feature_id` hash) — is structurally correct but
changes the partition scheme, which one-way-doors §5 requires locking before
genesis.

**Either way this must be decided before the first planet build, not after.**

## 5. The transport wall is worse than recorded

Separately, measuring the artifacts from run `30113308268`:

```
cv1-map-N   89 artifacts   63.5 GB total
cv1-plan     1 artifact    63.5 GB
```

`cv1-plan` bundles `mapdl/store`, and the store is carried by artifact through
**every** downstream phase:

| phase | transfer |
|---|---|
| reduce (128 batches) | downloads 63.5 GB **each**, and re-uploads `cv1/mapdl/store` **each** |
| head | downloads `cv1-reduce-*` with `merge-multiple` — all 128 copies |
| finalize | downloads `cv1-head` + `cv1-plan` |

That is roughly **24 TB** of artifact traffic for one family. More decisively,
63.5 GB does not fit on a GitHub runner at all, which is why `reduce batch 2`
failed its `df >= 30 GB` guard after a 12-minute download. This is not slow —
it is impossible. The earlier note captured the reduce leg only; head and
finalize have the same defect.

**The good news:** the reducer's compute is already selective. It reads
`row_groups=[index]` for only the row groups whose routing summaries match its
partition cell, and verifies a per-row-group binding proof. Nothing about the
semantics needs to change — only the transport. And the store is
content-addressed (`{prefix}/sha256/{digest}{suffix}`), which maps onto R2
create-only writes exactly as `finalize` already does them.

## 6. Path back to a planet attempt

Ordered by what blocks what, not by size.

1. **Settle the partition scheme (§4).** One-way door; must precede genesis.
   Decide between raising `term_rows` and adding a within-token split
   dimension. Needs owner input.
2. **R2 staging for the store (§5).** Map writes packs to
   `staging/{request_sha}/...` in R2 instead of `cv1-map-N`; artifacts carry
   only markers and proofs (single-digit MB). Reduce/head/finalize range-read
   the row groups they need. `rebuild-r2-shards.yml`'s `id-stage-release` →
   `id-stage-release-finalize` jobs are the working template for matrix jobs
   writing disjoint R2 staging prefixes behind a marker barrier — the pattern
   you asked to keep.
   - Requires R2 credentials in the map/reduce jobs, which today only
     `finalize` has. On a public repo with 89 parallel jobs that deserves a
     scoped, staging-only key rather than reusing the publish credentials.
3. **Map-side partition assignment + the fail-closed gate.** The committed plan
   makes assignment local; map emits per-partition counts (a few MB) and a cheap
   gate job asserts every cap. §2 shows the gate would have fired on this
   release, which is the behaviour we want.
4. **Regenerate the plan as v2** from `2026-07-22.0` with `--headroom-fraction
   0.5`, under whatever caps step 1 settles on. Do not commit a v2 before step 1
   — it would be generated against caps we are about to change.
5. **Re-attempt the planet**, places first.

Deliberately not doing: re-dispatching the address run. It would only confirm
the earlier eviction was a one-off, and it would hit §5 at reduce regardless.

## 7. Reproducing this

```bash
python scripts/places_inventory_v1.py --release 2026-07-22.0 --output places.json
# project -> hydrate -> places-transform-v1 for each of the 88 tasks
python scripts/generate_places_partition_plan.py --packs 'packs/*.parquet' \
  --release 2026-07-22.0 --headroom-fraction 0.5 --output plan-v2.json
```

Note for offline runs: `.github/requirements-hosted-rowgroup.txt` is
`--require-hashes` and its `numpy==2.3.5` hash set does not cover the wheel pip
selects on CPython 3.12 here, so a local venv needs the versions without
`--require-hashes`. The hosted runners pin Python 3.11.14 and are unaffected.

---

# Appendix: partition scheme options

Added 2026-07-24 after measuring each candidate. This is the §6 step-1 decision.

## The constraint, precisely

- `route()` is a **fixed 256x256 equirectangular grid**; `partition_cell` is
  `{y:02x}{x:02x}`. `b2e3` is Tokyo. The cell level is **not** adaptive — only
  the token-hash nibble prefix is.
- The serving index key is `(mode, cell, token)`
  (`places_serving_encode_v1.rs:62`), and reduce keeps
  `row_number() OVER (PARTITION BY partition_cell, token ORDER BY
  confidence_rank DESC, feature_id, ...) <= maximum_serving_candidates`, which
  is **256**.
- Partition count is dominated by **populated cells** (16,633 of 17,863), not by
  splits. Cap changes barely move the budget; cell-grid changes move it a lot.
- `reduce/places-v1/leaves` is written (`places_construction_v1.py:976`) and
  **never read anywhere in the repo**. The head phase explicitly avoids it
  ("Merge only bounded per-task candidates, never planet term leaves"). It is
  lineage/evidence, not a serving input.

## A. Raise the caps (row 1M -> 2M, tokens 200k -> 400k)

Measured, re-deriving the tree from the new release at each cap:

| row cap | token cap | partitions | reduce min | floor margin |
|---|---|---|---|---|
| 1,000,000 | 200,000 | 17,863 | 17,863 | 1.35x |
| 1,500,000 | 300,000 | 17,173 | 17,173 | 2.02x |
| **2,000,000** | **400,000** | **16,978** | **16,978** | **2.69x** |
| 3,000,000 | 600,000 | 16,798 | 16,798 | 4.04x |

- **Fixes:** the `a1d5` token breach outright; doubles the irreducible margin.
- **Costs nothing in budget** — it *reduces* partition count by 885 (fewer
  forced splits), since the floor is the 16,633 populated cells.
- **Byte cap stays satisfied**: at the worst observed 178 B/row, 2M rows is
  356 MB against the 512 MiB cap. 3M rows (534 MB) would exceed it, so 2M is
  the natural stop without also raising bytes.
- **Not a scheme change.** Addressing is untouched; only tree depth changes.
- **Risk, uncalibrated:** the row cap plausibly bounds reducer *memory and wall
  time*, not just output bytes. A reduce batch runs ~140 partitions serially
  inside a 330-minute timeout. If per-partition cost scales with rows, doubling
  the cap could approach that timeout. `MEASURED_REDUCE_MINUTES_PER_PARTITION`
  has never been calibrated because no reduce phase has completed.
- **Does not remove the floor**, only moves it.

## B. Within-token split (add a `feature_id` hash dimension)

- **Fixes:** removes the floor entirely — unbounded divisibility.
- **Blocked on a correctness problem.** Serving is keyed `(mode, cell, token)`
  and top-256 is computed `PARTITION BY partition_cell, token`. If one token's
  rows span two partitions, both emit the **same index key** from different
  subsets: a key collision at encode and a wrong top-256 at serving.
- Therefore requires a new merge stage (reduce fragments -> combine -> re-apply
  top-256) with its own proof obligations.
- **Most expensive; the only structurally complete fix.**

## C. Map-side combiner (top-256 per `(cell, token)` at map time)

- **The observation:** only 256 candidates per `(cell, token)` ever reach
  serving. Carrying 742,392 `'jp'` rows into reduce to select 256 is a 2,900x
  overshoot.
- **Exact, not approximate.** Top-N under a *total* order is decomposable: a
  per-task top-256 followed by a global top-256 over at most 88 x 256 = 22,528
  rows yields byte-identical output to today. The order
  (`confidence_rank DESC, feature_id, source_object_index, source_row_group,
  source_row_index`) is already total.
- **Fixes the floor by deleting the mass**, and shrinks the 63.5 GB store
  enough to relieve the §5 transport wall at the same time. Highest leverage.
- **Cost:** the published leaf would no longer contain every row. No code reads
  leaves, so nothing functionally breaks — but it changes what the pipeline
  durably attests, and the reduce binding proof
  (`selected + discarded = row-group binding`) is built around carrying
  everything. The proof frame needs rework.
- Note this only helps where a `(cell, token)` group is huge. It does nothing
  for a cell that is over cap via many distinct tokens — but that is the
  divisible case, which subdivision already handles.

## D. Finer cell grid (256x256 -> 512x512)

Measured on a spread sample (every 8th task, 11 of 88, 64,628,143 term rows):

| grid | populated cells | vs 256x256 | largest `(cell, token)` | % of 1M cap |
|---|---|---|---|---|
| 256x256 | 1,748 | 1.00x | 450,426 | 45.0% |
| 512x512 | 4,563 | **2.61x** | 226,350 | 22.6% |
| 1024x1024 | 12,195 | 6.98x | 166,173 | 16.6% |

- **Fixes:** refinement genuinely halves the floor per level — `'jp'` in Tokyo
  splits geographically.
- **But the budget cannot absorb one level.** 16,633 populated cells x 2.61
  is roughly **43,400 partitions**, past the 40,000 runner-minute ledger cap
  before any splits at all. (Sample ratios approximate the global union, but
  even 2x would breach.)
- **And it is client-visible**: the serving key contains the cell, so every
  lookup key changes.
- **Worst cost/benefit of the four.**

## Recommendation

**A now, C next, and treat B/D as rejected.**

A is the only option that is cheap, budget-*positive*, and not a scheme change —
it buys 2.69x on the floor and removes the observed breach for the cost of one
constant. It is enough to attempt the planet.

C is the durable fix and it also attacks the transport wall, but it reworks the
proof frame, so it should not gate the first attempt.

The one thing A needs before committing to it: **calibrate reduce cost against
rows.** That is unmeasurable until a reduce phase completes, which needs the R2
staging work (§6 step 2) anyway. So the honest sequence is R2 staging first,
one completed reduce at the current caps, then set the cap from real timings.
