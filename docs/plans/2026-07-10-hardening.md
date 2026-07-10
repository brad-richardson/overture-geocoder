# Hardening — build-meta reproducibility, FTS guardrails, data_version

Date: 2026-07-10
Branch: `hardening-buildmeta-guardrails`

## Goal
Small hardening improvements per roadmap:
- F17 reproducibility: version = date, not Overture release; emit build-meta artifact
- F15 FTS guardrails: 1-char autocomplete prefix + 200B tiny-token DoS
- F8 additive data_version to responses (no breaking change)
- F11 stage telemetry (optional, deferred)

## Changes

### 1. Build-meta reproducibility [S]

**File:** `scripts/build_shards.py`

- New arg `--overture-release` (e.g. `2026-05-21.0`) for reproducibility.
- New helpers:
  - `get_git_sha()` — `git rev-parse HEAD`
  - `get_wiki_importance_sha()` — SHA256 of local Nominatim file or None
  - `get_version_from_catalog(catalog_path)` — reads latest version from `shards/catalog.json` (mirrors `stac.rs` `get_ordered_versions` / `version_sort_key`), future use for CI / release discovery
  - `write_build_meta()` — writes `shards/{version}/build-meta.json`
- `build-meta.json` contents:
  - `version` (date-based), `build_timestamp` UTC, `overture_release`, `division_s3_paths` derived from release tag (division + division_area)
  - `wikimedia_importance`: URL, local path, SHA256
  - `git_sha`, `python_version`, `platform`, `duckdb_version`
  - `thresholds`: head_threshold, head_wiki_importance_threshold (0.65), wiki_locality_keep_threshold (0.5), shard_size_threshold_bytes (50MiB), region_split_record_threshold (400k), fts5_prefix_lengths, tokenizer, router_token_min_len (3), router_max_shards_per_token (3), dedup_radius_km, reverse_locality_population_threshold
  - `constants`: TYPE_PRIOR, LOCALITY_CLASS_PRIOR, WIKI_IMPORTANCE_WEIGHT, etc.
  - `input`: parquet path + size
  - `record_counts`: total_records, total_size_bytes, shard_count, per-shard counts
  - `args` snapshot
- Written after every build (forward/reverse) to `version_dir/build-meta.json`; best-effort with warning on failure.
- R2 publish: workflow can upload `build-meta.json` alongside collection; it lives under versioned prefix (immutable) and is safe to cache long.

**Rationale:** Previous versions were date-based but had no artifact tying them to upstream Overture release, wiki importance file, or git SHA. Now reproducibility is auditable. `get_version_from_catalog()` unifies Python-side version discovery with Rust `get_ordered_versions`.

### 2. FTS guardrails [XS/S]

**File:** `crates/geocoder-worker/src/handlers.rs`
- Constants: `MIN_AUTOCOMPLETE_QUERY_CHARS=2`, `MAX_TOKEN_COUNT=10`
- Before shard load:
  - `q.split_whitespace().count() > 10` → 400 `"Too many tokens: max 10"` (DoS bound)
  - `autocomplete && trimmed.chars().count() < 2` → 200 empty `{"results":[]}` (or empty FeatureCollection for geojson) without touching R2/DB — avoids 1-char prefix scan (`"a"*`) that with `prefix='2 3 4'` falls back to full FTS scan.
- Cost: one extra string split + char count; negligible.

**File:** `crates/geocoder-core/src/query/fts.rs`
- Defense-in-depth inside core (Worker can be bypassed via CLI/library):
  - `MIN_FTS_TOKEN_CHARS=2`: drop tokens <2 chars during normalization
  - `MAX_FTS_TOKENS=10`: truncate to 10 after filtering
  - Autocomplete last-token <2 → pop (so `"new y"` → `"new"*` instead of `"new" "y"*`)
  - Empty after filtering → empty FTS query → `Database::search` early-returns `[]` (existing behavior)
- Before: `"a a a ..."` 100 tokens (200B) produced `"a" "a" ... "a"*`, each term scanning entire FTS index. After: filtered to empty → no DB work.
- Before: `"a"` autocomplete → `'"a"*'` broad prefix. After: `""` → empty.
- Tests: existing 40 core tests + 13 integration still pass; new behavior covered by handler early-return and FTS filtering.

### 3. data_version additive [S]

**Files:** `crates/geocoder-worker/src/stac.rs`, `handlers.rs`

**Problem:** `/search`, `/reverse`, `/id` returned results without indicating which `{version}` shard set served them, hindering debuggability and client cache reasoning.

**Solution (non-breaking, additive):**
- `SearchResult` now always carries `version: String` (populated from `try_search` param), not only when `debug=true`.
- New wrappers:
  - `ReverseSearchResult { result: Option<ReverseResult>, version }`
  - `IdSearchResult { result: Option<IdLookupResult>, version }`
- `with_version_fallback!` macro unchanged; inner `try_*` now returns wrapped type with version, preserving fallback semantics for missing shards.
- Handlers:
  - `/search` JSON: `{"results": [...], "data_version": "<ver>", "debug": {...}}`; header `X-Data-Version: <ver>`; GeoJSON: FeatureCollection gains `"data_version"` property + same header.
  - `/reverse` JSON: original `ReverseResult` fields + `"data_version"` merged via `serde_json::Value` + header `X-Data-Version`; GeoJSON similar.
  - `/id` JSON: `IdLookupResult` + `"data_version"` + header + `Cache-Control` preserved.
  - `/health` already returns version; unchanged.
- Explorer (`explorer/` static) does `data.results || []` and ignores extra fields/headers → safe.

**Alternative considered:** Only header `X-Data-Version`. Added body field as well because CLI clients and PWA offline debugging often lack header inspection; additive field is cheap.

### 4. F11 stage telemetry (optional) — deferred
Left for follow-up: structured `stage` logs already emitted via `console_log!`; proper OTel would need Worker env var wiring.

## Testing
- `cargo check --workspace` → pass
- `cargo test --workspace` → 40 core lib + 13 integration + 32 worker + 1 doctest = 86 pass
- `pytest tests/test_build_shards.py` → 64 pass
- Manual `write_build_meta` creation → JSON validated
- Verified `prepare_fts_query` filtering: `"a"` → `""`, `"new y"` → `'"new"*'`, 11-token query truncated.

## Branch status
- Branch `hardening-buildmeta-guardrails` created from `main` (commit `2330bed651...`)
- Modified files:
  - `scripts/build_shards.py`
  - `crates/geocoder-worker/src/handlers.rs`
  - `crates/geocoder-worker/src/stac.rs`
  - `crates/geocoder-core/src/query/fts.rs`
- New file: `docs/plans/2026-07-10-hardening.md`
- Pushed? No yet per task (push branch, do not create PR yet if you want user review) — will push now.

## Follow-ups
- Wire `build-meta.json` upload in `.github/workflows/rebuild-r2-shards.yml` (upload artifact + R2 `wrangler r2 object put`).
- Optional: include `build-meta.json` href in `collection.json` (`collection["build_meta"] = {"href": "./build-meta.json"}`) for discoverability.
- Consider adding `data_version` to JS/Python client return types (minor bump, additive).
- F11 telemetry: emit `data_version`, shard selection reasons (suffix/router/nearby), token count as structured logs for Cloudflare Logpush.

## Return
- Branch ready for review: `hardening-buildmeta-guardrails`
- No breaking contract changes; explorer safe.
- Reproducibility now auditable via `build-meta.json`.
- FTS DoS vectors mitigated at edge (handler) and core (fts.rs).
- `data_version` surfaced via header + JSON body.
