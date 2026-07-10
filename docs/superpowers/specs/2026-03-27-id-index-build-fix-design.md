# Fix ID Index Build Failure & Enable Version Fallback

**Date:** 2026-03-27
**Status:** Approved

## Problem

The March 25 scheduled "Rebuild R2 Shards" workflow failed at the `id-stage (base, stage-base)` job. DuckDB 1.5.1 (installed via unpinned `pip install duckdb`) requires an explicit `INSTALL httpfs` before `LOAD httpfs`. The `phase_partition_release_r2()` function is the only phase that doesn't call `_ensure_httpfs_installed()` before using httpfs.

Because `id-stage (base)` failed, the downstream `id-build` and `id-post` jobs were skipped. The new version `2026-03-25.0` has no `id-collection.json`, so ID lookups return 503.

The worker has a `with_version_fallback!` macro that tries older versions on "not found" errors, but it can't help here because `catalog.json` only ever contains one version — the CI runner builds it fresh each time without preserving previous version links.

## Changes

### A. Fix httpfs in `phase_partition_release_r2()`

**File:** `scripts/build_id_index.py`

Add `_ensure_httpfs_installed()` at the start of `phase_partition_release_r2()`, matching the pattern used by every other phase (`phase_stage_r2`, `phase_build_r2`, `_run_pool`).

Also pin the DuckDB version in the workflow to avoid future surprise upgrades:
- `pip install duckdb` -> `pip install duckdb==1.5.1` in all `id-stage`, `id-build`, and `id-post` job steps.

### B. Preserve previous versions in `catalog.json`

**File:** `scripts/build_shards.py`

Before generating catalog.json, fetch the existing catalog from R2 (via wrangler) and merge its version links. This way when the new catalog is uploaded, it still references the previous version as a fallback.

**Approach:** In the catalog generation section (~line 1158), before writing catalog.json:
1. Run `wrangler r2 object get geocoder-shards/catalog.json --remote --pipe` to fetch the current R2 catalog
2. Parse it and extract existing child version links
3. Merge them into `existing_versions` (deduplicating)
4. If wrangler fails (first deploy, no catalog yet), proceed with just the new version

This keeps the logic in `build_shards.py` itself rather than adding shell plumbing to the workflow. The script already has merge logic for local catalogs — we extend it to also check R2.

### C. Fix old version cleanup

**File:** `.github/workflows/rebuild-r2-shards.yml`

The cleanup step already parses catalog.json for versions older than 90 days. Once catalog.json has multiple versions (from change B), this will start working. One addition needed: after deleting old version data from R2, re-upload catalog.json with the old version links removed so the worker doesn't try to fall back to deleted data.

**Approach:** After the delete loop, rebuild catalog.json from the surviving versions and upload it. The simplest way: use a small inline Python snippet that filters the version list and writes a new catalog, then upload with wrangler.

## Non-goals

- Changing the `with_version_fallback!` macro or worker Rust code (it already works correctly once catalog.json has multiple versions)
- Adding retry logic to the workflow for failed stages (the httpfs fix prevents the root cause)
- Changing the ID index build architecture

## Testing

- Run `cargo test` (no Rust changes, but verify nothing regressed)
- Manually verify `build_shards.py` catalog merge logic by running with `--countries US` locally with a fake R2 catalog
- After deploying, trigger the workflow manually and verify catalog.json in R2 contains both the new and previous version
