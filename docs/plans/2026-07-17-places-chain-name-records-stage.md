# chain_name records-stage cold gate

Date: 2026-07-17
Status: scoped design + implementation spec. Clears (or measurably fails) the
last failing Places cold technical gate after #98. Implementation is chained
behind the famous-unique head-admission PR
(`docs/plans/2026-07-17-famous-unique-head-admission.md`) because both touch
`places_pages.rs` and the smoke prep.

## Problem

`relevance_chain_name` (7-Eleven, Tokyo context) is the only case still
failing the cold located gate (≤ 8 physical reads, ≤ 512 KiB, ≤ 1.0 s): it
measured 15 reads / 1.10 MB, with the records stage alone at 829 KB even
after #98 reduced the fetched window to the served top-10. Two structural
causes, verified in the code:

1. **Records are laid out in doc order and coalesced with a 256 KiB gap.**
   The records blob follows serving-doc order — `(spatial_cell, -qconf, id)`
   — so the branches of a chain scatter across spatial cells and the top-10
   record extents span most of the records component. The records coalescer
   (`places_pages.rs:999`, mirrored by the Python oracle default
   `record_gap=256*1024` in `experiment_places_compact_shard.py:517`) merges
   anything within 256 KiB, so ten ~100-byte records drag in hundreds of KB
   of dead gap bytes.
2. **Physical reads are awaited sequentially** in `RangeReader::coalesced`,
   measured at ~60–130 ms each. The ≤ 8-read gate is effectively the latency
   budget; read count and wall time are currently the same axis.

## Design

Three coordinated changes; the first is the primary fix, the other two are a
measured tuning knob and a latency decoupling.

### 1. Records layout by rank (primary)

The records component's internal order is free: `record_index` (8 B/doc
offset+length) is the only coupling, and every reader resolves record extents
through it. Doc IDs, postings, lexicon, and result semantics are untouched.
Change the producer to lay out records in global serving-rank order
`(-qconf, doc)` instead of doc order.

Why this targets the failure: the served window is selected by exactly
`(-qconf, doc)` (`places_pages.rs:955`), so any query's top-10 extents become
monotone positions in rank space, concentrated toward the front for
high-confidence results — replacing spatial scatter (span ≈ the whole
component) with rank-local clustering. The chain case's ten same-brand,
similar-confidence branches land near one another almost by construction.

Producer change in the compact-shard builder; Python and Rust readers need no
logic change. Artifact bytes change, so checked-in fixtures regenerate and
byte-equality oracles re-pin. Two consecutive builds must stay SHA-identical.

### 2. Records/index coalesce-gap sweep (measured, offline)

With rank layout in place, sweep the records and record_index gap thresholds
(e.g. 256 Ki / 64 Ki / 16 Ki / 4 Ki / 0) in the offline reader model over the
smoke fixture's full case set, and pick the smallest-bytes setting that keeps
every case within 8 physical reads. Record the sweep table in the PR. The
chosen constants must change in lockstep in both readers
(`places_pages.rs:976,999` and the `CompactShard.query` defaults) — add an
equivalence assertion so they cannot drift.

### 3. Concurrent physical reads in the shared range reader

Issue the planned ranges of one `coalesced()` call concurrently (bounded
in-flight, e.g. 4) instead of sequentially awaiting each. This is the shared
reader core, so it must be implemented once and benefit Places, head, and
address paths alike (decision #2: one range-reader core, no forks). Metrics
must still report the same logical/physical read and byte counts; only wall
time changes. Keep the plan deterministic; only the await order changes.

## Acceptance gates

1. `relevance_chain_name` independently cold: ≤ 8 physical reads, ≤ 512 KiB,
   ≤ 1.0 s client time, through the real R2 smoke (post-merge dispatch).
2. Every other technical case and relevance seed keeps passing; oracle
   equality (result IDs, order, projections) holds Python↔Rust on the
   regenerated fixtures.
3. No gate is relaxed. If the sweep cannot find a setting inside all three
   budgets, report the measured frontier and stop; do not tune the contract.
4. Unit coverage: rank-layout determinism, record_index/records consistency,
   coalesce plans at the chosen gaps, concurrent-read metrics equivalence
   (same counts as sequential), and a scattered-extents regression case
   shaped like chain_name.

## Non-goals

Complete bounded near-me ranking (separate design; distance remains
diagnostic), head-format changes (in the head-admission PR), posting or
lexicon layout changes, and any relaxation of the routed gates.
