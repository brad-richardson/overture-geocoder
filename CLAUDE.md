# Overture Geocoder - Project Guide

## Operating Policy

For construction-v1, the objective is the shortest safe path to the next
non-promoting planet-scale execution milestone for both Places and Addresses.
Optimize for measured progress toward that milestone, not for closing every
possible follow-up.

### Priority

Work in this order:

1. A measured blocker that prevents the next execution rung.
2. A defect that would corrupt output, make resume unsafe, invalidate the next
   measurement, or waste a planet run.
3. Everything else is recorded and deferred.

Do not turn hygiene, speculative hardening, design completeness, or last-mile
optimization into active work while a measured planet blocker is open. Labels
such as P1 are milestone-relative: a finding blocks only when it prevents or
invalidates the next agreed execution milestone.

### Verification ladder

Use the cheapest rung that can falsify the current hypothesis, then advance:

1. Focused unit and contract tests.
2. Monaco Places / Seattle Addresses real-data slice for end-to-end correctness.
3. A representative scale probe for the dimension being changed, such as the
   preserved Europe runs.
4. A non-promoting planet run, only with operator authorization.

The small slice is a correctness gate, not planet-scale evidence. Once it passes,
do not keep expanding tests at that rung when the open risk is RAM, disk, shard
fan-out, object count, remote semantics, or wall time.

### Change and review budget

- Keep one primary concern per pull request.
- Use one independent adversarial review pass.
- Allow at most one scoped fix/reverify pass. Reverify only the named blocker
  claims changed by the fix; do not re-audit the subsystem.
- P0 findings always block. P1 findings block only when they prevent or
  invalidate the next execution milestone. Record unrelated or non-blocking
  findings for later instead of expanding the PR.
- Mutation-test only the few load-bearing production call sites for the stated
  blocker. Do not mutation-test every new guard or turn each claim into a new
  framework.
- Focused validation plus green required CI and closure of the original blocker
  ends the PR. A new finding that materially expands scope becomes separate work.

### Work in progress and stopping

- Keep at most two active code PRs. Do not run parallel hygiene or design
  workstreams while the measured long pole is open.
- Keep the active task list to the current blocker, its next probe, and at most
  one blocked downstream item. Deferred work is documentation, not an active
  queue.
- "Pause", "stop", or "wind down" means stop launching work immediately,
  cancel reviewers and waiters, persist the checkpoint, and report. Do not
  finish another review cycle unless explicitly requested.
- Never dispatch a planet build without the operator supplying the workflow's
  cost/confirmation authorization.

## Project Structure

```
crates/
  geocoder-core/     Platform-agnostic geocoding engine (SQLite FTS5, shared types)
  geocoder-worker/   Cloudflare Worker (R2 shard loading, edge caching, parquet ID lookup)
  geocoder-cli/      CLI tool for local testing
  Cargo.toml         Workspace root
clients/
  js/                TypeScript client library
  python/            Python client library (>=3.10)
scripts/
  build_shards.py    Build forward/reverse SQLite shards from Overture divisions
  build_id_index.py  Build UUID-prefix-sharded parquet ID index (R2 pipeline)
  stac.py            STAC catalog utilities (release discovery, S3 paths)
  download_divisions.sh  Download division data via DuckDB
```

## Key Architecture

- **Forward/Reverse geocoding**: SQLite shards per country/region, FTS5 search, stored in R2
- **ID lookup**: Sorted parquet shards (UUID + FLOAT bbox), uncompressed, 4096 shards (3 hex prefix), range-read via R2
- **Worker runtime**: wasm32, Cloudflare Workers. Dependencies must compile to wasm32-unknown-unknown
  - `parquet` crate: only `snap` feature for backward compat with older Snappy shards (zstd does NOT compile to wasm32)
  - `rusqlite`: uses Workers' built-in SQLite via `from_bytes`/`deserialize`
- **Edge caching**: Cache API with TTLs (5min catalog/collection, 1hr shards)
- **STAC metadata**: catalog.json -> {version}/collection.json, reverse-collection.json, id-collection.json

## Build & Test

```bash
# Rust workspace (from crates/)
cargo check                    # Type check all crates
cargo test                     # Run all tests (integration tests need local shards)
cargo fmt --all                # Format
cargo clippy --all-targets     # Lint

# Worker dev server
cd crates/geocoder-worker && wrangler dev

# Generate test data
./scripts/download_divisions.sh
python scripts/build_shards.py --countries US
```

## construction-v1 (planet Places/Addresses build)

**Read `docs/plans/construction-v1-state.md` before touching this.** It is the
living state document; the dated docs in `docs/plans/` are point-in-time
analyses and it supersedes them where they disagree.

Two things that repeatedly get lost:

- Use the fast loop. `scripts/build_slice_inventory_v1.py` +
  `scripts/run_slice_construction_v1.py` run all five phases on real Overture
  data in ~13 seconds with no credentials. Use it to establish correctness, then
  advance to the representative scale rung when the risk is scale-dependent.
- The address map shuffle will NOT be ported, decided 2026-07-25. Address map
  output is already hash-clustered (`pack_id` is a row counter over
  `TOTAL_ORDER`, which begins `country, maximum_bucket` = the top 16 bits of
  `route_hash`), so the shuffle buys little; the transport cost is whole-object
  hydration at reduce, not layout. The address partition key
  (`address_key_hash` / `route_hash` / `hash_bucket`) and `MAXIMUM_HASH_BITS`
  remain FROZEN. See the "DEFERRED, do not lose" section of
  `docs/plans/2026-07-24-construction-v1-follow-ups.md`.

## Data Pipeline

The `Rebuild R2 Shards` workflow runs monthly (25th) with two parallel jobs:

1. **Forward + Reverse**: download_divisions.sh -> build_shards.py -> wrangler upload to R2
2. **ID Index**: build_id_index.py streams Overture S3 -> R2 staging -> sorted parquet shards

Both jobs produce date-versioned data (e.g., `2026-02-25.0/`). The worker discovers the latest version via catalog.json.

## Conventions

- Commit messages: imperative tense, concise
- Never force push unless rebasing an entire branch
- Never push directly to main without a feature branch (unless explicitly told to)
- Python scripts target 3.10+
- Rust: stable toolchain, `cargo fmt` + `cargo clippy -D warnings` enforced in CI
