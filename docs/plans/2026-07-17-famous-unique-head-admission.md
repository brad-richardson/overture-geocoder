# Famous-unique packed-head admission

Date: 2026-07-17
Status: scoped design + implementation spec. This clears (or measurably fails)
the last relevance STOP seed from the 2026-07-16 routed Places runs.

## Problem

The `famous_unique` relevance seed ("Tokyo Tower", no context) returns zero
results. The context-free path is packed-head-only, and the head fails this
query twice over:

1. **Admission is density-gated.** `build_heads`
   (`scripts/experiment_places_locality_head.py`) admits `e:{token}` only when
   the token's exact posting list has at least `head_minimum_candidates` (64)
   documents. Rare tokens attached to prominent places — the English alt-name
   token `tower` in the Tokyo fixture — are exactly the famous-unique query
   surface and are exactly what the rule excludes. A missing entry for any
   clause makes `query_head` (and the Worker's `lookup_places_head_spike`)
   return a head miss, i.e. zero results.
2. **Intersection is top-k-membership-gated.** Even with both entries present,
   a two-token query intersects the two top-10 lists by ID. A famous place must
   independently rank top-10 by quantized confidence for *each* token. For a
   dense token like `tokyo` (present in thousands of records via the context
   field), the top-10 is dominated by other high-confidence places, so the
   intersection silently drops the famous target. Density-gating plus
   top-k intersection is structurally backwards for famous-unique queries.

Fixing admission alone is insufficient because of failure mode 2; fixing
intersection alone is insufficient because of failure mode 1.

## Design

Two additive key families in the existing single-object repacked head
(`PHRP` format), no change to the framed record encoding, no new objects, no
new read on the query path.

Fame proxy: quantized confidence `round(confidence * 255)`, the same signal
the head already ranks by. Confidence is not literally fame; the relevance
panel remains the arbiter, and the seed gate is the acceptance test.

### 1. Famous set selection (builder)

Deterministically select a famous set `F`: the top `head_famous_cap` places by
`(-quantized_confidence, stable serving order)` over the combined ordered
input. `head_famous_cap` is a builder parameter (fixture default: 1024;
the planet value is a later, separately measured decision). A hard cap, not a
confidence floor, keeps the added bytes bounded regardless of the confidence
distribution. Token source per famous place: the tokenized `name` field text
(primary + alt names) plus `brand` — not category/context, which would
reintroduce density.

### 2. Rare-prominent single-token admission (builder)

For every token of every place in `F`: admit `e:{token}` even when its posting
count is below `head_minimum_candidates`. Entry semantics are unchanged — the
entry is the global top-`head_limit` by `(-quantized_confidence, doc)` over
*all* documents holding that token, identical to dense entries. This fixes
failure mode 1 and single-token famous queries (e.g. a rare transliterated
name).

### 3. Famous pair keys (builder)

For each place in `F`, take its first 8 distinct name/brand tokens (in
tokenizer emission order, for determinism) and emit every unordered pair as
key `e2:{a} {b}` with `a < b` lexicographically (≤ 28 pairs/place). The entry
is the top-`head_limit` by `(-quantized_confidence, doc)` of the *exact
posting intersection* of the two tokens, framed identically to `e:` entries.
Build-time posting intersection is cheap at fixture scale; planet build cost
is part of the later global-head measurement, not this slice.

All keys — `e:` and `e2:` — live in the one sorted key index (byte-sorted;
`e2:` sorts before `e:` since `'2' < ':'`, which is fine — the index only
requires global sort, which `encode_key_index` already enforces).

### 4. Reader probe order (Python oracle + Rust Worker, in lockstep)

For a two-clause head-eligible query: first probe `e2:{sorted token pair}`.
On a hit, serve that entry directly (it is by construction the correct
bounded top-k of the AND). On a miss, fall back to the existing per-token
top-10 intersection unchanged. One-clause queries are unchanged and benefit
from admission automatically. Reads stay: cold = key index + one coalesced
entry fetch (≤ 4 physical reads); resident = one entry read.

Both readers must change identically, and the smoke's producer-oracle
equality is the enforcement:

- `scripts/prepare_places_worker_smoke.py::query_head` — probe `e2:` first.
- `crates/geocoder-worker/src/places_pages.rs::lookup_places_head_spike` —
  same probe order, same tie-breaks, same truncation.

### Directory/versioning

Add explicit provenance fields to the head directory JSON (additive):
`head_famous_cap`, `e2_key_count`, and an `admission` marker. Decide
explicitly whether to keep `schema_version: 1` with additive fields or bump
to 2; whichever is chosen, the Rust directory check and the Python builder
must agree and fail closed on mismatch. The smoke always builds fixture and
deploys Worker from the same commit, so no cross-version serving exists.

## Files to change

- `scripts/experiment_places_locality_head.py` — `build_heads` gains famous
  set selection, rare-prominent admission, and `e2:` pair generation
  (parameters threaded, defaults preserving old behavior when
  `head_famous_cap=0` so the historical spike reports remain reproducible).
- `scripts/experiment_places_head_repack.py` — parameter plumb-through and
  report fields (key counts, bytes split by `e:`/`e2:`).
- `scripts/prepare_places_worker_smoke.py` — `query_head` probe order; oracle
  cases regenerate; report the head object/key-index byte deltas.
- `crates/geocoder-worker/src/places_pages.rs` (+ `handlers.rs` if the route
  label needs an `packed_head_pair` distinction — keep `route` values stable
  unless the smoke asserts on them).
- `tests/test_experiment_places_locality_head.py`,
  `tests/test_experiment_places_head_repack.py`,
  `tests/test_prepare_places_worker_smoke.py`,
  `tests/generate_places_page_fixtures.py` + Rust fixture tests — cover the
  new admission/probe paths and Python↔Rust equivalence.
- `docs/places-routed-prototype-gates.md` — amend the head-eligibility
  paragraph: two-token queries consult the famous pair entry first, then the
  per-token intersection. Keep "a packed entry is acceleration evidence, not
  relevance evidence."

## Acceptance gates

1. **Relevance**: the regenerated `relevance_famous_unique` oracle returns
   Tokyo Tower in the top five (target rank 1) with stable repeat order, via
   the local fixture path. Post-merge, `smoketest-places-worker.yml` (main-only,
   credentialed) must reproduce it through real R2.
2. **No regression**: the other five relevance seeds and all technical cases
   keep their oracle results; untouched entries stay byte-identical; two
   consecutive builds produce identical object SHA-256s.
3. **Byte/read budget**: cold packed-head gate unchanged — ≤ 4 physical reads,
   ≤ 256 KiB, ≤ 1.0 s. The whole key index is read cold, so report key-index
   bytes before/after; if pair keys push the cold path over budget, shrink
   `head_famous_cap` and record the trade-off rather than relaxing the gate.
4. **Unit coverage**: rare-prominent admitted; rare-non-prominent still
   excluded; dense behavior unchanged; pair caps enforced; sorted-index
   invariant holds with mixed `e:`/`e2:` keys; Python and Rust readers agree
   on hit, miss, fallback, tie-break, and truncation.

## Non-goals

Three-or-more-token queries, prefix/fielded context-free queries, typo
tolerance, any located-routing change, any fame signal beyond stored
confidence, and planet-scale head format ratification (still gated on the
separate multi-country head measurement). Unsupported queries keep returning
their current bounded miss.
