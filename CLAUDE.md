# Overture Geocoder - Project Guide

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
- **ID lookup**: Sorted parquet shards (UUID + FLOAT bbox), snappy compressed, 4096 shards (3 hex prefix)
- **Worker runtime**: wasm32, Cloudflare Workers. Dependencies must compile to wasm32-unknown-unknown
  - `parquet` crate: only `snap` feature (zstd does NOT compile to wasm32)
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
