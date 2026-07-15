# Places cell-local heavy-postings factory spike

Date: 2026-07-14

## Verdict

Reject complete precomputed cell-local prefix postings as a serving tier.

They improve query fanout and retain complete candidate recall, but the best bounded configuration still adds 86.8 MB and 10,944 objects to the 1M-place sample. It also fails the three-operation located-query gate. The remaining four-read case is two posting objects plus two scattered result-hydration pages, so adding more posting duplication cannot remove the final read.

The experiment supports keeping the packed global top-k head. The next architecture should instead use a compact per-spatial-shard binary containing an exact-token lexicon, delta-coded exact postings, and record offsets. Prefixes should resolve through the lexicon at query time rather than publishing complete posting unions for every prefix length.

## Factory runs

All runs used the same 1,000,000 California Places sample from Overture release `2026-06-17.0` in the isolated factory scratch directory. They were single-core model builds and did not alter remote repositories or runner configuration.

| cell eligibility | bucket layout | cell-local keys | cell-local objects | cell-local bytes | total bytes | worst located ops/bytes |
|---:|---|---:|---:|---:|---:|---:|
| 64 candidates | global 1,024 buckets/cell | 24,920 | 89,819 | 128,303,659 | 331,248,967 | 4 / 139,113 B |
| 512 candidates | global 1,024 buckets/cell | 7,010 | 85,036 | 106,654,752 | 309,600,060 | 4 / 137,706 B |
| 1,800 candidates | adaptive 64–1,024 buckets/cell | 2,873 | 10,944 | 86,785,108 | 289,730,416 | 4 / 168,262 B |

The final 1,800 cutoff was chosen just below the least frequent clause exercised by the benchmark (`golden`, 1,813 candidates). That makes it an optimistic bound rather than a generally validated production threshold.

Final-run builder measurements:

- Wall time: 4:32.06
- Peak RSS: 4,458,268 KiB (4.25 GiB)
- CPU: one core at 100%
- Swap: none

The factory machine can build this layout comfortably. Builder feasibility does not make the publication shape attractive.

## Final bounded inventory

| component | objects | modeled bytes |
|---|---:|---:|
| lexical directory + complete global postings | 67,136 | 88,542,481 |
| cell-clustered hydrated results | 1,470 | 89,330,417 |
| packed global top-k head | 4,088 | 25,072,410 |
| complete cell-local heavy postings | 10,944 | 86,785,108 |
| **total** | **83,638** | **289,730,416** |

This is 289.7 bytes/place, 42.8% larger than the preceding locality + global-head layout and 63.3% larger than the original uniform-page 1M model.

Using the earlier 75M-place working count only as a linear diagnostic:

- Total: about 21.73 GB and 6.27 million objects per release.
- Two-release retention: about 43.46 GB and 12.55 million objects.
- The cell-local posting tier alone: about 6.51 GB per release.

California term/cell distributions are not representative of the planet, and the threshold was selected against this query set. These figures should not be treated as forecasts.

## Query results

| located query | global clause candidates | complete fallback | bounded cell-local | recall |
|---|---:|---:|---:|---|
| `starbucks` | 2,387 | 3 ops / 141,200 B | 3 / 146,793 B | complete |
| `golden` + `gat*` | 1,813 + 3,677 | 12 / 677,560 B | 4 / 168,262 B | complete |
| category `hotel` | 4,927 | 2 / 84,718 B | 2 / 90,933 B | complete |
| `starbu*` | 2,395 | 8 / 162,463 B | 3 / 164,496 B | complete |

The local tier successfully turns a long prefix from eight reads into three and the multi-clause prefix from twelve reads into four. Its byte transfer can be slightly worse for simpler queries because an adaptive root bucket contains unrelated posting entries.

## What to carry forward

Keep:

- Spatially ordered document IDs.
- The packed global top-k head for popular unlocated single-clause queries.
- Complete global fallback semantics.
- Per-cell adaptive sizing rather than forcing the densest cell's hash fanout on every cell.

Drop:

- Complete materialized posting unions for prefix lengths 2–8.
- A separate R2 object namespace containing hundreds of thousands of term/cell entries.
- The assumption that posting locality alone solves hydration locality.

## Recommended next experiment

Build one compact binary per approximately 1M-row spatial partition, split into addressable sections:

1. A compact sorted lexicon or finite-state trie mapping exact tokens and prefix ranges to posting offsets.
2. Delta-coded exact-token postings only; no precomputed long-prefix unions.
3. A minimal ranked result projection and offset table, with optional Overture S3 hydration for fields outside the search response.
4. A small partition manifest containing byte ranges, checksums, release ID, and rollback metadata.

The experiment should model range reads into these larger shard objects and compare object count, prefix fanout, and bytes against the page layouts. The acceptance target is hundreds—not millions—of immutable objects per planet release, while preserving the one-object packed head path and an exact fallback.

## Reproduction

```bash
.venv/bin/python scripts/experiment_places_cell_postings.py \
  exports/places-ca-1m.parquet \
  --json-out reports/places-ca-1m-cell-postings-1800-adaptive.json \
  --markdown-out reports/places-ca-1m-cell-postings-1800-adaptive.md \
  --head-minimum-candidates 64 \
  --cell-minimum-candidates 1800
```
