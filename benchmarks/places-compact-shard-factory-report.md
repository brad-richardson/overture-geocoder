# Places compact spatial-shard factory spike

Date: 2026-07-14

## Recommendation

The compact binary rescues a **Places-only, low-traffic experimental service**. It does not justify committing to a full-planet addresses + places + streets geocoder under a hard $30/month ceiling.

Recommended narrow path:

1. Keep the full 116.7 B/place basic result projection. Storage is already cheap enough that removing category or context saves pennies while damaging search and forcing expensive hydration.
2. Keep a range-readable packed global head for popular single-clause queries.
3. Publish approximately 1M-row compact spatial shards plus one release manifest. Gate usage with API keys/rate limits and measure real query/head/cache distributions.
4. Stop before global address/street ingestion. Require a relevance evaluation and a real Worker/R2 latency prototype first.

If the actual commitment is “full planet, all three themes, good fuzzy/relevance behavior, 50M uncached queries/month, and less than $30/month,” the candid recommendation is to bail or materially change one of those constraints.

## Measured artifact

The factory built a real artifact from a 1,000,000-row, source-order-limited
rectangular California-area bbox slice of Overture release `2026-06-17.0`.
This is not exact California containment or a random/representative sample.
The input SHA-256 is
`4c4cb3711e806a08801ed87d08c0f2acbc2f7b3f1d69796d65a3824f253c6f84`.

- Artifact: 116,684,322 bytes (116.7 B/place)
- Immutable objects: 1
- Exact tokens: 211,478
- Artifact build: 23.75 seconds on one core
- Full run including six brute-force oracle queries: 1:18.02
- Peak RSS: 1,834,720 KiB (1.75 GiB)
- Swap: none
- Candidate recall against the experiment's token oracle: complete
- Top-10 agreement with the static-rank oracle: exact

The correctness result is retrieval correctness, not relevance quality. For example, static confidence ranking for `golden` + `gat*` includes features matching `gat*` through context as well as names. The experiment has no typo tolerance, edit distance, phonetic matching, language-aware analysis, learned ranking, or labelled relevance judgments.

## Component storage

| component | bytes | B/place |
|---|---:|---:|
| directory and lexicon block index | 69,123 | 0.1 |
| front-coded exact-token lexicon | 2,307,040 | 2.3 |
| field-masked delta postings | 26,407,727 | 26.4 |
| fixed-width record offsets | 8,000,000 | 8.0 |
| basic result projections | 79,900,432 | 79.9 |
| **total** | **116,684,322** | **116.7** |

Prefixes are not materialized. Matching exact tokens are adjacent in the lexicon and their postings occupy one contiguous byte span per query clause.

At the 75M-place working count, the compact spatial-shard-only linear shape is approximately:

- 8.75 GB per release.
- 17.50 GB for two releases.
- 75 shard objects plus one manifest: about 76 measured-format objects/release.

The separate packed-head experiment measured 25.1 MB and 4,088 modeled
objects for the 1M sample. Repacking that head into one range-readable object
is a proposed next format, not something built or measured by this experiment;
head bytes are excluded from the 8.75 GB compact-shard estimate.

This is not a planet forecast. The sample is California-only and flattened to the fields used by the experiment.

## Accuracy versus result storage

| result projection | measured B/place | 75M/release | two releases | accuracy/product effect |
|---|---:|---:|---:|---|
| Basic result: ID, name, category, locality, region, country, coordinates, confidence | 116.7 | 8.75 GB | 17.50 GB | Current measured behavior; usable basic response without hydration |
| Name-only result | 84.9 | 6.37 GB | 12.73 GB | Search can remain complete, but category and administrative context are absent from results and must be hydrated |
| Locator-only result | 62.8 | 4.71 GB | 9.42 GB | Every result requires hydration before it is useful to most clients |

The severe locator-only option saves about 8.1 GB across two retained planet releases. At current R2 Standard rates that difference is roughly twelve cents/month before billable rounding. It remains a poor trade for the default response. Format-v3 ID shards already provide a release-pinned Overture filepath and exact bbox, and a follow-up factory/native probe measured warm full-feature hydration at 0.32–0.37 seconds after a 0.10–0.19 second ID lookup. That makes optional selected-result hydration plausible, but the Worker still needs ZSTD decoding and a compact source-row-group sidecar to avoid fetching a 3.09 MB Parquet footer on cold requests.

