# Pending Work — 2026-07-16

This is the durable roadmap after the July architecture and experiment series.
The code baseline is `0aa9822` (`main` after #82). Keep measured evidence
separate from decisions and proposed work so this file can be updated without
preserving branch- or PR-specific history.

## Baseline

### Completed work

- #43–#49 added subtype-first reverse results, global forward routing,
  antimeridian-aware reverse metadata, build/data-version guardrails, and the
  safe rule that ambiguous country bboxes fall through to reverse `HEAD`.
- #46 and #50 added the regional Places prototype, `types=` filtering, bounded
  rank/routing experiments, and fail-closed experimental manifests. Places are
  not enabled by default.
- #50 also completed the H3/exact-country comparison harness. It did not add a
  production H3 dependency or router.
- #51 completed the address/street experiment harness and initial hybrid
  architecture findings.
- #52 and #53 completed the backward-compatible compact ID locator v3 and the
  content-addressed inventory fan-in needed to build it at scale.
- #54 completed bounded current-release address/source and Boston
  transportation-topology experiments. It did not add Worker, API, R2, or
  production-build integration.
- #55 split the external-source/R2 smoke into merge-only shard and ID-index
  workflows with narrow production-path filters, fixed per-family prefixes,
  and cancel-in-progress concurrency. Its first merged runs passed.
- #55 also completed the pinned Overture exact-country decision artifact. It
  retained an exact all-claims oracle, rejected the `territorial-primary` and
  ordinary simplified candidates, and added no production router or H3
  dependency.
- #57 replaced the global Monaco smoke export with the validated country,
  required-ID closure, and bbox-pushdown path. The first merged current-release
  extraction completed in 31.569 seconds and the full shard smoke in 1m52s,
  down from 4m13s and 5m35s respectively.
- #58 published `router.db` before its metadata, added exact size/SHA fresh-R2
  reads and SQLite queries for forward, reverse, and router artifacts, and added
  an isolated preview Worker intended to check current and historical v3 ID
  responses before strictly cleaning up the Worker and fixed R2 prefix.
- #59 fixed the two harness assumptions exposed by #58's first merged run:
  DuckDB 1.5.1 result enumeration now uses `fetchall()`, and a legitimate empty
  Monaco router is accepted only after exact metadata-count, integrity, schema,
  index, and queryability checks. The patch passed adversarial review, 287 local
  tests, and all six pull-request CI jobs.
- #60 added a smoke-only historical registry sentinel because a bounded real
  prefix sample cannot guarantee sparse path-null rows. Its merge-only ID smoke
  passed every build/read stage, returned valid current and historical v3
  responses through the isolated Worker, and verified Worker plus R2 cleanup in
  8m35s.
- #61 added immutable-prefix protection, exact release readback, atomic catalog
  promotion, production health/search/reverse/ID smoke, rollback, and guarded
  retention. Rebuild run `29286993689` completed in 3h34m28s and safely
  promoted production data version `2026-07-13.0`; cleanup was intentionally
  disabled for that first hardened rehearsal.
- #63 published a GitHub Pages tester for health, forward, reverse, and ID APIs.
  #64 hardened the reverse benchmark and made returned hierarchies internally
  country-consistent.
- Commit `55d6498` split release staging into seven concurrent per-type Actions
  jobs with exact marker fan-in, bounded DuckDB resources, application
  heartbeats, per-phase timings, connection cleanup, and batched metadata
  validation. Its CI and external-source/R2 smokes passed.
- #65 corrected reverse country routing so caller IP cannot override coordinate
  evidence, added bounded routing diagnostics and regressions, and preserved the
  Places architecture experiments in a reviewed decision record. Its compact
  spatial-shard direction retains a minimal useful result projection; Places
  serving and relevance remain experimental.
- The address/division spike built a 159,252,812-byte reader-verified compact
  artifact from 3,634,040 keyable Massachusetts addresses (43.8 B/address),
  including a 102.05-second boundary-inclusive point-in-polygon join to 635
  current-release division areas under 4 threads and a 12 GB limit. It
  proves a structured exact-address storage shape, not global parsing, fuzzy
  matching, interpolation, or production readiness.
- The global Places/address processing design specifies a pull-based bounded
  producer, stable split lineage, immutable per-family manifests, resumable
  hash-verified stages, and one atomic multi-family promotion. The factory is an
  offline producer only, never a serving dependency or general CI runner.
- #67 added the deterministic, non-promoting global build control plane: strict
  source inventory, map completion, fragment, reduce artifact, record
  reconciliation, and catalog-candidate contracts plus a public hosted-runner
  planner smoke.
- #68 added the bounded current-release address row-group data spike. Its first
  public hosted run projected 1,415,000 addresses from 24 row groups in 6.11
  seconds at 764.8 MB peak RSS, wrote 54,101,306 bytes, and measured a 106.6 MB
  initial runner-network upper bound against a 657.3 MB source object. Source
  identity, artifact metadata, record count, and three locator hydrations
  verified; no artifact, R2 object, catalog, or production state was published.
- The follow-on hosted reduce spike projected 3,743,307 current-release rows,
  retained 1,382,264 with both street and number, wrote 30 sorted fragments,
  and assembled and fully scanned a 204,646,996-byte range-readable shard. The
  complete public-runner job passed in 2m38s; projection peaked at 1.534 GB RSS,
  while the reducer peaked at 715.4 MB and its assembly took 31.66 seconds.
  Compute and disk are encouraging, but the
  deliberately uncompressed 148.1 B/indexed-row shape is rejected as the final
  planet format. R2 shuffle, multi-source dictionaries, and global skew remain
  unmeasured.

- The 2026-07-16 review-driven correctness pass fixed the pipeline defects a
  six-area adversarial review confirmed: the concurrent staging path could
  again host-OOM (3×10 GB after the dedicated-runner retune; now parameterized
  back to 3×4 GB), `--prefixes` patch builds bypassed the build-marker
  inventory (they now rewrite the containing range marker or fail closed),
  `patch_failed_shards.py` could mutate a published version (now guarded, with
  `--force-unsafe`), finalize verified ID shards by size only (single-part R2
  ETags are now bound to recorded Content-MD5), staging markers without staged
  data went undetected (now fail closed), and batched schema classification
  depended on unguaranteed row order (now keyed by column name).
- The same pass fixed shard-generation and routing defects: the reverse
  "exact WKB containment" path had been active on every shard while all `wkb`
  values were NULL (the probe now requires actual geometry data, and the
  parser accepts only plain 2D types); reverse candidate ordering gained
  `gers_id` tiebreaks; shard builds are now byte-reproducible (ORDER BY on
  every build SELECT, `created_at` derived from the data version — verified
  byte-identical across rebuilds); aggregate country bboxes now wrap the
  antimeridian instead of spanning the globe (takes effect on the next fleet
  rebuild and should sharply reduce ambiguous reverse routes); and the router
  builder now shares the worker's exact normalization/token contract via a
  cross-language fixture, dropping provably unreachable 2-char/hyphenated
  tokens and fixing the never-working raw-schema fallback.
- The consolidation half of that pass: the worker's 3.5k-line `stac.rs` split
  into six focused modules (cache, catalog, forward, reverse, id_index,
  router_db) with dead CF-IPCountry reverse plumbing removed and parsed
  collections memoized; the ~370 lines of promote/rollback/prune bash moved
  into unit-tested `finalize_rebuild.py promote|recover` and
  `scripts/prune_catalog.py` (exact-match reference guard replacing substring
  grep); retention raised to the worker's four-version fallback window with a
  cache-TTL grace before any deletion; workflow contract tests rewritten to
  parse YAML structurally; actionlint added to CI; recovery procedure
  documented in `docs/rebuild-recovery.md`; the Places experiment moved out of
  `build_shards.py`; and the Monaco equivalence evidence regenerated with a
  real `2026-06-17.0` run (drift 0, subset smoke 45.4 s).

- #75 landed the full review pass above after two adversarial review rounds
  (one internal, one independent) that added: a true minimal-circular-cover
  antimeridian bbox (the first cut could exclude central territory for a
  France-shaped composition), ETag/content-MD5 binding at both metadata and
  finalize time with multipart ETags failing closed, Greek final-sigma
  normalization parity, an O(1) `has_wkb` metadata flag, exact version-prefix
  inventory equality at finalize, forward-router tie determinism, and a
  390-second deletion grace covering the compounded catalog + text-memo TTLs.
- #76 moved the pure reverse-routing logic into `geocoder_core::routing`
  (primitive `(shard_id, bbox)` seam; worker keeps a thin STAC adapter) and put
  the address-spike decoder plus `flate2` behind an off-by-default
  `address-spike` cargo feature enabled only by the smoke build — the
  production wasm bundle no longer ships spike code.
- #77 extracted the ID-index marker/inventory protocol into
  `scripts/id_index_protocol.py` (attest/verify single home; bii re-exports
  keep the import surface stable), added a fail-closed build-phase
  reconciliation of staged files against per-type markers (build previously
  trusted raw globs), added `scripts/common.py`, and fixed a latent TypeError
  in `gen_id_collection.py`'s v3 dictionary validation.
- #78 decided the divergent address formats with measured evidence: converge
  the division-joined spike INTO the division-free lookup-safe format; raw
  `address_levels` stay the source of truth and divisions ride as an optional
  self-describing per-page extension (dictionary + per-row index + one
  match-method byte, +0.311 B/row measured including framing). On a 189,248-row
  Boston-core box (40k sampled), genuine cross-context conflicts were 0.24%;
  the once-headline 14.35% "disagreement" decomposed to 14.11% valid
  finer-granularity neighborhood labels — measured proof that overwriting
  source labels with containment would be wrong.
- #79 materialized division lineage into reverse shards: the ordered primary
  hierarchy path (root→…→self; verified single-hierarchy for all 55,517
  reverse-eligible divisions in 2026-06-17.0) rides as a nullable column, and
  hierarchy assembly becomes an exact by-identity lookup resolved by chain
  position — immune to the per-subtype candidate cap and to same-subtype
  straddles — with the old heuristic preserved byte-for-byte for legacy shards
  and for any malformed/oversized chain. Takes effect at the next fleet
  rebuild.
- #81 built the shared range-reader core decided in §8: a payload-agnostic
  page module in `geocoder-core` (bounds-checked byte cursor, generic capped
  side-index, extended-page framing byte-matched to the Python codec via
  committed cross-language fixtures, and a fuzz-verified range-coalescing
  planner), with the feature-gated address spike refactored onto it as the
  first consumer — behavior, caps, cache keys, and route gating pinned
  unchanged. Adversarial review (2M-case coalescer fuzz, fixture
  regeneration, wasm32 trace) approved with note-level items only.
- #82 ran the two Places de-riskers from §9. Single-object head repack: the
  4,090-object packed head fits ONE range-readable object at 25,753,724
  bytes (385 KB key index + entries), slightly smaller than the bucket
  layout, with mean hit overfetch 7.1x in the bucket baseline's disfavor and
  a disclosed 385 KB resident-index cost. Non-CA partition: a deterministic
  1M-row Tokyo-metro sample holds 122.9 B/place vs California's 116.4 with
  components rebalanced (lexicon 2.3→13.2 B/place); the lexicon triples with
  82% singletons and 86% CJK-dominant tokens, and the oracle (complete
  recall, exact top-k) passes on both partitions — storage and layout
  generalize; tokenization does not (space-free CJK names collapse to one
  token, NFKD strips dakuten). Review recomputed every headline number and
  approved.

### Current production data

- Production serves data version `2026-07-13.0`, built from Overture release
  `2026-06-17.0`. No additional fleet rebuild is currently approved.
- The production ID index is v3 with compact locator metadata. The reader
  remains backward compatible with v1 artifacts and uses the legacy response
  path when new metadata is absent.
- Reverse routing uses request coordinates and country bboxes only as a
  guardrail. Ambiguous overlap falls through to `HEAD`; caller IP is diagnostic
  context only and no exact-country decision artifact is deployed.
- Forward search remains division-focused by default. No Places, address, or
  street artifact has been promoted to the production fleet.
- The hardened `2026-07-13.0` rebuild and promotion passed production health,
  search, reverse, and ID checks. Later Places/address/global-producer
  experiments remained read-only or ephemeral and did not modify production.

## Decisions and constraints in force

1. Do not rebuild or promote a shard fleet until the relevant experiment has
   reviewed labels, explicit byte/heap/latency gates, and fail-closed metadata.
2. Keep required smoke work merge-only (`push` to `main`, plus manual dispatch).
   Do not turn the external-source/R2 smoke into a pull-request check.
3. Exact country containment is the preferred durable reverse router. The
   direct all-claims oracle implements the stated experiment semantics but is
   far above the proposed 5 MiB budget, while ordinary polygon simplification
   is not decision-safe. Keep `HEAD` fallback for missing, invalid, boundary,
   multiply contained, or policy-ambiguous inputs. If the 5 MiB gate stands,
   test a conservative H3 or equivalent interior cover with exact full-geometry
   handling for boundary cells; do not use simplification as the decision
   layer.
4. Preserve country polygon components rather than unioning them by country.
   The Overture `division_area` schema says exactly one of `is_land` and
   `is_territorial` is true, but the pinned release contains dual-true rows that
   must be classified explicitly. Political perspectives are available through
   the linked parent `division` and must be joined explicitly. `X*` values are
   permitted synthetic Overture country codes, not malformed ISO codes; route
   them only under an explicit mapping/policy and otherwise use `HEAD`.
5. Keep Places regional and `HEAD` division-focused until the range-readable
   global-head format and independently labelled relevance suite pass.
   Confidence measures feature existence, not venue fame; brand/category priors
   and raw source count are not adequate fame signals. Chains and other
   high-fanout names require query geography or caller location.
6. Use transportation name/connector components for street topology,
   addresses/divisions for locality and postcode context, and a separate
   regional candidate store for exact numbered/unit addresses. Do not
   destructively deduplicate address keys or infer absent ranges.
7. Published build metadata must describe every artifact family in an immutable,
   SHA-bound manifest. A sequential build must not overwrite the only record
   with whichever family ran last.

## Evidence retained

### Reverse country routing

- Testing 980,494 division centroids against 219 aggregate country bboxes found
  91.34% in more than one bbox. Smallest-bbox selection was wrong for 37.63% of
  all rows (40.90% of ambiguous rows), and 0.353% were not contained by their
  own aggregate country bbox. Bboxes are not a durable country classifier.
- A Natural Earth low-resolution comparator over 177 country polygons and
  20,000 seeded points measured direct R-tree + exact WKB at 7.65 microseconds
  per query, H3 resolution 2 + exact boundary at 3.25 microseconds, and H3
  resolution 3 at 1.75 microseconds. All matched on that sample. Compact
  estimates were about 158 KiB direct, 189 KiB H3 r2, and 316 KiB H3 r3.
  These are feasibility results, not Overture/Worker performance measurements.
- The reproduced `2026-06-17.0` inventory contains 378 division-area rows for
  219 country codes: 159 land-only rows, 159 territorial-only rows, and 60 rows
  with both flags true. The 60 dual rows include 42 standard codes and 18
  synthetic-code rows. There are 21 distinct `X*` codes across 24 rows. The
  parent-division join found no populated country perspectives in this release.
  Current schema documentation says exactly one flag should be true, so the
  dual rows are a release fact that the artifact must classify explicitly, not
  silently reject or reinterpret.
- The all-claims exact oracle was 183,095,296 hot SQLite bytes. Exact
  `territorial-primary` retained all 219 territorial-flag rows, including 60
  dual-flag rows, while excluding 159 land-only rows. It was 90,083,328 bytes
  and had zero drift on 5,000 global, 200 territorial-boundary, and 2,400 nearby
  points, but it false-uniquely routed 195 of 200 sampled all-claims boundary
  vertices. Excluding land-only claims is not conservative under the current
  any-boundary-to-`HEAD` rule.
- The 5,263,360-byte 0.0025-degree simplified candidate exceeded 5 MiB and
  produced 64 false-unique and 145 wrong-country routes on the primary corpus.
  The 3,305,472-byte 0.005-degree candidate produced 59 and 161. Ordinary
  simplification is rejected as an exact router.

### Places

- The 1,768-row Places fixture is a roughly 0.98 km² downtown San Francisco
  slice from `2025-12-17.0`, not a statewide sample. At N=100,
  confidence-only retained 53 categories and 52 spatial cells, while the
  rejected prominence formula retained four categories and 86 hotels.
- In the audited `2026-06-17.0` release, a bounded 100k-row partition sample had
  confidence, sources, and addresses populated throughout, but brand Wikidata
  was only 1.3%. An independent addressed-California audit found 1.6% brand
  Wikidata and materially different brand coverage, confirming strong source
  composition effects.
- Root provenance was one external dataset per sampled feature. Additional
  source rows were often Overture-derived confidence/status signals, so source
  count is not independent corroboration.

### Addresses and street topology

- Twelve address boxes produced 19,702 records across nine root datasets.
  Sampled rows had one root source, while confidence, license, source record ID,
  `between`, and update time were absent. Missing confidence is unknown, not
  zero.
- The Massachusetts artifact contained 3,637,794 rows (153.1 MiB); 36.4% had a
  unit. Conservative structured normalization still produced 6,804 exact keys
  with multiple coordinates, including 29 keys spanning more than 1 km. Exact
  text must return candidate lists, not a destructively deduplicated row.
- The Boston polygon+halo snapshot measured 65,176 road segments, 22,385 named
  segments, 99,878 connector IDs, and 5,832 conservative name/connector
  clusters. Hot cluster+alias lookup was 2.01 MiB and deduplicated detail was
  16.44 MiB. These are bounded prototype sizes, not global estimates.
- Exact-name address context covered 4,391 of 5,832 clusters (75.29%) only as a
  diagnostic. A spatial address-to-segment/component join is still required;
  shared connector IDs, not visual crossings, define graph connectivity.
- In the hosted reduce range, only 1,382,264 of 3,743,307 projected rows
  (36.93%) had both non-empty street and number; 2,361,043 were explicitly
  rejected and no Point geometries were invalid. This one source-object range
  is not globally representative. Excluding incomplete rows is a defensible
  exact-address scope but a potentially severe coverage choice, not harmless
  compression.

### ID locator and smoke runtime

- Real ID samples measured a 4.58% / about 1.466-byte-per-row v3 delta. The
  sample dictionary was 90 KiB for 698 source tuples; real footers were 12.5
  KiB and cold row groups about 1.74 MiB. These are sizing evidence, not a fleet
  forecast.
- The exact-main smoke took 14m20s; 7m26s was spent exporting global divisions
  to build Monaco.
- After #55, the first independent merged runs passed in 5m35s for shards and
  6m00s for the ID index. The shard run still spent 4m13s (76% of job time)
  exporting global divisions; both Monaco shard builds, upload, verification,
  and cleanup then took about one minute combined. The global export remains
  the first runtime bottleneck to remove.
- After #57, the current-release Monaco extraction took 31.569 seconds and the
  complete shard smoke took 1m52s. After #59, exact generated-object and router
  readback passed. The ID smoke built and published v3 artifacts but stopped
  before preview Worker deployment because prefixes `000`–`004` contained no
  retained historical row. After #60, the deterministic historical smoke input
  completed the isolated Worker assertions and strict cleanup in 8m35s.

## Pause checkpoint

Production `2026-07-13.0` is healthy. PRs #65–69 completed the coordinate-first
country-routing correction, compact Places/address design evidence,
deterministic global control plane, and first real hosted row-group projection.
The hosted address reducer then passed its current-release 1.38M-row retained
range in 2m38s with 1.534 GB projection peak RSS, 715.4 MB reducer peak RSS,
and exact maximum-fanout verification.

The first address storage gate now has a credible path: independent,
candidate-group-aligned gzip pages targeting 256 rows preserved the full reducer
response at 35.50 B/indexed row, with 8.98 KB p50 pages. The conservative
all-473M-row diagnostic is 16.79 GB for addresses and about 25.54 GB combined
with the current 8.75 GB Places diagnostic. This is bounded evidence, not a
planet forecast. The isolated Worker decoder then served the lossless
synthetic fixture sized to the observed 137-candidate maximum fanout in 434 ms
for the first run-unique lookup and a 156 ms subsequent median. It decoded all
records but returned only a compact verification body; Cache API hits were not
instrumented. Strict cleanup removed both the Worker and its run-specific R2
objects. Worker gzip/range-read cost is therefore not the immediate stop
condition. Next sample global completeness, page tails, side-index scale, and
shard skew before any catalog or production promotion behavior.

## Work inventory and execution priority

Execute the remaining inventory in this order: finish the address
global-completeness and skew gates in items 4–5; repeat Worker reads against a
representative large index; then add a hash-verifying R2 shuffle/resume spike.
Keep Places relevance/global-head work
and the research-only exact-country comparator queued behind that bounded
address decision. The item numbers preserve subject grouping rather than
execution order. None of these experiments authorizes a production rebuild.

### 1. Monaco subset and generated-object read smokes (completed)

The implementation, pinned equivalence proof, and first merged current-release
run are complete. On Overture
`2026-06-17.0`, it validated 22 divisions and all 22 known division-area rows,
produced the same three forward and three reverse rows as the legacy global
export, matched the built forward, reverse, and router databases logically, and
completed recurring extraction in 44.747 seconds with DuckDB 1.5.1. The first
post-merge current-release extraction then completed in 31.569 seconds; the
full R2 shard smoke completed in 1m52s instead of the prior 5m35s.

PRs #58 and #59 implemented the validated shard read/router smoke: publish
`router.db` before metadata references it and read back and query exact actual
R2 objects. The #59 shard run passed. Its ID run failed while selecting the
historical v3 case, before catalog upload or preview Worker deployment, because
the bounded registry sample had no retained historical row. PR #60 kept that
assertion, made the historical smoke input deterministic, and passed the full
current/historical preview Worker path plus strict cleanup in 8m35s.

Keep the completed workflow split, merge-only triggers, narrow production-path
filters, fixed prefix per smoke family, and cancel-in-progress concurrency.
Do not add the external-source/R2 smokes to pull requests and do not add
run-specific prefixes or a stale-prefix sweeper.

- Replace the global forward and reverse division exports with smoke-specific
  queries for only the Monaco division, division-area, and required hierarchy
  rows. Make country/code plus the immutable required-ID hierarchy closure the
  authoritative correctness filter. Use bbox only as an additional coarse
  Parquet-scan predicate after validating the required rows have present,
  consistent bbox metadata; fail closed if any expected Monaco division, area,
  or hierarchy row is missing. Confirm pushdown by profiling the actual rendered
  predicates, requiring bbox on both scans and country on the division scan, and
  proving from complete footer statistics that the country+bbox fast branch
  makes fewer row groups eligible; do not infer it from elapsed time alone.
  DuckDB may elide an exact ownership branch that is redundant for the current
  source. Its 1.5.1 `rows_scanned` counter is not a gate: it reports twice the
  complete source cardinality for these remote nested scans.
- Preserve the production transformations, geometry, aliases, hierarchy, and
  forward/reverse build paths. The smoke should remain an external-source
  integration test, not become a checked-in fixture.
- On the pinned same release, require schema, IDs/rows, hierarchy/context,
  aliases, geometry, and built shard contents to match the legacy global export
  filtered to Monaco. Preserve the executed scan profile and demonstrate fewer
  metadata-eligible row groups, then target less than 60 seconds for extraction
  on the same runner; if that bound is not practical, ratify a replacement
  before accepting the optimization.
- Record total and stage timings on the first post-change merged run. The
  immediate goal is to make source extraction a bounded setup step rather than
  the dominant 4m13s of a 5m35s job.
- A release+SQL-SHA export cache is a fallback only if the subset query remains
  too slow. Keep a slower scheduled full-source integration run only if the
  bounded merge smoke stops covering material production extraction behavior.

### 2. Ratify the exact-country gates and build the next comparator

The completed research-only direct artifact and decision report use pinned
release `2026-06-17.0`. They reject `territorial-primary` and simplified
variants under conservative boundary semantics and add no Worker, R2,
fleet-build, or production routing integration.

- Decide whether the proposed 5 MiB hot-artifact gate stands. The valid direct
  all-claims oracle is about 174.6 MiB (183.1 MB).
- If the gate stands, build a research-only H3 or equivalent conservative
  interior cover from all 378 land and territorial claims. A cell may route
  directly only when exact build-time proof over the entire closed cell, using
  the oracle's normalized antimeridian semantics, shows a constant claim set
  with no boundary, synthetic `X*`, perspective, or decode blocker and interior
  hits that deduplicate to exactly one standard country. This must also reject
  cells wholly inside overlapping claims from different countries. Any failure
  to prove the rule uses exact unsimplified full-geometry predicates; unresolved
  cases return `HEAD`. Compare every decision to the 183,095,296-byte
  all-claims oracle and do not add a production dependency.
- Ordinary topology-preserving simplification may be measured only as a
  conservative candidate-pruning aid with proof that it cannot affect the
  routing decision. The existing R-tree/bbox filter remains the safe coarse
  filter; the measured simplified polygons are rejected as routers.
- Independently review the border/coast/island/enclave/antimeridian and `X*`
  labels. The committed curated seed queries and deterministic corpora are
  diagnostic evidence, not independently reviewed gold data.
- Ratify heap, cold-open, and warm-p95 gates only after a candidate passes the
  correctness and stored-byte gates.

Candidate gates to ratify before runtime benchmarking are: zero route-target
drift on independently reviewed correctness labels (including zero
false-unique, false-negative, and wrong-country routes), at most 5 MiB of
uncompressed hot runtime-object bytes, at most 12 MiB incremental peak heap, at
most 10 ms cold open, and at most 100 microseconds warm p95. Cold audit/manifest
bytes and compressed/published-object bytes require separate explicit budgets.
For the hybrid comparator, the 5 MiB numerator includes the cell index and every
exact boundary geometry/index object it requires; it must not hide the exact
fallback in an uncounted sidecar. A future lazy/sharded design needs separate
total-published, fetched-per-request, and hot-resident budgets. Cell-clipped
exact geometry, if tested, must prove decision equivalence to the original
unsimplified components. These are proposed starting points, not accepted
production thresholds; every current candidate fails either correctness or
bytes.

### 3. Places compact serving path (storage selected; relevance and head blocked)

The compact spatial-shard experiment built a real 116,684,322-byte artifact from
a reproducibly extracted one-million-row California-area sample. Exact candidate
sets and static top-ten results matched its oracle. Its 116.7 B/place result
linearly diagnoses about 8.75 GB for 75M Places, excluding the global head,
aliases, multilingual coverage, and typo support. No Places artifact is in the
production catalog.

- Build the proposed range-readable global head; the prior experiment measured
  4,088 modeled objects and 25.1 MB for the 1M sample, so a one-object head is
  still unproven.
- Independently label brand, local-name, category-near-me, ambiguous-context,
  famous unique, and chain-name queries across dissimilar regions.
- Measure Worker range reads, cache behavior, result locality, and explicit
  unsupported broad/tail-query behavior before enabling `place` by default.
- Add multilingual aliases only as a separately budgeted format decision.

### 4. Address compact serving path (compute viable; compact format blocked)

The Massachusetts compact artifact confirms that structured address repetition
can fit a much denser format than Places. Its hot record contains ID,
coordinates, number, unit, a source-label-set reference, and a normally-zero
division-chain override; indexed dictionaries preserve source display labels and
all covered division memberships. The hosted reducer subsequently preserved raw
address levels and exact source row-group/row locators, but its naive repeated
strings raised storage to 148.1 B/indexed row.

- Keep the current-release reducer candidate oracle and compare dictionary/
  prefix-compressed strings, address-level sequence IDs, multi-source IDs, and
  a bare useful hot response. Repeat the winning format on current-release
  Massachusetts and at least one non-US country.
- Implement a structured exact-address endpoint first. Evaluate a bounded US
  one-line parser independently; do not claim one universal global grammar.
- Keep the initial NFC/ASCII-whitespace/ASCII-case normalization contract identical in
  DuckDB, Python, and Worker code. Multilingual case-insensitivity is blocked on
  a separately versioned cross-runtime golden corpus.
- Review repeated-name, boundary, unit, postcode, duplicate, ambiguous-key, and
  no-result queries. Preserve all candidates and do not infer ranges.
- Measure Worker exact/prefix/ambiguous/no-result range reads before any catalog
  integration. Roads remain out of scope except for a later evidence-backed
  association join.

### 5. Global producer and hosted-runner feasibility (format gate active)

- The first control-plane prototype now creates deterministic source inventory,
  map/reduce task, map-completion, artifact, and catalog-candidate manifests.
  Source inventories pin discovery provenance, release, family, schema, object
  identity, and Parquet record counts. Fan-in reconciles source, selected,
  rejected, fragment, and artifact records; binds each reduce artifact to its
  exact per-partition fragment digest; and carries an expected previous-catalog
  digest for a later serialized compare-and-swap promotion. Every candidate is
  explicitly promotion-ineligible until remote object/hash verification,
  rejection policy, and serialized publication exist. The prototype does not
  transfer or publish data.
- A manual, read-only GitHub-hosted control-plane smoke now exercises the
  fixture planner on four bounded matrix jobs and records CPU, memory, disk,
  and wall-time telemetry. It has no cloud credentials, OIDC permission,
  artifact upload, or promotion path; a successful run proves only that each isolated job can
  reproduce and inspect the static plan. It does not compare jobs or measure
  source I/O, shuffle, reduce, R2, or global data throughput.
- The first real hosted address projection now proves bounded Parquet range
  reads are viable for one source shape. Initial receive bytes were 16.2% of the
  source object and 2.08x the selected-column compressed metadata estimate;
  total receive including three hydration reads was 113.0 MB. The sample is the
  first eligible object/range and is explicitly non-representative.
- At this one-range shape, a diagnostic linearization to the 473M planning count
  requires 335 covering jobs, 35.6 GB source receive, 18.1 GB projected
  Parquet, and 89.3 aggregate runner-minutes including observed job overhead.
  This is not a forecast: it
  excludes inventory, skew, retries, R2 shuffle, reduce/sort, compact index
  assembly, division joins, Places, and cross-release publication.
- Before R2, build row-group inventories across all address objects and choose
  byte-balanced task ranges, then determine whether staying at or below 128 map
  jobs is safe. That target implies about 3.70M addresses per task, 2.61x the
  measured range. Repeat the
  projection on small/median/large and non-US source objects; reject a plan if
  any range exceeds hosted memory, disk, runtime, or transfer amplification
  gates.
- The first hosted reduce run proves the local fragment/sort/merge/verify
  envelope at 1.38M retained rows: 30 fragments, 204.3 MB fragment bytes,
  204.6 MB final bytes, 31.66-second merge/assembly, 715.4 MB reducer peak RSS,
  and a
  757.5 MB conservative workspace estimate. It preserves raw address levels and
  exact source row-group/row locators. Its 148.1 B/row encoding repeats strings
  and would linearly diagnose 70.0 GB if applied to all 473M planning rows; do
  not promote that encoding or relax the 40 GB stop gate.
- Next keep the same exact-candidate oracle while dictionary/prefix-compressing
  normalized/display strings, raw address-level sequences, and multi-source
  locator IDs. Compare a self-contained useful response against a bare hot
  record with optional Overture hydration. Mandatory live hydration is not an
  acceptable dependency until bounded Worker zstd/Parquet range decoding is
  proven.
- Extend the prototype with hash-verifying R2 fragment upload/download and clean
  restart behavior without trusting file existence or overwriting completed
  objects.
- Spike a GitHub-hosted producer using bounded matrix partitions and R2 as the
  cross-job artifact store. Measure runner disk/RAM/runtime, source-download
  amplification, concurrency, retry/resume behavior, and total Actions minutes.
- Keep credentials short-lived and least-privileged. Prefer GitHub OIDC to a
  broker or narrowly scoped rotating R2 credentials if Cloudflare supports the
  required trust path; never require a key stored on the factory.
- Fan in exact family inventories and update the root catalog only after every
  required artifact verifies. Partial or failed matrix runs remain
  undiscoverable.
- Define and fault-test additive degraded/partial-result signaling and Worker
  request-stage timings separately from build-stage timings.

The initial global stop gates are 12 hours, 12 CPU on the factory equivalent,
48 GiB RAM, 700 GiB temporary disk, and 40 GB combined compact Places/address
output per release. Hosted runners have much smaller per-job limits, so the
spike must prove partition independence rather than attempting a monolithic job.

### 6. Consider production exact-country integration only if a future candidate passes

Every current candidate fails correctness or stored bytes. Only after a future
candidate passes the ratified gates should a separate review cover Worker
caching, artifact publication/rollback, failure behavior, and production smoke
coverage. Continue using ambiguous-bbox-to-`HEAD` fallback until that
integration is deployed and verified. Note the wrapped antimeridian country
bboxes shipped 2026-07-16 apply at the next fleet rebuild and should sharply
reduce the ambiguous-route share on their own; re-measure the multi-bbox
ambiguity rate after that rebuild before sizing the exact-country work.

### 7. Engineering follow-ups from the 2026-07-16 review

Completed by #75–#79: lineage materialization, routing-to-core, the
`address-spike` cargo feature, the marker-protocol module plus build-staging
guard, `common.py`, and the address-format convergence decision. Still open,
none blocking the current experiment tracks:

- Rehearse the new Python promotion path once before or with the July 25 run:
  a manual dispatch with `promote=false` exercises build + verify + manifest
  without touching the catalog; the promote/recover/prune subcommands are
  unit-tested but have not yet run against live R2.
- Decide the reverse `wkb` column's future: populate locality-scale geometry
  (containment must then apply before the per-subtype cap, and HEAD needs
  geometry too) or drop the column and its query path. Correctly dormant
  either way; keep experimental until a complete candidate oracle exists.
- Replace the hardcoded seven-type ID matrix with a discovery job emitting the
  matrix via `fromJSON`; today an Overture type addition fails the monthly run
  closed until a workflow edit.
- Split `build_id_index.py`'s staging/build/metadata phases into separate
  scripts sharing `id_index_protocol.py` (the module extraction landed; the
  CLI flag matrix is still the main source of dangerous state combinations).
- Convert `build_shards.py` to `scripts/common.py` helpers at its next
  evidence-regenerating change (the pinned Monaco hash makes standalone
  conversion needlessly expensive).
- Catalog promotion is serialized only by the shared workflow concurrency
  group; the publish itself is fetch/compare then unconditional write, not an
  object-level CAS against manual or external writers. Before broader automated
  publication, adopt R2 conditional writes (If-Match) or a single serialized
  publisher.
- Re-enable shellcheck/pyflakes in the CI actionlint job after a dedicated
  quoting cleanup of the 71 pre-existing findings in the large workflows.
- Remove or consume the unconsumed research artifacts in the pipeline
  directory (`build_country_h3_index.py`, `extract_country_router.sql`), whose
  current output format was judged unfit to wire in.
- After the July 25 rebuild: re-measure the multi-bbox ambiguous-route share
  (wrapped antimeridian bboxes should sharply reduce it) and verify
  lineage-path hierarchies serve correctly, then re-size the exact-country
  comparator work against the new baseline before investing in it.
- Before the Places payload lands on the shared reader core (#81 review
  notes): replace the unchecked `as usize`/add in `range_reader.rs` slicing
  with `checked_add`/`try_from` (safe today only because the address preset
  caps a read at 256 KiB; a payload with a large `max_range_len` could
  overflow on 32-bit wasm), and either generalize or relocate the
  address-specific division-extension decode in the core (its dictionary has
  no explicit entry cap and a Places extension would today require editing
  the core, not supplying a preset). Also drop the now-dead `not_found`
  fallback and redundant length recheck in `cache.rs`.

### 8. Architecture direction: one binary family, one reader, one publisher

The largest structural risk going forward is quiet divergence, and #78 showed
the antidote works: measure, decide, converge. Three convergences should be
explicit decisions rather than defaults:

- **One range-readable binary family and one Worker range-reader core.**
  Addresses (lookup-safe gzip pages + self-describing extensions, decoder
  already measured at 434 ms cold / 156 ms median) and Places (compact
  spatial shard: lexicon, postings, records) should share a single reader
  core — checksum validation, bounded range coalescing, cache policy, page
  framing — with the format-specific payloads behind it. Building a second
  bespoke reader for Places would recreate the two-address-formats drift one
  level up. **Done in #81**: the core lives in `geocoder_core::pages` with
  the address spike as its first consumer; the Places compact shard is the
  intended second payload (harden the two §7 reader notes first).
- **One publication path.** The repo now carries three manifest/fan-in systems
  (rebuild finalizer, ID-index inventory chain, global build control plane).
  The control plane's per-family manifests should absorb address/Places family
  publication, with `finalize_rebuild.py promote` (plus a future If-Match CAS)
  as the only catalog writer. Do not let a fourth publication mechanism
  appear.
- **Offline/online logic parity.** Routing now lives in geocoder-core; the
  same treatment should follow for any logic a future evaluation harness needs
  (reranking, hierarchy assembly) so relevance work measures exactly what
  production serves.

### 9. Places geocoding: assessment (2026-07-16)

Position, from the July review and spike evidence:

- **The storage direction is settled and right.** Compact ~1M-place immutable
  spatial binaries plus a packed global head beat every measured alternative
  (per-token objects, KV/R2 pages, cell-local duplication) on object inventory
  and read fanout, and PostgreSQL was reasonably rejected on idle-cost grounds.
  Storage is a non-issue: Places + addresses + rollback is roughly 50 GB,
  under a dollar a month at R2 prices. Nothing in the spike needs re-litigating
  at the storage layer.
- **The dominant unmeasured risk is serial read-chain latency, not bytes.**
  A query walks lexicon → postings → record index → records: at least four
  *dependent* R2 round-trips before the first result, and every published
  number is a simulated local-disk read. At plausible warm edge RTTs the
  modeled 4–20 reads per query spans "fine" to "fatal." This is the first
  thing the prototype must measure, and the format should be laid out for it
  from the start (co-locate lexicon and posting spans, embed top results in
  the head, coalesce aggressively).
- **Relevance is the product risk.** Retrieval-correctness oracles prove
  encoding fidelity, not answer quality; no labeled evaluation exists. Keep
  the launch contract literal — name/brand tokens, last-token prefix,
  structured category, explicit lat/lon or context — and treat `coffee near
  me` as out of scope until a query planner, category aliases, and distance
  ranking exist and are evaluated.
- **Sequencing: reader first, shared with addresses.** Done through the
  de-riskers: #81 landed the shared range-reader core with the address
  payload, and #82 answered both cheap de-risking questions favorably — the
  global head fits one range-readable object (25.75 MB, ~7x less hit
  overfetch than the bucket layout), and bytes/place is stable on a Tokyo
  partition (122.9 vs 116.4). Next in this track, in order: (1) the two §7
  reader hardening notes; (2) CJK segmentation — the Tokyo build proved
  storage generalizes but the tokenizer does not (single-token space-free
  names, NFKD dakuten stripping); a segmentation/normalization decision must
  precede any multilingual serving claim; (3) the Places compact shard as
  the core's second payload, building toward the three-shard Worker
  prototype with real measured read-chain latency (the dominant unmeasured
  risk below).
- **Get real query data early.** Head hit rate, fanout distribution, and the
  no-result rate all depend on a traffic distribution that does not exist. A
  small gated beta with query logging — even divisions + addresses only —
  would inform every later Places decision more than another synthetic
  benchmark.
- The spike's own implementation gate (three real shards, Worker reader,
  measured p50/p95, labeled relevance at five, explicit stop conditions)
  remains the right bar. Nothing above weakens it; the shared-reader decision
  strengthens it by making the latency measurement reusable.

