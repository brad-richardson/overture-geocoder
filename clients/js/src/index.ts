/**
 * Overture Geocoder JavaScript/TypeScript Client
 *
 * Forward geocoder using Overture Maps data with Nominatim-compatible API.
 */

import {
  getStacCatalog,
  findRegistryFile,
  getRegistryS3Path,
  getDataS3Path,
  type StacCatalog,
  type GersRegistry,
} from "./stac";

// Re-export STAC utilities for advanced usage
export { getStacCatalog, findRegistryFile, getLatestRelease, clearCatalogCache } from "./stac";

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
  /** Overture Maps release version for geometry fetching */
  overtureRelease?: string;
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

  // DuckDB-WASM for geometry fetching (lazy loaded)
  private duckdb: unknown = null;
  private duckdbConn: unknown = null;
  private duckdbInitPromise: Promise<unknown> | null = null;

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

  /**
   * Fetch full geometry for a GERS ID directly from Overture S3 via DuckDB-WASM.
   *
   * Uses the STAC catalog's GERS registry for efficient lookup:
   * 1. Binary search manifest to find registry file
   * 2. Query registry for filepath + bbox (predicate pushdown)
   * 3. Query actual geometry from the specific parquet file
   *
   * Note: Requires @duckdb/duckdb-wasm package (~15MB, lazy loaded on first call):
   *   npm install @duckdb/duckdb-wasm
   *
   * @param gersId The GERS ID to look up
   * @returns GeoJSON Feature with full geometry, or null if not found
   */
  async getFullGeometry(gersId: string): Promise<GeoJSONFeature | null> {
    // Step 1: Get STAC catalog and find registry file
    const catalog = await getStacCatalog(this.fetchFn);

    if (!catalog.registry) {
      throw new GeocoderError("GERS registry not found in STAC catalog");
    }

    const registryFile = findRegistryFile(catalog.registry.manifest, gersId);
    if (!registryFile) {
      return null; // GERS ID not in any registry file
    }

    // Step 2: Initialize DuckDB-WASM (lazy load)
    const conn = await this.initDuckDB();

    // Step 3: Query registry for filepath + bbox
    const registryPath = getRegistryS3Path(catalog.registry, registryFile);

    const registryResult = await this.queryDuckDB(conn, `
      SELECT filepath, bbox
      FROM read_parquet('${registryPath}')
      WHERE id = '${gersId}'
      LIMIT 1
    `);

    if (registryResult.length === 0) {
      return null;
    }

    const registryRow = registryResult[0];
    const filepath = registryRow.filepath as string | null;
    const bbox = registryRow.bbox as { xmin: number; ymin: number; xmax: number; ymax: number } | null;

    if (!filepath) {
      return null; // NULL filepath means deleted in this release
    }

    // Step 4: Query actual geometry + names from the specific data file
    const dataPath = getDataS3Path(filepath);

    const geometryResult = await this.queryDuckDB(conn, `
      SELECT id, ST_AsGeoJSON(geometry) as geojson, names
      FROM read_parquet('${dataPath}')
      WHERE id = '${gersId}'
      LIMIT 1
    `);

    if (geometryResult.length === 0) {
      return null;
    }

    const row = geometryResult[0];
    const geometry = JSON.parse(row.geojson as string) as GeoJSONGeometry;

    return {
      type: "Feature",
      id: gersId,
      properties: {
        names: row.names as Record<string, unknown> | null,
      },
      bbox: bbox ? [bbox.xmin, bbox.ymin, bbox.xmax, bbox.ymax] : undefined,
      geometry,
    };
  }

  /**
   * Close DuckDB connection and release resources.
   * Call this when done with geometry fetching to free memory.
   */
  async close(): Promise<void> {
    if (this.duckdbConn) {
      const conn = this.duckdbConn as { close: () => Promise<void> };
      await conn.close();
      this.duckdbConn = null;
    }
    if (this.duckdb) {
      const db = this.duckdb as { terminate: () => Promise<void> };
      await db.terminate();
      this.duckdb = null;
    }
    this.duckdbInitPromise = null;
  }

  // ==========================================================================
  // Private methods
  // ==========================================================================

  /**
   * Initialize DuckDB-WASM with httpfs extension.
   * Lazy loaded on first geometry fetch call.
   */
  private async initDuckDB(): Promise<unknown> {
    // Return existing connection if available
    if (this.duckdbConn) {
      return this.duckdbConn;
    }

    // If initialization is in progress, wait for it
    if (this.duckdbInitPromise) {
      return this.duckdbInitPromise;
    }

    // Start initialization
    this.duckdbInitPromise = this.doInitDuckDB();
    return this.duckdbInitPromise;
  }

  private async doInitDuckDB(): Promise<unknown> {
    try {
      // Dynamic import to avoid bundling if not used
      const duckdb = await import("@duckdb/duckdb-wasm");

      // Select the best bundle for this environment
      const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());

      // Create worker and database
      const worker = new Worker(bundle.mainWorker!);
      const logger = new duckdb.ConsoleLogger(duckdb.LogLevel.WARNING);
      const db = new duckdb.AsyncDuckDB(logger, worker);

      await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
      this.duckdb = db;

      // Create connection
      const conn = await db.connect();
      this.duckdbConn = conn;

      // Install and load httpfs for S3 access
      await conn.query("INSTALL httpfs;");
      await conn.query("LOAD httpfs;");
      await conn.query("SET s3_region = 'us-west-2';");

      // Install spatial extension for ST_AsGeoJSON
      await conn.query("INSTALL spatial;");
      await conn.query("LOAD spatial;");

      return conn;
    } catch (error) {
      this.duckdbInitPromise = null;
      if (error instanceof Error && error.message.includes("Cannot find module")) {
        throw new GeocoderError(
          "@duckdb/duckdb-wasm required for geometry fetching. " +
          "Install with: npm install @duckdb/duckdb-wasm"
        );
      }
      throw error;
    }
  }

  private async queryDuckDB(
    conn: unknown,
    sql: string
  ): Promise<Record<string, unknown>[]> {
    const connection = conn as { query: (sql: string) => Promise<unknown> };
    const result = await connection.query(sql);
    const table = result as { toArray: () => Record<string, unknown>[] };
    return table.toArray();
  }

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

export default OvertureGeocoder;
