# Places tokenizer decision

Date: 2026-07-16

Use tokenizer version `nfkd-lower-stripmark-cjk-bigram-v3` for the compact Places
global serving shards. V3 replaces the Python-only case-fold/base-script logic
from v2 with a contract the Python builder and Rust Worker can reproduce exactly.

The normalization contract is:

- Unicode lowercase, then NFKD;
- remove every combining mark;
- retain runs of Unicode alphanumeric characters plus ASCII underscore; and
- collapse all other separators between runs.

Index each normalized word as a whole token. For every contiguous Han,
Hiragana, Katakana, or Hangul run, also index overlapping two-character grams.
A one-character CJK run keeps its unigram. Deduplicate tokens while preserving
their order.

Examples:

| input | normalized/indexed |
|---|---|
| `Café` | `cafe` |
| `スターバックス` | `スターハックス`; full token plus `スタ`, `ター`, `ーハ`, … |
| `スターハックス` | the same normalized token (a known v3 recall/precision tradeoff) |
| `東京タワー` | full token plus `東京`, `京タ`, `タワ`, `ワー` |

This is deliberately a bounded, cross-runtime retrieval contract, not linguistic word
segmentation. It supports exact full names and CJK substring/prefix discovery
without a language-specific dictionary. Single-character CJK prefixes and
broad prefix enumeration remain unsupported product queries because their
fanout can be extreme. Transliteration, typo tolerance, and morphological
analysis remain separate relevance work.

Stripping all combining marks means voiced Japanese kana can collide with their
unvoiced forms. That is accepted for this PoC because silently building and
querying with different token bytes is worse; a future tokenizer can preserve
those distinctions once both runtimes share a pinned Unicode category table.

The tokenizer version is embedded in every compact-shard directory, and the
Worker rejects a different version rather than silently mixing query/index
semantics.
