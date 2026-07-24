# Places construction-v1 checkpoint 4

Status: complete local vertical slice; not planet-ready.

This checkpoint intentionally discards predecessor Places formats. Nothing in
the new map, reduce, or serving namespaces accepts old bytes, and no Worker
route is active.

## Result

The provisional contract in
`2026-07-22-places-construction-v1-contract.md` is executable end to end:

1. A bounded Arrow hydration streams the flattened Places projection without
   Python feature dictionaries or `read_all`.
2. Rust owns admission, tokenization, rank, spatial routing, total order fields,
   and the two semantic digest lanes.
3. DuckDB performs the local external order and deterministic pack assignment.
4. Rust proof directories bind each immutable pack, Parquet row group, and
   route summary before a marker-last task completion is published.
5. Genesis starts with no predecessor and emits one assignment per provisional
   spatial cell under a hard row cap.
6. Selective reducers reconcile selected plus discarded bindings for every
   overlapping row group and publish new leaf/routed objects.
7. The global head is derived from the same leaves with a deterministic
   per-token top-ten cap.
8. A separate Rust verifier rejects malformed, misordered, or miscounted
   serving bytes. A dormant Worker module parses and queries both formats under
   byte, record, entry, candidate, and result caps.

All objects use fresh `map/places-v1`, `reduce/places-v1`, and
`serve/places-v1` namespaces. Completion markers are the only mutable-looking
coordination boundary and are written last with create-if-absent semantics.

## Local proof fixture

The full-slice test constructs 450 features and 2,700 term rows across two
spatial cells. The order intentionally makes one pack contain rows from both
cells, forcing a reducer to prove selected plus discarded rows rather than
testing only cell-pure row groups.

The fixture proves:

- an interruption after immutable object publication leaves no completion
  marker;
- retry produces two deterministic content-addressed packs and a valid marker;
- a subsequent retry admits the existing marker only after revalidating every
  referenced object;
- pack, plan, partition, and final leaf bindings reconcile exactly, including
  duplicate multiplicity;
- missing or extra reducer markers are rejected;
- both routed partitions and the capped global head pass the independent byte
  verifier and a test decoder; the same layouts and query caps are covered by
  dormant Worker unit fixtures;
- a truncated serving artifact is rejected.

The hand-authored nine-row contract fixture separately covers multilingual and
CJK tokenization, combined field masks, duplicate UUID provenance, rejection
precedence, strict 2D Point WKB, locator conversion/bounds, route computation,
and typed physical-boundary corruption.

## Validation

- `pytest -q tests/test_places_construction_v1.py`: 3 passed.
- `cargo test -p geocoder-construction`: 5 passed across the Address and Places
  construction binaries.
- `cargo test -p geocoder-worker places_construction_v1`: 2 passed.
- `cargo clippy -p geocoder-construction -p geocoder-worker --all-targets -- -D warnings`:
  passed.
- `ruff check scripts/places_construction_v1.py tests/test_places_construction_v1.py`:
  passed.
- `git diff --check`: passed.

No retained Address corpus was read or regenerated.

## Still provisional / planet gates

The following are deliberately not claimed by this checkpoint and remain the
next evidence gates:

- replace or validate the 256-by-256 grid against planet skew; oversized cells
  currently fail instead of subdividing;
- run exact source inventory and bounded real-source projection for Places;
- remove local reducer concatenation in favor of bounded external ingestion for
  planet-sized assignments;
- define assignment batching, retries, and deterministic fan-in for many map
  tasks rather than the single local task;
- demonstrate hard RSS, scratch, wall-time, object-size, row-group, and
  amplification limits on representative and then full-planet input;
- prove global-head construction can merge many leaves without one local
  DuckDB relation becoming the bottleneck;
- settle serving lookup indexing/range layout and latency before activating a
  Worker route;
- add hosted object-store conditional publication and failure recovery only
  after the local proof remains stable.

Those gates may change the spatial assignment and serving container. They do
not need to preserve this checkpoint's unshipped bytes.
