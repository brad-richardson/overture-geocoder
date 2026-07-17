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

- Box `tokyo-core-jp`: lon [139.7, 139.85], lat [35.62, 35.75]
- Addresses in box (pre-cap): 35,042; sampled (cap-saturated: false, row cap 40,000): 35,042; classified rows: 35,042
- Division polygons in box: 1,308 (42 region/county/locality context polygons + taxonomy-only neighborhoods)
- Finer division points in municipality scope: 8,242 (3,574 locality/finer-name associations; taxonomy-only)
- DuckDB `1.4.4`; producer commit `f9d2eb8`

## Agreement taxonomy

address_levels[0]/[-1] (NFC/ASCII-lowercase/whitespace normalized) compared against the smallest containing region/county/locality division name from a bbox-prefiltered ST_Covers point-in-polygon join; country is a gate. A label matching a containing neighborhood-subtype polygon, or the name of a macrohood/neighborhood/microhood division POINT inside the containing municipality, is decomposed as finer_granularity_neighborhood (a granularity difference, not a conflict); these finer-subtype channels are taxonomy-only and never stored context. address_levels meanings are country-dependent, so first/last are general/specific proxies.

| Bucket | Rows | Share |
|---|---:|---:|
| exact_agreement | 34,997 | 99.87% |
| normalization_only | 0 | 0.00% |
| finer_granularity_neighborhood | 0 | 0.00% |
| postal_city_vs_containment | 0 | 0.00% |
| missing_address_levels | 0 | 0.00% |
| point_outside_any_division | 0 | 0.00% |
| country_disagreement | 0 | 0.00% |
| unresolved_disagreement | 45 | 0.13% |
| **total** | **35,042** | **100.00%** |

- Rows with `address_levels`: 35,042
- Rows with a containing division: 35,042
- Rows with a populated `postal_city`: 0
- **Genuine cross-context conflicts: 45 (0.13%)** -- country disagreements plus unresolved disagreements after granularity decomposition.

> postal_city is empty for all 35,042 sampled rows, so the postal_city_vs_containment bucket cannot trigger in this sample.
> Raw label equality was observed for 34,997 rows; normalization-only agreement is reported separately.
> Box 'tokyo-core-jp' is purposive rather than random; its conflict rate must not be generalized beyond this bounded sample.

The agreement-plus-granularity rate (exact + normalization-only +
finer-granularity) shows how often `address_levels` and geometric
containment already describe compatible places; the small genuine-conflict
remainder is precisely why a separately identified GERS ID with an
explicit match method is safer than overwriting the source label.

### Concrete mismatch examples (distinct label shapes; no IDs)

| Category | Rows | Country | AL region | AL locality | Postal city | Cont. region | Cont. locality | Cont. neighborhood | Method |
|---|---:|---|---|---|---|---|---|---|---|
| unresolved_disagreement | 8 | JP | 東京都 | 豊島区 |  | 東京都 | 文京区 | 千石四丁目 | point_in_polygon_interior |
| unresolved_disagreement | 3 | JP | 東京都 | 新宿区 |  | 東京都 | 港区 | 元赤坂二丁目 | point_in_polygon_interior |
| unresolved_disagreement | 2 | JP | 東京都 | 新宿区 |  | 東京都 | 文京区 | 関口一丁目 | point_in_polygon_interior |
| unresolved_disagreement | 2 | JP | 東京都 | 中央区 |  | 東京都 | 千代田区 | 丸の内一丁目 | point_in_polygon_interior |
| unresolved_disagreement | 2 | JP | 東京都 | 港区 |  | 東京都 | 渋谷区 | 恵比寿三丁目 | point_in_polygon_interior |
| unresolved_disagreement | 2 | JP | 東京都 | 千代田区 |  | 東京都 | 文京区 | 湯島一丁目 | point_in_polygon_interior |
| unresolved_disagreement | 1 | JP | 東京都 | 新宿区 |  | 東京都 | 豊島区 | 目白三丁目 | point_in_polygon_interior |
| unresolved_disagreement | 1 | JP | 東京都 | 港区 |  | 東京都 | 渋谷区 | 神宮前三丁目 | point_in_polygon_interior |
| unresolved_disagreement | 1 | JP | 東京都 | 品川区 |  | 東京都 | 港区 | 港南二丁目 | point_in_polygon_interior |
| unresolved_disagreement | 1 | JP | 東京都 | 北区 |  | 東京都 | 豊島区 | 西巣鴨四丁目 | point_in_polygon_interior |
| unresolved_disagreement | 1 | JP | 東京都 | 千代田区 |  | 東京都 | 文京区 | 湯島一丁目 | point_in_polygon_interior |
| unresolved_disagreement | 1 | JP | 東京都 | 文京区 |  | 東京都 | 台東区 | 池之端一丁目 | point_in_polygon_interior |
| unresolved_disagreement | 1 | JP | 東京都 | 中央区 |  | 東京都 | 千代田区 | 丸の内三丁目 | point_in_polygon_interior |
| unresolved_disagreement | 1 | JP | 東京都 | 板橋区 |  | 東京都 | 豊島区 | 池袋三丁目 | point_in_polygon_interior |
| unresolved_disagreement | 1 | JP | 東京都 | 文京区 |  | 東京都 | 豊島区 | 南大塚二丁目 | point_in_polygon_interior |
| unresolved_disagreement | 1 | JP | 東京都 | 新宿区 |  | 東京都 | 文京区 | 関口一丁目 | point_in_polygon_interior |
| unresolved_disagreement | 1 | JP | 東京都 | 渋谷区 |  | 東京都 | 目黒区 | 中目黒一丁目 | point_in_polygon_interior |
| unresolved_disagreement | 1 | JP | 東京都 | 中央区 |  | 東京都 | 千代田区 | 大手町二丁目 | point_in_polygon_interior |
| unresolved_disagreement | 1 | JP | 東京都 | 品川区 |  | 東京都 | 港区 | 高輪四丁目 | point_in_polygon_interior |
| unresolved_disagreement | 1 | JP | 東京都 | 豊島区 |  | 東京都 | 新宿区 | 下落合三丁目 | point_in_polygon_interior |

