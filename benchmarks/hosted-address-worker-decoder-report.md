# Hosted address Worker decoder spike

Date: 2026-07-15  
GitHub Actions run: [29445012372](https://github.com/brad-richardson/overture-geocoder/actions/runs/29445012372)

## Verdict

The Worker-native useful-gzip path is viable enough to continue. A cold request
performed the bounded side-index read, one R2 page range read, gzip inflate,
strict decode, and JSON response for the 137-candidate maximum-fanout oracle in
**434 ms wall / 341 ms Worker time**. Five subsequent requests had a **156 ms
wall / 30 ms Worker median**, with the last two at about 56 ms wall and 13–16 ms
Worker time.

This clears the basic “can a Worker do it in low seconds?” gate comfortably on
one North Central US run. It does not prove global p95 latency or production
cache behavior. The fixture is intentionally tiny: a 60-byte side index and one
921-byte compressed page inside a 985-byte data object. Real indexes remain
bounded but are much larger, and cold R2 latency varies by geography.

## Request shape exercised

```text
normalized exact-address key
  -> bounded side-index range read and binary search
  -> exactly one useful-gzip page range read
  -> Rust gzip inflate and strict binary validation
  -> materialize all 137 ordered candidates
  -> return display address, raw address levels, coordinates, ID, and locator
```

The smoke asserted the known first record, complete address-level sequence,
candidate count, and last UUID. The decoder also enforces index, stored-page,
decoded-page, row-count, dictionary, varint, UTF-8, sorting, coordinate, and
aggregate heap limits. Production does not expose the endpoint: it returns 404
unless the exact isolated smoke environment is active.

## Measurements

| sample | cache class | wall | Worker `Server-Timing` |
|---:|---|---:|---:|
| 1 | cold | 434.450 ms | 341 ms |
| 2 | warm | 156.161 ms | 30 ms |
| 3 | warm | 158.005 ms | 33 ms |
| 4 | warm | 196.447 ms | 32 ms |
| 5 | warm | 56.976 ms | 13 ms |
| 6 | warm | 55.935 ms | 16 ms |

The first failed deployment attempt was operational rather than a decoder
failure: the initial response decoded correctly, then workers.dev briefly
alternated with a 404 during propagation. PR #73 made the smoke retry only 404,
fail closed on every other status, and use the direct Workers API for deletion.
The corrected run verified both Worker deletion and an empty run-specific R2
prefix.

## Decision and next gate

Do not bail because of Worker gzip/range-read performance. The useful response
remains the right default: the earlier bare format saved only 5.28 B/row while
discarding display fidelity and raw address context, and this run shows the
lossless page can be served well below one second.

The dominant uncertainty moves back to dataset shape and publication:

1. sample small, median, large, non-US, and high-fanout source objects;
2. measure global structured-address retention, dictionary/page tails, and
   byte-balanced shard skew;
3. run the same decoder against a representative large side index and several
   page sizes from multiple regions;
4. then build the hash-verifying R2 shuffle/resume path—still without promoting
   a production catalog.

