# geocoder-worker

Cloudflare Worker for Overture geocoding using R2-stored SQLite shards.

## Status

**Work in Progress** - The worker structure is in place but SQLite WASM queries are not yet implemented.

### Completed
- Worker routing (`/search`, `/reverse`, `/health`, `/`)
- STAC catalog loading from R2
- Shard selection (HEAD + country based on CF-IPCountry)
- R2 bucket integration

### TODO
- [ ] WASM SQLite query execution
- [ ] Edge caching for catalog and shards
- [ ] Reverse geocoding implementation

## SQLite WASM Options

The challenge is running SQLite queries in a Cloudflare Worker (WASM environment). Options being considered:

1. **sql.js via JS interop**: Import sql.js and call from Rust
2. **sqlite-wasm-rs**: Rust bindings for SQLite WASM
3. **rusqlite WASM build**: Compile rusqlite for wasm32 target

## Development

```bash
# Check compilation
cargo check -p geocoder-worker

# Build for WASM (requires worker-build)
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
- `limit`: Max results (default: 10, max: 50)
- `autocomplete`: Enable prefix matching (default: true)
- `format`: `json` or `geojson` (default: json)

### GET /reverse

```
/reverse?lat=42.36&lon=-71.06
```

Parameters:
- `lat` (required): Latitude
- `lon` (required): Longitude

(Not yet implemented for R2 shards)
