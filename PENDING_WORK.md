# Pending Work — 2026-07-17

This is the active roadmap. Completed PR history is intentionally omitted unless
its result constrains the next decision. The implementation baseline is
`06ad26b` (`main` after #94); the prior remote evidence is recorded in
`benchmarks/2026-07-17-remote-address-places-r2-evidence.md`, and the first
dispatched measurement evidence lives in the retained artifacts of address run
29550877740 and Places smoke runs 29552483748 (pre-#92), 29553945554
(post-#92), and 29554748712 (post-#93/#94, with per-stage attribution).

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
- The main-only, non-promoting two-task address workflow was dispatched against
  real Overture fragments and R2 (run 29550877740) and passed: US task 48
  (3,999,111 source rows, 3,999,106 retained, 541,958,847 fragment bytes, map
  157 s / reduce 148 s, map peak RSS 866 MB) and Mexico task 3 (3,998,978
  source rows, 40.29% retained). Partial/repeated resume, empty/stale restore,
  byte-identical local-oracle reduction (`local_oracle_match` true), cleanup,
  and a 2.03x retry-readback amplification were all verified from artifacts.
- The routed Places smoke now runs end to end from `main` after four fixes
  (#88 restored the corrupted setup-node pin, #89 fixed an invalid jq
  assertion, #90 added expected-vs-actual failure dumps, #91 retries the
  transient 500/"error code: 1104" propagation race). Two green dispatches
  (29552483748, 29553945554) passed strict producer/Worker equivalence on all
  ten cases; the automated technical classification is `optimize` with
  `launch_approved` false, and the relevance seed file's stop rule fired (two
  of six seed classes returned zero results). #92 (bounded posting reads),
  #93 (alt-name indexing + prominence-sampled fixture), and #94 (per-stage
  smoke capture) are merged. The post-#93/#94 dispatch (29554748712) is green:
  the `names.common` SQL works on real data, `local_name` (東京タワー) now
  returns Tokyo Tower, but `famous_unique` still returns zero (the
  density-gated packed head has no entry for its rare tokens), so the seed
  stop rule still fires on one class. Cold failures narrowed to
  `relevance_brand_with_context` (11 reads / 596,490 B) and
  `relevance_chain_name` (15 reads / 2,765,440 B), and per-stage capture
  attributes them to the records stage.
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

### 0. Fix release blockers before the July 25 rebuild (2026-07-16 review)

An external architecture review of the 2026-07-16 PRs (#75–#87) found two
verified defects that will fail or strand the next production rebuild. Both
remain OPEN as of #94 (`finalize_rebuild.py` still has no `id-inventories`
handling). Fix these before anything else in this document.

- `verify_release` in `scripts/finalize_rebuild.py` requires an exact
  version-prefix object set that omits the `{version}/id-inventories/` objects
  `scripts/id_index_protocol.py` now publishes and makes mandatory for v3
  locator dictionaries. The next full rebuild fails closed at finalize with
  `unexpected=[id-inventories/...]`. Add the inventory objects to the expected
  set and extend the `tests/test_finalize_rebuild.py` fixtures to include them
  so the gap cannot silently reopen.
- The `id-post` job deletes `{version}/staging/` — including the build
  `_SUCCESS` markers — before `finalize-release` runs. If finalize fails for
  any reason (including the defect above), ID metadata can no longer be
  regenerated and `--prefixes` patching is dead; with immutable version
  prefixes the whole month's build must be redone. Move staging deletion after
  successful finalize, or preserve the marker objects that finalize and
  patching depend on.
- ~~`.github/workflows/smoketest-places-worker.yml` pins `actions/setup-node`
  to a corrupted 64-hex-character ref~~ — FIXED by #88 (merged 2026-07-17); the
  workflow now starts and has produced two green evidence runs.

After the first two fixes, the `promote=false` dry run already planned in
section 3 is the verification that finalize passes over a real v3 build.

### 1. Close the Places `optimize` verdict: cold reads and relevance

The routed smoke was dispatched three times (29552483748, 29553945554,
29554748712) and every run passed strict producer/Worker equivalence; the
recorded decision is `optimize`, not `stop`. The blocking work is now (a) the
records-stage cold read plan and (b) the `famous_unique` head-admission gap.

Measured cold-gate status, latest run 29554748712 (gate: cold ≤ 1.0 s, warm
median ≤ 0.250 s, non-head reads ≤ 8, bytes ≤ 524,288):

- Eight of ten cases now pass. The two failures are
  `relevance_brand_with_context` (11 reads / 596,490 B) and
  `relevance_chain_name` (15 reads / 2,765,440 B). All warm medians pass.
- Per-stage capture (#94) attributes both failures to the result-record fetch:
  chain = 2,345,253 B in the `records` stage plus 368,400 B in `record_index`
  against 235 B of postings; brand = 503,524 B `records` / 80,800 B
  `record_index`. The 256 KiB-gap records coalescer drags dead bytes between
  scattered result records, exactly as hypothesized. Postings and lexicon are
  byte-trivial in every case.
- The Worker fetches RESULT_LIMIT = 25 result records but the handler
  truncates the response to 10 (`results.truncate(10)` in `handlers.rs`);
  ranking is fully decided before the record fetch, so fetching only the
  returned window is a free ~2.5x records-stage cut with no observable change.
- Cold worker time scales roughly with R2 read count (~60–130 ms per read
  across the three runs) because physical reads are awaited sequentially, both
  across stages and inside each `coalesced()` plan. Earlier runs flapped
  `shard_exact`/`shard_prefix`/`relevance_ambiguous_context` across the 1.0 s
  line on this server-side effect (e.g. shard_exact 1,473 ms worker time for
  8 reads / 11,949 B), so the ≤ 8-read budget is also the latency budget.
- #92 (per-entry coalesced posting reads + empty-intersection early-exit) was
  measured byte-for-byte identical pre/post on every case: each smoke clause
  matches exactly one lexicon entry, so the dead-gap span never occurs in this
  fixture. Kept as a correctness/bound improvement; postings were never the
  cold-gate lever.
- #93 re-baselined the fixture (confidence-ordered sample + `names.common`
  tokens): `relevance_category_near_me` dropped from 12 reads to 8 and chain
  bytes grew from 716,688 to 2,765,440 purely from fixture composition. Never
  compare byte/read numbers across fixture generations.

Next steps, in order:

- Fix the records/record_index stages: fetch only the returned result window,
  retune the two gap thresholds (64 KiB / 256 KiB) against the reads-vs-bytes
  gate pair, and issue the independent physical reads of a coalesced plan (and
  independent stages) concurrently instead of one awaited round trip each. Do
  not fork the range-reader core (decision 2).
- Relevance: `local_name` (東京タワー) is fixed by #93 and returns Tokyo Tower.
  `famous_unique` ("Tokyo Tower", no context) still returns zero: the packed
  head admits only tokens with ≥ 64 candidates (density-gated, then top-K by
  confidence), so a famous name built from rare tokens never gets a head
  entry and the context-free path has nothing to consult. Design a
  fame/prominence head admission rule; this is the remaining seed-rule
  stopper.
- Build the side-by-side top-five comparator panel for the six relevance seeds
  (Nominatim and the previously evaluated engines) and adjudicate coverage,
  local names, category semantics, and requested context. This has not been
  started.
- Design a bounded ranking component that can compare every eligible located
  candidate before applying distance. Do not present the current decoded
  top-window distances as complete near-me ranking.
- Keep `place` disabled by default until every seed class has a relevant
  top-five result, context ordering is correct, repeated order is stable, and
  the routed read/byte/latency gates pass.

Exit artifact: one routed Worker/relevance report with a proceed, optimize, or
stop decision backed by per-stage read evidence and the comparator panel.

### 2. Address: publication guard, then broader coverage

The two-task real-fragment rehearsal passed (see Current state); the bounded
producer shape is proven viable on real data. The verdict is proceed to the
next integration slice, NOT promotion. The next step is the object-level
publication guard (decision 3), which is still unimplemented.

- Add the object-level conditional-publication guard required by the shared
  finalizer path before any address (or Places) publication is automated. This
  is the chosen next address slice.
- Collect structured-retention and output-byte summaries across the planned 127
  tasks before selecting regional partitions or claiming planet storage.
- Validate the proposed exact endpoint and country/hash-range partition against
  the multi-task evidence. The current normalization contract is NFC,
  Unicode-whitespace collapse, and ASCII-only lowercasing; decide and version
  any broader Unicode folding before building publishable shards.
- Evaluate a bounded two-level/sparse side index or smaller serving partition so
  a cold exact lookup does not automatically fetch the observed 941,745-byte
  full index (98.7% of cold address bytes). Preserve exact predecessor
  selection and the three-range cap.
- Keep measuring Actions minutes, retry amplification, peak disk/RAM, and wall
  time on further task dispatches (task 48 measured 2.03x readback
  amplification). Keep partial runs undiscoverable and do not hydrate Overture
  on the request path.

Exit artifact: the publication guard merged plus a reviewed address
coverage/partition contract informed by multi-task retention evidence.

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

## Review follow-ups — 2026-07-16 architecture review

Findings from the external review of #75–#87, excluding the section 0
blockers. None block the next measurement runs; the Worker items are
graduation gates, the rest are robustness and hygiene debt.

### Worker gates before family graduation

- Memoize the parsed address side index and the Places routing catalog per
  isolate (the existing `DB_CACHE`/collection-memo pattern). Both are currently
  re-fetched from edge cache and fully re-parsed on every request — up to a
  4 MiB index parse per address lookup and a full serde parse plus O(n) bbox
  scan of a ≤4096-entry catalog per routed Places request.
- Replace the linear packed-head key scan (a `String` allocation per key,
  re-run per clause) with a binary-searchable layout or an isolate memo; the
  context-free "fast path" currently pays O(index) CPU per request.
- ~~Bound posting reads~~ — DONE in #92 (per-entry `coalesced()` reads at gap
  0, sum-of-lengths caps, early-exit on unmatched clause or emptied
  intersection). Two residues: (a) the multi-entry coalescing branch is
  exercised by no test and no smoke seed (every seed clause matches exactly
  one lexicon entry — the dispatch measured byte-identical reads), so add a
  seed or unit fixture whose prefix clause matches several lexicon entries;
  (b) `clause_candidate_counts` now reports 0 for skipped clauses while the
  producer oracle (`CompactShard.query`) still counts every clause
  unconditionally — the field is recorded but unasserted today; align or
  document before anyone promotes it into an assertion.