## Storage delta vs the 35.50 B/row baseline

Measured on the same 35,042 rows, page_rows=256, independent gzip pages.

| Format | B/indexed row | p50 page bytes | Linear all-473M GB |
|---|---:|---:|---:|
| Lookup-safe baseline (measured here) | 31.935 | 8,151 | 15.11 |
| + division GERS IDs + match byte | 32.323 | 8,242 | 15.29 |
| **delta** | **+0.387** | | |

All GB projections in the table linearize the box-measured 31.935/32.323 B/row values. Applying the same +0.387 B/row delta to the hosted `useful_gzip` reference baseline (35.502976 B/row, measured on the separate hosted reduce range) gives 16.79 GB -> 16.98 GB. Both sit far inside the 40 GB stop gate (addresses share that budget with Places; these are labeled diagnostics, not forecasts). Every stored extended page is verified to decode from its bytes alone (self-describing core-length framing).

> The measured delta is a favorable-case lower bound: the sample box has an atypically small division-polygon set, so few distinct GERS IDs repeat within each page and the page dictionary amortizes to almost nothing. The delta scales with the number of distinct containing divisions per page under global polygon density (boundaries, dense locality/neighborhood fabric, multi-membership).

## Build cost (labeled diagnostic, not a forecast)

- Spatial join: 4.596 s wall on a 4-thread DuckDB session, 4.595 s CPU, over 35,042 points x 1,308 polygons
- Peak process RSS: 1320.5 MB
- Linear all-473M-row wall diagnostic: 17.23 factory-hours (single 4-thread-session wall-clock basis)

> A purely linear wall-clock diagnostic on a 4-thread DuckDB session, NOT a build forecast. It excludes global polygon density, extraction, sort, shuffle, retries, and country-specific work; the measured box covers one small geographic area with a bounded polygon set, and the timed join includes the taxonomy-only neighborhood polygons.

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

- One small purposive box in JP; not globally representative.
- Stored context covers region/county/locality only; finer subtypes are taxonomy-only channels. Neighborhood polygons are joined where they exist, and macrohood/neighborhood/microhood division points are associated with their containing municipality by name -- a name-level heuristic, not verified neighborhood-polygon containment for those point-only features.
- The box population is below the row cap, so every address in the configured box is included.
- The measured storage delta is a favorable-case lower bound; see the storage delta caveat.
- address_levels semantics are country-dependent; first/last are proxies.
- Division-area geometry is current-release; a single release join only.
- Source row-group/row locators are absent from raw addresses and set to zero; the storage delta is unaffected because both variants encode them identically.
- Match confidence is a structural-completeness proxy, not a calibrated probability; Overture division areas carry no per-point confidence.
- No R2 object, catalog, shard, Worker, or production state is written.
