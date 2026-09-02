# construction-v1: current state

## Production direction: v2 paused — 2026-09-02

This decision supersedes every later "current milestone" or "next action"
statement in this document. The `/search`, `/reverse`, and `/id/:gers_id`
endpoints are the supported production API; do not describe them collectively
as legacy. Construction-v1 is an implementation-generation name, not the name
of that production API.

Further Places/POI and address v2 processing, preview, publication, and serving
are paused. Retain the implementation, tests, frozen contracts, and historical
evidence so the work can be resumed if a concrete downstream owner and
migration plan emerge. Production is configured fail-closed: `/v2` and all
descendant paths return 404 before the Worker reads any v2 catalog or family
object, and the root discovery document advertises only the supported
production endpoints.

The v2 production, build, scale, and evidence workflows were disabled in GitHub
on 2026-09-02. This includes `construction-v1.yml`, `reverse-v2.yml`,
`promote-v2-release.yml`, `preview-v2-candidate.yml`,
`build-places-region.yml`, `rehearse-address-r2-map-reduce.yml`,
`release-slice-families.yml`, `usa-scale-signal.yml`,
`hosted-rowgroup-data-spike.yml`, and `reverse-address-probe.yml`. They were
manual-only at the time of the decision; no recurring v2 build schedule
existed. Small credential-free code-preservation checks remain available.
The monthly v1 production rebuild and code-only cold-start smoke remain enabled.

The deletion-capable `retire-build-scratch.yml` and `r2-cleanup.yml` workflows
are also disabled so they cannot race the reviewed one-time cleanup. Operator
clarification on 2026-09-02 corrected an earlier policy transcription: the
seven-day delay applies to future post-schedule v1 generation cleanup, not to
the already-approved initial paused-v2 and old-v1 retirement. The one-time
workflow was therefore allowed to run immediately after a fresh dry run bound
the exact inventory. Delete only the reviewed v2-specific release, slice, and
duplicate construction data objects. Preserve the implementation and
historical evidence required to audit or resume the work, including construction
finalize markers and family/slice manifests plus the global staging inventories,
manifests, reports, and completion records.

The separate move to one v1 working copy was approved on 2026-09-02. The
retained production generation is `2026-08-25.0`; it has already had the
roughly seven-day predecessor overlap required by the prior retention policy.
The one-time `decommission-paused-data.yml` workflow performed the initial
paused-v2 and old-v1 cleanup. It was designed to fail closed on a changed
catalog, a newer or unclassified root generation, an unverified v2 release
chain, an enabled or active producer, enabled Worker logging, or an exact
recursive object inventory that differed from a fresh dry-run fingerprint.
Before its first delete it durably preserved the private deletion inventory,
the v1 catalog, and all SHA-verified v2 catalog/release documents under
`backups/`. Construction and global staging namespaces are classified
object-by-object: only recognized large `objects/`, `positions/`, or `records/`
data subtrees are deletable, while
the compact run evidence remains in place and is checked by exact key, size,
and ETag after the data deletion. Before any destructive step, the workflow
writes an
immutable pending transaction containing the reviewed plan, full private
inventory, and source metadata; its final content-identity manifest is the
resume commit marker. It then compare-and-swap publishes the
single-current v1 catalog, passes the production smoke, waits out both catalog
caches, and only then removes one logical data copy at a time. Each prefix is
verified absent and followed by the complete supported-v1/v2-404 production
smoke plus a pause. If recursive deletion is interrupted, a retry named by the
same reviewed plan SHA loads the durable transaction and accepts only an exact
key/size/ETag subset of the original target inventory; no newly appeared or
changed object can enter the resumed delete.

