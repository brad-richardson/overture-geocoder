# Overture Geocoder

A forward and reverse geocoder built on [Overture Maps](https://overturemaps.org/) data with minimal infrastructure costs.

## Features

- **Forward geocoding**: Search addresses and divisions by free-form text query
- **Reverse geocoding**: Find divisions containing a coordinate (localities, neighborhoods, counties)
- **Global divisions**: 450K+ cities, neighborhoods, and administrative areas worldwide
- **Autocomplete**: Prefix matching enabled by default for type-ahead search
- **Minimal storage**: SQLite with FTS5 indexes (~50MB globally)
- **On-demand geometry**: Full Overture polygons fetched via GERS ID
- **Zero egress costs**: Client-side DuckDB queries Overture S3 directly

## API

### Forward Geocoding (Search)

```bash
# Search for places
curl "https://overture-geocoder.bradr.workers.dev/search?q=boston"

# With autocomplete (default: enabled)
curl "https://overture-geocoder.bradr.workers.dev/search?q=bost"

# Disable autocomplete for exact matching
curl "https://overture-geocoder.bradr.workers.dev/search?q=boston&autocomplete=0"
```

### Reverse Geocoding

```bash
# Find divisions containing a point
curl "https://overture-geocoder.bradr.workers.dev/reverse?lat=42.3601&lon=-71.0589"
```

Response includes divisions sorted by specificity (most specific first), with hierarchy information showing containment relationships.

## Quick Start

### Python

```python
from overture_geocoder import OvertureGeocoder

geocoder = OvertureGeocoder()

# Forward geocoding
results = geocoder.search("boston")
print(results[0].primary_name)  # "Boston, MA"

# Reverse geocoding
divisions = geocoder.reverse(42.3601, -71.0589)
for div in divisions:
    print(f"{div.subtype}: {div.primary_name}")

# Get full geometry (queries Overture S3 directly)
geometry = geocoder.get_geometry(results[0].gers_id)
```

### JavaScript/TypeScript

```typescript
import { OvertureGeocoder } from 'overture-geocoder';

const geocoder = new OvertureGeocoder();

// Forward geocoding
const results = await geocoder.search("boston");
console.log(results[0].primary_name);

// Reverse geocoding
const divisions = await geocoder.reverse(42.3601, -71.0589);

// Get full geometry (uses DuckDB-WASM)
const geometry = await geocoder.getFullGeometry(results[0].gers_id);
```

## Deployment

### Manual Triggers

The deploy workflow supports manual triggers via GitHub Actions:

| Input | Description |
|-------|-------------|
| `force_data_update` | Force data update even if Overture release unchanged |
| `force_schema_rebuild` | Force rebuild with current release |
| `force_full_rebuild` | **Drop and recreate all tables** with fresh data (use after schema/logic changes) |

To trigger a full rebuild after changing search logic:
1. Go to **Actions** → **Deploy**
2. Click **Run workflow**
3. Check **"Force full rebuild"**
4. Click **Run workflow**

## Architecture

```
Client (Python/JS)
    │
    ├── /search ──────► Cloudflare Worker ──► D1 (FTS5 Index)
    │
    ├── /reverse ─────► Cloudflare Worker ──► D1 (Divisions Reverse Index)
    │
    └── getGeometry() ──► DuckDB ──► Overture S3 (free egress)
```

## Data

Uses [Overture Maps](https://overturemaps.org/) division data:
- 450K+ global divisions (localities, neighborhoods, counties, regions)
- GERS IDs for stable entity references
- Monthly releases with differential updates

## Cost Estimates

| Scale | Storage | Monthly Cost |
|-------|---------|--------------|
| Global divisions | ~50MB | $0 (Cloudflare free tier) |
| Global + addresses | ~15GB | $5-20 (D1 paid tier) |

## License

MIT
