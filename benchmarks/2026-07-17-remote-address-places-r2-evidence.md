# Remote address, Places, and R2 evidence

Date: 2026-07-17  
Release under test: Overture `2026-06-17.0`  
Production baseline: `https://geocoder.bradr.dev`, data version `2026-07-13.0`

## Decision

- **Address producer: proceed to the next integration slice, not promotion.**
  Three inventoried tasks completed well inside the runner resource gates, and
  every tested lossless page variant reproduced the producer oracle. Structured
  row retention varied from 40.29% to effectively 100%, so a global completeness
  ratio cannot be inferred from one region. The endpoint contract, partition
  rule, incomplete-row policy, and a real-fragment R2 resume run remain gates.
- **Verified R2 resume: pass.** Partial upload, repeated upload, restore into an
  empty directory, replacement of a stale local fragment, and mandatory cleanup
  all passed against real R2. This small rehearsal is suitable for path-filtered
  `main` smoke CI; it is not a pull-request check.
- **Places read chain: protocol pass, product gate fail.** Exact producer/Worker
  equivalence passed for packed-head, exact-shard, prefix-shard, and CJK cases.
  A head miss still fans out to 15 logical ranges across three shards and the
  first observed, partly warm fallback took 1.347 seconds. Context routing,
  independent cold samples, and human relevance scoring are required before
  Places can be enabled by default.
- **Production: unchanged and healthy.** Read-only checks reported health `ok`,
  Boston as a division result, and no result for `Tokyo Tower`. All prototype
  workflows used isolated Worker names and run-specific R2 prefixes.

## Sources

