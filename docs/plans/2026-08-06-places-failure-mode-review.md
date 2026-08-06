# Places failure-mode review: the skeletons audit, 2026-08-06

A deliberate step back from mechanism work, prompted by the operator's
suspicion that the project keeps rabbit-holing into niche solutions while
larger structural problems go unmeasured. The review ran four independent
passes — a consolidation of every documented failure mode, an independent
recomputation of the benchmark miss distribution from the raw JSONs, a
structural review of the Worker query path, and a sweep of historical session
transcripts for dropped leads — then sized the top candidates with local
corpus scans and live production probes.

**The suspicion is confirmed, in three separate ways.** (1) ~70% of the
everyday-POI misses cannot be won by any geocoder — the instrument is
measuring registry legal names absent from all map data. (2) The sole
remaining sanctioned lever addresses ~7.5% of everyday misses and its sizing
join was never computed. (3) There are real skeletons, verified live on
2026-08-06 against build `2026-08-03.0`, that zero benchmark cases measure:
the proximity ("near me") path is broken end to end, and the tokenizer has
variant holes covering millions of records.

Method notes: live probes hit `https://geocoder.bradr.dev/v2/forward`
(production, v2 build `2026-08-03.0`, Overture `2026-06-17.0`). Corpus scans
ran on the local `2026-06-17.0` planet mirror — the same vintage production
serves — via DuckDB over `theme=places/type=place` (75,642,289 records) and
the `2026-07-22.0` divisions mirror. Code references are against `main` at
`9c3714b`.

## 1. The measured yield of the recent mechanism work

The last five shipped mechanisms yielded, in order: +3 gold @1 (RC2),
+1 gold @10 (RC3), +4/+5 gold and +1 everyday (v4 phrase admission, at the
cost of a planet rebuild), +4 everyday (additive Worker wave), +1
(compound-category proof — predicted 6). Single digits each. Over the same
period, the three structural facts below were only quantified on 08-04/08-05,
and the P1968 sidecar consumed a 200-decision audit apparatus before anyone
ran the one SPARQL COUNT (7,234 planet-wide) that killed it.

This is not an argument that the work was wrong — RC2/RC3 and the seam
calibration were real, and the seam fix alone moved t@1 0.261 -> 0.587. It is
an argument that the *next* unit of effort has better places to go.

## 2. The instrument is the largest single problem

Independent recomputation from the raw benchmark JSONs (all numbers
reproduced from the `results` arrays, not the stored summaries; script
retained in the session scratchpad, inputs are the committed
`benchmarks/2026-08-04-everyday-poi-post-additive-wave.json` and
`benchmarks/2026-08-04-everyday-head-miss-interrogation-v1.json`):

| mechanism × head verdict | ABSENT | NOT_ADMITTED | IN_HEAD | total |
|---|---|---|---|---|
| empty, ≤3 tokens | 51 | 9 | 10 | 70 |
| empty, >3 tokens | 26 | 4 | 6 | 36 |
| non-empty, not in top-10 | 15 | 3 | 6 | 24 |
| **total misses (latest run)** | **92** | **16** | **22** | **130** |

- **92 of 130 misses are ABSENT** — no name-matching Overture record within
  2× tolerance of the gold point. This is not an Overture coverage hole:
  ABSENT cases have a median 10,898 corpus rows inside the probe box. The
  *names* don't match, because the gold names are government-registry legal
  names ("Rossi Clothing Pty Ltd", "CITY EXPRESS BY MARRIOT CDMX LA VILLA").
  84 of the 92 have no OSM name evidence within 500 m either.
- MX misses 35/35 and CO 20/20 — exactly the registry-sourced strata.
  Nominatim solves 22/200 and Photon 23/200 against production's 70/200;
  only 12 of our 130 misses are solvable by any compared provider.
- The instrument is blind in the directions real users query: **all 200
  queries are the bare `expected_name` verbatim** — zero proximity, zero
  locality context, zero spelling variants — and `recall@10 ≡ recall@1` by
  construction, so ranking improvements cannot register on it at all.
- The gold set fails the opposite way: of its 15 misses, 13 are IN_HEAD —
  serving/ranking defects, fix-forward territory.

