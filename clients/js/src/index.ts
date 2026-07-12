/**
 * Overture Geocoder JavaScript/TypeScript Client
 *
 * Client for the divisions-first Overture Geocoder API.
 */

// NOTE: @bradrichardson/overturemaps is an OPTIONAL peer dependency.
// It pulls in DuckDB-WASM + Apache Arrow, so it is only loaded on demand
// (via dynamic import) by the geometry methods that need it:
// getFullGeometry, getNearbyPlaces, getNearbyAddresses, verifyContainsPoint,
// and reverse(..., { verifyGeometry: true }).
// There must be NO top-level/static import of it in this file so bundlers
// can tree-shake it away for users that only need the HTTP API.

// ============================================================================
// Types
// ============================================================================

/** Bounding box in Overture convention. */
export interface BoundingBox {
  xmin: number;
  ymin: number;
  xmax: number;
  ymax: number;
}

/**
 * Minimal structural type for the optional @bradrichardson/overturemaps
 * module. Kept local so the published type declarations do not reference
 * the optional package.
 */
interface OvertureMapsModule {
  getFeatureByGersId(gersId: string): Promise<OvertureFeatureLike | null>;
  closeDb(): Promise<void>;
  readByBboxAll(
    type: string,
    bbox: BoundingBox,
    options?: { limit?: number }
  ): Promise<OvertureFeatureLike[]>;
}

interface OvertureFeatureLike {
  id?: unknown;
  properties: Record<string, unknown>;
  bbox?: number[];
  geometry: { type: string; coordinates: unknown };
}

export interface GeocoderResult {
  gers_id: string;
  primary_name: string;
  lat: number;
  lon: number;
  boundingbox: [number, number, number, number];
  importance: number;
  type: string;
  /** ISO country code (e.g. "US"), when known */
  country?: string;
  /** Region code (e.g. "US-MA"), when known */
  region?: string;
}

export interface SearchOptions {
  /** Maximum number of results (1-40, default: 10) */
  limit?: number;
  /** Response format */
  format?: "json" | "jsonv2" | "geojson";
}

