# Pending Work — 2026-07-17

This is the active roadmap. Completed PR history is intentionally omitted unless
its result constrains the next decision. The implementation baseline is
`699696c` (`main` after #98); the current remote evidence is recorded in
`benchmarks/2026-07-17-remote-address-places-r2-evidence.md`, updated by the
2026-07-16 first end-to-end dispatch of both measurement workflows.

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
     - NEXT: scale-signal pass (CONUS Places + all-US address), a Worker
       smoke against retained slice data (re-run with cleanup=false),
       and the Places serving gates (chain_name read-chain redesign,
       comparator relevance panel, bounded located ranking + the
       measured multi-shard route_point seam recall hole from #112's
       review, per-shard head strategy).
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