This extends `2026-08-04-measurement-apparatus-findings.md` ("fix the
instrument first") with the denominator: production is at roughly 65% of the
achievable ceiling on this set, not the 35% the headline reads as.

## 3. Skeleton 1: the proximity lane is broken end to end, and nothing measures it

Flagged as the top structural risk in a July 28 session ("a global per-token
top-10 has no notion of 'near me'"), then dropped: no benchmark case carries
proximity (which is also what let the antimeridian panic sit live for three
weeks). Verified live 2026-08-06:

- `q=starbucks&proximity=-73.9855,40.7580` (Times Square) returns 10
  Starbucks at distances **67.0, 38.5, 52.3, 24.1 km** (first four, in rank
  order). In Manhattan, where dozens exist within 1 km, the nearest returned
  record is 24 km away. Two independent defects compose:
  1. **`distance_km` is computed and returned but never ranked on.**
     `place_score` (`crates/geocoder-worker/src/v2.rs:1778`) has no distance
     term, and the final sort is score-only, so equal-name equal-prior chain
     records tie and order arbitrarily.
  2. **The retrieved set itself excludes the nearby records.** The routed
     lane serves one construction cell from a per-`(cell, token)` posting
     capped at 256 (`ROUTED_CANDIDATE_CAP`,
     `places_construction_v1.rs:93`) selected by the build-time cap order,
     not by distance — the same eviction mechanism as the head, one level
     down. (Mechanism-supported by the observed 24 km floor; the cell
     posting itself was not dumped. A bounded probe can confirm.)
- `q=McDonald's&proximity=-73.9855,40.7580` ranks **McDonald County, MO**
  (relevance 0.5749) above every McDonald's (0.5199) — the division lane
  ignores proximity in the cross-lane merge.
- Structurally, proximity is a hard single-cell filter, not a bias: one
  ~1.4°×0.7° cell, no neighbor-cell probe, no fall-through to the head when
  the cell yields nothing, and both locality inference and the prefix-head
  fallback are gated `proximity.is_none()` (`v2.rs:2711`). With proximity,
  ≥5-token queries (`ROUTED_QUERY_TOKEN_CAP` 4) return empty with no
  fallback lane at all. A user meters from a cell edge cannot see across it.

Every one of these is **Worker-only fixable** — distance term in the score,
neighbor-cell probe, empty-cell fall-through, proximity-aware seam — no
rebuild, no format change. This is plausibly the largest real-user failure
mode the system has, and it is invisible to every existing metric.

## 4. Skeleton 2: tokenizer variant holes, sized on the production corpus

One DuckDB pass over the 75,642,289-place production corpus (2026-06-17.0),
`names.primary`:

| variant class | records | share | live confirmation (2026-08-06) |
|---|---|---|---|
| apostrophe in name | 2,553,035 | 3.4% | `q=mcdonalds` + NYC proximity: best hit "McDonald's in Chinatown" at 0.375, then *Queens Center Dental* at 0.018. `McDonald's` (typed with apostrophe) works — the tokens are `[mcdonald, s]`, and the apostrophe-free spelling users actually type matches neither |
| ampersand in name | 2,814,456 | 3.7% | `H&M` survives only because both single-letter postings do; "h and m" cannot match (`and` is a token no record proves) |
| Thai/Lao/Khmer/Myanmar script | 1,633,750 | 2.2% | no bigrams at build (`is_cjk` covers Han/Kana/Hangul only) and no query expansion: reachable only by byte-exact whole-name equality |
| non-decomposable Latin (ø ł æ œ ß đ þ) | 640,672 | 0.85% | `q=orsted` and `q=Ørsted` return **disjoint** result sets — NFKD never folds these letters |
| St/Mt/Dr/Ft abbreviations | 656,223 | 0.9% | no expansion layer anywhere |

Two adjacent confirmations:

- **The Monte-Carlo divisions gap is real, and it is a class.** The locality
  `Monte-Carlo` exists in divisions (`59b1eedb-ebbc-4de5-b616-6e30081a32ee`),
  yet `q=Monte Carlo` returns 10 POIs named "Monte Carlo" and **no division**
  — the open question from `2026-08-03-claude-audit-feedback.md` §13 is now a
  confirmed defect. ~89,000 locality-type divisions carry hyphens (62,889
  locality + 22,081 neighborhood + 3,547 macrohood + 513 localadmin).
  Because locality-suffix routing requires that division lookup to succeed,
  this same gap explains the `Novotel Monte Carlo` and `Casino de
  Monte-Carlo` gold misses (both verified returning zero live): the head
  lane has `novotel` cap-evicted, and the routed rescue can never fire.
- **The scoring normalizer is weaker than the retrieval normalizer.**
  `normalize_for_match` folds only a small Western-European diacritic table,
  while retrieval tokenization is NFKD, so a record retrieved for
  `skoda muzeum` scores `match_quality = 0` and is floored at rank time.
  Czech/Polish/Turkish/Romanian/Hungarian names are structurally unable to
  rank on exact hits. Worker-only fix: score with the same NFKD tokens
  retrieval uses.

The build-vs-query normalization parity warning was raised in the July 28
session in exactly these terms (apostrophes, diacritics, CJK); only the CJK
instance was ever chased. No normalizer-equivalence test exists.

## 5. Skeleton 3: 28% of names are beyond every exact lane

Word-count distribution of `names.primary` (whitespace tokens, the index's
unit), production corpus:

| words | records |
|---|---|
| 1 | 11,163,082 |
| 2 | 21,595,508 |
| 3 | 19,807,996 |
| 4 | 11,397,776 |
| 5 | 5,743,907 |
| 6 | 2,828,471 |
| 7+ | 3,105,549 |

**21.1M records (28%) have ≥4-word primary names.** For those, no phrase key
can exist (`ENTITY_PHRASE_MAX_WORDS = 3`,
`crates/geocoder-construction/src/bin/places_transform_v1.rs:30`), the head
refuses the full name (`HEAD_QUERY_TOKEN_CAP = 3`), and the 4–6-token
prefix-head fallback must prove every dropped token from context fields
stored as **ISO codes** — `washington` can never be proven from `US-WA`, nor
`france` from `FR` (`project_places_construction_v1.py` stores
`addresses[1].region`/`.country` verbatim). This also vetoes the head AND for
any query carrying a full region/country name, and locality inference
excludes `region`/`country` types, so `<name> <state>` cannot route either.

Also stored but never consulted: **brand**. Brand tokens are indexed as
identifying at build, but the record projection drops `brand_name` and
`poi_match_quality` compares only `primary_name` — a POI named "Store #123"
with brand Starbucks can be retrieved and can never rank.

## 6. What the sanctioned roadmap actually covers

- **`prominence_rank = 0` phrase admission** (the sole remaining sanctioned
  lever): claims ~10 of the 134 everyday misses (~7.5%); the formal sizing
  join (`2026-08-04-v5-build-readiness.md` §3.2) is still PENDING and was
  never computed. Its real constituency is the **gold** set, where 31/45
  cases are cap-evicted in both releases. Worth keeping — as a gold-set
  lever, not a strategy.
- **The largest winnable everyday class has no lever.** Wrong-entity global
  homonyms — the right name at the wrong place, 777–17,000 km away — cover
  ~16–24 everyday plus ~7 gold cases. Since the P1968 closure, commercial-POI
  fame has no open mechanism at all. The only living fame lever is the
  **theme=base landmark import** (14/45 gold case rows, all currently
  unservable, with QIDs on 9/14 and `names.common` on 10/14), which remains
  unscoped.
- **`names.common` identically empty on Places** (0 of 75,642,289, vs 33–40%
  on divisions and populated on base) silently caps all cross-language work
  and looks like an upstream Overture defect worth reporting.
- Other dropped leads recovered from transcripts: v2 has no character-prefix
  matching and the typeahead property was never measured (`autocomplete` is
  parsed and dead in the construction lane); locality Fixes 3–4 from the
  08-03 audit are parked with no tracking; `relevance` semantics are still
  undocumented in `docs/api-v2.md`.
- Housekeeping contradictions worth clearing: the v5 gate sheet
  (`2026-08-04-v5-build-readiness.md` §3.3) still lists the dead P1968
  sidecar as a mechanism, and the phrase-admission prohibition in the state
  doc still cites the 31.7 MB head-byte "reserve" that
  `2026-08-04-measurement-apparatus-findings.md` §3 showed gates a
  rehearsal fixture, not production.

## 7. Recommendations, in order

1. **Fix the instrument before shipping any more mechanisms.** Quarantine
   the registry-name strata and re-baseline the denominator; add a
   proximity/"chain near me" stratum, a variant-typing stratum (apostrophes,
   hyphens, ampersands, diacritics, abbreviations), and cases where rank@10
   can differ from rank@1. Every lever below is invisible to the current
   benchmark.
2. **Ship a proximity-lane wave (Worker-only, no rebuild):** distance term
   in `place_score`, neighbor-cell probe, fall-through to the head on an
   empty cell, and a proximity-aware division/POI seam. Measure with the new
   stratum from (1).
3. **Fold the variant holes into the already-planned v5 rebuild** alongside
   phrase admission: apostrophe/possessive folding, ampersand variants,
   non-decomposable Latin folding, hyphen folding (divisions included), and
   a build-vs-query normalizer equivalence test so the class cannot silently
   reopen. The scoring-normalizer parity fix (§4) is Worker-only and need
   not wait.
4. **Compute the PENDING phrase-admission sizing join before the rebuild is
   paid for**, so its expected yield is on record.
5. **Scope the theme=base landmark import** — the only living fame lever;
   coverage, QIDs, and alternate names in one producer change.
6. Bounded follow-ups: the Monte-Carlo divisions-gap mechanism (likely
   divisions shard-router or FTS hyphen handling), brand-at-scoring, the
   empty-gate suppression (one weak division hit blocks every POI fallback),
   and reporting `names.common` upstream.

## 8. Limits

- Live probes are n-of-1 spot checks against one build on one day; they
  demonstrate existence, not rates. The corpus counts are denominators
  (records carrying a variant), not measured failure rates — a record with
  an apostrophe fails only for the query spelling that omits it.
- The routed-lane eviction claim in §3 is mechanism-supported plus one
  observation (24 km nearest in Manhattan); the cell posting was not dumped.
- The 130-vs-134 miss counts are different runs of a moving build
  (post-additive-wave vs post-v4); this doc uses 130 (the interrogation run)
  for the bucket table and 134 where quoting the frozen classification.
- Nothing here re-litigates closed decisions: RC2/RC3 stay closed, the cap
  stays at 10, the release move stays refuted, the sidecar stays dead.
