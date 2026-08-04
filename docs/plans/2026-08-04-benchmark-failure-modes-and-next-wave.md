# Benchmark failure modes and the next wave

Date: 2026-08-04

Inputs: the 2026-08-03 benchmark round (55-case forward gold
`2026-08-03-forward-gold-external-after-a2bc.json`, 200-case everyday-POI
`2026-08-03-everyday-poi-external-baseline-v1.json`, Overpass presence control
`2026-08-03-everyday-poi-overpass-presence-v1.json`), live spot-checks against
the promoted v4 build (`2026-08-03.0`), read-only code investigation of the
Worker forward path, and a forensic pass over the 34-hour v4 build/promotion
agent session (2026-08-02 17:24 → 2026-08-04 03:47 UTC).

Caveat that frames everything: **both analyzed runs measured `2026-08-02.0`,
i.e. pre-v4.** Preview acceptance already showed v4 moving gold rank@1 26→30 and
rank@10 34→39 (4/0 and 5/0 gains/losses) and everyday-POI 62→65 / 65→66. Live
spot-checks confirm v4 recovered the prominent 2–3-token exact-name class
(Sydney Opera House, Golden Gate Bridge, Singapore General Hospital, Seattle
Central Library, Bugis MRT, single-token Korean hospital names). The taxonomy
below marks what v4 fixed and what remains.

## 1. The dominant mechanism: empty responses by construction

113 of Overture's 135 everyday-POI misses returned a **completely empty result
set** — not misranked results. Root causes, located in code:

- **`HEAD_QUERY_TOKEN_CAP = 3`** (`places_construction_v1.rs:102`): every
  ≥4-token no-proximity query returns empty at `v2.rs:2101` **before any index
  read**; the phrase lane sits below the check and never runs. Latin queries
  with ≥4 tokens were empty 40/40. "YISHUN MRT STATION" (3 tokens) hits while
  "GEYLANG BAHRU MRT STATION" (4 tokens) is empty — same source, same entity
  class. A second outer cap at `v2.rs:2042` empties ≥5-token queries even for
  the proximity lane.
- **Conjunctive empty-posting gate**: for ≤3 tokens, any token with no head
  shard or empty posting aborts the ordinary lane
  (`places_construction_v1.rs:1233`); only *saturated-full* postings get the
  display-field relaxation (RC2).
- **`HEAD_RESULT_CAP = 10` starvation**: candidates seed only from per-token
  planet-wide top-10 postings, so an entity absent from all of its tokens'
  top-10 ("dover", "mrt", "station") is unreachable outside a phrase posting.
  The cap is a frozen operator decision — the fix is not raising it.
- **Locality-inference escape hatch mostly fails**: `exact_locality_result`
  (`v2.rs:1884`) requires an exact *primary-name* division match, so "Mexico
  City" and "Tokyo" fail against endonym primary names, and when inference does
  fire it takes the first exact match (wrong Rochester for "Mayo Clinic
  Rochester").

v4's phrase admission (`e2:`/`e3:` keys, `prominence_rank > 0`, 2–3-word
primary names only) fixes none of the ≥4-token class by construction, nothing
with `prominence_rank == 0` (Dover MRT Station), and no name+context query
whose tokens don't equal a primary name (Noma Copenhagen, Apple Marunouchi
Tokyo, Mayo Clinic Rochester — all still empty live on v4).

The state doc prohibits widening phrase admission this generation (31.7 MB
head-byte reserve above the headroom floor), so the residue fixes are
query-side. Three are Worker-only, fix-forward deployable, and **additive
fallbacks that run only on otherwise-empty responses** (cannot regress a
non-empty result):

1. **Prefix-head fallback**: when a 4–6-token query is empty and locality
   inference failed, probe the head once with the first 3 tokens (including
   their `e2:`/`e3:` keys) and fail-closed verify the dropped trailing tokens
   against candidate display fields, reusing the saturated-posting machinery
   (`places_construction_v1.rs:~1270-1292`). Flips the "X Y MRT Station" /
   category-suffix class — the largest single flip count available.
2. **Locality inference beyond exact primary name**: accept common/alt division
   names in `exact_locality_result`. Flips "Hotel del Angel Mexico City",
   "Apple Marunouchi Tokyo" (contingent on routed-cell presence).
3. **Homonym-tolerant locality retry**: iterate the first few (≤3) exact
   locality matches until the routed search is non-empty. Flips "Mayo Clinic
   Rochester".

