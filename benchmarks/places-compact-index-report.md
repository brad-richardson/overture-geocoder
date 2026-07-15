# Places compact-index experiment

This is a bounded range-read architecture spike, not a production size forecast.

- Input: `exports/experiment/places-raw.parquet` (1,768 named Places)
- Artifact: 230,576 bytes (130.4 bytes/place)
- Build: 0.020 seconds; 2,781 field/token keys
- SQLite FTS comparator: 512,000 bytes
- Existing unproven trie baseline: 121,426 bytes
- Linear shape only: 130,416,290 bytes at 1M Places; 9,781,221,719 bytes at 75M
- Historical full region SQLite: 794,624 bytes
- Historical prefix/minimal SQLite: 405,504 / 221,184 bytes

## Components

| component | bytes | bytes/place |
|---|---:|---:|
| directory | 2,501 | 1.4 |
| lexicon | 29,757 | 16.8 |
| postings | 32,351 | 18.3 |
| record_offsets | 14,152 | 8.0 |
| records | 151,815 | 85.9 |

## Retrieval comparison

Recall below is overlap with SQLite's top-k, not human-labelled relevance.

- Mean SQLite top-k recall: 0.98125
- Minimum SQLite top-k recall: 0.7
- Query bytes touched p50/max: 16,952 / 29,102
- Maximum simulated range reads: 70
- Optimistic 1 KiB-gap coalesced max ranges/bytes: 23 / 32,753

| query | recall | candidates | bytes touched | raw ranges | coalesced ranges/bytes |
|---|---:|---:|---:|---:|---:|
| Golden Gate Bridge | n/a | 0 | 11,900 | 12 | 6 / 12,077 |
| Disneyland | n/a | 0 | 9,445 | 6 | 4 / 9,445 |
| Tartine Manufactory | n/a | 0 | 6,975 | 5 | 4 / 6,975 |
| Starbucks | 1.00 | 2 | 7,145 | 10 | 8 / 7,409 |
| McDonald's | n/a | 0 | 9,482 | 6 | 4 / 9,482 |
| Warfield Hotel | 1.00 | 2 | 11,357 | 16 | 9 / 11,563 |
| warfield hot | 1.00 | 2 | 11,363 | 18 | 9 / 11,569 |
| YOTEL San Francisco | 1.00 | 1 | 18,215 | 19 | 12 / 18,221 |
| yotel san fra | 1.00 | 1 | 18,269 | 25 | 12 / 18,275 |
| Music City Hotel - Home of the San Francisco Music Hall of Fame | 1.00 | 1 | 29,088 | 68 | 16 / 32,461 |
| music city hotel home of the san francisco music hall of fam | 1.00 | 1 | 29,102 | 70 | 16 / 32,753 |
| Best Western Red Coach Inn | 1.00 | 1 | 16,952 | 25 | 13 / 16,952 |
| Courtyard by Marriott San Francisco Union Square | 1.00 | 1 | 26,870 | 29 | 17 / 27,520 |
| courtyard by marriott san francisco union squ | 1.00 | 1 | 26,870 | 29 | 17 / 27,520 |
| Hotel Adagio, Autograph Collection | 1.00 | 1 | 16,059 | 20 | 9 / 18,196 |
| hotel adagio autograph col | 1.00 | 1 | 17,375 | 28 | 9 / 19,494 |
| Luz Hotel | 1.00 | 1 | 11,257 | 14 | 10 / 11,257 |
| luz hot | 1.00 | 1 | 11,263 | 16 | 10 / 11,263 |
| San Francisco Proper | 1.00 | 6 | 22,470 | 32 | 16 / 24,481 |
| san francisco pro | 0.70 | 42 | 22,952 | 53 | 23 / 26,437 |

## Interpretation

**Verdict: do not replace SQLite yet; byte shape is promising, but remote range fanout and top-k ranking divergence need another design iteration**

The old 68.7 B/place radix trie was keyed only by normalized full primary name and returned only ID plus coordinates. It had no token search, aliases, category/context fields, ranking, display names, or independently addressable blocks; it is therefore a size floor, not a like-for-like competitor.

The new artifact proves the richer storage layout can be range-addressed, but does not yet prove it should replace SQLite. Raw ranges are conservative seek/read calls; the 1 KiB coalesced column is an optimistic lower bound calculated only after every offset is known and may overfetch bytes. A real remote reader needs staged directory, lexicon, postings, and record requests. A high range-count or low SQLite overlap is a failure signal, even if total bytes are attractive. The extrapolations above are deliberately linear shape calculations only; token fanout, language coverage, and source distributions can change bytes/place materially.
