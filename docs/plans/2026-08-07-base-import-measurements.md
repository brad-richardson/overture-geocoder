# The base-import measurements, taken — 2026-08-07

`2026-08-07-base-theme-landmark-import-scope.md` §6 listed four things that had
to be measured before the theme=base import could be committed. **All four are
now measured**, offline except for one 114-request benchmark run. Three of the
four move the decision:

- the leading risk was mispriced — duplicates are **5.5%**, not a blocker;
- the head cost was overstated by 3× — **+4.8%**, not the +14.0% upper bound;
- the "names-only join is the cheaper half" hypothesis is **refuted** — it
  reaches 2.8% of the cross-language names.

| probe | result |
|---|---|
| `2026-08-07-base-import-measurements.py` | `benchmarks/2026-08-07-base-import-measurements-v1.json` |
| `2026-08-07-build-natural-feature-cases.py` | `benchmarks/natural-feature-cases-v1.json` |
| stock runner, `--semantic-scoring` | `benchmarks/2026-08-07-natural-feature-baseline-v1.json` |

## 1. §6.1 — the duplicate rate is 5.5%, not the leading risk

The scope put duplicates at the top of the risk register, because six of the ten
gold landmarks base would "add" are already served from Places. Measured over
the full admission set:

| radius | duplicate base rows | rate |
|---|---|---|
| 50 m | 171,737 | 3.65% |
| **200 m** | **258,004** | **5.48%** |
| 500 m | 276,677 | 5.88% |

**94.5% of the admission set has no same-name Places record within 200 m.** The
import is overwhelmingly additive.

Where the duplicates concentrate is the useful part — it is not uniform, and it
tracks exactly the classes Places already covers as POIs:

| class | admitted | duplicates @200 m | rate |
|---|---|---|---|
| `medical/hospital` | 72,953 | 17,239 | **23.6%** |
| `land_use park/park` | 570,875 | 109,835 | **19.2%** |
| `education/college` | 30,934 | 5,880 | 19.0% |
| `education/university` | 30,591 | 5,777 | 18.9% |
| `medical/clinic` | 11,732 | 2,152 | 18.3% |
| `physical/peak` | 678,772 | 63,321 | 9.3% |
| `pedestrian/plaza` | 67,518 | 6,020 | 8.9% |
| `cemetery/cemetery` | 247,070 | 17,979 | 7.3% |
| `bridge/bridge` | 2,544,617 | 21,425 | **0.8%** |
| `land/islet` | 127,451 | 1,441 | 1.1% |
| `land/island` | 59,758 | 755 | 1.3% |
| `pedestrian/artwork` | 198,352 | 3,477 | 1.8% |

Hospitals, universities, parks and clinics are the overlap — Places carries them
as POIs already. Bridges, islands, islets and artwork are almost pure addition.
**This is an argument for admitting by class, and the per-class rate is how to
choose.**

The figure is a **lower bound**: it counts exact normalized-name equality, so
`Golden Gate Bridge` in one theme and `Golden Gate Br` in the other are two
records here, not one.

## 2. §6.3 — the natural-feature stratum exists, and it is measured

114 cases, 20 countries, six classes (peak, volcano, island, bridge, square,
park). **Gold is independent of both sides**: names and coordinates are Wikidata
labels and `P625` coordinates, which is neither the system under test (Overture)
nor a compared provider (Nominatim and Photon derive from OpenStreetMap). Every
case carries its QID.

Each case also carries two offline controls, which is what lets the stratum size
the import instead of merely scoring the system:

| control | meaning | cases |
|---|---|---|
| `control_places` | a name-matching Places record within tolerance — already servable in principle | 28 |
| `control_base` | a name-matching record in the admission set — what the import would add | 28 |
| in both | | 9 |
| **base only** | **exactly what the import adds** | **19** |
| in neither | absent from Overture in the scanned types | **67** |

### Production today, against build `2026-08-03.0`

**15/114 at rank 1, 20/114 at rank 10, zero errors.**

Broken down by control, the result is exactly as clean as an instrument can be:

| stratum | cases | found at 10 |
|---|---|---|
| in Places | 28 | **20** |
| base only | 19 | **0** |
| in neither | 67 | **0** |