Therefore: do not hydrate basic name/category/context fields from public Overture S3 merely to reduce R2 storage. Hydrate optional properties such as websites, socials, phones, sources, and full taxonomy only on a separate detail endpoint. See `places-overture-hydration-report.md` for measured timings and row-group sizes.

## Accuracy versus searchable-field storage

These are measured posting payloads; a real substituted artifact would also have a somewhat smaller lexicon.

| searchable fields | posting bytes/1M | approximate saving at 75M | accuracy loss |
|---|---:|---:|---|
| name + brand + category + context | 26,407,727 | baseline | Preserves current token oracle |
| omit context | 16,398,572 | 0.75 GB/release | Loses locality, region, and country matching/disambiguation |
| name + brand only | 13,169,760 | 0.99 GB/release | Also loses category queries such as `hotel` |
| name only | 12,460,287 | 1.05 GB/release | Also loses brand-only discovery and aliases represented only by brand |

Again, the storage saving is too small to justify the accuracy loss. Context is expensive relative to the posting section, but the entire two-release full artifact remains inexpensive to store.

## Requests versus transferred bytes

| range policy | maximum warm reads, nonempty cases | maximum bytes, nonempty cases | interpretation |
|---|---:|---:|---|
| Tight: 512 B index / 1 KiB records | 24 | 25,856 B | Cheapest bytes, excessive R2 operations |
| Balanced: 64 KiB / 256 KiB | 20 | 382,905 B | Better common queries; scattered multi-term result remains bad |
| Aggressive: 1 MiB / 8 MiB | 9 | 16,700,274 B | Request reduction bought with unacceptable overfetch |
| Whole span | 6 | 39,629,332 B | Artificial lower request bound, operationally wasteful |

Balanced per-query examples:

| query | candidates | warm reads | bytes | dominant issue |
|---|---:|---:|---:|---|
| `starbucks` | 2,387 | 6 | 63,009 | two offset and two result ranges |
| category `hotel` | 4,277 | 4 | 29,660 | ideal single-clause path |
| `starbu*` | 2,395 | 6 | 63,049 | prefix itself remains one posting span |
| `golden` + `gat*` | 26 | 20 | 382,905 | top results scattered across seven offset and nine result spans |
| context `san` + `francisco` + `cafe*` | no final match | 6 | 341,144 | high-frequency posting traversal before empty intersection |

The packed global head previously answered eligible `starbucks`/`starbu*` cases in one read and 4–6 KiB. It is necessary for both latency and high-scale request economics, but it is top-k-only under a static rank and cannot provide arbitrary tail enumeration or dynamic location reranking.

## Processing tradeoffs

| choice | build/runtime processing | storage/request effect | accuracy effect |
|---|---|---|---|
| Exact-token shard, prefixes resolved at runtime | Small builder; one contiguous prefix posting span | Avoids all prefix-union duplication | Complete token-prefix recall; short prefixes can read large spans |
| Precompute every prefix | More build memory and publication work | Prior spike added 86.8–128.3 MB per 1M | Same recall; lower some posting fanout, but rejected on replication |
| Static global top-k head | Extra offline ranking and ~25.1 MB/1M in the broad threshold model | One small read for popular queries | Candidate tail omitted; ranking fixed until next release |
| Larger range coalescing | Little extra CPU; much more I/O | Fewer Class B operations, up to tens of MB/query | No retrieval loss, but poor latency/cost |
| Tight ranges | More subrequests and response coordination | Very low transferred bytes | No retrieval loss; high operation cost at scale |
| Stronger runtime ranking/fuzzy search | More Worker CPU and candidate state | May require extra n-gram/edit indexes | Better user relevance/typo recall, but unmeasured and likely the next major complexity |
| Confidence cutoff or top-N postings | Less build/storage/runtime work | Potentially large reduction | Severe, unmeasured recall loss; excludes long-tail/new/low-confidence entities |
| Route to fewer spatial shards | Less I/O and CPU | Largest practical query saving | Router mistakes and border cases become false negatives unless a broader fallback runs |

