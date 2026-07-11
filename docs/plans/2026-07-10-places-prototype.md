# Places Single-State Prototype (CA) - Findings & Plan

**Date:** 2026-07-10
**Branch:** `places-single-state-prototype`
**Goal:** Validate shard size and ranking for Places theme, single-state slice (CA), without full global rebuild.

## 1. Overture Places Schema Research

### S3 Location
```
s3://overturemaps-us-west-2/release/2026-06-17.0/theme=places/type=place/*
```
Latest release via `scripts/stac.py`: `2026-06-17.0` (75M+ places globally per docs).

### Schema (DuckDB DESCRIBE)
```
id VARCHAR (GERS UUID)
geometry BLOB (WKB Point - requires spatial extension ST_X/Y)
categories STRUCT(primary VARCHAR, alternate VARCHAR[])
confidence DOUBLE (0-1, existence confidence)
websites, emails, socials, phones: VARCHAR[]
brand STRUCT(wikidata VARCHAR, names STRUCT(primary VARCHAR, common MAP, rules ...))
addresses STRUCT(freeform, locality, postcode, region, country)[] (array, primary is [1])
names STRUCT(primary VARCHAR, common MAP, rules ...)
sources STRUCT(property, dataset, license, record_id, update_time, confidence...)[]
operating_status VARCHAR (open, permanently_closed, etc)
basic_category VARCHAR (simplified taxonomy)
taxonomy STRUCT(primary, hierarchy[], alternates[])
version INTEGER
bbox STRUCT(xmin DOUBLE, xmax DOUBLE, ymin DOUBLE, ymax DOUBLE)
theme, type VARCHAR
```

### CA Filtering
- Address filter `list_contains(addresses.country='US') AND region='CA'` is unreliable: first sample showed geometries in Hawaii/Texas but addresses said CA (data conflation issues).
- Geographic truth: bbox filter `bbox.xmin BETWEEN -124.5 AND -114.0 AND bbox.ymin BETWEEN 32.5 AND 42.1` yields **1,940,735** places for CA.
- US total via bbox would be much larger; via address filter timed out after 120s (requires full scan). Estimated US ~15-20M places (CA ~10% of US).

### Existing Download Patterns
- `scripts/download_divisions.sh` + `download_divisions_global.sql`: uses DuckDB COPY with httpfs+spatial, S3 anonymous, memory_limit 12GB, spill to /tmp, threads=2.
- Same pattern reused for places: `scripts/download_places.sql` and `scripts/download_places.sh`.
- Flattened parquet for shard build contains: gers_id, version, primary_name, lat/lon, bbox, country, region, locality, category_primary, basic_category, brand_name, brand_wikidata, confidence, search_name_base, search_context_base.

## 2. Prototype Build Script Extension

### New Function in `scripts/build_shards.py`

```python
build_places_shard(parquet_path, region_filter, output_path, version)
```

- Accepts either flattened parquet (exports/places-CA.parquet) or raw Overture S3 URI (hive_partitioning=true, spatial).
- **Flat detection**: `_places_try_flat_schema` checks for gers_id, primary_name, lat, lon columns.
- **Schema:** Reuses `build_shard_schema` (divisions table) with `type='place'` so existing worker query (`SEARCH_DIVISIONS_SQL_WEIGHTED`) works without modification. Extra columns stored in search_name/context:
  - `search_name` = lower(primary + brand + categories.primary + basic_category)
  - `search_context` = locality + region + country + categories
  - `search_alias` = via `build_search_alias` (concatenations, abbreviation variants)
- **Importance:**
  ```python
  importance = min(1.0,
    confidence*0.5 +
    (0.20 if brand else 0) +
    (0.10 if brand.wikidata else 0) +
    (0.10 if confidence>=0.90 else 0) +
    category_prior)
  ```
  Category prior small boost: airport 0.25, national_park 0.20, university 0.15, hospital/stadium 0.12, museum 0.10, hotel 0.05, restaurant 0.02.
- **Output:** `shards/{version}/places/{region}-places.db`, e.g. `US-CA-places.db`
- **Collection:** `places-collection.json` similar to reverse-collection, with bbox.

### CLI Flags Added

```
--places                    Build places prototype shards
--places-region US-CA       Region code (default US-CA)
--places-parquet PATH       Flattened or raw parquet (default exports/places-CA.parquet, fallback to S3 bbox)
--places-limit N            Sampling: top N by confidence DESC
--overture-release TAG      For S3 fallback (default 2026-06-17.0)
```