export interface ReverseOptions {
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

export interface HierarchyEntry {
  gers_id: string;
  subtype: string;
  name: string;
}

/** Confidence levels returned by the reverse geocoder. */
export type Confidence = "high" | "medium" | "low";

export interface ReverseGeocoderResult {
  gers_id: string;
  primary_name: string;
  subtype: string;
  lat: number;
  lon: number;
  boundingbox: [number, number, number, number];
  distance_km: number;
  confidence: Confidence;
  /** Containing division hierarchy (always present; may be empty). */
  hierarchy: HierarchyEntry[];
}

/** Result of an ID lookup via GET /id/{gers_id}. */
export interface IdLookupResult {
  id: string;
  bbox: BoundingBox;
  /** Present for format-v3 ID-index shards. */
  feature_type?: string | null;
  theme?: string | null;
  filename?: string | null;
  last_seen_release?: string | null;
  registry_member?: boolean;
  exists_in_current_release?: boolean;
  overture_path?: string | null;
}

/** Response from GET /health. */
export interface HealthStatus {
  status: string;
  version?: string;
  error?: string;
}


export interface OvertureGeocoderConfig {
  /** API base URL (default: 'https://geocoder.bradr.dev') */
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

export interface GeoJSONFeature {
  type: "Feature";
  id: string;
  properties: Record<string, unknown>;
  bbox?: [number, number, number, number];
  geometry: GeoJSONGeometry;
}

export type GeoJSONGeometry =
  | { type: "Point"; coordinates: [number, number] }
  | { type: "LineString"; coordinates: [number, number][] }
  | { type: "Polygon"; coordinates: [number, number][][] }
  | { type: "MultiPoint"; coordinates: [number, number][] }
  | { type: "MultiLineString"; coordinates: [number, number][][] }
  | { type: "MultiPolygon"; coordinates: [number, number][][][] };

export interface GeoJSONFeatureCollection {
  type: "FeatureCollection";
  features: GeoJSONFeature[];
}

// ============================================================================
// Overture S3 Query Types (Places, Addresses)
// ============================================================================

export interface OverturePlace {
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

export interface OvertureAddress {
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

export interface NearbySearchOptions {
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

export interface RefinedReverseResult {
  /** Division hierarchy for the location */
  divisions: ReverseGeocoderResult[];
  /** Nearby places from Overture */
  places?: OverturePlace[];
  /** Nearby addresses from Overture */
  addresses?: OvertureAddress[];
}

export interface ReverseAndRefineOptions {
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

// ============================================================================
// Errors
// ============================================================================

export class GeocoderError extends Error {
  constructor(
    message: string,
    public readonly status?: number,
    public readonly response?: Response
  ) {
    super(message);
    this.name = "GeocoderError";
  }
}

export class GeocoderTimeoutError extends GeocoderError {
  constructor(message: string = "Request timed out") {
    super(message);
    this.name = "GeocoderTimeoutError";
  }
}

export class GeocoderNetworkError extends GeocoderError {
  constructor(message: string, public readonly cause?: Error) {
    super(message);
    this.name = "GeocoderNetworkError";
  }
}

// ============================================================================
// Constants
// ============================================================================

const DEFAULT_BASE_URL = "https://geocoder.bradr.dev";
const DEFAULT_TIMEOUT = 30000;
const DEFAULT_RETRIES = 0;
const DEFAULT_RETRY_DELAY = 1000;
/** Maximum backoff/Retry-After delay between attempts (ms). */
const MAX_RETRY_DELAY = 30000;

const OVERTUREMAPS_INSTALL_HINT =
  "npm install @bradrichardson/overturemaps to use geometry features " +
  "(getFullGeometry, getNearbyPlaces, getNearbyAddresses, verifyGeometry)";

// ============================================================================
// Client
// ============================================================================

export class OvertureGeocoder {
  private readonly baseUrl: string;
  private readonly timeout: number;
  private readonly retries: number;
  private readonly retryDelay: number;
  private readonly headers: Record<string, string>;
  private readonly fetchFn: typeof globalThis.fetch;
  private readonly onRequest?: OvertureGeocoderConfig["onRequest"];
  private readonly onResponse?: OvertureGeocoderConfig["onResponse"];
  /** Lazily-loaded optional @bradrichardson/overturemaps module. */
  private overtureMaps?: Promise<OvertureMapsModule>;

  constructor(config: OvertureGeocoderConfig = {}) {
    this.baseUrl = (config.baseUrl || DEFAULT_BASE_URL).replace(/\/$/, "");
    this.timeout = config.timeout ?? DEFAULT_TIMEOUT;
    this.retries = config.retries ?? DEFAULT_RETRIES;
    this.retryDelay = config.retryDelay ?? DEFAULT_RETRY_DELAY;
    this.headers = config.headers ?? {};
    // Bind fetch to globalThis to avoid "Illegal invocation" errors in browsers
    this.fetchFn = config.fetch ?? globalThis.fetch.bind(globalThis);
    this.onRequest = config.onRequest;
    this.onResponse = config.onResponse;
  }

  /**
   * Search for places matching the query.
   *
   * With `format: "geojson"` the worker returns a GeoJSON FeatureCollection,
   * so the return type changes accordingly (or use {@link searchGeoJSON}).
   */
  async search(
    query: string,
    options?: Omit<SearchOptions, "format"> & { format?: "json" | "jsonv2" }
  ): Promise<GeocoderResult[]>;
  async search(
    query: string,
    options: Omit<SearchOptions, "format"> & { format: "geojson" }
  ): Promise<GeoJSONFeatureCollection>;
  async search(
    query: string,
    options: SearchOptions = {}
  ): Promise<GeocoderResult[] | GeoJSONFeatureCollection> {
    const params = new URLSearchParams({
      q: query,
      format: options.format || "jsonv2",
      limit: String(Math.min(Math.max(1, options.limit || 10), 40)),
    });

    const url = `${this.baseUrl}/search?${params}`;
    const response = await this.fetchWithRetry(url);
    const data = await response.json();

    if (options.format === "geojson") {
      return data as GeoJSONFeatureCollection;
    }

    return this.parseResults(data);
  }

