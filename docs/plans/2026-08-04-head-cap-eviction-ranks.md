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

## The hypothesis, what survived, and what did not

**Hypothesis (2026-08-04):** everyday POIs are indexed but sink below
`HEAD_RESULT_CAP` on their own name token, because `COMMODITY_CATEGORIES` gives
them `prominence_rank = 0` and confidence is a flat per-source constant, so the
cap key degenerates to `feature_id` order. Predicted fix: raise the cap.

**Mostly CONFIRMED, with one clause refuted and one wrong prediction.**

CONFIRMED -- the sinking is real, and it is on the DISTINCTIVE token, not just
generic ones. Every commodity case is evicted from its own name token, usually
by a wide margin:

```
habana  rank  72     pensil  rank  20     estoril rank  79
antenas rank  97     harare  rank  25     porto   rank 332 / novo rank 304
harrods rank  22     novotel rank  91     mayo    rank 213
```

All are above the cap of 10, so the record is in **no posting at all**, and
every one of them carries `prominence_rank = 0`.

REFUTED -- the `feature_id`-order clause. Not one token in either set has its
top ten decided by UUID order (`tokens_decided_by_feature_id: 0`). The records
above lose to contenders with genuinely higher prominence, not to a coin flip.

WRONG PREDICTION -- "raise the cap" still does not work, but not for the reason
first written here. It fails because these queries ALSO carry a generic token
(`hotel`, `london`, `clinic`, `museum`) where no cap is enough: recovering
`Harrods London` would mean out-ranking every record on Earth carrying
`london`. A cap large enough for `habana` (>= 72, and that is a lower bound)
still leaves `hotel` unsatisfiable.

### Correction, 2026-08-04

The first version of this document claimed "the distinctive word is never the
problem" and pointed the fix at query-time selectivity. **Both were wrong**, and
the error was caught by trying to implement the recommendation: the Worker
ALREADY has exactly the proposed mechanism. `merge_bounded_candidates` relaxes
a saturated posting and proves the missing token from `record_display_tokens`,
which already spans `primary_name`, `brand_name`, `category`, `locality`,
`region` and `country`.

It does not fire here because it can only rescue a record that survived the cap
in at least ONE token, and these survive in none. **No query-time change can
retrieve what the index does not store.** The fixes are therefore all
rebuild-scoped, which is the opposite of the original conclusion.

## Headline

**Raising `HEAD_RESULT_CAP` alone recovers none of these cases** -- not because
the records are near the cap, but because they are far below it on their own
name token AND vetoed by a generic token no cap can satisfy. The losses are:

| what happened | everyday | gold |
|---|---|---|
| query refused: more than `HEAD_QUERY_TOKEN_CAP` words | 2 | 2 |
| evicted on a GENERIC token while ranking fine on the distinctive one | 6 | 5 |
| token never emitted for the record at all | 3 | 0 |
| indeterminate (see below) | 0 | 3 |
| already inside the cap — the head is not what loses them | 2 | 0 |
| **EXACT-match IN_HEAD misses examined** | **13** | **9** |

## Two independent losses, and both must be fixed

The head intersects a per-token top-`HEAD_RESULT_CAP` list for every query
word, so a record must survive the cap in **all** of them. These cases fail
twice over:

```
HOTEL HABANA      habana  rank  72 of 452     hotel  EVICTED (pack 10/10)
HOTEL PENSIL      pensil  rank  20 of  69     hotel  EVICTED (pack 10/10)
HOTEL ESTORIL     estoril rank  79 of 174     hotel  EVICTED (pack 10/10)
Harrods London    harrods rank  22 of  69     london EVICTED (pack 10/10)
Novotel Monte Carlo novotel rank 91 of 500    monte, carlo  EVICTED
Mayo Clinic Rochester mayo rank 213 of 753    clinic, rochester EVICTED
Louvre Museum     -                           louvre, museum EVICTED
```

**Loss 1, the distinctive token.** Every rank above is > 10, and each is a
LOWER bound. A commodity POI at `prominence_rank = 0` loses its own name token
to every non-commodity contender that happens to carry the same word. This is
fixable by cap size in principle, but the required cap is measured in hundreds.

**Loss 2, the generic token.** `hotel`, `london`, `clinic`, `museum`, `berlin`
are the *least* informative words in these queries and each alone empties the
intersection at any cap. `hotel` is saturated at the measurement ceiling
(880 = 88 packs x 10) and its true planet-wide count is far higher. The
consequence is inverted from what a user expects: **adding a locality word makes
a query strictly harder**. `Harrods London` is harder than `Harrods`, not
easier.

Fixing either one alone changes nothing, which is why "raise the cap" fails as
a plan even though the cap is genuinely part of the problem.

### Why the phrase lane does not catch them either

`e2:hotel habana` exists as a key shape but has zero rows. The entity-phrase
lane admits only `prominence_rank > 0`, and `COMMODITY_CATEGORIES` maps hotels
to 0. So the class that most needs a phrase key is precisely the class
excluded from it — and every one of the six Mexican hotels measured here
carries `prominence_rank = 0`.

**The lever is admission, and it is rebuild-scoped.** Give this class a key
that is selective by construction instead of hoping it wins a generic one: an
`e2:`/`e3:` phrase posting for `hotel habana` has ten slots for a phrase almost
nothing else carries, where the word `hotel` has ten slots contested by every
hotel on the planet. That is one admission-rule change --
`prominence_rank > 0` -- and it targets the exact class the rule excludes.

It is NOT a query-time fix, and this is where the first draft of this document
was wrong. The Worker already relaxes a saturated posting and proves the
missing token from `record_display_tokens` (which already spans `locality`,
`region` and `country`). That machinery cannot help here because it rescues
only records that survived the cap in at least one token, and these survive in
none.

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

All rebuild-scoped, because no query-time change can retrieve a record the
index does not store.

1. Admit `prominence_rank = 0` records to the entity-phrase lane. Targets the
   largest measured class and gives it a key generic-word contention cannot
   reach. Size it against the Wave C posting-growth numbers first.
2. Selectivity in the head — either exclude very low-information tokens from
   the intersection requirement or make the cap selectivity-aware, so `hotel`
   stops acting as a veto.
3. Alternate/common-name coverage for non-Latin queries against English-named
   records (the three HK cases).
4. Settle the three indeterminate tokens by reading the merged head or the
   source `names.common` directly.

A cap raise on its own is NOT on this list: it addresses loss 1 and leaves
loss 2 untouched, so nothing measured here would start resolving.
