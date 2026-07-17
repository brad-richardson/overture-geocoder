# Pending Work — 2026-07-17

This is the active roadmap. Completed PR history is intentionally omitted unless
its result constrains the next decision. The implementation baseline is
`6ff382c` (`main` after #86); the current remote evidence is recorded in
`benchmarks/2026-07-17-remote-address-places-r2-evidence.md`.

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
- A main-only, non-promoting two-task address workflow now connects real reducer
  fragments to the verified R2 store, including partial/repeated resume,
  empty/stale restore, byte-identical local-oracle reduction, cleanup, and
  structured byte/resource/timing evidence. It has not yet been dispatched.
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

- Dispatch the main-only Places workflow. Retain independently cold/warm client
  and Worker time, physical reads, R2/cache bytes, full projections, point and
  context routing, catalog failure, duplicate IDs, and repeated order.
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

- Dispatch the main-only US/Mexico workflow and retain its real fragment,
  partial/repeated resume, empty/stale restore, byte-identical reduce, cleanup,
  retry-amplification, wall-time, RAM, disk, retention, and output-byte evidence.
- Collect structured-retention and output-byte summaries across the planned 127
  tasks before selecting regional partitions or claiming planet storage.
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