  /**
   * Search and return results as GeoJSON FeatureCollection.
   */
  async searchGeoJSON(
    query: string,
    options: Omit<SearchOptions, "format"> = {}
  ): Promise<GeoJSONFeatureCollection> {
    const params = new URLSearchParams({
      q: query,
      format: "geojson",
      limit: String(Math.min(Math.max(1, options.limit || 10), 40)),
    });

    const url = `${this.baseUrl}/search?${params}`;
    const response = await this.fetchWithRetry(url);
    return response.json();
  }

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
   * The worker returns a single best-match object; this method normalizes
   * it into an array. Returns an empty array when nothing is found (404).
   *
   * NOTE: `format: "geojson"` is currently IGNORED by the deployed worker
   * for /reverse (a server-side fix is in flight); until then the geojson
   * overload may receive a plain JSON object instead of a FeatureCollection.
   *
   * @param lat Latitude
   * @param lon Longitude
   * @param options Reverse geocoding options
   * @param options.verifyGeometry If true, fetches full polygons from S3 and filters
   *                               to only results where point is inside the polygon.
   *                               Requires the optional @bradrichardson/overturemaps
   *                               peer dependency.
   */
  async reverse(
    lat: number,
    lon: number,
    options?: Omit<ReverseOptions, "format"> & { format?: "jsonv2" }
  ): Promise<ReverseGeocoderResult[]>;
  async reverse(
    lat: number,
    lon: number,
    options: Omit<ReverseOptions, "format"> & { format: "geojson" }
  ): Promise<GeoJSONFeatureCollection>;
  async reverse(
    lat: number,
    lon: number,
    options: ReverseOptions = {}
  ): Promise<ReverseGeocoderResult[] | GeoJSONFeatureCollection> {
    const params = new URLSearchParams({
      lat: String(lat),
      lon: String(lon),
      format: options.format || "jsonv2",
    });

    const url = `${this.baseUrl}/reverse?${params}`;
    let response: Response;
    try {
      response = await this.fetchWithRetry(url);
    } catch (error) {
      // The worker returns 404 when no division contains the point.
      if (
        error instanceof GeocoderError &&
        error.status === 404 &&
        options.format !== "geojson"
      ) {
        return [];
      }
      throw error;
    }
    const data = await response.json();

    if (options.format === "geojson") {
      return data as GeoJSONFeatureCollection;
    }

    let results = this.parseReverseResults(data);

    // Optional: verify geometry using point-in-polygon
    if (options.verifyGeometry) {
      const verifyLimit = options.verifyLimit ?? 10;
      const toVerify = results.slice(0, verifyLimit);
      const verified = await this.verifyResultsGeometry(toVerify, lat, lon);
      // Keep verified results + any remaining unverified (beyond limit)
      results = [...verified, ...results.slice(verifyLimit)];
    }

    return results;
  }

  /**
   * Verify which reverse geocode results actually contain the point.
   * Fetches full geometry from S3 and performs point-in-polygon checks.
   * Updates confidence to "high" for verified results.
   */
  private async verifyResultsGeometry(
    results: ReverseGeocoderResult[],
    lat: number,
    lon: number
  ): Promise<ReverseGeocoderResult[]> {
    const verified: ReverseGeocoderResult[] = [];

    // Fetch geometries in parallel
    const geometryPromises = results.map(async (result) => {
      try {
        const contains = await this.verifyContainsPoint(result.gers_id, lat, lon);
        return { result, contains, verified: true };
      } catch {
        // If geometry fetch fails, keep result with original bbox confidence
        return { result, contains: false, verified: false };
      }
    });

    const checks = await Promise.all(geometryPromises);

    for (const { result, contains, verified: wasVerified } of checks) {
      if (contains) {
        verified.push({
          ...result,
          confidence: "high", // Upgraded: point verified inside polygon
        });
      } else if (!wasVerified) {
        // Geometry fetch failed, keep original result with original confidence
        verified.push(result);
      }
      // If verified but not contains, exclude from results (false positive)
    }

    return verified;
  }

