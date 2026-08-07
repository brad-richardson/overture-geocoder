# The PENDING phrase-admission sizing join, computed — 2026-08-07

**Decision, taken by the operator on this evidence: §3.1 is adopted for v5 and
§3.2 is CLOSED.** Do not reopen admission softening without a measurement that
contradicts the cost below.

`2026-08-04-v5-build-readiness.md` §3.1 and §3.2 each carry a "Decision input
PENDING" line, and §3.2 calls the choice between them "the central v5 product
decision". Neither was ever computed. Both are computed here, offline, against
the corpus production serves (Overture `2026-06-17.0`) and the complete local
planet head of that same release.

Probe: `benchmarks/probes/2026-08-07-phrase-admission-sizing-join.py`.
Result: `benchmarks/2026-08-07-phrase-admission-sizing-v1.json`.
No network, no credentials, 33 seconds on the staging workstation.

## 0. The budget the decision was supposed to be sized against does not exist

Both sections say the options "compete for the same 31.7 MB reserve".
`2026-08-04-measurement-apparatus-findings.md` §3 already showed that reserve
gates a **rehearsal fixture** (773,590,640 B against a self-imposed 1 GiB cap ×
0.75), not production. This probe measures a real head instead — the complete
local planet build of the same Overture release, with records and bytes read off
that one build so nothing is mixed across generations:

| | |
|---|---|
| planet head records | 33,604,005 |
| planet head bytes | 5,717,067,235 |
| shards | 4,096 |
| mean shard object | 1,395,768 B |
| bytes per head record | 170.131 |

The head is fetched per token per query as whole R2 objects, so the quantity
that actually costs something is **mean shard bytes**, not a fictional 31.7 MB
of slack.

Note which head this is. `2026-08-04-measurement-apparatus-findings.md` quotes
the *live* head at 5,141,583,720 B, but that is a later generation
(`2026-08-03.0`, carrying bounded phrase admission). Dividing one generation's
bytes by another's record count is a meaningless ratio, so the probe derives
both numbers from the same build and the growth *fractions* below — which are
record ratios — are unaffected by the choice either way.

## 1. Method, and why the replication is trustworthy

The probe replicates in SQL both halves of the producer's gate: the prominence
prior (`places_type_prior_v1.py`, legacy `categories` path) and
`entity_phrase_key`/`normalized_words`
(`places_transform_v1.rs` — 2..=3 words, `e{n}:` + words joined by a space),
then applies the frozen per-token cap of 10.

It is checked against what the real planet build actually emitted:

| | predicted from source | emitted by the build |
|---|---|---|
| `e2:`/`e3:` keys | 2,381,840 | 2,381,564 |
| `e2:`/`e3:` rows | 2,942,617 | 2,892,165 |

**99.988% key agreement.** Keys are the admission unit, so that is the number
that matters; the 1.7% row gap is records the transform drops for other reasons
(no valid point) plus the NFKD/`strip_accents` gap noted below.

## 2. Cost: the two options differ by 34×, so they never competed

| option | new keys | new head records | new head bytes | head growth |
|---|---|---|---|---|
| **§3.1** `e4:` keys, prominent only | 892,625 | 999,815 | 170,099,057 | **+3.0%** |
| **§3.2** admit `prominence_rank == 0` at 2–3 words | 28,325,914 | 34,457,893 | 5,862,339,654 | **+102.5%** |

§3.2 **doubles the head**: 33.6M → 68.1M records, 5.72 GB → 11.58 GB, mean
shard object 1.40 MB → 2.83 MB. Every head read already transfers the whole
object to use ≤10 records, so this doubles head read transfer for every query
on the planet, not only for the queries it helps.

The corpus explains why: 39,010,100 places are non-prominent with a 2–3-word
name, against 2,942,617 prominent ones. Admission is a 13× widening by
construction.

**Bounding it by key rarity does not rescue it.** Admitting non-prominent
records only where the phrase key is *globally unique* still costs 25,527,353
new head records (+76.0%, 4.34 GB) — most 2–3-word POI names are unique, so
rarity is not the discriminator anyone hoped it was:

| admit only keys shared by ≤ | new head records | head growth |
|---|---|---|
| 1 | 25,527,353 | +76.0% |
| 2 | 29,267,230 | +87.1% |
| 3 | 30,652,474 | +91.2% |
| 5 | 31,964,097 | +95.1% |
| 10 | 33,311,860 | +99.1% |
| unbounded | 34,457,893 | +102.5% |

