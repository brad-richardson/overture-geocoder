# Structured address exact-lookup contract

Status: prototype contract; not a production endpoint or publication approval.

## Launch slice

The first address surface is structured exact lookup only. It does not parse a
free-form line, expand prefixes, correct spelling, or silently treat omitted
context as a wildcard.

The request has eight named string fields, in this normalized lookup order:

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
enrichment defines that mapping. All eight keys must be present. `country`,
`street`, and `number` must be
non-empty. The other values may be empty, but an empty value is literal: it
matches producer rows whose corresponding normalized value is empty and is not
a wildcard. A client that has less context needs a separately designed search
surface rather than surprising exact-lookup fanout.

Normalization is the current producer's `strip`, whitespace collapse, and
ASCII lowercase contract. Non-ASCII characters remain exact; this slice does
not promise Unicode case folding. The response echoes the data version and
normalization version so a client can diagnose a miss.

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

## Candidate partition and catalog rule

For the next producer rehearsal, route by a stable hash of the complete
normalized eight-field key, nested under country. The versioned address catalog
maps `(country, hash range)` to exactly one immutable shard. Consequently every
exact lookup reads one shard and all duplicate candidates for a key remain
together.

Start with a target of roughly one million retained rows per serving shard,
then size from measured bytes rather than forcing a fixed global shard count.
At the current four-million-row evidence point the resident side index was
941,745 bytes; the one-million-row target is intended to test a materially
smaller cold index while preserving exact predecessor selection and the
three-range serving cap. Split or merge only at hash-range boundaries and
record parent lineage in the release manifest.

The rule is a candidate until the multi-task verified-R2 rehearsal records
retention, fragment and artifact bytes, peak RAM/disk, retry amplification, and
wall time. Partial task outputs remain outside every discoverable catalog. The
shared release finalizer may reference address artifacts only after object-level
conditional publication and complete family verification are in place.

## Unsupported in this slice

- free-form address parsing;
- street, house-number, or unit prefix search;
- omission-as-wildcard semantics;
- cross-shard fallback after an exact miss;
- destructive duplicate removal;
- request-path access to Overture source data; and
- independent address promotion outside the shared release finalizer.
