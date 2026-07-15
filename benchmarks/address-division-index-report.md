# Compact address + division-link spike

Date: 2026-07-14

## Verdict

This experiment measures a deterministic known-address lookup format, not
free-form parsing, fuzzy matching, interpolation, or human relevance.
Each record round-trips its source state/city/postal-city/postcode/street
label tuple through a dictionary reference; geometric division links remain
separate derived context.

The storage result is encouraging: address cardinality is large, but its
structured repetition compresses much better than the POI token index. The
remaining hard problem is parsing and country-specific normalization, not R2
storage.

## Input and build

- Addresses: 3,634,040 keyable Massachusetts records
- Address release: `unknown-historical`; SHA-256 `b93f26145d0e0b8aeb7895a382dbc71a719a98a0f7aecefeb3ab731682781bcc`
- Division areas: 635 from Overture release `2026-06-17.0`
- Division input SHA-256: `c3880d29addfef361760ce758271cf11ccd1393e101f7d35069feeee6d85b54e`
- DuckDB: `1.5.4`; 4 threads; memory limit `12GB`
- Producer commit: `73db3424d8c22fd1c4d15276d1836b6250f8db67`
- Division spatial join: 101.16 seconds
- Total experiment: 150.04 seconds
- Artifact: 159,252,812 bytes (43.8 B/address)
- Artifact SHA-256: `88d839df7d2c9e77acd24a75c0173e1a9dd40284d8ed76d0ce9cc15983fdc0ff`
- Strict reader/oracle verification: True

The address artifact is historical while the division areas are current-release;
the result is architecture evidence, not a release-valid production join.

## Artifact components

| component | bytes | B/address |
|---|---:|---:|
| chain_offsets | 4,952 | 0.00 |
| chain_blob | 33,156 | 0.01 |
| context_offsets | 12,568 | 0.00 |
| context_blob | 39,405 | 0.01 |
| street_offsets | 993,912 | 0.27 |
| street_blob | 4,397,745 | 1.21 |
| label_offsets | 993,912 | 0.27 |
| label_blob | 4,203,341 | 1.16 |
| record_offsets | 29,072,328 | 8.00 |
| record_blob | 119,501,119 | 32.88 |
| header/magic | 374 | 0.00 |
| **total** | **159,252,812** | **43.82** |

The hot record contains a 16-byte feature ID, quantized coordinates,
number, unit, a source-label-set reference, and a normally-zero
division-chain override. Context, street, source-label, and chain records are
indexed dictionary blobs. A global per-record offset table makes the
variable-length record block binary-searchable. Source provenance, raw
address levels, and source-file locators are not present in this historical
input and require a measured allowance in a current-release producer.

## Division linking

- Addresses inside at least one selected division area: 3,633,628 / 3,634,040
- Distinct per-address division chains: 618
- Chain matches context-dominant chain: 3,492,465
- Records requiring an override: 141,575
- Contexts: 1,570; unanimous contexts: 851
- Ambiguous address/subtype pairs retained in the chain: 45,602 (maximum 2)
- Boundary-covered addresses: 0
- Addresses unmatched by any selected division area: 412

| subtype | linked addresses | source-label matches | distinct divisions |
|---|---:|---:|---:|
| region | 3,608,421 | 0 | 1 |
| county | 3,608,421 | 11,442 | 14 |
| macrohood | 475,579 | 263,108 | 43 |
| locality | 3,666,068 | 3,296,233 | 363 |
| neighborhood | 31,838 | 0 | 111 |
| microhood | 738 | 0 | 9 |

A chain dictionary ID is useful for response context, routing, and linking
to the existing divisions index. It must not replace `address_levels` or
`postal_city`: Overture explicitly notes that address levels are country-
dependent addressing units and need not correspond to administrative divisions.
All covered region/county/locality IDs are retained, including multiple IDs
for one subtype. They come from containing division areas; a
country division ID can be stored once in the shard manifest rather than on
every address. Registry membership should be verified before describing any
particular Overture division ID as a GERS ID.

## Lookup shape

- Contexts: 1,570
- Context/street groups: 124,238
- Distinct source-label sets: 124,238
- Distinct normalized street names: 54,396
- Records per street group: median 12, p90 61, p99 314, max 4,008
- Median/max encoded record-group bytes: 372 / 141,029

The serving path is: parse or accept structured country/region/locality/
postcode, resolve a context, binary-search its normalized street range,
then binary-search number and unit through the record-offset table. The
strict local reader verifies an exact candidate set against DuckDB. Prefix
street lookup follows adjacent sorted street entries. This does not yet
supply a robust parser for arbitrary one-line input.

Normalization is deliberately NFC + collapsed ASCII whitespace + ASCII-only case
folding so the DuckDB builder and reader agree for every Unicode string. This
does not yet provide case-insensitive matching for non-ASCII scripts.

## Linear diagnostics, not forecasts

At the measured 43.8 B/address, 473M addresses would be
about 20.73 GB per release before source-locator
metadata, country-specific parser indexes, manifests, or rollback retention.
Massachusetts source and unit distributions are not globally representative.
A purely linear processing diagnostic is about 5.42 factory-hours;
global polygon density, extraction, sorting, upload, and country-specific work
make that a lower-confidence shape rather than a build-time forecast.

Official schema references: [Address](https://docs.overturemaps.org/schema/reference/addresses/address/),
[AddressLevel](https://docs.overturemaps.org/schema/reference/addresses/types/address_level/), and
[DivisionArea](https://docs.overturemaps.org/schema/reference/divisions/division_area/).

## Next gate

The bounded global producer proposal is documented in
[`docs/plans/2026-07-14-global-places-address-processing-design.md`](../docs/plans/2026-07-14-global-places-address-processing-design.md).

1. Repeat on one current-release country/region extract carrying source filepath
   and row-group locator fields.
2. Preserve raw `address_levels` and measure their dictionary cost separately
   from geometric division-chain IDs.
3. Implement and evaluate a bounded US one-line parser plus structured endpoint
   against independently labelled queries.
4. Measure Worker range reads for exact, prefix, ambiguous, unit, and no-result
   cases before extrapolating a public API.
5. Define political-perspective semantics for the multiple containing
   division memberships that this artifact now preserves.

## Reproduction

```bash
python3 scripts/experiment_address_division_index.py exports/US-MA.parquet \
  exports/US-MA-division-areas-2026-06-17.parquet \
  --artifact artifacts/addresses-ma-division.oadr \
  --database artifacts/addresses-ma-division.duckdb --overwrite-database \
  --address-release unknown-historical --division-release 2026-06-17.0 \
  --threads 4 --memory-limit 12GB \
  --json-out benchmarks/address-division-index-report.json \
  --markdown-out benchmarks/address-division-index-report.md
```
