/**
 * Overture Geocoder JavaScript/TypeScript Client
 *
 * Forward geocoder using Overture Maps data with Nominatim-compatible API.
 */

import { getFeatureByGersId, closeDb } from "@bradrichardson/overturemaps";

// Re-export STAC utilities from overturemaps for advanced usage
export {
  getStacCatalog,
  getLatestRelease,
  clearCache as clearCatalogCache,
} from "@bradrichardson/overturemaps";
export type { StacCatalog, StacLink } from "@bradrichardson/overturemaps";

// ============================================================================
// Types
// ============================================================================

export interface GeocoderResult {
  gers_id: string;
  primary_name: string;
  lat: number;
  lon: number;
  boundingbox: [number, number, number, number];
  importance: number;
  type?: string;
  address?: AddressDetails;
}

export interface AddressDetails {
  house_number?: string;
  road?: string;
  city?: string;
  state?: string;
  postcode?: string;
  country?: string;
  country_code?: string;
}

export interface SearchOptions {
  /** Maximum number of results (1-40, default: 10) */
  limit?: number;
  /** Comma-separated ISO 3166-1 alpha-2 country codes */
  countrycodes?: string;
  /** Bounding box [lon1, lat1, lon2, lat2] */
  viewbox?: [number, number, number, number];
  /** Restrict results to viewbox */
  bounded?: boolean;
  /** Include address breakdown in results */
  addressdetails?: boolean;
  /** Response format */
  format?: "json" | "jsonv2" | "geojson";
}

export interface ReverseOptions {
  /** Response format */
  format?: "jsonv2" | "geojson";
}

export interface HierarchyEntry {
  gers_id: string;
  subtype: string;
  name: string;
}

export interface ReverseGeocoderResult {
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


export interface OvertureGeocoderConfig {
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

const DEFAULT_BASE_URL = "https://overture-geocoder.bradr.workers.dev";
const DEFAULT_TIMEOUT = 30000;
const DEFAULT_RETRIES = 0;
const DEFAULT_RETRY_DELAY = 1000;

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
   * Search for addresses matching the query.
   */
  async search(query: string, options: SearchOptions = {}): Promise<GeocoderResult[]> {
    const params = new URLSearchParams({
      q: query,
      format: options.format || "jsonv2",
      limit: String(Math.min(Math.max(1, options.limit || 10), 40)),
    });

    if (options.countrycodes) params.set("countrycodes", options.countrycodes);
    if (options.viewbox) params.set("viewbox", options.viewbox.join(","));
    if (options.bounded) params.set("bounded", "1");
    if (options.addressdetails) params.set("addressdetails", "1");

    const url = `${this.baseUrl}/search?${params}`;
    const response = await this.fetchWithRetry(url);
    const data = await response.json();

    if (options.format === "geojson") {
      return data as unknown as GeocoderResult[];
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

    if (options.countrycodes) params.set("countrycodes", options.countrycodes);
    if (options.viewbox) params.set("viewbox", options.viewbox.join(","));
    if (options.bounded) params.set("bounded", "1");
    if (options.addressdetails) params.set("addressdetails", "1");

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
   */
  async reverse(
    lat: number,
    lon: number,
    options: ReverseOptions = {}
  ): Promise<ReverseGeocoderResult[]> {
    const params = new URLSearchParams({
      lat: String(lat),
      lon: String(lon),
      format: options.format || "jsonv2",
    });

    const url = `${this.baseUrl}/reverse?${params}`;
    const response = await this.fetchWithRetry(url);
    const data = await response.json();

    if (options.format === "geojson") {
      return data as unknown as ReverseGeocoderResult[];
    }

    return this.parseReverseResults(data);
  }

  /**
   * Reverse geocode and return results as GeoJSON FeatureCollection.
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
   * Fetch full geometry for a GERS ID directly from Overture S3.
   *
   * Uses the @bradrichardson/overturemaps package for efficient lookup.
   *
   * @param gersId The GERS ID to look up
   * @returns GeoJSON Feature with full geometry, or null if not found
   */
  async getFullGeometry(gersId: string): Promise<GeoJSONFeature | null> {
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
   * Call this when done with geometry fetching to free memory.
   */
  async close(): Promise<void> {
    await closeDb();
  }

  // ==========================================================================
  // Private methods
  // ==========================================================================

  private async fetchWithRetry(url: string, attempt = 0): Promise<Response> {
    try {
      const response = await this.doFetch(url);

      if (!response.ok) {
        // Don't retry client errors (4xx)
        if (response.status >= 400 && response.status < 500) {
          throw new GeocoderError(
            `Request failed: ${response.status} ${response.statusText}`,
            response.status,
            response
          );
        }

        // Retry server errors (5xx)
        if (attempt < this.retries) {
          await this.delay(this.retryDelay);
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

      // Handle timeout and network errors
      if (error instanceof Error) {
        if (error.name === "AbortError") {
          if (attempt < this.retries) {
            await this.delay(this.retryDelay);
            return this.fetchWithRetry(url, attempt + 1);
          }
          throw new GeocoderTimeoutError(
            `Request timed out after ${this.timeout}ms (${attempt + 1} attempts)`
          );
        }

        if (attempt < this.retries) {
          await this.delay(this.retryDelay);
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
    if (!Array.isArray(data)) return [];

    return data.map((r) => {
      const record = r as Record<string, unknown>;
      return {
        gers_id: record.gers_id as string,
        primary_name: record.primary_name as string,
        lat: parseFloat(record.lat as string),
        lon: parseFloat(record.lon as string),
        boundingbox: (record.boundingbox as string[]).map(parseFloat) as [
          number,
          number,
          number,
          number
        ],
        importance: (record.importance as number) || 0,
        type: record.type as string | undefined,
        address: record.address as AddressDetails | undefined,
      };
    });
  }

  private parseReverseResults(data: unknown): ReverseGeocoderResult[] {
    if (!Array.isArray(data)) return [];

    return data.map((r) => {
      const record = r as Record<string, unknown>;
      return {
        gers_id: record.gers_id as string,
        primary_name: record.primary_name as string,
        subtype: record.subtype as string,
        lat: record.lat as number,
        lon: record.lon as number,
        boundingbox: record.boundingbox as [number, number, number, number],
        distance_km: record.distance_km as number,
        confidence: record.confidence as "exact" | "bbox" | "approximate",
        hierarchy: record.hierarchy as HierarchyEntry[] | undefined,
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
}

// ============================================================================
// Convenience functions
// ============================================================================

/**
 * Quick geocode function using default settings.
 */
export async function geocode(
  query: string,
  options?: SearchOptions
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
  options?: ReverseOptions
): Promise<ReverseGeocoderResult[]> {
  const client = new OvertureGeocoder();
  return client.reverse(lat, lon, options);
}

export default OvertureGeocoder;