- [Address three-task matrix](https://github.com/brad-richardson/overture-geocoder/actions/runs/29543948867)
- [Address Worker read chain, first pass](https://github.com/brad-richardson/overture-geocoder/actions/runs/29544510887)
- [Address Worker read chain with retained metrics](https://github.com/brad-richardson/overture-geocoder/actions/runs/29545506984)
- [Verified R2 shuffle/resume](https://github.com/brad-richardson/overture-geocoder/actions/runs/29543949392)
- [Places Worker read chain](https://github.com/brad-richardson/overture-geocoder/actions/runs/29544511279)

## Address scale matrix

All tasks came from the checked complete footer inventory. `useful_gzip` is
lossless relative to the reducer, including display fields, raw address levels,
coordinates, IDs, and source locators.

| task | projected rows | structured rows retained | projection peak RSS | map/reduce elapsed | useful gzip B/row | useful gzip p50 / p95 page |
|---|---:|---:|---:|---:|---:|---:|
| Mexico full (3) | 3,998,978 | 1,611,322 (40.29%) | 1.73 GB | 149.8 s | 35.325 | 8,960 / 9,745 B |
| smallest tail (126) | 154,296 | 154,296 (100%) | 0.29 GB | 12.3 s | 29.510 | 7,507 / 8,272 B |
| US full (48) | 3,999,111 | 3,999,106 (99.9999%) | 1.66 GB | 304.1 s | 36.495 | 9,284 / 10,069 B |

The largest measured total non-loopback network-receive upper bound was 327.4
MB, including hydration and potentially unrelated runner traffic. The largest
reducer peak RSS was 869.5 MB, and the longest four-variant compression pass was
692.3 seconds. The observed 29.51-36.50 B/row range would be 14.0-17.3 GB if
applied to all 473,576,753 source rows, but that is deliberately not a forecast:
retention and token/address-level content are visibly regional.

Every variant passed the full decode digest and indexed candidate-set checks.
Maximum observed exact-key fanout was 252 in Mexico, 92 in the US task, and 44
in the tail task.

## Address Worker read chain

Run `29544510887` rebuilt the full US task, uploaded and read back the 145.0 MB
`useful_gzip` data object and 941,745-byte side index, and deployed an isolated
preview Worker. The Worker returned the producer's exact 92-candidate UUID
digest, reported three logical range stages, and returned an empty candidate list
for the miss case. Cleanup deleted both the preview Worker and run-specific R2
objects.

The first pass's first observed request took 416 ms client time and 294 ms Worker
time. Its five subsequent requests had a 127 ms client median. A branch-ref
repeat with the corrected recorder took 392 ms client / 236 ms Worker time cold,
then a 174 ms client / 30 ms Worker median over five warm requests.

| state | R2 reads | R2 bytes | cache hits / bytes | index | stored page | decoded page | materialized page |
|---|---:|---:|---:|---:|---:|---:|---:|
| cold | 3 | 954,362 | 0 / 0 | 941,745 B | 8,521 B | 15,838 B | 90,978 B |
| warm | 0 | 0 | 3 / 954,362 | 941,745 B | 8,521 B | 15,838 B | 90,978 B |

Both runs returned the same 92-candidate digest and the repeat cleaned up its
Worker and R2 prefix. The page itself is small and materialization stayed below
91 KB, but the full side index contributed 98.7% of cold fetched bytes. The next
address slice should evaluate the partition/index tradeoff before treating a
roughly 942 KB cold index read per shard as the final serving shape.

## Verified R2 shuffle/resume

Run `29543949392` used the isolated prefix
`smoke/r2-shuffle/29543949392-1` and performed these assertions against real R2:

1. Upload a one-fragment partial manifest.
2. Resume a four-fragment manifest without overwriting a verified object.
3. Repeat the full upload and require all four statuses to be
   `existing_verified`.
4. Restore all fragments from empty local state and byte-compare them.
5. Corrupt one local fragment, restore again, and byte-compare all fragments.
6. Delete the run prefix and require the remaining R2 key count to be zero.

The workflow completed successfully, wrote no catalog, and made no object
discoverable from production.

## Places Worker read chain

The workflow extracted 50,000 current-release places from each of Boston, Tokyo,
and Mexico City, built one compact shard per region, and built one packed head.

| region | compact shard | bytes/place | tokens |
|---|---:|---:|---:|
| Boston | 5,818,855 B | 116.38 | 28,102 |
| Tokyo | 8,466,959 B | 169.34 | 104,373 |
| Mexico City | 6,219,010 B | 124.38 | 33,885 |

Tokyo's substantially larger lexicon/postings cost is a warning against using a
California or Latin-script sample as a global byte coefficient. The packed head
for this three-region fixture was 8,899,144 bytes with 8,835 keys; it is a real
range-readable R2 object but is not the full global-head size estimate.

The timing samples below are client-observed wall time. “Warm” reports the
median and observed range, not a statistically stable p95; there are only five
or six warm requests per case.

| case | first observed request | first-request R2/cache reads | warm p50 (observed range) | logical ranges |
|---|---:|---:|---:|---:|
| packed-head exact | 863 ms | 4 / 0, 132,219 R2 B | 195 ms (98-273) | 4 |
| three-shard exact fallback | 1,347 ms | 12 / 3, 75,042 R2 B | 224 ms (139-247) | 15 |
| three-shard prefix fallback | already warm | 0 / 15 | 210 ms (171-273) | 15 |
| CJK exact fallback | 706 ms, partly warm | 3 / 12, 101 R2 B | 196 ms (163-387) | 15 |

The run validates cache behavior—fully warm requests had zero R2 reads—but does
not provide independent cold samples for every case because later cases reuse
the same run-scoped objects and Cache API entries. A production-oriented next
prototype should route explicit context to one shard, measure each chain from an
independent cold namespace, and score `benchmarks/places-relevance-seed.json`
against returned names, categories, context, distance, duplicates, and order.

## CI placement

- Run the verified R2 rehearsal on `main` only when its workflow, store helper,
  or contract tests change. Keep its manual trigger for diagnosis.
- Keep the address matrix and address Worker manual. They read current external
  data, build millions of rows, and are evidence jobs rather than unit checks.
- Keep the Places Worker manual until context routing and relevance cases are
  stable. A smaller hermetic Rust/Python cross-decoder corpus remains in normal
  PR CI.
- None of these cloud workflows should be a required pull-request check.
