/**
 * Overture Geocoder JavaScript/TypeScript Client
 *
 * Forward geocoder using Overture Maps data with Nominatim-compatible API.
 */

// ============================================================================
// Types
// ============================================================================

export interface GeocoderResult {
  gers_id: string;
  display_name: string;
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

export interface LookupOptions {
  /** Response format */
  format?: "json" | "jsonv2" | "geojson";
}

export interface OvertureGeocoderConfig {
  /** API base URL (default: 'http://localhost:8787') */
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
  /** Overture Maps release version for geometry fetching */
  overtureRelease?: string;
}

export interface GeoJSONFeature {
  type: "Feature";
  id: string;
  properties: Record<string, unknown>;
  bbox?: [number, number, number, number];
  geometry: {
    type: "Point";
    coordinates: [number, number];
  };
}

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

const DEFAULT_BASE_URL = "http://localhost:8787";
const DEFAULT_TIMEOUT = 30000;
const DEFAULT_RETRIES = 0;
const DEFAULT_RETRY_DELAY = 1000;
const DEFAULT_OVERTURE_RELEASE = "2025-12-17.0";

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
  private readonly overtureRelease: string;

  constructor(config: OvertureGeocoderConfig = {}) {
    this.baseUrl = (config.baseUrl || DEFAULT_BASE_URL).replace(/\/$/, "");
    this.timeout = config.timeout ?? DEFAULT_TIMEOUT;
    this.retries = config.retries ?? DEFAULT_RETRIES;
    this.retryDelay = config.retryDelay ?? DEFAULT_RETRY_DELAY;
    this.headers = config.headers ?? {};
    this.fetchFn = config.fetch ?? globalThis.fetch;
    this.onRequest = config.onRequest;
    this.onResponse = config.onResponse;
    this.overtureRelease = config.overtureRelease ?? DEFAULT_OVERTURE_RELEASE;
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
   * Lookup features by their GERS IDs.
   */
  async lookup(
    gersIds: string | string[],
    options: LookupOptions = {}
  ): Promise<GeocoderResult[]> {
    const ids = Array.isArray(gersIds) ? gersIds : [gersIds];
    if (ids.length === 0) return [];

    const params = new URLSearchParams({
      gers_ids: ids.join(","),
      format: options.format || "jsonv2",
    });

    const url = `${this.baseUrl}/lookup?${params}`;
    const response = await this.fetchWithRetry(url);
    const data = await response.json();

    if (options.format === "geojson") {
      return data as unknown as GeocoderResult[];
    }

    return this.parseResults(data);
  }

  /**
   * Lookup features and return as GeoJSON FeatureCollection.
   */
  async lookupGeoJSON(gersIds: string | string[]): Promise<GeoJSONFeatureCollection> {
    const ids = Array.isArray(gersIds) ? gersIds : [gersIds];
    if (ids.length === 0) {
      return { type: "FeatureCollection", features: [] };
    }

    const params = new URLSearchParams({
      gers_ids: ids.join(","),
      format: "geojson",
    });

    const url = `${this.baseUrl}/lookup?${params}`;
    const response = await this.fetchWithRetry(url);
    return response.json();
  }

  /**
   * Get full geometry for a GERS ID.
   * Note: This returns point geometry from the API.
   * For full building/parcel geometries, use DuckDB with Overture S3 directly.
   */
  async getGeometry(gersId: string): Promise<GeoJSONFeature | null> {
    const result = await this.lookupGeoJSON(gersId);
    return result.features[0] || null;
  }

  /**
   * Get the Overture release version configured for this client.
   */
  getOvertureRelease(): string {
    return this.overtureRelease;
  }

  /**
   * Get the base URL configured for this client.
   */
  getBaseUrl(): string {
    return this.baseUrl;
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
        display_name: record.display_name as string,
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
 * Quick lookup function using default settings.
 */
export async function lookup(
  gersIds: string | string[],
  options?: LookupOptions
): Promise<GeocoderResult[]> {
  const client = new OvertureGeocoder();
  return client.lookup(gersIds, options);
}

export default OvertureGeocoder;