  /**
   * Reverse geocode and return results as GeoJSON FeatureCollection.
   *
   * NOTE: requires an updated worker — the currently deployed worker
   * ignores `format=geojson` on /reverse and returns a plain JSON object
   * (a server-side fix is in flight).
   */
  async reverseGeoJSON(
    lat: number,
    lon: number
  ): Promise<GeoJSONFeatureCollection> {
    const params = new URLSearchParams({
      lat: String(lat),
      lon: String(lon),
      format: "geojson",
    });

    const url = `${this.baseUrl}/reverse?${params}`;
    const response = await this.fetchWithRetry(url);
    return response.json();
  }

  /**
   * Verify if a point is inside a division's polygon.
   *
   * Fetches the full geometry from Overture S3 and performs
   * a point-in-polygon check using ray casting algorithm.
   */
  async verifyContainsPoint(
    gersId: string,
    lat: number,
    lon: number
  ): Promise<boolean> {
    const feature = await this.getFullGeometry(gersId);
    if (!feature) return false;

    const geometry = feature.geometry;
    if (geometry.type === "Polygon") {
      return this.pointInPolygon([lon, lat], geometry.coordinates[0]);
    }
    if (geometry.type === "MultiPolygon") {
      return geometry.coordinates.some((poly) =>
        this.pointInPolygon([lon, lat], poly[0])
      );
    }

    return false;
  }

  /**
   * Get the base URL configured for this client.
   */
  getBaseUrl(): string {
    return this.baseUrl;
  }

  /**
   * Look up a GERS ID's bounding box via the worker's parquet ID index.
   *
   * @param gersId The GERS ID (UUID) to look up
   * @returns The lookup result, or null if the ID is unknown (404).
   * @throws GeocoderError with status 503 when the ID index is unavailable.
   */
  async lookupId(gersId: string): Promise<IdLookupResult | null> {
    const url = `${this.baseUrl}/id/${encodeURIComponent(gersId)}`;
    try {
      const response = await this.fetchWithRetry(url);
      return (await response.json()) as IdLookupResult;
    } catch (error) {
      if (error instanceof GeocoderError && error.status === 404) {
        return null;
      }
      throw error;
    }
  }

  /**
   * Check the health of the geocoder service.
   *
   * Returns the health payload even when the service reports unhealthy
   * (non-2xx with a JSON body); throws on network/timeout failures.
   */
  async health(): Promise<HealthStatus> {
    const url = `${this.baseUrl}/health`;
    try {
      const response = await this.fetchWithRetry(url);
      return (await response.json()) as HealthStatus;
    } catch (error) {
      if (error instanceof GeocoderError && error.response) {
        try {
          return (await error.response.json()) as HealthStatus;
        } catch {
          // Body wasn't JSON; fall through to rethrow
        }
      }
      throw error;
    }
  }

  /**
   * Fetch full geometry for a GERS ID directly from Overture S3.
   *
   * Requires the optional @bradrichardson/overturemaps peer dependency.
   *
   * @param gersId The GERS ID to look up
   * @returns GeoJSON Feature with full geometry, or null if not found
   */
  async getFullGeometry(gersId: string): Promise<GeoJSONFeature | null> {
    const { getFeatureByGersId } = await this.loadOvertureMaps();
    const feature = await getFeatureByGersId(gersId);
    if (!feature) return null;

    return {
      type: "Feature",
      id: feature.id as string,
      properties: feature.properties,
      bbox: feature.bbox as [number, number, number, number] | undefined,
      geometry: feature.geometry as GeoJSONGeometry,
    };
  }

  /**
   * Close DuckDB connection and release resources.
   * Call this when done with geometry/place/address fetching to free memory.
   * No-op if the optional geometry module was never loaded.
   */
  async close(): Promise<void> {
    if (this.overtureMaps) {
      const { closeDb } = await this.overtureMaps;
      await closeDb();
    }
  }

  // ==========================================================================
  // Overture S3 Direct Query Methods (Places, Addresses)
  // ==========================================================================

