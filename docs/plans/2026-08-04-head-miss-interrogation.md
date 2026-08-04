# 2026-08-04 — Why benchmark misses return nothing, answered against the index

The everyday-POI set says 106 of 130 misses return NOTHING, and re-scoring
proved that is retrieval rather than scoring (at most 5 cases were
name-unscorable). What it could not say is WHICH retrieval problem, because a
deployed Worker answers "empty" identically for three unrelated causes.

The local planet run made that answerable offline. `scripts/interrogate_head_for_misses.py`
probes every case against the planet source parquet and the planet head
candidate packs — no deploy, no rebuild, no live requests — and splits it.

## The split

| verdict | misses | share | what fixes it |
|---|---|---|---|
| **ABSENT** | 92 | 70.8% | nothing — not in Overture near the point under that name |
| **NOT_ADMITTED** | 15 | 11.5% | a rebuild, and only this class |
| **IN_HEAD** | 23 | 17.7% | **the Worker, fix-forward, no rebuild** |

Calibration, which is what makes the ABSENT number trustworthy: the same probe
run against the 70 cases the system provably solves finds **68 of 70 IN_HEAD
(97.1%)**. It is not blind to entities that are there.

Three consequences.

**The benchmark's real ceiling is ~0.54, not 1.0.** 92 of 200 cases cannot be
won by any amount of work. Against a ceiling of (70 + 15 + 23) / 200, today's
0.350 is 65% of achievable, not 35% of achievable. Every "we have gained little"
reading this session was measuring against the wrong denominator.

**The largest actionable class needs no rebuild.** IN_HEAD (23) is bigger than
NOT_ADMITTED (15). The nearest available win is in serving, not construction.

**v5's admission scope is at most 15 cases.** That is the honest ceiling for a
rebuild against this benchmark, and it should size the decision that the
`e4:`-versus-softening framing was previously guessing at.

## The IN_HEAD mechanism, traced to a line

16 of the 23 IN_HEAD misses returned EMPTY. Six share one cause, and it is
exact.

Overture stores Singapore's stations as **`Geylang Bahru MRT`** — no "Station" —
with category `train_station`. The gold query is `GEYLANG BAHRU MRT STATION`.

1. Four tokens, so the head lane refuses (`HEAD_QUERY_TOKEN_CAP = 3`).
2. The prefix-head fallback splits: head tokens `[geylang, bahru, mrt]`,
   dropped `[station]`.
3. `places_construction_head_records` composes the entity phrase key
   `e3:geylang bahru mrt`, which **exists in the head**, and returns the right
   record. Retrieval succeeds.
4. `retain_records_proving_dropped_tokens` then requires the record's display
   tokens to contain `station`. Display tokens are
   `query_terms(primary_name) ∪ query_terms(category)` =
   `{geylang, bahru, mrt, train_station}`.
5. `normalized_words` treats `_` as a word character
   (`places_pages.rs:106`), so `train_station` is ONE token and never yields
   `station`. The proof fails and the correct record is discarded.

**The right answer is retrieved and then thrown away**, by a token that is
present inside a compound category. This is a defect in the fallback shipped
earlier today, not in construction.

Two coupled fixes are needed for these six cases to score:

- **Serving**: let the dropped-token proof match a component of a compound
  category token. Narrow, Worker-only, no rebuild.
- **Scoring**: even served, `Geylang Bahru MRT` fails exact-name equality
  against `GEYLANG BAHRU MRT STATION`. Containment accepts it
  (3 of 4 tokens, both floors cleared), so `--name-match containment` has to be
  part of measuring this class. That reframes the earlier "+1 case" containment
  result: containment was measured against a run where these records were never
  returned at all.

## Limits of this measurement

- It probes head CANDIDATES, which are pre-merge. The merge applies
  `result_cap` (10) per token afterwards, so a candidate can exist and still
  lose its slot. IN_HEAD means "admitted", not "served"; cap eviction is folded
  into IN_HEAD and needs the merged head to separate out.
- NOT_ADMITTED is an upper bound. Containment matching accepts
  `MoneyMax Pawnshop - Marsiling MRT Station` as a corpus match for
  `MARSILING MRT STATION` — a different business named after the station. At
  least 2 of the 15 are that shape; `Australian Diamond Company`,
  `Moko Select` and `Spencer Street Pharmacy` are genuine.
- ABSENT means absent near the expected point under a matching name. A badly
  geocoded gold coordinate would look identical.
- Probe box is ±0.02° latitude (~2.2 km), deliberately wider than the 1 km
  scoring tolerance so a near miss reads as found rather than inflating ABSENT.

## ABSENT by country

`CO 20, AU 18, MX 18, JP 15, KR 13, TW 7, SG 1`

Colombia is 20 of 20 — the entire Bogotá health-post stratum, consistent with
all three geocoders scoring zero on it in every round. Those cases are
measuring a registry that Overture does not carry, and they should be
quarantined rather than counted as failures.

---

# Both sets, and a correction to the numbers above

