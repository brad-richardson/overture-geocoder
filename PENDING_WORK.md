# Pending Work — 2026-07-19

This is the active roadmap. Completed PR history is intentionally omitted unless
its result constrains the next decision. The implementation baseline is
`775ad21` (`main` after #125). The older 2026-07-17 sequencing and evidence log
remain below for provenance; this handoff supersedes their stale NEXT labels.

## Executor implementation complete — 2026-07-19

The bounded global-v2 family executor is implementation- and review-complete
on `agent/global-v2-executor`. No planet build, production catalog write, R2
data write, Worker deployment, or authentication flow has been started from
this branch.

- The address and Places map/reduce/head paths are disk-bounded, stream remote
  fragments one at a time, validate exact completion sets and remote SHA/byte
  evidence, preserve permanent sticky lineage, and perform one final streaming
  whole-slice verification.
- The hosted workflow uses exact typed execution confirmation, create-only
  immutable writes, serialized family matrices under one concurrency cap,
  resumable phase markers, step-scoped secrets, an isolated preview Worker,
  signed family-specific smoke queries, and verified preview cleanup. Execute
  mode rejects GitHub reruns; recovery is a fresh dispatch with cumulative
  prior runner minutes and a renewed confirmation binding every budget input.
- The read-only core preflight validates the exact reusable unpromoted core
  `2026-07-18.0` before its manifest identity can enter the canonical request.
  The executor never writes `v2/catalog.json` or `v2/releases/*`.
- Final validation is green: 1,045 Python tests; Rust formatting, clippy, 103
  core tests, 13 integration tests, 115 Worker tests, one doctest, and the wasm
  check; changed-file Ruff; actionlint; and `git diff --check`.
- Independent whole-diff and Worker/publication reviews found no remaining
  dispatch blocker. Cross-root v1/v2 retention remains a hard
  **before-publication** task; do not run exceptional cleanup against the
  retained `2026-07-18.0` core while the unpublished family build exists.

Next: land this implementation through the PR/review/green-CI/merge loop. After
merge, dispatch only the credentialed read-only core preflight and request
preparation/dry-run evidence. Present the exact request hash, versions, task and
reducer matrices, concurrency, runner-minute/cost caps, and storage/verification
plan for operator sign-off. Do **not** dispatch execute mode before that sign-off.

## Paused executor handoff — 2026-07-19

Work paused at the operator's request before quota exhaustion. No global build,
R2 write, Worker deployment, authentication flow, or v2 catalog publication was
started. The worktree is intentionally dirty on
`agent/global-v2-executor` (tracking `origin/main`); none of the executor work
below is committed or pushed yet.

### Merged foundation

- PR #125, `Prepare bounded global v2 family build`, merged green as
  `775ad21ced3aed565432404ff2c70f97364834bb`. It closed the pre-executor
  trust/scaling gaps: exact retained request lineage; canonical Places/address
  inventories; bounded mappers; strict address WKB and task identity; fragment,
  spill, Worker memory/cache, and readiness caps; streaming family finalization;
  and direct Worker SHA/byte verification of `head.phrp`.
- The foundation PR's seven CI jobs passed. Its one CI correction replaced 12
  `workflow_dispatch` inputs (GitHub permits at most 10) with one validated
  `family_inputs_json` input.

### Canonical planet preflight evidence

Read-only canonical inventories passed locally with CPython 3.11.14,
PyArrow 25.0.0, NumPy 2.3.5, and
`.github/requirements-hosted-rowgroup.txt` for Overture `2026-06-17.0`:

- Addresses: inventory SHA
  `6a306fc9937dac82602dbc5233952c1f74fdb0f7467ad4cc38dcc559dfc9d34e`,
  source-inventory SHA
  `aa196a40730676efef70413d45bdcadaada3df07c94599b954efd38cd096ec37`,
  schema fingerprint
  `05260dc6878478fe750a82ad3fb9ddd2fdffcda3f25c00f950acfccca132d7e0`,
  473,576,753 rows, 32 objects, 8,704 row groups, and 127 bounded map tasks.
  The largest task has 3,999,690 rows and about 296.8 MB selected uncompressed
  bytes. Total selected compressed input is about 16.7 GB.
- Places: inventory SHA
  `b1830aee50ea61395cda14f6b04888d846dcba12f24967c7ab52c64fe5944eff`,
  schema fingerprint
  `49453ed2b28a7940fe6664b13ec89631fbee2d98efdad0ff8ab1a26972212a5a`,
  75,642,289 rows, 16 objects, 5,120 row groups, and 89 bounded map tasks.
  The largest task has 999,805 rows (median 987,454).
- Both matrices fit the retained 128-source-task family cap. No shard creation
  was dispatched.

### Reusable legacy core

- Successful dry-run Actions run `29624600543` built and remotely finalized
  immutable, unpromoted legacy core `2026-07-18.0` from the same Overture
  release `2026-06-17.0` at checkout
  `db97c0bc7b0c1d515e26bf24b98e666caea12e21`. It verified 262 forward shards,
  253 reverse shards, and all 4,096 ID shards; promotion was deliberately
  skipped.
- The exact SHA of `2026-07-18.0/release-manifest.json` was not present in the
  Actions log and the unpromoted prefix is not publicly readable. The eventual
  credentialed **read-only** workflow preflight must download it from R2,
  validate version/release/completeness, compute its SHA-256, and freeze that
  identity in the retained request. Do not authenticate interactively or infer
  the digest.

### Local executor implementation (uncommitted)

- Address plan/reducer modules now validate the exact 127-task completion set,
  stream remote fragments one at a time through an argv-only fetch adapter,
  derive stable sticky partitions, assign replaceable reduce jobs, build the
  existing `.aidx`/`.adat` format, and finalize the Worker collection plus exact
  global duplicate fanout. Predecessor validation permits maximum-hash-bit
  growth and row-cap retuning while forbidding bit-depth decreases and retaining
  sticky split IDs.
- Address reduce jobs were changed from heaviest-leaf scattering to balanced
  contiguous `(country, hash_start)` ranges. In the regression fixture, a broad
  fragment spanning 16 leaves is referenced by at most two adjacent jobs rather
  than up to 16 jobs, avoiding planet-scale remote download amplification.
- Places plan/reducer/head modules implement exact map fan-in, sticky
  `world-quadkey-v1` planning, replaceable reduce jobs, streaming remote fragment
  reads, existing PCSH/PCAT output, and a bounded two-pass global PHRP head.
  Predecessor work was being relaxed so maximum level can grow, row caps can be
  retuned, and tokenizer rotation does not discard geometric split history.
- `global_v2_executor.py` and `finalize_rebuild.py` contain retained-request,
  runtime, cost/coverage, exact task/phase completion, immutable slice publish,
  and remote-verification primitives. The first draft's roughly four full-fleet
  readbacks were identified as a scaling bug; keep per-upload readback plus one
  final whole-slice verification, followed only by exact key listing and marker
  verification.
- The Worker and `v2_release_manifest.py` locally support an isolated preview
  catalog only at `smoketest-v2/<safe-run-id>/catalog.json`, only with
  `ENVIRONMENT=smoke|preview`. Its release manifest is a sibling preview object,
  preview history is forbidden, and no object is written under `v2/releases/`
  or `v2/catalog.json`.
- New/modified executor files include
  `scripts/global_v2_{address_plan,address_reduce,places_plan,places_reduce,places_head,executor}.py`,
  `scripts/r2_fragment_fetch.py`, `scripts/finalize_rebuild.py`, the Worker v2
  preview changes, and their focused tests. `PENDING_WORK.md` also contained
  pre-existing local edits; stage files explicitly when resuming.

### Exact paused validation state

- Latest focused run: 50 passed, 4 failed across address executor, Places
  executor, control-plane executor, R2 fetch, and v2 release-manifest tests.
- Two failures are the same Places issue: `execute_reduce_job`'s new peak
  scratch/workspace evidence does not yet satisfy `validate_reduce_report`.
  Align producer and validator, retaining hard scratch/workspace caps.
- Two failures are the same R2 key issue: `safe_key` must reject empty and dot
  path components (`a//b`, `a/./b`) in addition to absolute paths, `..`, and
  shell metacharacters.
- The manual dispatch workflow, preview Worker deploy/query/cleanup wiring,
  read-only legacy-core SHA preflight, and tests for that workflow were not yet
  added. Therefore this branch is **not dispatch-ready** and should not be
  pushed as a PR until those are complete and reviewed.

### Resume order and parallel ownership

These lanes can resume in parallel; only the final review/PR depends on all
three:

Global-v2 execution resume is always a **fresh `workflow_dispatch`**, never
GitHub's **Re-run jobs** action. Execute mode accepts only
`github.run_attempt == 1`. For a resume, retain the exact request, enter the
cumulative minutes spent by every earlier attempt as `prior_runner_minutes`,
and regenerate the typed confirmation: it binds the request and mode plus
`max_parallel`, `max_total_runner_minutes`, `max_estimated_cost_usd`, and
`prior_runner_minutes`. This prevents a rerun from silently reusing a zero
prior-minute budget.

1. **Places lane:** fix the two scratch-evidence tests; finish and test relaxed
   predecessor compatibility (maximum-level growth, row-cap and tokenizer
   changes; reject depth decrease/scheme/min-level drift); retain exact remote
   SHA/byte checks and peak disk evidence.
2. **Orchestration lane:** fix `safe_key`; finish a main-only, typed-confirmation
   workflow with resumable phases, create-only R2 writes, exact completion
   matrices, pinned Python/PyArrow dependencies, a pre-job free-disk gate, and
   isolated preview Worker smoke/cleanup. Record GitHub runner image provenance,
   but do not make staged `ImageVersion` rollout nondeterministically break one
   matrix unless that non-resumability is explicit. If reusing
   `jlumbroso/free-disk-space`, pin commit
   `54081f138730dfa15788a46383842cd2f914a1be`, never `@main`.
3. **Address lane:** finish the bounded bucket-count refactor and rerun locality,
   predecessor, reducer, duplicate-fanout, replay, and remote-streaming tests.
4. Run the full Python/Rust/wasm/actionlint/Ruff/diff-check suite, then perform an
   independent whole-diff review focused on permanent lineage, remote exact-set
   verification, hosted disk/time limits, create-only semantics, and zero
   production catalog writes.
5. Use the standard PR loop: explicitly exclude unrelated local pending-work
   edits from the implementation commit as appropriate, push, open the PR,
   address review, require green CI, and merge.
6. After merge, run only the credentialed read-only preflight needed to obtain
   the core manifest SHA and build the exact canonical request. Present the
   request hash, slice/build versions, 127+89 map matrices, reducer counts,
   concurrency, runner-minute/cost caps, and expected storage/verification work
   for operator sign-off. **Do not dispatch the planet build before that explicit
   sign-off.**

## Current handoff (2026-07-19)

### Completed today

- USA-scale evidence closed green against real Overture `2026-06-17.0` data:
  18,014,140 full-CONUS Places rows produced 13 deterministic serving shards
  and a verified 2,079,901,541-byte family; all 33 US-dominant address tasks
  reconciled 130,996,768 input rows to 128,285,220 retained rows and
  16,830,144,071 fragment bytes. The combined 18,910,045,612-byte signal passed
  the 40 GB gate. The address scope is deliberately an upper bound, not an
  exact row-filtered US export, and the result was non-promoting.
- Atomic v2 release/catalog composition merged in #120. A v2 release binds one
  exact legacy divisions/ID core plus optional same-Overture-release family
  manifests. Public responses carry both `overture_release` and
  `geocoder_build`; `v2/catalog.json` is separate from the historical v1 root.
- Stable Places ownership merged in #121: `world-quadkey-v1`, level 6-12,
  1.5M-row split cap, stable `q-{quadkey}` identities, and sticky split history.
  Historical ordinal shards remain readable but are not the v2 layout.
- Stable address ownership and strict serving artifacts merged in #122:
  `country-fnv1a-high-bits-v1`, one-million-row split cap, complete normalized
  eight-field key ownership, sticky splits, explicit empty ranges, and separate
  `.aidx`/`.adat` Worker objects.
- Unified v2 Worker API merged in #123 and deployed successfully:
  `/v2/forward`, `/v2/reverse`, and `/v2/features/:gers_id`. The former
  `/address`, `/__address-page-spike`, and `/__places-page-spike` routes and
  their isolated smoke deployments were removed. There is no batch endpoint.
  The first address capability is structured exact forward; reverse currently
  serves divisions; Places supports bounded routed or global-head forward.
- Global-build contract preparation merged in #124. The deterministic request
  records families-only scope, reuses the legacy divisions/ID core, keeps
  compute-task numbering separate from permanent shard identity, requires a
  global Places `head.phrp`, and makes publication a separate explicit step.
  The preparation workflow has no cloud credentials or data-plane action. It is
  a baseline contract, not yet a complete executable request: it still needs to
  pin the exact core identity, slice namespace, inventories/schema fingerprints,
  predecessor lineage, and global-head policy.
- Full local validation passed (861 Python tests plus Rust fmt/clippy/tests and
  actionlint), both PR CI suites were green, the final main-head Worker deploy
  and post-deploy verification passed, and the isolated v3 ID smoke queried
  current and historical IDs then cleaned up its Worker/R2 state.
- No global Places/address shard build was dispatched and no v2 catalog was
  published. Live v2 routes correctly fail closed with structured 503
  `release_unavailable` until that happens.

### Product and storage decisions currently in force

1. Build Places and addresses as a new families-only slice; do not rebuild or
   reshard divisions for v2. The v2 release references the verified historical
   core cryptographically, but retention is not safe until v1 cleanup also
   protects every core/slice prefix reachable from the v2 catalog.
2. Serving partitions are data contracts; map/reduce job assignments are
   replaceable execution details and must never appear as shard identities.
3. Places forward cannot be advertised unless both `catalog.pcat` and the
   global `head.phrp` occur in the hashed family artifact set.
4. Address structured lookup preserves every duplicate/ambiguous exact-key
   candidate up to the explicit 512-result safety cap. Free-text address search
   and address/POI reverse need separate later indexes.
5. Public `poi` is canonical; `place` remains an input alias. The API borrows a
   standard forward/reverse/features shape but does not claim wire compatibility
   with Mapbox, TomTom, or Nominatim.
6. A build first creates an unpublished immutable slice. Constructing and
   swapping `v2/catalog.json` is a later reviewable operation, not a side effect
   of the data build.

### Next: first global v2 family build

The foundations are resolved, but several public/lineage decisions and the
global data-plane executor are not dispatch-ready. The existing
`global_build_manifest.py` numbered reduce partitions predate stable family
ownership and must not be treated as serving shard IDs.

#### Must close before dispatching the global build

1. Pin the exact legacy core version and manifest SHA, its matching Overture
   release, and the immutable `slice-*` source prefix in the request. The first
   family build cannot select an arbitrary newer Overture release while also
   reusing core: v2 composition intentionally requires all families and core to
   share one Overture release.
2. Freeze canonical source inventories and real schema fingerprints (required
   columns and types), not a release string labelled `schema_version`.
3. Make rejection accounting total and named. Addresses must reject/reconcile
   blank or invalid country and invalid UUID values before stable planning.
   Places must reject/reconcile invalid/missing GERS IDs, geometry/coordinates,
   names, and status; production code must never synthesize `__row_N` IDs or
   coerce missing coordinates to `(0,0)`.
4. Publish `families/addresses/partition-plan.json` as a hashed durable artifact.
   Stamp an explicit null predecessor family-manifest digest on build 1 and
   require the exact latest compatible predecessor thereafter. Bind the same
   predecessor identity for Places even though `catalog.pcat` already carries
   its sticky split cells.
5. Freeze the global-head admission/ranking version and its minimum-candidate,
   famous-cap, result-cap, and predecessor/provenance settings in the request
   and head directory.
6. Implement bounded map tasks that emit reconciled, content-addressed maximum-
   level spatial/hash fragments, then aggregate exact retained counts and derive
   stable Places and address serving partition plans.
7. Assign those stable partitions to at most 256 replaceable reduce jobs; build
   the existing `.pcsh`, `.aidx`, and `.adat` formats and the global Places
   head without coupling object names to runner topology.
8. Verify every remote object size/hash and family total, finalize an
   unpublished families-only slice, and smoke `/v2/forward` through the real
   Worker using an isolated preview Worker/bucket or a new guarded v2 preview
   catalog mechanism. No v2 catalog override exists today.
9. Inspect global completeness, rejection accounting, skew, head relevance,
   bytes, build time, range-read caps, and cleanup evidence. Only then construct
   a v2 release/catalog candidate. Promotion remains a separate human decision.

#### Must close before first publication

1. Implement v2 publication and recovery: create-only uploads plus readback for
   the slice manifest and `v2/releases/{build}/release.json`; backup and
   `If-Match` compare-and-swap for `v2/catalog.json`; exact readback, live smoke,
   CAS rollback, and crash recovery. Candidate generation alone is not a
   publisher, and unguarded `aws s3 cp` is not acceptable for the mutable root.
2. Disable version-prefix deletion until retention walks both catalog roots and
   transitively protects every core and family slice referenced by every live or
   rollback v2 release. Wait out both catalog caches before object deletion.
3. Decide `/v2/features/:gers_id` semantics before discovery. It currently
   returns bbox/locator metadata, not hydrated Overture geometry/properties;
   emits a non-GeoJSON bbox object instead of `[xmin,ymin,xmax,ymax]`; and can
   silently degrade locator failure to a bbox-only 200. Either rename it to an
   ID/locator surface or explicitly version the partial/full-feature contract,
   then make locator status and historical-ID semantics unambiguous.
4. Expose `X-Geocoder-Build` and `X-Overture-Release` through CORS, not only
   `X-Data-Version`.
5. Carry explicit runtime artifact identities for both Places `catalog.pcat`
   and `head.phrp` in the v2 release instead of deriving the head as an
   unrepresented sibling key.
6. Define the v2 rollback/retention depth in the publisher. The Worker accepts
   at most 64 catalog releases; candidate construction currently has no cap.
7. Prove exact retained-row-to-leaf coverage, measure global address duplicate
   fanout against the 512 response cap, and review global-head/boundary recall
   evidence before the human catalog-swap decision.

### Additional findings — independent contract review of #120–#124 (2026-07-19)

A second read-only review confirmed the lists above and adds the following.
Items 1–3 must close before the first global build dispatch because they are
baked into permanent split lineage; the rest before first publication.

Frozen-parameter sign-offs (before dispatch):

1. Places `maximum_level` is frozen at 12 by a lineage *equality* check
   (`build_places_region_shards.py:293`), and a level-12 cell over the 1.5M cap
   aborts planning with no escape hatch (`places_partition.py:150-154`) even
   though the format supports level 15. Before the first build freezes this,
   either count the densest global level-12 cells against the cap on
   `2026-06-17.0`, or relax lineage to allow `maximum_level` to grow.
2. Address `maximum_hash_bits` is likewise frozen by lineage equality
   (`address_partition.py:277-282, 361-362`), so the current *default* of 16
   silently becomes the permanent per-country split ceiling. Pin it as a
   deliberate value or allow growth. Related cliff: any single exact key with
   more than 1M duplicate rows shares one full hash, can never split, and
   hard-fails the build with no post-publish tuning knob.
3. Places lineage requires exact `tokenizer_version` equality
   (`build_places_region_shards.py:288`) although splits depend only on row
   counts and geometry, and the Worker itself reads a legacy tokenizer. As
   written, any tokenizer rotation discards split history. Relax before the
   first history exists.

Client-visible surface (before publication, in addition to the features fix
above):

4. Split `capability_unavailable` into distinct codes for 503 temporary
   (family/index absent — retry later) and 400 structural (free-text address,
   reverse poi/address — do not retry); clients cannot branch on it today.
5. Decide and set `Cache-Control` for every v2 response including errors —
   `versioned_response` and `json_error` currently set none, so intermediaries
   may cache across a catalog flip or cache the pre-publish 503 through the
   publish itself.
6. Remove or gate `properties.source` (`source_object_index`/`source_row_group`
   /`source_row_index`, `v2.rs:794-798`) — undocumented internal parquet
   coordinates in structured-address responses; reconsider `filename`/
   `overture_path` as permanent public feature fields.
7. Make `metadata` deterministic and stable: `metadata.types` serializes from a
   `HashSet` (nondeterministic order), and the degraded places-unavailable path
   drops `types`/`proximity` entirely.
8. Document the full error taxonomy (`capability_unavailable`,
   `candidate_overflow`, `invalid_request`, 413/404 statuses) and every
   response field (`ambiguous`, `coverage`, `address_levels`, `source`) in
   `docs/api-v2.md` before the surface freezes; also validate
   `overture_release` format in the release manifest/catalog (today only
   `_require_string`, vs `RELEASE_RE` in the build request).
9. Pick one address normalization version string before it appears in public
   responses: v2 renamed `nfc-uniws-asciilower-1` to
   `nfc-uniws-collapse-ascii-lower-1` with zero behavior change
   (`address.rs:39-40`), spending the version signal on cosmetics.
10. Reverse currently promises "zero or one feature" (`docs/api-v2.md`); drop
    the wording or scope it to divisions now, before POI/address reverse breaks
    the cardinality and homogeneous-props shape.
11. `relevance` is incommensurable across families (divisions `importance/2`
    vs POI `confidence`) yet is the merged sort key and a public field; at
    minimum document per-type semantics before clients depend on it.
12. Strengthening publication item 5: the `head.phrp` requirement is enforced
    only in `build_release_manifest` (`v2_release_manifest.py:467`), not in
    `validate_release_manifest` or `verify_release_sources` — catalog admission
    re-proves sources precisely because it distrusts self-digests, yet would
    admit a hand-authored release advertising Places forward without a head.
    Enforce the dependency in the verify path, not only the constructor.

Rollback semantics — DECIDED 2026-07-19 (owner): **roll-forward-only.**
Reverting a bad `latest` means publishing a strictly newer `geocoder_build`
that repeats the prior known-good composition; there is no active/rollback
pointer in `CATALOG_SCHEMA`, and the builder's monotonicity check
(`v2_release_manifest.py:822-823`) plus the Worker's monotonic-descending
validation stay as-is. Publication item 1's "CAS rollback" must therefore be
implemented as roll-forward re-publication, not backup restoration — the
backup exists for crash recovery of an interrupted swap, not for reinstating
an older `latest`. Rationale: the v2 client surface is tiny pre-adoption and a
brief blip during roll-forward is acceptable.

Core reuse and storage audit (2026-07-19, follow-up question):

- Divisions and ID shards are preserved as-is. The v2 release binds the legacy
  core at its existing v1 prefix — `{legacy_version}/collection.json`,
  `reverse-collection.json`, `id-collection.json`, `router.db`, and the exact
  `id-index/{000..fff}.parquet` set (`v2_release_manifest.py:189-197,
  572-580`) — and the Worker serves v2 through `release.core_version()` via
  the same loader path and objects as v1. Nothing rebuilds, reshards, or
  copies the core; `v2/` holds only small JSON metadata.
- No shard type is ever written to two locations. Family artifacts are
  referenced in place under their slice prefix (entrypoint keys are rewritten
  to `{source_version}/…`, never copied), and the ID index exists exactly once
  per core version.
- The one real duplicate-storage vector is **cross-root version skew, not
  copying**: v1 rebuilds a full new core (including 4,096 ID parquets)
  monthly, and v1 retention keeps only the newest `--keep` versions
  (`prune_catalog.py`). Because a v2 release must share one Overture release
  across core and families, v2 cannot adopt a newer monthly core without
  rebuilding the families from that same Overture release. So either (a)
  families are rebuilt each month and both roots converge on one core
  generation, or (b) v2 pins an aging core that retention must protect after
  it falls out of v1's keep window — roughly one extra full core generation of
  R2 storage per month of lag. Bounded and acceptable short-term, but the
  publisher/retention design (publication item 2) should surface this skew
  explicitly, and the exact per-generation core byte size should be measured
  once from an R2 listing to price the lag.

Reviewed and judged sound as decided: sticky-splits-never-merge, tunable row
caps with frozen split history, `q-{cell}`/`a-{country}-h-{bits}` identities,
direct `{object_key, bytes, sha256}` entrypoint identity, hash collisions made
harmless by exact-key in-page matching, the 512-cap/413 no-truncation
semantics, no batch endpoint, `poi`/`place` aliasing, and structured-address
dispatch leaving a compatible door open for free-text later. Non-blocking
notes: v2 address shard selection linearly scans all items/empty ranges plus
two more country scans per request (`address.rs:496-521`) — fine regionally, a
serving-scalability smell at global collection sizes; and the in-memory Places
ingestion path maps missing coordinates to `(0,0)` while the streaming path
raises — the streaming path is the global-build path, and dispatch item 3
already requires rejecting coerced coordinates outright.

### One-way-door checklist for adversarial review before the first publish

- `/v2/features/:gers_id` is confirmed to need a product decision and GeoJSON/
  degradation fix before clients adopt the path;
- whether the v2 release manifest carries enough direct identity for every
  runtime dependency (especially `head.phrp`) rather than relying on a derived
  sibling key plus the bound family manifest;
- first-build split-history semantics and how a later build proves it loaded the
  immediately preceding compatible Places catalog/address plan;
- permanent R2 namespace/object naming, cache immutability, content replacement
  protection, rollback reachability, and retention across both v1 and v2 roots;
- source completeness and rejection accounting for globally unlocated Places,
  malformed coordinates, missing address country/street/number, duplicates,
  synthetic country codes, and Overture schema drift;
- whether address normalization/tokenization/format version fields and Places
  tokenizer/partition versions are sufficient to prevent cross-runtime or
  cross-release mixing;
- whether global-head admission/ranking and one-shard located routing create
  product promises or recall seams that would be expensive to undo after
  publication; and
- promotion, rollback, degraded-family behavior, and client-visible semantics
  when one optional family is absent, corrupt, or temporarily unreadable.

The cross-boundary one-shard Places recall seam, better ranking/head tuning,
free-text/fuzzy/reverse expansion, and worker isolation remain safe deferrals:
they can change behind a later geocoder build without changing shard ownership.

### Deliberate non-blocking deferrals

- free-text address parsing/search, reverse addresses, reverse POIs, fuzzy or
  semantic search, tail-complete global POI enumeration, and batch requests;
- worker-isolation refinements beyond the shared strict range-reader; and
- cleanup of historical prose below that refers to the removed experimental
  routes. Those sections are retained as an evidence record, not current API
  documentation.

## Sequencing (2026-07-17)

Ordered plan to reach a meaningful non-promoting Places/address shard build
test. Steps 2 and 3 are independent and can run in parallel; step 4 must not
start before both, because its inputs are the partition rule (step 3) and a
Places relevance verdict (step 2).

1. **Dispatch the rebuild dry-run (`promote=false`).** DISPATCHED 2026-07-17
   (Actions run 29586616677, `confirm=REBUILD promote=false`; note a manual
   dispatch without the typed `confirm` input silently skips every job and
   reports success). Validates the #96/#97 finalize fixes over a real v3
   build before the July 25 scheduled rebuild.
   OUTCOME: first attempt **failed at finalize-release** — every build/id
   job succeeded, then `finalize_rebuild.py` died at import
   (`scripts/common.py` top-level `import duckdb`, introduced in e0591c2;
   the finalize-release job intentionally installs no duckdb). Fixed by
   PR #104 (lazy duckdb import + no-duckdb regression tests; finalize stays
   dependency-thin).
   RESOLVED 2026-07-18: re-dispatch as `2026-07-18.0` (run 29624600543)
   **green end-to-end** — build, all id stages, finalize-release,
   post-finalize (the promote+smoke step is gated on `promote=true` and was
   correctly skipped for this `promote=false` dry-run). The #96/#97/#104
   finalize path is validated over a
   real v3 build; the July 25 scheduled rebuild is cleared. Two notes:
   (a) the workflow fails closed on a version-prefix collision rather than
   resuming, so the first attempt's `2026-07-17.0/` prefix is orphaned,
   non-promoted, undiscoverable data in R2 — schedule an R2 cleanup pass;
   (b) same-version resume via dispatch input is not supported by design.
   **DONE.**