  /**
   * Dynamically load the optional @bradrichardson/overturemaps peer
   * dependency. Throws a clear GeocoderError if it is not installed.
   */
  private loadOvertureMaps(): Promise<OvertureMapsModule> {
    if (!this.overtureMaps) {
      this.overtureMaps = import("@bradrichardson/overturemaps").then(
        (mod) => mod as unknown as OvertureMapsModule,
        (error: unknown) => {
          // Don't cache the failure; a later install/retry may succeed
          this.overtureMaps = undefined;
          throw new GeocoderError(
            `Optional dependency @bradrichardson/overturemaps is not available: ` +
              `${OVERTUREMAPS_INSTALL_HINT} ` +
              `(${error instanceof Error ? error.message : String(error)})`
          );
        }
      );
    }
    return this.overtureMaps;
  }

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
  async getNearbyPlaces(
    lat: number,
    lon: number,
    options: NearbySearchOptions = {}
  ): Promise<OverturePlace[]> {
    const radiusKm = options.radiusKm ?? 1;
    const limit = options.limit ?? 10;
    const category = options.category;

    // Convert km to approximate degrees for bounding box
    const bbox = this.radiusToBbox(lat, lon, radiusKm);

    const { readByBboxAll } = await this.loadOvertureMaps();

    try {
      // Fetch more than needed since we'll filter by exact distance
      const features = await readByBboxAll("place", bbox, { limit: limit * 3 });

      // Calculate distances and filter
      return features
        .filter((f) => f.geometry.type === "Point") // Only Point geometries
        .map((f) => {
          const props = f.properties as Record<string, unknown>;
          const coords = (f.geometry as { type: "Point"; coordinates: [number, number] }).coordinates;
          const fLon = coords[0];
          const fLat = coords[1];
          return {
            id: f.id as string,
            names: props.names as OverturePlace["names"],
            categories: props.categories as OverturePlace["categories"],
            addresses: props.addresses as OverturePlace["addresses"],
            phones: props.phones as string[],
            websites: props.websites as string[],
            brand: props.brand as OverturePlace["brand"],
            lat: fLat,
            lon: fLon,
            distance_km: this.haversineDistance(lat, lon, fLat, fLon),
            confidence: (props.confidence as number) ?? 0,
          };
        })
        .filter((p) => p.distance_km <= radiusKm)
        .filter((p) => !category || p.categories?.primary === category)
        .sort((a, b) => a.distance_km - b.distance_km)
        .slice(0, limit);
    } catch (error) {
      throw new GeocoderError(
        `Failed to query nearby places: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }

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
  async getNearbyAddresses(
    lat: number,
    lon: number,
    options: Omit<NearbySearchOptions, "category"> = {}
  ): Promise<OvertureAddress[]> {
    const radiusKm = options.radiusKm ?? 0.5;
    const limit = options.limit ?? 10;

    // Convert km to approximate degrees for bounding box
    const bbox = this.radiusToBbox(lat, lon, radiusKm);

    const { readByBboxAll } = await this.loadOvertureMaps();

    try {
      // Fetch more than needed since we'll filter by exact distance
      const features = await readByBboxAll("address", bbox, { limit: limit * 3 });

      // Calculate distances and filter
      return features
        .filter((f) => f.geometry.type === "Point") // Only Point geometries
        .map((f) => {
          const props = f.properties as Record<string, unknown>;
          const coords = (f.geometry as { type: "Point"; coordinates: [number, number] }).coordinates;
          const fLon = coords[0];
          const fLat = coords[1];
          return {
            id: f.id as string,
            number: props.number as string | undefined,
            street: props.street as string | undefined,
            unit: props.unit as string | undefined,
            postcode: props.postcode as string | undefined,
            freeform: props.freeform as string | undefined,
            lat: fLat,
            lon: fLon,
            distance_km: this.haversineDistance(lat, lon, fLat, fLon),
          };
        })
        .filter((a) => a.distance_km <= radiusKm)
        .sort((a, b) => a.distance_km - b.distance_km)
        .slice(0, limit);
    } catch (error) {
      throw new GeocoderError(
        `Failed to query nearby addresses: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }

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
  async reverseAndRefine(
    lat: number,
    lon: number,
    options: ReverseAndRefineOptions = {}
  ): Promise<RefinedReverseResult> {
    const {
      verifyGeometry = true,
      includePlaces = true,
      includeAddresses = true,
      radiusKm = 0.5,
      nearbyLimit = 5,
      placeCategory,
    } = options;

    // Run all queries in parallel
    const [divisions, places, addresses] = await Promise.all([
      this.reverse(lat, lon, { verifyGeometry }),
      includePlaces
        ? this.getNearbyPlaces(lat, lon, {
            radiusKm,
            limit: nearbyLimit,
            category: placeCategory,
          })
        : Promise.resolve(undefined),
      includeAddresses
        ? this.getNearbyAddresses(lat, lon, {
            radiusKm,
            limit: nearbyLimit,
          })
        : Promise.resolve(undefined),
    ]);

    return {
      divisions,
      places,
      addresses,
    };
  }

  // ==========================================================================
  // Private methods
  // ==========================================================================

  private async fetchWithRetry(url: string, attempt = 0): Promise<Response> {
    try {
      const response = await this.doFetch(url);

      if (!response.ok) {
        // 429: rate limited - honor Retry-After when retries remain
        if (response.status === 429 && attempt < this.retries) {
          await this.delay(this.retryAfterDelay(response, attempt));
          return this.fetchWithRetry(url, attempt + 1);
        }

        // Don't retry other client errors (4xx)
        if (response.status >= 400 && response.status < 500) {
          throw new GeocoderError(
            `Request failed: ${response.status} ${response.statusText}`,
            response.status,
            response
          );
        }

        // Retry server errors (5xx) with exponential backoff + jitter
        if (attempt < this.retries) {
          await this.delay(this.backoffDelay(attempt));
          return this.fetchWithRetry(url, attempt + 1);
        }

        throw new GeocoderError(
          `Request failed after ${attempt + 1} attempts: ${response.status} ${response.statusText}`,
          response.status,
          response
        );
      }

      return response;
    } catch (error) {
      if (error instanceof GeocoderError) throw error;

      // Handle timeout and network errors with exponential backoff + jitter
      if (error instanceof Error) {
        if (error.name === "AbortError") {
          if (attempt < this.retries) {
            await this.delay(this.backoffDelay(attempt));
            return this.fetchWithRetry(url, attempt + 1);
          }
          throw new GeocoderTimeoutError(
            `Request timed out after ${this.timeout}ms (${attempt + 1} attempts)`
          );
        }

        if (attempt < this.retries) {
          await this.delay(this.backoffDelay(attempt));
          return this.fetchWithRetry(url, attempt + 1);
        }

        throw new GeocoderNetworkError(
          `Network error after ${attempt + 1} attempts: ${error.message}`,
          error
        );
      }

      throw error;
    }
  }

  /**
   * Exponential backoff with jitter: retryDelay * 2^attempt, capped at
   * MAX_RETRY_DELAY, with the final delay jittered to 50-100% of the base.
   */
  private backoffDelay(attempt: number): number {
    const base = Math.min(this.retryDelay * 2 ** attempt, MAX_RETRY_DELAY);
    return base / 2 + Math.random() * (base / 2);
  }

  /**
   * Delay for a 429 response: honor the Retry-After header (seconds or
   * HTTP-date), capped at MAX_RETRY_DELAY. Falls back to exponential
   * backoff when the header is missing or unparseable.
   */
  private retryAfterDelay(response: Response, attempt: number): number {
    const header = response.headers?.get?.("Retry-After");
    if (header) {
      const seconds = Number(header);
      if (Number.isFinite(seconds) && seconds >= 0) {
        return Math.min(seconds * 1000, MAX_RETRY_DELAY);
      }
      const date = Date.parse(header);
      if (!Number.isNaN(date)) {
        return Math.min(Math.max(date - Date.now(), 0), MAX_RETRY_DELAY);
      }
    }
    return this.backoffDelay(attempt);
  }

  private async doFetch(url: string): Promise<Response> {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this.timeout);

    try {
      let init: RequestInit = {
        method: "GET",
        headers: {
          Accept: "application/json",
          ...this.headers,
        },
        signal: controller.signal,
      };

      // Apply request interceptor
      if (this.onRequest) {
        init = await this.onRequest(url, init);
      }

      let response = await this.fetchFn(url, init);

      // Apply response interceptor
      if (this.onResponse) {
        response = await this.onResponse(response);
      }

      return response;
    } finally {
      clearTimeout(timeoutId);
    }
  }

  private parseResults(data: unknown): GeocoderResult[] {
    // Handle wrapped response format { results: [...] }
    const results = (data as Record<string, unknown>)?.results;
    const items = Array.isArray(results) ? results : Array.isArray(data) ? data : [];

    return items.map((r) => {
      const record = r as Record<string, unknown>;
      // Handle both 'name' (API) and 'primary_name' (legacy) field names
      const name = (record.name as string) || (record.primary_name as string);
      // Handle both 'bbox' (API) and 'boundingbox' (legacy) field names
      const bbox = (record.bbox as [number, number, number, number]) ||
        (record.boundingbox as [number, number, number, number]);
      const result: GeocoderResult = {
        gers_id: record.gers_id as string,
        primary_name: name,
        lat: record.lat as number,
        lon: record.lon as number,
        boundingbox: bbox,
        importance: (record.importance as number) || 0,
        type: (record.division_type as string) || (record.type as string) || "unknown",
      };
      if (typeof record.country === "string") result.country = record.country;
      if (typeof record.region === "string") result.region = record.region;
      return result;
    });
  }

  private parseReverseResults(data: unknown): ReverseGeocoderResult[] {
    // Handle single result object (API returns single result, not array)
    const items = Array.isArray(data) ? data : data && typeof data === 'object' ? [data] : [];

    return items.map((r) => {
      const record = r as Record<string, unknown>;
      return {
        gers_id: record.gers_id as string,
        primary_name: record.primary_name as string,
        subtype: record.subtype as string,
        lat: record.lat as number,
        lon: record.lon as number,
        boundingbox: record.boundingbox as [number, number, number, number],
        distance_km: record.distance_km as number,
        confidence: record.confidence as Confidence,
        hierarchy: (record.hierarchy as HierarchyEntry[] | undefined) ?? [],
      };
    });
  }

  private pointInPolygon(
    point: [number, number],
    ring: [number, number][]
  ): boolean {
    let inside = false;
    const [x, y] = point;

    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i];
      const [xj, yj] = ring[j];

      if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
        inside = !inside;
      }
    }

