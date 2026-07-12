# Pending Work — 2026-07-12

This is the durable roadmap after the July architecture and experiment series.
The code baseline is `f8e8c25` (`main` after #53 and #54). Keep measured evidence
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

### Current production data

- Production data remains `2026-07-02.3`; no fleet rebuild is currently planned.
- Existing ID shards are v1. The v3 reader remains backward compatible and uses
  the legacy response path when the new metadata is absent.
- Reverse routing uses country bboxes only as a guardrail. Ambiguous overlap
  falls through to `HEAD`; no exact-country decision artifact is deployed.
- Forward search remains division-focused by default. No Places, address, or
  street artifact has been promoted to the production fleet.

## Decisions and constraints in force

1. Do not rebuild or promote a shard fleet until the relevant experiment has
   reviewed labels, explicit byte/heap/latency gates, and fail-closed metadata.
2. Keep required smoke work merge-only (`push` to `main`, plus manual dispatch).
   Do not turn the external-source/R2 smoke into a pull-request check.
3. Exact country containment is the preferred durable reverse router. Keep
   `HEAD` fallback for missing, invalid, boundary, multiply contained, or
   policy-ambiguous inputs. Test H3 only if the direct exact router misses an
   agreed budget.
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
  `territorial-primary` was 90,083,328 bytes and had zero drift on 5,000 global,
  200 territorial-boundary, and 2,400 nearby points, but it false-uniquely routed
  195 of 200 sampled all-claims boundary vertices. Excluding land claims is not
  conservative under the current any-boundary-to-`HEAD` rule.
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
  to build Monaco. This is the first runtime bottleneck to remove.

## Ordered work

### 1. Bound the merge-only smokes

The merge-only smoke split gives ID and forward/reverse separate workflows,
dependency filters, concurrency groups, and R2 prefixes.

- Trigger on merges to `main` and manual dispatch only, never on pull requests.
- Replace broad `scripts/**` path matching with only the scripts, schemas, and
  runtime dependencies each smoke actually executes. Research-only scripts and
  unrelated workflow wrappers must trigger neither smoke.
- Use the same pinned DuckDB generation as the production build path.
- Record before/after total and stage timings on the first merged run.

### 2. Ratify the exact-country gate and next comparator

The completed research-only direct artifact and decision report use pinned
release `2026-06-17.0`. They reject territorial-only and simplified variants
under conservative boundary semantics and add no Worker, R2, fleet-build, or
production routing integration.

- Decide whether the proposed 5 MiB hot-artifact gate stands. The valid direct
  all-claims oracle is about 174.6 MiB (183.1 MB).
- If the gate stands, build a bounded H3 or equivalent conservative interior
  cover paired with exact full geometry for boundary cells. Preserve land and
  territorial boundary blockers, compare to the all-claims oracle, and do not
  add a production dependency.
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
These are proposed starting points, not accepted production thresholds; every
current candidate fails either correctness or bytes.

### 3. Finish the remaining smoke-performance work

After the exact-country decision artifact, replace the global divisions export
with a query for only the Monaco division/area subset required by the
forward/reverse smoke. Preserve the production transformation, geometry, and
hierarchy semantics rather than substituting a fixture.

Keep one fixed prefix per smoke family and use the workflow concurrency guard
to cancel an older in-progress run before the replacement starts. A
release+SQL-SHA divisions-export cache is a fallback, not the first fix. Keep a
slower scheduled full external-source integration run if the bounded merge
smoke no longer covers that path.

### 4. Consider production exact-country integration only if gates pass

If the decision artifact meets the quality and resource gates, add a separate
review for Worker caching, artifact publication/rollback, failure behavior, and
production smoke coverage. Continue using ambiguous-bbox-to-`HEAD` fallback
until that integration is deployed and verified.

### 5. Places rank and routing audit

- Produce a compact current-release California audit union, not a shard.
- Independently label famous unique, local unique, and chain examples across
  sources, confidence deciles, and taxonomy branches.
- Stratify rank quality by root source and exact-name/alias fanout; compare
  byte-capped regional policies with soft taxonomy/geographic coverage floors.
- Design explicit low/high-fanout token routing. Only reviewed venue-level fame
  evidence may justify a future unique landmark in `HEAD`.

### 6. Address and street-context evaluation

- Spatially join the bounded Boston address sample to transportation segments
  and connector components; measure missing and contradictory locality/postcode
  context.
- Review 50–100 repeated-name, alias, boundary, unit, postcode, and context
  queries independently.
- Only then build a temporary regional exact-address candidate store and street
  dictionary to measure cold bytes, heap, and latency. Keep interpolation out
  of scope unless a later experiment proves ordering, parity, and coverage.

### 7. Minor hardening

- Define and fault-test additive degraded/partial-result signaling: required
  `HEAD` or catalog failure should fail the request; optional regional/Places
  failure should be visibly degraded with a coherent data version.
- Replace the current single-file build metadata behavior with a complete,
  immutable, multi-artifact manifest and atomic publication/linking.
- Add stage-level timing for catalog, cache, R2, deserialize, FTS, and merge.

## Open decisions

1. Ratified exact-country byte, heap, cold-open, and warm-latency gates.
2. Political-perspective and synthetic `X*` routing policy, including which
   cases can ever resolve uniquely instead of falling through to `HEAD`.
3. Exact boundary semantics and antimeridian component representation.
4. Places regional byte budget, soft coverage floors, fame evidence, and
   low/high-fanout thresholds; do not enable `place` by default before fleet
   validation.
5. Address regional budget, adaptive postcode-prefix grouping, street-component
   association rules, and spatial join threshold.
6. Additive degraded-response/header contract and detail granularity.
