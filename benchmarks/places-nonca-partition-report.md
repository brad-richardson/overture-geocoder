# Places non-California partition stability spike (tokyo vs california)

All byte/skew numbers in the settled compact-shard direction came from one
California-area 1M sample. This rebuilds the same compact shard on a second
partition (tokyo) under the identical deterministic extractor and shard builder
and compares the shapes. Diagnostics only; no latency was measured.

## Provenance

- california: `exports/places-ca-1m.parquet` (1,000,000 places)
- tokyo: `exports/places-tokyo-1m.parquet` (1,000,000 places)
- Both are source-order-free deterministic samples (ORDER BY id before LIMIT)
  of a rectangular bbox from release 2026-06-17.0; neither is exact administrative
  containment nor a representative random sample.

## Headline comparison

| metric | california | tokyo |
|---|---:|---:|
| bytes/place | 116.4 | 122.9 |
| artifact bytes | 116,416,996 | 122,947,224 |
| exact tokens (lexicon entries) | 209,784 | 736,628 |
| avg distinct tokens/place | 7.69 | 4.99 |
| max token frequency | 960,061 | 999,996 |
| top-10 token share of postings | 0.307 | 0.291 |
| singleton-token proportion | 0.627 | 0.816 |
| CJK-dominant lexicon proportion | 0.006 | 0.860 |
| record bytes p50/p99 | 78/118 | 82/135 |
| build seconds (one core) | 48.6 | 20.3 |

## Component storage (bytes/place)

| component | california | tokyo |
|---|---:|---:|
| directory | 0.1 | 0.4 |
| lexicon | 2.3 | 13.2 |
| postings | 26.4 | 18.0 |
| record_index | 8.0 | 8.0 |
| records | 79.7 | 83.4 |

## Lexicon script mix (tokyo)

| script | tokens | proportion | mean chars | median chars | max chars |
|---|---:|---:|---:|---:|---:|
| digit | 2,259 | 0.003 | 5.0 | 4 | 105 |
| han | 329,006 | 0.447 | 6.7 | 6 | 43 |
| kana | 304,347 | 0.413 | 8.4 | 8 | 51 |
| latin | 98,736 | 0.134 | 7.5 | 7 | 55 |
| other | 31 | 0.000 | 3.5 | 2 | 13 |
| other_letter | 2,249 | 0.003 | 5.9 | 5 | 33 |

## Posting skew, top 25 tokens (tokyo)

| token | script | frequency | share |
|---|---|---:|---:|
| `jp` | latin | 999,996 | 0.2005 |
| `東京都` | han | 116,666 | 0.0234 |
| `japanese_restaurant` | latin | 88,444 | 0.0177 |
| `神奈川県` | han | 45,696 | 0.0092 |
| `convenience_store` | latin | 36,577 | 0.0073 |
| `港区` | han | 35,189 | 0.0071 |
| `埼玉県` | han | 33,960 | 0.0068 |
| `渋谷区` | han | 33,383 | 0.0067 |
| `rental_kiosks` | latin | 30,898 | 0.0062 |
| `新宿区` | han | 30,657 | 0.0061 |
| `bar` | latin | 30,457 | 0.0061 |
| `千代田区` | han | 26,262 | 0.0053 |
| `中央区` | han | 26,151 | 0.0052 |
| `千葉県` | han | 25,705 | 0.0052 |
| `atms` | latin | 25,032 | 0.0050 |
| `cafe` | latin | 24,395 | 0.0049 |
| `restaurant` | latin | 24,205 | 0.0049 |
| `世田谷区` | han | 22,335 | 0.0045 |
| `hair_salon` | latin | 20,377 | 0.0041 |
| `タイトー` | kana | 19,251 | 0.0039 |
| `タイトートリンコ` | kana | 19,209 | 0.0039 |
| `pharmacy` | latin | 17,520 | 0.0035 |
| `台東区` | han | 16,079 | 0.0032 |
| `横浜市` | han | 15,950 | 0.0032 |
| `beauty_salon` | latin | 15,837 | 0.0032 |

## Multilingual tokenizer behavior (measured finding, not fixed here)

The shared tokenizer is `[\w]+` over NFKD-folded text. On tokyo names it
shows two measurable weaknesses. Both are reported, not repaired:

