# geocoder-worker

Cloudflare Worker for Overture geocoding using R2-stored SQLite shards.

SQLite WASM queries use rusqlite 0.38+, which natively supports
`wasm32-unknown-unknown`. See the repository root `README.md` for the full
public API reference and `SPEC.md` for architecture details.

## Features

- Worker routing (`/search`, `/reverse`, `/id/:gers_id`, `/v2/forward`,
  `/v2/reverse`, `/v2/features/:gers_id`, `/health`, `/`), with HEAD request
  support on all endpoints
- STAC catalog loading from R2 with version fallback
- Forward shard selection: HEAD + location shards (coordinates or
  CF-IPCountry/CF-Region-Code headers)
- Reverse geocoding: coordinate-routed country shards (bbox containment)
  with IP-country and HEAD fallbacks
- GERS ID lookup via range reads against UUID-prefix-sharded parquet
- Edge caching (Cache API): catalog/collection JSON, SQLite shards,
  parquet footers and row groups (id-index TTL bounded for patch runs)
- In-isolate memo caches for opened shards and small JSON
- Rate limiting (60 req/min per IP) and privacy-safe request timing
  (`Server-Timing` header; logs carry endpoint class only)

## Development

```bash
# Check native compilation
cargo check -p geocoder-worker

# Build for WASM (requires wasm32 target)
rustup target add wasm32-unknown-unknown
cargo build -p geocoder-worker --target wasm32-unknown-unknown

# Run with wrangler (builds and serves locally)
cd crates/geocoder-worker
npx wrangler dev

# Deploy
npx wrangler deploy
```

## Prerequisites

1. R2 bucket named `geocoder-shards`
2. Shards built and uploaded by the `Rebuild R2 Shards` workflow
   (`scripts/build_shards.py` + `scripts/build_id_index.py`)
3. STAC catalog at `catalog.json` in bucket root; unified v2 additionally
   requires an atomic `v2/catalog.json` and its referenced immutable release

## API

See the root `README.md` for the authoritative endpoint reference.
Summary:

- `GET /search?q=<text>&limit=<n>&autocomplete=<bool>&format=json|geojson`
  — forward geocode; optional `lat`/`lon` override the IP-derived location
  bias
- `GET /reverse?lat=<f>&lon=<f>&format=json|geojson` — reverse geocode
  (bbox-based containment over countries, regions, counties, and populated
  localities)
- `GET /id/:gers_id` — resolve a GERS ID to its bounding box
- `GET /v2/forward` — unified division/POI text search or structured exact
  address lookup, with comma-separated `types`
- `GET /v2/reverse?lat=<f>&lon=<f>` — division reverse geocoding
- `GET /v2/features/:gers_id` — release-pinned GeoJSON feature lookup
- `GET /health` — verifies the catalog loads and a version exists

See `docs/api-v2.md` for the full staged v2 contract and current limitations.

## Architecture

The worker:
1. Fetches the STAC catalog from R2 to discover available versions/shards
2. Selects HEAD (global prominent places) + location shards
3. Fetches SQLite databases from R2 (edge- and isolate-cached)
4. Opens each database in-memory using rusqlite
5. Executes FTS5 queries against each shard
6. Merges, deduplicates, and ranks (match quality + static importance +
   BM25 tiebreak, with location bias)
7. Returns results as JSON or GeoJSON
