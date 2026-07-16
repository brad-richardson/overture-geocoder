# Address-format convergence decision

Date: 2026-07-16 / release `2026-06-17.0` / schema `v1.17.0` (Addresses theme is Alpha).

> Bounded, purposive single-box current-release run. Not a statistically
> representative sample. Counts are raw, unweighted sample counts. No R2,
> catalog, shard, Worker, or production state is written.

## Decision

**Converge the division-joined spike INTO the division-free hosted
lookup-safe format.** The hosted `useful_gzip` page format (the 35.50 B/indexed
row baseline) stays the hot record and remains the source of truth for the
response: it already retains raw `address_levels`, display fields, exact
candidate keys, coordinates, IDs, and source locators. The spike's
contribution -- containing-division GERS IDs and an explicit
match-method/confidence byte -- is ADDED as a separate, optional per-page
extension (a page division dictionary + per-row index + one byte), never a
replacement for `address_levels` or `postal_city`. The runtime
point-in-polygon join is eliminated: division containment is materialized
once during the offline build. This is exactly the shape
`docs/places-search-spike.md` predicted as likely safe: retain source
address fields, add separately identified containing-division GERS IDs plus
an explicit match method/confidence.

The measured/modeled separation below justifies keeping the two contexts
distinct rather than collapsing one into the other.

## Sample

- Box `boston-core-ma`: lon [-71.15, -71.05], lat [42.35, 42.4]
- Addresses in box (pre-cap): 189,248; sampled (cap-saturated: true, row cap 40,000): 40,000; classified rows: 40,000
- Division polygons in box: 18 (13 region/county/locality context polygons + taxonomy-only neighborhoods)
- Finer division points in municipality scope: 323 (271 locality/finer-name associations; taxonomy-only)
- DuckDB `1.4.4`; producer commit `65f903ede6c9dc03aa19751e60c41644180edd42`

## Agreement taxonomy

address_levels[0]/[-1] (NFC/ASCII-lowercase/whitespace normalized) compared against the smallest containing region/county/locality division name from a bbox-prefiltered ST_Covers point-in-polygon join; country is a gate. A label matching a containing neighborhood-subtype polygon, or the name of a macrohood/neighborhood/microhood division POINT inside the containing municipality, is decomposed as finer_granularity_neighborhood (a granularity difference, not a conflict); these finer-subtype channels are taxonomy-only and never stored context. address_levels meanings are country-dependent, so first/last are general/specific proxies.

| Bucket | Rows | Share |
|---|---:|---:|
| exact_agreement | 0 | 0.00% |
| normalization_only | 34,262 | 85.66% |
| finer_granularity_neighborhood | 5,642 | 14.11% |
| postal_city_vs_containment | 0 | 0.00% |
| missing_address_levels | 0 | 0.00% |
| point_outside_any_division | 0 | 0.00% |
| country_disagreement | 0 | 0.00% |
| unresolved_disagreement | 96 | 0.24% |
| **total** | **40,000** | **100.00%** |

- Rows with `address_levels`: 40,000
- Rows with a containing division: 40,000
- Rows with a populated `postal_city`: 0
- **Genuine cross-context conflicts: 96 (0.24%)** -- country disagreements plus unresolved disagreements after granularity decomposition.

> postal_city is empty for 40,000 of 40,000 sampled rows, so the postal_city_vs_containment bucket cannot trigger in this sample.
> exact_agreement is structurally unreachable here: the sampled address_levels labels are uppercase while Overture division names are title case, so raw string equality never holds and agreement appears as normalization_only.
> The sample box was chosen to span municipal boundaries and a neighborhood-labeled core, which maximizes granularity-artifact and boundary cases relative to a random box; the genuine-conflict rate is the meaningful headline, not the raw disagreement share.

The agreement-plus-granularity rate (exact + normalization-only +
finer-granularity) shows how often `address_levels` and geometric
containment already describe compatible places; the small genuine-conflict
remainder is precisely why a separately identified GERS ID with an
explicit match method is safer than overwriting the source label.

### Concrete mismatch examples (distinct label shapes; no IDs)

