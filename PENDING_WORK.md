# Pending Work — 2026-07-15

This is the durable roadmap after the July architecture and experiment series.
The code baseline is `8c9b00d` (`main` after #69). Keep measured evidence
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
137-candidate oracle in 434 ms cold wall time and a 156 ms warm median, and
strict cleanup removed both the Worker and its run-specific R2 objects. Worker
gzip/range-read cost is therefore not the immediate stop condition. Next sample
global completeness, page tails, side-index scale, and shard skew before any
catalog or production promotion behavior.

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
integration is deployed and verified.

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

### 2026-07-13 read-only readiness audit: no-go pending workflow hardening

No rebuild or production write was triggered. Production is healthy on
`2026-07-02.3`, and the latest upstream release remains `2026-06-17.0`. A new
build would therefore exercise the current v3 ID producer and workflow fixes
against the same upstream snapshot rather than ingest newer source data.

Measured candidate inputs and current output headroom are healthy:

- Divisions: 10 Parquet objects, 5,788,444 rows, and 5,508,502,487 bytes.
- ID inputs: 3,586,392,274 registry rows plus 944,242,075 current-release
  address/base rows, scanning about 353.6 GB of compressed Parquet.
- Current output: 261 forward shards (56,864,768 bytes), 252 reverse shards
  (32,690,176 bytes), and 4,096 ID shards (138,576,344,116 bytes). The largest
  forward, reverse, and ID objects are about 12.6 MB, 11.2 MB, and 32.4 MB,
  respectively, below the present 50 MB division-family warning threshold.
- The last successful full workflow took about 2h06m; forward/reverse published
  after about nine minutes, while ID staging/build determined the critical path.

The current workflow is not ready for another production dispatch:

1. `rebuild-shards` publishes the root discovery catalog independently of the
   parallel ID pipeline. The Worker health check requires forward, reverse, and
   ID metadata on the latest version, so normal publication can make `/health`
   fail until ID completes, and indefinitely if ID fails.
2. Scheduled/default retention runs in `rebuild-shards` immediately after that
   early catalog publish, while ID is still building. Keeping only the current
   incomplete version plus one fallback removes the deeper recovery margin.
3. An explicit version is format-checked but not rejected when its prefix
   already exists, so a manual dispatch can mutate supposedly immutable live
   objects. Partial country, forward-only, or ID-disabled runs can also publish
   an incomplete version as global latest; reverse-only creates an unreferenced
   prefix instead.
4. Forward/reverse collections contain per-object size and SHA-256, but the
   workflow readback verifies only collections, `HEAD`, and the router. ID
   metadata inspects Parquet formats but publishes neither per-shard sizes nor
   hashes; the live ID collection reports zero total bytes. `build-meta.json`
   is generated locally but is not uploaded.
5. The rebuild has no final live production smoke and no automatic catalog
   rollback if post-publication health, search, reverse, or ID checks fail.

Before dispatching, add one finalization job that waits for every enabled
production family, verifies exact immutable inventories, publishes the root
catalog once, runs live four-endpoint smoke checks, and only then performs
retention. Reject existing version prefixes, keep partial builds non-promoting,
disable or guard the July 25 scheduled run until this lands, use cleanup=false
for the first hardened rehearsal, and retain at least three complete versions
until the new version has passed soak checks.

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
