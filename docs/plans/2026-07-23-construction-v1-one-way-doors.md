# Construction v1: one-way-door decisions before public shards

Date: 2026-07-23. Status: DRAFT for owner review. These are the decisions that
become expensive or impossible to reverse once v2 shards are served publicly.
Everything else in the pipeline is disposable by design (fresh namespaces,
non-promoting slices); these are not.

## 1. Public API surface (`/v2/forward`, `/v2/reverse`, `/v2/ids/`)

Once external users hit these routes, request/response field names, error
shapes, and pagination semantics are frozen in practice. Decisions to solidify:

- Final route set: is `/v2/features/:gers_id` really dropped in favor of
  `/v2/ids/`? Batch endpoint permanently out?
- Response envelope: the two data-version fields (Overture release + geocoder
  build) — exact field names and format become a compatibility contract.
- Error contract: status codes and body shape for miss vs invalid vs
  rate-limited.
- Do we version by URL only (`/v2/`) or also accept a header? URL-only is
  simpler and already the plan; confirm.

## 2. Serving artifact format identifiers

`.av1`, `PLRV0002`/`PLHD0002` (`.pcsh`, `.phrp`, `.adat`, `.aidx`) magic bytes
and layouts. The Worker decoder must read every format version ever published
for as long as a catalog release referencing it is retained (Worker accepts up
to 64 releases). Bumping a format is cheap *before* first publication and a
permanent decoder-matrix obligation after. Confirm the v1/0002 layouts are the
ones we want to carry (e.g., E7 ties-to-even coordinate encoding, 16-bit
routing bucket ceiling, fixed-row-count packs).

## 3. Catalog and release identity

- `v2/catalog.json` + `v2/releases/{build}/release.json` dual-catalog contract
  with If-Match CAS on the mutable root: once public, clients and the Worker
  cache these paths and semantics. Path layout, release naming
  (`YYYY-MM-DD.N`), and the monotonic-sequence rule (no `.0` → sequence-0
  ambiguity — this already caused a planet failure) should be pinned in one
  doc.
- Retention: Worker caps at 64 releases; candidate construction has no cap.
  Decide the retention/deletion policy BEFORE the first publish, because
  version-prefix deletion must stay disabled until retention logic walks both
  catalog roots. An accidental deletion of a referenced release is the
  clearest irreversible failure mode in the system.
- Rollback identity: the v2 catalog has independent rollback identity from
  legacy — confirm the legacy catalog is never mutated by v2 publication.

## 4. Lineage genesis namespace

`global-v2-construction-v1` is declared "lineage genesis." The first ACCEPTED
run's request SHA becomes the ancestor every future incremental/monthly build
chains from. Accepting a run whose semantics we later regret (e.g., the
rejection-precedence order, duplicate policy, or ASCII fold version) means
either living with it or another genesis reset — cheap today, disruptive after
public serving. The acceptance gates in the scope doc are the backstop; do not
waive any of them for schedule pressure on the 25th.

## 5. Places partition contract

- Level-12 quadkey ownership with split-never-merge sticky splits: "never
  merge" is by definition a one-way accumulation. Fine — but the frozen
  `maximum_level = 12` equality check plus the 1.5M-row hard abort means a
  future dense cell (Tokyo-class) can wedge planning with no escape hatch.
  Before first publish, either (a) census-verify the densest global cells
  against the cap on the target release, or (b) adopt the adaptive
  subdivision (checkpoint-5) as the contract. Changing the partition scheme
  after publication invalidates all Places serving partitions at once.
- Address: country + FNV-1a high-bit, 1M-row sticky splits — same logic;
  cheaper to widen the hash-bit ceiling now than after genesis.

## 6. Semantic-digest algorithm

The dual-SHA-256 additive lanes are a versioned contract; every future
producer AND independent verifier must reproduce them bit-exactly to validate
old packs. Pin the exact canonical digest-input serialization in a doc + test
fixtures (partially done via hand-authored fixtures). If we ever want a
cheaper lane (e.g., xxh3) the time is before genesis, not after.

## 7. Ranking / admission semantics visible to users

Places head admission (famous-unique policy), tokenization, and the versioned
ASCII fold determine which queries return which results. Not technically
irreversible, but result-set changes after public launch are user-visible
regressions. The intentional-semantic-changes list (scope doc, gate 1) should
be reviewed once and signed off as "the launch semantics."

## 8. Licensing / attribution

Serving Overture-derived data publicly requires ODbL/CDLA-Permissive-2.0
attribution depending on theme (addresses vs places sources differ). Decide
attribution text and where it lives (API response? docs page?) before the
endpoint is public. Retroactive attribution is fixable but public
non-attribution is a bad look even for a hobby project.

## Explicitly NOT one-way doors (safe to iterate after launch)

- Runner topology, concurrency, pack row/byte targets, DuckDB thread/memory
  settings (per-run measured knobs).
- Reducer count / job matrix composition.
- Edge-cache TTLs.
- The Python control-plane code itself (bytes served are what's frozen).
- Anything in a non-promoted slice — by design disposable.