- Align range-cache keys to fixed blocks instead of query-shaped
  `(offset,length)` pairs so a global query mix does not fragment the edge
  cache across overlapping ranges of identical bytes.
- Close two decoder gaps: `decode_head_projection` accepts any finite f32
  confidence (shard records are u8/255-bounded), and the posting-offset
  subtraction in the shard reader is the one unchecked u64 subtraction in an
  otherwise fail-closed family (no monotonicity validation across lexicon
  matches).
- Finiteness-check reverse-collection bbox values before `normalize_lon`; a
  corrupt bbox from R2 JSON (e.g. 1e308) loops one 360° step at a time until
  the CPU limit kills the request.
- Decide `lineage_hierarchy` behavior when the lineage chain names ancestors
  with no row in the shard: it currently returns whatever resolved instead of
  falling back to the heuristic path, so a builder bug would silently degrade
  hierarchies the legacy path used to fill.
- At Places graduation, apply decision 2's no-fork rule internally:
  consolidate the three near-identical preamble/directory fetch blocks and the
  two projection decoders in `places_pages.rs`, and use core
  `haversine_distance` instead of the local copy in `handlers.rs`.

### Pipeline robustness

- Resume granularity is coarse in three places: `id-build` and
  `stage-registry` write one marker per 1024-prefix range so a runner kill
  near completion redoes the whole quarter, and
  `r2_verified_store.ensure_uploaded` fully re-downloads every already-verified
  artifact on resume even though keys are content-addressed and uploads are
  create-only — a HEAD size/metadata check gives the same guarantee.
