export { BoundingBox, Feature, OvertureType, StacCatalog, StacLink, clearCache as clearCatalogCache, getLatestRelease, getStacCatalog, readByBbox, readByBboxAll } from '@bradrichardson/overturemaps';

/**
 * Overture Geocoder JavaScript/TypeScript Client
 *
 * Forward geocoder using Overture Maps data with Nominatim-compatible API.
 */

interface GeocoderResult {
    gers_id: string;
    primary_name: string;
    lat: number;
    lon: number;
    boundingbox: [number, number, number, number];
    importance: number;
    type: string;
}
interface SearchOptions {
    /** Maximum number of results (1-40, default: 10) */
    limit?: number;
    /** Response format */
    format?: "json" | "jsonv2" | "geojson";
}
interface ReverseOptions {
    /** Response format */
    format?: "jsonv2" | "geojson";
    /**
     * Verify point-in-polygon using full geometry from S3.
     * When enabled, fetches full polygon for each bbox-matched result
     * and filters to only results where the point is truly inside.
     * @default false
     */
    verifyGeometry?: boolean;
    /**
     * Maximum number of results to verify geometry for.
     * Higher values increase accuracy but take longer.
     * @default 10
     */
    verifyLimit?: number;
}
interface HierarchyEntry {
    gers_id: string;
    subtype: string;
    name: string;
}
interface ReverseGeocoderResult {
    gers_id: string;
    primary_name: string;
    subtype: string;
    lat: number;
    lon: number;
    boundingbox: [number, number, number, number];
    distance_km: number;
    confidence: "exact" | "bbox" | "approximate";
    hierarchy?: HierarchyEntry[];
}
interface OvertureGeocoderConfig {
    /** API base URL (default: 'https://overture-geocoder.bradr.workers.dev') */
    baseUrl?: string;
    /** Request timeout in milliseconds (default: 30000) */
    timeout?: number;
    /** Number of retry attempts for failed requests (default: 0) */
    retries?: number;
    /** Delay between retries in milliseconds (default: 1000) */
    retryDelay?: number;
    /** Custom headers to include in all requests */
    headers?: Record<string, string>;
    /** Custom fetch implementation (useful for testing or custom transports) */
    fetch?: typeof globalThis.fetch;
    /** Request interceptor - modify request before sending */
    onRequest?: (url: string, init: RequestInit) => RequestInit | Promise<RequestInit>;
    /** Response interceptor - process response before returning */
    onResponse?: (response: Response) => Response | Promise<Response>;
}
interface GeoJSONFeature {
    type: "Feature";
    id: string;
    properties: Record<string, unknown>;
    bbox?: [number, number, number, number];
    geometry: GeoJSONGeometry;
}
type GeoJSONGeometry = {
    type: "Point";
    coordinates: [number, number];
} | {
    type: "LineString";
    coordinates: [number, number][];
} | {
    type: "Polygon";
    coordinates: [number, number][][];
} | {
    type: "MultiPoint";
    coordinates: [number, number][];
} | {
    type: "MultiLineString";
    coordinates: [number, number][][];
} | {
    type: "MultiPolygon";
    coordinates: [number, number][][][];
};
interface GeoJSONFeatureCollection {
    type: "FeatureCollection";
    features: GeoJSONFeature[];
}
interface OverturePlace {
    id: string;
    names: {
        primary: string;
        common?: Record<string, string>;
    };
    categories?: {
        primary?: string;
        alternate?: string[];
    };
    addresses?: Array<{
        freeform?: string;
        locality?: string;
        region?: string;
        country?: string;
        postcode?: string;
    }>;
    phones?: string[];
    websites?: string[];
    brand?: {
        names?: {
            primary?: string;
        };
    };
    lat: number;
    lon: number;
    distance_km: number;
    confidence: number;
}
interface OvertureAddress {
    id: string;
    number?: string;
    street?: string;
    unit?: string;
    postcode?: string;
    freeform?: string;
    lat: number;
    lon: number;
    distance_km: number;
}
interface NearbySearchOptions {
    /**
     * Search radius in kilometers
     * @default 1
     */
    radiusKm?: number;
    /**
     * Maximum number of results
     * @default 10
     */
    limit?: number;
    /**
     * Category filter for places (e.g., 'restaurant', 'cafe')
     */
    category?: string;
}
interface RefinedReverseResult {
    /** Division hierarchy for the location */
    divisions: ReverseGeocoderResult[];
    /** Nearby places from Overture */
    places?: OverturePlace[];
    /** Nearby addresses from Overture */
    addresses?: OvertureAddress[];
}
interface ReverseAndRefineOptions {
    /**
     * Verify division geometry using point-in-polygon
     * @default true
     */
    verifyGeometry?: boolean;
    /**
     * Include nearby places in results
     * @default true
     */
    includePlaces?: boolean;
    /**
     * Include nearby addresses in results
     * @default true
     */
    includeAddresses?: boolean;
    /**
     * Search radius for nearby places/addresses in km
     * @default 0.5
     */
    radiusKm?: number;
    /**
     * Maximum nearby results per type
     * @default 5
     */
    nearbyLimit?: number;
    /**
     * Category filter for places
     */
    placeCategory?: string;
}
declare class GeocoderError extends Error {
    readonly status?: number | undefined;
    readonly response?: Response | undefined;
    constructor(message: string, status?: number | undefined, response?: Response | undefined);
}
declare class GeocoderTimeoutError extends GeocoderError {
    constructor(message?: string);
}
declare class GeocoderNetworkError extends GeocoderError {
    readonly cause?: Error | undefined;
    constructor(message: string, cause?: Error | undefined);
}
declare class OvertureGeocoder {
    private readonly baseUrl;
    private readonly timeout;
    private readonly retries;
    private readonly retryDelay;
    private readonly headers;
    private readonly fetchFn;
    private readonly onRequest?;
    private readonly onResponse?;
    constructor(config?: OvertureGeocoderConfig);
    /**
     * Search for addresses matching the query.
     */
    search(query: string, options?: SearchOptions): Promise<GeocoderResult[]>;
    /**
     * Search and return results as GeoJSON FeatureCollection.
     */
    searchGeoJSON(query: string, options?: Omit<SearchOptions, "format">): Promise<GeoJSONFeatureCollection>;
    /**
     * Reverse geocode coordinates to divisions.
     *
     * Returns divisions (localities, neighborhoods, counties, etc.) that
     * contain the given coordinate. Results are sorted by specificity
     * (smallest/most specific first).
     *
     * @param lat Latitude
     * @param lon Longitude
     * @param options Reverse geocoding options
     * @param options.verifyGeometry If true, fetches full polygons from S3 and filters
     *                               to only results where point is inside the polygon
     */
    reverse(lat: number, lon: number, options?: ReverseOptions): Promise<ReverseGeocoderResult[]>;
    /**
     * Verify which reverse geocode results actually contain the point.
     * Fetches full geometry from S3 and performs point-in-polygon checks.
     * Updates confidence to "exact" for verified results.
     */
    private verifyResultsGeometry;
    /**
     * Reverse geocode and return results as GeoJSON FeatureCollection.
     */
    reverseGeoJSON(lat: number, lon: number): Promise<GeoJSONFeatureCollection>;
    /**
     * Verify if a point is inside a division's polygon.
     *
     * Fetches the full geometry from Overture S3 and performs
     * a point-in-polygon check using ray casting algorithm.
     */
    verifyContainsPoint(gersId: string, lat: number, lon: number): Promise<boolean>;
    /**
     * Get the base URL configured for this client.
     */
    getBaseUrl(): string;
    /**
     * Fetch full geometry for a GERS ID directly from Overture S3.
     *
     * Uses the @bradrichardson/overturemaps package for efficient lookup.
     *
     * @param gersId The GERS ID to look up
     * @returns GeoJSON Feature with full geometry, or null if not found
     */
    getFullGeometry(gersId: string): Promise<GeoJSONFeature | null>;
    /**
     * Close DuckDB connection and release resources.
     * Call this when done with geometry/place/address fetching to free memory.
     */
    close(): Promise<void>;
    /**
     * Get nearby places from Overture S3.
     *
     * Queries the Overture places theme directly from S3 within a radius
     * of the given coordinates. Results include business names, categories,
     * addresses, and contact info.
     *
     * @param lat Latitude of center point
     * @param lon Longitude of center point
     * @param options Search options (radius, limit, category filter)
     * @returns Array of nearby places sorted by distance
     */
    getNearbyPlaces(lat: number, lon: number, options?: NearbySearchOptions): Promise<OverturePlace[]>;
    /**
     * Get nearby addresses from Overture S3.
     *
     * Queries the Overture addresses theme directly from S3 within a radius
     * of the given coordinates. Returns structured address components.
     *
     * @param lat Latitude of center point
     * @param lon Longitude of center point
     * @param options Search options (radius, limit)
     * @returns Array of nearby addresses sorted by distance
     */
    getNearbyAddresses(lat: number, lon: number, options?: Omit<NearbySearchOptions, "category">): Promise<OvertureAddress[]>;
    /**
     * Combined reverse geocode with optional geometry verification and
     * nearby places/addresses lookup.
     *
     * This is a convenience method that combines:
     * 1. Reverse geocoding (division hierarchy)
     * 2. Optional point-in-polygon verification
     * 3. Nearby places from Overture S3
     * 4. Nearby addresses from Overture S3
     *
     * @param lat Latitude
     * @param lon Longitude
     * @param options Configuration for what to include
     * @returns Combined result with divisions, places, and addresses
     */
    reverseAndRefine(lat: number, lon: number, options?: ReverseAndRefineOptions): Promise<RefinedReverseResult>;
    private fetchWithRetry;
    private doFetch;
    private parseResults;
    private parseReverseResults;
    private pointInPolygon;
    private delay;
    /**
     * Convert a radius in km to a bounding box centered on lat/lon
     */
    private radiusToBbox;
    /**
     * Calculate Haversine distance between two points in km
     */
    private haversineDistance;
}
/**
 * Quick geocode function using default settings.
 */
declare function geocode(query: string, options?: SearchOptions): Promise<GeocoderResult[]>;
/**
 * Quick reverse geocode function using default settings.
 */
declare function reverseGeocode(lat: number, lon: number, options?: ReverseOptions): Promise<ReverseGeocoderResult[]>;

export { type GeoJSONFeature, type GeoJSONFeatureCollection, type GeoJSONGeometry, GeocoderError, GeocoderNetworkError, type GeocoderResult, GeocoderTimeoutError, type HierarchyEntry, type NearbySearchOptions, type OvertureAddress, OvertureGeocoder, type OvertureGeocoderConfig, type OverturePlace, type RefinedReverseResult, type ReverseAndRefineOptions, type ReverseGeocoderResult, type ReverseOptions, type SearchOptions, OvertureGeocoder as default, geocode, reverseGeocode };
