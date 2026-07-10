# Build Upload Retry & Parquet Footer Caching

**Date:** 2026-03-28
**Status:** Approved

## Problem

1. The ID index build phase uses DuckDB `COPY TO s3://` with no retry. A transient R2 502 on a single shard fails the entire build job (as happened today with shard `8d4`). The staging phases already use `_retry_transient` for this — the build phase doesn't.

2. Every ID lookup does 2 R2 range reads: a 32KB suffix read (parquet footer) and a row group read. The footer is immutable for the lifetime of a shard version but is never cached. Caching it at the edge would cut cold-but-same-prefix lookups from 2 R2 reads to 1.

## Changes

### A. Wrap build COPY TO with `_retry_transient`

**File:** `scripts/build_id_index.py`

In `_worker_build_r2_batch`, wrap the `con.execute(COPY TO s3://...)` call with `_retry_transient`, matching the pattern used by `_partition_release_type` and the registry staging workers. This retries on DuckDB HTTP 502/503 with exponential backoff (30s, 60s, 120s).

Also fix `_upload_to_r2` (used for metadata JSON uploads via wrangler) — its retry loop has no sleep between attempts. Add a short backoff (5s, 10s, 20s).

### B. Cache parquet footer suffix at the edge

**File:** `crates/geocoder-worker/src/stac.rs`

In `try_lookup_id`, cache the 32KB suffix read result using the existing Cache API pattern (`SHARD_CACHE_TTL` = 1 hour).

**Cache key:** `{CACHE_PREFIX}{version}/id-index/{prefix}.parquet__suffix`

**Cached value:** The raw suffix bytes (32KB). The file size is derived from the `Content-Range` response header on the original R2 request and must also be stored. Encode as: 8 bytes (file_size as u64 little-endian) + suffix bytes.

**Lookup flow (after change):**
1. Check Cache API for footer — on hit, parse footer + file size from cached bytes, skip to step 3
2. On miss: suffix read from R2 (existing code), store result in Cache API
3. Parse parquet metadata from footer, find matching row group
4. Range read just that row group (unchanged)

**No changes to the `cached_get` helper** — the suffix read has different semantics (it returns bytes + needs file size metadata). A small inline cache check/store in `try_lookup_id` is simpler than generalizing `cached_get`.

## Non-goals

- Caching the row group data (it's ID-specific, low hit rate)
- Changing the parquet format or shard size
- Adding retry to the staging phases (they already have it)

## Testing

- `cargo test` — verify no regressions
- `cargo clippy --all-targets` — lint
- Manual benchmark: cold/warm/cached-footer latency comparison after deploy