The section above covered only the 200-case everyday-POI set. Running the gold
55 as well changed both the method and the conclusion.

## Two method fixes the gold set forced

**The probe box now follows each case's own tolerance** (2x tolerance, floored
at 1 km). A fixed +/-2.2 km box was fine for the everyday set, where every case
is 1.0 km, but the gold set ranges 0.25 km to 25 km and a 25 km locality case
would have been reported ABSENT for something plainly present.

**Locality cases are excluded.** 10 of the 55 gold cases expect a `locality`,
which the DIVISION lane serves, not the Places head. Probing the head for them
would manufacture ABSENT verdicts. All 10 are hits, so the division lane is not
implicated in any gold miss.

## The correction: containment inflates the verdicts

Chasing a gold result exposed a flaw in the first run. `Times Square` was
reported IN_HEAD -- but `e2:times square` has only **6 rows globally**, well
under the cap of 10, and **none of them is Manhattan's Times Square**; the most
prominent sits in Brooklyn, 8.7 km away. What the probe actually matched was
`Discovery Times Square`, a museum named after the square.

So containment matching accepts entities merely NAMED AFTER the target, and
counting those as "the target is indexed" turns "the landmark is missing" into
"the serving path is broken" -- which sends work to the wrong place. Verdicts
are now split into an exact-name tier and a containment-only tier, and anything
resting on containment alone is flagged for eyes rather than trusted.

Containment-only is not automatically wrong: `Upper Thomson MRT` for
`UPPER THOMSON MRT STATION` is the same entity under a shorter name, while
`MoneyMax Pawnshop - Marsiling MRT Station` and `AKA Times Square` are not.
The flag means "unresolved", not "false".

## Both sets, exact-name tier only

| | gold (45 head-lane) | everyday (200) | total |
|---|---|---|---|
| misses | 15 | 130 | 145 |
| ABSENT | 1 | 92 | **93** |
| NOT_ADMITTED, exact | 0 | 9 | **9** |
| IN_HEAD, exact | 9 | 13 | **22** |
| containment-only, unresolved | 5 | 16 | 21 |

Calibration holds on both: 29 of 30 gold known hits (96.7%) and 68 of 70
everyday (97.1%) are found IN_HEAD.

**Confidently actionable work is 22 serving-side cases against 9 that need a
rebuild — roughly 2.4 to 1.** That ratio, not the earlier 23-vs-15, is the one
to plan against, and it points the same way: the nearest wins are in the Worker.

## The two sets fail for opposite reasons

The everyday set is dominated by ABSENT (92 of 130, 70.8%) — its cases largely
name entities Overture does not carry. The gold set is dominated by IN_HEAD
(13 of 15) with only ONE ABSENT — its entities are almost all indexed, and the
failures are in serving them.

They are therefore not interchangeable evidence, and a single headline number
across both would hide this. Concretely: the everyday set cannot measure ranking
work (its recall@10 equals recall@1), while the gold set can and is where the
three unshipped ranking changes have their target.

## Gold misses are wrong-instance, not missing

Every gold IN_HEAD miss returns the RIGHT NAME at the WRONG PLACE, and none has
a candidate inside tolerance:

```
Times Square        (tol 2.0 km)  ->  777 km, 15992 km, 5356 km, 8.7 km, 5361 km
Golden Gate Bridge  (tol 2.0 km)  ->  13510 km, 7.2 km, 7.1 km, 9114 km, 12174 km
Louvre Museum       (tol 2.0 km)  ->  4.4 km, 5.1 km, 3.3 km, 25.2 km, 6.8 km
Brandenburg Gate    (tol 2.0 km)  ->  25.8 km, 1417 km
Harrods London      (tol 1.0 km)  ->  Harrods Furniture Depository, 4.8 km
```

This is a homonym-arbitration problem, not a retrieval one: the head holds many
same-named POIs worldwide and nothing selects the famous instance for a
no-proximity query. It is NOT cap eviction — `e2:times square` has 6 rows
against a cap of 10 — so raising the cap would not help. Note also that the
landmark itself is often absent while derived businesses are present, which is
a construction-side admission question rather than a serving one.

---

# CORRECTION: the headline above is wrong

A follow-up diagnosis of the remaining IN_HEAD cases invalidated this doc's
central claim. Three findings, all verified directly rather than accepted.

## 1. The interrogation measured a DIFFERENT RELEASE than the benchmark

Production serves `overture_release 2026-06-17.0`. The head-candidate packs
probed here are from the local **2026-07-22.0** build. So every verdict compares
a July index against a June measurement.

This is a methodological flaw in the whole report, not a footnote. At least one
case is explained by it outright: KR `하나효요양병원` exists in 2026-07-22.0 as a
single head row at global rank 1 (`prominence_rank 102`), and returns empty from
the deployed June index even with proximity at the gold point. It is a
measurement artifact and should not be counted as a serving defect.

## 2. "IN_HEAD" mostly means CAP-EVICTED, which needs a rebuild