Every hit comes from the 28 Places-backed cases — **71% of today's ceiling** —
and nothing indexes base, so the base-only column is zero by construction rather
than by accident. That is the control working.

**The import raises the achievable ceiling on this stratum from 28 to 47
(+68%).** It does not touch the 67 cases absent from Overture entirely, which is
59% of the stratum and the same shape as the everyday tripwire's registry-name
problem — measured up front this time rather than discovered months later.

## 3. §6.2 — the real head cost is +4.8%, not +14.0%

The scope's +14.0% assumed one head record per admitted feature and no eviction.
Applying the frozen per-token cap of 10 against the **actual head input** of the
local planet build:

| | |
|---|---|
| head records today, ordinary tokens | 30,841,082 |
| after the import | 32,440,770 |
| **new head records** | **1,599,688** |
| new head bytes | 272,155,770 |
| **growth** | **+4.76%** |
| tokens that would exist only because of base | 705,230 |

The cap absorbs two thirds of the upper bound. The method is checkable: the
today-figure of **30,841,082 reproduces exactly** the ordinary-token head record
count recorded for the hosted planet run, which is what makes the after-figure
worth trusting.

For calibration against decisions already taken: `e4:` keys adopted at +3.0%,
apostrophe folding at +1.98%, §3.2 admission softening closed at +102.5%.

## 4. §6.4 — a names-only join is NOT the cheaper half

The hypothesis was that most of base's cross-language names attach to entities
Places already has, so a join could take the names without importing records.

| | |
|---|---|
| admitted rows carrying `names.common` | 893,972 |
| Places records with a base twin within 200 m | 255,478 |
| **Places records that would gain cross-language names** | **25,026** |
| **reach of a names-only join** | **2.8%** |

**Refuted.** The cross-language names live overwhelmingly on base features that
have no Places twin — bridges, islands, peaks — so they arrive only if the
records do. If cross-language names are the goal, the import is the mechanism.

## 5. What this changes about the scope

- **The leading risk was mispriced.** Duplicates are 5.5%, concentrated in five
  civic classes. Duplicate collapse is not a precondition for the import; it is
  a precondition for admitting *hospitals, universities, parks and clinics*.
- **The import now has a measured coverage number** rather than four gold
  landmarks: +19 of 114 natural-feature cases, +68% on the achievable ceiling.
- **The cheapest defensible first cut** is the low-duplicate, high-addition half:
  bridges, islands, islets, artwork, peaks. That is 3,608,950 rows at a **2.5%**
  combined duplicate rate, and it excludes every class where Places already has
  a POI record.

## 6. Where the import now stands

Every §6 precondition is measured, and the shape of a defensible proposal is:

- **+4.8% of the planet head**, between apostrophe folding (+1.98%) and nothing
  else on the board — an order of magnitude below the §3.2 softening that was
  closed at +102.5%;
- **+19 of 114 natural-feature cases**, raising that stratum's achievable ceiling
  from 28 to 47;
- **5.5% duplicate rate**, concentrated in five civic classes that can simply be
  left out of a first cut;
- **the only cross-language names in the corpus**, reachable no other way.

What it still does NOT do: it is not a fame lever, it does not touch the homonym
class, and it leaves 59% of the natural-feature stratum unreachable because those
entities are absent from Overture in the scanned types.

The remaining unmeasured input is not on this list: the import's effect on the
two frozen sets is unknown and unknowable offline, so it needs the paired gate
like any other change.

## 7. Limits

- The stratum queries **English Wikidata labels**. A local-language query is a
  different measurement this stratum does not make, and for the non-English
  countries in the set that is a real gap in what it can say.
- Wikidata coordinates are a summit for a peak and a centroid for an island,
  which is why tolerance varies by class (1–10 km). A generous tolerance makes
  the controls generous too.
- Sampling took the endpoint's first `--pool` rows per (class, country) and
  hash-ranked them. The pool is not stable across Wikidata edits, so the case
  file is frozen and committed rather than rebuilt per run.
- 114 cases across six classes is 19 per class; per-class rates from this
  stratum are directional, not precise.
- The duplicate join uses bbox centroids on both sides. For a sprawling
  `land_use` polygon the centroid can sit far from the entrance a user means,
  which makes 200 m a strict test for exactly the classes that overlap most.
