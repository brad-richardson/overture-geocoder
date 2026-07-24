# Places construction v1: plan to global scale

Date: 2026-07-24. Status: LOCAL SPIKE — uncommitted, for owner review. No PR,
no workflow, no remote writes. Produced from local measurement over the real
2026-06-17.0 census data (12 projected map tasks, ~10.7M of 75.6M places) plus
the merged readiness evidence in
`benchmarks/places-construction-v1-data/evidence/`.

A companion spike is root-causing the Rust↔Python semantic-digest divergence;
this plan assumes that lands and focuses on everything else: the dense-task
scratch breach, the global-head index cap, the verification architecture, the
two spec-level gate defects, and the hosted path to a planet slice.

## New measurements (this spike, 2026-07-24, local, real data)

Ran the release `places-transform-v1` binary over all 12 projected census
tasks (10,681,495 admitted features, 14.1% of the 75.64M source rows;
89,162,905 emitted term rows) and censused the token universe with DuckDB:

- **Distinct tokens, 12-task union: 4,749,161.** Per-task distinct ranges
  from 128k (US tasks) to 1.19M (task 85, Korea-heavy). Distinct-per-feature
  *rises* as CJK tasks join the union (0.251 over the first six tasks →
  0.445 over all twelve) — token growth is not saturating in-sample, so
  linear extrapolation is the honest planet bound: **~25–34M distinct tokens
  planet-wide** (upper bound 0.445 × 75.64M ≈ 33.6M).
- **Head rows** (`sum(min(rows,10))` per token): 9,273,254 in-sample →
  **~65M planet head rows** at the linear bound. At the measured ~15-byte
  head-weighted token length and ~165-byte encoded entry, that is a
  **~10–11 GB total head payload** — two orders of magnitude past any
  single-artifact design, settling the sharding question.
- **Token frequency is extremely head/tail split:** 95.1% of tokens have ≤10
  total rows (their full result set IS the head entry); p50 = 1 row,
  p99 = 89 rows, max = 2,122,574 (a country token). Only 22,568 tokens
  (0.5%) exceed 256 rows in-sample.
- **Distinct (cell, token) pairs: 9,901,140** (~2.1 cells per token) — the
  routed lane's per-cell scoping stays cheap.
- **Terms IPC sizes** (map-scratch sizing): ~0.9–1.8 GiB for normal tasks,
  3.12/3.09 GiB for dense tasks 86/87 (~230 B/term, uniform across tasks).

## Blocker 1 — dense-task scratch breach is pipeline hygiene, not architecture

Tasks 86/87 (~13.7–13.8M terms, Japan; 13.9 terms/feature vs 6.5–7.5
elsewhere, driven by CJK bigrams over ~1M CJK features) breach the 8 GiB
map-stage scratch cap. The breach is additive retention, not any single
oversized structure. Everything below stays on disk simultaneously inside one
`map_task` workspace until `TemporaryDirectory` cleanup
(`scripts/places_construction_v1.py`):

| workspace item | task 87 estimate | when it becomes dead weight |
|---|---|---|
| `hydrated.arrow` | ~0.16 GiB | after the Rust transform reads it |
| `terms.arrow` | 3.09 GiB (measured; task 86: 3.12 GiB) | after DuckDB ingest |
| DuckDB `terms` table | ~2.5–3 GiB | after `packed` is created |
| DuckDB `packed` sorted copy | ~2.5–3 GiB | after pack export |
| per-pack `.parquet` + `.arrow` (~274 packs) | ~2.5–4 GiB accumulated | each pack, after `store.put_content` (which copies) |

Sum ≈ 11–13 GiB against an 8 GiB cap — matching the observed breach. The fix
is staged deletion plus eliminating the double table:

1. Delete `hydrated.arrow` after the transform exits.
2. Delete `terms.arrow` after ingest.
3. Do not keep `terms` and `packed` side by side. Either `DROP TABLE terms`
   before pack export with a `CHECKPOINT`, or (cleaner) skip the second copy
   entirely: one sorted `COPY ... PARTITION_BY (pack_id)` write, the pattern
   `build_id_index.py` already uses. DuckDB external-sorts with spill under
   the existing 1 GB memory limit; spill is transient and bounded.
4. Unlink each pack `.parquet`/`.arrow` immediately after `put_content`
   (the store copies bytes; originals are dead).

Estimated post-fix peak for task 87: ~4.5–5.5 GiB, under the 8 GiB cap with
the required 25% headroom. This is an estimate; the plan gates on a measured
re-run of tasks 86/87 (step 2 below) before the cap is frozen in a new
evidence-spec version. No cap is being relaxed — the pipeline is fixed under
the same cap, and the run set changes, which already requires a new spec
version by the frozen relaxation policy.

Alternative considered and rejected: splitting dense tasks upstream in the map
plan. Term fan-out is content-dependent (CJK bigrams), invisible to the
footer-only inventory planner; predicting it would require a planet-scale
census pass to solve a problem the hygiene fix removes outright. Task-internal
sharding is likewise unnecessary once retention is fixed.

