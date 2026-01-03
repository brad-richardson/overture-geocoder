# geocoder-worker

Cloudflare Worker for Overture geocoding using R2-stored SQLite shards.

## Status

SQLite WASM queries are implemented using rusqlite 0.38+ which natively supports `wasm32-unknown-unknown`.

### Completed
- Worker routing (`/search`, `/reverse`, `/health`, `/`)
- STAC catalog loading from R2
- Shard selection (HEAD + country based on CF-IPCountry)
- R2 bucket integration
- SQLite query execution via rusqlite WASM
- Location bias (country-based from CF-IPCountry header)

### TODO
- [ ] Edge caching for catalog and shards
- [ ] Reverse geocoding implementation
- [ ] Performance optimization for large shards

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
2. Shards uploaded via `scripts/upload_shards.sh`
3. STAC catalog at `catalog.json` in bucket root

## API

### GET /search

```
/search?q=boston&limit=10&autocomplete=true&format=json
```

Parameters:
- `q` (required): Search query
- `limit`: Max results (default: 10, max: 40)
- `autocomplete`: Enable prefix matching (default: true)
- `format`: `json` or `geojson` (default: json)

Location bias is automatically applied based on the `CF-IPCountry` header.

### GET /reverse

```
/reverse?lat=42.36&lon=-71.06
```

Parameters:
- `lat` (required): Latitude
- `lon` (required): Longitude

(Not yet implemented for R2 shards)

## Architecture

The worker:
1. Fetches STAC catalog from R2 to discover available shards
2. Selects HEAD shard (global high-population places) + country shard (if available)
3. Fetches SQLite databases from R2
4. Opens each database in-memory using rusqlite
5. Executes FTS5 queries against each shard
6. Merges, deduplicates, and applies location bias
7. Returns results as JSON or GeoJSON