The factory is not the bottleneck. At the measured artifact build rate, 75 independent 1M-place shards are about 30 minutes of single-core build work or roughly four idealized minutes at eight-way concurrency. Eight builders at the measured peak would use about 14 GiB, fitting comfortably on the 64 GiB factory machine. Overture extraction, sorting, upload, validation, and release churn will dominate the real pipeline.

## Cost envelope

Cloudflare R2 Standard currently includes 10 GB-month, 10M Class B operations, and 1M Class A operations; excess storage is $0.015/GB-month and Class B reads are $0.36/million. Workers Paid is $5/month, includes 10M requests and 30M CPU-ms, then charges $0.30/million requests and $0.02/million CPU-ms.

At current traffic of hundreds of queries/day:

- Two full Places releases cost only cents of incremental R2 storage.
- Requests remain well inside the included R2 and Workers allotments.
- The likely bill remains approximately the $5 Workers minimum, assuming CPU stays reasonable.

At 50M queries/month, excluding CPU and storage because reads dominate:

| average R2 reads/query | Workers requests | R2 Class B | subtotal |
|---:|---:|---:|---:|
| 1 | ~$17 | ~$14.40 | ~$31.40 |
| 4 | ~$17 | ~$68.40 | ~$85.40 |
| 6 | ~$17 | ~$104.40 | ~$121.40 |
| 20 | ~$17 | ~$356.40 | ~$373.40 |

These are simple usage estimates before CPU, billing-unit rounding, or cache hits. At 50M/month, the $30 ceiling is missed even at one R2 read per uncached query. A high edge-cache hit rate, request limits, API keys, or a higher budget would be required.

## Release-to-release tradeoffs

- Immutable shards make rollback simple: publish a new manifest, retain the prior manifest/shards, and atomically switch the active release.
- Approximately 76 compact-shard/manifest objects per release is operationally manageable; the global-head publication shape remains unproven.
- Full rebuilds are cheap enough on the factory machine, but full Overture extraction may not be. Changelog-driven shard rebuilding can reduce work, though moved/removed entities and shard-boundary changes complicate correctness.
- Stable spatial partition boundaries are important. Count-balanced repartitioning every month would churn many otherwise unchanged shards.
- The full Places schema deprecates/replaces fields over time; the compact format needs explicit schema/version compatibility and rebuild tests.

## Candid decision matrix

| goal | decision |
|---|---|
| Places-only experiment, current traffic, basic exact/prefix/category/context search | **Continue** with compact shards + packed head |
| Places-only public beta with API keys/rate limits and willingness to tune relevance | **Plausible**, after a real Worker range prototype and labelled query evaluation |
| 50M queries/month under $30 without relying on cache hits or limits | **Bail/change constraint** |
| Full planet addresses + places + street/transportation now | **Bail/pause**; this spike validates only 75M Places, while the other themes contain hundreds of millions of features and different query semantics |
| Strip basic display/context fields and hydrate every result from Overture | **Reject**; saves trivial storage cost and creates a slow, complex second retrieval system |
| Strong fuzzy, multilingual, address-quality geocoding comparable to a mature search engine | **Not demonstrated**; this is a substantially larger relevance/indexing project |

## Minimum evidence before proceeding beyond Places

1. Implement one Worker proof-of-concept reading three compact shards and the packed head from R2.
2. Measure p50/p95 latency, Worker CPU, R2 reads/query, bytes/query, cache hit rate, and head hit rate over a representative query corpus.
3. Label at least a few hundred real queries for relevance, including typos, category/context queries, border locations, and multilingual names.
4. Set explicit bailout gates—for example p95 latency, average R2 reads, relevance@5, and projected 50M-query cost.
5. Only then estimate address/street formats independently; do not linearly reuse Places bytes or accuracy conclusions.

## Reproduction

```bash
.venv/bin/python scripts/factory_extract_places.py \
  --release 2026-06-17.0 \
  --limit 1000000 \
  --output exports/places-ca-1m.parquet

.venv/bin/python scripts/experiment_places_compact_shard.py \
  exports/places-ca-1m.parquet \
  --artifact artifacts/places-ca-1m.pcsh \
  --json-out reports/places-ca-1m-compact-shard.json \
  --markdown-out reports/places-ca-1m-compact-shard.md
```
