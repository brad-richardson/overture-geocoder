# Overture Geocoder - JavaScript/TypeScript Client

Forward geocoder using Overture Maps data with Nominatim-compatible API.

## Installation

```bash
npm install @bradrichardson/overture-geocoder
```

The core HTTP client (search, reverse, ID lookup, health) has **zero runtime
dependencies**.

The geometry features (`getFullGeometry`, `getNearbyPlaces`,
`getNearbyAddresses`, `verifyContainsPoint`, and
`reverse(..., { verifyGeometry: true })`) query Overture S3 directly via
DuckDB-WASM and require the optional peer dependency:

```bash
# Only needed for geometry features
npm install @bradrichardson/overturemaps
```

If it is not installed, calling a geometry method throws a `GeocoderError`
telling you to install it. Everything else works without it.

> **Upgrading from 0.2.x:** `@bradrichardson/overturemaps` moved from a hard
> dependency to an optional peer dependency, and its STAC utilities
> (`getStacCatalog`, `getLatestRelease`, `readByBbox`, `readByBboxAll`, etc.)
> are no longer re-exported — import them from `@bradrichardson/overturemaps`
> directly. Reverse confidence values changed from `"exact" | "bbox" |
> "approximate"` to `"high" | "medium" | "low"` to match the API.

## Usage

### Basic Search

```typescript
import { OvertureGeocoder } from '@bradrichardson/overture-geocoder';

const geocoder = new OvertureGeocoder();

// Search for a place
const results = await geocoder.search('Boston, MA');
console.log(results[0].primary_name); // "Boston"
console.log(results[0].lat, results[0].lon); // 42.3588336, -71.0578303
console.log(results[0].country, results[0].region); // "US", "US-MA"
```

### Reverse Geocoding

```typescript
// Reverse geocode coordinates
const results = await geocoder.reverse(42.3588336, -71.0578303);
console.log(results[0].primary_name);
console.log(results[0].confidence); // "high" | "medium" | "low"
console.log(results[0].hierarchy);  // containing divisions, always present

// Returns [] when no division contains the point (server 404)
```

### ID Lookup

Look up a GERS ID's bounding box via the worker's parquet ID index:

```typescript
const result = await geocoder.lookupId('0123abcd-0000-4000-8000-000000000000');
if (result) {
  console.log(result.bbox); // { xmin, ymin, xmax, ymax }
} else {
  // null: ID not found (404)
}
// Throws GeocoderError with status 503 if the ID index is unavailable
```

### Health Check

```typescript
const health = await geocoder.health();
console.log(health.status, health.version);
```

### Convenience Functions

```typescript
import { geocode, reverseGeocode } from '@bradrichardson/overture-geocoder';

// Quick one-off search
const results = await geocode('Cambridge, MA', { limit: 5 });

// Quick reverse geocode
const reverse = await reverseGeocode(42.3588336, -71.0578303);
```

### GeoJSON Output

```typescript
const geojson = await geocoder.searchGeoJSON('Boston');
// Returns FeatureCollection with Point geometries

// Equivalent, with the return type narrowed by overload:
const geojson2 = await geocoder.search('Boston', { format: 'geojson' });

const reverseGeojson = await geocoder.reverseGeoJSON(42.3588336, -71.0578303);
```

> **Note:** `reverseGeoJSON` (and `reverse` with `format: "geojson"`) requires
> an updated worker. The currently deployed worker ignores `format=geojson`
> on `/reverse` and returns a plain JSON object; a server-side fix is in
> flight.

### Full Geometry Fetching (optional peer dependency)

Fetch full geometries from Overture S3 data (requires
`@bradrichardson/overturemaps` to be installed):

```typescript
const geometry = await geocoder.getFullGeometry('gers-id');
// Returns full polygon/multipolygon geometry from Overture

// Release resources when done
await geocoder.close();
```

### Nearby Search (optional peer dependency)

```typescript
// Find nearby places
const places = await geocoder.getNearbyPlaces(42.3588336, -71.0578303, {
  radiusKm: 1,
  limit: 10,
});

// Find nearby addresses
const addresses = await geocoder.getNearbyAddresses(42.3588336, -71.0578303, {
  radiusKm: 0.5,
});
```

## Configuration

```typescript
const geocoder = new OvertureGeocoder({
  baseUrl: 'https://geocoder.bradr.dev', // default
  timeout: 30000, // ms
  retries: 3,
  retryDelay: 1000, // ms, base delay for exponential backoff
});
```

### Retries and Rate Limiting

When `retries > 0`:

- **429 (rate limited):** the client honors the `Retry-After` header
  (capped at 30 seconds) before retrying. The API allows 60 requests/minute
  per IP.
- **5xx / network errors / timeouts:** retried with exponential backoff
  (`retryDelay * 2^attempt`, capped at 30 seconds) plus jitter.
- **Other 4xx:** never retried.

## Types

All types are exported for TypeScript users:

```typescript
import type {
  GeocoderResult,
  ReverseGeocoderResult,
  Confidence, // "high" | "medium" | "low"
  HierarchyEntry,
  IdLookupResult,
  HealthStatus,
  BoundingBox,
  SearchOptions,
  ReverseOptions,
  OvertureGeocoderConfig,
  GeoJSONFeature,
  GeoJSONFeatureCollection,
  OverturePlace,
  OvertureAddress,
  NearbySearchOptions,
} from '@bradrichardson/overture-geocoder';
```

## API Reference

### `OvertureGeocoder`

- `search(query: string, options?: SearchOptions): Promise<GeocoderResult[]>`
  (returns `GeoJSONFeatureCollection` when `format: "geojson"`)
- `searchGeoJSON(query: string, options?: SearchOptions): Promise<GeoJSONFeatureCollection>`
- `reverse(lat: number, lon: number, options?: ReverseOptions): Promise<ReverseGeocoderResult[]>`
  (returns `GeoJSONFeatureCollection` when `format: "geojson"`; requires updated worker)
- `reverseGeoJSON(lat: number, lon: number): Promise<GeoJSONFeatureCollection>` (requires updated worker)
- `lookupId(gersId: string): Promise<IdLookupResult | null>` (null on 404; throws with status on 503)
- `health(): Promise<HealthStatus>`
- `verifyContainsPoint(gersId: string, lat: number, lon: number): Promise<boolean>` *
- `getFullGeometry(gersId: string): Promise<GeoJSONFeature | null>` *
- `getNearbyPlaces(lat: number, lon: number, options?: NearbySearchOptions): Promise<OverturePlace[]>` *
- `getNearbyAddresses(lat: number, lon: number, options?: NearbySearchOptions): Promise<OvertureAddress[]>` *
- `reverseAndRefine(lat: number, lon: number, options?: ReverseAndRefineOptions): Promise<RefinedReverseResult>` *
- `close(): Promise<void>` - Release resources when done with geometry fetching

\* Requires the optional `@bradrichardson/overturemaps` peer dependency.

### `SearchOptions`

```typescript
interface SearchOptions {
  limit?: number;  // 1-40, default: 10
  format?: 'json' | 'jsonv2' | 'geojson';
}
```

### `ReverseGeocoderResult`

```typescript
interface ReverseGeocoderResult {
  gers_id: string;
  primary_name: string;
  subtype: string;
  lat: number;
  lon: number;
  boundingbox: [number, number, number, number];
  distance_km: number;
  confidence: 'high' | 'medium' | 'low';
  hierarchy: HierarchyEntry[]; // always present
}
```

## License

MIT