## Deferred geocoder feature-gap review

The current forward path is normalized exact/token/prefix search over divisions,
with aliases, hierarchy context, autocomplete, and match-quality reranking. It
is tolerant in the product sense but is not edit-distance, trigram, or
typo-correcting fuzzy search. Keep that terminology explicit in API and product
comparisons. The experimental regional Places path uses related exact/prefix
candidate logic only when `types=place` is requested.

Review these gaps after the smoke, manifest/catalog, and rebuild-readiness work:

- Measure true typo tolerance on independently labeled division and multilingual
  queries before choosing an edit-distance, n-gram, or query-rewrite design.
- Add bounded multilingual name variants with requested-language display and
  ranking behavior; do not put every language into one undifferentiated FTS
  field.
- Define structured address input and component-level match reporting for house
  number, unit, street, locality, region, postcode, and country.
- Add street and intersection search only after the Boston spatial
  address-to-segment/component join establishes association and ambiguity rules.
- Add regional POI/category/proximity search with explicit chain-name fanout
  routing. Keep `HEAD` limited to independently reviewed global landmarks.
- Define response accuracy/confidence fields that distinguish exact address
  points, candidate lists, future interpolation, street-level results, and
  administrative bbox or exact-containment results.

The likely serving shape is a small routing directory plus separate immutable
families: exact-address candidate shards partitioned by postcode prefix or
spatial cell; a hot street name/alias/component index separated from larger
segment detail; regional Places name/category shards; and spatial reverse-detail
cells selected only after country/region routing. Do not fold these much larger
families into the existing division shards.

