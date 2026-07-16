# Places tokenizer decision

Date: 2026-07-16

Use tokenizer version `nfkd-latin-fold-cjk-bigram-v2` for the compact Places
prototype.

The normalization contract is:

- Unicode case-fold, then NFKD;
- remove combining marks only when their base character is Latin;
- preserve non-Latin combining marks, including Japanese dakuten and
  handakuten;
- recompose with NFC; and
- retain Unicode word runs.

Index each normalized word as a whole token. For every contiguous Han,
Hiragana, Katakana, or Hangul run, also index overlapping two-character grams.
A one-character CJK run keeps its unigram. Deduplicate tokens while preserving
their order.

Examples:

| input | normalized/indexed |
|---|---|
| `Café` | `cafe` |
| `スターバックス` | preserves `バ`; full token plus `スタ`, `ター`, `ーバ`, … |
| `スターハックス` | remains distinct from `スターバックス` |
| `東京タワー` | full token plus `東京`, `京タ`, `タワ`, `ワー` |

This is deliberately a bounded retrieval contract, not linguistic word
segmentation. It supports exact full names and CJK substring/prefix discovery
without a language-specific dictionary. Single-character CJK prefixes and
broad prefix enumeration remain unsupported product queries because their
fanout can be extreme. Transliteration, typo tolerance, and morphological
analysis remain separate relevance work.

The tokenizer version is embedded in every compact-shard directory, and the
Worker rejects a different version rather than silently mixing query/index
semantics.