| Category | Rows | Country | AL region | AL locality | Postal city | Cont. region | Cont. locality | Cont. neighborhood | Method |
|---|---:|---|---|---|---|---|---|---|---|
| finer_granularity_neighborhood | 3,009 | US | MA | BRIGHTON |  | Massachusetts | Boston |  | point_in_polygon_interior |
| finer_granularity_neighborhood | 2,577 | US | MA | CHARLESTOWN |  | Massachusetts | Boston |  | point_in_polygon_interior |
| finer_granularity_neighborhood | 44 | US | MA | CHARLESTOWN |  |  | Boston |  | point_in_polygon_interior |
| unresolved_disagreement | 27 | US | MA | SOMERVILLE |  | Massachusetts | Medford |  | point_in_polygon_interior |
| unresolved_disagreement | 21 | US | MA | ARLINGTON |  | Massachusetts | Cambridge |  | point_in_polygon_interior |
| unresolved_disagreement | 18 | US | MA | CHELSEA |  | Massachusetts | Everett |  | point_in_polygon_interior |
| unresolved_disagreement | 17 | US | MA | CAMBRIDGE |  | Massachusetts | Somerville |  | point_in_polygon_interior |
| unresolved_disagreement | 13 | US | MA | SOMERVILLE |  | Massachusetts | Cambridge |  | point_in_polygon_interior |
| finer_granularity_neighborhood | 12 | US | MA | SOUTH BOSTON |  | Massachusetts | Boston |  | point_in_polygon_interior |

## Storage delta vs the 35.50 B/row baseline

Measured on the same 40,000 rows, page_rows=256, independent gzip pages.

| Format | B/indexed row | p50 page bytes | Linear all-473M GB |
|---|---:|---:|---:|
| Lookup-safe baseline (measured here) | 34.144 | 8,714 | 16.15 |
| + division GERS IDs + match byte | 34.455 | 8,801 | 16.30 |
| **delta** | **+0.311** | | |

All GB projections in the table linearize the box-measured 34.144/34.455 B/row values. Applying the same +0.311 B/row delta to the hosted `useful_gzip` reference baseline (35.502976 B/row, measured on the separate hosted reduce range) gives 16.79 GB -> 16.94 GB. Both sit far inside the 40 GB stop gate (addresses share that budget with Places; these are labeled diagnostics, not forecasts). Every stored extended page is verified to decode from its bytes alone (self-describing core-length framing).

> The measured delta is a favorable-case lower bound: the sample box has an atypically small division-polygon set, so few distinct GERS IDs repeat within each page and the page dictionary amortizes to almost nothing. The delta scales with the number of distinct containing divisions per page under global polygon density (boundaries, dense locality/neighborhood fabric, multi-membership).

## Build cost (labeled diagnostic, not a forecast)

- Spatial join: 2.768 s wall on a 4-thread DuckDB session, 2.764 s CPU, over 40,000 points x 18 polygons
- Peak process RSS: 845.0 MB
- Linear all-473M-row wall diagnostic: 9.09 factory-hours (single 4-thread-session wall-clock basis)

> A purely linear wall-clock diagnostic on a 4-thread DuckDB session, NOT a build forecast. It excludes global polygon density, extraction, sort, shuffle, retries, and country-specific work; the measured box has an atypically small polygon set and single region, and the timed join includes the taxonomy-only neighborhood polygons.

## What the hot record carries vs what stays optional

- **Hot record (unchanged 35.50 B/row lookup-safe page):** exact candidate
  key, feature ID, quantized coordinates, number/unit, display fields, raw
  `address_levels` sequence, and source row-group/row locators.
- **Optional per-page division extension (added, measured above):**
  containing region/county/locality GERS IDs via a page dictionary + per-row
  index, plus one match-method/confidence byte, framed behind a uvarint
  core-length prefix so the stored page decodes with no out-of-band
  knowledge. Absent-context rows cost a single zero byte.
- **Not on the hot record:** no runtime point-in-polygon join, no
  overwriting of `address_levels`/`postal_city`, no full division geometry.
  Registry membership must be confirmed before calling any division ID a GERS
  ID in a public response.

## Reproduction

```bash
python3 scripts/experiment_address_format_convergence.py \
  --json-out benchmarks/address-format-convergence-report.json \
  --markdown-out benchmarks/address-format-convergence-report.md
```

## Limitations

- One tiny purposive box in a single US region; not globally representative, and deliberately biased toward municipal-boundary and neighborhood-label cases.
- Stored context covers region/county/locality only; finer subtypes are taxonomy-only channels. Neighborhood polygons are joined where they exist, and macrohood/neighborhood/microhood division points are associated with their containing municipality by name -- a name-level heuristic, not verified neighborhood-polygon containment, because Overture has no polygons for these neighborhoods in this box.
- The sample is cap-saturated: sampled rows equal the row cap and are a deterministic md5(id) subset of the box population.
- The measured storage delta is a favorable-case lower bound; see the storage delta caveat.
- address_levels semantics are country-dependent; first/last are proxies.
- Division-area geometry is current-release; a single release join only.
- Source row-group/row locators are absent from raw addresses and set to zero; the storage delta is unaffected because both variants encode them identically.
- Match confidence is a structural-completeness proxy, not a calibrated probability; Overture division areas carry no per-point confidence.
- No R2 object, catalog, shard, Worker, or production state is written.
