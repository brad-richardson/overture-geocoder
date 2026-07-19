# Global v2 Places + address build readiness

Status: one-way contracts resolved; data-plane executor not yet dispatch-ready.
No global shard build or catalog publication has been started.

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

Implement the two-stage global data plane behind the frozen request:

- current-release Places and address inventory;
- exact maximum-level count/fragments with per-task reconciliation;
- stable plan derivation with no prior split history for the first v2 build;
- bounded reducers that emit the existing `.pcsh`, `.aidx`, and `.adat`
  formats;
- a bounded global `head.phrp` producer;
- remote manifest verification and v2 Worker smoke tests against the
  unpublished slice.

Only after that slice is green should a human dispatch the first global build.
Promotion remains a separate decision after inspecting its evidence.
