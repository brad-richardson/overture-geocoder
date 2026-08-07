# Scope: the theme=base landmark import — 2026-08-07

`2026-08-06-places-failure-mode-review.md` §6 calls this "the only living fame
lever" since the P1968 sidecar closed, citing **"14/45 gold case rows, all
currently unservable, with QIDs on 9/14"**, and leaves it unscoped. This is the
scope. Two parts of that citation need correcting first, and the correction
changes what the work is *for*.

Probe: `benchmarks/probes/2026-08-07-base-theme-import-inventory.py`.
Result: `benchmarks/2026-08-07-base-theme-import-inventory-v1.json`.

## 1. Two corrections, before any scoping

### The "14 rows" are 10 landmarks, and production already serves 6 of them

| | |
|---|---|
| exact base name matches (rows) | 26 |
| …that are transit stops **named after** the landmark | 12 |
| …non-transit rows (the cited "14") | 14 |
| distinct landmarks behind those 14 rows | **10** |
| already answered by production today | **6** |
| **incremental gold landmarks** | **4** |

The four: **Golden Gate Bridge** (`bridge/bridge`, QID, 11 common names),
**Times Square** (`pedestrian/plaza`, QID, 9), **The Royal Children's Hospital**
(`medical/hospital`, QID), and **Mayo Clinic** (`medical/clinic`).

Rows are not landmarks — the gold set carries `name` and `name_locality`
variants of the same entity — and "unservable" was read off a base-coverage
probe that never asked production. Six of the ten are answered today.

### The transit classes are a trap, not coverage

Twelve of the 26 exact matches are a stop named after the landmark:

- `Colosseum` → `transit/subway_station` (the Colosseo metro)
- `Brandenburg Gate` → `transit/subway_station`
- `Harrods` → `transit/bus_stop`
- `Louvre Museum` → `transit/ferry_terminal`
- `Taj Mahal` → `transit/subway_station`
- `Sagrada Família` → `transit/stop_position`

Admitting transit classes would make `Harrods` return a **bus stop**. The
earlier probe already tiered these separately and said so; the review's headline
dropped the distinction. **Any admission set must exclude `subtype = transit`**,
and that exclusion is the single most important line in this scope.

## 2. What is actually in base

Named rows in the three scanned types, `2026-06-17.0`:

| | rows |
|---|---|
| base/infrastructure | 11,339,715 |
| base/land_use | 4,824,790 |
| base/land | 1,634,963 |
| **total named** | **17,799,468** |

Of those, 810,039 carry a wikidata QID (4.6%) and **2,602,578 carry
`names.common` (14.6%)**. Places carries `names.common` on **exactly 0** of
75,642,289 records, so base is the only place in the corpus where cross-language
names exist at all. That is the most durable reason to look here, and it has
nothing to do with fame.

## 3. Proposed admission set

Derived from where the genuine (non-transit) gold landmarks live, plus the
natural-feature classes users ask for by name. Bus stops, bridges' near-duplicate
scale, and residential land use are the reason this must be class-scoped rather
than "import base".

| class | rows | QID | names.common |
|---|---|---|---|
| infrastructure `bridge/bridge` | 2,544,617 | 72,447 | 631,407 |
| land `physical/peak` | 678,772 | 137,480 | 101,837 |
| land_use `park/park` | 570,875 | 34,938 | 46,616 |
| land_use `cemetery/cemetery` | 247,070 | 32,779 | 16,029 |
| infrastructure `pedestrian/artwork` | 198,352 | 16,828 | 22,235 |
| land `land/islet` | 127,451 | 14,391 | 16,705 |
| land_use `medical/hospital` | 72,953 | 8,003 | 11,816 |
| land_use `pedestrian/plaza` | 67,518 | 3,741 | 8,000 |
| land `land/island` | 59,758 | 19,027 | 11,601 |
| land `physical/saddle` | 52,010 | 4,736 | 9,883 |
| land_use `education/college` | 30,934 | 3,653 | 4,912 |
| land_use `education/university` | 30,591 | 8,032 | 7,733 |
| land_use `medical/clinic` | 11,732 | 179 | 1,339 |
| land_use `protected/national_park` | 7,876 | 4,223 | 2,512 |
| land `physical/volcano` | 3,172 | 1,167 | 767 |
| infrastructure `tower/bell_tower` | 3,388 | 651 | 580 |
| **total** | **4,707,069** | 362,275 (7.7%) | 893,972 (19.0%) |

