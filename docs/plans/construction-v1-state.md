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

## The next increment, precisely

**Reduce reads by bucket range.** Today a reduce job is per-partition and
re-derives which fragments to open each time. It should own a *bucket range*
(like `build_id_index.py --prefix-start/--prefix-end`), read each of its
fragments once, and emit every partition whose cell falls in that range.

Then, in order:

1. Map writes fragments to R2 staging instead of the artifact store, through the
   existing `ObjectStore` seam (`scripts/r2_verified_store.py` already has
   `FilesystemStore` and `S3Store`, create-only, content-addressed, tested).
2. Reduce/head/finalize read their own keys from R2. Artifacts carry markers and
   JSON only.
3. Scoped **staging-only** R2 token for map/reduce — today only `finalize` has
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

**Unbuilt and recorded nowhere else: POI and address reverse need their own
spatial index.** No design exists. Do not assume the construction-v1 output can
be adapted to it.

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

See `2026-07-24-construction-v1-follow-ups.md`. The two that can bite:

1. `reduce_partition` has no `StageWatchdog`, and the raised row cap doubles the
   peak of the one phase nothing bounds.
2. Three evidence-spec `*_hard_cap` values are dead declarations the build now
   exceeds. Enforce them or delete them.
