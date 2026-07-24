# Places tokenizer decision

Date: 2026-07-16

Use tokenizer version `nfkd-lower-stripmark-cjk-bigram-v4` for the compact Places
global serving shards. V4 replaces the Python-only case-fold/base-script logic
from v2 with a contract the Python builder and Rust Worker can reproduce exactly,
and pins the three Unicode string semantics that v3 left unstated (see
`2026-07-24-places-digest-divergence-root-cause.md`).

The normalization contract is pinned to Rust semantics at **Unicode 17.0** (the
Rust standard-library tables for the word class and lowercase map, and
`unicode-normalization` 0.1.25 for NFKD and combining marks; both carry 17.0). `places-transform-v1` is authoritative; the Python
baseline drives its lowercase, whitespace and word-character tables from
`scripts/places_unicode_tables_v1.json`, exported from those same Rust tables, so
neither side carries an independent (version-skewed) Unicode opinion. The steps:

- trim leading/trailing Unicode `White_Space` only (Rust `str::trim`), which —
  unlike CPython `str.strip()` — keeps the C0 separators U+001C..U+001F;
- NFKD-decompose;
- lowercase per character (context-free `char::to_lowercase`, no Greek
  `Final_Sigma` rule), applied **after** NFKD so compatibility-decomposed styled
  capitals (e.g. `𝓓` → `D`) become lowercase-searchable;
- fold Greek final sigma `ς` (U+03C2) → plain `σ` (U+03C3) so a lowercase Greek
  query matches the context-free `σ` held in the index (applied in both the index
  tokenizer and the Worker query tokenizer);
- remove every combining mark;
- retain runs of Unicode alphanumeric characters (`char::is_alphanumeric`, which
  includes the `Other_Alphabetic` enclosed symbols CPython `str.isalnum` omits)
  plus ASCII underscore; and
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
