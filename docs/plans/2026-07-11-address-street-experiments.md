# Address and street experiments

Status: local architecture/benchmark work only. No worker, API, publication,
interpolation, or production-shard changes are proposed here.

## Evidence

`scripts/benchmark_address_street.py` keeps high-cardinality analysis in
DuckDB and accepts any flattened address Parquet with common column aliases.
Its returned samples and per-source tables are capped, but DuckDB aggregate and
sort memory remains input-dependent and may spill to disk; this is not a fixed
total-memory guarantee.
It intentionally distinguishes:

- a conservative structured normalization: NFC, lowercase, whitespace
  cleanup, punctuation and diacritics preserved;
- a deliberately lossy punctuation-stripping comparison used only to measure
  collisions while preserving Unicode letters and numbers;
- address-derived street-name coverage, which is only a proxy and says nothing
  about transportation topology, connectors, road class, or routability.

The collision and coordinate metrics use a **proxy-context key**: normalized
country, first and last `address_levels` values, postcode, number, street, and
unit. The address-level endpoints are most-general/most-specific routing
proxies whose meanings are country-dependent. This is not a globally typed
structured address key. Coordinate spread is an envelope approximation, not a
maximum pairwise or road-network distance.

For current exports it also reads the nested `sources` and `root_sources`
arrays, reports release and feature-version distributions, root-source
cardinality and field coverage, and caps source-stratified normalization and
coordinate-variation output to 20 root datasets. Multiple embedded releases
fail closed unless an explicit comparison opts into `--allow-mixed-release`.
Rows with zero root sources are reported as zero-root rows, not as populated
`root_source_count` values. Coordinate longitude span uses the minimum circular
arc (`360° - largest gap`) rather than a min/max shortcut.

### Massachusetts address artifact

Measured from the historical `exports/US-MA.parquet` artifact:

| Metric | Result |
|---|---:|
| Rows / Parquet size | 3,637,794 / 153.1 MiB |
| Number + street rows | 3,634,040 (99.9%) |
| Unit rows | 1,325,041 (36.4%) |
| Distinct base keys, excluding unit | 2,324,305 |
| Base keys with multiple units | 215,547 |
| Maximum distinct units on one base | 821 |
| Lossy punctuation keys with collisions | 1,128 |
| Conservative keys collapsed by lossy form | 2,258 |
| Duplicate proxy-context keys | 6,804 |
| Proxy-context keys with multiple coordinates | 6,804 |
| Proxy-context keys with coordinate-envelope spread over 10 m | 4,367 |
| Coordinate-envelope spread over 1 km | 29 keys / 1,263 rows |
| Postcodes | 531 |
| Median / P90 / max rows per postcode | 4,935 / 15,687 / 34,995 |
| Address-derived street names | 54,486 |
| Names spanning multiple localities / postcodes | 15,287 / 15,927 |
| P90 locality / postcode fanout | 4 / 4 |

Consequences:

1. Unit is not decorative search text. Dropping it merges many independently
   addressable records, so it belongs in exact lookup and FTS input.
2. Removing punctuation is not a safe canonical identity operation. A collapse
   does not prove two source strings identify different addresses, but it does
   show that equivalence cannot be assumed without labeled queries, coordinates,
   feature IDs, and source evidence.
3. Exact normalized text can have materially different coordinates. Preserve
   candidate lists, Overture feature IDs, provenance, and coordinates rather
   than `GROUP BY key` with an arbitrary first row.
4. Postcode is a useful routing/index boundary, but making 531 independently
   fetched Worker files for one state would be too granular. Keep postcode as
   an indexed partition within a regional shard or group adjacent/small
   postcodes under a measured size budget.
5. A global street-name dictionary is under-scoped. Street names need locality
   and postcode context, and common names need distinct connected components.

The old export predates the new extraction fields, so it cannot measure release,
feature version, or source coverage. `download_addresses.sql` now embeds the
Overture release and preserves the complete source array plus all root sources
and their count. This retains property-specific provenance, licenses, record
IDs, update times, and source-supplied confidence for the next bounded export.
Source confidence is defined by each source dataset and must not be treated as a
cross-source calibrated ranking score without validation.

### Historical byte extrapolations

The January SF bbox experiment measured 17,014 address rows. Linear application
of those old bytes-per-row values to the MA row count yields approximately:

| Historical artifact | Linear result |
|---|---:|
| Region SQLite | 1.0 GiB |
| Tiered index | 897.8 MiB |
| Tiered detail | 821.0 MiB |
| Prefix-only SQLite | 587.1 MiB |
| Minimal SQLite | 314.0 MiB |
| Trie | 156.3 MiB |

These are explicitly **not build-size forecasts**. The SF bbox had different
density, source release, schema, fixed SQLite page overhead, index design, and
compression. They show only that one monolithic state artifact is unlikely to
fit the current whole-database Worker loading model.