## Blocker 2 — the global head must become hash-sharded and range-read

The head encoder's `MAX_INDEX_ENTRIES = 250_000`
(`crates/geocoder-construction/src/bin/places_serving_encode_v1.rs`) failed
closed on real merged data, exactly as designed. The measured token universe
(below) says planet distinct tokens are in the tens of millions — no single
artifact fits either the cap or the Worker (the dormant decoder parses the
whole artifact and materializes the full index in memory; a planet head index
alone would be gigabytes).

Note the routed lane does NOT share this problem: the adaptive-genesis
partition cap `partition_distinct_tokens = 250_000` equals the encoder cap, so
every routed artifact fits by construction. Only the head lane is broken.

### Design: token-hash-prefix head shards

Shard the head by the top N bits of the index hash the format already
computes (`SHA-256("overture-places-serving-index-v1\0" + token)[..8]`):

- **Construction.** Per-task head candidates (already emitted by every map
  task, top-10 per token) are tree-merged: top-10-per-token merge is
  associative and idempotent (`top10(A ∪ B) = top10(top10(A) ∪ top10(B))`),
  so bounded fan-in stages replace the current single 5M-row-capped merge.
  The final stage writes one `COPY ... PARTITION_BY (shard = hash >> k)`
  pass; each shard is encoded as an independent `PLHD` artifact and
  independently verified. A small head manifest (shard count, per-shard
  key/hash/bytes/records) binds the set.
- **Serving.** Worker: hash token → shard id → two bounded reads through the
  existing `range_reader` (footer/index region, then the one payload extent),
  instead of whole-artifact parse. This mirrors the production ID-index
  lookup (4096 UUID-prefix parquet shards, R2 range reads) and the PR #137
  selective row-group rehearsal.
- **Shard count: 4096** (matching the ID index), from the measurements
  above: ~34M planet tokens / 4096 ≈ **8.2k index entries per shard** (33×
  under `MAX_INDEX_ENTRIES`), index region ≈ 450 KB (one cacheable range
  read), payload ≈ 2.7 MB per shard (~11 GB total). The manifest makes the
  count a per-build value, not a format constant, so it can be re-measured
  per release.

Full-coverage head (every distinct token, top-10) is retained — and the
measurement strengthens it: for the 95.1% of tokens with ≤10 total rows the
head entry *is* the complete planet answer, so the head shard is the entire
query for the long tail. A
"famous-only" admission filter would shrink the artifact but is a
user-visible launch-semantics decision (one-way-doors §7) and is unnecessary
once shards make full coverage cheap; it stays available as a later,
non-format optimization.

Format consequence (one-way door, §2): `PLHD` gains a sharded layout and the
Worker head decoder gains a range-read path. This is exactly the moment to do
it — nothing has ever been published, so it is a free change today and a
permanent decoder-matrix obligation the day after first publication.

## Blocker 3 — retire per-run dual-implementation parity; keep it as a census tool

The frozen Python semantic baseline just did its job: it caught a real
cross-implementation divergence on 6 of 7 role tasks during census. That is
also the argument for demoting it. As a *per-planet-run gate* it costs a full
second planet of compute per run, and the bug class it catches is
cross-language Unicode/serialization skew — which is present or absent per
*contract version*, not per *run*.

Recommendation — verification architecture (c)+(b), no per-run baseline:

- **Per contract version (once):** run the Python baseline differentially
  against Rust over the stratified 12-task census (the mechanism that caught
  the current divergence), plus the hand-authored golden fixture corpus
  covering the nasty classes (multilingual, CJK bigrams, combining marks,
  duplicate UUIDs, every rejection rule). Both digests lanes must match
  exactly. Freeze the result in the evidence spec.
- **Per planet run (always):** single Rust implementation with the structural
  frame that is already built and is single-implementation: exact counts +
  dual-lane digest sums carried and reconciled at every boundary
  (map marker → adaptive plan → per-row-group selected/discarded reduce
  reconciliation → head input binding), independent Rust verifier decoding
  every serving artifact, determinism double-runs on sampled tasks, and
  fail-closed resource gates.

Honest cost: a systematic Rust tokenizer bug that only manifests on planet
data shapes absent from the census strata would not be caught by a planet
run. Mitigation: the census strata were chosen adversarially (near-cap,
worst-skew, CJK-heavy, multilingual) and can be extended cheaply; extending
census coverage is strictly cheaper than 2× planet compute per run.

## Spec-level gate fixes

1. **Duplicate-coverage gate.** Real 2026-06-17.0 data has zero duplicate
   UUIDs across all 12 census tasks (`duplicate_uuid_rows: 0` everywhere), so
   the gate can never close on real data. Move duplicate coverage to the
   golden fixture corpus (synthetic duplicate UUIDs driven through
   map→plan→reduce, multiplicity preserved through provenance — the fixture
   already exists), and replace the planet gate with an informational
   recorded observation. Fail-closed is preserved: the *fixture* gate fails
   closed; the *planet* gate stops lying about being closable.
