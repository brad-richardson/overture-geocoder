# Multilingual type-ahead benchmark — 2026-07-09

Date: 2026-07-09
Live version measured: `2026-07-02.3` (Overture `geocoder.bradr.dev`).
Comparator: Photon (`photon.komoot.io`). Nominatim skipped (not an
autocomplete engine, strict 1 req/s).

Raw data: `benchmarks/2026-07-09-multilingual-typeahead.json`
(13 third-language cases, progressive prefixes, top-5 window, 50 km tolerance).

Purpose: measure the demand/capability gap for multilingual forward search
called out in `docs/plans/2026-07-02-future-work.md` (section 2). Today
`search_name` carries only
primary + short + English common/official/alternate names, so third-language
exonyms ("moscou", "kolonia", "nueva york") are predicted to miss.

## Method and its one important caveat

Each case is a place typed in a language that is neither its local language
nor English (French "moscou", German "moskau", Spanish "nueva york", Polish
"kolonia", plus Japanese "モスクワ" and Russian "варшава" in native script).
Every target city is already present in Overture's index under its native and
English names — verified by direct query (`firenze`, `venezia`, `praha`,
`warszawa`, `koln`, `cologne`, `athens` all resolve). So any miss below is
purely a missing *name variant*, not a missing place.

Caveat that **advantages Overture**: the live worker gets a location bias from
CF request headers; Photon is queried with no location and no `lang=`
parameter. So this is a conservative comparison — Overture is helped, Photon
is handicapped, and Photon still wins decisively on completed queries.

## Headline

- **Completed third-language query, correct city in top-5:** Overture **1/13**
  (8%) vs Photon **6/13** (46%). Rank-1: Overture **0/13**, Photon **3/13**.
- Overture's only "hit" (`pekin` → 北京市 at rank 2) works only because the
  English exonym **Peking** is in the index and shares the `pekin` prefix — not
  because French/Spanish "Pékin/Pekín" is understood.
- **The keystrokes-to-top-1 metric flatters Overture and must not be read at
  face value.** Overture's median 3.5 vs Photon's 4.0 is an *artifact*: 8 of
  Overture's early rank-1 hits are Latin-prefix collisions with the English
  exonym already indexed ("mos"→Moscow, "florenc"→Florence, "prag"→Prague,
  "veni"→Venice, "tok"→Tokyo), every one of which **collapses out of the top-5
  the instant the foreign spelling diverges**. Full-query top-1 = 0.
- **Latency:** Overture p50 **56.5 ms** vs Photon **467.5 ms** (~8x faster,
  consistent with prior benchmarks). Speed is not the issue; coverage is.

## Per-case results

Columns: source language of the query; the accepted target (native/English
variants also accepted); and the **full-query** rank each engine returns
(`MISS` = target not in top-5 once the whole word is typed). The trailing
column shows how Overture's early rank-1 evaporates as the word completes.

| Query        | Lang | Target (accepted variants)        | Overture full-q | Photon full-q | Overture ranks by prefix len |
|--------------|------|-----------------------------------|:---------------:|:-------------:|------------------------------|
| moscou       | fr   | Москва / Moscow                   | MISS            | **1**         | mos→1, mosc→1, mosco→1, moscou→MISS |
| moskau       | de   | Москва / Moscow                   | MISS            | **1**         | mos→1, mosk→MISS |
| tokio        | de/es/pl | 東京都 / Tokyo                   | MISS            | **1**         | tok→1, toki→MISS |
| londres      | fr/es | London                           | MISS            | 2             | lon→3, lond→1, londr→MISS |
| venise       | fr   | Venezia / Venice                  | MISS            | 2             | ven→3, veni→1, venis→MISS |
| pekin        | fr/es | 北京市 / Beijing (Peking)        | **2**           | 2             | pek→1, peki→1, pekin→2 |
| florencia    | es   | Firenze / Florence                | MISS            | MISS          | florenc→1, florenci→MISS, florencia→MISS |
| praga        | es/it/pl | Praha / Prague                 | MISS            | MISS          | prag→1, praga→MISS |
| nueva york   | es   | New York                          | MISS            | MISS          | never (no Latin-prefix overlap) |
| atenas       | es/pt | Αθήνα / Athens                   | MISS            | MISS          | never |
| kolonia      | pl   | Köln / Cologne                    | MISS            | MISS          | never |
| モスクワ      | ja   | Москва / Moscow                   | MISS            | MISS          | never |
| варшава      | ru   | Warszawa / Warsaw                 | MISS            | MISS          | never |

