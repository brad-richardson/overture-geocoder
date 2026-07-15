# Places KV/R2 factory scale run

Date: 2026-07-14
Overture release: `2026-06-17.0`

This is a bounded build-resource and retrieval-shape experiment on the private
always-on factory machine. It is not a planet-scale forecast and it did not
publish any R2 objects.

## Factory state

- 20 logical CPUs; the benchmark intentionally used one Python CPU.
- 62 GiB RAM, 56 GiB initially available, and 23 GiB swap.
- 1.8 TiB filesystem with about 1.4 TiB free.
- Negligible starting load.
- Existing repositories and services were left untouched. Work ran under
  `~/tmp/overture-geocoder-kv-r2-spike-20260714`.

## Real-data extraction

The now-committed `scripts/factory_extract_places.py` extractor read the official Places GeoParquet from S3 and wrote a
minimal projection containing ID, primary name, brand, category, locality,
region, country, coordinates, and confidence.

The input is a source-order `LIMIT` over the rectangle `-124.5 <= xmin <= -114.0`
and `32.5 <= ymin <= 42.1`, not a California boundary clip or a randomized,
globally representative sample. The 1M Parquet SHA-256 is
`4c4cb3711e806a08801ed87d08c0f2acbc2f7b3f1d69796d65a3824f253c6f84`.

| input | wall time | peak RSS | parquet size |
|---|---:|---:|---:|
| 250,000 California-bbox Places | 13.55 s | 411 MiB | 12 MiB |
| 1,000,000 California-bbox Places | 32.82 s | 553 MiB | 47 MiB |

The repository's existing limited-download shell path was not used after it
failed immediately due to an unterminated embedded Python string. The exact
factory extractor is now committed so the selection can be audited and rerun.

## Page planner/build measurements

The 250K run evaluated all four page layouts. The 1M run evaluated only the
fixture-preferred 256 KiB layout so rejected layouts were not rebuilt.

| input | layouts | wall time | peak RSS | selected simulated bytes | bytes/place |
|---|---:|---:|---:|---:|---:|
| 250,000 | 4 | 132.38 s | 1.56 GiB | 44,986,032 | 179.9 |
| 1,000,000 | 1 | 136.69 s | 3.47 GiB | 177,414,544 | 177.4 |

The 1M single-layout planner processed about 7,316 records/second on one core.
It constructs Python posting maps and simulated page metadata; the timing does
not include serializing 177 MB of page objects or uploading them. Peak memory
was about 3.6 KiB per input row, so a monolithic 75M-row Python build is not
viable even though the machine has ample capacity for bounded shards.

A sensible next producer shape is approximately 1M rows per independent build
partition with 8 concurrent workers. That would use roughly 29 GiB for posting
maps at the observed density, leaving service and OS headroom. A producer must
measure actual serialization and upload before increasing concurrency. The
factory runbook's existing 12-core ceiling should remain the absolute maximum.

## Retrieval gates on real data

Every tested layout failed at least one gate at 250K rows. The selected 256 KiB
layout remained a diagnostic choice, not a passing configuration.

At 1M rows:

| query | candidates | warm operations | bytes | complete recall |
|---|---:|---:|---:|---:|
| `starbucks` exact | 2,387 | 8 | 1,845,331 | yes |
| `golden` + `gat*` | 26 | 12 | 2,643,508 | yes |
| category `hotel` | 4,277 | 3 | 543,305 | yes |
| `starbu*` | 2,395 | 13 | 1,866,248 | yes |

The exact brute-force candidate set was preserved; there was no silent posting
truncation. The important failure is result-page locality. `golden gat*` has
only 26 candidates but still requires 12 operations because its top results are
scattered across record pages. Popular-token posting traversal contributes to
the `starbucks` cost, while long-prefix lookup adds a lexical stage.

## Size and cost interpretation

The 1M projection linearly suggests 13.31 GB for one 75M-Place release and
26.61 GB for current plus rollback. This is much more stable than the tiny
1,768-row fixture result, but remains a California-biased projection with a
minimal field set—not a global forecast.

At the project's current hundreds-of-queries-per-day traffic, R2 reads remain
well inside the monthly free allowance even with the measured inefficient query
plans. Build capacity and retrieval latency/fanout matter more than operation
cost at current usage.

## Decision

The factory is suitable for monthly, partitioned global index construction. It
does not rescue the current retrieval layout by itself.

The next architecture iteration should combine:

1. Region/cell-local document IDs and result pages so location-routed results
   are physically clustered.
2. A small global head index for famous/unlocated queries, with compact top
   result projections colocated with the posting root.
3. Explicitly restricted behavior for broad, unlocated prefixes instead of
   reading every regional tail.
4. One-million-row build partitions, resumable content-addressed output, and
   8-way factory concurrency as the initial resource envelope.
