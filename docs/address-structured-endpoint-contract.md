# Structured address exact-lookup contract

Status: build-ready structured-index contract. It does not approve or dispatch
a global build, publication, or endpoint migration.

## Launch slice

The first address surface is structured exact lookup only. It does not parse a
free-form line, expand prefixes, correct spelling, or silently treat omitted
context as a wildcard.

The `/v2/forward` request maps eight named string fields into this normalized
lookup order:

1. `country`
2. `admin_level_general`
3. `admin_level_specific`
4. `postal_city`
5. `postcode`
6. `street`
7. `number`
8. `unit`

The two administrative values are the first and last retained source
`address_levels` values; the response also preserves the complete raw list.
They are deliberately not relabelled as region/locality until division
enrichment defines that mapping. `country`, `street`, and `number` must be
present and non-empty. The other values may be omitted or empty; either maps to
the literal empty string. An empty value is literal: it
matches producer rows whose corresponding normalized value is empty and is not
a wildcard. A client that has less context needs a separately designed search
surface rather than surprising exact-lookup fanout.

Normalization is the current producer's NFC normalization,
Unicode-whitespace collapse, and ASCII-only lowercasing contract. Canonically
equivalent non-ASCII sequences are merged, but this slice does not promise
Unicode case folding. The response echoes the data version and normalization
version so a client can diagnose a miss.

## Results and ambiguity

Every retained feature with the exact normalized eight-field key is returned in
deterministic producer order. Feature IDs are never deduplicated and one result
is never selected as “the” address. The response includes the raw display
fields, coordinates, feature ID, and exact source locator.

The initial hard response cap is 512 candidates. This is above the measured
maximum fanout of 252. If a future release exceeds the cap, the Worker returns a
bounded overflow error with the observed count; it must not silently truncate.
An exact miss is a successful empty result, not permission to issue an
unbounded fallback.

Rows missing `street` or `number`, or with invalid point geometry, remain in the
producer coverage report but are not representable in this exact family. They
must not be inferred, repaired, or discarded without per-task accounting.

## Partition and catalog rule

Route by a stable hash of the complete
normalized eight-field key, nested under country. The versioned address catalog
maps `(country, hash range)` to exactly one immutable shard. Consequently every
exact lookup reads one shard and all duplicate candidates for a key remain
together.

The stable partition scheme is `country-fnv1a-high-bits-v1`. Each country starts
as one full 64-bit range. A leaf above the one-million-row cap splits by the next
high hash bit, and a prior split is sticky: cells can split but never merge in a
later release. Partition IDs are `a-{country}` or
`a-{country}-h-{binary_prefix}`. The collection records every empty child range
explicitly, allowing a Worker to return a proven exact miss without an object
read while still rejecting an accidental catalog gap.

Start with a target of roughly one million retained rows per serving shard,
then size future caps from measured bytes rather than forcing a fixed global shard count.
At the current four-million-row evidence point the resident side index was
941,745 bytes; the one-million-row target is intended to test a materially
smaller cold index while preserving exact predecessor selection and the
three-range serving cap. The schema-v2 collection validates complete country
hash coverage, split ancestry, canonical object paths, row caps, and immutable
size/SHA identities before routing.

`build_address_shard.py` verifies that every row of one reduced partition belongs
to its country/hash range, then emits only the Worker's independently gzipped
`useful_gzip` pages and bounded predecessor index. `build_address_collection.py`
requires exactly one page pair for every non-empty planned leaf and none for
proven-empty ranges. Partial task outputs remain outside every discoverable
catalog. The shared release finalizer may reference address artifacts only after
object-level conditional publication and complete family verification.

This exact-key view is not the future general-search layout. Free-form street
forward search and nearest-address reverse search should be separate secondary
indexes (likely country/postcode/locality and spatial ownership respectively)
behind the same v2 API. Forcing all three access patterns into this hash layout
would make each of them worse and would not make the public API more standard.

## Unsupported in this slice

- free-form address parsing;
- street, house-number, or unit prefix search;
- omission-as-wildcard semantics;
- cross-shard fallback after an exact miss;
- destructive duplicate removal;
- request-path access to Overture source data; and
- independent address promotion outside the shared release finalizer.