Notes:
- **Overture full-query resolution is effectively zero.** The single non-MISS
  (`pekin`, rank 2) is the English "Peking" alt-name prefix, and the real
  city sits behind Pekin, IL (a US town literally named "Pekin").
- **Photon collapses too on `praga`/`florencia`** — real decoys (Praga, a
  Warsaw district; Florencia, Colombia) outrank the famous city without a
  location bias. And even Photon MISSES `nueva york`, `atenas`, `kolonia`,
  and both non-Latin queries. The exonym problem is genuinely hard; a full
  multilingual index is necessary but not sufficient without ranking help.
- The Spanish/Polish/non-Latin queries with **no Latin-prefix overlap**
  (`nueva york`, `atenas`, `kolonia`, `モスクワ`, `варшава`) are the honest
  measure of "no candidate exists at all" — Overture returns nothing usable
  at any prefix length for all five.

## Takeaway — is the gap big enough to build for?

The capability gap is real and unambiguous. On completed third-language
queries Overture resolves 0/13 at rank-1 (1/13 anywhere in the top-5, and that
one only through the English "Peking" alt), while Photon — deliberately
handicapped here with no location or language bias — still surfaces the correct
city for 6/13 (rank-1 for 3). Because every test city is already in Overture's
index under its native and English names, the failures are exactly the
`search_name` variant gap the future-work doc predicted: the moment a query
stops sharing a Latin prefix with the English exonym, Overture has no signal at
all. The keystroke metric that made Overture look competitive (3.5 vs 4.0) is a
mirage — it measures accidental prefix overlap with English names, not
multilingual understanding, and it vanishes on the full word.

But the data also argues against the *heaviest* fix. Even Photon, with the full
OSM `name:<lang>` set, only resolves about half the completed queries and just
three at rank-1 without tuning, because the residual misses are **ambiguity**,
not absence: Kolonia (Micronesia/Poland), Londres (Argentina), Praga (Warsaw),
Florencia (Colombia), and Venise (France) are all real, smaller places that
outrank the famous city when there is no location or importance bias. Overture
already has both a location bias and a wiki-importance term, which is precisely
the disambiguation muscle un-tuned Photon lacked here. That points to the
lighter of the two designs in the future-work doc: **add the missing name
variants (a `names_i18n` alt-name table, or additional `;`-separated variants
in `search_name`) rather than minting 40 per-language shard families.** A
variant table directly fixes the "no candidate exists" cases (`nueva york`,
`atenas`, `варшава`, `モスクワ`, `kolonia`), and Overture's existing
importance + location ranking should keep the newly-introduced decoys in check
— the exact ranking help that un-tuned Photon was missing. Per-language shard
families are only justified if adding variants actually deflates BM25 or bloats
shards, and we cannot know that until we try the single-table version.

Recommendation (the item is "measure, then choose"; this is a lean, not a
lock-in): the gap justifies building *something*, and the lowest-risk first
step is a bounded `names_i18n`/multi-variant `search_name` for a high-value
language set (fr/de/es/it/pl + each place's major exonyms), then re-running this
exact benchmark to confirm the lift and watch for BM25/shard-size regressions.
Escalate to per-language shard families only if that regression materializes.
One caveat before prioritizing at all: this measures *capability*, not observed
*demand* — there are no query logs here showing real third-language traffic, so
this work should still rank below the P0 catalog rebuild and the places-theme
scope decision until such traffic is confirmed.

## How to reproduce

```bash
python scripts/benchmark_typeahead.py --cases multilingual --skip nominatim \
    --output benchmarks/2026-07-09-multilingual-typeahead.json
# Standard set is unchanged and still the default:
python scripts/benchmark_typeahead.py --output benchmarks/<date>-typeahead.json
```
