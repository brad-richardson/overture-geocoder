# Places construction-v1 provisional contract

Status: local-only and replaceable until a planet result is accepted.

## Boundary and semantics

The physical Arrow input is a flattened, columnar projection: `id`,
`primary_name`, `common_names: list<string>`, `brand_name`, `category`,
`locality`, `region`, `country`, `confidence: float64`, `operating_status`,
strict 2D Point WKB, and signed `int32` object/group/row locators. Hydration may
flatten source nesting with Arrow arrays but never creates Python feature rows.

Rust is authoritative for admission, normalization, tokenization, routing,
ranking, and proof payloads. Rejection precedence is missing primary name,
invalid UUID, permanently closed, invalid geometry, invalid confidence,
invalid source locator, oversized record, invalid record. Names use primary plus
multilingual common values. Tokenization preserves alphanumeric/underscore words
and adds CJK bigrams. Field masks are name `1`, brand `2`, category `4`, context
`8`; repeated tokens combine masks.

### Pinned tokenizer semantics (`nfkd-lower-stripmark-cjk-bigram-v4`)

The tokenizer is pinned to Rust standard-library semantics at **Unicode 17.0**,
as implemented by `places-transform-v1` and mirrored in the Worker query
tokenizer. In order: trim leading/trailing Unicode `White_Space` only (Rust
`str::trim`, so the C0 separators U+001C..U+001F are *kept*, unlike CPython
`str.strip`); NFKD-decompose; lowercase per character with context-free
`char::to_lowercase` (no Greek `Final_Sigma`), applied **after** NFKD so
compatibility-decomposed styled capitals fold to lowercase; fold final sigma `ς`
(U+03C2) → `σ` (U+03C3); drop combining marks; keep runs of
`char::is_alphanumeric` (which includes the `Other_Alphabetic` enclosed symbols
CPython `str.isalnum` omits) plus ASCII `_`; and split on everything else.

Because a dual-implementation digest contract is implicitly pinned to a Unicode
version, the Python semantic baseline does **not** use CPython's Unicode tables
for the lowercase mapping, whitespace set, or word-character class. It loads
`scripts/places_unicode_tables_v1.json`, exported from the Rust toolchain by the
`places-unicode-tables-v1` binary, so the two implementations cannot drift apart
across CPython/Rust Unicode upgrades. (NFKD decomposition and the combining-mark
filter remain CPython's; they matched Rust exactly on all local task data, and a
regenerated table is required before adopting a new Unicode version.) This
`v4` string replaces `v3`, which never pinned these behaviours; nothing shipped
on `v3`, so there is no back-compatibility obligation.

Confidence must be finite and in `[0,1]`; rank is `round(confidence*255)`. The
provisional spatial grid is 256 by 256: `x=floor((lon+180)/360*256)` and
`y=floor((lat+90)/180*256)`, clamped at the upper edge. Partition key is
`y<<8|x`, cell is four lowercase hex digits `yyxx`, and execution group is the
first two cell digits. This is intentionally simpler than predecessor lineage.

## Logical rows and proofs

The transform emits one row per distinct feature token. Each row carries group,
cell, key, token, combined field mask, rank, UUID, coordinates, display fields,
unsigned provenance, and two SHA-256 semantic digests. Total order is group,
cell, UTF-8 token, descending rank, UUID, object, row group, row index. Exact
count plus addition modulo `2^256` in two independent domains is the proof
frame. Duplicate UUIDs remain distinct through provenance.

Map packs are deterministic typed Parquet in fresh `map/places-v1/` content
namespaces with exact pack/row-group directories and marker-last completion.
Genesis has no predecessor: cells are grouped into replaceable reduce
assignments under a row cap. Reducers select overlapping pack row groups,
discard non-owned rows, and reconcile selected plus discarded bindings to every
input proof.

Routed serving artifacts are token-to-ranked-projection tables scoped by cell.
The global head uses the exact same transformed term rows and keeps top ten by
descending rank then UUID/provenance. Both formats are new, independently
verified, and have dormant Worker decoders; no old Places bytes are accepted.