Build-side residue is v5-scoped: `e4:` keys for 4-word primary names (letting a
4-token query run only the phrase probe past the cap), and softening the
`prominence_rank > 0` admission gate — both cost head bytes and wait on the
post-v4 miss classification.

## 2. Golden-set taxonomy (55 cases, pre-v4 run: 26/55 @1, 34/55 @10)

Zero regressions from a2bc (7 gains / 0 losses @1, McNemar p=0.0156). All 29
rank-1 misses are POI-expected except Manchester; locality/seam retrieval is
essentially solved. Classes:

- **A. Multi-token phrase not retrieved at all — 12 cases** (all of
  `type_starved`). Externals hit 9/12 at rank 1–3 → retrieval, not coverage.
  Largely the §1 mechanism; v4 recovered the prominent-landmark subset.
  Genuine open-data gaps: Novotel Monte Carlo, Seattle Central Library (both
  externals also miss; Seattle Central Library nonetheless hits on v4 live).
- **B. Distant exact-name homonyms beat the canonical landmark — ~7 cases**
  (Manchester NH over Manchester UK at 5,045 km; Smederevo "Colosseum" over
  Rome; Statue replicas; Louvre 874 km). Externals hit 6/7 at rank 1 — pure
  ranking. Two sub-cases with different fixes:
  - **Divisions** (Manchester): the division shard's static importance column
    ranks NH above UK; the Worker just halves it (`v2.rs:1653`). Bounded
    Worker fix: population tie-break among equal-normalized-name divisions in
    one response (`population` is already on `GeocoderResult`; precedented in
    `query/ordering.rs:60`), nudge kept below the 0.05 match-ladder gap.
  - **POIs** (Colosseum, Statue, Harrods, Raffles): `prominence_rank` is a
    *category-class* prior, not instance fame — a replica in a "louder"
    category (monument 1.0 vs landmark floor 0.85) legitimately wins, and
    same-category exact-name ties fall to merge order. **Rebuild-scoped**; the
    sanctioned vehicle is the famous-unique re-spec + GERS↔QID fame sidecar
    (v5). No honest Worker workaround; do not reshuffle the prior table.
- **C. Locality conditioning hurts — 2 cases.** "Machu Picchu Cusco": the
  routed lane restricts to exactly one level-8 cell (~156 km) containing the
  locality centroid (`v2.rs:2049`, `places_construction_v1.rs:976`); Cusco is
  cell y=108, the monument y=109 — **structurally unreachable**, and the routed
  top score (0.6975, a tour office) is strong, so a weak-score fallback would
  not fire. Fix is a **blend**: retain pre-inference head/phrase candidates
  whose full normalized name equals the stripped query (already fetched, zero
  new reads) and rank them through the existing `place_score` sort, routed
  preferred on ties — `e2:machu picchu` yields the monument at 0.75 > 0.6975.
  Second piece: the centroid distance computed at `v2.rs:2092` and discarded at
  `v2.rs:1941` becomes a small near-tie demotion in this lane only (flips
  Potsdam vs Berlin Brandenburg Gate ordering; the gold case itself may stay
  uncredited — surviving Berlin record ~3.1 km vs 2.0 km tolerance). Guard set:
  Taj Mahal Agra, Sagrada Familia, Eiffel Tower Paris stay rank 1 (RC3
  regressed this lane before).
- **D. Alias/near-miss cases — 5 audited, verdicts final.** None is a
  benchmark-matcher bug; the refused top1 rows are genuinely different entities
  (GPO Museum, MTV billboard, hospital ATM, Freetrailer, Harrods Depository).
  Real gaps: "GPO" ↔ "General Post Office" alias (target indexed 7 m away,
  hits under the full name); apostrophe folding ("Childrens" ↔ "Children's" —
  the hospital hits rank 1 with the apostrophe, 51 m); Times Square and
  Harrods are class-B fame failures (Harrods store 10 m away at relevance 0.52
  loses to the Depository at 0.6625). One gold artifact: **IKEA Lichtenberg's
  gold coordinate is a district geocode ~1.5 km from the store** (correct to
  ~52.5342, 13.5128; externals' correct answers land 1.464 km out against
  tolerance 1.5) — and the store POI itself appears absent from Overture.