The limitation was stated above — candidates are pre-merge, and `result_cap`
(10) is applied afterwards — and then the headline ignored it. Measured:

```
token 'habana' posting, 452 rows, cap keeps 10
  rank  1  Aeropuerto de La Habana            prominence_rank 230
  rank  3  Museo Napoleónico de La Habana     prominence_rank 217
  rank 58  Hotel Habana                       prominence_rank 0   <- wanted
e2:hotel habana rows planet-wide: 0
```

`hotel` is a commodity category, so `prominence_rank = 0` sorts it below every
monument, airport and museum in the same posting, and the entity-phrase key is
never emitted at all (`entity_phrase_key` returns `None` at rank 0). Ten cases
share this shape — five empty, five wrong-answer. The wanted rows are **not in
the head artifact bytes**, so no Worker change can retrieve them.

**Fix side: construction. A rebuild.**

## 3. `alt_names` inflated IN_HEAD by at least four

The four Hong Kong Chinese-language queries (`聖母醫院`, `屯門醫院`,
`麥理浩復康院`, `香港兒童醫院`) were counted IN_HEAD because the probe accepts a
case's `alt_names`, and each carries the English name Overture actually holds.
Under the string that was QUERIED they are absent — the source records have an
English `names.primary` and a null `names.common`, so no Chinese string exists
to index. One match was outright spurious: `香港兒童醫院` matched
`Kai Tak Promenade (Hong Kong Children's Hospital Section) Children's
Playground`.

**Fix side: neither. No rebuild or Worker change reaches a name the source does
not carry.**

## Revised accounting for the 23 everyday IN_HEAD cases

| | count | fix side |
|---|---|---|
| cap eviction at `prominence_rank = 0` | 10 | **construction (rebuild)** |
| Chinese query, English-only record | 4 | neither — source data |
| deployed-release skew | 1 | neither — re-measure after promotion |
| correct feature served, scorer rejected the name form | 2 | benchmark scoring |
| compound-category proof (Singapore stations) | 6 | Worker — **fixed today** |

So the true Worker-fixable count on the everyday set is **6, not 23**, and the
claim that "the largest actionable class needs no rebuild" does not survive.
The largest actionable class is cap eviction, and it needs one.

What does survive: ABSENT at 92 of 130 is unaffected by all of this — those
cases have no candidate at any rank — so the ceiling argument (~0.54, and 0.350
being ~65% of achievable) still stands. The gold set's 9 exact-tier IN_HEAD
cases are also untouched by mechanisms 2-4, but they are wrong-instance
homonyms, which is arbitration and likewise construction-shaped.

## Also recorded: CJK query tokenization is whole-run-only

The index emits, per CJK run, the whole word plus every character bigram
(`places_transform_v1.rs:250-283`). The Worker's query tokenizer emits whole
words only — `query_terms` is `normalized_words`, and the bigram-producing
`tokenize_query` is `#[cfg(test)]` and dead on the serving path. So a CJK query
of three or more characters can only match a record whose normalized run is
exactly the query, and can never reach a longer name. No case in this set is
explained by it alone, but it will bite other CJK queries. Fix side is Worker;
the read-cost of adding bigram clauses is not determined.

---

# Post-deploy: the compound-category fix helps ONE case, not six

Measured against production after deploy (`geocoder_build 2026-08-03.0`,
`overture_release 2026-06-17.0`).

`GEYLANG BAHRU MRT STATION` now resolves. Debug metadata confirms the intended
path fired end to end:

```
places_prefix_head_fallback: {probe_query: "geylang bahru mrt",
                              verification: "display_fields",
                              verified_tokens: ["station"]}
```

The other five stations in the class still return empty, and the reason is that
**Overture's category assignment for them is inconsistent**:

| entity | category | proves "station"? |
|---|---|---|
| Geylang Bahru MRT | `train_station` | yes — fixed |
| Upper Thomson MRT | `transportation` | no |
| Marine Parade MRT | `tours` | no |
| Bukit Panjang | `structure_and_geography` | no |
| Labrador Park | `park` | no |

The fix relaxes the dropped-token proof to accept a COMPONENT of a compound
token, so it only reaches a record whose category literally contains the
dropped word. `transportation` does not contain `station` as a component (it is
a substring, and substrings are deliberately not proof), and `tours` and `park`
carry no relevant evidence at all.

So the earlier claim that this accounted for six cases was wrong: it accounts
for **one**. The remaining five have no evidence anywhere in the record that
proves the queried tail, and closing them would need either a category-synonym
map (which `tours` and `park` defeat anyway) or abandoning the
prove-every-dropped-token rule, which is a deliberate fail-closed contract. On
the current source data they are better classified with the source-quality
cases than with serving defects.

Revised Worker-fixable count on the everyday set: **1**, not 6 and not 23.

One incidental finding worth keeping: the first post-deploy probe returned EMPTY
for a query that in fact worked. Adding `debug=1` changed the URL and returned
the correct answer. That was a stale EDGE-CACHED response, so any post-deploy
verification must cache-bust or it will measure the previous build.