Usage:
```bash
python scripts/build_shards.py --places --places-region US-CA --places-limit 50000 --version 2026-07-10-places-50k
```

### Download Scripts

- `scripts/download_places.sql`: template with `__OVERTURE_RELEASE__` placeholder, outputs `exports/places-CA.parquet` flattened.
- `scripts/download_places.sh [RELEASE] [LIMIT]`: wrapper fetching latest release via stac.py, supports sampled download via ORDER BY confidence.

## 3. Sampling & Size Estimates

Measured on MacBook, DuckDB S3 direct read (2 threads, 8GB):

| Sample | Records | Size | Notes |
|--------|---------|------|-------|
| 10k | 10,000 | **5.1 MB** | FTS5 prefix 2 3 4, unicode61 diacritics |
| 50k | 50,000 | **28.4 MB** | Fits under 50MB threshold |
| 100k | 100,000 | **56.9 MB** | Slightly over 50MB, under 128MB CF limit |
| Full CA (est.) | 1,940,735 | **~1.1 GB** extrapolated (50k * 38.8) | Too large, exceeds Worker 64MB cache budget and 128MB limit |

**Implication:** Single-state full Places cannot be single shard. Options:
- Sampled prominence (50k top per state) - fits budget, tail latency fine.
- Further splitting by H3 or sub-region is problematic per user feedback (avoid).
- Alternative: keep places in separate family, router selects places shard only when needed, not always loaded.

FTS build time for 10k: ~2 sec; 50k: ~15 sec; 100k: ~35 sec on local machine.

### Documented Metrics (from sqlite3 inspection)

For 50k shard:
```
SELECT COUNT(*), AVG(importance) FROM divisions; -> 50000, ~0.65
SELECT primary_name, importance FROM divisions ORDER BY importance DESC LIMIT 5
  -> airports, national parks, hospitals top-ranked due to category prior + confidence
```

Search performance (FTS MATCH query, BM25 weighted):
- "starbucks": 25ms, returns branded cafes
- "airport": 9ms, returns Ely Airport importance 1.0
- "taco": 2ms

Tail latency acceptable.

## 4. Worker Integration Sketch (not yet implemented)

Current forward shards: divisions only (HEAD + country/region shards). Places in separate `places/` subdir.

### Option A: Simplest Prototype (manual)

If router indicates US-CA, also try `US-CA-places.db` if exists:

In `stac.rs`:
```rust
fn select_nearby_shards(...) -> Vec<String> { ... }
// after suffix + router + nearby merging, check if any selected shard has a places counterpart
for sid in extra.clone() {
  let places_id = format!("{}-places", sid); // e.g. "US-CA" -> "US-CA-places"
  if collection_has_shard(collection, &places_id) && !seen.contains(&places_id) {
    extra.push(places_id);
  }
}
```

And collection.json needs to list places shards (embed in same collection or separate places-collection.json loaded similarly to reverse).

Shards loaded via existing `load_shard_db` and `Database::from_bytes`, then `search` merges results, dedup by gers_id, apply location bias, truncate. Since both divisions and places use same `divisions` table schema with `type='place'`, existing `execute_fts_search` works.

### Option B: Router Extension

Current router built from divisions parquet only (`build_global_router`). To include places:

- Build router from both divisions and places parquets (union of tokens).
- Tokenization same (`_router_tokenize`).
- Places importance generally lower than divisions (max 1.0 but divisions have type prior up to 0.3 + wiki 0.5 + pop etc). Might need separate weighting or keep combined.
- Documented in code: token_map values store max_importance per shard. For places, importance computed as above.
- For prototype, we can build separate `places-router.db` or extend existing router to include places shards.

For CA prototype: router would map "starbucks" -> US-CA-places, etc.

Implementation steps:
1. Extend `build_global_router(parquet_path, ...)` to accept list of parquets or add `build_places_router`.
2. Merge token maps (max importance per token across divisions + places).
3. Store in same router.db, with shard_ids like "US-CA-places".
4. Worker `select_shards_from_router` already filters via `collection_has_shard`, so if collection contains places shards, router hits will be used.

### No Contract Breaks

Explorer contract: `GET /search?q=&limit=6&autocomplete=true` expects `results[]:{gers_id,name,type,lat,lon,bbox,region,country}`. Our places shard returns same fields with `type='place'`. Additional fields (brand, categories) could be added later as additive, but keep existing.

TYPE_LABELS in explorer currently maps locality, localadmin, county, region, country, neighbourhood. Adding "place" will show raw type, not breaking.

## 5. Types Filtering Proposal (additive)