**Upper-bound head cost: +4,707,069 records, 800,816,151 B, +14.0%** — assuming
one head record per feature and no cap eviction, so the real figure is lower.
**Bridges are 54% of it**; dropping them gives 2,162,452 rows and **+6.4%**.

For calibration against decisions already taken: `e4:` phrase keys were adopted
at +3.0%, apostrophe folding measured at +1.98%, and §3.2 admission softening was
closed at +102.5%.

## 4. The two shapes this could take, and only one is scoped here

**(A) Import base features as servable POI records.** New records in the existing
places family, keyed by their own GERS ids. This is what makes Golden Gate
Bridge, Times Square, peaks and islands answerable. It is a producer change of
the same kind as any admission change and rides a v5 generation.

**(B) Use base as a fame/prominence signal for existing Places records** — join a
Places record to its base twin and lift `prominence_rank` from the QID. This is
**cross-theme entity resolution**, which is what killed the P1968 sidecar: the
join key would be spatial-plus-name, weaker than the QID→GERS mapping already
refuted, and `2026-08-05-sidecar-p1968-dead-end.md` should be read before anyone
proposes it again. **Not scoped here, and not recommended.**

So: this is a **coverage** lever for feature classes Places does not carry —
bridges, peaks, islands, plazas, parks — and a **cross-language names** lever.
It is not a fame lever, and the review's framing of it as the surviving answer to
the homonym class does not survive contact with the numbers. The homonym class
still has no open mechanism.

## 5. Risks and one-way doors

- **Duplicates with Places.** The Eiffel Tower, Big Ben and Statue of Liberty
  exist in *both* themes. An import adds a second record for entities already
  served, and duplicate collapse is an open, unscheduled item. Six of the ten
  gold landmarks above are exactly this case.
- **Centroids.** Coordinates are bbox centroids. Right for a bridge, wrong for a
  sprawling `land_use` polygon whose entrance is kilometres from its middle.
- **theme=buildings is unscanned** (276 GB). Several landmarks are plausibly
  buildings, so absence here is never absence from Overture.
- **GERS churn.** Measured source-assignment stability between `2026-06-17.0` and
  `2026-07-22.0` is 94.1%; imported ids inherit that.
- **Not a one-way door.** These are records inside the existing places family,
  not a new family, so no permanent decode obligation is created — unlike the
  street layer.

## 6. What to measure before committing

1. **Duplicate rate**: of the admitted 4.7M, how many have a Places record with
   the same normalized name within 200 m. That decides whether the import needs
   collapse first, and it is a local join, not a rebuild.
2. **The real head cost after cap eviction**, rather than the upper bound above.
3. **A gold/everyday delta**: the 4 incremental landmarks are a thin case for a
   generation on their own. The natural-feature classes (peaks, islands, plazas)
   are untested by either frozen set, so an import justified on them needs a
   stratum first — the same instrument-before-mechanism rule the proximity wave
   followed.
4. **Whether `names.common` is worth more than the records.** 893,972 admitted
   rows carry cross-language names and Places has none; a names-only join into
   existing records may be the cheaper half of the whole idea.

## 7. Limits

- Class counts are from the three scanned base types at `2026-06-17.0` and are
  denominators, not evidence that any given class is searched for.
- The admission set is a proposal derived from 10 gold landmarks plus judgement
  about natural features. It is exactly the thing to argue about; the per-class
  table exists so that the argument has numbers.
- The gold delta is 45 POI cases. Four incremental landmarks out of ten is a
  small sample, and "production serves it today" was read from the frozen
  coverage artifact, not re-probed live for this document.
