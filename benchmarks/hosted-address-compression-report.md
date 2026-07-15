# Hosted address format/compression spike

Date: 2026-07-15  
GitHub Actions run: [29442273324](https://github.com/brad-richardson/overture-geocoder/actions/runs/29442273324)

## Verdict

The storage gate is no longer an immediate reason to abandon the address
experiment. A range-local, self-contained response preserved every field in the
reduce artifact and measured **35.51 B/indexed row**, 76.0% below the naive
148.05 B baseline. Its group-aligned gzip pages target 256 rows and measured
8.98 KB at p50 and 10.11 KB at p95, so a lookup can fetch and inflate one small page rather than a
whole shard or Parquet row group.

This is promising bounded evidence, not approval for planet publication. The
follow-up [Worker decoder spike](hosted-address-worker-decoder-report.md) passed:
434 ms cold wall time and 156 ms warm median for the 137-candidate oracle. The
remaining gate is global completeness and skew sampling; the measured source
range retained only addresses with both street and number.

## Measured formats

All four formats preserve candidate order, IDs, normalized lookup keys,
quantized coordinates, and exact Overture row-group/row locators. “Useful” also
preserves original display strings and every raw `address_levels` value.

| format | response fidelity | bytes/row | total | p50 page | p95 page | full decode + verification |
|---|---|---:|---:|---:|---:|---:|
| bare | normalized only | 46.47 | 64.23 MB | 11.77 KB | 12.42 KB | 15.50 s |
| bare + gzip | normalized only | 30.23 | 41.78 MB | 7.65 KB | 8.34 KB | 15.50 s |
| useful | lossless vs reducer | 59.67 | 82.48 MB | 15.22 KB | 16.65 KB | 20.39 s |
| **useful + gzip** | **lossless vs reducer** | **35.50** | **49.07 MB** | **8.98 KB** | **10.11 KB** | **21.75 s** |

The bare gzip format saves only 5.28 B/row (2.50 GB across all 473M planning
rows) versus useful gzip while discarding display fidelity and raw context. That
is a poor default trade: keep the useful response unless Worker measurements
show a severe reason not to.

## Format shape

Each page is independently decodable:

```text
sparse side index: first normalized key -> byte offset + byte length
data page (target 256 rows; never split an exact-candidate group):
  page-local display-string dictionary
  page-local address-level-sequence dictionary
  front-coded normalized keys
  UUID + int32 E7 lon/lat + source row-group/row
  dictionary IDs for exact display/context fields
  optional independent gzip envelope
```

The side index was only 359,010 bytes for 1,382,264 rows. The full experiment
decoded and hashed every generated record back to its source representation.
It also executed indexed lookups for three oracle groups, including the exact
137-candidate maximum-fanout group, and verified that candidate groups never
cross page boundaries. No result truncation or
lossy coordinate change was introduced; coordinates were already E7 integers
in the reducer baseline.

## Planet-scale diagnostics

Linearizing the winning 35.50 B shape across all 473M planning rows gives
16.79 GB for one address release. Adding the existing 8.75 GB compact Places
diagnostic gives about **25.54 GB**, leaving roughly 14.46 GB under the 40 GB
combined stop gate for catalogs, skew, aliases, and estimation error.

If the measured 36.93% structured-address retention happened to hold globally,
the address portion would be about 6.20 GB. It almost certainly will not hold
uniformly, so 16.79 GB is the more useful conservative diagnostic—not a global
forecast. At the winning density, 2–4M-row immutable shards are about 71–142 MB,
inside the prior 96–192 MB neighborhood without exploding object count.

## Processing and remaining risks

The format pass processed and fully verified all 1.38M rows and four variants
in 2m55s with 33.9 MB peak RSS. Per-variant useful-gzip encoding accounted for
21.92 seconds; decode and verification accounted for 21.75 seconds. The entire
hosted projection + reduce + four-format job passed in 5m01s.

Before treating this as a design:

1. Repeat the Worker measurement with a representative large side index,
   multiple page-size tails, and more than one request geography.
2. Sample small/median/large and non-US objects to measure structured-address
   retention, dictionary behavior, page-size tails, and shard skew.
3. Include multi-source filepath dictionaries and real byte-balanced reduce
   partitions; this run binds one source object outside row records.
4. Budget catalogs, release overlap, aliases, and retries. Do not spend the
   apparent 14.46 GB headroom before those measurements.
5. Keep hydration optional. The useful page is already a meaningful response;
   Overture Parquet/zstd decoding is not on the normal query path.

No R2 object, artifact upload, catalog, or production state was written.
