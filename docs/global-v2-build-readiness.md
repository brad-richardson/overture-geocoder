# Global v2 Places + address build readiness

> **Historical document — v2 paused 2026-09-02.** This was a pre-execution
> readiness snapshot. Later global builds and publications did occur, so its
> status and next-action statements are not current. V2 processing and serving
> are now paused with the code and evidence retained. See
> [the canonical construction state](plans/construction-v1-state.md) for the
> operational decision.

Status at the time of writing: frozen request and strict map primitives
implemented; fan-in executor not yet dispatch-ready. At that time, no global
shard build or catalog publication had been started.

## Decisions now fixed

- Build only the new Places and address families. Reuse the verified legacy
  core release for divisions and GERS ID lookup; do not rebuild or reshard it.
- Places ownership is `world-quadkey-v1`, with sticky splits. Addresses use
  `country-fnv1a-high-bits-v1`, also with sticky splits.
- A compute task is not a serving shard. Source maps, reduce jobs, and their
  concurrency limits are temporary execution details; permanent object names
  come only from the stable family partition plans.
- The first address capability is exact structured forward lookup. Free-text
  address forward and address reverse require separate secondary indexes later.
- Places forward requires both the routed `catalog.pcat` and the bounded global
  `head.phrp`. A v2 release cannot advertise Places forward unless both occur in
  the verified family object set.
- The build writes an unpublished families-only slice. Publication is a later,
  explicit atomic v2 catalog operation.

These values are emitted and revalidated by
`scripts/global_v2_build_request.py`. The manual
`prepare-global-v2-build.yml` workflow can freeze an exact request at a merged
producer commit, but intentionally has no cloud credentials or data-plane job.
Its inputs include the exact reusable core manifest, unpublished slice,
canonical inventory/schema digests, and nullable predecessor-family digests;
it reproduces the request byte-for-byte before retaining it.

## Implemented map boundary

- The address footer inventory fingerprints the required Arrow paths, types,
  and nullability and carries that fingerprint through the bounded row-group
  projection. The strict mapper uses exclusive named rejections, reconciles
  every input row, measures maximum exact-key fanout, and emits bounded
  content-addressed fragments owned by country and maximum-resolution hash
  bucket.
- The Places inventory pins the exact public S3 listing, per-object ETag/size,
  required nested Arrow schema, row groups, and a deterministic map-task plan.
  The strict mapper rejects invalid IDs, geometry, coordinates, names, and
  status without synthetic IDs or coordinate coercion, then emits bounded
  content-addressed fragments owned by maximum-level world quadkey.
- Map-task identities are provenance only. Neither mapper turns a task or
  fragment number into a serving-shard name, so task sizing can change without
  changing permanent ownership.
- Family publication streams large local files and remote readback hashes one
  object at a time; it no longer retains the whole family fleet in memory.
- The Worker prepares and caches bounded address/Places routing indexes. Global
  address selection is a country lookup plus range binary search, and located
  Places routing probes at most 15 quadkey prefixes. Cold v2 admission requires
  the immutable completion manifests, exact entrypoint sizes, and Places head;
  uploaded fragments alone remain undiscoverable and unservable.

## Why the existing global planner is not the executor

`global_build_manifest.py` proved deterministic source assignment, completion
manifests, and fan-in. Its numbered reduce partitions predate the stable family
layouts. Treating those numbers as shard IDs would turn runner sizing into a
permanent storage contract and force broad churn when concurrency changes.

The executor therefore needs two distinct layers:

1. Map pinned source ranges into content-addressed fragments carrying maximum-
   level spatial keys (Places) or country/hash buckets (addresses).
2. Aggregate exact retained counts, apply sticky split history, and create the
   stable serving partition plan.
3. Assign one or more stable serving partitions to each bounded reduce job.
   This assignment may change without changing shard IDs.
4. Build Worker-readable shards, build the global Places head, verify the exact
   remote object set, and finalize a non-promoting family slice.

## Evidence already obtained from production Overture data

The source is not fixture-only. Existing hosted runs exercised the pinned
`2026-06-17.0` release: a full CONUS Places extraction/build and a 33-task
US-dominant address sweep, plus the smaller regional reader/resume tests. Those
runs support the memory, row-retention, resumability, deterministic output, and
R2 readback assumptions used here. They are not a planet build and do not prove
global skew, global head relevance, or end-to-end planet duration.

## Next implementation slice

Complete the fan-in data plane behind the frozen request:

- stable plan derivation with no prior split history for the first v2 build;
- bounded reducers that emit the existing `.pcsh`, `.aidx`, and `.adat`
  formats, enforce reducer fan-in limits, and compact map fragments when needed;
- a pinned executor image (including the PyArrow/Parquet writer version),
  recorded in completion provenance so cross-host retries remain
  byte-reproducible;
- a bounded global `head.phrp` producer;
- remote manifest verification and v2 Worker smoke tests against the
  unpublished slice.

Only after that slice is green should a human dispatch the first global build.
Promotion remains a separate decision after inspecting its evidence.
