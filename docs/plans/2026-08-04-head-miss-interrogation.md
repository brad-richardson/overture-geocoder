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
