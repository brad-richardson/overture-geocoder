# Overture Places search spike

Date: 2026-07-14

## Status and decision

The architecture spike is complete. It establishes a credible storage and
serving shape for a low-traffic, Overture-native POI search experiment, but it
does not establish production search relevance or a complete Worker
implementation.

The selected direction is:

- one immutable, range-readable compact binary per approximately one million
  spatially related Places;
- a release manifest and a small packed global top-k head for common unlocated
  queries;
- exact-token postings with prefixes resolved through the sorted lexicon at
  query time, rather than materialized prefix postings;
- a basic result projection containing GERS ID, name, basic category,
  locality, region, country, coordinates, and confidence; and
- optional selected-result hydration from Overture source GeoParquet, using
  the existing ID locator plus future row-group metadata.

Do not adopt the modeled KV/R2 page namespace, complete cell-local prefix
postings, PostgreSQL, or hydrate-every-result designs for the first
implementation.

This is an experimental POI/entity locator, not yet a general natural-language
search engine. A defensible initial contract is literal name/brand search,
token-prefix search, structured category search, and explicit proximity or
context. Queries such as `coffee shop near me` require a query planner,
category aliases, location input, and distance ranking that have not yet been
built or evaluated.

## Why this work exists

Overture publishes an unusually rich, permissively usable Places dataset with
stable GERS IDs, but consuming the planet-scale GeoParquet release is still a
substantial data-engineering task. The useful gap for this project is an
Overture-native index and API that preserves IDs and source provenance, not an
attempt to reproduce every relevance feature of a mature commercial search
engine.

## What was tried

### 1. SQLite FTS and the earlier name trie

SQLite FTS5 is the working precedent in the divisions geocoder. An earlier
Places trie reached 68.7 bytes/place, but it indexed only normalized primary
names and returned only IDs and coordinates. It omitted token search, aliases,
categories, context, ranking, and display fields, so it is a size floor rather
than a comparable solution.

The first richer compact-index fixture used a front-coded lexicon,
field-specific postings, record offsets, and result projections. It measured
130.4 bytes/place, but a single query could require dozens of small ranges.
This established that bytes were tractable while remote-read fanout was not.

Evidence: [compact-index report](../benchmarks/places-compact-index-report.md).

### 2. KV catalog plus paged R2 index

The paged design kept only a release pointer and page targets in KV, while R2
held packed posting and result pages. It preserved candidate recall and
modeled deterministic overflow for frequent terms.

The small fixture looked promising, but the one-million-place California run
showed the core failure: useful results were scattered across pages. Queries
required 3-13 warm reads and approximately 0.5-2.6 MB in the measured cases.
The design also projected a large publication namespace. Changing page sizes
traded requests for overfetch without removing the underlying locality
problem.

Evidence: [page-model report](../benchmarks/places-kv-r2-pages-report.md) and
[factory scale report](../benchmarks/places-kv-r2-factory-scale-report.md).

### 3. PlanetScale PostgreSQL

A PostgreSQL schema and query model explored `tsvector`/GIN, prefix indexes,
release partitions, and atomic release selection. No database was provisioned,
so this produced no measured query plans, index sizes, network latency, or
concurrency evidence.

PostgreSQL would remove custom byte-range planning, but it adds a permanently
running remote database, connection management, retained-release index
duplication, and a less predictable hobby-project cost floor. It remains a
reasonable conventional architecture, but not the selected low-idle-cost
direction.

Evidence: [PostgreSQL report](../benchmarks/places-postgres-report.md).

### 4. Spatial result locality and a packed global head

Spatially ordered result records improved locality for routed queries. A
packed global head colocated static top-ten results for frequent exact and
long-prefix terms; exercised `starbucks` and `starbu*` queries required one
modeled read and 4-6 KiB while matching the experiment's static-rank oracle.

The head is intentionally incomplete. It is suitable for common top-k queries,
not arbitrary tail enumeration or dynamic proximity ranking. Spatial result
ordering alone did not eliminate posting fanout for multi-clause queries.

Evidence: [locality/head report](../benchmarks/places-locality-head-factory-report.md).

### 5. Complete cell-local heavy and prefix postings

Duplicating frequent postings by spatial cell reduced several located queries
to 2-4 reads with complete retrieval recall. The best bounded run nevertheless
added 86.8 MB and 10,944 objects per one million Places, and still could not
remove result-hydration reads. Materializing prefix unions replicated too much
data and publication work for too little benefit.

This design was rejected. Its useful lessons—spatial document ordering and
the packed global head—carry into the selected format.

Evidence: [cell-postings report](../benchmarks/places-cell-postings-factory-report.md).

### 6. Compact spatial binary shard

The final experiment put a sorted exact-token lexicon, delta-coded postings,
fixed record offsets, and basic result projections into one range-readable
binary per spatial partition. Prefix matches map to adjacent lexicon entries
and one contiguous posting span; prefix unions are not published separately.

On one million California Places from release `2026-06-17.0`:

| measurement | result |
|---|---:|
| artifact size | 116,684,322 bytes |
| bytes/place | 116.7 |
| immutable shard objects | 1 |
| artifact build time | 23.75 seconds, one core |
| peak RSS | 1.75 GiB |
| exact tokens | 211,478 |
| retrieval recall against experiment oracle | complete |
| static-rank top-ten agreement | exact |