- **E. Same-name crowding — KaDeWe r3, Raffles r8, Plaza r8.** Assembly dedups
  by id only (`v2.rs:2517-2522`). Bounded Worker fix: after the final sort,
  keep the best-scored representative per (exact normalized primary name,
  ~0.5–1 km) cluster — the variant the 2026-08-01 duplicates probe measured as
  safe; fuzzy merging stays disqualified. Frees top-10 slots on Machu Picchu,
  Colosseum, replica landmarks. Honest limit: does **not** fix Raffles (the
  crowding rows are differently-named contained sub-POIs; the missing English
  "Raffles Hotel" record is class-B fame/admission again).

## 3. Everyday-POI taxonomy (200 cases, pre-v4: 62/200 @1, 65/200 @10)

Overture beats Nominatim (22 @10) and Photon (23 @10) overall and uniquely
resolves 54 cases — including 33 with no OSM name at all — so the
commercial-feed premise is validated; the empty-response mechanism suppresses
the score. Strata: non-Latin r@10 0.58 vs Latin 0.07 (driven by query length,
not script); worst cells are Latin America 0/55, lodging 0/20, retail 2/40
(family and country are confounded by design). Misses:

- **113 empty responses** (= the `type_starved` 114 minus one locality-only
  response) — §1 mechanism.
- **~16–20 wrong-entity-with-results**: top1 is typically a same-named POI
  7,000–17,000 km away ("Innisfree" 17,296 km). The frozen case set carries no
  `proximity` bias though the harness supports it — decide deliberately whether
  the tripwire measures unbiased global queries (then short-brand cases measure
  ranking priors we cannot win without bias) or the next case generation adds
  proximity.
- **13 competitor-won cases** (12 with exact OSM names): MX hotels, KR
  hospitals, AU retail, 2 SG stations — our §1 residue, entity confirmed
  present in open data.
- **~94 empty-and-no-OSM-name cases**: official-registry names vs commercial
  signage ("Unidad de Servicios de Salud 10 Abastos"). True coverage share
  unknown — currently masked by §1; re-classify only after the query-side
  fixes land.
- Within-top-10 ranking is a non-problem on this set (3 cases).

## 4. Hardening findings from the v4 session forensics

The 34-hour session carried ~8–9 hours of incident-driven delay. Ranked by
payoff:

