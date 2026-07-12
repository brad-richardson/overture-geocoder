# Current-release bounded address/source experiment

Release: `2026-06-17.0` / schema `v1.17.0` / Addresses maturity: **Alpha**.

> This is a purposive, bounded 12-box experiment, not a statistically representative sample.
> Counts are raw unweighted sample counts; no global design-weighted estimate is reported.
> Row/byte/temp/memory guards bound local outputs and DuckDB workspace only. They do not meter S3 bytes scanned or HTTP requests. The single remote query is interrupted at the reported wall-clock cap.
> Remote I/O was bounded with one combined bbox-prefiltered scan, followed by exact Point-coordinate membership. Missing or spatially discordant bbox rows are unobservable, so candidate populations are not fully geometry-authoritative.
> Post-query artifact acceptance only: the cap removes a completed sample whose observed box population is too large; it does not bound the remote scan, count, window, or deterministic sort. The wall-clock and DuckDB workspace guards bound that work.

Official references: [address schema](https://docs.overturemaps.org/schema/reference/addresses/address/), [source item schema](https://docs.overturemaps.org/schema/reference/core/source_item/), and [address data guide](https://docs.overturemaps.org/guides/addresses/).

## Operational cost

- Single combined remote pass: **595.19 seconds**
- Combined bounded sample: **997,523 bytes**
- Wall-clock interrupt: **600 seconds**
- S3 bytes and request count were not metered; bbox metadata pruned input before exact Point verification.

## Sample coverage

| Box | Stratum | Country | Sample / recorded candidate count | Fraction | Root datasets |
|---|---|---:|---:|---:|---:|
| `manhattan` | high-rise | US | 2,000 / 4,156 | 48.1% | 1 |
| `singapore-cbd` | high-rise | SG | 2,000 / 2,130 | 93.9% | 1 |
| `melbourne-cbd` | high-rise | AU | 2,000 / 94,359 | 2.1% | 1 |
| `paris` | dense | FR | 2,000 / 8,754 | 22.8% | 1 |
| `mexico-city` | dense | MX | 2,000 / 6,869 | 29.1% | 1 |
| `sao-paulo` | dense | BR | 2,000 / 10,845 | 18.4% | 1 |
| `cambridge-ma` | suburban | US | 2,000 / 13,153 | 15.2% | 1 |
| `parramatta` | suburban | AU | 2,000 / 41,040 | 4.9% | 1 |
| `auckland` | suburban | NZ | 2,000 / 30,734 | 6.5% | 1 |
| `rural-kansas` | rural | US | 264 / 264 | 100.0% | 1 |
| `rural-france` | rural | FR | 1,438 / 1,438 | 100.0% | 1 |
| `rural-kwazulu-natal` | rural | ZA | 0 / 0 | n/a | 0 |

## Source evidence

- Root source records: 19,702
- Features with a root source: 19,702
- Distinct root datasets: 9
- Distinct populated licenses: 0
- Root records with update time: 0
- Root records with confidence: 0
- Property-specific source records: 0

> Source confidence is source-supplied and is not calibrated across datasets; missing confidence is not zero confidence.

> update_time describes a source record when populated; it is neither Overture release time nor a guaranteed observation time.

| Root dataset | Records | Features | Boxes | License example | Updated range | Confidence coverage |
|---|---:|---:|---:|---|---|---:|
| `OpenAddresses/Geoscape Australia` | 4,000 | 4,000 | 2 | n/a | n/a | 0/4,000 |
| `OpenAddresses/adresse.data.gouv.fr` | 3,438 | 3,438 | 2 | n/a | n/a | 0/3,438 |
| `OpenAddresses/AddressForAll/INEGI` | 2,000 | 2,000 | 1 | n/a | n/a | 0/2,000 |
| `OpenAddresses/LINZ` | 2,000 | 2,000 | 1 | n/a | n/a | 0/2,000 |
| `OpenAddresses/MA/MassGIS` | 2,000 | 2,000 | 1 | n/a | n/a | 0/2,000 |
| `OpenAddresses/NY/NYC Open Data` | 2,000 | 2,000 | 1 | n/a | n/a | 0/2,000 |
| `OpenAddresses/Singapore Land Authority` | 2,000 | 2,000 | 1 | n/a | n/a | 0/2,000 |
| `br_ibge` | 2,000 | 2,000 | 1 | n/a | n/a | 0/2,000 |
| `NAD` | 264 | 264 | 1 | n/a | n/a | 0/264 |

### SourceItem field coverage

| Scope | Records | Dataset | License | Record ID | Update time | Confidence | Between |
|---|---:|---:|---:|---:|---:|---:|---:|
| all | 19,702 | 19,702 | 0 | 0 | 0 | 0 | 0 |
| root | 19,702 | 19,702 | 0 | 0 | 0 | 0 | 0 |

## Per-box field shape

| Box | Number | Street | Unit | Postcode | Postal city | Address levels | Missing bbox | Discordant bbox |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `manhattan` | 2,000 | 2,000 | 159 | 1,999 | 0 | 2,000 | unobservable | 0 |
| `singapore-cbd` | 2,000 | 2,000 | 2,000 | 2,000 | 0 | 2,000 | unobservable | 0 |
| `melbourne-cbd` | 2,000 | 2,000 | 1,846 | 2,000 | 0 | 2,000 | unobservable | 0 |
| `paris` | 2,000 | 2,000 | 0 | 2,000 | 0 | 2,000 | unobservable | 0 |
| `mexico-city` | 1,745 | 2,000 | 0 | 2,000 | 0 | 2,000 | unobservable | 0 |
| `sao-paulo` | 1,972 | 2,000 | 0 | 2,000 | 0 | 2,000 | unobservable | 0 |
| `cambridge-ma` | 1,998 | 2,000 | 1,059 | 2,000 | 0 | 2,000 | unobservable | 0 |
| `parramatta` | 2,000 | 2,000 | 1,662 | 2,000 | 0 | 2,000 | unobservable | 0 |
| `auckland` | 2,000 | 2,000 | 829 | 0 | 0 | 2,000 | unobservable | 0 |
| `rural-kansas` | 264 | 264 | 0 | 258 | 264 | 264 | unobservable | 0 |
| `rural-france` | 1,438 | 1,438 | 0 | 1,438 | 0 | 1,438 | unobservable | 0 |
| `rural-kwazulu-natal` | 0 | 0 | 0 | 0 | 0 | 0 | unobservable | 0 |

## Address identity evidence

- Sample rows: 19,702
- Rows with multiple root sources: 0
- Keyable number+street rows: 19,417
- Rows with unit: 7,555
- Lossy normalization collision keys: 1
- Proxy-context keys with multiple coordinates: 76
- Proxy-context keys over 10 m by envelope-spread proxy: 56

Proxy-context key definition: NFC/lowercase/whitespace-normalized country, first address_levels value (most-general proxy), last address_levels value (most-specific proxy), postcode, number, street, and unit. Address-level meanings remain country-dependent, so this is not a globally typed structured address key.

> Coordinate spread is a latitude-range/minimum-circular-longitude-envelope proxy, not maximum pairwise or road-network distance.

Per-source normalization and coordinate-variation tables are retained in the JSON report.
The current bbox-prefiltered, exact-Point-verification SQL is retained in the JSON report.

## Gold-query status

No gold queries were created. Sampled source records are not independently verified relevance labels. Promoting them to gold queries would manufacture circular ground truth.

## Architecture implications

- Preserve the full source array and multi-root membership; do not flatten to a single provider.
- Preserve full `address_levels`; use positional ends only as routing proxies, not globally typed locality/region fields.
- Treat source confidence as within-source metadata until calibration evidence exists.
  In this sample it was absent for every root record, so it cannot weight candidates.
- Dictionary-code source dataset identity and omit all-null optional provenance columns from hot lookup records; keep complete provenance in a colder detail record.
- Retain candidate lists for coordinate-varying proxy-context keys and keep unit in identity.
- Measure current-release source and spatial skew before choosing regional/postcode shard boundaries.

## Limitations

- The Overture Addresses theme is Alpha in the cited release.
- The boxes are purposive and tiny; estimates must not be extrapolated globally.
- All counts are raw unweighted sample counts unless explicitly labeled as preflight box populations.
- Rows are deterministic pseudo-random samples only when a box exceeds its cap.
- A zero-row box is evidence of no records in that exact box, not country-wide absence.
- address_levels are preserved in full. The first/last values are only general/particular routing proxies; their semantic meaning is country-dependent.
- Source-stratified records are multi-membership: one feature may count in multiple root datasets.
- Coordinate variation uses an envelope-spread proxy, not pairwise distance.
- Local output/workspace guards do not meter S3 scan bytes or HTTP requests; the remote query has an explicit wall-clock interrupt.
- No production shard, catalog, Worker, R2 object, deployment, or interpolation is changed.
