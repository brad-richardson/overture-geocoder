# Places locality + packed global-head factory spike

Date: 2026-07-14

## Verdict

This design is a qualified win, not yet a complete serving design.

- Keep the packed global top-k head concept. It answered the eligible common exact and long-prefix cases with exact top-10 results under the static confidence rank in one modeled object read and at most 5,839 bytes.
- Spatially clustering hydrated result records reduced bytes for the located `golden gat*` case, but it did not solve posting-list fanout. The located path still reached eight operations, so it failed the three-operation gate.
- Do not expand this into more duplicated hydrated-record tiers. The global head already adds 25.1 MB and 4,088 objects on the 1M-place sample. The next useful experiment is cell-local heavy postings plus the packed global head.

The top-k head is deliberately not a full-recall API. It is authoritative only for top-k under the experiment's static ranking. Full candidate enumeration and ineligible queries continue through the complete posting path.

## Factory run

The run used the isolated `~/tmp/overture-geocoder-kv-r2-spike-20260714` scratch directory on the always-on factory machine. It did not modify either existing remote repository or register the machine as a GitHub Actions runner.

- Input: 1,000,000 source-order Places in the factory rectangle from Overture release `2026-06-17.0`
- Selection: `-124.5 <= xmin <= -114.0`, `32.5 <= ymin <= 42.1`; no boundary clip, randomization, or ordering
- Input SHA-256: `4c4cb3711e806a08801ed87d08c0f2acbc2f7b3f1d69796d65a3824f253c6f84`
- Wall time: 4:07.02
- CPU: one core at 100%
- Peak RSS: 3,950,364 KiB (3.77 GiB)
- Swap: none
- Spatial grid: 0.25 degrees, 139 occupied cells
- Head eligibility: at least 64 candidates
- Head result limit: 10
- Packed-head target: 64 KiB objects, 4,096 hash buckets selected

The located-query benchmark is optimistic: it routes each query to the cell containing its globally highest-ranked match. It is useful for measuring the storage layout, but it is not evidence that a production country/cell router can always select that cell.

## Inventory

| component | objects | modeled bytes |
|---|---:|---:|
| lexical directory + complete postings | 67,136 | 88,542,481 |
| cell-clustered hydrated results | 1,470 | 89,330,417 |
| packed global top-k head (25,005 keys) | 4,088 | 25,072,410 |
| **total** | **72,694** | **202,945,308** |

The total is 202.9 bytes/place. The previous uniform 256 KiB 1M-place spike modeled 177.4 bytes/place and 67,477 objects, so this design adds about 14.4% in bytes and 7.7% in objects.

Using the earlier 75M-place working count only as a linear sizing diagnostic:

| projection | one release | two releases |
|---|---:|---:|
| total bytes | 15.22 GB | 30.44 GB |
| objects | 5.45 million | 10.90 million |
| global-head bytes alone | 1.88 GB | 3.76 GB |

These are not planet forecasts. California term distributions, occupied-cell density, compression, partition boundaries, and per-release churn will differ globally. The projection does show that raw storage bytes are less alarming than object inventory, publication work, and release-to-release replacement.

## Query results

| query | complete fallback | optimistic located | packed head | result |
|---|---:|---:|---:|---|
| `starbucks` | 3 ops / 141,200 B | 3 / 141,196 B | 1 / 5,839 B | exact top-10 |
| `golden` + `gat*` | 12 / 677,560 B | 4 / 153,797 B | ineligible | located misses op gate |
| category `hotel` | 2 / 84,718 B | 2 / 84,718 B | ineligible | complete recall |
| `starbu*` | 8 / 162,463 B | 8 / 162,459 B | 1 / 3,937 B | exact top-10 |

All exercised fallback and located paths had complete candidate recall. Both exercised packed-head paths matched the oracle top-10 exactly and intentionally omitted their candidate tails.

## Recommended next shape

Use three bounded tiers:

1. A small packed global head for high-frequency, single-clause exact/prefix queries. Build only static top-k results and consider eventually choosing keys from observed query demand instead of every term over a frequency threshold.
2. Cell-local postings for heavy terms and prefixes. A located query should read only the selected cell's posting fragments; this directly attacks the remaining four-to-eight-operation fanout.
3. Complete global postings as the correctness fallback for multi-clause, fielded, unlocated, or tail-enumeration requests.

The next spike should avoid another full hydrated-record copy. It should measure whether cell-local heavy postings can put located exact/prefix queries under three reads while keeping incremental bytes materially below the 25.1 MB head overhead measured here.

## Reproduction

```bash
.venv/bin/python scripts/experiment_places_locality_head.py \
  exports/places-ca-1m.parquet \
  --json-out reports/places-ca-1m-locality-head.json \
  --markdown-out reports/places-ca-1m-locality-head.md
```