1. **GitHub "rerun failed jobs" re-ran the entire 128-reducer matrix** after a
   single runner loss, polluting job history and breaking the resume gate
   (fixed in #237: any-attempt authenticated success). Keep: prefer targeted
   resume dispatches over the rerun button; rerun semantics are matrix-wide.
2. **DuckDB spill caps are the recurring planet-scale killer**: head merge died
   at 79% on the 8.5 GiB half-share cap; rehearsal task 87 died on a 2 GiB
   temp allowance; a category probe died twice. Add a readiness check that
   every DuckDB stage's spill allowance is stated and ≥ measured peak + 25%
   (the head temp peak is still unmeasured — only the cap is emitted).
3. **First-execution workflow failures burned two preview attempts**: unpinned
   `cargo install worker-build` pulled 0.8.x (#243); hosted runner lacked
   Python `requests` (#245). Pin every workflow dependency at authoring time;
   add a setup-only cold-start smoke to CI.
4. **promote-slice runs 4,096 serial marker HEAD+GET reads (~2h) with zero
   progress signal** (GitHub live-log 404 made it a black box; ~10 manual
   polling check-ins). Parallelize the fan-out with the existing bounded-client
   pattern and/or write a progress counter object every N markers.
5. **The preview gate conflates env / transient-infra / quality failures** —
   attempts 1–2 (env), 3–4 (real Worker bug, correctly caught), 5–6 (55×404;
   1 timeout + 2×500) all surfaced identically. Classify failure type in the
   run summary so the retry decision is mechanical; give benchmark harnesses
   bounded per-request retry for definite transient codes (mirror of the R2
   definite-5xx retry).
6. **Deploy smoke was statistically too small**: 4 requests missed a 30%
   failure-rate 1101 outage (~45 min degraded prod). Fixed at 15 consecutive
   route-independent probes — keep.
7. **Agent-behavior**: a sandbox-invalid `GH_TOKEN` was misread as a hard
   blocker (~2h stall until user correction) — and the token is *still*
   invalid; fix or remove it, and record the rule "auth/network failures inside
   the sandbox must be retried outside it before being reported as blockers."
   Ship-then-measure regressed prod once (RC3 cap change predated the paired
   gate): require the paired no-loss gate for Worker ranking/routing changes,
   not just data promotions. Fail-closed zero-count checks caught two
   mechanical slips (evidence glob mismatch, wrong-test edit) — keep mandatory.
8. Session-recorded follow-ups not covered elsewhere: **re-audit the August 19
   Overture release** (taxonomy profile + physical `sources[]` provenance
   across all 15 feature types — September removes `categories`); migrate four
   research tools off `sources[].dataset`; comparator columns in final tables
   are frozen runs, not reruns.

## 5. Ranked next wave

Respecting the state doc: the named next milestone is the classification of the
remaining everyday-POI misses; no phrase widening this generation; one
mechanism per PR, each measured with `benchmark_v2_forward.py --compare`;
confidence never ranks; nothing touches the frozen address key or
`head_result_cap`.

1. **Post-v4 frozen re-measure + miss classification** (small; the milestone).
   Re-run both sets against `2026-08-03.0`, freeze the artifacts, and bind the
   §1–§3 mechanism attribution to the post-v4 misses. Everything below sizes
   off this; the pre-v4 taxonomy says the answer, this makes it evidence.
2. **Worker PR: prefix-head fallback for 4–6-token empty queries** (§1 fix 1).
   Largest flip count on the 200-set's worst strata (SG transit, MX/CO, AU
   retail); additive-only risk profile; measured by the everyday-POI compare.
3. **Worker PR: locality-inference upgrades** (§1 fixes 2+3 — alt-name
   division match + bounded homonym retry). Flips the name+context class on
   both sets (Noma, Apple Marunouchi, Mayo Clinic, Hotel del Angel).
4. **Worker PR: locality blend + centroid-distance near-tie demotion** (§2
   class C). Separate PR from #3 per the no-bundling instruction; guard set
   Taj Mahal / Sagrada / Eiffel.
5. **Worker PR: exact-name small-radius assembly clustering** (§2 class E) and
   **division population tie-break** (§2 class B-divisions; flips Manchester).
   Two small independent PRs; both pure-ranking upside on entities already in
   top-10.
6. **Hardening batch from §4** (items 2–5): DuckDB spill readiness check,
   workflow dependency pinning + cold-start smoke, promote-slice progress/
   parallel fan-out, preview-gate failure classification + harness transient
   retry. None blocks quality work; schedule as the buffer between measured
   waves — each item saved real wall-clock this week and the next planet run
   pays them all again.
7. **Sidecar Phase 0 completion**: the 200-decision hand audit (zero false
   provisional accepts) + broadcast byte/RSS measurement. This is the *only*
   honest fix for the POI fame class (Colosseum/Statue/Harrods/Raffles/Times
   Square — ~7 gold cases plus the 200-set's global-homonym class) and it
   gates all v5 fame work. Start once wave items 1–3 are in flight; it is
   independent, local, non-promoting.
8. **v5 build-side batch** (after 1–7 evidence exists): fame/famous-unique
   re-spec onto instance prominence, `e4:` phrase keys + 4-token phrase probe,
   admission-gate softening sized by the post-fix miss classification,
   apostrophe folding in the tokenizer, alias track (GPO ↔ General Post
   Office). One re-attestation pass, never pin rewrites.
9. **Bench maintenance** (anytime, tiny): fix IKEA Lichtenberg gold coords
   (~52.5342, 13.5128) and note the store's probable absence from Overture;
   decide the proximity-bias policy for the next tripwire generation;
   optionally add the decorated NYC Times Square variant to alt_names.
10. **Queued, unchanged**: structured Address forward latency (p50 ~1.6 s —
    next serving-performance gate after the quality wave); August 19 Overture
    release re-audit (time-boxed: before any release move); remaining
    promotion-efficiency items (zero-copy precondition — the v1-only cleanup
    guard is also the one latent live-slice-deletion path — serial HEAD loops,
    one-resumable-job scope); streets stay gated behind their seven
    experiments.

Why this order: item 1 is the agreed milestone and re-baselines everything;
items 2–5 are the entire Worker-only, fix-forward-deployable surface of the
failure taxonomy, ordered by expected case flips per unit risk (2 alone
plausibly moves everyday-POI r@10 from ~0.33 toward the 0.5–0.6 band its
mechanism analysis bounds); item 7 unblocks the one class no Worker change can
honestly fix; item 8 spends rebuild scope exactly once, informed by all the
evidence the earlier items generate.