2. **Hydrate batch vs IPC cap.** `hydrate()` emits fixed 65,536-row batches
   and `ingest()` rejects batches *over* 65,536 rows — at the boundary, equal
   is admitted, but the evidence-spec IPC cap and the two hardcoded constants
   are maintained independently in five call sites. Derive both from one
   shared constant and assert the relationship in the readiness validator so
   the spec, hydrate, ingest, and `write_arrow_query` cannot drift.

## Alternatives considered (owner asked for full reconsideration)

**B. Single token-hash-sharded store, no routed/head split.** Serve every
query from one global sharded token store (ID-index pattern), filtering by
cell at query time. Rejected: unbounded per-token payloads — a planet-popular
token's candidate list has no cell scoping, so query cost is unbounded
exactly where the routed lane's per-cell 256-candidate cap currently bounds
it. Bounding it globally reintroduces head-style admission for *every*
lookup, which is the worse semantic door.

**C. Return to SQLite FTS5 shards (legacy-core pattern).** Proven serving
path, but construction is row-at-a-time — the exact planet-scale failure the
DuckDB+Rust reset exists to escape — and it has no exact-digest proof story.
Rejected as a regression to the abandoned architecture.

**A. Current design + the three targeted fixes above.** Recommended. The
map/reduce/proof frame survived real-data rehearsal; every observed failure
is either retention hygiene, one under-sized encoder constant, or
verification-policy cost — none is structural.

## Bounded sequence to a Places planet slice

Each step is separately reviewable and fail-closed; later steps do not start
early. Steps 1–3 are independent of each other and parallelizable.

1. **Digest divergence fix** (companion spike) + golden-fixture additions for
   whichever Unicode/serialization class caused it. Gate: 12/12 census tasks
   baseline↔candidate parity, twice.
2. **Map hygiene fix** (staged deletion + single sorted partitioned COPY).
   Gate: tasks 86/87 measured under the 8 GiB cap with ≥25% headroom; new
   evidence-spec version; byte-identical packs vs current pipeline on a
   parity task (the sort/pack contract is unchanged, so outputs must not
   change).
3. **Sharded head**: tree-merge planner + `PARTITION_BY` shard write +
   per-shard `PLHD` encode/verify + head manifest + Worker range-read decoder
   path. Gate: real merged census head (the one that failed at 250k) encodes,
   verifies, and answers Worker probes; shard index regions within one
   coalesced range read.
4. **Full census re-run to `ready:true`** under the new evidence spec
   (12 tasks, all roles, both spec fixes in). This closes all 17 readiness
   reasons or honestly reports what remains.
5. **Hosted Places workflow** mirroring the Address map/reduce topology
   (marker-gated resume, create-only uploads, non-promoting namespace,
   execute-mode typed confirmation). Concurrency and reducer counts from
   measured planner output, per the scope doc.
6. **Non-promoting planet slice** — the last item, dispatched only after 4–5
   are green and reviewed. Promotion stays a separate decision.

Rough hosted-scale arithmetic for step 6 (from census wall times, ~89 map
tasks at 30–60s construction each plus hydration/upload, reduce bounded by
partition caps, head tree-merge in log-depth stages): well inside a single
330-minute-per-job matrix with single-digit parallelism. The binding planet
numbers come from step 4's evidence, not this paragraph.

## One-way doors touched by this plan

- `PLHD` sharded layout + head manifest (one-way-doors §2): change it now,
  before first publication, or carry the decoder matrix forever.
- Verification policy demotion (this doc, blocker 3): not itself a format
  door, but the evidence-spec change must be versioned and the census
  differential must stay mandatory per contract version.
- Everything else here (retention hygiene, shard count, tree-merge fan-in,
  spec constant unification) is explicitly NOT a one-way door.


## What is already proven (do not redesign)

- The map transform is fast and deterministic: Rust candidates are
  byte-identical across isolated runs on all 7 role tasks (same
  `output_sha256`), 1.2–4.1× faster than the frozen Python baseline.
- Adaptive subdivision works on real skew: 64 partitions at depth 1 over the
  four >1M-term cells, bindings reconcile exactly against map markers.
- Selective reduce reconciles selected+discarded bindings per row group
  against every input proof; read amplification measured at 1.18–23.9.
- The proof frame (exact count + dual-lane mod-2^256 digest sums) is
  commutative/associative, so it survives any fan-in topology.
- The Worker already has a bounded, edge-cached, coalescing range reader
  (`crates/geocoder-worker/src/range_reader.rs`) shared by v2 readers, and the
  repo already operates a 4096-shard hash-prefix-sharded index in production
  (`scripts/build_id_index.py`). Both are direct precedents for the head fix.

