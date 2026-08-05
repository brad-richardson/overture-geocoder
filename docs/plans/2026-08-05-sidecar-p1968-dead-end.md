# The P1968 sidecar is a dead end for Places. Do not reopen it.

Measured 2026-08-05. This closes the GERS-to-QID sidecar as a fame source for
the **places** theme. It does not close entity fame as a goal — it relocates it.

## The number that ends it

```
Wikidata items carrying P1968 (Foursquare City Guide venue ID), planet-wide:  7,234
Overture places, 2026-06-17.0:                                          75,642,289
                                                                    coverage 0.0096%
```

Counted directly against the live Wikidata SPARQL endpoint
(`SELECT (COUNT(*) AS ?n) WHERE { ?item wdt:P1968 ?v }`).

The Phase 0 candidate set was built by joining Overture's Foursquare bridge file
against a `LIMIT 1000` P1968 query, producing 344 joined rows and a 200-decision
audit queue. So the audit was sizing a path whose **total planet-wide ceiling is
7,234 entities**. No amount of hand review makes that a fame signal for a 75.6M
record corpus.

## What the sidecar was for, and where that need now goes

Overture carries no ENTITY fame. `prominence_rank` is a per-category prior: it
knows a museum outranks a laundromat, but nothing in the Places corpus
distinguishes *the* Times Square from *a* Times Square. That is the homonym
arbitration failure that dominates the forward gold set — see
`docs/plans/2026-08-04-head-cap-eviction-ranks.md` and the gold misses that
return the right name at the wrong place.

The sidecar was to supply that: a durable GERS-keyed accepted-match ledger plus
an independently refreshed QID-keyed fame table.

**The need is real; the P1968 route to it is not.** `theme=base` carries
`wikidata` as a first-class column on `infrastructure`, `land_use` AND `land`,
verified by `DESCRIBE` on all three. Golden Gate Bridge is
`base/infrastructure`, subtype `bridge`, `wikidata=Q44440`, at the correct
coordinates. Times Square is `base/land_use`, `pedestrian`/`plaza`,
`wikidata=Q11259`. Neither is in `places` at all.

So for the class that actually needs arbitration — landmarks, the explore-site
jump-and-browse case — **the QID is already in the data and needs no matcher,
no ledger and no hand adjudication.** See
`docs/plans/2026-08-05-gold-coverage-in-base-theme.md` for how much of the gold
set that reaches.

## The circularity hypothesis: tested, refuted

The concern was a closed loop — Wikidata editors create items, Foursquare
ingests them to expand its places data, Overture publishes Foursquare, and the
sidecar joins back to the origin, making the match tautological.

Foursquare venue ids are MongoDB ObjectIds, so their first four bytes are a
creation timestamp. Comparing that against each Wikidata item's first revision:

| | |
|---|---|
| Foursquare venue **older** than its Wikidata item | **98 of 100 (98%)** |
| Wikidata item older than the venue | 2 (by one day, and by two months) |
| Median lead of venue over item | **7.2 years** |
| Thai subset | **28 of 28** venue-first, median lead **10.0 years** |

All 200 venue ids decode to plausible timestamps; 165 of 200 were created
2009-2011, Foursquare's check-in era. The actual sequence is the reverse of the
hypothesis:

```
2009-2011  Foursquare users create venues
2020-2021  Wikidata editors create items (the Thai temples natively in Thai)
2021       an editor attaches P1968 -> the decade-old venue id
2026       Overture publishes the Foursquare record; the sidecar joins on it
```

Sample limit: 100 of 200 QIDs resolved a creation timestamp; the rest dropped
under request concurrency. At 98% and 28/28 on the Thai subset the direction is
not in doubt.

## What the audit was actually measuring

`match_method` is `direct_source_wikidata_id` on all 200 rows: **an equality
join on an identifier**. There is no matcher. Every P1968 claim sampled (5 of 5)
carries **zero references**.

So Phase 0's "zero false accepts" would have established that *Wikidata editors'
hand-added, unreferenced P1968 links are accurate*. That is a real property and
worth knowing — but it is a statement about a 7,234-row Wikidata corpus, and it
**would not transfer to any fuzzy matcher**, which is what the sidecar would
need for the ~99.99% of Places no editor has ever touched.

The gate was also over-specified for the use. A wrong link attaches the wrong
fame prior to one POI: a ranking error, bounded and reversible, not output
corruption. The one genuine risk is that fame is heavy-tailed — a false link to
a top-tier QID would let an ordinary POI inherit a landmark's prominence — so
any future revival should gate *fame-weighted*, not on a raw count.

## Two instrument defects, recorded so they are not rebuilt

1. **The frozen SPARQL snapshot scoped labels to one language**
   (`SERVICE wikibase:label { bd:serviceParam wikibase:language "en,[AUTO_LANGUAGE]" }`).
   Wikidata returns the QID string when no label exists in that language. Effects:
   39 Thai rows appeared to have no name overlap when the `th` labels match the
   Overture names **exactly** (`Q100449088` = `วัดลาดบัวหลวง`); three rows showed
   a bare QID and were unjudgeable, though `Q2944944` is
   `fr: centre commercial Carrefour Grand Est`, `Q10428062` is `sv: Bengans`,
   `Q1652448` is `de: Hürth Park`. The single recorded false accept
   (`gqd-2055be5b11b385fa81e25e10`, C.C Carrefour) was a rejection made on
   evidence the instrument withheld.
2. **`no_normalized_name_overlap` is whole-string equality**, not token overlap.
   It flags 134 of 200 rows; 77 of those actually share tokens and 44 are one
   name wholly containing the other (`Universite Laval` / `Laval University`).
   Only 11 rows are same-script with genuinely no shared token.

Also worth knowing if the queue is ever regenerated: the 200 are **25% Thailand**
(50 rows), overwhelmingly `wat` temple records from one editor's campaign, all
provisionally accepted and all cross-script. A precision claim over this queue
would substantially be a claim about Thai temple records.

## Status

The audit is **stopped at 57 of 200** (`accept` 56, `reject` 1,
`needs_more_evidence` 1, `integrity_ok: true`). It is not resumed and the gate is
not met; nothing is marked eligible for prominence. The review instrument
(`scripts/serve_sidecar_phase0_review.py`) is kept because it is generic over the
frozen review-set schema, not because this queue will be finished.

## What would change this

Only one thing: a fame path for **non-landmark commercial POIs**, which base does
not carry. That would need fuzzy GERS-to-QID matching, and this audit produces no
usable prior for it. If that is ever wanted, start from the coverage question
(*how many Places can be linked at all?*), not from a precision audit of a
7,234-row identifier corpus.

Do not reopen P1968 as a Places fame source.
