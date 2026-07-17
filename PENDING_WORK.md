# Pending Work — 2026-07-16

This is the active roadmap. Completed PR-by-PR history is intentionally omitted;
the merged changes, benchmark reports, and dated design documents remain the
record of how the current decisions were reached. The code baseline is `f9d2eb8`
(`main` after #83).

## Current state

- Production serves data version `2026-07-13.0`, built from Overture release
  `2026-06-17.0`. Health, forward, reverse, and ID checks passed after promotion.
- Forward search remains division-focused. No Places, address, or street family
  has been promoted.
- The production ID index is v3 with compact locator metadata and remains
  backward compatible with v1 artifacts.
- Reverse routing uses request coordinates and conservative country-bbox
  guardrails. Ambiguous routes fall through to `HEAD`; caller IP does not choose
  the country.
- The scheduled July 25 rebuild is accepted to proceed through the hardened
  build, verification, smoke, promotion, rollback, and retention gates. This is
  not approval for an additional ad-hoc rebuild or for promoting experimental
  artifact families.

The reconciled working tree adds the foundation needed by the next work. It is
not yet published, so the remote workflows below cannot be dispatched until
this branch is committed and pushed:

- one payload-agnostic range-reader core in `geocoder-core`, with the address
  decoder and compact Places reader as its first two consumers;
- one lookup-safe compressed address page family, with raw address levels as the
  source of truth and optional division IDs in a self-describing extension;
- a selected Places storage shape: compact spatial shards plus one
  range-readable global-head object;
- deterministic global-build manifests and non-promoting map/reduce controls;
- a complete 127-task address footer inventory, global source-object locators,
  checked range slicing, and capped payload-generic extensions;
- local hash-verifying shuffle/resume logic plus isolated address, Places, and
  R2 manual smoke workflows;
- exact reverse lineage materialized for the next fleet rebuild; and
- unit-tested Python promotion, recovery, and pruning commands.

## Decisions and constraints in force

1. Do not promote Places, address, street, or exact-country artifacts until their
   explicit correctness, bytes, heap, latency, and failure-behavior gates pass.
2. Keep one range-readable binary family and one Worker range-reader core.
   Format-specific payloads may differ, but framing, integrity checks, range
   planning, and cache behavior must not fork.
3. Keep one publication path. Address and Places manifests should feed the
   existing control-plane/finalizer flow; do not create another catalog writer.
   Before broader automated publication, add an object-level conditional write
   or otherwise enforce a single serialized publisher.
4. Preserve offline/online logic parity by putting serving decisions needed by
   evaluation—routing, future reranking, and hierarchy assembly—in
   `geocoder-core`.
5. Exact structured address lookup must preserve duplicate and ambiguous
   candidates. Do not destructively deduplicate keys, infer missing ranges, or
   claim one universal free-form address grammar.
6. Places storage and tokenizer version are settled. The remaining product gates
   are real Worker read-chain latency, labeled relevance, and explicit handling
   of unsupported broad or tail queries.
7. No exact-country candidate currently passes both correctness and byte gates.
   Keep ambiguous-bbox-to-`HEAD` behavior until a future candidate passes and a
   separate production integration review approves it.
8. Keep external-source/R2 smokes merge-only or manually dispatched. They should
   not become required pull-request checks.

## Execution priority

### 1. Run and review the address evidence workflows

The complete footer inventory now covers 32 objects, 473,576,753 rows, and 8,704
row groups. A 4,000,000-row/350 MB selected-column/72-row-group plan produces
127 tasks, so the 128-job control-plane gate passes. Global source-object indexes
are part of every locator; a task may safely span adjacent source objects.

Run `hosted-rowgroup-data-spike.yml` for the full US task, full Mexico task, and
smallest tail. Combine those reports with the current-release format-convergence
samples: 40,000 rows in Boston and every 35,042 address in the Tokyo box. Reject
the plan on any accounting mismatch or runner memory, disk, runtime, page,
fanout, or transfer-amplification gate.

Then run `smoketest-address-worker.yml`. It now builds a real ~4M-row task and
large side index, verifies the maximum-fanout UUID digest against the producer,
tests a miss, and reports Cache API/R2 reads plus stored, decoded, and
materialized bytes. Add exact/prefix/ambiguous structured cases only where the
endpoint contract defines them; the current binary lookup itself is exact.

Exit artifact: one reviewed address scale/Worker report with a proceed or
rescope decision. Keep live Overture hydration out of the request path.

### 2. Run the hash-verifying R2 shuffle rehearsal

The local resume layer and `smoketest-r2-shuffle.yml` are ready. Run the manual,
non-promoting workflow and retain its reports for partial completion, repeated
upload, empty-local restore, stale-local restore, and mandatory cleanup.
Existence is never sufficient: immutable SHA-addressed objects must match stored
size/metadata and a downloaded SHA-256, and a conflicting object is never
overwritten.

After that small rehearsal passes, connect the same manifest contract to real
map fragments and measure Actions minutes, retry amplification, peak disk/RAM,
and wall time. Keep partial runs undiscoverable. Replace the current secret-based
smoke credentials with a short-lived OIDC/broker path if Cloudflare support makes
that practical.

Exit artifact: a non-promoting remote rehearsal resumable from empty local state.

### 3. Run and score the Places Worker prototype

The CJK decision, packed-head reader, compact-shard reader, three-region fixture
builder, stage metrics, and isolated workflow are implemented. Run
`smoketest-places-worker.yml` to extract Boston, Tokyo, and Mexico City samples,
then measure packed-head hits and head-miss fallback through directory → lexicon
→ postings → record index → records against real R2.

Score the seed cases in `benchmarks/places-relevance-seed.json` independently of
the producer oracle. The oracle proves byte/reader equivalence, not relevance.
Expand the labels only after inspecting actual results for brand-with-context,
local-name, category-near-me, ambiguous-context, famous-unique, and chain-name
queries. Record cold/warm p50/p95, physical reads, cache hits, bytes, failure
behavior, and relevance-at-five.

The launch contract remains literal: name/brand tokens, last-token prefix,
structured category, and explicit location/context. Single-character CJK
prefixes, broad semantic queries, and candidate-tail enumeration are unsupported.
Do not enable `place` by default until the latency and relevance stop gates pass.

### 4. Rehearse and validate the existing production path

This is deadline-driven operational work alongside the address track.

- Before or with the July 25 run, manually dispatch the rebuild with
  `promote=false` to exercise build, verify, manifest, and the new Python
  finalizer path without changing the catalog.
- After the July 25 rebuild, verify that lineage-backed reverse hierarchies serve
  correctly and re-measure the multi-bbox ambiguous-route share. Wrapped
  antimeridian country bboxes and materialized lineage only take effect after a
  new fleet build.
- Re-size exact-country work from that measured baseline before investing in a
  new comparator.
- Record complete per-family inventories, promotion checks, rollback result, and
  retention behavior for the run.

No Places or address artifact should be added to this rebuild.

### 5. Keep exact-country research queued

The direct all-claims oracle is correct but about 183 MB. Measured simplified
candidates either exceed the proposed 5 MiB budget or change routing decisions.
Do not wire any current research artifact into the Worker.

After the post-rebuild ambiguity measurement:

- decide whether the proposed 5 MiB hot-object gate still stands;
- independently review border, coast, island, enclave, antimeridian, synthetic
  `X*`, and political-perspective labels;
- if justified, build a research-only conservative interior-cover comparator
  with exact unsimplified fallback and zero route-target drift; and
- ratify heap, cold-open, and warm-p95 gates only after a candidate passes
  correctness and stored-byte gates.

## Supporting engineering backlog

These items remain useful but do not block the first address measurement slice:

- replace the hardcoded seven-type ID workflow matrix with a discovery job that
  emits the matrix via `fromJSON`;
- split `build_id_index.py` staging, build, and metadata phases into separate
  scripts sharing `id_index_protocol.py`;
- convert `build_shards.py` to `scripts/common.py` helpers during its next
  evidence-regenerating change;
- decide whether to populate locality-scale reverse `wkb` correctly or remove
  the dormant column and query path;
- add object-level `If-Match` publication or enforce a single publisher before
  broader automated catalog writes;
- clean the existing workflow quoting findings, then re-enable
  shellcheck/pyflakes in the actionlint job; and
- remove or consume unfit research outputs from
  `build_country_h3_index.py` and `extract_country_router.sql`.

## Measured gates that still matter

### Addresses

- The lookup-safe gzip-page experiment retained the full reducer response at
  35.50 B/indexed row with 8.98 KiB p50 pages. A conservative all-473M-row
  diagnostic is 16.79 GB for addresses and about 25.54 GB combined with Places.
  These are bounded diagnostics, not a planet forecast.
- The isolated Worker decoded the observed 137-candidate maximum-fanout fixture
  in 434 ms on the first run-unique lookup and 156 ms subsequent median. Cache
  API hits and representative large-index behavior were not measured; the new
  real-shard workflow is ready but has not yet produced a remote report.
- The complete release inventory is 473,576,753 rows, 8,704 row groups, and
  33.17 GB of selected uncompressed source columns. Its 127-task plan has
  3,998,407 p95 rows and 292.9 MB p95 selected uncompressed bytes.
- The current-release format extension added 0.311 B/indexed row in the bounded
  Boston sample and 0.387 B/indexed row in the all-35,042-row Tokyo box. Tokyo
  address-level and containment labels agreed for 99.87% of rows; these are
  purposive single-box measurements, not global forecasts.
- In the measured hosted range, only 1,382,264 of 3,743,307 projected rows
  (36.93%) had both non-empty street and number. Global completeness and the
  product effect of rejecting incomplete rows remain open.
- Conservative normalized address keys can map to materially different
  coordinates. Exact lookup must return candidate lists.

### Places

- Compact spatial shards measured 116.7 B/place on a deterministic California
  sample and 122.9 B/place on a Tokyo sample. Storage shape generalized across
  those partitions. Tokenizer v2 now preserves dakuten and adds CJK bigrams;
  its relevance effect is not yet scored.
- The packed global head fits one range-readable 25.75 MB object. The unresolved
  risk is its hit rate and real request latency, not whether it can be packed.
- A normal query still has at least four dependent logical reads before the first
  result. The shared Worker reader and stage instrumentation compile for wasm,
  but real R2 measurements are still pending.

### Producer

- One hosted projection processed 1,415,000 addresses from 24 row groups in
  6.11 seconds at 764.8 MB peak RSS. It was one eligible range, not a skew
  sample.
- The hosted reducer retained 1,382,264 rows and completed in 2m38s; reducer peak
  RSS was 715.4 MB. Its earlier uncompressed 148.1 B/row output is rejected as a
  final format.
- Initial global stop gates remain 12 hours, 12 CPU on the factory equivalent,
  48 GiB RAM, 700 GiB temporary disk, and 40 GB combined compact
  Places/address output per release.

## Open decisions

1. Address regional byte budget, partition rule, incomplete-row coverage policy,
   structured endpoint contract, and Worker latency/heap gates.
2. Places latency gates, relevance labels, head eligibility, fame evidence, and
   low/high-fanout thresholds.
3. Publication CAS/serialization and additive degraded/partial-result signaling.
4. Reverse `wkb` disposition and post-rebuild exact-country priority.
5. Exact-country byte/heap/latency gates plus synthetic-code,
   political-perspective, boundary, and antimeridian policy.

## Historical references

- `docs/plans/2026-07-14-global-places-address-processing-design.md`
- `docs/plans/2026-07-12-exact-country-decision-artifact.md`
- `docs/plans/2026-07-12-id-locator-scale-gates.md`
- `docs/plans/2026-07-11-address-street-experiments.md`
- `docs/places-search-spike.md`
- `benchmarks/address-format-convergence-report.md`
- `benchmarks/address-format-convergence-tokyo-report.md`
- `benchmarks/hosted-address-compression-report.md`
- `benchmarks/hosted-address-worker-decoder-report.md`
- `benchmarks/places-compact-shard-factory-report.md`
- `benchmarks/places-head-repack-report.md`
- `benchmarks/places-nonca-partition-report.md`
- `benchmarks/address-rowgroup-inventory-report.md`
- `docs/places-tokenization-decision.md`
- `benchmarks/2026-07-16-live-service-baseline.md`
