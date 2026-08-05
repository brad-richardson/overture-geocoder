# How much of the forward gold set lives in `theme=base`

Measured 2026-08-05 against Overture `2026-06-17.0` (the release production
serves), by `benchmarks/probes/2026-08-05-gold-coverage-in-base-theme.py`.
Evidence: `benchmarks/2026-08-05-gold-coverage-in-base-theme.json`. Read
remotely over HTTPS with bbox pruning — no download.

## Result

| | |
|---|---|
| gold POI cases | 45 |
| found in base under any matching name | 31 |
| **exact name, non-transit** | **14 case rows / 10 distinct entities** |
| exact name but a TRANSIT stop named after the target (excluded) | 12 |
| of the 14: carrying a `wikidata` QID | 9 |
| of the 14: carrying `names.common` | 10 |
| **of the 14: cases `places` cannot serve** | **14 — all of them** |
| not found in the three scanned base types | 14 |

```
Big Ben              base/infrastructure  tower       Q37733713   5 langs   0.002 km
Golden Gate Bridge   base/infrastructure  bridge      Q44440     11 langs   0.046 km
Eiffel Tower         base/infrastructure  pedestrian  -           7 langs   0.039 km
Buckingham Palace    base/infrastructure  pedestrian  -           0 langs   0.183 km
Times Square         base/land_use        pedestrian  Q11259      9 langs   0.092 km
Statue of Liberty    base/land_use        protected   Q359939     1 lang    0.640 km
Singapore Gen. Hosp. base/land_use        medical     Q7522983    5 langs   0.137 km
SickKids             base/land_use        medical     Q3140964    0 langs   0.090 km
Royal Children's     base/land_use        medical     Q7373942    1 lang    0.011 km
Mayo Clinic          base/land_use        medical     -           0 langs   0.049 km
```

**Every one of these is a case the Places head cannot serve** — 12 `EVICTED`,
2 `QUERY_REFUSED` against the release-move probe's verdicts. So this is not
overlap with existing coverage; it is 14 case rows of pure addition, roughly
31% of the gold POI set.

## The transit tier, and why it is excluded

Exact-name matching alone would have reported 26. Twelve of those are transit
stops *named after* the landmark, not the landmark:

```
Brandenburg Gate -> Brandenburger Tor (station)  Colosseum -> Colosseo (metro)
Louvre Museum    -> Louvre (métro)               Sagrada Familia -> (metro)
Harrods          -> Harrods (bus stop)           Raffles Hotel -> (bus stop)
Taj Mahal        -> Taj Mahal (station)          Union Station -> Union Station
```

This is the same failure that forced the `Discovery Times Square` retraction, so
they are tiered rather than counted. **Union Station is the case where the
transit match is the right answer**, which is why the tier is reported and not
dropped — read the headline as 14, or 15 if Union Station is admitted.

## Two probe defects found and fixed before publishing

The first run reported 26 exact matches and 25 additions. Both were wrong.

1. The probe selected `wikidata` only from `infrastructure` and hardcoded `NULL`
   for `land_use` and `land`. All three types carry the column. This also
   corrupted candidate selection, because the ranking prefers a candidate with a
   QID: **Times Square resolved to the subway station Q11265 at 0.269 km instead
   of the plaza Q11259 at 0.092 km**, purely because the plaza's QID had been
   blanked by the query.
2. Transit contamination, above.

## Limits

* **`theme=buildings` (276 GB) was not scanned.** The 14 not-found cases are
  Empire State Building, Sydney Opera House, Seattle Central Library, KaDeWe,
  Noma, Cafe de Flore, Din Tai Fung, Apple Marunouchi, Hotel Sacher, The Plaza,
  Pike Place Chowder, Burton Barr Central Library, General Post Office. Several
  are plainly buildings and several are plainly ordinary POIs. Read the bucket as
  "not in the three scanned base types", never as "not in Overture".
* `base/water` and `base/land_cover` were not scanned; no gold case is a water
  or land-cover feature.
* Coordinates are bbox centroids. Right for a bridge, potentially far from the
  entrance a user means for a sprawling `land_use` polygon.
* **Presence is not servability.** Nothing indexes base today, and the producer
  work to do so is not scoped here: base features are polygons and lines where
  the places path assumes a point, and they carry `subtype`/`class` rather than
  Overture place categories, so `prominence_rank` needs a mapping for them.

## Why this matters beyond coverage

9 of the 14 carry a `wikidata` QID and 10 carry `names.common` — where `places`
carries `names.common` on exactly **0 of 75,642,289** records. So indexing base
would supply, in one change, both the missing landmark coverage and the entity
fame signal that the P1968 sidecar could not
(`docs/plans/2026-08-05-sidecar-p1968-dead-end.md`).