2. **Places relevance iteration.** Famous-unique head admission (the last
   relevance STOP seed; design:
   `docs/plans/2026-07-17-famous-unique-head-admission.md`), then the
   remaining cold-gate failure `relevance_chain_name` (records-stage layout
   or coalesce-gap change plus concurrent reads). Re-dispatch the smoke and
   re-score all six seeds.
   2026-07-17 chain_name status: records-stage layout merged as PR #102 and
   coverage/contract pinning as PR #103. The post-merge smoke (run
   29591168866, fresh fixture shards) shows bytes fixed (1.10 MB → 307,681 B,
   inside the 512 KiB gate) but cold reads WORSE (15 → 19 vs the 8-read cap)
   and cold latency unchanged (1.42 s vs the 1.0 s gate).
   Diagnosis (2026-07-17 per-stage attribution of run 29591168866): NOT a
   Worker bug — reader/model parity holds. (1) The model's 8-read budget
   omits the 2 catalog-routing reads the live gate counts; even the model's
   best case is 10 live reads, over the gate before any scatter. (2) The #102
   design premise is false: global-rank records layout interleaves a chain's
   branches with every similar-confidence place, so the 10 served records
   coalesce to 9 physical reads, and doc-id-keyed record_index drags 215 KB
   of gap into 2 reads; the gap-sweep fixture was engineered with uniform
   17.7 KiB spacing that cannot reproduce real scatter. (3) Arithmetic wall:
   a two-clause catalog_context query spends 8 structural reads
   (catalog 2 + directory 2 + lexicon 2 + postings 2) before record stages —
   no records layout can pass the current gate. Required next change: fold
   record stages into already-fetched reads (rank-ordered doc-ids so
   record_index is rank-monotone; chain-local record clustering, e.g.
   (brand-key, rank)) AND re-derive the gate read budget to name the 2
   unavoidable catalog reads; also fix the sweep fixture to real scatter and
   add the +2 to the model budget. Directory stage (2 reads / 45 KB every
   query) is the next byte target.
   Separately, three cases (`relevance_local_name` 1.046 s, `shard_prefix`
   1.045 s, `shard_early_exit_sentinel` 1.119 s) fail only the 1.0 s
   cold-latency line by 4–12%: all 8-read cases sit on the same ~6-8
   sequential data-dependent round-trip floor (passing peers span
   723–1059 ms worker time) — R2 jitter straddling the gate, not
   regressions. The floor itself is the concern: stage-count reduction is
   the only lever that moves it.
   2026-07-17 status: head admission implemented and MERGED as PR #100 (CI
   green, adversarially reviewed, budget gates hold, zero regressions). The
   famous_unique seed itself is data-blocked on release 2026-06-17.0: the
   東京タワー feature has `names.common = None` (verified against raw S3),
   so zero fixture places hold both `tokyo` and `tower` and the exact AND is
   provably empty; independently, the quantized-confidence fame proxy
   saturates (37,012 places at qconf ≥ 254 vs the tower's 253). Decision
   (2026-07-17): merged the machinery; the seed is to be re-adjudicated —
   pick a rule-based replacement famous entity supportable by the data
   and/or verify a newer Overture release carries the alias. A
   saturation-resistant fame signal (e.g. alt-name language count) is a
   flagged design gap for planet scale regardless.
3. **Address stratified multi-task sweep.** DONE 2026-07-17 (PR #99, run
   29585075948): 12 stratified tasks, 12/12 complete, byte-identical
   local-oracle reduce on every task, rows reconciled vs inventory.
   Retention min/median/p95/max: 40.29 / 99.74 / 100.00 / 100.00 % — the
   Mexico anchor's 40.29% is a regional outlier; CJK and Latin-Europe strata
   retain ≥99.2%. Fragment output 117.1–161.5 B/retained row; peak RSS
   ≤869.6 MB; retry amplification 2.02–2.83 (max at the sparse tail).
   Evidence: `benchmarks/2026-07-17-address-stratified-sweep-report.md`
   (PR #101). The partition/normalization-contract review (open decision 2)
   is now unblocked. Still open from this step: the 941 KB cold side-index
   decision.
4. **The shard build test.** Release-versioned family manifests, the
   object-level publication guard, both families integrated as optional
   non-promoting families in the shared finalizer, then the design doc's
   two-region-per-family slice through the real `releases/{version}/` layout
   with remote verification and a scale report.
   2026-07-18 status (US-NE regional track; scope + decisions in
   `docs/plans/2026-07-18-us-ne-regional-deploy-scope.md`, merged #105):
   - Family manifests DONE (#107): `overture-global-family-manifest-v1`
     with lineage, format/tokenizer/normalization versions, region bbox +
     scope mode, artifact hashes, canonical-JSON self-digest; wired to the
     fan-in candidate, `promotion_eligible: false`.
   - Object-level publication guard DONE (#108): server-side create-only
     (If-None-Match) and expected-current CAS (If-Match) PUTs in the
     finalize path — R2 enforces both; 412 aborts, never retried;
     idempotent same-content backups; CLI capability check fails fast.
   - Address bbox-scoped producer DONE (#106): per-row-group bbox stats +
     bbox-pruned task planning, `bbox_scope: row_group_approximate`
     (superset semantics), default path byte-identical. Note: address row
     groups are not spatially clustered, so pruning selectivity is modest.
   - Production `/address` route DONE and DEPLOYED (#109): structured
     8-field exact lookup, all-candidates ambiguity (512 bound → 413),
     out-of-coverage vs not-found split, stable 404
     `address_family_unavailable` until the family enters the catalog;
     existing routes proven byte-identical; wasm ~1.34 MB gzip. The
     explorer-safety contract (undocumented `types=` default excluding
     `place`; catalog additive discovery) is verified and documented in the
     scope doc.
   - Side-index decision CLOSED: Option C (no format change) with a
     recorded guardrail — no served address shard above ~4M rows (index
     22.5% of the 4 MiB reader cap at 4M rows); two-level index (Places
     `lexicon_blocks` pattern) triggers past that or at planet scale.
   - Region sizing measured (release 2026-06-17.0): US-NE box 4,133,950
     places (~481 MB, 3-4 shards); CONUS 18,014,140 (~2.1 GB); global
     75,631,061. All-US address remains 33 tasks / ~131.0M rows.
   - Slice COMPLETE 2026-07-18 — step 4's exit artifact exists:
     - Optional non-promoting families in the shared finalizer DONE
       (#110: `verify --families` allowlist, manifest-derived expected
       sets fail-closed incl. path-traversal guard, `publish-family`
       with data-before-marker ordering; #113 added `verify-families-only`
       for core-less slice versions and `SLICE_VERSION_RE` so a slice
       version can never collide with a production release).
     - NE address region rehearsal GREEN (run 29629389486): 34,112,192
       rows / 9 tasks, exact reconciliation, 8,118 of 8,704 row groups
       pruned (93%), family manifest verified. Largest task 3,983,695
       rows — just under the 4M side-index guardrail, which is therefore
       load-bearing for NE-scale shards.
     - NE Places region build GREEN (runs 29629499331+29630398903, cwd
       fix #114): 4,133,950 places (matches the count-only scan
       exactly), 3 shards + catalog, 479.6 MB verified, deterministic
       double-build; packed head omitted `over_reader_caps` at region
       scale — context-free head queries need per-shard heads or raised
       caps before any regional Places serving.
     - Two-region-per-family slice GREEN (`slice-2026-07-18.1`, run
       29631677440, after first-dispatch fixes #115): both families
       published through the one guarded finalizer path into
       `slice-2026-07-18.1/families/{family}/`, verify-families-only +
       independent downloaded hashes + negative catalog probe passed.
       Scale report (retained artifact `slice-scale-report-29631677440`):
       places NE 4,133,950 rows / 479,599,903 B + dc-metro 151,187 /
       18,035,207 B, build 778 s, publish 43 s; addresses NE 34,112,192
       rows / 9 artifacts / 4,650,368,299 B + dc-metro 1,788,974 /
       254,896,248 B, build 2,137 s, publish 240 s; verify 3 s.
       NOTE: NE address serving bytes (~4.65 GB fragments-as-published)
       dominate the region and put naive all-US address publication
       (~18 GB) inside but near the 40 GB combined gate — the compact
       serving-page format (35 B/row measured) rather than raw reduce
       fragments is the planet-scale path.
     - USA scale-signal dispatch path READY (not yet run):
       `.github/workflows/usa-scale-signal.yml` composes the existing verified
       region/address workflows into a typed-confirmation, main-only run. It
       pins the full CONUS Places box plus all 33 US-dominant address tasks,
       cleans every run-unique R2 prefix, and retains one combined fail-closed
       report against the 40 GB measured-byte gate. The address fleet is
       explicitly an upper bound, not an exact country-filtered export.
       Recovery mode can combine retained evidence from separate successful
       Places/address run IDs, so transient hosted-runner loss does not force a
       second 33-task address fleet.
     - NEXT: dispatch that USA scale-signal pass. Worker isolation is deferred
       by owner preference. After the data-scale evidence, continue the Places
       serving gates (chain_name read-chain redesign, comparator relevance
       panel, bounded located ranking + the measured multi-shard route_point
       seam recall hole from #112's review, per-shard head strategy).
5. **After July 25:** verify the promoted rebuild's lineage/reverse baseline,
   then re-size exact-country work. It does not queue-jump the family work.

## Current state

- Production is healthy on data version `2026-07-13.0` and remains
  division-only. No address, Places, street, or exact-country experiment is
  discoverable from the production catalog.
- The shared range-reader, compressed address pages, compact Places shards,
  packed Places head, global address task inventory, and hash-verifying R2
  store are merged.
- Three current-release address tasks passed source accounting, exact reducer
  checks, lossless page decode checks, and the hosted runner envelope. Structured
  retention varied from 40.29% to effectively 100%; regional completeness is a
  product constraint, not a single global coefficient.
- The isolated address Worker returned the exact 92-candidate producer digest.
  Its cold request used three R2 reads and 954,362 bytes in 392 ms; warm requests
  used three cache hits, zero R2 reads, and a 174 ms median. The 941,745-byte side
  index dominated cold transfer while page materialization stayed below 91 KB.
- The isolated Places Worker passed exact producer/reader equivalence through
  real R2. Packed-head cold access used four reads and 132 KB; a three-shard
  fallback used 15 logical ranges and took 1.347 seconds on its first observed
  request. Places remains disabled by default.
- The remote R2 rehearsal passed partial upload, repeated verified resume, empty
  restore, stale-local repair, and cleanup. The small rehearsal now belongs in
  path-filtered `main` smoke CI; the large external-data jobs remain manual.
- A versioned Places routing catalog, one-shard context/point routing,
  multi-clause field masks, packed-head-only context-free path, independent
  cold namespaces, full-projection oracles, and technical gate classification
  are implemented in #87. Distance is diagnostic only: the bounded result
  window is not yet a complete nearest-candidate ranking.
- Both main-only measurement workflows were dispatched for the first time on
  2026-07-16 against real R2/Cloudflare/Overture (release `2026-06-17.0`).
  Address (US task 48 + Mexico task 3): PASS — byte-identical local-oracle
  reduce, verified upload/resume/restore/stale-repair, ~4M rows/task, map RSS
  866 MB, retry read amplification 2.03×. Places: technical `optimize`,
  relevance `stop` under the seed's own rule. Follow-ups #88–#94 and #98
  (RESULT_LIMIT 25→10) took the cold gate from 5/10 failing to 1/10; alt-name
  indexing (#93) fixed the 東京タワー seed. Remaining: `famous_unique`
  (context-free "Tokyo Tower", head-admission design —
  `docs/plans/2026-07-17-famous-unique-head-admission.md`) and the
  `relevance_chain_name` cold gate (post-#102/#103 status in Sequencing
  step 2: bytes fixed, reads/latency still failing).
- The `Smoke Test R2 ID Index` run on the #103 merge (29591153753) failed on
  Cloudflare workers.dev propagation flake (404/1042 ×4 then 500/1104 within a
  ~25 s retry window right after preview-Worker deploy), not code — #103
  touched only Python experiment/test files and the identical smoke passed on
  #102's merge 35 minutes earlier. Re-dispatched 2026-07-18 (run 29623176674).
  If this recurs, widen the retry window.
- The finalize release blockers are fixed: #96 derives the exact
  id-inventories key set in `verify_release`; #97 deletes staging only after
  finalize succeeds. A `promote=false` rebuild dry-run is now runnable.
- Prototype contracts now define structured exact-address ambiguity and a
  candidate country/hash-range partition rule, plus Places launch/stop gates.
  These are measurement contracts, not publication approval.
- The July 25 rebuild remains the next production rebuild. The July 13 rebuild
  is recent enough that no ad-hoc production rebuild is justified by this work.

## Decisions and constraints in force

1. Do not promote experimental families until their correctness, byte, heap,
   latency, failure, and product-coverage gates pass.
2. Keep one range-readable framing family and one Worker range-reader core.
   Payload formats may differ; integrity, exact-range behavior, caching, and
   physical-read planning must not fork.
3. Keep one publication path. Before address or Places publication is automated,
   add object-level conditional publication or otherwise prove there is one
   serialized publisher.
4. Exact structured address lookup returns all duplicate/ambiguous candidates.
   It must not destructively deduplicate keys or infer a universal free-form
   grammar.
5. Places launch scope remains literal: name/brand tokens, last-token prefix,
   structured category, and explicit location/context. Broad semantic search,
   one-character CJK prefixes, and unbounded tail enumeration are unsupported.
6. External Overture and Cloudflare workflows are merge-only, manual, or
   low-frequency integration evidence. They are not required pull-request CI.
7. Keep ambiguous reverse routes on `HEAD` until an exact-country candidate
   passes correctness and byte gates after the next production rebuild.
8. Credentialed external-data workflows run only from `main`. A green evidence
   workflow means measurement and cleanup succeeded; it does not approve a
   family for launch or publication.

## Next work

### 1. Run and benchmark the routed Places prototype

The byte reader works; query planning and product relevance are now the blocking
work.

- ~~Dispatch the main-only Places workflow.~~ Done 2026-07-16; evidence
  retained. Re-dispatch after each relevance/read-path change; next changes are
  famous-unique head admission and the chain_name records-stage fix.
- Build a side-by-side top-five panel for the six relevance seeds using the
  previously evaluated reference geocoders, including Nominatim. Treat those
  engines as comparators rather than ground truth and adjudicate disagreements
  in coverage, local names, category semantics, and requested context.
- Record `proceed`, `optimize`, or `stop`. The automated state
  `awaiting_relevance_benchmark` is intentionally not launch approval.
- Design a bounded ranking component that can compare every eligible located
  candidate before applying distance. Do not present the current decoded
  top-window distances as complete near-me ranking.
- Keep `place` disabled by default until every seed class has a relevant
  top-five result, context ordering is correct, repeated order is stable, and
  the routed read/byte/latency gates pass.

Exit artifact: one routed Worker/relevance report with a proceed, optimize, or
stop decision.

### 2. Run real address fragments through verified resume

The bounded producer shape is viable; the next risk is global coordination and
coverage policy.

- ~~Dispatch the main-only US/Mexico workflow.~~ Done 2026-07-16: PASS with
  full evidence retained.
- ~~Collect structured-retention and output-byte summaries via a stratified
  sweep.~~ Done 2026-07-17: 12-task stratified sweep green (see Sequencing
  step 3 and `benchmarks/2026-07-17-address-stratified-sweep-report.md`).
  Full 127-task coverage remains a planet-readiness gate; the stratified
  evidence is the input for the partition-rule review, not a substitute for
  complete coverage.
- Validate the proposed exact endpoint and country/hash-range partition against
  the multi-task evidence. The current normalization contract is NFC,
  Unicode-whitespace collapse, and ASCII-only lowercasing; decide and version
  any broader Unicode folding before building publishable shards.
- Add the object-level publication guard required by the shared finalizer path.
- Evaluate a bounded two-level/sparse side index or smaller serving partition so
  a cold exact lookup does not automatically fetch the observed 941,745-byte
  full index. Preserve exact predecessor selection and the three-range cap.
- Measure Actions minutes, retry amplification, peak disk/RAM, and wall time.
  Keep partial runs undiscoverable and do not hydrate Overture on the request
  path.

Exit artifact: a multi-task, non-promoting R2 map/reduce rehearsal plus a
reviewed address coverage/partition contract.

### Production rebuild readiness for address and Places

Neither family is part of the next scheduled full shard rebuild. A limited
regional rebuild is acceptable before planet readiness, but only after its
scope is explicit and the following gaps close:

- **Producer scope and coverage:** choose named address and Places regions from
  source inventory; record included/excluded rows and reasons; produce exact
  family totals rather than extrapolating one regional retention ratio.
- **Final artifacts and catalogs:** turn the prototype address partition rule
  and Places routing catalog into release-versioned family manifests with
  immutable object identities, lineage, format/tokenizer/normalization versions,
  and deterministic local plus downloaded verification.
- **Serving correctness:** complete bounded located-Places ranking, comparator
  relevance adjudication, multilingual/local-name coverage, address ambiguity
  and overflow behavior, and the smaller address side-index decision.
- **Publication serialization:** integrate both optional families into the one
  shared finalizer with create-only object publication, expected-current
  promotion, undiscoverable partial runs, and no independent publisher.
- **Operational envelope:** run a non-promoting regional build through resume,
  family verification, candidate-catalog generation, remote Worker smoke,
  failure injection, cleanup, and retained time/RAM/disk/byte evidence.
- **Promotion and recovery:** add family-aware production smoke, degraded or
  partial-result signaling, rollback/recovery behavior, and retention rules that
  never prune the live or rollback release.

Only after those gates pass should a promoted rebuild opt into address or
Places. Planet scale additionally requires complete 127-task address coverage,
global Places partition/fame evidence, and a combined stored-byte/build-time
result inside the existing 40 GB and 12-hour stop gates.

### 3. Rehearse the scheduled production path

This remains deadline-driven operational work, not a reason to rebuild early.

- Before or with the July 25 run, dispatch the existing rebuild with
  `promote=false` to exercise build, verification, immutable release-manifest
  publication, and catalog-candidate generation without changing the catalog.
  This dry run does not exercise catalog swap, production smoke, recovery, or
  pruning because those paths are correctly gated on `promote=true`.
- After the rebuild, verify lineage-backed reverse hierarchies and re-measure the
  multi-bbox ambiguous-route share. Wrapped antimeridian bboxes and materialized
  lineage only affect a newly built fleet.
- On the scheduled promoted run, record per-family inventories, promotion and
  production-smoke checks, any rollback/recovery outcome, and retention cleanup.
  Do not add address or Places artifacts to this run.

Exit artifact: one production-operations report for the July 25 release.

### 4. Re-size exact-country work after the rebuild

No current exact-country candidate passes both correctness and the proposed
5 MiB byte gate. After the new reverse baseline exists:

- decide whether the 5 MiB hot-object gate still stands;
- independently review borders, coasts, islands, enclaves, antimeridian cases,
  synthetic `X*` codes, and political-perspective labels;
- only if justified, test a conservative interior cover with exact unsimplified
  fallback and zero route-target drift; and
- ratify heap, cold-open, and warm-p95 gates only after stored-byte and
  correctness gates pass.

## Supporting engineering backlog

These do not block the first routed Places or real-fragment address slice:

- replace long-lived R2 smoke secrets with a short-lived OIDC/broker path when
  Cloudflare support makes that practical;
- replace the hardcoded seven-type ID workflow matrix with discovery plus
  `fromJSON`;
- split ID staging, build, and metadata phases while retaining
  `id_index_protocol.py` as the shared contract;
- decide whether to populate locality reverse `wkb` correctly or remove the
  dormant column/query path;
- clean existing workflow quoting findings, then re-enable shellcheck/pyflakes
  in actionlint; and
- remove or consume unfit exact-country research outputs.

## Measured constraints to carry forward

### Address

- Full task useful-gzip pages measured 35.325 B/retained row in Mexico and
  36.495 B/retained row in the US; the smallest tail measured 29.510 B/row.
- Projection peak RSS stayed below 1.73 GB and map/reduce peak RSS below 870 MB.
  The longest measured compression pass was 692 seconds.
- Maximum exact-key fanout was 252, and every tested page variant preserved
  full candidate order and IDs.
- The large-shard Worker cold lookup measured 392 ms, three R2 reads, and
  954,362 bytes; five warm lookups had a 174 ms median and zero R2 reads. The
  stored/decoded/materialized page sizes were 8,521/15,838/90,978 bytes.
- The complete source inventory remains 473,576,753 rows, 8,704 row groups, and
  127 planned tasks. Do not multiply it by one regional retention ratio.

### Places

- Current three-region shards measured 116.38 B/place in Boston, 169.34 in
  Tokyo, and 124.38 in Mexico City. CJK token density materially changes bytes.
- Packed-head cold access measured 863 ms client time, four R2 reads, and
  132,219 bytes. Five warm observations had a 195 ms median.
- The first three-shard fallback measured 1.347 seconds, 15 logical ranges,
  12 R2 reads plus three cache hits, and 75,042 R2 bytes. Fully warm fallbacks
  had zero R2 reads but roughly 196-224 ms median client time.
- The workflow proves reader equivalence, not relevance or an independently
  cold latency distribution.

### Producer and operations

- Initial global stop gates remain 12 hours, 12 CPU on the factory equivalent,
  48 GiB RAM, 700 GiB temporary disk, and 40 GB combined compact
  Places/address output per release.
- Immutable R2 objects require verified size, metadata, and downloaded SHA-256.
  Existence alone is never a resume signal, and a conflicting object is never
  overwritten.

## Open decisions

1. Places comparator relevance, complete bounded located ranking, regional
   coverage, and measured numeric gate results.
2. Address Unicode normalization version and 127-task coverage. (Resolved
   2026-07-18: side index — Option C with the 4M-row shard guardrail;
   publication serialization — object-level CAS guard merged in #108.)
3. Additive degraded/partial-result signaling shared by experimental families.
4. Reverse `wkb` disposition and post-rebuild exact-country priority.

## References

- `benchmarks/2026-07-17-remote-address-places-r2-evidence.md`
- `benchmarks/2026-07-16-live-service-baseline.md`
- `benchmarks/address-rowgroup-inventory-report.md`
- `benchmarks/address-format-convergence-report.md`
- `benchmarks/address-format-convergence-tokyo-report.md`
- `benchmarks/places-compact-shard-factory-report.md`
- `benchmarks/places-head-repack-report.md`
- `docs/places-tokenization-decision.md`
- `docs/plans/2026-07-14-global-places-address-processing-design.md`
- `docs/plans/2026-07-17-famous-unique-head-admission.md`
- `docs/plans/2026-07-17-places-chain-name-records-stage.md`
- `docs/plans/2026-07-17-places-92-residues.md`
- `docs/plans/2026-07-18-us-ne-regional-deploy-scope.md`
- `docs/plans/2026-07-17-address-stratified-sweep.md`
- `docs/plans/2026-07-12-exact-country-decision-artifact.md`
