# Address and street experiments

Status: local architecture/benchmark work only. No worker, API, publication,
interpolation, or production-shard changes are proposed here.

## Evidence

`scripts/benchmark_address_street.py` keeps high-cardinality analysis in
DuckDB and accepts any flattened address Parquet with common column aliases.
It intentionally distinguishes:

- a conservative structured normalization: NFC, lowercase, whitespace
  cleanup, punctuation and diacritics preserved;
- a deliberately lossy punctuation-stripping comparison used only to measure
  collisions;
- address-derived street-name coverage, which is only a proxy and says nothing
  about transportation topology, connectors, road class, or routability.

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
| Duplicate exact structured keys | 6,804 |
| Exact keys with multiple coordinates | 6,804 |
| Coordinate ambiguity over 1 km | 29 keys / 1,263 rows |
| Postcodes | 531 |
| Median / P90 / max rows per postcode | 4,935 / 15,687 / 34,995 |
| Address-derived street names | 54,486 |
| Names spanning multiple cities / postcodes | 15,287 / 15,927 |
| P90 city / postcode fanout | 4 / 4 |

Consequences:

1. Unit is not decorative search text. Dropping it merges many independently
   addressable records, so it belongs in exact lookup and FTS input.
2. Removing punctuation is not a safe canonical key operation. Examples such
   as ranges, hyphenation, apostrophes, and directional punctuation can become
   different valid address forms after punctuation removal.
3. Exact normalized text is not a unique coordinate. Preserve candidate lists,
   GERS IDs, provenance, and coordinates rather than `GROUP BY key` with an
   arbitrary first row.
4. Postcode is a useful routing/index boundary, but making 531 independently
   fetched Worker files for one state would be too granular. Keep postcode as
   an indexed partition within a regional shard or group adjacent/small
   postcodes under a measured size budget.
5. A global street-name dictionary is under-scoped. Street names need locality
   and postcode context, and common names need distinct connected components.

The old export predates the new extraction fields, so it cannot measure
feature version or root-source coverage. `download_addresses.sql` now preserves
`version` plus the root external source dataset, update time, and source
confidence for the next bounded export.

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
The value is a candidate list containing GERS ID, coordinates/bbox, feature
version, and root source provenance. Do not interpolate missing house numbers
and do not collapse coordinate-ambiguous exact keys during build.

Search text should contain number, street, unit, locality, region, postcode,
and country, but text search is a recall path; the structured key remains the
identity/precision path.

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

1. Re-extract one current-release region and verify version/root-source field
   coverage and source-specific collision/ambiguity rates.
2. Curate exact address queries covering ranges, apostrophes, hyphens,
   directionals, unit formats, repeated house numbers, and ambiguous cities.
3. Compare conservative lookup with lossy normalization; lossy lookup must not
   silently choose among conservative keys.
4. Measure exact-key candidate counts and distance buckets by root source before
   defining deduplication or precedence.
5. Build transportation components for one bounded metro; validate crossings,
   divided roads, disconnected same-name roads, ramps, and name changes against
   connector topology.
6. Join address locality/postcode context to those components and measure
   missing/contradictory context without altering the transportation graph.
7. Build one regional exact store and one street dictionary, then measure cold
   bytes, deserialized heap, query latency, and postcode routing recall.

Until those pass, do not add interpolation, promote address/road shards, or
change the public API/default types.