## Potential production rebuild checkpoint

Do not trigger a rebuild without explicit approval. Before proposing one,
require the merge-only shard and ID smokes to be green, reconcile the complete
per-family manifests and atomic final catalog plan, pin the Overture release,
inventory expected files/bytes/row counts, confirm range and retry coverage,
and document promotion plus rollback checks. Treat scale or correctness defects
found during that rehearsal as hardening work, not reasons to relax gates.

### 2026-07-13 readiness audit: resolved

The 2026-07-13 no-go audit's five findings were implemented by #61 (single
finalizer gated on every enabled family, exact immutable-inventory readback,
prefix-collision rejection, live four-endpoint smoke, automatic rollback,
guarded retention) and validated by the hardened rebuild that promoted
`2026-07-13.0`. The audit's measured input/output inventory remains useful
sizing context: 10 division Parquet objects (5,788,444 rows), about 353.6 GB
of compressed ID-input Parquet, and a 2h06m-to-3h34m full-workflow envelope
with ID staging/build on the critical path.

The 2026-07-16 pass then moved the promotion, rollback, and prune logic into
unit-tested Python (`finalize_rebuild.py promote|recover`,
`scripts/prune_catalog.py`), raised retention to the worker's four-version
fallback window, added a worker cache-TTL grace before any object deletion,
and documented the interrupted-rebuild recovery ritual in
`docs/rebuild-recovery.md`. The scheduled July 25 run will rebuild and
auto-promote; with the rollback and smoke gates in place and a recent
successful hardened rebuild, it is accepted to proceed.

## Open decisions

1. Ratified exact-country byte, heap, cold-open, and warm-latency gates.
2. Political-perspective and synthetic `X*` routing policy, including which
   cases can ever resolve uniquely instead of falling through to `HEAD`.
3. Whether the experiment's conservative any-claim-boundary-to-`HEAD` rule is
   the production boundary contract, plus the antimeridian component
   representation.
4. Places regional byte budget, soft coverage floors, fame evidence, and
   low/high-fanout thresholds; do not enable `place` by default before fleet
   validation.
5. Address regional budget, adaptive postcode-prefix grouping, street-component
   association rules, and spatial join threshold.
6. Additive degraded-response/header contract and detail granularity.