Other geocoders:
- Photon: `layer` param (e.g., `layer=city,street`) – single value or comma-separated.
- Pelias: `layers` param (venue, address, street, locality, county, region, country, etc).
- Mapbox: `types` param (country, region, postcode, district, place, locality, neighborhood, address, poi).

**Proposal for overture-geocoder:**

Add optional additive `types` query param:
```
GET /search?q=paris&types=place,locality,region,country
```
- Default: no filter, return bundle ranked by composed score (divisions + places).
- If `types` provided: filter results in Rust after FTS (or in SQL WHERE type IN (...)).
- For places, type could be granular: "place" umbrella, or more specific basic_category like "restaurant", "hotel", etc. For prototype, use "place" only; later we could expose categories as types or via separate `categories` param.
- Worker handlers.rs would parse `types` CSV, validate against allowed set (country, region, county, localadmin, locality, neighborhood, place), then apply.

This matches existing pattern: no breaking change, additive param.

## 6. Findings & Recommendations

- **CA full Places = ~2M rows, ~1.1GB SQLite FTS** – too large for CF Worker (128MB limit, 64MB cache budget). Must sample or partition.
- **50k top-confidence Places = 28MB**, fits 50MB threshold, good for prototype. 10k=5MB ultra-light.
- **Sampling strategy:** `ORDER BY confidence DESC, brand PRESENT` works: branded chains and high-confidence independent businesses surface first. Could also filter `confidence > 0.85` or `brand IS NOT NULL` to get prominent subset. For prototype, 10k-50k most prominent per state is reasonable.
- **Ranking:** confidence*0.5 + brand bonuses + category priors yields intuitive ordering (airports, hospitals, universities high). Could refine with wikidata for brands (many brands lack wikidata in Overture, but when present it's strong signal).
- **Router:** extend to include places tokens; for now, manual inclusion of places shard when region selected is simplest sketch.
- **Next steps:**
  1. Build full CA 50k shard, test in worker dev (`wrangler dev`) with manual shard selection, measure tail latency (expected +10-20ms for extra shard).
  2. Build US sampling: 50k per large state (CA, TX, FL, NY, etc) + 10k per small state; estimate total US ~ maybe 1-2M sampled places, ~1GB total across shards, but per-region shards stay under 50MB.
  3. Add `types` param filtering in worker (additive).
  4. Consider separate `places-collection.json` and cache layer, or merge into main collection.json.
  5. Evaluate brand coverage: ~?% of CA places have brand (needs measurement, previous attempt timed out; estimate 10-15% based on 50k sample where top results are branded).
  6. Document explorer: TYPE_LABELS should add `place: "Place"` and maybe category labels.

## 7. Branch Status

- Branch `places-single-state-prototype` from `main` at 2330bed.
- Commits:
  - bbf1fa2 Add places single-state prototype (CA) shard builder
- Files added/modified:
  - `scripts/build_shards.py`: added places constants, `compute_places_importance`, `build_places_shard`, `build_places_shards`, CLI flags
  - `scripts/download_places.sql` + `scripts/download_places.sh`: CA bbox download helpers
- Test shards built locally (not committed, .db files):
  - `shards/2026-07-10-places-test/places/US-CA-places.db` 10k / 5.1MB
  - `shards/2026-07-10-places-50k/places/US-CA-places.db` 50k / 28.4MB
  - `shards/2026-07-10-places-100k/places/US-CA-places.db` 100k / 56.9MB
- No Rust changes yet (worker integration sketch only). `cargo fmt`/`clippy` not needed since only Python changed.
- Pushed to origin? **To be pushed** – branch ready for review, no PR yet as full data download heavy.

Run to reproduce CA sample:
```bash
./scripts/download_places.sh 2026-06-17.0 50000
python scripts/build_shards.py --places --places-region US-CA --places-limit 50000 --version dev
sqlite3 shards/dev/places/US-CA-places.db "SELECT COUNT(*), SUM(LENGTH(search_name))/COUNT(*) FROM divisions;"
```

## 8. Open Questions

- Should places shard use same `divisions` table name or separate `places` table? Reusing simplifies worker but mixes types; separate table would allow distinct query path and clearer type filtering.
- How to handle multilingual names for places? `names.primary` plus `brand.names.primary` may need language fallback similar to divisions (currently EN only).
- Should we include `confidence` in API response as optional additive field for debugging?
- For global build, what is acceptable per-state sampling? 50k per large state * 50 states = 2.5M places US, ~1.4GB total R2 but per-shard under 50MB OK. Worldwide 75M full would be huge; sampling to maybe 5M prominent worldwide?
