# Implementation Specification

## Overview

Forward and reverse geocoder using Overture Maps division data (administrative boundaries).
The system uses a **sharded architecture** hosted on Cloudflare Workers and R2.

## Data Source

**Overture Maps Divisions Theme**
- Location: `s3://overturemaps-us-west-2/release/{release}/theme=divisions/type=division/*.parquet`
- Current release: Auto-detected (e.g., `2024-12-18.0`)
- Schema: [docs.overturemaps.org/schema/reference/divisions/division](https://docs.overturemaps.org/schema/reference/divisions/division/)

### Division Schema (relevant fields)
```
id: string              # GERS UUID
geometry: Polygon (WKB) # Boundary polygon
bbox: struct            # {xmin, ymin, xmax, ymax}
subtype: string         # locality, county, region, country, etc.
names: struct           # Primary and alternate names
population: int         # Population count (where available)
```

## Storage Design

The system uses **SQLite Shards** stored in Cloudflare R2. This bypasses D1 storage limits and allows for scalable, cost-effective hosting of large datasets (like global addresses).

### Sharding Strategy
- **Global Divisions**: Stored in a single or few shards (e.g., `divisions.db`).
- **Addresses**: Sharded by country and region (e.g., `US.db`, `US-MA.db`).
- **Worker Logic**: The Rust worker dynamically fetches the appropriate shard from R2 based on the query context or loads a default shard.

### Forward Geocoding Schema (SQLite + FTS5)

```sql
CREATE TABLE divisions (
    rowid INTEGER PRIMARY KEY,
    gers_id TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,              -- Division subtype
    primary_name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    bbox_xmin REAL NOT NULL,
    bbox_ymin REAL NOT NULL,
    bbox_xmax REAL NOT NULL,
    bbox_ymax REAL NOT NULL,
    population INTEGER,
    country TEXT,
    region TEXT
);

CREATE VIRTUAL TABLE divisions_fts USING fts5(
    search_text,
    content=divisions,
    content_rowid=rowid,
    tokenize='porter unicode61 remove_diacritics 1',
    prefix='2 3'                     -- For autocomplete support
);
```

### Reverse Geocoding Schema

```sql
CREATE TABLE divisions_reverse (
    rowid INTEGER PRIMARY KEY,
    gers_id TEXT NOT NULL UNIQUE,
    subtype TEXT NOT NULL,
    primary_name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    bbox_xmin REAL NOT NULL,
    bbox_ymin REAL NOT NULL,
    bbox_xmax REAL NOT NULL,
    bbox_ymax REAL NOT NULL,
    area REAL NOT NULL,              -- For sorting by specificity
    population INTEGER,
    country TEXT,
    region TEXT
);

CREATE INDEX idx_bbox ON divisions_reverse(bbox_xmin, bbox_xmax, bbox_ymin, bbox_ymax);
CREATE INDEX idx_area ON divisions_reverse(area);
```

## API Design

### Forward Geocoding: `/search`

**Request**
```
GET /search?q={query}&limit=10
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| q | string | required | Free-form search query |
| limit | int | 10 | Max results (1-40) |
| autocomplete | bool | true | Enable autocomplete mode |

**Response**
```json
[
  {
    "gers_id": "01234567-89ab-cdef-0123-456789abcdef",
    "primary_name": "Boston",
    "lat": 42.3601,
    "lon": -71.0589,
    "boundingbox": [42.227, 42.397, -71.191, -70.923],
    "importance": 0.85,
    "type": "locality"
  }
]
```

### Reverse Geocoding: `/reverse`

**Request**
```
GET /reverse?lat={lat}&lon={lon}
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| lat | float | required | Latitude (-90 to 90) |
| lon | float | required | Longitude (-180 to 180) |

**Response**
```json
[
  {
    "gers_id": "01234567-89ab-cdef-0123-456789abcdef",
    "primary_name": "Boston",
    "subtype": "locality",
    "lat": 42.3601,
    "lon": -71.0589,
    "boundingbox": [42.227, 42.397, -71.191, -70.923],
    "distance_km": 0.12,
    "confidence": "bbox",
    "hierarchy": [
      {"gers_id": "...", "subtype": "locality", "name": "Boston"},
      {"gers_id": "...", "subtype": "county", "name": "Suffolk County"},
      {"gers_id": "...", "subtype": "region", "name": "Massachusetts"}
    ]
  }
]
```

**Note:** Reverse geocoding selects a country shard from the requested coordinate only when one collection bbox is unambiguous, then uses bounding box filtering. Overlapping bboxes and legacy metadata fall back to the caller's IP country; without either, the service uses `HEAD`. Results remain bbox-confidence estimates, not exact polygon containment.

## Indexing Pipeline

1.  **Download & Extract**: `scripts/download_divisions.sql` (DuckDB) extracts data from Overture S3 to Parquet.
2.  **Build Shards**: `scripts/build_shards.py` creates SQLite databases (shards) from the Parquet data.
3.  **Upload**: `scripts/upload_shards.sh` uploads the `.db` files to Cloudflare R2.

## Hosting

### Cloudflare Stack

| Service | Purpose |
|---------|---------|
| **Workers** (Rust) | API endpoints, Request handling, Shard logic |
| **R2** | Storage for SQLite shards (low cost, high capacity) |

### Deployment

The Rust worker is located in `crates/geocoder-worker`.
Configuration: `crates/geocoder-worker/wrangler.toml`

## Client Libraries

- **Python**: `clients/python`
- **JavaScript/TypeScript**: `clients/js`