# Pending Work — 2026-07-13

This is the durable roadmap after the July architecture and experiment series.
The code baseline is `62ca869` (`main` after #59). Keep measured evidence
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

### Current production data

- Production data remains `2026-07-02.3`; no fleet rebuild is currently planned.
- Existing ID shards are v1. The v3 reader remains backward compatible and uses
  the legacy response path when the new metadata is absent.
- Reverse routing uses country bboxes only as a guardrail. Ambiguous overlap
  falls through to `HEAD`; no exact-country decision artifact is deployed.
- Forward search remains division-focused by default. No Places, address, or
  street artifact has been promoted to the production fleet.
- No production shard fleet was rebuilt or published during this work. The
  merge-only #59 shard smoke passed. The ID smoke failed before preview Worker
  deployment because its bounded registry prefixes contained no historical
  path-null row.

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
5. Keep Places regional and `HEAD` division-focused. Confidence measures feature
   existence, not venue fame; brand/category priors and raw source count are not
   adequate fame signals. Chains and other high-fanout names require query
   geography or caller location.
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
  retained historical row.

## Pause checkpoint

The original checkpoint paused after merged PR #59 with its merge-only runs in
progress. The later status check found the shard smoke passed and the ID smoke
failed before deploying its isolated Worker. A bounded registry sample cannot
guarantee sparse historical rows, so the immediate follow-up is an explicit
smoke-only historical sentinel that exercises inventory fan-in, v3 encoding,
publication, and the isolated Worker path. Production remains intentionally
unchanged.

After the ID smoke passes, prioritize immutable per-family manifests and atomic
catalog publication (starting with exact ID file/hash inventories), then build
the research-only conservative exact-country comparator. Keep the Places
label/rank audit and Boston address-to-transport spatial join queued after that
smoke/read and reverse-routing work.

## Ordered work

Finish the shard-smoke and reverse-country work in items 1–2 before resuming
forward-geocoding expansion. The Places and address/street prototypes in items
3–4 are retained research baselines, not abandoned work, but they should not
drive production shard or API changes until the smoke/read path and conservative
reverse router are settled.

### 1. Monaco subset and generated-object read smokes (ID follow-up in progress)

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
the bounded registry sample had no retained historical row. Keep that assertion
and make the historical smoke input deterministic before declaring the Worker
path complete.

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

### 3. Places rank and routing audit (prototype exists; promotion blocked)

The regional CA-bbox builder, opt-in `types=place` request path, bounded routing
experiments, and fail-closed experimental manifest are implemented. No Places
artifact is in the production catalog. Promotion remains blocked on reviewed
ranking labels because confidence measures feature existence rather than fame,
and the tested brand/category/source-count prominence formula was rejected.

- Produce a compact current-release California audit union, not a shard.
- Independently label famous unique, local unique, and chain examples across
  sources, confidence deciles, and taxonomy branches.
- Stratify rank quality by root source and exact-name/alias fanout; compare
  byte-capped regional policies with soft taxonomy/geographic coverage floors.
- Design explicit low/high-fanout token routing. Only reviewed venue-level fame
  evidence may justify a future unique landmark in `HEAD`.

### 4. Address and street-context evaluation (research artifacts exist; no serving path)

Current-release address/source extraction, the Massachusetts sizing artifact,
and the bounded Boston transportation name/connector-component prototype are
complete. They have no Worker, API, catalog, R2 publication, or production-build
integration. The next required experiment is the spatial address-to-segment and
component join; do not begin production forward address/road shards before it
and the independent query review pass.

- Spatially join the bounded Boston address sample to transportation segments
  and connector components; measure missing and contradictory locality/postcode
  context.
- Review 50–100 repeated-name, alias, boundary, unit, postcode, and context
  queries independently.
- Only then build a temporary regional exact-address candidate store and street
  dictionary to measure cold bytes, heap, and latency. Keep interpolation out
  of scope unless a later experiment proves ordering, parity, and coverage.

### 5. Manifest/catalog and operational hardening (next implementation priority)

- Define and fault-test additive degraded/partial-result signaling: required
  `HEAD` or catalog failure should fail the request; optional regional/Places
  failure should be visibly degraded with a coherent data version.
- Replace the current single-file build metadata behavior with complete,
  immutable, per-family manifests and atomic publication/linking. Start with
  exact ID per-file/hash inventories, then publish the final release catalog
  only after every referenced artifact and family manifest exists.
- Add Worker request-stage timing for catalog, cache, R2, deserialize, FTS, and
  merge. Keep this distinct from the workflow build-stage timings above.

### 6. Consider production exact-country integration only if a future candidate passes

Every current candidate fails correctness or stored bytes. Only after a future
candidate passes the ratified gates should a separate review cover Worker
caching, artifact publication/rollback, failure behavior, and production smoke
coverage. Continue using ambiguous-bbox-to-`HEAD` fallback until that
integration is deployed and verified.

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
