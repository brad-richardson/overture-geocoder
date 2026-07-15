# Compact address + division-link spike

Date: 2026-07-14

## Verdict

This experiment measures a deterministic known-address lookup format, not
free-form parsing, fuzzy matching, interpolation, or human relevance.
Source address labels remain authoritative display/query data; geometric
division links are separate derived context.

The storage result is encouraging: address cardinality is large, but its
structured repetition compresses much better than the POI token index. The
remaining hard problem is parsing and country-specific normalization, not R2
storage.

## Input and build

- Addresses: 3,634,040 keyable Massachusetts records
- Division areas: 635 from Overture release `2026-06-17.0`
- Division spatial join: 39.12 seconds
- Total experiment: 73.66 seconds
- Artifact: 114,699,538 bytes (31.6 B/address)

The address artifact is historical while the division areas are current-release;
the result is architecture evidence, not a release-valid production join.

## Artifact components

| component | bytes | B/address |
|---|---:|---:|
| chain_blob | 32,625 | 0.01 |
| context_offsets | 12,568 | 0.00 |
| context_blob | 39,405 | 0.01 |
| street_offsets | 993,912 | 0.27 |
| street_blob | 4,465,587 | 1.23 |
| record_blob | 109,155,185 | 30.04 |
| header/magic | 256 | 0.00 |
| **total** | **114,699,538** | **31.56** |

The hot record contains a 16-byte feature ID, quantized coordinates,
number, unit, and a normally-zero division-chain override. Context and street
strings are dictionary/group encoded. Source provenance and source-file
locators are not present in this historical input and require a measured
allowance in a current-release producer.

## Division linking

- Addresses inside at least one selected division area: 3,633,628 / 3,634,040
- Distinct per-address division chains: 614
- Chain matches context-dominant chain: 3,492,728
- Records requiring an override: 141,312
- Contexts: 1,570; unanimous contexts: 859
- Address/subtype pairs with two containing areas: 45,602 (maximum 2)

| subtype | linked addresses | source-label matches | distinct divisions |
|---|---:|---:|---:|
| region | 3,608,421 | 0 | 1 |
| county | 3,608,421 | 11,442 | 14 |
| macrohood | 475,579 | 263,108 | 43 |
| locality | 3,620,529 | 3,296,233 | 360 |
| neighborhood | 31,775 | 0 | 111 |
| microhood | 738 | 0 | 9 |

A chain dictionary ID is useful for response context, routing, and linking
to the existing divisions index. It must not replace `address_levels` or
`postal_city`: Overture explicitly notes that address levels are country-
dependent addressing units and need not correspond to administrative divisions.
The region/county/locality IDs here come from containing division areas; a
country division ID can be stored once in the shard manifest rather than on
every address. Registry membership should be verified before describing any
particular Overture division ID as a GERS ID.

## Lookup shape

- Contexts: 1,570
- Context/street groups: 124,238
- Distinct normalized street names: 54,396
- Records per street group: median 12, p90 61, p99 314, max 4,008
- Median/max encoded record-group bytes: 340 / 133,013

The serving path is: parse or accept structured country/region/locality/
postcode, resolve a context, binary-search its normalized street range,
then exact-search number and unit within one compact record block. Prefix
street lookup follows adjacent sorted street entries. This does not yet
supply a robust parser for arbitrary one-line input.

## Linear diagnostics, not forecasts

At the measured 31.6 B/address, 473M addresses would be
about 14.93 GB per release before source-locator
metadata, country-specific parser indexes, manifests, or rollback retention.
Massachusetts source and unit distributions are not globally representative.
A purely linear processing diagnostic is about 2.66 factory-hours;
global polygon density, extraction, sorting, upload, and country-specific work
make that a lower-confidence shape rather than a build-time forecast.

Official schema references: [Address](https://docs.overturemaps.org/schema/reference/addresses/address/),
[AddressLevel](https://docs.overturemaps.org/schema/reference/addresses/types/address_level/), and
[DivisionArea](https://docs.overturemaps.org/schema/reference/divisions/division_area/).

## Next gate

The bounded global producer, partitioning, promotion, and stop-gate proposal is
documented in
[`docs/plans/2026-07-14-global-places-address-processing-design.md`](../docs/plans/2026-07-14-global-places-address-processing-design.md).

1. Repeat on one current-release country/region extract carrying source filepath
   and row-group locator fields.
2. Preserve raw `address_levels` and measure their dictionary cost separately
   from geometric division-chain IDs.
3. Implement and evaluate a bounded US one-line parser plus structured endpoint
   against independently labelled queries.
4. Measure Worker range reads for exact, prefix, ambiguous, unit, and no-result
   cases before extrapolating a public API.
5. Define how overlapping political perspectives and multiple containing
   division areas are represented rather than silently choosing one globally.
