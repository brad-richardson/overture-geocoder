# Overture Geocoder - JavaScript/TypeScript Client

Forward geocoder using Overture Maps data with Nominatim-compatible API.

## Installation

```bash
npm install @bradrichardson/overture-geocoder
```

## Usage

### Basic Search

```typescript
import { OvertureGeocoder } from '@bradrichardson/overture-geocoder';

const geocoder = new OvertureGeocoder();

// Search for a place
const results = await geocoder.search('Boston, MA');
console.log(results[0].primary_name); // "Boston"
console.log(results[0].lat, results[0].lon); // 42.3588336, -71.0578303
```

### Reverse Geocoding

```typescript
// Reverse geocode coordinates
const results = await geocoder.reverse(42.3588336, -71.0578303);
console.log(results[0].primary_name);
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

const reverseGeojson = await geocoder.reverseGeoJSON(42.3588336, -71.0578303);
```

### Full Geometry Fetching

Fetch full geometries from Overture S3 data:

```typescript
const geometry = await geocoder.getFullGeometry('gers-id');
// Returns full polygon/multipolygon geometry from Overture

// Release resources when done
await geocoder.close();
```

### Nearby Search

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
  baseUrl: 'https://overture-geocoder.bradr.workers.dev', // default
  timeout: 30000, // ms
  retries: 3,
  retryDelay: 1000, // ms
});
```

## Types

All types are exported for TypeScript users:

```typescript
import type {
  GeocoderResult,
  SearchOptions,
  ReverseOptions,
  OvertureGeocoderConfig,
  GeoJSONFeature,
  GeoJSONFeatureCollection,
  OverturePlace,
  OvertureAddress,
  NearbySearchOptions,
  // STAC types for advanced usage
  StacCatalog,
  BoundingBox,
} from '@bradrichardson/overture-geocoder';
```

## API Reference

### `OvertureGeocoder`

- `search(query: string, options?: SearchOptions): Promise<GeocoderResult[]>`
- `searchGeoJSON(query: string, options?: SearchOptions): Promise<GeoJSONFeatureCollection>`
- `reverse(lat: number, lon: number, options?: ReverseOptions): Promise<ReverseGeocoderResult[]>`
- `reverseGeoJSON(lat: number, lon: number): Promise<GeoJSONFeatureCollection>`
- `verifyContainsPoint(gersId: string, lat: number, lon: number): Promise<boolean>`
- `getFullGeometry(gersId: string): Promise<GeoJSONFeature | null>`
- `getNearbyPlaces(lat: number, lon: number, options?: NearbySearchOptions): Promise<OverturePlace[]>`
- `getNearbyAddresses(lat: number, lon: number, options?: NearbySearchOptions): Promise<OvertureAddress[]>`
- `reverseAndRefine(lat: number, lon: number, options?: ReverseAndRefineOptions): Promise<RefinedReverseResult>`
- `close(): Promise<void>` - Release resources when done with geometry fetching

### `SearchOptions`

```typescript
interface SearchOptions {
  limit?: number;        // 1-40, default: 10
  countrycodes?: string; // e.g., "us,ca"
  viewbox?: [number, number, number, number]; // [lon1, lat1, lon2, lat2]
  bounded?: boolean;     // Restrict to viewbox
  format?: 'json' | 'jsonv2' | 'geojson';
}
```

## License

MIT
