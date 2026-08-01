# construction-v1: current state

Last updated 2026-08-01 after Stage 1 landed end to end and the rebuild-blocker
queue was agreed with the operator. See "Rebuild queue, 2026-08-01".

This is the operational snapshot for construction-v1. It intentionally contains
only the current milestone, measured blockers, next actions, and frozen
decisions. Dated documents in this directory preserve the evidence and history;
they do not override this file unless their findings have been incorporated
here.

## Current milestone

The previous milestone -- **reverse serving for both families** -- is **MET**
as of 2026-07-31T19:05Z. v2 release `2026-07-31.0` is live and `/v2/reverse`
answers `types=poi` and `types=address` from planet-scale point-family indexes;
it no longer returns `capability_unavailable`. Both reverse builds, the slice
promotion, the release publication, and the catalog CAS are all done. See
"Promotion result, 2026-07-31".

**The next milestone is AGREED (2026-07-31): forward search correctness.**
Operator approved Stages 0 and 1 of
`docs/plans/2026-07-31-search-quality-and-street-layer.md` (merged #220). The
planet rebuild and the street layer both wait behind it.

The justification is measured, not aesthetic. Against the build promoted today:

- `q=Seattle&limit=10` returns ten POIs at relevance 1.0 -- UPS Store, Verizon,
  KeyBank -- and **the city of Seattle does not appear at all**;
  `types=locality` returns it at 0.8408.
- `q=paris` returns Dessirier, Rexel, Midas; **Paris is absent from its own
  query**.
- `q=Eiffel Tower`, `q=Space Needle`, `q=Statue of Liberty` all return zero.

### QUEUED: Stage 0 -- make improvement measurable

Prerequisite for claiming any Stage 1 result. Nothing below is falsifiable
without it, and today's promotion smoke was red for months on an assertion
nobody could evaluate.

1. Curated global gold set (~20+20), still unbuilt. Balanced regions/scripts,
   gold from open primary/government sources, never from a compared provider.
2. **Seam stratum** (~10 cases): queries that are simultaneously a division name
   and a POI context token -- Seattle, Paris, Berlin, Monaco. Assert the
   division wins at rank 1 and named POIs may follow.
3. **Inverse seam** (~5 cases): a POI must beat a same-named division.
4. **Type-composition metric** alongside rank@k: fraction of untyped queries
   whose top-1 has the expected feature_type, plus a starvation check. rank@k
   alone scores `q=Seattle` as an ordinary miss and never reveals that the city
   was displaced by ten tied results.

Harness exists: `scripts/benchmark_v2_forward.py` (rank@1/@10, MRR, `--compare`,
`--assert-recall`). The metric and strata are what is missing.

### QUEUED: Stage 1 -- Worker-only fixes

No rebuild, no format change, no new R2 reads, no new request identity. All four
are query-path changes gated by Stage 0 measurement.

1. **Field-mask-aware rerank.** `field_mask` (name 1 / brand 2 / category 4 /
   context 8) is emitted at `places_transform_v1.rs:449-475`, stored, and
   decoded at `places_construction_v1.rs:144, 600, 624` -- then never consulted
   when ranking. Demote candidates whose query tokens matched only the context
   bit. Fixes the `q=paris` / `q=Seattle` pollution.
2. **Division/POI seam calibration.** Division score is `importance / 2` clamped
   (`v2.rs:1646-1647`), capping near 0.9; POI score is raw saturated
   `confidence` (`v2.rs:1667`), so any confidence-1.0 POI buries every division
   at the merge sort (`v2.rs:2320`). Put POI confidence in the same static-prior
   band so an exact division match cannot be starved out.
3. **Two-token locality routing.** `locality_suffix_candidates` gates on
   `(3..=4).contains(&tokens.len())` (`v2.rs:1709`, PR #214); widen to `2..=4`
   with suffix length 1 when `len == 2`, keeping the existing "only when head
   returned empty" and "no explicit proximity" guards. Fixes `IKEA Berlin`.
   Exact-locality match is required, so `Eiffel Tower` cannot misroute on
   "tower".
4. **P6 for divisions:** relative score cutoff and deterministic tie-breaks
   (`query/merge.rs`). The only ranking-research item never landed; also kills
   the arbitrary ordering inside the ten-way 1.0 tie.

**Not in Stage 1** (recorded so scope does not creep): the head cap of 10, the
saturating confidence byte, the missing fame signal, and famous-unique `e2:`
admission are all Stages 2-3 and need a planet head rebuild plus a
`PLHD0002 -> PLHD0003` format bump. The street layer is Stage 4 and is the only
genuine one-way door in the plan.

All construction work runs under operator request
`88b7f17149fd5d75bf64720f0640d2cbe8aeb5ead750d279c1881f9bd5332614`.
Forward Address completed under it; Places runs `30226086949`, `30263207263`,
and `30288619536` used the same request, and `30288619536` remains the
finalize-only resume source (run `30305749838` cannot be, because its head job
was intentionally skipped and the recovery gate requires one successful prior
head job).

The typed per-workflow confirmation strings were removed on 2026-07-31 (PR
#219). Dispatch cost is not a constraint on public runners. `construction-v1.yml`
still takes an `EXECUTE_CONSTRUCTION_V1::` string, but that is its **input
format** — it carries the request digest and every cost parameter, keys the
concurrency group, and feeds `construction_v1_control.py admit-dispatch` for
byte-verification. It is not a gate and must not be removed without redesigning
that contract.

## Rebuild queue, 2026-08-01 (AGREED WITH OPERATOR)

The next planet rebuild waits behind this queue. Ordered by what it protects,
not by size. Full detail for every numbered item is in
`2026-07-31-promotion-copy-and-efficiency.md`.

### Wave A — independent, parallelizable, land first

These three touch disjoint files and can proceed concurrently.

| item | what | where | why now |
|---|---|---|---|
| **9** | Bounded retry of a *definite* R2 5xx, kept distinct from an ambiguous read timeout | `scripts/r2_verified_store.py:643` | Already discarded a 4h17m run after Places had fully succeeded |
| **7** | Reverse `max_parallel` 2 -> 4 | `reverse-v2.yml` dispatch input | Workflow already permits 4; last run used 2 across 16 ranges |
| **13** | aarch64 wheel hashes, then construction onto `ubuntu-24.04-arm` | `.github/requirements-hosted-rowgroup.txt`, `construction-v1.yml` | **Operator has observed ARM runners faster and wants it in scope.** 18 jobs elsewhere already run ARM; construction's 8 are all x86 |

### Wave B — depends on Wave A

| item | what | blocked by |
|---|---|---|
| **12** | Build the construction binaries **once** in `admit`, download in map/reduce/head | 13 — the artifact must be built for the target architecture |
| **6** | Reduction records into R2 beside the markers, not GitHub artifacts | — (independent, but larger than Wave A) |

### Wave C — decides whether the rebuild is worth paying for

**Wide cap simulation across regions.** Extend
`benchmarks/probes/2026-07-31-poi-type-prior-probe.py` to every gold-set token
across diverse regions and apply the admission order to direct Overture source.
A three-token check on one city already caught a regression (Part 6g), so this
is the cheapest de-risking available.

It also has to answer a measured problem: on a 38,182-record Monaco slice,
**98 `(token, prominence)` groups already hold more than 10 tied name matches**,
so the cap falls back to `feature_id` — UUID order, which is RC1 one level down.
The category prior separates *classes*, not *instances*.

### Wave D — the operator priority

**Item 11 / Track C: one resumable planet job replacing four workflows.** The
planet path is 4 workflows and 22 jobs (`construction-v1` 8, `reverse-v2` 3,
`release-slice-families` 6, `promote-v2-release` 5), each boundary a
GitHub-artifact hand-off plus a fresh environment plus a re-authentication.
Track C (`2026-07-28-planet-build-wall-clock-review.md:411`, Wave 5,
**unexecuted**) is the existing design. It subsumes item 12 by keeping binaries
warm, and pairs with items 1/6/9 as the copy-minimization half.

Next deliverable is a written scope against the current phase boundaries, not an
implementation.

### Still open, unscheduled

RC2 (two-token intersection of ten-deep lists), RC3 (three tokens never touch
the head), duplicate collapse, and `head_result_cap` 10 -> 64. These are why
`Eiffel Tower`, `Space Needle` and `Statue of Liberty` all still return **0
features** in production as of 2026-08-01, and a rebuild alone does not fix
them.

## Current snapshot

The checkpoint on `main` is `8aea031`. On top of the forward publisher and
finalizer series it carries the v2 promotion series (#194, #197-#201), the
reverse construction/serving series (#202, #204-#210, #212), the forward
quality fixes (#213 address locality aliases, #214 Places locality suffixes),
the ID-lookup route (#209), the open-geocoder benchmark harness (#211, #216),
the read-only Address density probe (#215, #217), the ARDX0002 per-field
dictionary code widths (#218), the workflow confirmation-gate removal (#219),
and the `promote-slice` reduction-artifact fallback (`8aea031`). There are no
open construction-v1 code PRs.

`8aea031` matters for the promotion immediately ahead. `promote-slice` fetched
reduction records with `gh run download --pattern 'cv1-reduce-*'` only, but a
finalize-only recovery run runs no reducers and so publishes no such artifact —
it carries the reduction set it authenticated and reused as
`cv1-resume-reductions`. The named Places source, run `30323929757`, is exactly
that shape: `cv1-control` plus `cv1-resume-reductions`, zero `cv1-reduce-*`.
Promotion would have failed at the download step, after both planet reverse
builds were already paid for. The fetch now prefers the per-batch artifacts and
falls back to the resume set, and still fails closed when a run carries
neither. Verified against the real artifacts: `30323929757` falls back and
flattens 16,601 Places records with no duplicate partition id, while Address
run `30215529919` keeps the per-batch path at 117 batches and 581 records.

### Live serving

**v2 release `2026-07-31.0` is live as of 2026-07-31T19:05Z**, backed by
protected core `2026-07-18.0` and Overture `2026-06-17.0`, with both families
served from `slice-2026-07-30.0`. Global-head `/v2/forward`, routed
`/v2/forward`, structured Address `/v2/forward`, division `/v2/reverse`, and
release-pinned ID lookup all return the advertised build.

**Point-family `/v2/reverse` is live.** The milestone is met. Verified against
the public endpoint, not only by the workflow's own smoke:

- `types=poi` at 47.6205,-122.3493 returns Space Needle,
  `landmark_and_historical_building`, `distance_m` 0.53;
- `types=address` at the same point returns 400 BROAD Street, Seattle WA 98109,
  `distance_m` 0.81;
- `types=poi,address` returns both; an untyped query still answers from
  divisions.

`docs/api-v2.md` was updated in the same change — it still claimed `poi` and
`address` were rejected.

The promotion's own `/v2/forward` smoke FAILED, and it is the recorded
false-red: 10/10 attempts logged `OK divisions (q=Berlin, build 2026-07-31.0)`
followed by `FAIL places: no features for Eiffel Tower`. Live probes confirm
the shape is name-only global retrieval, not a promotion defect — `Eiffel
Tower` and `Space Needle` each return zero features while `Eiffel Tower Paris`
returns 3 and `Space Needle Seattle` returns 2. The catalog CAS itself
succeeded and nothing auto-recovered. See open gates 3 and 4.

### Reverse execution, 2026-07-31

**Both planet reverse builds are complete and green.** Every job on both runs
succeeded: the plan job, all 16 bucket ranges, and the marker-last
"reconcile all ranges and publish the binary reverse catalog" job.

- **Places reverse execute** run `30595904973`, source
  `88b7f171...5332614:30323929757`, into
  `slice-2026-07-30.0/families/places/reverse/`. 16 ranges at `max_parallel=2`,
  2h 15m wall. Published catalog: 75,631,061 records over 16,511 cells and
  16,528 artifacts, 7.82 GiB, 111.03 B/record, largest object 97.6 MB.
- **Address reverse execute** run `30599227663`, source
  `88b7f171...5332614:30215529919`, into the same slice, 2h 33m wall. Published
  catalog: 431,705,590 records over 4,230 cells and 4,247 artifacts, 21.85 GiB,
  largest object 374 MB.

Both catalogs' record counts match their plans exactly. The slice claim key is
`{version}/claims/{family}.json`, so the two families claimed independently and
both now sit in `slice-2026-07-30.0` — which still has no
`{version}/slice-manifest.json`, so it is not yet finalized.

**ARDX0002 is validated at planet scale.** Addresses measured **54.36
B/record**, below both the probe's 57.65 B/record on the densest cell and the
ARDX0001 Seattle basis of 59.58 — the per-field widths came out cheaper than
the fixed-u16 format they replaced, not merely survivable. Aggregate 21.85 GiB
against the 48 GiB ceiling (45%), and the largest single object is 374 MB
against the 2 GiB serving cap. The probe's 35.9 GiB projection carried a 1.5x
reserve; 35.9/1.5 = 23.9 GiB against 21.85 GiB actual, so the projection model
was accurate rather than lucky. Addresses also finished faster than its record
count suggests because it has 4,230 cells to Places' 16,511 — fewer, larger
objects amortize per-object overhead.

Both families passed the preserved Europe execution rung through publication
and marker-last completion. Address has now also passed the hosted planet rung
through marker-last R2 publication. Places now passed the hosted planet rung
through marker-last R2 publication in finalize-only run `30323929757`.
Places planet runs `30226086949` and
`30263207263` passed fail-closed admission, all 89 maps, planning, and all 128
reducers. Head-only recovery `30288619536` authenticated those outputs, skipped
every paid upstream phase, completed the global head, then failed before
publication because finalize looked for `head.json` under one extra directory
component. Finalize-only recovery `30305749838` proved that correction and
authenticated all retained inputs, but a single staging GET body timed out
during the pre-publication admission pass. Run `30323929757` reused those same
authenticated inputs, completed the corrected bounded finalizer in 56 minutes
50 seconds, verified all 40,931 final members, and wrote the completion marker
last. There is no remaining measured forward planet blocker.

### Places

Planet run `30226086949` was admitted at main workflow SHA `40a7682` for the
request above, completed all 89 map jobs at concurrency 4, planning, and all
128 reducer jobs. Fresh resume `30263207263` repeated those successful phases
and preserved a complete current plan/reducer artifact set.

Both global heads failed during the first candidate tree merge with DuckDB
`max_temp_directory_size` exhausted at 4.2 GiB. The first failure established
that the deliberately derived quarter-share spill cap was the blocker, not
runner ENOSPC, the 17 GiB whole-stage scratch watchdog, RSS, R2 transport, or a
candidate-count admission cap. The second failure established that the
workflow's compatibility shim had not changed the producer used by Places:
`places_construction_v1.py` dynamically loads `address_construction_v1.py` as
`H.PLACES.A`, while the shim mutated an unrelated top-level import.

The recovery workflow now has a Places-only, explicit head-only resume path.
Before skipping paid phases it authenticates the prior failed run, canonical
request, byte-identical contract, exact 89-marker set, contiguous reducer
matrix, all 128 successful reducer jobs and artifacts, every reducer ledger
fragment, and all 16,601 reduction records. It then carries those reducer
minutes into the head ledger. The compatibility shim mutates `H.PLACES.A`,
changes its spill share from four to two, and asserts the effective
9,126,805,504-byte DuckDB limit is exactly half of the admitted
18,253,611,008-byte scratch cap before executing the head. The independent
whole-stage scratch guard and runner floor remain unchanged.

Head-only run `30288619536` proved that recovery path and closed the planet
head gate. The head asserted the effective 9,126,805,504-byte spill cap, then
completed in 12,381 seconds (207 rounded runner minutes). It admitted
62,573,648 candidates and produced 30,841,082 records / 13,971,501 index
entries across all 4,096 populated shards. It hydrated and released
7,436,087,621 bytes from 89 staged objects with 1,504,727,912 bytes peak
staged-cache residency, and published 4,098 staged objects /
5,141,583,720 bytes.

Finalize in run `30288619536` failed in 43 seconds before publication. Uploading the single
`head/` directory flattens its contents into `cv1-head`, so downloading it at
`headdl` creates `headdl/head.json`; the workflow passed
`headdl/head/head.json`. The finalizer rejected the missing `--head` result
before any R2 publication or completion-marker write. The scoped recovery
corrects both consumers of that path and adds an authenticated finalize-only
resume from `30288619536`, preserving the successful head rather than spending
another 207 runner minutes. It also avoids appending reducer ledger fragments
twice on recovery; the retained resume plan already carries all 502 reducer
minutes. The fail-closed head projection is raised from the disproved 90-minute
estimate to the job's 330-minute timeout.

Finalize-only run `30305749838` then authenticated the complete plan, all
16,601 reductions, and the successful 4,096-shard head from `30288619536`.
Every upstream execution job remained skipped. The finalizer reconciled an exact
set of 40,931 members / 51,814,660,317 bytes (40,929 staged members plus two
manifests), then spent 4 hours 17 minutes in the serial admission pass before one
R2 `GetObject` streaming body raised `ReadTimeoutError`. The exception occurred
after `get_object` had returned, outside botocore's request retry loop. Admission
had not completed, so the barrier correctly prevented every final-prefix PUT,
completion marker, final result, and ledger write. All authenticated source
artifacts remain reusable.

The scoped correction keeps that barrier but removes avoidable latency and the
single-transient abort:

- staging hydration uses one GET whose response supplies length and SHA metadata,
  rather than a HEAD followed by GET;
- a mid-body timeout or truncated response retries the whole GET with bounded
  backoff;
- admission hydrates five members concurrently, derived from the untrusted
  5 GB contract cap and the 25.6 GB runner disk floor;
- after admission proves producer-recorded identities, upload uses the measured
  largest 209,194,480-byte object and all 16 persistent-client workers;
- whole-slice stored-byte metadata verification uses all 16 workers; and
- logs report admission, upload, and verification progress every 1,000 objects.

The same change removes full GET read-back from immutable staging uploads and
resumes. A new object is proved with HEAD, create-only PUT, HEAD; an existing
object needs one proof HEAD. The HEAD proof compares size, the store-computed
single-part ETag/content MD5, and recorded SHA-256 metadata.

Finalize-only run `30323929757` execution-proved that correction on the exact
planet set. Admission used five workers and completed in 24 minutes 56 seconds;
publication used 16 workers and completed in 27 minutes 02 seconds; whole-slice
verification used 16 workers and completed in 4 minutes 24 seconds. The
40,931-member / 51,814,660,317-byte exact set reconciled, all stored-byte
metadata verified, and
`construction-v1/86558218e2b67db0e0249abbee0c6d17650dea43467ed14c59789bc60c7bacb0/markers/finalize/places.json`
was written last. The finalizer completed 81,858 logical staged-object
hydrations, two passes over each of the 40,929 staged members; the one-GET path
avoided 81,858 redundant Class B HEAD requests relative to the old
implementation. Silent bounded whole-GET retries mean physical GET attempts can
be higher than the logical counter. The full result and operation analysis is
`docs/plans/2026-07-28-planet-places-publication-result.md`.

The Europe run covered 43.9% of the planet. After PR #176 merged, all five
phases completed and head produced all 4,096 populated shards. The full head
measured 2,022.27 seconds wall time, 8,179,167,232 bytes peak RSS and
5,399,313,835 bytes peak sampled runner disk. It hydrated and released all
3,088,544,880 input bytes, with 855,605,976 bytes peak staged-cache residency,
and published 4,098 staged objects / 2,134,262,243 bytes.

The old failure was driven by shard fan-out, not candidate row count. Batching
the write at 256 shard ranges completed at an 8x smaller DuckDB memory limit
and reduced files per partition from 113 to 3, maximum 4. The post-merge Europe
run proves the fix through encode and verify, not only through the former
failing statement.

`DEFAULT_HEAD_SHARD_BITS = 12` remains frozen. The encoder entry cap is a floor
on shard count and serving fetch granularity is the deciding constraint.

The earlier "~40 seconds" rerun estimate covered only the former failing
DuckDB statement. A full 4,096-shard head takes about 34 minutes at Europe
scale. The old projected ~1% planet disk residual remains a planet-preparation
gate, but Europe exercised the complete phase without approaching it.

On merged `94eae08`, finalize then completed in 136.37 seconds at 247,562,240
bytes peak RSS. It reconciled 20,567 exact-set members (12,109 serving, 8,456
positions, 2 manifests), wrote the marker last, and produced 20,568 local files
/ 21,039,995,295 bytes including that marker. It hydrated and released all
41,130 staged reads / 42,060,324,048 bytes.

### Addresses

The first execute run, `30207544725`, completed all 127 map tasks but exposed a
workflow transport defect before useful reduce work: `cv1-plan` omitted
`control/contract.json`. PR #183 added the contract and made a fresh dispatch
resume from an authenticated prior plan ledger without changing the request
hash or R2 staging namespace.

Resume run `30215529919` then completed successfully in 17,441 seconds wall
time. All 127 map markers were reused from immutable R2 staging, the compact
plan emitted 117 reducer jobs, all 117 reducers passed, the Address head no-op
passed, and finalize completed in 4,029 seconds. Finalize reconciled and verified
10,931 objects (581 serving, 10,348 per-record positions, and two finalizer
manifests) and wrote
`construction-v1/86558218e2b67db0e0249abbee0c6d17650dea43467ed14c59789bc60c7bacb0/markers/finalize/addresses.json`
last. The slice is immutable and non-promoting.

The reducers hydrated 314,240,107,255 bytes (292.66 GiB) from staging across
117 jobs; the largest job hydrated 6,519,238,407 bytes and peak staged-cache
residency was 4,462,286,106 bytes. This closes the Address planet R2 fleet
throughput and bounded-residency gates for this release.

PR #182 removed full Address markers from every hosted consumer. The plan phase
streams each full marker once and emits a query-only SQLite reduce projection
plus an exact finalize identity projection. Reducers query only the row groups
for their two owned partitions and re-prove the fetched packs before trusting
the projected envelope.

The preserved 7,799,189,884-byte Europe marker set projected in 128.56 seconds
at 1,949,052,928 bytes peak RSS to:

- a 667,648-byte reduce projection;
- a 586,797-byte finalize projection; and
- a 99,749-byte plan / 1,363,843-byte core plan payload.

The plan remained byte-identical (`f5d875...333e`) and described 151,371,029
records, 204 partitions, 102 two-partition jobs, 160 packs, 2,417 row groups,
2,520 country envelopes, and 2,466 per-record objects.

All 102 local reducer jobs then completed successfully with four workers and no
retries. Per hosted job, the observed maxima were 2,263,089,152 bytes RSS,
3,309,679,666 bytes sampled scratch+store, and 34.75 seconds wall time. All 204
partition bindings verified and every hydrated pack was released.

Finalize completed in 265.13 seconds at 75,907,072 bytes peak RSS. It reconciled
2,672 exact-set members (204 serving, 2,466 per-record, 2 manifests), wrote the
marker last, and produced 2,673 local files / 47,110,551,015 bytes including
that marker. It hydrated and released all 5,340 staged reads /
94,218,506,354 bytes. The former marker OOM and hidden Address publication-size
gate are closed by execution.

### Publication

PR #178 replaced the serial `aws s3api` mirror with direct bounded R2
publication through one persistent botocore client. Publication concurrency is
derived from the contract's enforced per-object cap and the 25.6 GB runner
floor: Places admits 5 workers at its 5 GB single-PUT ceiling; Addresses admits
11 at its 2 GiB serving-object cap. Every member, including finalizer-created
manifests, is checked against the effective cap before any upload.

The live R2 half of the contract is now execution-proven. PR #181 added a
manual, main-only, one-object probe, and Actions run `30203859256` passed against
`geocoder-shards` at main SHA `82b4731`:

- the non-empty object was created through the production persistent-client
  selector with `IfNoneMatch: "*"`;
- R2's single-part ETag equalled the content MD5 and the recorded SHA-256
  metadata equalled the admitted identity;
- a fresh identical retry received the create-only conflict and was accepted
  only after byte-exact read-back;
- same-length different bytes under the same key were rejected; and
- the unconditional cleanup deleted the exact key and proved it absent.

Remote create-only/checksum semantics are therefore closed. The two local
Europe finalizers also proved bounded streaming and exact-set reconciliation at
21.04 GB Places and 47.11 GB Addresses. R2 fleet throughput remains a planet-run
measurement for Places. Address measured it successfully in run `30215529919`:
292.66 GiB of reducer hydration plus marker-last publication and whole-slice
verification of 10,931 objects.

## Promotion result, 2026-07-31

**The milestone is met.** All three rungs executed; `v2/catalog.json` now points
at build `2026-07-31.0`. See "Live serving" above for the endpoint evidence.

| rung | run | result |
|---|---|---|
| `promote-slice` | `30647191487` | 42,058 objects / 188.4 GiB bound; slice manifest published 18:52:39Z |
| `publish-release` | `30657455505` | `v2/releases/2026-07-31.0/release.json`, sha256 `b1677f4c...d827e` |
| `promote-catalog` | `30657619881` | CAS succeeded against `5c9a9e7e...88184`; smoke false-red |

Measured promotion evidence:

- Places `already_present=20698 copied=0 prepositioned=16528 claim=verified`,
  verified 37,228 objects / 56,754,975,464 bytes;
- Addresses `already_present=531 copied=50 prepositioned=4247 claim=verified`,
  verified 4,830 objects / 145,534,552,085 bytes.

The release document is deterministic: the dry-run and the execute assemble
produced the same 2,947 bytes and the same sha256. Composition is Places
`forward`+`reverse`, Addresses `structured_forward`+`reverse`, legacy core
`2026-07-18.0` referenced in place for `feature_lookup`/`forward`/`reverse`.

**Three failures were paid on the way, all now understood:**

1. `30629402228` (promote-slice) died after 4h17m -- 42 minutes inside its
   timeout -- on one transient R2 `InternalError` during the Addresses copy,
   discarding a fully verified Places family. The copy client cannot retry even
   a definite 5xx. Addresses had already landed 531 of 581 objects; the rerun
   copied the remaining 50. See item 9 of
   `2026-07-31-promotion-copy-and-efficiency.md`.
2. `30656950804` (publish-release) failed assembling: `addresses has both
   in-source and external reverse entrypoints`. PR #202 binds a prepositioned
   reverse set into the family manifest; PR #205 added the external
   `--reverse-publication` path plus a mutual-exclusion check, but the workflow
   attached the external record unconditionally. Any promotion that ran
   `promote-slice` with `--reverse-catalog` -- the normal path -- would have
   failed. Fixed in `17e1a9d`: read the promoted family manifest, since the
   dispatch inputs cannot distinguish the two cases.
3. `30657619881`'s `/v2/forward` smoke is the recorded false-red. Nothing
   auto-recovered; the CAS had already succeeded.

The GitHub artifact retention deadline that governed this promotion
(2026-08-02T20:38Z) is now spent and no longer applies.

Wall-clock optimization of the forward build is an adopted secondary track,
run within the two-active-PR budget and never ahead of reverse R1. The adopted
direction is staged Track A-to-B per
`2026-07-28-planet-build-wall-clock-review.md` and its post-publication
addendum: execute its Waves 1-3 (shared binaries, early marker discovery,
eight-way probe, Address reducer cap probe, selective Places planning, head
radix plus bounded workers, single-write final publication), then the
Track B-specific pieces; Track C is not planned. Single-write publication is on
the Places critical path because measured finalize (57 minutes) is about 3x its
budget. The post-change cold control should ride the next real Overture release
ingest, not a dedicated measurement run.

### Reverse self-recall against the live build, 2026-07-31

Ran `benchmark_v2_reverse.py` in its default `exact_id_self_recall` mode against
`https://geocoder.bradr.dev` on build `2026-07-31.0`, 3 warm repeats.
Cases: `benchmarks/v2-reverse-self-recall-cases-v1.json` (6 Places, 4
Addresses), derived from the forward pilot set — each case probes a feature's
own published coordinates and expects its own GERS ID back. Results:
`benchmarks/2026-07-31-reverse-self-recall-live.json`.

| family | recall@1 | recall@5 | MRR | errors | worker p50 | worker p95 |
|---|---:|---:|---:|---:|---:|---:|
| Addresses | **1.000** | **1.000** | 1.000 | 0 | 15.5 ms | 28 ms |
| Places | 0.667 | **1.000** | 0.806 | 0 | 13.0 ms | 18 ms |

**recall@5 = 1.000 on both families with zero errors is the load-bearing
result:** every probed feature is present and retrievable from the published
index at its own coordinates. That is the index-integrity claim the promotion
needed.

Places recall@1 is depressed by **duplicate POI records in the source data**,
not by an index or ranking defect. Both misses were verified by hand and the
nearest-first ordering is correct in each:

- `stade-louis-ii` — three representations of the same stadium within 50 m
  (`Stade Louis II` at 0.01 m, a styled-unicode duplicate at 24.74 m, and
  `Stade Louis II Stadium, Monaco` at 49.15 m). The expected ID is the third.
- `aldi-nord-nice` — `ALDI Nord` at 9.79 m and `Aldi` at 10.35 m; the expected
  ID is the second.

Reverse latency is excellent and stands in sharp contrast to structured Address
forward (open gate 3, p50 ~1,300-1,600 ms): reverse worker p50 is 13-15.5 ms.

**This is not a quality claim.** Self-recall draws its expectations from the
same data it queries, and coverage is Monaco/Nice Places plus Seattle Addresses
only. The curated global gold set (~20+20) is still unbuilt, and the
provider-neutral comparison against Nominatim/Photon is a separate claim that
has not been run on this build.

### Provider-neutral reverse comparison, 2026-07-31

First Overture reverse score against the public services. In the 2026-07-30
pilot every Overture reverse case returned `capability_unavailable`; this run
scores. 5 Monaco Places + 5 Seattle Addresses, semantic scoring, 2 warm
repeats, zero HTTP errors on any provider. Evidence:
`benchmarks/2026-07-31-reverse-external-comparison.json`.

| provider | Places q@1 | Places q@5 | Addresses q@1 | Addresses q@5 | client p50 |
|---|---:|---:|---:|---:|---:|
| Overture | **0.40** | **0.60** | **1.00** | **1.00** | 195.4 ms |
| Nominatim | 0.20 | 0.20 | 0.80 | 0.80 | 369.3 ms |
| Photon | 0.20 | 0.20 | **1.00** | **1.00** | 138.1 ms |

Read this narrowly:

- **Addresses is a genuine tie at the top** with Photon, both perfect, with
  Nominatim one case behind.
- **The Places column is not apples-to-apples and the lead is inside the
  noise.** Five cases means one case is 0.20. Nominatim reverse returns a
  single result, so its q@5 is necessarily its q@1; Photon has no portable
  generic POI layer, so its Places row uses the unfiltered reverse response.
  All three providers miss `monaco-cathedral` and
  `princess-grace-japanese-garden`.
- **Latency is client-observed for all three.** Only Overture reports
  `worker_ms` (places p50 43 ms, addresses 228 ms); comparing that against the
  others' client times would flatter it, because theirs include public-internet
  RTT. On the like-for-like client number Overture sits between Photon and
  Nominatim.
- Nominatim and Photon both derive from OpenStreetMap: separate
  implementations, not independent source datasets.

This is a plumbing and case-review pilot, not a quality baseline. The curated
global gold set (~20+20) remains unbuilt and is the prerequisite for any
defensible comparative claim.

## Open blockers and gates

### Open gates, 2026-07-31

**No forward availability blocker.** No reverse implementation blocker.
**Both reverse execute gates are CLOSED** — both planet builds ran green and
their output is promoted and serving (see "Promotion result, 2026-07-31"). The
ARDX0002 probe projections held: Addresses measured 54.36 B/record against the
57.65 the probe measured on the densest cell and a 59.58 ARDX0001 basis.

1. **Promotion smoke fixture — now demonstrated false-red on a real
   promotion.** Run `30657619881` logged `OK divisions` and `FAIL places: no
   features for Eiffel Tower` on 10 of 10 attempts while the promoted build was
   live and correct. Replace the context-free `Eiffel Tower` global-head
   assertion with one reliable global-head POI plus one located routed-Places
   assertion. Until then every promotion ends red.
2. **Forward Places quality — NOW THE AGREED MILESTONE**, root-caused in
   `2026-07-31-search-quality-and-street-layer.md` and queued as Stages 0/1 in
   "Current milestone" above. The mechanism is confirmed in source: head entries
   keep only the top 10 per token by `(confidence_rank DESC, feature_id ASC)`
   where `confidence_rank` is a saturating u8, so an entry is effectively the
   ten UUID-smallest confidence-1.0 records; two-token queries intersect two
   such lists and get the empty set; three-token queries never touch the head
   (`v2.rs:1946-1948`); and the Places projection carries no fame signal at all
   (`project_places_construction_v1.py:151-180`). `ranking-research.md` P0-P5
   landed for divisions only — that asymmetry is the bug. Locality-token
   overconstraint is FIXED (PR #214); address locality aliases bridged
   (PR #213).
3. **Structured Address serving latency.** The 211-case run measured p50
   1,595.4 ms and the 2026-07-30 pilot 1,303.8 ms, against 24-140 ms for the
   public comparators. Next serving-latency measurement.
4. **Single-write publication / storage cleanup**, unchanged — see
   "Deferred, not active".

The former Address marker fan-in, Address publication aggregate, watchdog
diagnosis, and missing reducer-cap gates closed in PR #182 plus the successful
Europe execution. Do not reopen them without contradictory measured evidence.

## What is already established

- All five phases run on real Monaco Places and Seattle Addresses slices in CI.
- Map output moves through run-scoped, content-addressed R2 staging rather than
  GitHub artifact fan-out.
- Places reduce owns bucket ranges; address reduce releases hydrated packs at
  last use.
- Places term rows are combined before shuffle, removing about 46% at planet
  scale.
- Places head is routed through 4,096 shards with a published routing manifest.
- Planet Places head completes under request `88b7f...32614`: 62,573,648
  candidates, 30,841,082 records, 13,971,501 index entries, all 4,096 shards,
  and 1.50 GB peak staged-cache residency.
- The complete 43.9%-of-planet Europe Places head passes under merged #176:
  4,096 populated shards, 8.18 GB peak RSS, 5.40 GB peak sampled disk.
- Europe Places completes finalize under merged `94eae08`: 20,567 exact-set
  members, 21.04 GB including marker, reconciles true, marker written last.
- Europe Addresses completes projected plan plus all 204 reduce partitions and
  finalize under merged `94eae08`: 2,672 exact-set members, 47.11 GB including
  marker, reconciles true, marker written last.
- Planet Addresses completes under request `88b7f...32614`: 127 reused maps,
  117 reducers, 10,931 verified exact-set members, reconciles true, marker
  written last.
- Planet Places completes under request `88b7f...32614`: 89 maps, 128 reducers,
  4,096 populated head shards, 40,931 verified exact-set members /
  51,814,660,317 bytes, reconciles true, marker written last.
- Both families emit and durably publish per-record artifacts needed by a later
  spatial reverse index.
- Finalize verifies an exact publication set and has a projected remote
  operation budget.
- Finalize publishes that exact set directly through a bounded persistent R2
  client. Live R2 create-only, ETag/content, identical-resume, conflicting-byte,
  and cleanup semantics pass.
- The address forward partition key and serving layout have not changed.

## Frozen decisions

Do not relitigate these while closing the current blockers:

- The address map shuffle will not be ported. Address output is already
  hash-clustered; the transport and marker fan-in are reduce-side concerns.
- `address_key_hash`, `route_hash`, `hash_bucket`, `MAXIMUM_HASH_BITS`, and the
  address forward partition key remain frozen.
- Places uses the 256x256 equirectangular cell scheme and the high bits of the
  multiplicative shuffle hash.
- Places head remains at `DEFAULT_HEAD_SHARD_BITS = 12`.
- The per-place positions and per-address records artifacts are required,
  durable outputs. A reverse index must consume them rather than forcing a new
  planet map.
- Reverse geocoding is a separate spatial serving index. It must not distort the
  forward partition keys.
- The `PLRX0001` shard container version is independent of the `ARDX` dictionary
  version and was deliberately NOT bumped for ARDX0002. Places shards carry no
  dictionary, so bumping the container would have invalidated Places reverse
  output for an Addresses-only format change. Keep dictionary-format changes
  inside the `ARDX` block.
- `/v2/features/:gers_id` has been removed. `/v2/ids/:id` provides
  release-pinned ID locator metadata but is not a dependency for reverse
  rendering.

## Reverse geocoding fast follow

The map-time prerequisites for reverse are already present:

- Places emits one rich, spatially keyed record per admitted source row.
- Addresses emits an analogous spatially keyed records artifact without
  changing its forward packs.
- Both are content-addressed, published by finalize, and retain source-locator
  identity.

This means forward planet readiness remains the long pole. Reverse can be built
as a second reduce over the published per-record artifacts without rereading
Overture or rerunning map.

The accepted requirements are:

1. One spatial reverse design serves both POI and addresses.
2. Route by the existing level-8 Places cell scheme. Address E7-to-cell
   derivation must pass cross-language parity against the authoritative Places
   route.
3. Emit one reverse shard per populated cell. Dense cells use fine-quadkey
   leaves, with row-major payload order and latitude-aware depth bounds.
4. Queries are bounded-radius k-nearest, with explicit read/byte budgets and
   honest `budget_exhausted` / effective-radius reporting.
5. Use a binary, sharded reverse catalog rather than a large JSON fan-in.
6. Records are self-sufficient for rendering; do not depend on
   `/v2/ids/:id`.
7. The reverse finalizer proves total emitted records equal admitted per-record
   inputs before advertising the operation.
8. The v2 catalog advertises reverse per family only when its verified hashed
   artifacts exist. Forward-only publication remains valid.

Keep reverse work off the critical path by sequencing it as:

- **R0, requirements only** — DONE.
- **R1:** shared encoder, verifier, cell parity gate, small real-data harness —
  DONE 2026-07-28 (PRs #187, #190, #191, #192).
- **R2:** bucket-range reverse reducer and binary catalog — DONE.
- **R3:** Worker range reader, bounded query planner, API capability wiring —
  DONE.
- **R4:** exact-set publication integration and release rehearsal — DONE
  (PRs #202, #204-#210, #212).

All implementation rungs are merged. What remains is execution and promotion,
tracked under "Fastest path".

### Address shard dictionary: ARDX0002

The Address reverse shard dictionary format was bumped on 2026-07-31 (PR #218).
ARDX0001 fixed every dictionary code at u16, which the real planet densest cell
overflows: `street` carries 96,738 distinct values and `postcode` 95,865
against a 65,536 ceiling, and `number` sits at 62,582 with under 3,000 codes of
headroom. Probe run `30593777237` measured it and fail-closed.

ARDX0002 gives each of the seven fields a one-byte code width chosen from its
own cardinality (1 / 2 / 4). Readers in all four independent implementations —
encoder, verifier, Worker, Python oracle — reject any width that is not the
canonical function of the count, so one logical dictionary has exactly one byte
encoding and the dual-lane additive digest stays unambiguous. A uniform u32
would have cost roughly +13 B/record against a 59.58 B/record baseline and
penalized every cell to fix one.

Places shards are unaffected: they carry no dictionary, and the `PLRX0001`
container version is deliberately unchanged.

### Benchmarks: two different claims

Do not conflate these.

- **Index self-recall / serving correctness** (`benchmark_v2_reverse.py`
  against the exact published artifacts, queried at each record's own
  coordinates) proves round-trip indexing and serving. The 2026-07-29 run was
  240/240 valid, recall@1 = recall@5 = 1.00 for both families, warm p50 8.5 ms
  Places / 32.8 ms Addresses. **This is not external accuracy.**
- **External quality** needs an independent, geography- and density-stratified
  gold set with external geocoders used only as comparators, scoring semantic
  agreement and spatial error rather than provider IDs.

The 2026-07-30 open-geocoder pilot (`benchmarks/2026-07-30-v2-open-geocoder-pilot-report.md`)
is plumbing validation, not a baseline: 10 forward and 10 reverse cases, all
Overture-selected, and Nominatim and Photon are separate implementations but
both derive from OpenStreetMap, so they are not independent source datasets.
The curated global set (~20 Places + 20 Addresses, gold from open primary or
government sources) has not been built.

The full reviewed design, including geometry and API details, is
`docs/plans/2026-07-25-reverse-v2-design.md`.

## Verification ladder for this project

1. Focused unit/contract tests for the changed invariant.
2. Monaco Places or Seattle Addresses slice for end-to-end correctness.
3. Preserved Europe run for RAM, disk, fan-out, object-count, and wall-time
   behavior.
4. Live one-object R2 probe for remote SDK semantics.
5. Non-promoting planet run with operator authorization.

Do not substitute more rung-1 or rung-2 checks for a rung-3 or rung-4 risk.

## Deferred, not active

These remain useful but do not block the next measured milestone:

- **promotion should copy zero bytes, and five other efficiencies measured off
  the 2026-07-31 promotion** — see
  `docs/plans/2026-07-31-promotion-copy-and-efficiency.md`. Headline: forward
  promotion server-side-copies 21,279 objects / 158.68 GiB from the construction
  namespace into the release slice namespace and leaves both copies in place,
  while reverse and the v2 catalog's division/ID references already move zero
  bytes. Also queued there: ~84,000 sequential HEAD requests across three loops
  (one of them measured at 17-36 minutes), unsized `COPY_WORKERS = 4`, serial
  per-family promotion, and moving reduction records out of 7-day GitHub
  artifacts into R2;
- **PR #218 recorded P2s:** the probe still names two summary keys
  `projected_ardx0001_dictionary_bytes` / `ardx0001_dictionary_exceeds_serving_cap`;
  `reverse-address-probe.yml` does not surface the new `dictionary_code_widths`
  / `wide_code_fields` in its job summary, so widths must be read from the
  evidence artifact; the 59.5804 B/record Seattle projection basis is an
  ARDX0001 measurement and now over-projects (conservative direction);
  `reverse_shard_v1.py` raises `IndexError` and short-reads on truncation where
  the old `struct.unpack_from` failed fast (still fails closed via the
  trailing-byte check);
- **width-4 end-to-end encoder coverage.** The `test_reverse_shard_v1.py`
  address fixture exercises widths 1 and 2 through the real encoder, verifier,
  and oracle (`street` and `number` each carry 2,100 distinct values). Width 4
  is covered only by the Worker's decode unit test and the boundary test;
  closing it end to end needs a 65,537-value fixture field;
- range-owning address reducer and row-group range reads, unless the resumed
  Europe run proves them necessary;
- a narrower staging-only R2 credential;
- cleanup of dead evidence-spec hard-cap declarations;
- request-count and storage-cost optimizations;
- skip checkout/dependency cleanup/Rust build before a durable map-marker reuse
  check (the resumed Address map command took five seconds, while 127 complete
  jobs consumed 296 runner-minutes);
- build the pinned Rust binaries once per workflow and distribute an
  architecture-specific artifact rather than rebuilding them in every map and
  reduce job;
- reduce Address job fan-out after a bounded probe of the existing
  `max_reduce_jobs` knob: 117 jobs hydrated 292.66 GiB and had a median total
  duration of only 255 seconds, leaving substantial per-job timeout headroom;
- make the runner-minute ledger include setup/build, plan, head, and finalize.
  The successful resume's GitHub job durations sum to about 917 runner-minutes,
  while its ledger appended 613; this did not threaten the 40,000-minute
  authorization but is not complete accounting;
- profile whether finalize's two staged hydrations per published input can share
  a verified digest without weakening whole-slice read-back. Address hydrated
  21,858 staged objects for 10,929 staged exact-set inputs and still completed
  within its projection;
- general review findings that do not corrupt output or prevent the next probe;
- reverse implementation beyond R0 while the forward long pole is open.

## Evidence and history

- `docs/plans/2026-07-31-promotion-copy-and-efficiency.md` — measured cost of
  the forward promotion copy, the path to a zero-copy promotion, and the rest of
  the efficiency queue for the next planet run.
- `docs/plans/2026-07-28-planet-build-wall-clock-review.md` — wall-clock
  review of all planet attempts, Tracks A/B/C, and the adopted staged
  A-to-B optimization sequence (see its addendum).
- `docs/plans/2026-07-28-planet-places-publication-result.md` — successful
  planet Places publication evidence and measured finalize phase timings.
- `docs/plans/2026-07-26-planet-probe-findings.md` — Europe runs, corrected
  projections, preserved work trees, and resume commands.
- `docs/plans/2026-07-25-reverse-v2-design.md` — reviewed POI/address reverse
  design.
- `docs/plans/2026-07-24-construction-v1-follow-ups.md` — append-only historical
  findings; not the active queue.
- `docs/plans/2026-07-25-pending-work-archive.md` — historical project handoff
  and former backlog.
- `docs/plans/2026-07-23-construction-v1-one-way-doors.md` — irreversible
  storage and contract choices.
