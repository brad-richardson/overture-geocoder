import {
  closeDuckDB,
  isDuckDBAvailable,
  queryOverture
} from "./chunk-KYG7E6JB.mjs";
import "./chunk-OOIUSZB4.mjs";

// src/index.ts
import {
  getFeatureByGersId,
  closeDb
} from "@bradrichardson/overturemaps";
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
   * @param options.verifyGeometry If true, fetches full polygons from S3 and filters
   *                               to only results where point is inside the polygon
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
    let results = this.parseReverseResults(data);
    if (options.verifyGeometry) {
      const verifyLimit = options.verifyLimit ?? 10;
      const toVerify = results.slice(0, verifyLimit);
      const verified = await this.verifyResultsGeometry(toVerify, lat, lon);
      results = [...verified, ...results.slice(verifyLimit)];
    }
    return results;
  }
  /**
   * Verify which reverse geocode results actually contain the point.
   * Fetches full geometry from S3 and performs point-in-polygon checks.
   * Updates confidence to "exact" for verified results.
   */
  async verifyResultsGeometry(results, lat, lon) {
    const verified = [];
    const geometryPromises = results.map(async (result) => {
      try {
        const contains = await this.verifyContainsPoint(result.gers_id, lat, lon);
        return { result, contains };
      } catch {
        return { result, contains: true };
      }
    });
    const checks = await Promise.all(geometryPromises);
    for (const { result, contains } of checks) {
      if (contains) {
        verified.push({
          ...result,
          confidence: "exact"
          // Upgraded from bbox
        });
      }
    }
    return verified;
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
   * Close all DuckDB connections and release resources.
   * Call this when done with geometry/place/address fetching to free memory.
   */
  async close() {
    await Promise.all([closeDb(), closeDuckDB()]);
  }
  // ==========================================================================
  // Overture S3 Direct Query Methods (Places, Addresses)
  // ==========================================================================
  /**
   * Get nearby places from Overture S3 using DuckDB spatial query.
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
  async getNearbyPlaces(lat, lon, options = {}) {
    const radiusKm = options.radiusKm ?? 1;
    const limit = options.limit ?? 10;
    const category = options.category;
    const latDelta = radiusKm / 111;
    const lonDelta = radiusKm / (111 * Math.cos(lat * Math.PI / 180));
    const bboxFilter = `
      bbox.xmin <= ${lon + lonDelta} AND bbox.xmax >= ${lon - lonDelta} AND
      bbox.ymin <= ${lat + latDelta} AND bbox.ymax >= ${lat - latDelta}
    `;
    const categoryFilter = category ? `AND categories.primary = '${category.replace(/'/g, "''")}'` : "";
    const query = `
      SELECT
        id,
        names,
        categories,
        addresses,
        phones,
        websites,
        brand,
        ST_X(geometry) as lon,
        ST_Y(geometry) as lat,
        -- Haversine distance calculation
        6371 * 2 * ASIN(SQRT(
          POWER(SIN((RADIANS(ST_Y(geometry)) - RADIANS(${lat})) / 2), 2) +
          COS(RADIANS(${lat})) * COS(RADIANS(ST_Y(geometry))) *
          POWER(SIN((RADIANS(ST_X(geometry)) - RADIANS(${lon})) / 2), 2)
        )) as distance_km,
        confidence
      FROM read_parquet(
        's3://overturemaps-us-west-2/release/__LATEST__/theme=places/type=place/*',
        hive_partitioning = true
      )
      WHERE ${bboxFilter}
      ${categoryFilter}
      ORDER BY distance_km ASC
      LIMIT ${limit * 2}
    `;
    try {
      const rows = await queryOverture(query);
      return rows.filter((row) => row.distance_km <= radiusKm).slice(0, limit).map((row) => ({
        id: row.id,
        names: row.names,
        categories: row.categories,
        addresses: row.addresses,
        phones: row.phones,
        websites: row.websites,
        brand: row.brand,
        lat: row.lat,
        lon: row.lon,
        distance_km: row.distance_km,
        confidence: row.confidence
      }));
    } catch (error) {
      throw new GeocoderError(
        `Failed to query nearby places: ${error instanceof Error ? error.message : String(error)}`
      );
    }
  }
  /**
   * Get nearby addresses from Overture S3 using DuckDB spatial query.
   *
   * Queries the Overture addresses theme directly from S3 within a radius
   * of the given coordinates. Returns structured address components.
   *
   * @param lat Latitude of center point
   * @param lon Longitude of center point
   * @param options Search options (radius, limit)
   * @returns Array of nearby addresses sorted by distance
   */
  async getNearbyAddresses(lat, lon, options = {}) {
    const radiusKm = options.radiusKm ?? 0.5;
    const limit = options.limit ?? 10;
    const latDelta = radiusKm / 111;
    const lonDelta = radiusKm / (111 * Math.cos(lat * Math.PI / 180));
    const query = `
      SELECT
        id,
        number,
        street,
        unit,
        postcode,
        freeform,
        ST_X(geometry) as lon,
        ST_Y(geometry) as lat,
        -- Haversine distance calculation
        6371 * 2 * ASIN(SQRT(
          POWER(SIN((RADIANS(ST_Y(geometry)) - RADIANS(${lat})) / 2), 2) +
          COS(RADIANS(${lat})) * COS(RADIANS(ST_Y(geometry))) *
          POWER(SIN((RADIANS(ST_X(geometry)) - RADIANS(${lon})) / 2), 2)
        )) as distance_km
      FROM read_parquet(
        's3://overturemaps-us-west-2/release/__LATEST__/theme=addresses/type=address/*',
        hive_partitioning = true
      )
      WHERE bbox.xmin <= ${lon + lonDelta} AND bbox.xmax >= ${lon - lonDelta}
        AND bbox.ymin <= ${lat + latDelta} AND bbox.ymax >= ${lat - latDelta}
      ORDER BY distance_km ASC
      LIMIT ${limit * 2}
    `;
    try {
      const rows = await queryOverture(query);
      return rows.filter((row) => row.distance_km <= radiusKm).slice(0, limit).map((row) => ({
        id: row.id,
        number: row.number,
        street: row.street,
        unit: row.unit,
        postcode: row.postcode,
        freeform: row.freeform,
        lat: row.lat,
        lon: row.lon,
        distance_km: row.distance_km
      }));
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
  async reverseAndRefine(lat, lon, options = {}) {
    const {
      verifyGeometry = true,
      includePlaces = true,
      includeAddresses = true,
      radiusKm = 0.5,
      nearbyLimit = 5,
      placeCategory
    } = options;
    const [divisions, places, addresses] = await Promise.all([
      this.reverse(lat, lon, { verifyGeometry }),
      includePlaces ? this.getNearbyPlaces(lat, lon, {
        radiusKm,
        limit: nearbyLimit,
        category: placeCategory
      }) : Promise.resolve(void 0),
      includeAddresses ? this.getNearbyAddresses(lat, lon, {
        radiusKm,
        limit: nearbyLimit
      }) : Promise.resolve(void 0)
    ]);
    return {
      divisions,
      places: places ?? void 0,
      addresses: addresses ?? void 0
    };
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
        lat: record.lat,
        lon: record.lon,
        boundingbox: record.boundingbox,
        importance: record.importance || 0,
        type: record.type || "unknown"
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
  closeDuckDB,
  index_default as default,
  geocode,
  getLatestRelease,
  getStacCatalog,
  isDuckDBAvailable,
  queryOverture,
  reverseGeocode
};