- `post-finalize` recovery demotes a promotion that actually succeeded: if the
  runner dies after production smoke passes but before job success is
  recorded, recovery sees live == candidate and restores the previous catalog.
  Teach it to recognize a completed promotion.
- Absence detection greps wrangler stderr for substrings (including `404`), so
  an unrelated error whose text contains a marker is misread as genuine
  absence. Currently backstopped fail-closed, but match exact error codes.
- `addresses/address` staging is a single 180-minute job with no intra-type
  parallelism — the first thing to blow the 6-hour ceiling as Overture address
  coverage grows. Plan an intra-type split before that happens.
- Decide whether the stage-inventory and inventory-set layers of
  `id_index_protocol.py` stay mandatory. They defend a private bucket only CI
  writes, every new layer must be taught to finalize and cleanup (the section 0
  blocker is exactly this failure), and validate-by-reconstruction makes every
  schema addition a hard format break. The ETag/content-MD5 binding and marker
  protocol already close the real corruption windows.
- Small dedup debt: `prepare_address_verified_resume.py` carries a local
  non-atomic `write_json` beside `common.write_json`; `r2_verified_store.py`
  re-implements `sha256_file`; `_version_key` lives in both
  `finalize_rebuild.py` and `prune_catalog.py`.

### Repository hygiene

