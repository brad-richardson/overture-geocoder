// src/index.ts
import { getFeatureByGersId, closeDb } from "@bradrichardson/overturemaps";
import {
  getStacCatalog,
  getLatestRelease,
  clearCache
} from "@bradrichardson/overturemaps";
var GeocoderError = class extends Error {
  constructor(message, status, response) {
    super(message);
    this.status = status;
    this.response = response;
    this.name = "GeocoderError";
  }
};
var GeocoderTimeoutError = class extends GeocoderError {
  constructor(message = "Request timed out") {
    super(message);
    this.name = "GeocoderTimeoutError";
  }
};
var GeocoderNetworkError = class extends GeocoderError {
  constructor(message, cause) {
    super(message);
    this.cause = cause;
    this.name = "GeocoderNetworkError";
  }
};
var DEFAULT_BASE_URL = "https://overture-geocoder.bradr.workers.dev";
var DEFAULT_TIMEOUT = 3e4;
var DEFAULT_RETRIES = 0;
var DEFAULT_RETRY_DELAY = 1e3;
var OvertureGeocoder = class {
  baseUrl;
  timeout;
  retries;
  retryDelay;
  headers;
  fetchFn;
  onRequest;
  onResponse;
  constructor(config = {}) {
    this.baseUrl = (config.baseUrl || DEFAULT_BASE_URL).replace(/\/$/, "");
    this.timeout = config.timeout ?? DEFAULT_TIMEOUT;
    this.retries = config.retries ?? DEFAULT_RETRIES;
    this.retryDelay = config.retryDelay ?? DEFAULT_RETRY_DELAY;
    this.headers = config.headers ?? {};
    this.fetchFn = config.fetch ?? globalThis.fetch.bind(globalThis);
    this.onRequest = config.onRequest;
    this.onResponse = config.onResponse;
  }
  /**
   * Search for addresses matching the query.
   */
  async search(query, options = {}) {
    const params = new URLSearchParams({
      q: query,
      format: options.format || "jsonv2",
      limit: String(Math.min(Math.max(1, options.limit || 10), 40))
    });
    if (options.countrycodes) params.set("countrycodes", options.countrycodes);
    if (options.viewbox) params.set("viewbox", options.viewbox.join(","));
    if (options.bounded) params.set("bounded", "1");
    if (options.addressdetails) params.set("addressdetails", "1");
    const url = `${this.baseUrl}/search?${params}`;
    const response = await this.fetchWithRetry(url);
    const data = await response.json();
    if (options.format === "geojson") {
      return data;
    }
    return this.parseResults(data);
  }
  /**
   * Search and return results as GeoJSON FeatureCollection.
   */
  async searchGeoJSON(query, options = {}) {
    const params = new URLSearchParams({
      q: query,
      format: "geojson",
      limit: String(Math.min(Math.max(1, options.limit || 10), 40))
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
  async reverse(lat, lon, options = {}) {
    const params = new URLSearchParams({
      lat: String(lat),
      lon: String(lon),
      format: options.format || "jsonv2"
    });
    const url = `${this.baseUrl}/reverse?${params}`;
    const response = await this.fetchWithRetry(url);
    const data = await response.json();
    if (options.format === "geojson") {
      return data;
    }
    return this.parseReverseResults(data);
  }
  /**
   * Reverse geocode and return results as GeoJSON FeatureCollection.
   */
  async reverseGeoJSON(lat, lon) {
    const params = new URLSearchParams({
      lat: String(lat),
      lon: String(lon),
      format: "geojson"
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
  async verifyContainsPoint(gersId, lat, lon) {
    const feature = await this.getFullGeometry(gersId);
    if (!feature) return false;
    const geometry = feature.geometry;
    if (geometry.type === "Polygon") {
      return this.pointInPolygon([lon, lat], geometry.coordinates[0]);
    }
    if (geometry.type === "MultiPolygon") {
      return geometry.coordinates.some(
        (poly) => this.pointInPolygon([lon, lat], poly[0])
      );
    }
    return false;
  }
  /**
   * Get the base URL configured for this client.
   */
  getBaseUrl() {
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
  async getFullGeometry(gersId) {
    const feature = await getFeatureByGersId(gersId);
    if (!feature) return null;
    return {
      type: "Feature",
      id: feature.id,
      properties: feature.properties,
      bbox: feature.bbox,
      geometry: feature.geometry
    };
  }
  /**
   * Close DuckDB connection and release resources.
   * Call this when done with geometry fetching to free memory.
   */
  async close() {
    await closeDb();
  }
  // ==========================================================================
  // Private methods
  // ==========================================================================
  async fetchWithRetry(url, attempt = 0) {
    try {
      const response = await this.doFetch(url);
      if (!response.ok) {
        if (response.status >= 400 && response.status < 500) {
          throw new GeocoderError(
            `Request failed: ${response.status} ${response.statusText}`,
            response.status,
            response
          );
        }
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
  async doFetch(url) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this.timeout);
    try {
      let init = {
        method: "GET",
        headers: {
          Accept: "application/json",
          ...this.headers
        },
        signal: controller.signal
      };
      if (this.onRequest) {
        init = await this.onRequest(url, init);
      }
      let response = await this.fetchFn(url, init);
      if (this.onResponse) {
        response = await this.onResponse(response);
      }
      return response;
    } finally {
      clearTimeout(timeoutId);
    }
  }
  parseResults(data) {
    if (!Array.isArray(data)) return [];
    return data.map((r) => {
      const record = r;
      return {
        gers_id: record.gers_id,
        primary_name: record.primary_name,
        lat: parseFloat(record.lat),
        lon: parseFloat(record.lon),
        boundingbox: record.boundingbox.map(parseFloat),
        importance: record.importance || 0,
        type: record.type,
        address: record.address
      };
    });
  }
  parseReverseResults(data) {
    if (!Array.isArray(data)) return [];
    return data.map((r) => {
      const record = r;
      return {
        gers_id: record.gers_id,
        primary_name: record.primary_name,
        subtype: record.subtype,
        lat: record.lat,
        lon: record.lon,
        boundingbox: record.boundingbox,
        distance_km: record.distance_km,
        confidence: record.confidence,
        hierarchy: record.hierarchy
      };
    });
  }
  pointInPolygon(point, ring) {
    let inside = false;
    const [x, y] = point;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i];
      const [xj, yj] = ring[j];
      if (yi > y !== yj > y && x < (xj - xi) * (y - yi) / (yj - yi) + xi) {
        inside = !inside;
      }
    }
    return inside;
  }
  delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
};
async function geocode(query, options) {
  const client = new OvertureGeocoder();
  return client.search(query, options);
}
async function reverseGeocode(lat, lon, options) {
  const client = new OvertureGeocoder();
  return client.reverse(lat, lon, options);
}
var index_default = OvertureGeocoder;
export {
  GeocoderError,
  GeocoderNetworkError,
  GeocoderTimeoutError,
  OvertureGeocoder,
  clearCache as clearCatalogCache,
  index_default as default,
  geocode,
  getLatestRelease,
  getStacCatalog,
  reverseGeocode
};
