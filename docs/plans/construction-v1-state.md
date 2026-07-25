# construction-v1: current state and the next increment

**Living document. Read this before touching construction-v1.** Undated on
purpose — the dated docs in this directory are point-in-time analyses; this one
is the current picture and supersedes them where they disagree.

Last updated 2026-07-25.

## The one-paragraph version

The pipeline works end to end on real data at small scale and cannot run at
planet scale, for exactly one reason: **intermediate data moves between phases
as GitHub Actions artifacts.** The 63.5 GB Places store gets downloaded by every
reduce batch, re-uploaded by each, downloaded again whole by head and by
finalize. It does not fit on a runner, so reduce has never started. The fix is
not a better cache or a smarter plan — it is to shuffle map output by key into
R2 and let each consumer read only its own keys, exactly as
`build_id_index.py` already does.

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
- **Reduce is watched** — `StageWatchdog` now wraps the reducer's Python +
  pyarrow + DuckDB ingest and its serving encode, with the same caps and the
  same fail-closed semantics as the two `map_task` stages. It was the one phase
  nothing bounded.

## The next increment, precisely

**Map writes its fragments to R2 staging instead of the artifact store**, through
the existing `ObjectStore` seam (`scripts/r2_verified_store.py` already has
`FilesystemStore` and `S3Store`, create-only, content-addressed, tested). Reduce
now reads exactly the fragments in its own bucket range, so this is the change
that turns that from "reads less of a store it downloaded whole" into "fetches
only its own keys" — and it is the reason the 63.5 GB store no longer has to
travel between phases.

Then, in order:

1. Reduce/head/finalize read their own keys from R2. Artifacts carry markers and
   JSON only.
2. Scoped **staging-only** R2 token for map/reduce — today only `finalize` has
   credentials, and this is a public repo with ~89 parallel map jobs.

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

Not started, deliberately — Places first, proven, then port.

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

### Decide BEFORE the planet build: does map emit a per-place artifact?

This is the only part of reverse that is expensive to add late, so it is a
sequencing decision rather than a design one.

Reverse must be built from **per-place records** (one row per place with a
position), not from term rows:

- term rows are ~7.19 per place -- wrong shape and 7x the volume; and
- the combiner keeps only the top `maximum_serving_candidates` per
  `(cell, token)`, so a place whose tokens are all generic and all in saturated
  groups can be dropped from the term store entirely. Harmless for forward
  search, **silently missing from reverse**.

Map currently emits **no per-place artifact**. Term rows and head candidates
(top-N per token) are both the wrong shape. So adding reverse later means
**re-running the planet map phase** -- not redesigning it, but re-running it,
plus a re-publish.

**The insurance is cheap.** Have map also emit a per-place, cell-keyed artifact
alongside the term fragments: `feature_id`, longitude/latitude, and whatever
reverse must return. At ~32 bytes for the minimal form that is roughly **2.4 GB
planet-wide against a ~34 GB term store**, and it rides the same shuffle, the
same staging, and the same proof frame.

- Emit it now -> reverse is purely additive later. New encoder, new index, no
  map re-run.
- Skip it -> the price of reverse is one full planet map re-run.

Open question if we do it: whether the artifact carries only `feature_id`
(cheapest, forces an `/id` round-trip to render a result) or also
name/category/address fields (self-sufficient, larger). That choice does not
block emitting the positions.

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