- Shrink `benchmarks/address-rowgroup-inventory-report.json` (100k lines,
  3.2 MB in git). Its consumers read only `plan.tasks` and
  `source_inventory.objects` (~4% of the file); keep those plus totals in git
  and publish the full per-row-group detail as a workflow artifact or R2
  object pinned by SHA.
- Promote the load-bearing encoders out of the `experiment_*` namespace:
  fixture generators and smoke-prep now import
  `experiment_address_compression`, `experiment_address_format_convergence`,
  `experiment_places_compact_index/compact_shard`, and
  `experiment_places_head_repack`, so production format contracts live in
  files named as throwaway. Move the format code into named modules and shrink
  the experiments back to drivers.
- Delete superseded experiments and their required-CI tests:
  `experiment_places_shard.py` (the older SQLite Places direction, imported by
  nothing but its own test) and `experiment_places_partition_compare.py` (the
  Tokyo partition question is answered; keep the reports). NOTE:
  `experiment_places_partition_extract.py` is no longer deletable — #93 made
  it the routed-smoke fixture extractor and deliberately diverged it from the
  pinned factory extractor (confidence-DESC sampling, `alt_names` projection);
  it now needs promotion out of the `experiment_*` namespace instead.
- Consolidate overlapping smoke workflows: `smoketest-r2-shuffle.yml` is a
  functional subset of `rehearse-address-r2-map-reduce.yml`, and
  `hosted-rowgroup-data-spike.yml` runs the same script on the same task
  indices as the rehearsal's first steps. One fast push gate plus one dispatch
  rehearsal loses no coverage.
- Decide the status of the extended-page/division-extension decode layer in
  `geocoder-core/src/pages.rs` (~200 lines, zero non-test consumers). It is
  the reserved path from the format-convergence decision, so either mark it
  explicitly reserved or defer it until division enrichment lands rather than
  carrying it silently.
- Pick one artifact dating convention: today's evidence mixes local-date names
  (`2026-07-16-live-service-baseline.md`) and UTC-date names
  (`2026-07-17-remote-address-places-r2-evidence.md`, this file's header).

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
- The dispatched two-task R2 rehearsal (run 29550877740) measured US task 48
  at 541,958,847 fragment bytes, 157 s map / 148 s reduce, 866 MB map peak
  RSS, and 2.03x retry-readback amplification, with `local_oracle_match` true
  on both tasks.
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
- Routed smoke runs 29552483748/29553945554 (pre/post #92, identical bytes):
  worst cold cases were `relevance_brand_with_context` 11 R2 reads / 796,409 B
  and `relevance_chain_name` 18 reads / 716,688 B; cold `worker_time_ms`
  scaled with read count at roughly 90–130 ms per sequential read (18 reads →
  1.9–2.3 s server-side). Warm medians were 71–180 ms with zero R2 reads on
  every case. Two of six relevance seeds returned zero results (both Tokyo
  Tower classes).
- Those fixture byte numbers expired with #93 (confidence-ordered sample plus
  `names.common` tokens). On the current fixture (run 29554748712) the worst
  cold cases are brand 11 reads / 596,490 B and chain 15 reads / 2,765,440 B,
  with the records stage holding 2,345,253 B of the chain total; one of six
  relevance classes (`famous_unique`) still returns zero.
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
2. Address Unicode normalization version, 127-task coverage, serving partition
   size/side index, and publication serialization.
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
- `docs/plans/2026-07-12-exact-country-decision-artifact.md`