    return inside;
  }

  private delay(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  /**
   * Convert a radius in km to a bounding box centered on lat/lon
   */
  private radiusToBbox(lat: number, lon: number, radiusKm: number): BoundingBox {
    // 1 degree latitude ≈ 111 km, longitude varies by latitude
    const latDelta = radiusKm / 111;
    const lonDelta = radiusKm / (111 * Math.cos((lat * Math.PI) / 180));

    return {
      xmin: lon - lonDelta,
      ymin: lat - latDelta,
      xmax: lon + lonDelta,
      ymax: lat + latDelta,
    };
  }

  /**
   * Calculate Haversine distance between two points in km
   */
  private haversineDistance(
    lat1: number,
    lon1: number,
    lat2: number,
    lon2: number
  ): number {
    const R = 6371; // Earth's radius in km
    const dLat = ((lat2 - lat1) * Math.PI) / 180;
    const dLon = ((lon2 - lon1) * Math.PI) / 180;
    const a =
      Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos((lat1 * Math.PI) / 180) *
        Math.cos((lat2 * Math.PI) / 180) *
        Math.sin(dLon / 2) *
        Math.sin(dLon / 2);
    const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
    return R * c;
  }
}

// ============================================================================
// Convenience functions
// ============================================================================

/**
 * Quick geocode function using default settings.
 */
export async function geocode(
  query: string,
  options?: Omit<SearchOptions, "format"> & { format?: "json" | "jsonv2" }
): Promise<GeocoderResult[]> {
  const client = new OvertureGeocoder();
  return client.search(query, options);
}

/**
 * Quick reverse geocode function using default settings.
 */
export async function reverseGeocode(
  lat: number,
  lon: number,
  options?: Omit<ReverseOptions, "format"> & { format?: "jsonv2" }
): Promise<ReverseGeocoderResult[]> {
  const client = new OvertureGeocoder();
  return client.reverse(lat, lon, options);
}

export default OvertureGeocoder;