1. CJK segmentation: space-free CJK names collapse into one long token, so
   interior words are unreachable by exact/prefix search. Deterministic examples:

| name | chars | tokens | token count |
|---|---:|---|---:|
| ゲオ御殿場店 | 6 | `ケオ御殿場店` | 1 |
| スターバックス | 7 | `スターハックス` | 1 |
| クアハウス山小屋 | 8 | `クアハウス山小屋` | 1 |
| ゴルフパートナー南浦和店 | 12 | `コルフハートナー南浦和店` | 1 |

The token column is post-normalization, so it also carries the dakuten loss from
finding 2 (e.g. `スターバックス` -> single token `スターハックス`): a real POI name
that is both unsegmented and altered.

2. NFKD + combining strip alters Japanese text: voiced kana lose their dakuten
   (merging distinct sounds) and halfwidth/fullwidth forms fold together.
   Deterministic probes:

| input | normalized |
|---|---|
| `ガ` | `カ` — voiced kana lose their dakuten under NFKD + combining strip |
| `カ` | `カ` — halfwidth/fullwidth folded to ascii/base form |
| `パ` | `ハ` — voiced kana lose their dakuten under NFKD + combining strip |
| `ハ` | `ハ` — halfwidth/fullwidth folded to ascii/base form |
| `東京` | `東京` — halfwidth/fullwidth folded to ascii/base form |
| `ﾄｳｷｮｳ` | `トウキョウ` — halfwidth/fullwidth folded to ascii/base form |
| `Ｔｏｋｙｏ` | `tokyo` — halfwidth/fullwidth folded to ascii/base form |

## Retrieval-oracle verification

- tokyo fixed CASES: complete candidate recall `True`, exact top-k `True` (4 nonempty of the English/CA cases).
- tokyo data-derived in-partition cases: complete candidate recall `True`, exact top-k `True` over 4 top-token queries (han/kana/latin).
- california data-derived cases: complete candidate recall `True`, exact top-k `True`.

## Implications for the shared-reader prototype

Reads/bytes shape only; nothing was measured over a network.

- Bytes/place moved from 116.4 (california) to 122.9 (tokyo); the compact-shard byte model is stable across these two partitions, so the ~1M-place shard target and object inventory hold.
- The total is stable but the components rebalance: the lexicon grows (2.3 -> 13.2 B/place, driven by 3.5x more, longer, multibyte CJK tokens) while postings shrink (26.4 -> 18.0 B/place, because CJK names collapse to fewer tokens/place). A reader that caches per-shard lexicons should budget for the larger CJK lexicon.
- Lexicon size and average tokens/place differ (209,784 vs 736,628 tokens; 7.69 vs 4.99 tokens/place), with the Tokyo lexicon 82% singletons: CJK segmentation collapses multi-word names into single, mostly-unique long tokens. The reader's range shapes are unaffected, but query planning/relevance must add CJK segmentation before a multilingual serving claim.
- The shard build and its range-read layout are correct on the non-CA partition (oracle equivalence holds on both fixed and data-derived tokens), so the shared reader can treat any partition uniformly. Tokenizer/relevance quality is the gap, not storage or object shape.

## Build cost and reproduction

- Measured build wall (one core, `build_artifact`): california 48.6 s, tokyo 20.3 s. The whole two-partition comparison peaked at ~2.2 GiB RSS (`/usr/bin/time -l`). The california build ran first (cold caches) and does more posting work (7.69 vs 4.99 tokens/place), so its wall is higher; both are well inside the factory build envelope.

```bash
python scripts/factory_extract_places.py \
  --release 2026-06-17.0 --limit 1000000 --output exports/places-ca-1m.parquet
python scripts/experiment_places_partition_extract.py \
  --release 2026-06-17.0 --limit 1000000 \
  --xmin 138.85 --xmax 140.9 --ymin 34.9 --ymax 36.4 \
  --output exports/places-tokyo-1m.parquet
python scripts/experiment_places_partition_compare.py \
  --baseline-input exports/places-ca-1m.parquet --baseline-label california \
  --baseline-artifact artifacts/places-ca-1m.pcsh \
  --input exports/places-tokyo-1m.parquet --label tokyo \
  --artifact artifacts/places-tokyo-1m.pcsh \
  --json-out benchmarks/places-nonca-partition-report.json \
  --markdown-out benchmarks/places-nonca-partition-report.md
```