## 3. Yield: what each option claims among the 145 cases still missed

Against the deployed Worker `00bc46c` on both frozen sets — 130 everyday
misses + 15 gold misses at rank 10 — asking whether a record exists near the
gold point whose primary name normalizes to the normalized query, whether it is
`prominence_rank == 0`, and how many words it carries:

| verdict | everyday | gold | total |
|---|---|---|---|
| no name match near gold (claimable by neither) | 108 | 11 | **119** |
| claimed by §3.2 | 18 | 1 | **19** |
| claimed by §3.1 | 2 | 1 | **3** |
| name too long for either lane | 2 | 0 | 2 |
| already admitted, fails for another reason | 0 | 2 | 2 |

§3.1's three are `TECK LEE LRT STATION`, `BUKIT PANJANG MRT STATION`, and the
gold `Casino de Monte-Carlo`. The two already-admitted gold failures are
`Brandenburg Gate Berlin` and `Louvre Museum`, both of which have open
follow-ups elsewhere and neither of which admission touches.

**Cost per claimed case: §3.1 ≈ 57 MB, §3.2 ≈ 309 MB.**

Read against the honest denominator, §3.2's everyday claim is 15 of the 38
*scorable* misses (18 claims less 3 that sit inside the ABSENT quarantine) —
a real share of what is left, which is why the option should not simply be
dropped without recording what it would have bought.

But its constituency is narrow in a way that matters: **11 of the 19 claims are
Mexican registry hotels** (`HOTEL GAMA`, `HOTEL LUA`, `HOTEL PENSIL` …). `hotel`
is a commodity category, so these are non-prominent *by design*, and MX is one
of the strata `2026-08-06-everyday-denominator-rebaseline-v1.json` flags for
population shift. Doubling the planet head to serve one country's registry
hotel names is not a defensible trade.

## 4. Side finding: three quarantined cases are not ABSENT

`everyday-au-41e89cc4…` (Rapha Australia), `everyday-au-2a73f159…`
(DOC Delicatessen), and `everyday-au-9b864485…` (Unreal Christmas Trees) are
inside the 92-case ABSENT quarantine, yet this probe finds a name-matching
non-prominent record within the same radius the quarantine rule uses.

The rebaseline's own equal-blind-rate scenario predicted exactly 3 false
quarantines. That is now **3 measured**, by an independent probe. The scenario
figure stops being an extrapolation and becomes corroborated, and the corrected
denominator should be read as n=111 rather than n=108.

## 5. What was decided on this evidence

1. **§3.1 is adopted for v5**: +3.0% head for 3 of 145 current misses, and it is
   the only lane that can ever reach a 4-word name.
2. **§3.2 is CLOSED, not deferred**, on measured cost — +102.5% head for 19
   claims concentrated in one registry stratum — rather than left open as a
   sanctioned lever it can no longer earn.
3. **The §3.1-vs-§3.2 "competing budget" framing is dead**: they differ by 34×
   against a budget that was never real. Delete the framing from the readiness
   sheet rather than re-sizing it.
4. The v5 byte gate should be restated against **mean shard object bytes**
   (1,395,768 B on this build), which is what a head query actually pays.

## 6. Limits

- **Yield is an upper bound.** A claim means the phrase key would exist and the
  record is refused today. It does not mean the case would rank: scoring is a
  separate step this probe does not measure, and the head cap of 10 still
  arbitrates within a key.
- **Cost is a lower bound on disruption**, not on bytes: it counts head records
  and prices them at the measured planet average of 170.131 B. It does not
  model the head-build DuckDB spill, which is the constraint that actually
  killed a v4 merge at 79%. Doubling the head makes that worse by an unmeasured
  amount.
- **Normalization**: DuckDB has no NFKD, so `strip_accents` stands in. On one
  planet head-candidate shard this agreed with the emitted keys on 21,706 of
  21,765 (99.73%); every disagreement was a compatibility form (styled math
  capitals, fullwidth Latin, `№`) and none changed the word count, which is
  what the gate turns on. The miss-side queries are normalized with real NFKD
  in Python.
- The prominence replication is exact for the legacy `categories` path this
  release uses. A release carrying `taxonomy` instead would need the hierarchy
  branch added before these numbers transfer.
- Nothing here reopens a closed decision: the cap stays at 10, the release move
  stays refuted, the sidecar stays dead.
