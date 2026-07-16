# Places global-head single-object repack spike

- Input: `exports/places-ca-1m.parquet` (1,000,000 Places)
- Input SHA-256: `0bc3f28887b2d93a0a204d036d97d141395270978189870909e20713dd74a786`
- Baseline packed head: 4,090 objects / 26,088,333 bytes (26.09 B/place), 26,051 keys
- Single-object repack: 1 object / 25,753,724 bytes (key index 385,153 B + entries 25,368,382 B + directory 177 B + preamble 12 B)
- Entry sizes: min 558 / median 966 / mean 974 / p90 1107 / max 1,604 bytes
- Mean hit overfetch, bucket vs resident-entry: 7.1x

## Provenance and measured vs modeled

This re-extracts the California 1M sample with the deterministic extractor (`ORDER BY id` before `LIMIT`), so it supersedes the earlier locality-head spike's non-deterministic sample (which pinned ~4,088 head objects / 25.1 MB). The re-extracted sample reproduces 4,090 head objects / 26.1 MB via the same `build_heads` + `PackedHeadStore` machinery.

- **Measured** (real bytes on disk): the baseline object inventory, the single object and its component sizes, and the entry-size distribution.
- **Modeled** (range-read accounting, no network, no latency): the per-query reads/bytes below.

## Reads / bytes per query (diagnostic model; no latency measured)

| query | kind | head candidates | (i) bucket baseline (4,090 obj) | (ii) single resident | (iii) single cold |
|---|---|---:|---:|---:|---:|
| starbucks_exact | hit | 10 | 1 / 6,624 B | 1 / 791 B | 2 / 385,944 B |
| warfield_hotel_tokens | ineligible | n/a | head not consulted | head not consulted | head not consulted |
| golden_gate_prefix | ineligible | n/a | head not consulted | head not consulted | head not consulted |
| hotel_category | ineligible | n/a | head not consulted | head not consulted | head not consulted |
| sf_cafe_context | ineligible | n/a | head not consulted | head not consulted | head not consulted |
| starbucks_long_prefix | hit | 10 | 1 / 4,545 B | 1 / 791 B | 2 / 385,944 B |
| absent_exact_miss | eligible_miss | 0 | 1 / 5,073 B | 0 / 0 B | 1 / 385,153 B |

## Reads model

- (i) baseline: hash key -> one bucket object; a hit or a miss both read the whole bucket (all keys hashing there)
- (ii) resident: release manifest resolves the object layout and the key index is cached; a hit is one entry read, a miss is zero reads
- (iii) cold: read the whole key index once, then the entry; a miss stops after the index read. Excludes the object's own 12-byte preamble + 177-byte JSON directory, assumed resolved from the resident edge-cached release manifest

## Implications for the shared-reader prototype

These are reads/bytes shapes only; nothing was measured over a network and no latency is claimed.

- The bucket baseline reads a whole 6379 B-average bucket (up to 18,068 B) for every hit, carrying unrelated co-hashed keys. The single-object repack with a resident index reads only the matched entry, so a hit transfers the entry (median 966 B) at 1 read.
- A cold reader pays one key-index read before the entry. The whole key index is 385,153 B; if that is too large to fetch cold on the first query, a block/offset directory over the key index would let a cold hit read only one index block plus the entry, still 2 reads.
- On a miss, the bucket baseline still reads a whole bucket, while a resident-index reader answers a miss with zero object reads and a cold reader stops after the index read. The single object collapses the head from thousands of objects to one, which the shared reader can range-read exactly like a shard.

## Reproduction

```bash
python scripts/factory_extract_places.py \
  --release 2026-06-17.0 --limit 1000000 --output exports/places-ca-1m.parquet

python scripts/experiment_places_head_repack.py \
  exports/places-ca-1m.parquet \
  --object-out artifacts/places-ca-head.repack \
  --json-out benchmarks/places-head-repack-report.json \
  --markdown-out benchmarks/places-head-repack-report.md
```

Head reproduction + baseline + repack ran in ~28 s wall at ~1.9 GiB peak RSS (`/usr/bin/time -l`), one core.

