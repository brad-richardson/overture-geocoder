# What actually loses an indexed place in the global head

Measured 2026-08-04 against the local planet Places head candidates for
Overture 2026-07-22.0, by
`benchmarks/probes/2026-08-04-head-cap-eviction-rank-probe.py`. Evidence:
`benchmarks/2026-08-04-head-cap-eviction-ranks-{everyday,gold}-v1.json`.

This refines the IN_HEAD verdict from
`docs/plans/2026-08-04-head-miss-interrogation.md`. IN_HEAD says the corpus has
the record and the producer emitted a candidate for it, so the loss happens at
or after the merge. It does not say *how* the record lost, and the plans that
follow from "raise the cap" and from "the cap is not the problem" are entirely
different.

## Headline

**Raising `HEAD_RESULT_CAP` recovers none of these cases.** Not one miss in
either set is a record that sits just outside the cap on the token that
identifies it. The losses are:

| what happened | everyday | gold |
|---|---|---|
| query refused: more than `HEAD_QUERY_TOKEN_CAP` words | 2 | 2 |
| evicted on a GENERIC token while ranking fine on the distinctive one | 6 | 5 |
| token never emitted for the record at all | 3 | 0 |
| indeterminate (see below) | 0 | 3 |
| already inside the cap — the head is not what loses them | 2 | 0 |
| **EXACT-match IN_HEAD misses examined** | **13** | **9** |

## The generic token is a veto, not a hint

The head intersects a per-token top-`HEAD_RESULT_CAP` list for every query
word. A record must therefore survive the cap in **all** of them. The
distinctive word is never the problem:

```
HOTEL HABANA      habana  rank  72 of 452     hotel  EVICTED (pack 10/10)
HOTEL PENSIL      pensil  rank  20 of  69     hotel  EVICTED (pack 10/10)
HOTEL ESTORIL     estoril rank  79 of 174     hotel  EVICTED (pack 10/10)
Harrods London    harrods rank  22 of  69     london EVICTED (pack 10/10)
Novotel Monte Carlo novotel rank 91 of 500    monte, carlo  EVICTED
Mayo Clinic Rochester mayo rank 213 of 753    clinic, rochester EVICTED
Louvre Museum     -                           louvre, museum EVICTED
```

`hotel`, `london`, `clinic`, `museum`, `berlin` are the *least* informative
words in those queries, and each one alone is enough to empty the
intersection. The consequence is inverted from what a user expects: **adding a
locality word makes a query strictly harder**. `Harrods` ranks 22 on its own
token; `Harrods London` cannot resolve, because Harrods is not one of the ten
most prominent things on Earth carrying the token `london`.

No cap raise fixes this. `hotel` is saturated at the measurement ceiling
(880 = 88 packs x 10) and its true planet-wide count is far higher; recovering
Hotel Habana by cap size alone would mean out-ranking every record on Earth
carrying the word `hotel`.

### Why the phrase lane does not catch them either

`e2:hotel habana` exists as a key shape but has zero rows. The entity-phrase
lane admits only `prominence_rank > 0`, and `COMMODITY_CATEGORIES` maps hotels
to 0. So the class that most needs a phrase key is precisely the class
excluded from it — and every one of the six Mexican hotels measured here
carries `prominence_rank = 0`.

**The lever is selectivity, not cap size.** The cheapest fix is query-time and
needs no rebuild: head entries already carry `primary_name`, so when a
multi-token intersection comes back empty the Worker can re-query the most
selective token alone and filter the returned entries by the remaining words.
That is a Worker change against data that already exists.

## Tokens never emitted

Sound where the record's own pack had cap room to spare — nothing was dropped,
so the producer simply never emitted the token:

```
聖母醫院      -> Our Lady of Maryknoll Hospital
屯門醫院      -> Tuen Mun Social Hygiene Clinic
麥理浩復康院  -> Maclehose Medical Rehabilitation Centre
```

The head indexes `primary_name`, `brand_name`, `category`, `locality`,
`region`, `country`. Where the primary name is English and the query is in the
local script, there is nothing to match. This is the same root cause as the
CJK routed-lane defect fixed today, but it is NOT fixed by that change: it is
a coverage gap in what the head indexes, not a tokenization bug.

## Indeterminate, stated rather than guessed

Three gold tokens (`brandenburg`, `gate`, `cusco`) sit in a pack that was full
AND are absent from the record's display columns. The packs cannot decide
these: they expose `primary_name` and `brand_name` but not COMMON names, and
the producer indexes common names under the same identifying field mask. So
absence is explained by eviction or by non-emission, and this probe declines to
pick. `Brandenburger Tor` is the interesting one — if it carries no common
name "Brandenburg Gate", the English query has nothing to match.

## What this probe can and cannot state

* Candidate packs are already capped **per map task**, so a computed rank at or
  below `HEAD_RESULT_CAP` is exact — that is the decomposability property
  `top_n(A u B) = top_n(top_n(A) u top_n(B))` the cap key is built on. A rank
  above it is a **lower bound**; the rows that beat it may already be gone.
  Read `rank 332 of 671` as "at least 332 of at least 671".
* Only `match_strength == EXACT` cases are ranked. The interrogation's
  CONTAINMENT_ONLY tier matches things like "Fushimi Times Square" (Kyoto) for
  the query "Times Square", which identifies the wrong record; ranking those
  would have produced confident numbers about a place in the wrong country.
  That exclusion is why 13 and 9 cases are examined here rather than 22 and 13.
* It reads head CANDIDATES, the merge's input, not the merged shards.

## Follow-ups this opens

1. Worker: empty-intersection retry on the most selective token with a
   display-name filter. No rebuild, and it targets the largest measured class.
2. Selectivity in the head at build time — either drop very low-information
   tokens from the intersection set or make the cap per-token-selectivity
   aware. Rebuild-scoped, so it belongs to a release that is rebuilding anyway.
3. Alternate/common-name coverage in the head for non-Latin queries against
   English-named records. Rebuild-scoped.
4. Settle the three indeterminate tokens by reading the merged head or the
   source `names.common` directly.
