# Hosted address Worker decoder spike

Date: 2026-07-15  
GitHub Actions run: [29445012372](https://github.com/brad-richardson/overture-geocoder/actions/runs/29445012372)

## Verdict

The Worker-native useful-gzip path is viable enough to continue. A cold request
performed the bounded side-index read, one R2 page range read, gzip inflate,
strict decode, and compact verification response for a synthetic fixture sized
to the observed 137-candidate maximum fanout in **434 ms wall / 341 ms Worker
time**. Five subsequent requests had a **156 ms wall / 30 ms Worker median**,
with the last two at about 56 ms wall and 13–16 ms Worker time.

This clears the basic “can a Worker do it in low seconds?” gate comfortably on
one North Central US run. It does not prove global p95 latency or production
cache behavior. The fixture is intentionally tiny: a 60-byte side index and one
921-byte compressed page inside a 985-byte data object. The run-unique key makes
the first lookup application-cache-cold, but Cache API hits were not
instrumented, so the subsequent samples must not be read as proven cache-hit
classes. Real indexes remain bounded but are much larger, and cold R2 latency
varies by geography.

## Request shape exercised

```text
normalized exact-address key
  -> bounded side-index range read and binary search
  -> exactly one useful-gzip page range read
  -> Rust gzip inflate and strict binary validation
  -> materialize all 137 ordered candidates
  -> return count + first full record + last ID for smoke verification
```

The Worker decoded all 137 records, but this spike did not measure the network
or JSON cost of returning 137 full production results. The smoke asserted the
synthetic first record, complete address-level sequence, candidate count, and
last UUID. The decoder also enforces index, stored-page,
decoded-page, row-count, dictionary, varint, UTF-8, sorting, coordinate, and
aggregate heap limits. Production does not expose the endpoint: it returns 404
unless the exact isolated smoke environment is active.

## Measurements

| sample | class | wall | Worker `Server-Timing` |
|---:|---|---:|---:|
| 1 | first run-unique lookup | 434.450 ms | 341 ms |
| 2 | subsequent | 156.161 ms | 30 ms |
| 3 | subsequent | 158.005 ms | 33 ms |
| 4 | subsequent | 196.447 ms | 32 ms |
| 5 | subsequent | 56.976 ms | 13 ms |
| 6 | subsequent | 55.935 ms | 16 ms |

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