A linear 75-million-place shape is approximately 8.75 GB per release. With
roughly one-million-place partitions, the publication shape is approximately
75 shard objects plus a manifest and global head, rather than millions of
small objects.

Balanced modeled reads ranged from four for a category query to twenty for a
scattered multi-clause query. The compact format solves storage and object
inventory, but a real Worker reader, routing, range cache behavior, and query
planning are still required to solve latency and relevance.

Evidence: [compact-shard report](../benchmarks/places-compact-shard-factory-report.md).

### 7. Overture source hydration

The format-v3 ID index already returns a release-pinned source filepath and
exact bbox. A native factory test resolved a selected Place through that
locator and hydrated its source feature in 0.43-0.51 seconds on warm runs.
Cold runs were about 2.5 seconds, primarily because the source Parquet file had
a 3.09 MB footer before the approximately 1.07 MB matching row group.

Enabling ZSTD in the Rust Parquet dependency compiled successfully for the
Worker WASM target, but runtime bundle size, CPU, and memory have not been
measured. A source-file sidecar or an ID-index extension should identify the
row group and relevant column ranges so the Worker does not fetch a large
Parquet footer for every cold detail request.

Hydration is appropriate for optional details such as websites, phones,
socials, sources, full taxonomy, and other selected-result fields. It is not a
good reason to remove name, category, context, or coordinates from the search
artifact: the R2 storage saving would be trivial and every result would become
slower and more fragile.

Evidence: [hydration report](../benchmarks/places-overture-hydration-report.md).

## What we learned

### Storage is not the limiting resource

The selected basic result is about 8.75 GB for a 75-million-place release and
about 17.5 GB for current plus rollback. Removing category and context saves
only a few gigabytes and harms both ranking and result usefulness. At R2
storage prices, this is the wrong optimization target.

### Object and request shape matter more than raw bytes

One-object-per-term and heavily paged layouts turn a modest byte total into
large publication inventories and many billable reads. Approximately 77
objects per release is operationally much healthier. Within a shard, scattered
top results can still create many byte ranges, so document ordering, range
coalescing, the packed head, and routing remain important.

### The factory is sufficient for release builds

The one-million-place compact build used 1.75 GiB and 23.75 seconds on one
core. Partitioned, bounded-concurrency builds fit comfortably within the
factory's 64 GiB memory and twelve-core safety ceiling. Extraction, sorting,
upload, validation, and release churn are more likely to dominate than binary
serialization.

The factory should be an offline build machine only. Serving remains on
managed infrastructure; the home machine does not need to be publicly exposed
or continuously available.

### Current traffic is inexpensive; arbitrary scale is not

At hundreds of queries per day, Workers and R2 usage should remain near the
Workers paid minimum. At 50 million uncached queries per month, even one R2
read per query was modeled above the $30/month target before Worker CPU.
API-key limits, caching, or a changed budget would be needed long before that
scale. This does not block a hobby-project beta.

### Retrieval correctness is not search quality

The experiments preserve their defined token candidate sets and static top-k
ordering. They do not demonstrate that humans receive the best answer.
Missing work includes possessive and punctuation normalization, brand aliases,
category synonyms, `near me`/`in X` parsing, distance ranking, typo tolerance,
multilingual analysis, and labelled relevance evaluation.

## Settled choices

Keep:

- approximately one-million-place immutable spatial binaries;
- exact-token lexicon and field-masked delta postings;
- runtime prefix resolution through contiguous lexicon/posting spans;
- spatially ordered document IDs and basic result records;
- the full basic result projection;
- a small packed global static top-k head;
- complete shard fallback semantics;
- immutable manifests with current/rollback releases; and
- optional source hydration after result selection.

Reject or defer:

- one R2 object per token or prefix;
- complete precomputed prefix unions;
- complete cell-local posting duplication;
- stripping basic response fields to save storage;
- PostgreSQL as the first hosted serving layer;
- arbitrary unbounded natural-language POI claims; and
- a commitment to 50 million uncached monthly queries under $30.

## Next implementation gate

The next POI work should be a deliberately small end-to-end prototype, not
another storage-layout spike:

1. Define and version the compact-shard binary and release manifest.
2. Build three adjacent real spatial shards and a small global head.
3. Implement a Worker range reader with checksum validation and bounded range
   coalescing.
4. Support an explicit initial query contract: normalized name/brand tokens,
   last-token prefix, structured basic category, explicit `lat`/`lon`, and
   locality/context filters.
5. Measure p50/p95 latency, Worker CPU, reads/query, bytes/query, head hit rate,
   and edge-cache behavior.
6. Label a few hundred representative queries and measure relevance at five,
   including punctuation, brand, category, context, proximity, borders,
   multilingual names, and no-result cases.
7. Stop or redesign if the prototype cannot meet explicit latency, read-count,
   relevance, and projected-cost gates.

Only after this gate should the project add fuzzy matching or optional Parquet
detail hydration to the Worker.

## Parked address/division follow-up

Addresses should probably be associated with Overture divisions during the
offline build. A materialized division chain can provide routing,
disambiguation, structured response context, and GERS division IDs without a
runtime spatial join. It may also allow address shards to reference a compact
division record and hydrate repeated locality/region names once.

That relationship needs its own experiment because address `address_levels`,
postal cities, and geometric containment do not always describe the same
thing. The likely safe shape is to retain the source address fields while
adding separately identified containing-division GERS IDs and an explicit
match method/confidence. It should not be folded into the POI format before
the POI Worker gate above is complete.