The local transportation street artifacts are empty, so this work has no
measured transportation coverage, component-size, topology, or connector
evidence. Address-derived street counts must not be presented as a replacement.

## Recommended hybrid architecture

### 1. Regional routing

Route country and region with the existing division/name router. Use postcode
and locality as address/street context after the region is selected. Keep
address and street data out of global HEAD except for compact routing metadata.

### 2. Exact structured address store

Use a regional, postcode-indexed store keyed by distinct structured components:

`country, region, postcode, locality, street, number, unit`

Normalize each component conservatively and retain the original display value.
The value is a candidate list containing Overture feature ID, coordinates/bbox,
feature version, release, and complete source provenance. An address ID must not
be assumed to be a GERS ID without validation. Do not interpolate missing house
numbers and do not collapse coordinate-varying proxy-context keys during build.

Search text should contain number, street, unit, locality, postal city, region,
postcode, and country. Geographic locality remains part of structured identity;
postal city is retained separately as a valid query alias. Text search is a
recall path; the structured key remains the identity/precision path.

### 3. Separate street dictionary

Build the authoritative street layer from Overture transportation data:

1. Connect named road segments through transportation connector IDs.
2. Split a repeated normalized name into connected components rather than one
   global centroid.
3. Preserve representative geometry/bbox, segment IDs, road class, and source
   version for each component.
4. Attach locality/postcode context from nearby address evidence and divisions.
   Address rows enrich labels and routing; they do not define road topology.
5. Store the resulting component dictionary regionally and query it separately
   from exact addresses.

This yields a clean contract:

- transportation answers “which street component?”;
- address/division context answers “in which locality/postcode?”;
- the exact address store answers “which numbered/unit record and point?”

Official schema references:

- [Overture Address](https://docs.overturemaps.org/schema/reference/addresses/address/)
- [Overture Segment](https://docs.overturemaps.org/schema/reference/transportation/segment/)
- [Overture Connector](https://docs.overturemaps.org/schema/reference/transportation/connector/)

## Acceptance experiments before integration

1. Re-extract one current-release region and verify release/version, full-source
   and root-source cardinality, and source-specific collision/variation rates.
2. Curate exact address queries covering ranges, apostrophes, hyphens,
   directionals, unit formats, repeated house numbers, and ambiguous cities.
3. Compare conservative lookup with lossy normalization; lossy lookup must not
   silently choose among conservative keys.
4. Measure proxy-context-key candidate counts and envelope-spread buckets by
   root source before defining deduplication or precedence.
   Treat multi-root features as members of each reported source stratum and do
   not interpret source-supplied confidence as calibrated across datasets.
5. Build transportation components for one bounded metro; validate crossings,
   divided roads, disconnected same-name roads, ramps, and name changes against
   connector topology.
6. Join address locality/postcode context to those components and measure
   missing/contradictory context without altering the transportation graph.
7. Build one regional exact store and one street dictionary, then measure cold
   bytes, deserialized heap, query latency, and postcode routing recall.

Until those pass, do not add interpolation, promote address/road shards, or
change the public API/default types.

## Current-release source follow-up

The first acceptance experiment has a completed bounded result in
[`2026-07-11-current-release-address-experiment.md`](./2026-07-11-current-release-address-experiment.md).
It sampled 19,702 records from release `2026-06-17.0` across twelve purposive
high-rise, dense, suburban, and rural boxes. Eleven boxes contained data and
the sample covered nine root datasets.

The corrected extraction made one combined remote pass, used bbox only as an
I/O prefilter, and then required exact Point coordinates inside each configured
box. It completed in 595.19 seconds under a 600-second interrupt and produced a
997,523-byte aggregate sample. Missing or spatially discordant bbox rows remain
unobservable, so candidate populations are exact only within bbox-observable
rows. Candidate caps now abort explicitly rather than silently looking empty;
per-box output-byte caps are measured independently.

Every sampled feature had exactly one root source and no property-specific
source. Dataset identity was complete, but license, update time, and source
confidence, record ID, and `between` were absent on every sampled root record.
The full source arrays also contained no property-specific records. This does
not establish a
fleet-wide invariant; it does establish that source confidence cannot be a
required ranking input and that missing confidence must not be interpreted as
zero confidence. Keep dataset identity in compact hot records, but retain the
complete source array in a colder provenance/detail record.

The sample also found 76 proxy-context keys with multiple coordinates; 56 had
an approximate envelope spread over 10 m. Most were in the sampled Brazil
and Mexico datasets. Units occurred on 7,555 rows and 970 unitless base keys had
multiple units. These findings reinforce candidate-list storage and keeping
unit in structured identity. They do not justify cross-source precedence,
deduplication, interpolation, or global size extrapolation.