That one-time cleanup completed successfully on 2026-09-02 in
[`33669497022`](https://github.com/brad-richardson/overture-geocoder/actions/runs/33669497022)
after the exact-inventory dry run
[`33669039316`](https://github.com/brad-richardson/overture-geocoder/actions/runs/33669039316).
It retired eight construction data subtrees, six v2 slice copies, six old v1
root generations, and the v2 metadata namespace; no global staging data subtree
was classified for deletion. Eleven evidence objects were durably backed up,
and the retained construction/staging evidence was reverified before dependent
copies were removed. Every one of the 21 logical deletion targets was verified
against the reviewed inventory, deleted and verified absent, followed by a
passing production smoke and 65-second pause. The final catalog contains only
`2026-08-25.0`; the workflow's final smoke and an independent post-run smoke
both passed. Cloudflare recorded 334,147 successful `DeleteObject` operations
and no non-successful delete operations during the run. Workers Logs, traces,
Logpush, and tail consumers remained disabled. Cloudflare's immediate storage
time series lagged the operation, so use the exact-prefix postconditions and
the durable transaction evidence—not a same-minute aggregate storage sample—as
the completion record.

Now that the one-time cleanup removed `v2/`, `retain-one-v1-copy.yml` enforces
the same policy for future monthly v1 rebuilds. Its daily pass is serialized
with the rebuild catalog writer, refuses to act whenever a v2 catalog exists,
and does not prune until `catalog.json` has been unchanged for seven full days. It
also binds and backs up an exact private recursive inventory and publishes an
immutable per-current pending pointer. It compare-and-swap prunes the catalog,
smokes, and waits out both caches before deleting predecessor copies serially
with a production smoke and pause after each. The daily pass automatically
resumes an unfinished pointer, permitting only exact remaining subsets of its
reviewed inventory, and writes an immutable completion marker at the end. The
current v1 prefix, unrelated top-level namespaces, retained
construction/staging evidence, source code, tests, contracts, and durable
evidence are not deletion targets.

Workers Logs, traces, Logpush, and tail consumers were confirmed disabled on
2026-09-02. No additional logging switch is required; the decommission workflow
rechecks that state before planning or deleting.

No additional construction-v1 execution is an active milestone unless this
section is explicitly revised.

## Historical construction snapshot (frozen 2026-08-07)

Everything below preserves the prior execution record. Present-tense words such
as "current", "next", "live", and "pending" are historical as of that date and
do not override the 2026-09-02 production direction above.

Last updated 2026-08-07 after the Worker apostrophe fix went live with a
zero-flip paired gate and a measured zero yield (see below), the PENDING
phrase-admission sizing join was computed and §3.2 was closed on it, the apostrophe-folding locus was settled
(`2026-08-07-apostrophe-folding-locus.md`), and the theme=base import was
scoped and reframed as a coverage lever (see "Phrase admission is sized, and §3.2 is
closed, 2026-08-07"), on top of the 2026-08-06 state: the Worker-only proximity
wave deployed, measured, corrected for one locality-inference regression, and
accepted. Build
`2026-08-03.0` still serves the bounded phrase-admission data; Worker commit
`00bc46c` now serves the proximity result recorded below. It also incorporates
the independent correctness audit, fresh v4 formal evidence, sidecar Phase 0's
cross-release identity measurement, completion of the 200-case everyday-POI
tripwire and its current-OSM presence control, and the new proximity/variant
strata.
Read, in order: "Planet Places rebuild and promotion, 2026-08-02", "Head
saturated-posting correction, 2026-08-02", "RC3 and remaining admission split,
2026-08-02", "First RC3 live result and locality interaction, 2026-08-02",
"RC3 accepted result, 2026-08-02", and "Big Ben and Machu Picchu contract
audit, 2026-08-02".

**RC2 and RC3 are closed; do not reopen the head intersection or start another
undirected rebuild.** Worker commits `3d3b33c`, `8f9a90f`, and `e655615` are
live. Across the same 35-case gold set, name-only head recall@10 moved from 2/10
before RC2 to 6/10 after RC3, with no serving rank@1 regression. The subsequent
contract audit removed a false-positive Big Ben rank and accepted official
Machu Picchu citadel names, producing an audited name-only score of 7/10.
Statue of Liberty is recovered at rank 4; Machu Picchu is recovered at rank 10
under an official alias. The bounded producer-admission design is now
implemented and real-slice validated for **Empire State Building and Big Ben**.
A direct source-stage audit corrected Brandenburg Gate: the nearby June records
expose German/Spanish names, not the English query surface, and even the German
routed control misses the canonical record. It is an alias/tokenization plus
routed-cap problem, not the same global-head eviction. Fresh v4 formal evidence
is now green, including the planet-wide phrase-posting falsification probe. The
non-promoting Places-only planet build and scoped head-only recovery completed.
**The final preview is accepted and is live in production. Against the 55-case baseline, rank@1 moved
26 -> 30 and rank@10 34 -> 39 with four/zero and five/zero paired
gains/losses. Against the 200-case everyday-POI baseline, rank@1 moved 62 -> 65
and rank@10 65 -> 66 with three/zero and one/zero paired gains/losses. Both
direct phrase gates passed, the run had zero request errors, and cleanup left
production untouched. The operation-preserving release publication and catalog
promotion then passed, including both retained reverse families.** Keep Brandenburg Gate
and `Machu Picchu Cusco` in their separate follow-up backlogs. Do not widen
phrase admission again in this generation — but the reason has changed and the
old one was wrong. It is **not** the 31.7 MB head-byte reserve, which gates a
rehearsal fixture rather than production; it is that softening admission is now
measured at **+102.5% of the planet head for 19 of 145 current misses**. See
"Phrase admission is sized, and §3.2 is closed, 2026-08-07".

This is the operational snapshot for construction-v1. It intentionally contains
only the current milestone, measured blockers, next actions, and frozen
decisions. Dated documents in this directory preserve the evidence and history;
they do not override this file unless their findings have been incorporated
here.

## The release axis is now an envelope, not a re-attestation — 2026-08-07

**A build may run on an Overture release the frozen evidence was not generated
against, provided the schema is identical and the work is no larger.** This is
what unpinned the build from `2026-06-17.0`, and it is a narrowing of one axis,
not a loosening of the gate.

Moving the release used to mean regenerating twelve projections and censuses,
seven task runs, and the functional rehearsal — on free public runners — to
re-derive evidence about a producer that had not changed. The coupling that
forced it was a single pin doing double duty: `inventory_sha256` bound both the
live inventory and the readiness document. Splitting it into
`inventory_sha256` (live, moves with the release) and
`attested_inventory_sha256` (what readiness names, frozen) is the whole change.

**What still binds exactly**: the spec, readiness and scale-evidence hashes;
`readiness.ready`; the inventory's bytes against its committed file; and the
**schema fingerprint** against the live inventory. A schema change voids the
evidence outright — no envelope rescues it.

**What the envelope allows**, one-directional in every dimension:

| dimension | bound against | why |
|---|---|---|
| `records`, `selected_uncompressed_bytes`, `map_tasks` | the attested release | totals scale the run's cost and the matrix width |
| per-task rows / row groups / selected bytes | **the spec's own declared cap** | the evidence asserts the producer stays inside these caps; the largest task that happened to occur is an observation, not a bound |

Per-task bounds went to the spec's caps rather than the attested maxima
deliberately. Two of twelve July dimensions exceed the attested *observation* —
addresses by 19 rows, places by 1.33% — so an observation-based envelope would
refuse the build over noise, and **false refusals are how gates get switched
off**. The spec cap is also tighter than the address planner's own gate
(350 MB against 400 MB), which is what stops a within-totals release from
producing a task the evidence never covered.

`2026-07-22.0` clears every dimension: both schema fingerprints byte-identical,
places −1.88% records / 89 → 88 tasks, addresses −0.18% records / 127 → 126
tasks, and every per-task maximum inside its declared cap.

**Honest caveat**: places `totals.bytes` *grew* 3.0% while
`selected_uncompressed_bytes` shrank 2.69%. The envelope reads the selected
columns, which is what the producer actually touches — but "the data got
smaller" is true only of the selected projection, not of the release.

**Not covered, and no proxy exists**: per-task RSS, duplicate multiplicity, and
`(country, maximum_bucket)` skew are absent from the inventory entirely. The
July and June task plans are **100% disjoint** — zero shared task digests, zero
shared source ETags — so June's per-task census attaches to nothing in July. The
12 GiB runtime `max_rss_bytes` means a skewed task aborts rather than corrupts,
which is why this is a recorded limit rather than a blocker.

**Two defects this surfaced**, both of which would have wasted or corrupted a
planet run:

1. Addresses sourced their map matrix from the **readiness document**, which
   pins the attested release's row-group ranges and ETags permanently. Task 0
   names ETag `1ca05e7a…`; July's object is `d4cbc779…`. Now read from the live
   inventory — a no-op on the attested release, since both carried a
   byte-identical task list, and the difference between correct and corrupt
   after it moves.
2. `construction-v1.yml` **hardcoded** the inventory path at four sites while
   threading the evidence-spec path from the contract. Addresses would have
   failed closed; places would have **succeeded**, building June data under a
   request, contract, ledger and slice claim all naming July.
   `project_places_construction_v1.py` had no release check at all — the address
   projector has always had one — so the mismatch was silent rather than loud.
   Both are now threaded from `control/contract.json` and contract-tested.

**Regenerating readiness still works**, and that is why the attested inventory
stays committed at the path its sha256-pinned spec names. The live inventory
gets a release-qualified path instead. Overwriting the attested path leaves a
frozen artifact describing a file that no longer holds what it attests, and
strands the readiness validators, which require `inventory.release ==
spec.release`.

## Current milestone

The previous milestone -- **reverse serving for both families** -- is **MET**
as of 2026-07-31T19:05Z. v2 release `2026-07-31.0` became live and `/v2/reverse`
answers `types=poi` and `types=address` from planet-scale point-family indexes;
it no longer returns `capability_unavailable`. Both reverse builds, the slice
promotion, the release publication, and the catalog CAS are all done. See
"Promotion result, 2026-07-31".

**2026-08-02 UPDATE.** The scoped planet Places rebuild is built, promoted, and
measured; `/v2` is live on `2026-08-02.0`. It moved overall place recall
0.314 -> 0.400 and doubled the routed `name_locality` path, but **Wave C's
head-path prediction is refuted** (0/10 -> 1/10, not 6/11). The subsequent
Worker-only saturated-posting correction is also deployed and measured: exact
self-recall moved 0.400 -> 0.429 at rank 1 and 0.486 -> 0.571 at rank 10,
while the name-only head stratum moved 1/10 -> 2/10 at rank 1 and 2/10 -> 5/10
at rank 10. RC2 is closed. See "Planet Places rebuild and promotion,
2026-08-02" and "Head saturated-posting correction, 2026-08-02". RC3 then
recovered Statue of Liberty without changing rank@1 and moved the name-only
head stratum to 6/10 at rank 10. Its initially exposed locality interaction is
fixed and accepted; see "RC3 accepted result, 2026-08-02". The later contract
audit moves the score to 7/10 by accepting an official Machu Picchu citadel
name; that extra point is a gold correction, not another Worker improvement.

**The next milestone is AGREED (2026-07-31): forward search correctness.**
Operator approved Stages 0 and 1 of
`docs/plans/2026-07-31-search-quality-and-street-layer.md` (merged #220). The
planet rebuild and the street layer both wait behind it.

**2026-08-04 UPDATE.** The bounded v4 phrase-admission increment is live as
build `2026-08-03.0`; its production acceptance and exact identities are in
"v4 production promotion, 2026-08-04". The next correctness measurement is a
failure-mode classification of the 134 everyday-POI cases still missed at
rank 10 by the accepted candidate, using the frozen authority gold and OSM
presence control before choosing another admission or routing mechanism. Do
not widen the phrase lane from the existing result. ~~Sidecar Phase 0 remains a
separate queued gate: 200 independently hand-checked decisions with zero false
provisional accepts~~ **— CLOSED 2026-08-05, see "The P1968 sidecar is a dead
end for Places" below; the audit is stopped at 57/200 and is not resumed.**
Structured Address latency remains the next serving-performance gate.

**2026-08-05 UPDATE.** That classification is done, and so is the question it
opened. The Overture release move `2026-06-17.0 -> 2026-07-22.0` — previously
recorded as "the largest untested quality lever on the board" — is **measured
and refuted**: +1 case of plain-head reachability across 206 exact-tier
benchmark cases, against five regressions, two of which production answers
correctly today. It is not a recall lever and must not gate anything. See "The
release move is not a quality lever, 2026-08-04" below. ~~**The
`prominence_rank = 0` phrase-lane admission change is now the sole remaining
candidate, and it rides on the June corpus** — no release move ahead of it.~~
**— superseded 2026-08-07: that candidate is sized and closed; the surviving
phrase-lane candidate is `e4:` keys. See "Phrase admission is sized, and §3.2 is
closed, 2026-08-07".**

**2026-08-06 UPDATE.** A four-track failure-mode review
(`2026-08-06-places-failure-mode-review.md`) reframed the quality roadmap.
Three findings change priorities: (1) a bounded corpus probe labels 92 of 130
everyday misses `ABSENT`, largely registry-legal-name gold not found in map
data — an instrument problem with the probe caveats measured below;
(2) the proximity lane is broken end to end and measured by zero benchmark
cases — `q=starbucks` at Times Square returns nearest-24 km because
`place_score` has no distance term and the routed cell posting evicts by cap
order, all Worker-fixable; (3) tokenizer variant holes (apostrophes 2.55M
records, ampersands 2.81M, non-decomposable Latin 640k, Thai/Lao/Khmer/
Myanmar 1.63M, ≥4-word names 21.1M) are structural misses no lane recovers,
and the confirmed Monte-Carlo divisions gap (~89k hyphenated localities) also
explains the Novotel/Casino de Monte-Carlo gold misses. Recommended order:
fix the instrument, ship a Worker-only proximity wave, fold variant folding
into the v5 rebuild beside phrase admission (whose PENDING sizing join must
be computed first), scope the theme=base landmark import. ~~The
`prominence_rank = 0` phrase admission remains sanctioned but is a gold-set
lever (~7.5% of everyday misses), not the strategy.~~ **— the sizing join is
now computed and that admission is CLOSED, 2026-08-07; see below.**

**2026-08-06 INSTRUMENT FOLLOW-UP.** The first two missing strata are now
frozen against production build `2026-08-03.0`; see
`benchmarks/2026-08-06-proximity-variant-baseline-v1.json` and its case files.
Proximity and POI-variant gold uses Places `2026-06-17.0`; the seven
hyphenated-division cases use the locally available Divisions `2026-07-22.0`
mirror, and each affected case records that vintage mismatch.
The proximity baseline is **3/40 at rank 1 and 22/40 at rank 10** for a
permissive chain-name match within 2 km; a chain appears somewhere in the top
10 for 40/40. The 3/40 is conservative/flattering as an entity metric because
Sydney credits `Woolworths Riley Street Car Park`, but it still establishes the
broken nearest-chain baseline the Worker wave must beat. The sampled anchor is
a construction aid, not gold: displacement makes it non-nearest in 38/40, so
the case-file contract explicitly forbids stock exact-GERS anchor scoring.

The everyday denominator re-baseline is also frozen in
`benchmarks/2026-08-06-everyday-denominator-rebaseline-v1.json`. Quarantining
92 `ABSENT AND production-miss` cases changes 0.345/0.350 (n=200) to
**0.639/0.648 (n=108), but those figures are upper bounds, not a simple
denominator correction**. The ABSENT probe was wrong for 2/70 production hits;
an illustrative equal-blind-rate scenario applies that observed rate to the
quarantine and gives about **0.622/0.631 (n=111)**.
That scenario is not an empirical false-quarantine estimate: it extrapolates a
rate observed among production hits to misses. Quarantine is not missing at
random: CO falls 20->0 cases, AU 25->7, and KR 20->7, while 94/108 kept cases
are from HK, TW,
SG, MX, or JP. Any headline must carry both the probe-blindness and population
shift caveats.

The initial variant stratum remains diagnostic rather than a headline metric.
Four of five apostrophe controls return no candidates, and the three apparent
control misses for Domino's, Søstrene Grene, and Phương Đông are global-
homonym/distance failures rather than proximity evidence. Two targeted
lower-population hyphen divisions supplement the five population-leading
localities: `Monte Carlo` / `Monte-Carlo` misses the Monaco division under
both spellings while returning a 9,351 km POI; `Opa locka` retrieves its
locality at rank 9 versus rank 1 for `Opa-locka`. This is evidence of a ranking
effect, but the current small mixed stratum is not suitable for a global rate.

**2026-08-06 WORKER PROXIMITY RESULT.** The Worker-only proximity wave is live
from merged `main` commit `00bc46c` (Cloudflare Worker version
`4d82dbd8-f2c7-4138-97a5-36eaa8d64844`); the v2 data build remains
`2026-08-03.0`. Final evidence is
`benchmarks/2026-08-06-proximity-variant-post-worker-00bc46c.json`.
Against the frozen 40-case baseline, a chain-name match within 2 km moved
**3 -> 28 at rank 1 and 22 -> 40 at rank 10**, with zero empty responses.
Median top-1 distance fell **8.771 -> 0.817 km** and the maximum fell
**8,356.476 -> 28.827 km**. The 22-case variant/control diagnostic is unchanged
(12 controls and 6 typed variants at rank 10), as expected for index-time
tokenizer gaps rather than Worker scoring/retrieval fixes.

The first deployment exposed one locality-inference regression before the
larger gate was allowed to proceed: neighbor-cell expansion also ran for an
internally inferred centroid and pushed `Plaza Hotel New York` beyond the
ten-result cap. PR #256 restricted neighbor probes to explicit user proximity;
independent review, full Rust fmt/clippy/tests, wasm32 compilation, a release
Worker build, and post-deploy smoke all passed. The corrected 55-case exact-ID
gold run is unchanged at **31/55 rank 1 and 40/55 rank 10**, and the 200-case
everyday tripwire is unchanged at **69/200 rank 1 and 70/200 rank 10**, both
with zero errors and no threshold regressions. Everyday top-1 type composition
improved 0.465 -> 0.475 and type starvation fell 107 -> 105. Evidence is
`benchmarks/2026-08-06-forward-gold-post-worker-00bc46c.json` and
`benchmarks/2026-08-06-everyday-poi-post-worker-00bc46c.json`.

The justification is measured, not aesthetic. Against the build promoted today:

- `q=Seattle&limit=10` returns ten POIs at relevance 1.0 -- UPS Store, Verizon,
  KeyBank -- and **the city of Seattle does not appear at all**;
  `types=locality` returns it at 0.8408.
- `q=paris` returns Dessirier, Rexel, Midas; **Paris is absent from its own
  query**.
- Before the saturated-posting correction, `q=Eiffel Tower`, `q=Space Needle`,
  and `q=Statue of Liberty` all returned zero. Eiffel Tower is now recovered;
  Statue of Liberty remains behind the separate three-token guard.

### DONE: Stage 0 -- make improvement measurable

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

### DONE: Stage 1 -- Worker-only fixes

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

### Stage 0/1 result, and where the milestone actually stands

All four Stage 1 fixes landed and were measured live on 2026-07-31, and Stage 2
item 5 landed with them. Fix 2 (seam calibration, `2389871`) is the one that
moved metrics: overall t@1 **0.261 -> 0.587**, type starvation **34 -> 15** of
46, `place:seam` t@1 0.000 -> 0.900 with starvation 10 -> 0, and the first
non-zero exact-id self-recall this gold set has produced. Fix 1 is correct and
live but moved nothing, for the reason Part 6b records: query-time reranking
cannot recover what the build-time cap already discarded.

**Historical gate, now closed.** At this point the Worker-side milestone was met
and the `PLHD0002 -> PLHD0003` prominence bump had only been validated on the
Monaco slice, so it was unmeasurable until a planet rebuild. The 2026-08-02
rebuild subsequently shipped PLHD0003 and made `prominence_rank` live. The
2026-08-03 independent audit recovered the live quantized priors from response
scores and proved the signal is not zero; its remaining defect is
alternate-category inflation, now clamped at query time by primary category.

### 2026-08-01: two Part 6d findings corrected by operator challenge

Probe `benchmarks/probes/2026-08-01-confidence-and-duplicates-probe.py`,
evidence `benchmarks/2026-08-01-confidence-and-duplicates.json`, written up as
Part 6h of `2026-07-31-search-quality-and-street-layer.md`. Both change what
the rebuild should carry.

1. **`confidence` is a per-source value, mostly a flat default.** Foursquare
   stamps exactly 0.7700 on 100% of its records (confirmed across six non-US
   regions, n=132,407); PinMeTo and DAC stamp exactly 1.0; Microsoft floors at
   0.85, AllThePlaces at 0.80. Only `meta` is continuous. Part 6d's
   "confidence anti-correlates with fame" was reading a source constant --
   the canonical Tour Eiffel is a Foursquare record. The operational
   conclusion survives (confidence cannot rank POIs) but the reason changes,
   and **`confidence_rank` is still the tie-break in `HEAD_CAP_ORDER` and
   `SERVING_ORDER`**, so tied records are currently ordered by upstream
   dataset. A confidence floor was tested and is **disqualified at every
   value** -- it deletes verified flagship retail (Messika Champs-Elysees
   0.0587, adidas Sao Paulo 0.0145), deletes landmark-class records in every
   region, and is geographically discriminatory (0.20 removes 7.19% of Lagos
   vs 1.75% of Tokyo). The US is a separate regime: heavily conflated,
   `LEN(sources)` up to 7, no flat defaults.

2. **The Eiffel Tower is ~87 records under 53 name-forms**, scattered to
   17.8 km, against a head cap of 10. `q=Eiffel Tower` loses to itself, not to
   hotels. Same shape on Colosseum (33/24), Sagrada Familia (17/7), Tokyo
   Tower (7/4). **A simple dedup heuristic does not reach it**: exact
   normalised-name equality at unlimited radius still leaves 53 forms, and the
   fuzzy variant that reaches 17 is unshippable because no Jaro-Winkler
   threshold exists (Statue of Liberty / Statue of Liberty Deli scores 0.958
   and must not merge; Colosseo / Coliseo Romano scores 0.830 and must).
   Measured and deferred. What does work is exact name + tight landmark
   category + small radius, which is a modest cleanup, not a fix.

**Consequence for Stage 3.** The famous-unique design
(`2026-07-17-famous-unique-head-admission.md`) names quantized confidence as
its fame proxy. That is falsified twice over and the design must be re-specced
onto `prominence_rank` before it is built.

**Two ordering defects found while tracing this, both cheap and both worth
fixing before a rebuild:**

- **`SERVING_ORDER` vs `TOTAL_ORDER` is ungated.** The map-side 256-row
  combiner selects by `SERVING_ORDER` then re-sorts the output by
  `TOTAL_ORDER`, so if the two tails diverged, a *different set of 256 rows*
  would be retained with no row lost, no binding violated and no test failing.
  It is the earliest and most irreversible cap in the system.
- **The ordering has nine textual spellings, not four** (four Python, two in
  the Rust encoder, one in the verifier, two in the Worker), and no test
  asserts they agree. The encoder/verifier assertions cover `TOTAL_ORDER` only.

**Also recorded: the obvious name normaliser destroys CJK.**
`regexp_replace(lower(strip_accents(n)),'[^a-z]','','g')` collapses **67% of
Tokyo records to the empty string** (Paris 0.27%, Seattle 0.11%) -- invisible in
Latin-script testing. Use `[^\p{L}\p{N}]`. Applies to any name normalisation in
the pipeline, not only to dedup.

### 2026-08-01: DuckDB 1.5.5 is prepared but HELD

1.5.5 is the current release and the bump is written (22 files; `1.5.1` also
appears in `construction_v1_control.py:56`, inside the hashed request, so it
moves the request digest and the staging namespace -- but that cost is already
sunk, because `places_source_sha256` is in the same contract and
`places_construction_v1.py` changed three times on 2026-07-31).

**It is held, not landed, because 1.5.5 changes the address forward pack
bytes.** `test_forward_packs_are_byte_identical_to_before_the_artifact` moves
pack 0 from `bd6c2984...` to `f740ad2c...`. Isolated: the same edits on 1.5.1
pass, on 1.5.5 fail. `test_committed_monaco_evidence_is_current` also fails,
against regenerated evidence. Both are pinned attestations, and the standing
rule from `cfb9601` is that rewriting frozen evidence to match falsifies an
attestation of a run that really happened. **Operator decision 2026-08-01: hold
the bump and land it with the planet rebuild**, where the packs are re-measured
and additivity re-proven under 1.5.5 anyway. Baseline for comparison: 14
pre-existing test failures on a clean tree in the local uv environment.

To redo it, the mechanical part is `1.5.1 -> 1.5.5` across
`.github/requirements-hosted-rowgroup.txt`, `construction_v1_control.py`,
`places_construction_v1.py`, `address_construction_v1.py`,
`run_slice_construction_v1.py`, `census_places_construction_v1.py`,
`census_address_construction.py`, `spike_address_construction.py`,
`verify_monaco_evidence.py`, the `smoketest-r2-id` / `build-places-region` /
`smoketest-r2-pipeline` / `release-slice-families` workflows, and the tests that
assert the pin. Leave `rebuild-r2-shards.yml` and `patch-id-stage.yml` alone --
they build the frozen legacy core, so re-versioning them is risk with no
benefit. The three cp311/cp312 wheel hashes are:

    x86_64  b9b6f86ed85d4ef5e0211eaebf75d057bd8bb520bba438a95dd0f4e42234bbfe
    aarch64 2e72f9e1a4f90a5c8483ad4d540e495bf0834ba61c360b52499a573d7ed62a3f
    macos   f0b88535a5d86fdd63dba6ea02ab68c003dfb9e4892b11256ef24c4da208baae

`test_workflow_pins_actions_and_dependency` asserts the aarch64 hash by value
and an exact total hash count, so both move with the pin. Two behavioural
comments are also attributed to 1.5.1 observations that were not re-measured on
1.5.5 (`download_divisions_smoke.py` and `verify_monaco_evidence.py`, both on
the non-gating `rows_scanned` double-count).

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

## 2026-08-02 planet rebuild: scope agreed 2026-08-01

Operator decisions, taken 2026-08-01:

| decision | value | consequence |
|---|---|---|
| families | **Places only** | Addresses need nothing from this rebuild; the promoted Address slice is untouched, and ~114 GiB of republication is avoided |
| DuckDB 1.5.5 | **held again** | It changes address forward pack bytes, so landing it would drag Addresses into the run for zero quality gain. See the "prepared but HELD" section |
| Places reverse | **not rebuilt** | `POSITIONS_COLUMNS` carries no `prominence_rank` and the reverse encoder's `OrderKey` is purely spatial, so the existing 7.82 GiB catalog is referenced in place. Saves 2h15m |
| `head_result_cap` | **STAYS AT 10** (revised after Wave C, same day) | Wave C measured the reorder as delivering the whole step change at cap 10, and 64 as the riskiest part of the run. See "Wave C result" |

**This was the rebuild's decision-time justification.** The then-published
PLHD0002 shards did not carry prominence, so the proposed ordering could not be
measured live. The rebuild completed and shipped PLHD0003 on 2026-08-02;
prominence is now live and was directly confirmed by the 2026-08-03 audit. This
historical rationale must not be reused to justify another rebuild.

### Wave C result, 2026-08-01: GATE PASSED, and it resized the run

Evidence: `benchmarks/2026-08-01-wave-c-cap-simulation.json`. 15 bbox-bounded
metros, 2,552,036 records -> 21,431,034 term rows -> 1,069,840 distinct tokens,
roughly 3-4% of the planet. Fidelity check worth trusting the rest on: the
simulation's head-rows-per-token at cap 10 came out **2.174 against the planet's
actual 2.208** (30,841,082 / 13,971,501), a 1.5% match on a number nobody fitted.

**The rebuild is justified, for a narrower reason than assumed.** The reorder is
invisible on the routed path and decisive on the head path:

- **Routed path: 23 of 25 gold POI cases admit IDENTICALLY** old vs new, at every
  cap. `merge_routed_candidates` has a saturated-posting fallback that routes
  around the cap entirely. If this were the only consumer, the rebuild would not
  be worth paying for.
- **Head path (context-free, strict AND, no fallback): 0 of 11 answerable today,
  6 of 11 under the new ordering AT THE EXISTING CAP OF 10.** That is the step
  change, and it does not need the cap raise.

**`head_result_cap` therefore stays at 10** (operator decision, same day,
revising the earlier 10 -> 64). Wave C measured 64 as the riskiest part of the
change:

- the real multiplier is **1.72x**, not 6.4x -- 69% of tokens have exactly one
  candidate and only 6.2% exceed 10;
- it buys **+2 of 11** gold cases, both in the 11-20 band, which a cap of ~24
  would buy at 1.2-1.4x;
- it makes the key MORE arbitrary: UUID order decides **34%** of cap decisions at
  10, **57% at 64**, 71% at 256;
- **every gate this document named passes with huge margin** -- per-shard index
  entries 1.4% of cap, per-shard bytes 2.16 MB against 1 GiB, candidate rows 54%
  of cap -- but **two gates it did not name are where 64 breaks**: head wall clock
  projects **330-392 min against a 330-min budget and a 360-min job ceiling**, and
  the DuckDB spill headroom is only 2.02x against 1.72x growth with the cap-10
  peak **recorded nowhere**. A blown head phase costs the whole run.

Staying at 10 also means the run measures the head timing and spill peak for
free, so the cap becomes an informed decision next time instead of a projection.

**Consequence for Track A: the evidence spec no longer needs to change for the
cap.** The re-attestation pass shrinks to `categories.alternate` alone.

**Two things Wave C found that are NOT fixed by this rebuild**, recorded so they
are not rediscovered: `brandenburg-gate` (31 -> 243) and `louvre-museum`
(26 -> 455) **regress** under the new ordering -- both match on a generic second
token (`gate`, `museum`) via a common name and land in a large
identity+prominence-tied block where a low continuous `meta` confidence buries
them. And 14 of 25 POI gold cases have more than two tokens, so the head path
returns nothing regardless of cap (`tokens.len() <= 2` is a hard limit).

**Planet-scale caveat, stated because it cuts against the decision:** required
head caps grow roughly as sqrt(n). Wave C measured this directly by widening
from home-region-only to all 15 regions (eiffel 3 -> 5, big-ben 6 -> 15, louvre
20 -> 455). If planet postings for global tokens are ~30x this union, cap 10 may
hold only ~3/11 and cap 64 ~6/11 at planet scale. The ordering of the answer
survives; the counts probably do not.

**One Wave C claim was checked and is WRONG.** It reported Foursquare's flat
default as 86.2% rather than 100% and attributed the softening to top-level
`confidence` vs `sources[].confidence`. Re-measured directly on the top-level
column: Paris 1 distinct value / 100% at 0.77, Tokyo 1 / 100%, but Seattle 299
distinct / 36.2% and NYC 1,031 / 35.9%. The cause is the **US region mix**, not
the column. The claim in `2026-08-01-confidence-and-duplicates.json` -- exactly
0.7700 across six NON-US regions -- stands exactly as scoped.

### Track A result, 2026-08-02: `categories.alternate` required, evidence at v3

`3d94b4f`. The prior no longer degrades to primary-only.

**Why promoting the field was honest, and the argument is empirical rather than
procedural:** the inventory learns nullability from the real release and requires
it identical across every source object, so a **successful regeneration is itself
the proof** that release `2026-06-17.0` satisfies the stricter contract. The
recorded runs already met it; nobody was checking.

The regenerated inventory differs in exactly **19 leaves, all contract-or-hash,
zero outside them**. Map plan and every total byte-identical: 89 map tasks, 16
objects, 75,642,289 records, 5,120 row groups.

    inventory_sha256    b1830aee... -> 9ea4eff6...
    schema_fingerprint  49453ed2... -> 31809dba...

**It is a v3 GENERATION, not an overwrite.** v2 is retained exactly as v1 was
retained beside v2 -- it remains the true attestation of the runs it describes.

- **Re-measured:** all seven task runs (1, 13, 73, 76, 85, 86, 87). A task run
  embeds its projection report and that report binds the contract hashes, so
  they could not be carried. All seven `binding_equal=true`,
  `deterministic=true`, speedups 4.2-6.8x. **Role selection came out identical
  to v2** under the new inventory, which is a real check rather than a
  coincidence: roles are chosen from inventory + census, so a shifted plan would
  have moved them.
- **Carried forward unchanged:** the twelve census reports and the functional
  rehearsal, after verifying that neither records any contract hash.
  `host-provenance-v3.json` states that split explicitly, and the task runs did
  execute on `bradflix`, the host v2 already declares for them.

`readiness-v3.json` is `ready=true`, no reasons. **It must be produced under the
spec's frozen runtime** -- python 3.12.12, duckdb 1.5.1, numpy 2.3.5, pyarrow
25.0.0, rustc/cargo 1.97.1. The validator compares the live runtime against the
spec and correctly refuses under 3.11, which is worth knowing before anyone tries
to regenerate it on a hosted runner pinned to 3.11.14.

**Local-environment note:** `unicodedata2==17.0.0` is required to run the suite
or any rehearsal step locally. It is x86_64-pinned in the hosted requirements,
and without it 14 tests fail and 7 error for reasons that have nothing to do with
the code under test. With it the suite is 1569 passed, 0 failed.

**Remaining before dispatch: nothing in Track A.** Track C (item 2's ~84,000
sequential HEADs, and the second half of item 1's precondition) is optional
efficiency, not a blocker.

### 2026-08-04: commodity POIs are cap-evicted, and a cap raise still does not fix it

Ranking all 13 everyday and 9 gold EXACT-match IN_HEAD misses against
`CAP_ORDER` straight from the head-candidate packs. Two losses, independent,
and fixing either alone changes nothing.

**Loss 1 — the distinctive token.** Commodity POIs really do sink below
`HEAD_RESULT_CAP` on their own name word, and by a wide margin: `habana` 72 of
452, `harrods` 22 of 69, `novotel` 91 of 500, `mayo` 213 of 753 — all lower
bounds, since candidate packs are already capped per map task. Every one
carries `prominence_rank = 0` from `COMMODITY_CATEGORIES`. So the record is in
NO posting at all.

**Loss 2 — the generic token.** The head intersects a top-`n` list per query
word, so a record must survive the cap in EVERY token, and `hotel` / `london` /
`clinic` / `museum` cannot be won at any cap. This inverts the user's
expectation: `Harrods London` is harder than `Harrods`, not easier.

So **a cap raise remains the wrong plan** — it addresses loss 1 and leaves
loss 2 untouched — but not because the cap is innocent. The cap-raise machinery
below stays moot, independently of its attestation cost.

**The lever is admission.** An `e2:hotel habana` phrase posting has ten slots
for a phrase almost nothing else carries, where `hotel` has ten slots contested
by every hotel on Earth. That posting has zero rows today because the phrase
lane admits only `prominence_rank > 0` — the class that most needs a phrase key
is the one excluded from it. One admission-rule change, rebuild-scoped.

One refuted clause worth keeping: the sinking is NOT `feature_id` order.
`tokens_decided_by_feature_id` is 0 across both sets; these records lose to
genuinely more prominent contenders, not to a UUID coin flip.

**Do not look for a query-time fix here.** `merge_bounded_candidates` already
relaxes a saturated posting and proves the missing token from
`record_display_tokens`, which already spans `locality`, `region` and
`country`. It cannot help, because it rescues only records that survived the
cap in at least one token and these survive in none. An earlier draft of this
section recommended exactly that redundant Worker change; the error surfaced on
trying to implement it.

Full measurement, and the three things the probe deliberately refuses to
overstate, in `docs/plans/2026-08-04-head-cap-eviction-ranks.md`.

### The release move is not a quality lever, 2026-08-04

Two complete local planet Places builds, same machine, same driver, differing
only in `release`. The `2026-06-17.0` head completed 2026-08-04T23:36Z in ~2h
with **zero DuckDB spill** at `duckdb_memory_limit=40GB`; the `2026-07-22.0`
head was already on disk. Probe
`benchmarks/probes/2026-08-04-release-move-recall-delta.py`, evidence
`benchmarks/2026-08-04-release-move-recall-delta-{everyday,gold}.json`.

**The newer release is smaller: 74,223,561 places against 75,642,289, a loss of
1,418,728 (-1.9%)**, while emitting 160,522 MORE distinct head index entries.
Fewer records with more tokens is the shape of upstream conflation, and it means
the move can lose what it does not gain. It was framed as the only change that
adds data; it does not.

Plain-head reachability, exact-name tier, per case in both releases:

| set | cases | servable @06-17 | servable @07-22 | gains | losses |
|---|---|---|---|---|---|
| everyday-POI 200 | 174 | 54 | 55 | 3 | 5 |
| forward gold 55 | 32 | 2 | 2 | 0 | 0 |
| **total** | **206** | **56** | **57** | **3** | **5** |

**Net +1 in 206.** On all tiers including containment it is 3 gains against 17
losses. All eight reachability flips are CJK, and two of the losses — HK
`沙田醫院` and KR `서울대학교치과병원` — are cases production answers correctly
today, going `SERVABLE -> ABSENT`. Probing current HITS as a regression control,
not just misses, is what exposed them.

**The gold set does not move with vintage at all**: 31 of 45 cases are EVICTED
in BOTH releases with zero reachability flips. That independently confirms, by a
different instrument, that the lever is admission.

Two things this measurement does NOT say. `SERVABLE` models the head
token-intersection lane only, so it is a LOWER BOUND on what production serves —
only 2 of 27 exact-tier gold production hits register as servable, the rest
being answered by the phrase lane or the prefix-head fallback. The delta is
sound because both sides run the identical function in one process and the
unmodelled lanes cancel; the LEVEL is not, and must never be quoted as
"the system reaches only 2 gold cases". Second, it does not diagnose the CJK
losses — five HK/KR hospital names degraded between releases is a bounded
follow-up, not a finding.

Full evidence and limits in
`docs/plans/2026-08-04-release-move-recall-delta.md`.

### The P1968 sidecar is a dead end for Places, 2026-08-05 — DO NOT REOPEN

**Planet-wide there are 7,234 Wikidata items carrying `P1968`** (Foursquare
venue id), against 75,642,289 Overture places — 0.0096%. Counted directly at the
Wikidata SPARQL endpoint. The Phase 0 candidate set was a `LIMIT 1000` query
joined to the Foursquare bridge, so the 200-decision audit was sizing a path
whose entire planet ceiling is 7,234 entities. No amount of review makes that a
fame signal.

**The need was real; only the route is dead.** Overture has no ENTITY fame —
`prominence_rank` is a per-category prior, so nothing distinguishes *the* Times
Square from *a* Times Square, which is the homonym failure dominating the gold
set. But `theme=base` carries `wikidata` as a first-class column on
`infrastructure`, `land_use` AND `land` (verified by DESCRIBE on all three).
Golden Gate Bridge is `base/infrastructure` `bridge` `Q44440`; Times Square is
`base/land_use` `plaza` `Q11259`. Neither exists in `places`. **For landmarks —
the explore-site case — the QID is already in the data and needs no matcher, no
ledger and no hand adjudication.**

**Measured reach, 2026-08-05:** of 45 gold POI cases, **14 case rows (10
distinct entities) have an exact-name, non-transit match in base, and all 14
are cases the Places head cannot serve** (12 EVICTED, 2 QUERY_REFUSED) — about
31% of the gold POI set in pure addition. 9 of the 14 carry a QID and 10 carry
`names.common`. A further 12 exact-name matches are transit stops NAMED AFTER
the landmark and are excluded from that count (Union Station is the one case
where the transit match is correct). 14 cases were not found, but
`theme=buildings` (276 GB) was not scanned and several of those are plainly
buildings — that bucket means "not in the three scanned base types", never "not
in Overture". Detail and the two probe defects corrected before publishing:
`docs/plans/2026-08-05-gold-coverage-in-base-theme.md`.

Not scoped by that measurement, and required before any of it serves: base
features are polygons and lines where the places path assumes a point, and they
carry `subtype`/`class` rather than Overture place categories, so
`prominence_rank` needs a mapping for them.

Three things settled on the way, so they are not re-litigated:

- **Circularity refuted.** The worry was Wikidata -> Foursquare -> Overture ->
  join back to origin. Foursquare venue ids are ObjectIds, so they carry a
  creation timestamp: the venue is older than its Wikidata item in **98 of 100**
  pairs, median lead **7.2 years**, and **28 of 28** on the Thai subset at
  10.0 years. Foursquare cannot have sourced items that did not yet exist.
- **There is no matcher.** `match_method` is `direct_source_wikidata_id` on all
  200 — an equality join — and every P1968 claim sampled carries zero
  references. A precision number here would not transfer to fuzzy matching.
- **The gate was over-specified.** A wrong link mis-ranks one POI; that is
  bounded and reversible, not corruption. Any revival should gate
  *fame-weighted*, since a false link to a top-tier QID is the only real risk.

Audit stopped at 57/200, `integrity_ok: true`, nothing marked eligible for
prominence. Full evidence and the two instrument defects found (single-language
label scoping; whole-string name-overlap flag) in
`docs/plans/2026-08-05-sidecar-p1968-dead-end.md`.

### `names.common` is identically empty across all Places, 2026-08-05

Measured on the local 2026-06-17.0 planet corpus, and it explains several
otherwise confusing results.

| theme | `names.common` populated |
|---|---|
| divisions / division | 1,526,014 of 4,655,003 — **32.78%** |
| divisions / division_area | 432,403 of 1,073,093 — **40.30%** |
| base / infrastructure | populated (Golden Gate Bridge carries 11 languages) |
| **places** | **0 of 75,642,289 — exactly 0.0000%** |

`names.rules` on places IS populated (455,111, 0.60%), so the struct is not
inert; only `common` is empty, and it is empty at exactly zero. Same release,
same publisher, same field, populated in two other themes.

**Consequences already paid.** This is why a CJK query cannot reach an
English-named record, why `Brandenburger Tor` cannot match "Brandenburg Gate",
and why the alt-name rescoring workstream measured a ceiling of 5 cases: there
were no alternates to score. Any future plan that assumes Places alternate names
exist is unfounded — check this number first.

Not established: whether Foursquare's own venue objects carry locale variants.
What is known is that Foursquare stores local-script names as the primary (the
Thai venues are natively `วัดลาดบัวหลวง`). Whether Overture drops localization in
the Places path or no source supplies it is unresolved, but "no source supplies a
single alternate for any of 75.6M records" is not a plausible data outcome.

### The Worker apostrophe fix is live, and its yield is UNMEASURED, 2026-08-07

Merged `#265`, deployed on `fc90f1f`. An apostrophe-born one-character token no
longer consumes a `HEAD_QUERY_TOKEN_CAP` slot, so `Queen's Medical Center`
reaches the head lane as three clauses instead of being refused as four. The
phrase lane keeps the unfiltered clauses, because `eN:` keys are built from the
record's full name and a filtered key names something the head does not hold.

**Paired gates: zero flips, zero errors, on all three frozen sets.**

| set | rank@1 | rank@10 | paired to-hit / to-miss |
|---|---|---|---|
| gold 55 | 31 -> 31 | 40 -> 40 | 0 / 0 |
| everyday 200 | 69 -> 69 | 70 -> 70 | 0 / 0 |
| proximity 40 | 28 -> 28 | 40 -> 40 | 0 / 0 |

**And zero recovered on the only stratum that tests the class.** The five frozen
apostrophe cases are unchanged, both spellings, against their 2026-08-06 rows:
0/5. The reason is that four of the five are not the class the fix addresses --
`Paula's Cafe` is three clauses and was already under the cap -- and the one
that is (`Len's Mill Store`) now gets *asked* and still returns empty, because
its remaining tokens are commodity words whose postings evict it at the cap
of 10.

So the 733,701 figure in `2026-08-07-apostrophe-folding-locus.md` counts records
whose own full name becomes **askable**, exactly as that document's Limits
section said, and not queries that improve. The change is kept because it is
provably identical for every query without an apostrophe (pinned by test), costs
nothing, and removes a real cap cost -- but **it has no demonstrated
user-visible gain, and none should be claimed for it** until a purpose-built
four-clause possessive stratum with presence controls measures one. That stratum
does not exist; the natural-feature stratum is the design to copy.

### Phrase admission is sized, and §3.2 is closed, 2026-08-07

The two "Decision input PENDING" lines in `2026-08-04-v5-build-readiness.md`
§3.1/§3.2 — the join §3.2 itself called "the central v5 product decision" — are
computed. Probe
`benchmarks/probes/2026-08-07-phrase-admission-sizing-join.py`, result
`benchmarks/2026-08-07-phrase-admission-sizing-v1.json`, write-up
`2026-08-07-phrase-admission-sizing.md`. Offline, 33 seconds, no credentials.

The replication is checked against what the real planet build emitted:
2,381,840 predicted `e2:`/`e3:` keys against 2,381,564 emitted, **99.988%
agreement** on the admission unit.

Head facts are derived from ONE build and not mixed: the complete local planet
build of `2026-06-17.0` — 33,604,005 records, 5,717,067,235 B, 4,096 shards,
170.131 B per record, mean shard object 1,395,768 B. That is deliberately not
the 5,141,583,720 B published by hosted run `30288619536` (30,841,082 records)
recorded further down, nor the live-head figure in the apparatus doc: those are
different generations, and a cross-generation bytes-per-record ratio would be
meaningless. The growth *fractions* below are record ratios and hold either way.

| option | new head records | new head bytes | head growth | claims |
|---|---|---|---|---|
| §3.1 `e4:`, prominent only | 999,815 | 170,099,057 | +3.0% | 3 of 145 |
| §3.2 admit `prominence_rank == 0` | 34,457,893 | 5,862,339,654 | +102.5% | 19 of 145 |

**Operator decision on this evidence: §3.1 is adopted for v5, §3.2 is CLOSED.**
§3.2 doubles the planet head (5.72 -> 11.58 GB, mean shard object 1.40 ->
2.83 MB, and every head read transfers the whole object) for 19 claims, 11 of
which are Mexican registry hotels — non-prominent by design, inside a stratum
the denominator rebaseline already flags for population shift. Bounding it by
key rarity does not rescue it: *globally unique* keys alone still cost +76.0%.
The two options never competed for a budget; they differ by 34×, and the
budget — the 31.7 MB reserve — was a rehearsal-fixture artifact. **Size any
future head growth against mean shard object bytes, 1,395,768 B over 4,096
shards.**

Yield is an upper bound: a claim means the phrase key would exist and the record
is refused today, not that the case would rank. The cost also ignores head-build
DuckDB spill, which is what killed the v4 merge at 79%.

Side finding, and it corroborates the instrument work: three quarantined AU
cases (`everyday-au-41e89cc4…`, `everyday-au-2a73f159…`, `everyday-au-9b864485…`)
have a name-matching record inside the quarantine rule's own radius, so they are
not ABSENT. The rebaseline's equal-blind-rate scenario predicted exactly 3 false
quarantines; that extrapolation is now independently measured at 3, and the
corrected everyday denominator should be read as **n=111**, not n=108.

### theme=base is a coverage lever, not the fame lever, 2026-08-07

Scoped in `2026-08-07-base-theme-landmark-import-scope.md`; inventory in
`benchmarks/2026-08-07-base-theme-import-inventory-v1.json`.

Two corrections to the citation in the 2026-08-06 review, which called this
"the only living fame lever" on 14 gold rows "all currently unservable":

- the 14 rows are **10 distinct landmarks** (the gold set carries `name` and
  `name_locality` variants of each), and **production already serves 6 of
  them**. The incremental gold yield is **4**: Golden Gate Bridge, Times
  Square, The Royal Children's Hospital, Mayo Clinic;
- **12 of the 26 exact base name matches are transit stops named AFTER the
  landmark** — `Harrods` matches a bus stop, `Colosseum` and `Brandenburg
  Gate` match subway stations, `Louvre Museum` a ferry terminal. Any admission
  set must exclude `subtype = transit`, or landmark queries start returning
  stops.

What base really offers is coverage of classes Places does not carry (bridges,
peaks, islands, plazas, parks) and **`names.common` on 2,602,578 named rows**
against Places' zero. A class-scoped admission set sized from where the genuine
gold landmarks live is 4,707,069 rows, an upper bound of **+14.0% of head
records** — 54% of which is bridges, so +6.4% without them.

**The homonym/fame class therefore still has no open mechanism.** Using base as
a prominence signal instead of as records would be cross-theme entity
resolution, which is what killed the P1968 sidecar; it is explicitly not
recommended.

**All four preconditions are now measured** (`2026-08-07-base-import-measurements.md`):

- **duplicate rate 5.5%** against Places at 200 m (3.65% at 50 m), concentrated
  in five civic classes — hospitals 23.6%, parks 19.2%, colleges/universities
  ~19%, clinics 18.3% — while bridges (0.8%), islets, islands and artwork are
  almost pure addition. Collapse is a precondition for admitting the civic
  classes, not for the import;
- **real head cost +4.76%** (1,599,688 records, 272,155,770 B) once the frozen
  per-token cap of 10 is applied against the actual head input — a third of the
  +14.0% upper bound. The method reproduces the hosted run's 30,841,082
  ordinary-token head records exactly;
- **the natural-feature stratum is built and run**:
  `benchmarks/natural-feature-cases-v1.json`, 114 Wikidata-gold cases across 20
  countries and six classes, each carrying offline Places and base presence
  controls. Production scores **15/114 at rank 1 and 20/114 at rank 10**, and
  every hit is one of the 28 Places-backed cases. The import raises the
  achievable ceiling **28 -> 47**; 67 cases are absent from Overture entirely;
- **a names-only join is refuted**: it would reach 25,026 Places records, 2.8%
  of the 893,972 admitted rows carrying cross-language names. Those names live
  on features Places does not have, so they arrive only if the records do.

The import's effect on the two frozen sets remains unknown and unknowable
offline; it needs the paired gate like any other change.

### If the head cap is ever raised, it is contract-bound

`head_result_cap` is not a plain default. `acceptance_gates.head.result_cap_per_token`
(and `candidate_cap_per_task_token`) live in
`benchmarks/places-construction-v1-evidence-spec-v2.json`, whose sha256
`5b779b9fadc7987bbf794d90c45e62da2866a43ef14ba4b90b904aea0ad0414d` is pinned at
`scripts/construction_v1_control.py:45` and asserted by
`test_control_pins_match_the_real_committed_evidence_files`.

So it would hit the same attestation wall as `categories.alternate` and DuckDB.
**With the cap staying at 10 this is now moot for the coming rebuild** -- kept
here because it is the fact that decides how any future raise is scheduled: a
cap change is an evidence regeneration, not a one-line edit, so it must be
batched with whatever else re-attests.

The re-attestation pass for THIS rebuild is therefore just step 1 below. Steps 2
and 3 apply whenever the cap or DuckDB do land:

1. `places_inventory_v1.py` -- promote `categories.alternate` from optional to
   `REQUIRED_FIELD_TYPES`. The projection currently degrades to a primary-only
   prior without it, so a rebuild that skips this ships a deliberately weakened
   prior. Note this also changes every row group's
   `selected_compressed_bytes` / `selected_uncompressed_bytes`, so the map plan
   re-partitions -- and `sources` was NOT added, deliberately, because it is a
   fat nested column and the per-task gates are
   `selected_uncompressed_bytes_hard_cap` 1 GB and `row_groups_hard_cap` 64.
2. the evidence spec -- `result_cap_per_token` and
   `candidate_cap_per_task_token` 10 -> 64, **sized against Wave C Q4 first**.
   Raising the cap grows head RECORDS, not index entries (entries track distinct
   tokens: the planet head was 30,841,082 records against 13,971,501 entries),
   but the real multiplier is a measurement, not an inference -- most tokens hold
   fewer than 10 candidates. Check against
   `per_shard_index_entries_hard_cap` 250,000 and
   `head_output_hard_cap_bytes` 1 GiB.
3. regenerate inventory / evidence / readiness, then re-pin
   `inventory_file_sha256`, `inventory_sha256`, `schema_fingerprint_sha256`,
   `spec_sha256`, `readiness_file_sha256`, `scale_evidence_sha256` in
   `construction_v1_control.py`.

**Do not rewrite the pins to match without regenerating.** That falsifies an
attestation of a run that really happened -- the standing rule from `cfb9601`.

### Sequencing, four tracks

- **Gate 0 -- Wave C. PASSED 2026-08-01.** See "Wave C result".
- **Track A -- DONE 2026-08-02 (`3d94b4f`).** `categories.alternate` became
  REQUIRED and the evidence was regenerated as a **v3 generation**, with v2
  retained rather than overwritten. At that point `readiness-v3` was
  `ready=true` and the control pinned the v3 chain. The current control now pins
  the later, fully fresh v4 generation recorded in the 2026-08-03 audit-closure
  section. Detail below.
- **Track B -- build-side quality.** Stage 3 famous-unique is OUT of this
  rebuild: it needs a re-spec onto `prominence_rank` (its design still names
  quantized confidence as the fame proxy) plus encoder/verifier/oracle lockstep.
- **Track C -- pipeline efficiency, fully parallel.** Item 2 (~84,000 sequential
  HEADs, one loop measured at 17-36 minutes, on the promotion critical path) and
  item 1's precondition, which is what unlocks zero-copy promotion for this run
  and is **worse than previously recorded** -- see immediately below.
  **Item 8 (launch reverse at the map barrier) is MOOT for this rebuild** --
  reverse is not re-running, so its -2h15m applies only to a future run that
  does rebuild reverse. Do not spend the week on it.
- **Track D -- streets, fully parallel and NOT on the rebuild path.** See below.

### Item 1's precondition is MET as of `8e4e0fc` — corrected 2026-08-07

**Zero-copy promotion is unblocked. The rest of this section is the hazard as
it stood before `8e4e0fc`, retained because it explains what the guard has to
do; its conclusion — "keep `release_slice_version` EMPTY" — no longer holds.**

Both halves of the precondition exist:

1. **A v2-aware unreferenced check**: `scripts/v2_retention_guard.py`, which
   resolves `v2/catalog.json` -> `v2/releases/{build}/release.json` -> every
   family source and retained external `operation_sources`. It **scans
   recursively rather than enumerating fields** (the v1 guard missed the v2
   chain precisely by enumerating), and it **fails closed** on a missing
   release document, a byte-mismatched one, an unreadable catalog, or a partial
   target. 17 tests of its own, plus the wiring tests below.
2. **A phase that can match a slice prefix**: phase 3 emits a bare
   `<prefix>/` target for each entry in `ORPHAN_PREFIXES` with no
   version-format restriction, and the guard accepts `slice-YYYY-MM-DD.N` as a
   bucket-root prefix. `r2-cleanup.yml` calls the guard in **both** bucket-root
   delete phases (3 and 5), fetching the chain before either of them.
   *Not* "before any delete", as this section first claimed: phases 1 and 2
   delete before the fetch step. That is sound but for a different reason —
   they pin their targets to `*/staging/` and
   `^staging/global-v2/[0-9a-f]{64}/$`, neither of which can be a bucket root.

**A third half was missing, and it was the dangerous one.**
`release-slice-families.yml`'s cleanup job ran
`aws s3 rm "s3://geocoder-shards/${SLICE_VERSION}/" --recursive` behind nothing
but a shape regex — the very shape zero-copy publishes into. It is `always()`,
so it ran even when preflight failed and every build job was skipped, and its
one reference check (`probe_catalog_excludes_slice.sh`) walks the **v1**
catalog's child links, so it reports "not referenced" for a fully live promoted
slice, always. Found by adversarial review of this section, 2026-08-07; the job
now fetches the same chain and calls the same guard before deleting.

The wiring is contract-tested structurally, not by substring count
(`test_every_delete_is_guarded_or_pinned_below_the_bucket_root`,
`test_the_guards_verdict_stays_fatal`). Mutation-checked against the four ways
this regresses in practice: `|| true` on the guard call (including on a
continuation line), `continue-on-error` on its step, reordering it after the
delete, and deleting the guard step outright. All four fail the suite.

**Still open, recorded not fixed:**

- **Pre-promotion window.** Between finalize and promotion nothing in the v2
  chain names the new slice, so the guard correctly reports it unreferenced and
  would permit deleting a just-completed planet run. The create-only claim at
  `<version>/claims/<family>.json` is the artifact that proves the namespace is
  spoken for, and the guard does not read it. Teaching it to would also make
  abandoned slices undeletable without an explicit override — a real trade-off,
  not an oversight.
- **Phase 3 needs a workflow edit to target a slice**: `ORPHAN_PREFIXES` is a
  hardcoded `env:`, not a dispatch input.
- **Phase 3 has no cache-TTL wait** (phase 5's 390 s wait is `if: phase5`), so a
  slice deleted immediately after being dereferenced can 404 for live isolates
  for up to `CATALOG_CACHE_TTL` = 300 s.
- **`rebuild-r2-shards.yml`'s retention prune is still v1-only** and can delete
  a legacy core that a live v2 release binds.

**What remains before flipping `release_slice_version` on is a decision, not an
engineering gap**: the slice smoke already promotes both layouts and asserts
byte-identical published `routing.json` and family manifests on every change
(`0bf477e`), and the measured Monaco result was 21 objects copied under the
construction layout against **0 copied / 21 prepositioned** under the release
layout. At planet scale that is the ~158 GiB / ~123 min forward copy.

### Historical: the cleanup guard was v1-only, and that was a hazard

Previously recorded as "no `r2-cleanup.yml` phase matches an abandoned
`slice-YYYY-MM-DD.N/` prefix". Investigated 2026-08-01, and the real shape is
more dangerous than an absence:

**Phase 3 CAN delete a bucket-root prefix.** It reads the `ORPHAN_PREFIXES`
allowlist (currently `2026-07-17.0`, a plain version prefix, NOT under
`staging/global-v2/`), and its only guard is
`prune_catalog.py assert-unreferenced`.

**That guard cannot see v2 at all.** `is_referenced`
(`scripts/prune_catalog.py:117-122`) walks child links of the **v1**
`catalog.json` only; the module contains no reference to `v2` or `slice`
anywhere. But a live slice is referenced exclusively through the v2 chain:
`v2/catalog.json` -> `v2/releases/<build>/release.json` -> the family manifest's
`slice_version` and `{version}/slice-manifest.json`
(`scripts/v2_release_manifest.py:408-414`, `:804-808`).

So adding a slice prefix to `ORPHAN_PREFIXES` to clean up an abandoned one would
also report "unreferenced" for a **live, serving** slice. Both phase 3 (`:374`)
and phase 5 (`:491`) call that guard. `PROTECTED_PREFIXES` lists no slice.

This is latent rather than live -- no slice prefix is in the allowlist today, so
nothing is currently at risk. But it means the precondition is **two** changes,
not one: a phase that matches `slice-*/`, AND a v2-aware unreferenced check that
resolves the release chain before permitting any slice delete. Shipping only the
first would make a 45 GiB cleanup path whose safety check is blind to the thing
it is protecting.

~~Until both land, keep `release_slice_version` EMPTY, which preserves the old
copy-through behaviour exactly.~~ **Both landed in `8e4e0fc`; see the
correction at the head of this section.**

### The street layer is scoped but unimplemented, and is decoupled

`FAMILIES = {"addresses", "places"}` (`global_build_manifest.py:23`); there is no
street family and the Worker rejects any other name. The **7 acceptance
experiments have never been run** -- local transportation artifacts are empty.
`benchmark_transport_components.py` is the offline experiment that produced the
Boston sizing (65,176 segments, 22,385 named, 5,832 clusters), and says in its
own docstring that it is not a shard builder. `build_us_streets.py` is an
unrelated legacy US radix-trie prototype referenced by no workflow.

Free-text address search returns `capability_unavailable`
(`v2.rs:2290`); street-only free text is the deferred item designed to ship
first because it sidesteps the house-number parser.

**It does not gate or ride the rebuild.** The catalog contract supports a new
family arriving through a non-promoting families-only slice, so streets can be
built without touching live Places/Addresses. It is also the only genuine
one-way door in the plan -- first publication is a permanent decode obligation
across 64 retained releases -- so it should not be rushed into the rebuild week.

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

**Sharpened 2026-08-01.** Two things now make Wave C more valuable, not less:
the tie-break below prominence is `confidence_rank`, which is an upstream
*source identifier* for roughly a fifth of the corpus, and the 98 tied groups
are substantially the duplicate phenomenon (the Eiffel Tower alone is ~87
records under 53 name-forms). Wave C should therefore emit, per token, **which
level of the cap key actually decided** — identity / prominence / confidence /
`feature_id` — and cross-tab the confidence level by `sources[].dataset`. The
probe queries Overture directly, so `sources` is available to it even though
the construction pipeline drops it. That converts "confidence is a source id"
from a background finding into a measured eviction rate.

### Wave A/B progress, 2026-08-01

Landed: **item 9** (definite-5xx copy retry), **item 7** (reverse max_parallel
4), **item 13** (ARM — construction and slice-smoke on `ubuntu-24.04-arm`,
validated by a green ARM slice-smoke on real data first), **item 12** (binaries
built once in a `binaries` job from the request-pinned producer commit), and the
**data half of item 6** (every reduce marker carries its grouping-invariant
reduction record; `construction_v1_hosted.py export-reductions` rebuilds the set
from R2 — verified 4/4 on the Monaco slice with an empty local store).

**Item 6 is now complete, both halves** (`be48baa`). `promote-slice` reads the
published family manifest for the expected partition count, rebuilds the set
from R2 with `export-reductions`, and falls back to `cv1-reduce-*` only for a
slice built before the marker carried its record — emitting a `::warning::`
when it does, because a silent fallback is a deadline nobody knows they are
under. The set must equal the manifest's `partitions` whichever source supplied
it, which is also the first per-partition check the artifact path ever had.
Verified on the Monaco slice: promotion planned from the R2-exported reductions
is **byte-identical** to promotion planned from the full artifact records
(`dc8e1360…5970`).

**Item 1 landed the same day** (`0a37456`), ahead of its queue position,
because it is independent of Track C and is the largest single arbitrary copy
in the system. `construction-v1.yml` takes an optional `release_slice_version`;
finalize publishes the serving objects straight into
`<version>/families/<family>/objects/`; `promote-slice` reads the layout off
the finalize marker's own keys and binds them. Measured on the Monaco slice,
both layouts promoted end to end:

| | copied | prepositioned | destination |
|---|---|---|---|
| construction layout | 21 | 0 | 23 objects, 34,540,148 B |
| release layout | **0** | 21 | 23 objects, 34,540,148 B |

with **byte-identical** published `routing.json` and family manifest. The slice
smoke now runs both layouts and asserts that equality on every change
(`0bf477e`).

**Item 1's precondition was unmet and is now MET (`8e4e0fc`, corrected
2026-08-07).** The risk it guarded against is real — a wrong
`release_slice_version` puts ~45 GiB (Places) or ~114 GiB (Addresses) of inert
objects into a release namespace — but an abandoned `slice-YYYY-MM-DD.N/`
prefix is now removable: phase 3 accepts a slice prefix and
`scripts/v2_retention_guard.py` refuses to delete one that any live v2 release
still references.

### Wave D — the operator priority

**Item 11 / Track C: one resumable planet job replacing four workflows.** The
planet path is 4 workflows and 22 jobs (`construction-v1` 8, `reverse-v2` 3,
`release-slice-families` 6, `promote-v2-release` 5), each boundary a
GitHub-artifact hand-off plus a fresh environment plus a re-authentication.
Track C (`2026-07-28-planet-build-wall-clock-review.md:411`, Wave 5,
**unexecuted**) is the existing design. It subsumes item 12 by keeping binaries
warm, and pairs with items 1/6/9 as the copy-minimization half.

**Scope written 2026-08-01: `2026-08-01-one-planet-job-scope.md`.** Its central
finding: "one job" cannot mean one GitHub job. Measured phase wall clock (Places
head 207 runner minutes, reverse 2h15m/2h33m, finalize 56m50s) exceeds the
6-hour job ceiling, and `test_every_phase_carries_the_330_minute_job_timeout`
pins the per-phase limit. It has to mean one DISPATCH and one RESUME BOUNDARY
over a long-lived worker pool — which is what Track C says, and the distinction
needs stating before anyone implements against the phrase.

### Still open, unscheduled

RC2 and RC3 are closed. Bounded producer admission for Empire State Building
and Big Ben is also complete and live in `2026-08-03.0`. Duplicate collapse and
any future `head_result_cap` change remain unscheduled. Brandenburg Gate belongs
to source alias/tokenization plus routed-cap work; Machu Picchu Cusco belongs to
region-context routing. Do not bundle these mechanisms into another ranking or
rebuild pass.

## Current snapshot

The checkpoint on `main` before this state-only update is `93f63e6`. It includes
the accepted phrase-serving corrections (`01856db`), final preview evidence
(#248), the operation-preserving overlay publisher (#249), and post-promotion
reverse smoke (#250). There are no open construction-v1 code PRs.

Historical checkpoint `8aea031` mattered for the 2026-07-31 promotion.
`promote-slice` fetched
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

**v2 release `2026-08-03.0` is live as of 2026-08-04T03:31Z**, backed by
protected core `2026-07-18.0` and Overture `2026-06-17.0`. Places forward is
served from `slice-2026-08-03.0`, request-matched Places reverse from
`slice-2026-08-04.0`, Address structured forward from `slice-2026-08-02.0`, and
Address reverse from `slice-2026-07-30.0`. Global-head and routed `/v2/forward`,
structured Address `/v2/forward`, all three `/v2/reverse` families, and
release-pinned ID lookup return the advertised build.

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

Historical note: the 2026-07-31 promotion's `/v2/forward` smoke failed 10/10
times after `OK divisions` because `Eiffel Tower` returned no features. That
was a true observation of the then-open name-only quality defect, but the wrong
fixture for a publication smoke. Later RC2 and v4 work fixed the query, and the
current smoke separates publication capability from the semantic benchmark.

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

## Planet Places rebuild and promotion, 2026-08-02

The rebuild scoped on 2026-08-01 (Places only, DuckDB held, reorder at cap 10,
`categories.alternate` at v3) was built, promoted, and measured. `/v2` is live
on build `2026-08-02.0`.

| stage | run | result |
|---|---|---|
| construction | `30728476415` | 10h06m wall; 219 jobs; `reconciles=true` |
| Places reverse | `30748269856` | 65m30s wall / 225 runner-min; 18 jobs |
| `promote-slice` | `30752624029` | 123 min; 21,279 objects copied |
| `publish-release` | `30757323897` | `v2/releases/2026-08-02.0/release.json` sha256 `9a7cbece...1c784` |
| `promote-catalog` | `30757383528` | CAS `1365b737...` -> `0457d57f...`; smoke green attempt 1/10 |

Construction phase wall clock: map 2h43m (89 jobs, concurrency 4), plan 1h40m,
reduce 2h26m (128 batches), **head 2h35m32s**, finalize 40m33s. Map+reduce cost
1,203 runner-minutes across 217 jobs. Output was byte-shape-identical to the
prior build: 40,931 objects, 20,698 serving + 20,231 positions, 4,096/4,096
populated shards -- only ordering changed.

### The head cap still lacks its second input

**Head ran 155.5 min against a 330-minute budget (47%).** That half of the
head-cap question is now answered with real planet data.

**The DuckDB temp peak is still not measured.** The head step emits only the
*configured* spill cap (9,126,805,504 B, i.e. `DUCKDB_TEMP_SHARE` overridden
4->2 against the 17 GiB scratch cap) and `staged_peak_resident_bytes`
(1,505,847,088). Nothing samples the temp directory high-water mark. The run
tripped neither the 8.5 GiB spill cap nor the 17 GiB whole-stage watchdog, which
bounds the peak from above but does not measure it. Raising the head cap still
requires instrumenting the head step and observing another planet run.

### Reverse re-attestation is forced by the chain, not by the data

`publish-release` requires `.request_sha256 == <forward request>` on every
family's reverse catalog (`promote-v2-release.yml:677`), so the existing Places
reverse -- bound to `88b7f171...` -- could not attach to a forward built at
`f3c7eef3...`. Reverse had to be rebuilt purely to re-attest.

**The rebuilt reverse produced 75,631,061 records over 16,511 cells: delta zero
on both against the prior build.** The operator's judgement that Places reverse
did not need rebuilding was correct about the data; only the attestation chain
disagreed. A re-attestation path that rebinds a reverse catalog to a new forward
request without rewriting 75.6M records would convert 225 runner-minutes plus a
full second copy in R2 into a metadata operation. Recorded, not scheduled.

Also measured: `max_parallel=4` cut reverse wall clock from 2h15m to 65m30s.
The prior 2 was a dispatch default, not a limit, exactly as the workflow comment
claimed.

### Addresses reduction records are a durability gap

The Addresses construction run `30215529919` predates item 6, so its reduce
markers carry no full reduction records and the R2 export fails:

    addresses reduce marker 0000 carries no full reduction record. It was
    written by a producer predating item 6; promote from that run's GitHub
    artifacts instead.

Promotion therefore fell back to that run's GitHub artifacts, whose retention
expired **2026-08-02T22:30Z** -- inside the same day. Had promotion slipped, the
Addresses family could not have been promoted into a new slice at all.

Backed up before expiry, verified by round trip (`sha256 915b1117...14e4f4`,
581/581 records restored):

    backups/construction-v1/88b7f171.../addresses-30215529919/artifacts.tar.gz
    backups/construction-v1/88b7f171.../addresses-30215529919/backup-manifest.json
    /home/brad/dev/cv1-artifacts-backup/addresses-30215529919/   (local mirror)

`backups` is in `PROTECTED_PREFIXES`, so `r2-cleanup.yml` cannot reach it. The
durable fix -- exporting those 581 records into the R2 construction namespace so
the item 6 path works -- is recorded, not scheduled. Note the same class of
problem applies to `cv1-control` on **every** construction run: 30-day
retention, so today's Places run lapses 2026-09-01.

### Wave C's falsifiable prediction is REFUTED

Wave C predicted head-path gold cases **0/11 -> 6/11** after the reorder at cap
10. Measured live against `2026-08-02.0`
(`benchmarks/2026-08-02-forward-gold-after-planet-rebuild.json`, 35 cases, vs
the 2026-08-01 baseline on the prior build):

| group | n | old r@1 | new r@1 | old mrr | new mrr |
|---|---|---|---|---|---|
| self_recall (all place) | 35 | 0.314 | **0.400** | 0.343 | 0.443 |
| `place:name` (head path) | 10 | 0.000 | **0.100** | 0.000 | 0.150 |
| `place:name_locality` | 10 | 0.200 | **0.400** | 0.250 | 0.450 |
| `place:seam` | 10 | 0.900 | 0.900 | 0.950 | 0.950 |
| `place:inverse_seam` | 5 | 0.000 | 0.000 | 0.000 | 0.000 |

**Head path went 0/10 -> 1/10 at r@1 (2/10 at r@5), not 6/11.** The rebuild is
not a null result -- overall place recall rose 27% relative and the routed
`name_locality` path doubled -- but the mechanism Wave C claimed for the head
path did not materialise. Bare name-only landmark queries still return zero on
the live build: Eiffel Tower, Statue of Liberty, Tokyo Tower, Musee du Louvre,
Big Ben all n=0; only Colosseum resolves.

**Name-only global retrieval is therefore still the open gate**, and it is now
the measured blocker for the forward-search-correctness milestone.

### Head saturated-posting correction, 2026-08-02

The remaining two-token failure is in the Worker merge, not evidence that the
planet data needs another ordering pass. Each global-head token posting is
independently capped at 10. The old `intersect_ranked` path treated absence from
that lossy list as authoritative, so `q=Eiffel Tower` discarded the Eiffel Tower
when the selective `eiffel` posting retrieved it but the saturated `tower`
posting had evicted it. The record's stored primary name proves both tokens.

The routed lane already implements the required distinction: absence from a
short posting is authoritative, while absence from a full posting may be
eviction and is recoverable only when stored display fields prove the missing
token. The local correction makes both lanes call one bounded-posting merge,
with their existing caps unchanged (routed 256, head 10). It preserves direct
posting evidence, source-locator identity, field-mask union, deterministic
producer ordering, and fail-closed cap/cardinality checks.

Pre-merge verification:

- focused `Eiffel Tower` regression: saturated ten-row absence recovers the
  target; nine-row unsaturated absence remains an empty result;
- Worker suite: 197 passed, 4 ignored; `wasm32-unknown-unknown` check green;
- Python Places contract suite under the frozen Python 3.12.12 environment:
  58 passed;
- full Python CI suite under Python 3.11.14: 1,609 passed, 2 skipped;
- real Monaco construction slice: all five phases green, 38,182 source records,
  16/16 populated head shards, `reconciles=true`.

PR #223 landed as `3d3b33c`; CI run `30761370438` and automatic Worker deploy
run `30761582221` both passed, including post-deploy smoke. The unchanged gold
set was then run against live build `2026-08-02.0`; durable evidence is
`benchmarks/2026-08-02-forward-gold-after-head-saturation.json`.

| group | n | before r@1 | after r@1 | before r@10 | after r@10 | before mrr | after mrr |
|---|---|---|---|---|---|---|---|
| self_recall (all place) | 35 | 0.400 | **0.429** | 0.486 | **0.571** | 0.443 | **0.495** |
| `place:name` (head path) | 10 | 0.100 | **0.200** | 0.200 | **0.500** | 0.150 | **0.333** |
| `place:name_locality` | 10 | 0.400 | 0.400 | 0.500 | 0.500 | 0.450 | 0.450 |
| `place:seam` | 10 | 0.900 | 0.900 | 1.000 | 1.000 | 0.950 | 0.950 |

The exact recovered name-only cases are Eiffel Tower (rank 2), Taj Mahal
(rank 3), and Buckingham Palace (rank 1). No gold stratum regressed, errors
remained zero, type starvation fell 13 -> 8 across the 35 place cases, and the
data version remained `2026-08-02.0`; this was a Worker-only change.

**RC2 is closed.** The saturated-posting mechanism was real, the targeted
Eiffel Tower case now resolves, and three exact IDs crossed the retrieval gate.
The remaining misses were then separated by the probe below; the earlier
two-RC3/three-two-token split was only a query-shape hypothesis.

### RC3 and remaining admission split, 2026-08-02

The live POI-only probe is
`benchmarks/2026-08-02-head-token-admission-probe.json`. Every constituent
single-token request used `types=poi&limit=10`; all postings returned ten rows,
so absence is producer-cap absence rather than a short authoritative posting.
Two-token projections use the already-live saturated merge to answer whether
any current posting can seed the target.

| gold miss | live posting evidence | classification |
|---|---|---|
| Statue of Liberty | target National Monument is rank 8 for `liberty`, absent from saturated `statue` / `of`; `Statue Liberty` recovers it at rank 3 | RC3 Worker-recoverable |
| Empire State Building | absent from all three token postings; all three two-token projections empty | producer admission, not RC3 alone |
| Big Ben | absent from both postings; pair returns only an Ontario memorial | producer admission |
| Brandenburg Gate | absent from both postings; pair empty | producer admission |
| Machu Picchu | exact-name candidate is already rank 1, 2.291 km from gold | retrieval succeeds; strict spatial-contract miss |

The Statue candidate is named `Statue Of Liberty National Monument`, the name
used by the US National Park Service. The curated case now accepts that official
unit name and records the NPS fact sheet as its source; this is a benchmark
correction independent of the Worker cap.

The local RC3 change raises one shared global-head token cap from two to three
for both construction-v1 and legacy readers. The saturated merge already
handles three postings; the new maximum is three bounded head reads, not an
unbounded fan-out. Four-token queries remain fail-closed. A focused regression
models the live Statue shape: the target appears only in the selective
`liberty` posting and its display name proves the two missing saturated tokens.

An external provider-neutral baseline was also run and saved as
`benchmarks/2026-08-02-forward-gold-external-before-rc3.json` (35 cases, zero
errors). Overall r@10 was Overture 0.571, Nominatim 0.914, Photon 0.943; on the
ten name-only cases it was 0.500 / 0.800 / 0.800. Big Ben missed under the same
semantic gold contract for all three providers, while Machu Picchu missed for
Overture and Nominatim but ranked 2 in Photon. Those cases need entity/gold
review before they are used to justify an admission design.

This was the pre-deploy prediction. The first live result and its newly exposed
interaction are recorded immediately below.

### First RC3 live result and locality interaction, 2026-08-02

PR #225 landed as `8f9a90f`; main CI run `30763429295` and automatic deploy run
`30763644173` passed, including post-deploy smoke. The two falsifiable endpoint
checks behaved exactly as predicted: Statue of Liberty now returns the New York
National Monument at rank 4, while Empire State Building remains empty.

The full gold run
(`benchmarks/2026-08-02-forward-gold-after-rc3.json`) is **not an acceptance
pass**, despite name-only r@10 improving 0.500 -> 0.600 and overall place r@10
improving 0.571 -> 0.600. It found a real interaction:

- overall place r@1 regressed 0.429 -> 0.371;
- `place:name_locality` r@1 regressed 0.400 -> 0.200, though its r@10 stayed
  0.500;
- Taj Mahal Agra fell rank 1 -> 5 and Sagrada Familia Barcelona rank 1 -> 2.

The cause is deterministic. Before RC3, three-token queries returned empty from
the head, which allowed `locality_suffix_candidates` to route the remaining
name near an exact locality centroid. After RC3, a nonempty three-token global
head made the old boolean guard suppress that stronger route. Live metadata
confirmed locality inference was absent for Taj Mahal Agra, Sagrada Familia
Barcelona, and Eiffel Tower Paris.

The local follow-up preserves the original rule for two-token queries. For a
nonempty three-token head it permits locality inference only when the head also
contains an exact primary-name candidate for the prefix: `Taj Mahal` proves the
parse `Taj Mahal` + `Agra`, while no result exactly named `Statue of` means
Liberty, NY cannot steal `Statue of Liberty`. Focused tests pin both sides.

**The acceptance gate was deploy and rerun, not more ranking work.** It required
Statue to remain recovered, Empire to remain empty, the prior name-locality
ranks to return, and no gold stratum to regress, followed by the same
three-provider external comparison. The result is recorded immediately below.

### RC3 accepted result, 2026-08-02

The bounded locality follow-up landed in PR #226 as `e655615`. Main CI run
`30764536307` and automatic Worker deploy run `30764760627` both passed,
including post-deploy verification. The direct live gates all passed: Statue of
Liberty remains rank 4, Empire State Building remains empty, Taj Mahal Agra and
Sagrada Familia Barcelona returned to rank 1 with
`places_locality_inference`, and Eiffel Tower Paris retained the same routed
path.

The accepted exact-ID run is
`benchmarks/2026-08-02-forward-gold-after-rc3-locality-fix.json` (46 total
cases, 35 place self-recall cases, zero errors). Compared with the accepted RC2
baseline:

| group | n | before r@1 | after r@1 | before r@10 | after r@10 | before mrr | after mrr |
|---|---|---|---|---|---|---|---|
| self_recall (all place) | 35 | 0.429 | 0.429 | 0.571 | **0.600** | 0.495 | **0.502** |
| `place:name` (head path) | 10 | 0.200 | 0.200 | 0.500 | **0.600** | 0.333 | **0.358** |
| `place:name_locality` | 10 | 0.400 | 0.400 | 0.500 | 0.500 | 0.450 | 0.450 |
| `place:seam` | 10 | 0.900 | 0.900 | 1.000 | 1.000 | 0.950 | 0.950 |

No gold stratum regressed, type starvation fell 8 -> 7, and the data version
remained `2026-08-02.0`; both RC3 changes were Worker-only.

The accepted provider-neutral rerun is
`benchmarks/2026-08-02-forward-gold-external-after-rc3.json` (35 cases per
provider, zero errors):

| provider | before overall r@10 | after overall r@10 | before name r@10 | after name r@10 |
|---|---|---|---|---|
| Overture | 0.571 | **0.600** | 0.500 | **0.600** |
| Nominatim | 0.914 | 0.914 | 0.800 | 0.800 |
| Photon | 0.943 | 0.943 | 0.800 | 0.800 |

**RC3 is closed.** The final run accepts the one Worker-recoverable miss without
the locality regression seen in the first deployment. The next bounded gate is
case-contract review for Big Ben and Machu Picchu, followed by producer
admission work only for misses that survive that review. Empire State Building
and Brandenburg Gate are already confirmed producer-admission misses by the
live capped-posting probe.

### Big Ben and Machu Picchu contract audit, 2026-08-02

The durable audit is
`benchmarks/2026-08-02-big-ben-machu-picchu-contract-audit.json`. It compares
live top-ten identities and coordinates with UK Parliament, Historic England,
Peru Ministry of Culture, and UNESCO sources that were not derived from a
compared provider.

**Big Ben survives the audit as a producer-admission miss.** UK Parliament
documents Big Ben as the famous Westminster landmark/bell at Elizabeth Tower,
and Historic England's statutory Palace of Westminster entry includes the
tower and its location. The old 2 km case radius was nevertheless too loose:
`Big Ben London` credited a different same-named London POI 1.337 km away at
rank 1 while the actual landmark was rank 3 and 0.002 km away. Both Big Ben
cases now use a case-specific 0.25 km radius. The context-free query still
returns no London target from Overture, Nominatim, or Photon, while all three
locality controls retrieve the landmark. The global Overture miss is therefore
real despite also exposing a weakness shared by the external providers.

**Machu Picchu does not survive as a producer-admission miss.** Peru's Ministry
of Culture uses `Ciudadela Inka (Llaqta) de Machupicchu`, `ciudadela de Machu
Picchu`, and `Santuario Histórico de Machu Picchu`; UNESCO uses `Historic
Sanctuary of Machu Picchu`. Those official aliases are now accepted. The
Overture exact-name rank-1 point remains correctly rejected at 2.291 km from
gold, while `Ciudadela De Machu Picchu` is accepted at rank 10 and 0.355 km.
`Machu Picchu Cusco` still misses because it returns a museum in Cusco city;
that is a separate region-context routing issue, not evidence for posting
admission.

The unchanged live service was rerun after the contract correction. Evidence is
`benchmarks/2026-08-02-forward-gold-after-contract-audit.json` and
`benchmarks/2026-08-02-forward-gold-external-after-contract-audit.json` (zero
errors):

| provider | before r@1 | audited r@1 | before r@10 | audited r@10 |
|---|---|---|---|---|
| Overture | 0.429 | 0.400 | 0.600 | **0.629** |
| Nominatim | 0.914 | **0.971** | 0.914 | **0.971** |
| Photon | 0.771 | **0.800** | 0.943 | 0.943 |

Overture's r@1 change is the intentional removal of the Big Ben false positive,
not a serving regression; its name-only r@10 improves 0.600 -> 0.700 by
crediting the already-present citadel. Focused benchmark regressions pin both
contract decisions.

**The audit gate is closed.** Proceed only with bounded producer admission for
Empire State Building, Big Ben, and Brandenburg Gate. Do not include Machu
Picchu in that mechanism.

The bounded `tower` proxy probe in `b8e3151` also needs to be read narrowly. It
refutes record count, `names.common` cardinality, and `sources` count as direct
orderings for four famous towers versus the ten measured incumbents. It does
not prove that no entity-fame signal exists anywhere in Overture.

### Upstream Places category migration gate, 2026-08-02

Durable audit:
`benchmarks/2026-08-02-overture-category-migration-audit.json`.

Overture's July 2026 release notes repeat a concrete deprecation deadline:
`categories` will be removed in the September 2026 release and replaced by
`basic_category` plus `taxonomy`. The strict construction-v1 source contract is
not compatible with that removal. `places_inventory_v1.py` requires
`categories.primary` and `categories.alternate`, projects no `taxonomy` root,
and `project_places_construction_v1.py` computes both the displayed category
and `prominence_rank` from those legacy fields. A September-or-later inventory
therefore fails closed at schema inspection. Published artifacts are
self-contained and unaffected, and the frozen June evidence remains true for
the source it actually measured.

This is not a field-only rename. The July Parquet schema was checked directly:
the replacement is `taxonomy.primary`, `taxonomy.hierarchy`, and
`taxonomy.alternates` (**plural**, unlike legacy `categories.alternate`). In a
bounded one-million-row compatibility probe, 33.8% of old/new primary labels
differed, 3.4% lacked `taxonomy`, 7.1% lacked a non-empty `basic_category`, and
73.2% carried legacy alternates while zero sampled rows carried
`taxonomy.alternates`. The sample is not a geography-balanced quality set, but
it decisively refutes mechanical substitution. The new `hierarchy` also carries
general query terms such as `restaurant` that the current single-category
projection does not preserve.

**Gate:** before constructing Places from the September 2026 release or later,
add a new source-schema/evidence generation that requires the new taxonomy,
separates display category from searchable hierarchy terms, and recalibrates
the prominence table against the renamed/repathed taxonomy. Retain validation
for the frozen legacy inventory; do not rewrite its evidence or pins. Accept
the migration only against a provider-neutral, category-stratified benchmark
covering restaurants and other everyday POIs as well as landmarks.

This is **P0 for the next source upgrade**, not a reason to interrupt the
current live-artifact producer-admission investigation. If producer admission
itself requires a fresh post-August source build, this gate moves ahead of it.

**Compatibility implementation, 2026-08-02:** the code side of this gate is
now complete. `places_inventory_v1.py` carries a second, taxonomy-native schema
contract while preserving validation and fingerprints for the frozen legacy
contract. Auto mode keeps selecting legacy while `categories` exists; an
explicit taxonomy profile permits a deliberate compatibility build against the
July dual-field release and becomes the automatic path once `categories` is
removed. The taxonomy physical projection is generation v2: display remains
one `taxonomy.primary` value (falling back to `basic_category`), while the
primary, basic, hierarchy, and alternate terms are separately searchable under
the existing category field mask. Legacy projection bytes and transform
semantics remain unchanged.

The bounded July recalibration found all 8,854 sampled legacy
`landmark_and_historical_building` primaries mapped to `historic_site`; the new
label therefore inherits the same deliberately weak 0.35 prior. Specific
museum, monument, castle, library, and train-station labels remained usable,
and hierarchy ancestors now generalize categories such as specialty museums
and specific restaurant cuisines. Commodity hierarchy terms remain
dispositive, so a restaurant cannot gain landmark prominence from an alternate.

The provider-neutral gold set is expanded from 35 to 55 cases with 20 named
everyday POIs across food/drink, retail, lodging, healthcare, and civic/transit,
including three explicit brand-branch queries. Evidence:
`benchmarks/2026-08-02-forward-gold-external-poi-expanded.json`. On the new
named-POI stratum, rank-10 recall is Overture 0.353 versus Nominatim 0.765 and
Photon 0.765; on brand branches it is 0.000 / 0.667 / 1.000. This confirms a
large ordinary-POI retrieval gap independent of the landmark audit. Generic
origin-dependent intents such as “restaurants near me” remain a separate
semantic benchmark because exact-place name/coordinate scoring cannot judge
their relevance honestly.

**Real taxonomy fast loop, 2026-08-02:** the explicit taxonomy profile now runs
through the same real Monaco inventory and all five construction phases used by
the slice smoke. Against the exact same July object, row groups 66-67, and
38,182 input rows, both legacy and taxonomy runs reconciled 21 serving objects.
Taxonomy emitted 338,950 input term rows versus 261,144 (+29.79%) and
86,589,934 total staged/serving bytes versus 76,537,910 (+13.13%); head records
moved only 69,069 -> 70,124 (+1.53%). Durable evidence is
`benchmarks/2026-08-02-taxonomy-slice-comparison-v1.json`. The pull-request
slice smoke now forces `--schema-profile taxonomy` and asserts the selected
profile, so July's still-present legacy field can no longer make the migration
path look green accidentally.

The required scale jump then repeated the exact legacy/taxonomy comparison in
New York, Tokyo, Lagos, Sao Paulo, and Sydney. All twelve runs reconciled. Across
six continents and 225,521 identical admitted rows, taxonomy added 23.69% input
term rows, 9.85% total bytes, and 1.32% head records. Total-byte growth ranged
from 4.82% in Tokyo to 18.61% in Sao Paulo, decisively showing that Monaco's
13.13% is neither noise nor a safe linear planet projection. Durable evidence:
`benchmarks/2026-08-02-taxonomy-multiregion-comparison-v1.json`.

**Remaining taxonomy gate:** the code and real-slice compatibility rungs are
closed. Do not call a post-August source upgrade ready until a formal new
evidence generation regenerates the global inventory, readiness, scale
projection, and control pins. The six-region aggregate materially narrows the
risk, but it is not a formal planet projection. The current live June artifacts
and their frozen evidence remain unaffected.

### Upstream shared-source migration audit, 2026-08-02

Durable audit:
`benchmarks/2026-08-02-overture-september-cross-theme-audit.json`.

The Places `categories` removal is the only September breaking change currently
confirmed in Overture's published release notes. There is also a second,
cross-theme change announced in merged schema PR #535: the rough September plan
makes `sources[].provider`, `resource`, and source `version` required while
making `dataset` optional and deprecated; `dataset` removal is planned for
March 2027 or later. Treat that distinction honestly: the shared-source step is
in a merged plan, but as of this audit it is not in the July release notes and
has no separate merged breaking-change PR. The upstream `vnext` branch is
identical to `main`, and repository searches found no third September change.

The source migration applies schema-wide to all 15 released feature types in
addresses, base, buildings, divisions, places, and transportation. It is not
yet represented physically in July data: a bounded first-object/first-row-group
read of every type found the same old seven fields (`property`, `dataset`,
`license`, `record_id`, `update_time`, `confidence`, `between`), with zero types
carrying `provider`, `resource`, or source `version`. All 15 samples populated
`dataset`. Therefore August is a required re-audit point, not an assumed bridge.

**Construction impact:** no active construction-v1 source contract or mapper
selects `sources`, and the division build does not use source identity, so this
does not block the Places/Addresses build or invalidate published artifacts.
The SQL exports that preserve the whole struct remain compatible through the
September optionality step. Four provenance-oriented research tools still use
`dataset` as identity: `experiment_current_release_addresses.py`,
`benchmark_address_street.py`, `benchmark_places_sampling.py`, and
`benchmark_transport_components.py`. Before their first post-August rerun they
must prefer the complete `(provider, resource, version)` tuple and retain
`dataset` only as a legacy fallback. Do not add provenance columns to the hot
construction projection merely to solve that research-tool migration.

### Bounded entity-phrase admission and expanded POI stage audit, 2026-08-03

Durable evidence:

- `benchmarks/2026-08-03-poi-admission-audit-v1.json`;
- `benchmarks/2026-08-03-entity-phrase-scale-v1.json`;
- `benchmarks/2026-08-03-entity-phrase-monaco-slice-v1.json`; and
- `benchmarks/2026-08-03-forward-gold-external-poi-expanded-audited.json`.

The 23-case stage audit combines an exact-release, gold-radius source read with
the live context-free query and explicit-proximity controls. It changes the
three-landmark conclusion:

- **Big Ben and Empire State Building are true global-head admission misses.**
  The June source carries exact primary-name records at the gold points;
  explicit-proximity forward search retrieves them at ranks 3 and 1; and the
  new phrase contract has one and two eligible source records respectively.
- **Brandenburg Gate is not the same failure.** The four nearby accepted source
  matches are named `Brandenburger Tor` or `Puerta de Brandeburgo`; no projected
  primary/brand field carries `Brandenburg Gate`. The English explicit-proximity
  control returns same-named records 25.8 km away, while `Brandenburger Tor`
  still does not admit the canonical record to the routed top ten. A phrase key
  cannot manufacture the missing English alias. Track this as source alias /
  tokenization plus routed-cap work, not as evidence for the global phrase lane.

The implementation emits at most one synthetic `e2:` or `e3:` key for a
two- or three-word **primary name** whose existing `prominence_rank` is nonzero.
Exact phrase equality is the identity evidence; the category prior is only the
hard admission budget. The keys participate in the existing per-task/global
top-10 and additive-digest proofs, are excluded from routed serving artifacts,
and are advertised by the head manifest as
`prominence-primary-name-v1`. The Worker probes the phrase shard first only when
that capability is present, validates every returned record against its stored
primary name, and otherwise uses the existing bounded token merge. Old releases
therefore pay no extra read and keep their exact behavior.

The scale result is bounded enough to continue and broad admission is not.
Across seven metros on six continents, 1,020,847 admitted rows produce 30,231
retained prominence-gated phrase rows (**2.961%** of source rows). Emitting the
same lane for every named POI would produce 526,689 (**51.593%**) and is rejected.
A directional ratio-to-planet projection is 2.24M rows / 373 MB, about 7.3% of
the current 5.14 GB head; it is not a formal planet projection.

The real Monaco taxonomy fast loop ran all five phases on the same 38,182 rows
as the 2026-08-02 comparison and reconciled. It added exactly 927 term rows and
927 head rows; total local store bytes grew 0.615%. All 927 phrase rows reached
the head, no `e2:`/`e3:` key entered a routed artifact, the manifest advertised
the capability, and the finalizer wrote its marker last.

The expanded ordinary-POI audit also prevents mis-scoping the next fix. Of 20
restaurant/retail/lodging/healthcare/civic cases, seven already retrieve. Of
the remaining thirteen, eleven retrieve the correct target through either the
original query with explicit proximity or an accepted-name proximity control;
only Raffles Singapore and Mayo Clinic fail every such routed control. Most of
the ordinary-POI gap is therefore locality placement, branch/alias expression,
or query tokenization -- not evidence for a planet-wide all-POI head lane.

One gold defect was corrected before accepting those counts: the live top-1
`Hotel Sacher Vienna` result is the intended property 0.032 km from gold, but
the case accepted only `Hotel Sacher` / `Hotel Sacher Wien`. The English city
form is now accepted and regression-tested. On the unchanged live service, the
audited named-POI r@10 moves 0.353 -> **0.412** and overall r@10 0.509 ->
**0.527**. This is a contract correction, not a serving improvement.

**The changed producer is deployed as build `2026-08-03.0`.** Its code,
real-slice rung, formal v4 inventory/readiness/scale evidence, control pins,
planet build, preview acceptance, operation-preserving publication, and catalog
promotion are complete. See "v4 production promotion, 2026-08-04". If a later
build moves to a post-August Overture release, the taxonomy source gate above
runs first. Do not widen phrase admission to commodity POIs; keep the remaining
ordinary routing/alias cases on their separate Worker and external-data tracks.

### Independent audit closure and v4 decision, 2026-08-03

Canonical external review:
`docs/plans/2026-08-03-claude-audit-feedback.md`. It is preserved as the
auditor's final report; the decisions below incorporate it without rewriting
that evidence.

**Production availability is closed.** The intermittent root `1101` came from
Worker version `786a3aad-4ea5-4f7f-878d-b9dbff8f129d` (git `05ac13b`); a
Cloudflare tail captured the runtime cancelling the request as hung. Redeploying
the identical commit produced version `b923d11d-d504-48ba-9aab-75897eaf2f3d`.
The root then passed 50/50 requests, Sydney/Seattle/Singapore/Paris passed 40/40,
and the tail stayed clean. Durable evidence is
`benchmarks/2026-08-03-worker-startup-incident.json`. Deployment now requires
15 repeated root probes, contract-tested by
`tests/test_deploy_workflow_contract.py`; a one-shot root smoke is not an
adequate startup gate.

**The bounded Worker correctness pass is implemented.** Candidate scoring now
happens before the final result cap. Place reranking applies the audited A2+B+C
contract: partial multi-token name match quality, exact/qualified full-name
quality, and bounded category priors. Locality suffix candidates cover two
through six tokens. The entity-phrase lane remains fail-closed: positive
prominence, name field mask, and exact normalized primary-name equality are all
required; an empty or invalid phrase result falls through to the ordinary path.
The first complete v4 preview refuted the earlier terminal-fast-path decision.
A valid phrase posting is now additive evidence: a three-token query reads the
full phrase, its exact two-token prefix, and the three ordinary postings, then
deduplicates identical source rows. This is bounded at five head reads and 50
pre-score candidates. It lets a selective ordinary posting recover a canonical
feature evicted from a saturated phrase posting, and lets an exact prefix feed
the already-bounded locality-suffix path. A saturated full phrase reserves one
slot only when a distinct ordinary candidate has strictly greater producer
prominence than its weakest row. This measured Statue guard retains nine exact
phrase results while admitting the canonical longer official name; equal or
weaker ordinary evidence cannot evict a phrase row.

The gold contract now records point-vs-extent tolerance explicitly, accepts the
official Machu Picchu extent case without loosening Big Ben, and reports paired
McNemar evidence. The planet phrase probe is decisive before construction:
`benchmarks/2026-08-03-entity-phrase-planet-gold-v1.json` finds 117 exact
normalized Big Ben records with four prominence-gated records (canonical rank
3), and seven Empire State Building records with five gated records (canonical
rank 2). Both canonical records survive cap 10.

**The v4 construction evidence gate is green.** Contract
`benchmarks/places-construction-v1-evidence-spec-v4.json` has sha256
`77bce6209c9c98ee4243167982fe11b13f7702c042e48bfad90daa6b3b26bfed`.
All 12 projections and censuses and all seven role task runs were regenerated
from public source data. The repeated functional rehearsal produced 4,419,976
head records, 2,349,588 head index entries, and 70,166 phrase rows; all outputs
reconciled, the Worker decoder succeeded, and zero phrase keys entered routed
artifacts. Head bytes are 773,590,640 against an 805,306,368-byte 25%-headroom
threshold. The first rehearsal's task-85 RSS result failed the reserve rule and
is recorded but not admitted; the fresh repeat passed every resource gate.
Durable evidence:

- `benchmarks/places-construction-v1-data/evidence/host-provenance-v4.json`;
- `benchmarks/places-construction-v1-data/evidence/rehearsal-v4.json`;
- `benchmarks/places-construction-v1-data/evidence/scale-evidence-v4.json`; and
- `benchmarks/places-construction-v1-data/evidence/readiness-v4.json` (`ready=true`).

The control plane and slice workflow now bind v4 and its exact hashes. This
closes the review's v2/v3 inconsistency. The **Places-only, non-promoting planet
build** is run `30799029151` from exact producer
`ad3ff5de72d20f7c8c52707ac08d624eed037cc7`. Admission, binaries, all 89 maps,
planning, and 127 of 128 first-attempt reducers passed. GitHub closed batch 30
after 46 minutes while its reducer step still reported `in_progress`; no post
step ran and the finalized job still had no log blob, confirming runner loss
rather than a reducer error.

A failed-jobs rerun preserved the request-scoped R2 namespace and produced a
successful batch 30. GitHub's matrix rerun semantics also repeated the other
127 reducers, so the completed run now has exactly 128 unique durable reducer
artifacts and attempt 2 has at least one successful job for every expected
batch. Its attempt-scoped history additionally exposes four stale queued
batch-30 records even though the run is completed; recovery therefore
authenticates the 128 unique successful expected jobs from the final attempt,
not raw job-row count or `run_attempt == 1`.

The rerun reached the global head and established the next measured blocker. At
79% through the first bounded tree-merge query DuckDB failed to offload another
256 KiB because the configured half-share spill limit was full: 8.4 GiB used
against 8.5 GiB / 9,126,805,504 bytes. This is not an RSS, reducer, R2, candidate
admission, or runner-ENOSPC failure. No head result, final object, or completion
marker was published.

The scoped `head_only_resume` recovery from run `30799029151` completed while
keeping the request-pinned producer and all immutable staged rows, keys,
ordering, and binaries. The workflow raised only this head's DuckDB spill
allowance to three quarters of the admitted scratch cap: 13,690,208,256 of
18,253,611,008 bytes. The independent whole-stage 17 GiB scratch watchdog and
the runner's 25,000,000-KiB free-disk floor remained unchanged. The workflow
proved both the producer's quarter-share default and the effective
three-quarter value before starting the expensive statement. The completed
candidate then entered the preview gate below and was promoted after that gate
passed; see "v4 production promotion, 2026-08-04".

**The head-only recovery and first complete preview are finished.** Preview run
`30865331854` deployed the run-scoped Worker, passed the Big Ben and Empire State
direct gates, completed both semantic batches, failed only the strict paired
no-loss gate, and cleaned up its Worker and catalog without touching production.
Against the 55-case audited baseline, candidate recall@1 was 29/55 versus 26/55
(four gains, one loss), recall@10 was 37/55 versus 34/55 (five gains, two
losses), and MRR was 0.580. Against the 200-case everyday baseline, recall@1 was
65/200 versus 62/200 (three gains, zero losses) and recall@10 was 66/200 versus
65/200 (one gain, zero losses).

The exact gold losses were `Statue of Liberty` (baseline rank 4 -> absent) and
`Union Station Toronto` (baseline rank 1 -> absent). Both canonical source rows
exist. Immutable candidate-shard inspection showed why neither needs another
planet build: the terminal `e3:statue of liberty` posting contains ten literal
replicas while the canonical National Park Service row remains retrievable from
the selective ordinary `liberty` posting; `e3:union station toronto` returns
literal variants while the official-name Toronto station is present in the
exact `e2:union station` prefix posting. The Worker returned immediately on the
non-empty full phrase and suppressed both recovery paths.

The additive composition correction preserves full phrase, optional exact
prefix, and ordinary results in that order; deduplication uses feature identity
plus source locator, preserving intentional duplicate UUID rows at different
source positions. Producer caps fail closed. A read-only local R2 proof fetched
the ten content-addressed candidate shards: all filenames matched their SHA-256,
the real Worker decoder resolved nine non-empty token/phrase postings and
rejected all nine deliberate misroutes, and 22 phrase rows passed the exact
contract. The `e2:statue of` posting was correctly empty; the Statue recovery
comes from ordinary `liberty`.

The first additive-composition repeat was run `30867768153` from merged commit
`cf12f0e`. It passed both direct phrase gates and fixed Union Station Toronto.
Gold recall@1 improved to 30/55 with four gains and zero losses; recall@10
improved to 38/55 with five gains and one loss. Everyday POI remained 65/200 at
rank 1 and 66/200 at rank 10 with zero paired losses. The sole remaining loss
was Statue of Liberty. Its composed candidate set did contain the canonical NPS
row, but all ten exact-name replicas scored ahead of the longer official name
and exhausted the public result cap. The second correction therefore removes
only the weakest full-phrase row when the phrase posting is saturated and a
distinct ordinary candidate has strictly greater producer prominence. On the
immutable shard the canonical NPS row has prominence 255 and the weakest phrase
replica has prominence 89.

The final zero-error repeat was run `30870735398` from merged commit `01856db`.
It passed both direct phrase gates and every acceptance and cleanup step. Gold
recall@1 improved from 26/55 to 30/55 with four gains and zero losses; recall@10
improved from 34/55 to 39/55 with five gains and zero losses. Everyday-POI
recall@1 improved from 62/200 to 65/200 with three gains and zero losses;
recall@10 improved from 65/200 to 66/200 with one gain and zero losses. Big Ben
and Empire State Building remained present, and both Statue of Liberty and
Union Station Toronto were recovered. Run `30870174744` had already produced
the same semantic result but was correctly rejected by the zero-error gate for
one timeout and two HTTP 500 responses; the accepted repeat had none. The
candidate slice was admitted and then published. The preview overlay
intentionally omitted external reverse operations, so production publication
must explicitly preserve the already-live reverse capabilities rather than
promoting that preview document verbatim.

### v4 production promotion, 2026-08-04

**The accepted v4 candidate is live as build `2026-08-03.0`.** Publication used
an operation-preserving overlay rather than the preview release document or the
single-family publisher, either of which would have omitted live reverse and/or
Address capabilities.

| stage | run | result |
|---|---|---|
| request-matched Places reverse | `30871526883` | 16/16 ranges green; 75,631,061 records, 16,511 cells, 16,528 artifacts / 8,397,603,739 bytes; marker-last catalog green |
| overlay dry-run | `30874752691` | exact main `93f63e6`; retained production operation graph; release sha256 `9fdc9eb1585ad61d45d5d6ce523122065bc8b9c718ed8e6128f1ea00d92d528b` |
| overlay execute | `30874857103` | create-only `v2/releases/2026-08-03.0/release.json`; R2 read-back byte-identical to dry-run |
| catalog promotion | `30874928696` | CAS `0457d57f...04cf9ef` -> `fc624cd3...d9dd64e3`; all production smokes green on attempt 1/10 |

The release retains exactly the prior operation set: ID lookup; division and
Places forward; Address, division, and Places reverse; and structured Address
forward. The retained Address manifest is byte-identical to production. Places
forward comes from `slice-2026-08-03.0`; its request-matched reverse comes from
`slice-2026-08-04.0` under construction request
`821cff0bcedefd02dbbefa01cd0758ac027dfe90aa047a85ac74cabfd52dd128`.

The enhanced post-CAS smoke passed Berlin divisions, context-free IKEA,
locality-routed Eiffel Tower Paris, structured 400 BROAD Street, Space Needle
POI reverse, and 400 BROAD Street Address reverse on its first attempt.
Independent live probes additionally recovered Big Ben, Empire State Building,
the canonical NPS Statue Of Liberty National Monument row, and Union Station
Toronto while both reverse fixtures remained present. Every response advertised
`data_version.geocoder_build = 2026-08-03.0`. The production Worker remains
exact phrase commit `01856db`, deployed by run `30869980540` as version
`d140ad74-251c-4b2f-a5ad-88db43d1c1b1`.

**The Worker pass is deployed and measured.** PR #232 merged as `6e9dfda` and
deploy run `30796985582` published Worker version
`817ffd86-4dba-4502-9ef0-27e143d024d4`. The strengthened gate passed on attempt
1: 15 consecutive root starts plus health, search, reverse, and ID checks.

The paired 55-case semantic run is
`benchmarks/2026-08-03-forward-gold-external-after-a2bc.json`. Against the
audited pre-change baseline, Overture moved:

| metric | before | after | paired flips | exact p |
|---|---:|---:|---:|---:|
| recall@1 | 0.345 (19/55) | **0.473 (26/55)** | **7 gains / 0 losses** | **0.015625** |
| recall@10 | 0.527 (29/55) | **0.618 (34/55)** | 5 gains / 0 losses | 0.0625 |
| MRR | 0.412 | **0.521** | — | — |
| type-starved | 14 | **12** | — | — |

This is a statistically detectable rank@1 result under the audit's paired gate;
the rank@10 movement is positive and regression-free but remains just outside
that gate. `name` rank@1 moved 2/10 -> 5/10, `name_locality` rank@1 moved 3/10
-> 6/10 and rank@10 5/10 -> 8/10, and `named_poi` rank@10 moved 7/17 -> 9/17.
The seam stratum had zero flips, and no reportable stratum regressed. Nominatim
and Photon both remain at 0.891 rank@10 on this cross-corpus set.

Do not attribute the remaining context-free Big Ben or Empire State Building
misses to this Worker pass: both still miss without locality and await the v4
phrase-admission build. The live gains are the composite result of query-time
scoring, primary-category correction, score-before-truncate, and locality
routing; this run does not attribute individual flips among those rules. The
benchmark harness now permits a like-for-like semantic baseline under
multi-provider mode and persists paired McNemar counts for Overture while
excluding external rows.

**The category audit changes both spellings and scope, but stays bounded.** The
planet scan found 2,261 distinct category strings across 228,421,560 values.
Observed commodity spellings include `atms`, `banks`, `bank_credit_union`,
`bank_or_credit_union`, `laundromat`, `laundry_service`, and
`laundry_services`; the old aliases remain harmless compatibility entries.
`atm` is real in `categories.basic`, so the earlier claim that ATM demotion
never fired is refuted. `stadium` is absent and `stadium_arena` is real.
`opera_and_ballet` and `hospital` now receive bounded landmark priors. Evidence:
`benchmarks/2026-08-03-places-category-contract-audit-v1.json`. No broader POI
admission is authorized by this result.

**Aliases are a separate future track.** Planet measurement finds
`names.common` empty and `names.rules` populated away from the landmark classes;
none of the seven landmark gold entities has a useful rules alias. Exact
URL/social clustering is either unsafe or negligible and is deferred. The
durable sidecar direction is fame first: a stable QID-keyed table plus a durable
GERS-to-QID match ledger and release-attested membership/deltas, with audited
match precision as the Phase 0 go/no-go. Since this work has consumed evidence
generation v4, a sidecar that
changes projection identity must start at **v5**, not reuse the audit's
provisional v4 label. Everyday-POI benchmark expansion precedes any commitment
beyond sidecar Phase 0.

### Sidecar Phase 0 identity result and everyday-POI tripwire, 2026-08-03

Durable plan:
`docs/plans/2026-08-03-sidecar-phase0-and-everyday-poi.md`. Contracts and
evidence:

- `benchmarks/gers-qid-sidecar-phase0-spec-v1.json`;
- `benchmarks/2026-08-03-gers-place-stability-v1.json`;
- `benchmarks/everyday-poi-tripwire-spec-v1.json`; and
- `benchmarks/everyday-poi-source-plan-v1.json`;
- `benchmarks/everyday-poi-source-snapshots-v1.json`;
- `benchmarks/everyday-poi-tripwire-cases-v1.json`; and
- `benchmarks/everyday-poi-tripwire-readiness-v1.json`.

The audit report's “release-scoped GERS-to-QID match table” is corrected.
Overture intends GERS IDs to be stable, so accepted GERS-to-QID associations are
durable and surviving unchanged IDs are not rematched each month. What is
release-scoped is the membership/delta/attestation used to identify added,
removed, data-changed, and reassigned exceptions.

The public-data probe joined the exact same `(dataset, record_id)` across June
and July for all nine Places bridge datasets present in both releases. Of
81,224,863 comparable persistent source records, 76,440,029 kept the same GERS
ID and 4,784,834 moved to another GERS ID: **94.109151% stable** at this source
assignment layer, with zero ambiguous keys. This is not an entity-level GERS
survival estimate: merge/split and conflation changes can move a source record
between GERS entities. It does prove that the exception path is material and a
blind unversioned carry-forward is unsafe. Krick exposed a separate limitation:
it regenerated every observed source record ID, leaving no comparable keys.

The first deterministic candidate set is now complete. A frozen 1,000-binding
Wikidata P1968 snapshot joined through Overture `2026-06-17.0`'s public
Foursquare bridge to 344 rows, 343 GERS IDs, and 344 QIDs. Of 344 candidates,
342 are unambiguous provisional direct-ID accepts; one GERS maps to two QIDs and
both candidates fail into review. The 200-row risk-first queue includes both
conflicts, every missing/over-gate distance observation, 134 label-mismatch
cases, and 50 clean controls. Evidence is
`benchmarks/2026-08-03-sidecar-phase0-foursquare-collection-v1.json`,
`benchmarks/2026-08-03-sidecar-phase0-candidates-v1.json`, and
`benchmarks/2026-08-03-sidecar-phase0-review-queue-v1.json`, with normalized
source rows hash-bound beside them.

Phase 0 still requires at least 200 independently hand-checked decisions with
zero false provisional accepts and measured mapping bytes/resident join memory.
Only direct external IDs may auto-accept; fuzzy candidates require review, and
no unreviewed match may affect prominence. This phase cannot alter construction
output.

The frozen everyday-POI fast loop targets 200 cases from eight government
sources and four macroregions, all outside Europe/North America, with 100
non-Latin cases and five families: civic/transit, food/drink, healthcare,
lodging, and retail. Selection is deterministic from source records before any
provider request. The validator enforces independent provenance and excludes
Nominatim/Photon for any gold later found to be OSM-derived. Collection batches
1-4 froze all 200 cases with zero provider requests: 20 Singapore rail stations,
30 Tainan food/drink records, 20 Hong Kong Hospital Authority facilities, 20
active Seoul hospitals, 20 Bogotá public healthcare facilities, 25 Melbourne
physical-retail establishments, 30 Shinjuku clinics, and 35 Ciudad de México
lodging/retail establishments. The current Taiwan feed has no
Taipei records, so the source filter moved to five Tainan core districts rather
than silently filling the quota from another provider. Hong Kong contributes
official bilingual WGS84 points with stable `OBJECTID`s. Seoul's full 929-row
licensing preview is parsed as data rather than evaluated, filtered on explicit
active/open status, and transformed from EPSG:5174 with pinned pyproj 3.7.2 /
PROJ 9.5.1 while retaining the original coordinates and operation contract.
The readiness report is now green with no blockers: 200/200 cases, 100/80
non-Latin cases, eight/eight countries, five/five families, and all four
macroregions. Bogotá uses stable district-health `ID` values from a frozen
117-feature layer (116 valid points, 114 inside the dense bounds). Melbourne
filters its frozen 19,672-record 2024 snapshot to ANZSIC physical-retail
divisions 39-42, excludes duplicate official names, and records one reviewed
storage-only rejection before admission. Japan deliberately pivots away from
the planned 2020 MLIT source because that catalog explicitly includes suspended
facilities; the current Shinjuku standard dataset provides 695 unique IDs and
valid points. Mexico uses the corrected DENUE 05/2026 bulk file with 462,732
unique `id` and `CLEE` values, fixed-establishment and SCIAN family filters,
and retained methodology/correction evidence.

The first provider-neutral measurement is retained as
`benchmarks/2026-08-03-everyday-poi-external-baseline-v1.json` (sha256
`8f9b9fd4a0e0ec575f32bfedd69dbf8ea80bb4bb6e21f8feb6179e0b5730c255`). It ran
all 200 cases against live Overture build `2026-08-02.0`, Nominatim, and Photon:
600 supported requests, 600 HTTP 200 responses, and zero harness errors.
Overture measured 31.0% recall@1 and 32.5% recall@10, ahead of Nominatim at
10.5%/11.0% and Photon at 10.0%/11.5%. Overture's recall@10 by family was 25.0%
civic/transit, 73.3% food/drink, 40.0% healthcare, 0% lodging, and 5.0% retail.
By macroregion it was 58.0% East Asia, 0% Latin America, 8.0% Oceania, and 25.0%
Southeast Asia. Of the 200 paired cases, all providers missed 122, Overture alone
hit 54, all three hit 11, and an external provider hit while Overture missed 13.
The Overture result was POI-type-starved on 114 cases, so the leading signal is
missing admission/routing coverage rather than a small within-top-10 rank issue.
Treat this live-build result as the pre-preview baseline and rerun the identical
case file against the completed v4 preview before choosing a targeted admission
change or a larger decision set. The sidecar Phase 0 audit remains separate and
incomplete.

The fourth benchmark column is an **OSM presence control**, not another ranked
provider. Evidence is
`benchmarks/2026-08-03-everyday-poi-overpass-presence-v1.json` (sha256
`c66d0446022a9ecc4a64af41c9c20b608a211dc16627771c59d9f043b3b8483e`). Ten
sequential Overpass queries inspected named nodes, ways, and relations within
500 m of every independent authority coordinate. Exact normalized accepted
names are present for 46/200 cases; 12 more are fuzzy review candidates, 134
have only a named same-family feature without an accepted-name match, and eight
have no named same-family candidate. Do not promote the latter 146 cases into
OSM existence claims.

The control isolates 24 Nominatim misses and 24 Photon misses where the exact
accepted name is demonstrably present in current OSM. All 22 Nominatim hits and
22/23 Photon hits align with exact OSM presence. Overture hits 37 cases without
an exact OSM name match and misses 18 with one. This separates upstream
representation from ranked-search behavior while preserving the government
source as gold. Keep Overpass beside the ranked-provider report, but label it
as a presence control because it does not execute the benchmark query or
produce a comparable top-ten ranking.

### Correction: the 2026-07-31 smoke red was TRUE, not false

The 2026-07-31 section below records that promotion's `/v2/forward` smoke as a
"false-red". That is wrong and is corrected here: `q=Eiffel Tower` genuinely
returned zero features until the live RC2 correction above. What was wrong was
gating a *promotion* on a known-open quality gate, not the observation itself.

Commit `1e2c0ab` replaced that assertion with `q=IKEA` (context-free head) and
`q=Eiffel Tower Paris` (locality-routed). That is a reasonable promotion gate,
but **both new assertions also pass on the build being replaced** -- verified
directly against `2026-07-31.0` before the edge cache flipped. The smoke can no
longer detect a head-path regression, by construction. Promotion smoke and
quality measurement are now disjoint: only the gold set speaks to the latter.

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

1. **Promotion smoke fixture — CLOSED 2026-07-31 in `1e2c0ab`.** Run
   `30657619881` had logged `OK divisions` and `FAIL places: no features for
   Eiffel Tower` on 10 of 10 attempts while the promoted build was live and
   correct. The context-free `Eiffel Tower` assertion is replaced by two checks
   that pin *which path* served the request — `q=IKEA` with
   `places_locality_inference` absent for the global head, `q=Eiffel Tower
   Paris` with it present for the located routed path — so neither can pass by
   accident nor absorb the other's regression. Fixing it unmasked a second
   broken fixture: the structured address check omitted the required country
   field and had never actually executed. Verified against live production,
   all four checks pass on attempt 1.
2. **Forward Places quality — BOUNDED V4 MILESTONE MET 2026-08-04.** Stage 1, the
   prominence rebuild, RC2, and RC3 improved distinct strata. Name-only head
   recall@10 is 6/10 under the accepted RC3 contract and 7/10 under the audited
   contract; the extra point is the official Machu Picchu alias, not a Worker
   change. Bounded exact-primary-name admission is implemented and slice-proven
   for the confirmed Empire State Building and Big Ben global-head misses. Its
   planet phrase-posting probe and fresh v4 formal evidence are green. The
   non-promoting Places-only planet build and first preview are complete. The
   preview was net positive but found two exact phrase short-circuit losses.
   Additive composition closed Union Station and the one-slot saturated-phrase
   prominence guard closed Statue; the final preview was accepted with zero
   paired losses and zero request errors. `slice-2026-08-03.0` is now live as
   build `2026-08-03.0`; operation-preserving publication retained both reverse
   families and the catalog CAS plus six production smokes passed.
   Brandenburg Gate is corrected to the
   source-alias/tokenization plus routed-cap backlog, and Machu Picchu Cusco
   remains in region-context routing. Do not reopen build ordering, duplicate
   collapse, phrase scope, or the head cap without contradictory evidence.
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

- `docs/plans/2026-08-05-gold-coverage-in-base-theme.md` — how much of the gold
  set lives in `theme=base`, and the QID/`names.common` it carries there.
- `docs/plans/2026-08-05-sidecar-p1968-dead-end.md` — why the GERS-to-QID
  sidecar is closed for Places, the refuted circularity hypothesis, and where
  entity fame actually lives.
- `docs/plans/2026-08-04-release-move-recall-delta.md` — the refutation of the
  Overture release move as a quality lever, and the CJK regressions it exposed.
- `docs/plans/2026-08-04-head-cap-eviction-ranks.md` — what actually loses an
  indexed place in the global head, and the refutation of the cap-eviction
  hypothesis.
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
