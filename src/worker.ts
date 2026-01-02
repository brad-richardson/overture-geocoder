/**
 * Overture Geocoder - Cloudflare Worker
 *
 * Forward geocoding API using Overture Maps data.
 */

export interface Env {
  // Global divisions database (forward geocoding)
  DB_DIVISIONS: D1Database;
  // Reverse geocoding database
  DB_DIVISIONS_REVERSE?: D1Database;
  // State-specific address databases (optional until created)
  DB_MA?: D1Database;
  // Add more states as needed:
  // DB_CA?: D1Database;
  // DB_TX?: D1Database;
}

interface GeocoderResult {
  gers_id: string;
  primary_name: string;
  lat: string;
  lon: string;
  boundingbox: string[];
  importance: number;
  type: string;
  address?: AddressDetails;
}

interface AddressDetails {
  house_number?: string;
  road?: string;
  city?: string;
  state?: string;
  postcode?: string;
  country?: string;
  country_code?: string;
}

interface FeatureRow {
  rowid: number;
  gers_id: string;
  type: string;
  primary_name: string;
  lat: number;
  lon: number;
  bbox_xmin: number;
  bbox_ymin: number;
  bbox_xmax: number;
  bbox_ymax: number;
  population?: number;
  city?: string;
  state?: string;
  postcode?: string;
  boosted_score: number;
}

interface DivisionRow {
  rowid: number;
  gers_id: string;
  type: string;
  primary_name: string;
  lat: number;
  lon: number;
  bbox_xmin: number;
  bbox_ymin: number;
  bbox_xmax: number;
  bbox_ymax: number;
  population?: number;
  country?: string;
  region?: string;
  boosted_score: number;
}

// Reverse geocoding types
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

interface HierarchyEntry {
  gers_id: string;
  subtype: string;
  name: string;
}

interface DivisionReverseRow {
  gers_id: string;
  subtype: string;
  primary_name: string;
  lat: number;
  lon: number;
  bbox_xmin: number;
  bbox_ymin: number;
  bbox_xmax: number;
  bbox_ymax: number;
  area: number;
  population: number | null;
  country: string | null;
  region: string | null;
  h3_cells: string | null;
}

/**
 * Get address database bindings based on query analysis.
 * For now, only MA is supported.
 */
function getAddressDatabases(env: Env, _query: string): D1Database[] {
  // TODO: Add state detection from query
  // For now, just return MA
  const dbs: D1Database[] = [];
  if (env.DB_MA) dbs.push(env.DB_MA);
  return dbs;
}

/**
 * Prepare FTS5 query from user input.
 * Handles common query patterns and escapes special characters.
 * @param query - User search query
 * @param autocomplete - If true, adds prefix wildcard to last token for autocomplete behavior
 */
function prepareFtsQuery(query: string, autocomplete: boolean = true): string {
  const tokens = query
    .toLowerCase()
    // Remove punctuation except hyphens and Unicode letters/numbers
    .replace(/[^\p{L}\p{N}\s-]/gu, " ")
    // Collapse whitespace
    .replace(/\s+/g, " ")
    .trim()
    // Split into tokens and filter empty
    .split(" ")
    .filter((t) => t.length > 0);

  if (tokens.length === 0) return "";

  // Quote each token; optionally add prefix wildcard to last token for autocomplete
  return tokens
    .map((t, i) =>
      autocomplete && i === tokens.length - 1 ? `"${t}"*` : `"${t}"`
    )
    .join(" ");
}

/**
 * Search global divisions database using FTS5.
 */
async function searchDivisions(
  db: D1Database | undefined,
  query: string,
  limit: number,
  autocomplete: boolean = true
): Promise<GeocoderResult[]> {
  if (!db) return [];

  const ftsQuery = prepareFtsQuery(query, autocomplete);
  if (!ftsQuery) return [];

  try {
    // Use population-based boosted ranking for divisions
    const stmt = db.prepare(`
      SELECT
        d.rowid,
        d.gers_id,
        d.type,
        d.primary_name,
        d.lat,
        d.lon,
        d.bbox_xmin,
        d.bbox_ymin,
        d.bbox_xmax,
        d.bbox_ymax,
        d.population,
        d.country,
        d.region,
        CASE
          WHEN d.population IS NOT NULL
          THEN bm25(divisions_fts) - (LOG(d.population + 1) * 2.0)
          ELSE bm25(divisions_fts) - 2.0
        END as boosted_score
      FROM divisions_fts
      JOIN divisions d ON divisions_fts.rowid = d.rowid
      WHERE divisions_fts MATCH ?
      ORDER BY boosted_score
      LIMIT ?
    `);

    const result = await stmt.bind(ftsQuery, limit).all<DivisionRow>();

    return (result.results || []).map((row) => ({
      gers_id: row.gers_id,
      primary_name: row.primary_name,
      lat: row.lat.toFixed(7),
      lon: row.lon.toFixed(7),
      boundingbox: [
        row.bbox_ymin.toFixed(7),
        row.bbox_ymax.toFixed(7),
        row.bbox_xmin.toFixed(7),
        row.bbox_xmax.toFixed(7),
      ],
      // boosted_score is negative (lower = better match + higher population)
      // Convert to 0-1 importance: more negative = higher importance
      importance: Math.max(0, Math.min(1, -row.boosted_score / 50)),
      type: row.type,
    }));
  } catch (e) {
    console.error("Division search error:", e);
    return [];
  }
}

/**
 * Search address databases using FTS5.
 */
async function searchAddresses(
  dbs: D1Database[],
  query: string,
  limit: number,
  addressdetails: boolean,
  autocomplete: boolean = true
): Promise<GeocoderResult[]> {
  const ftsQuery = prepareFtsQuery(query, autocomplete);
  if (!ftsQuery) return [];

  const results: GeocoderResult[] = [];

  // Query each database in parallel
  const promises = dbs.map(async (db) => {
    try {
      const stmt = db.prepare(`
        SELECT
          f.rowid,
          f.gers_id,
          f.type,
          f.primary_name,
          f.lat,
          f.lon,
          f.bbox_xmin,
          f.bbox_ymin,
          f.bbox_xmax,
          f.bbox_ymax,
          f.population,
          f.city,
          f.state,
          f.postcode,
          CASE
            WHEN f.type != 'address' AND f.population IS NOT NULL
            THEN bm25(features_fts) - (LOG(f.population + 1) * 2.0)
            WHEN f.type != 'address'
            THEN bm25(features_fts) - 2.0
            ELSE bm25(features_fts)
          END as boosted_score
        FROM features_fts
        JOIN features f ON features_fts.rowid = f.rowid
        WHERE features_fts MATCH ?
        ORDER BY boosted_score
        LIMIT ?
      `);

      return await stmt.bind(ftsQuery, limit).all<FeatureRow>();
    } catch (e) {
      console.error("Address search error:", e);
      return { results: [] };
    }
  });

  const allResults = await Promise.all(promises);

  // Merge results from all databases
  for (const result of allResults) {
    if (result.results) {
      for (const row of result.results) {
        const geocoderResult: GeocoderResult = {
          gers_id: row.gers_id,
          primary_name: row.primary_name,
          lat: row.lat.toFixed(7),
          lon: row.lon.toFixed(7),
          boundingbox: [
            row.bbox_ymin.toFixed(7),
            row.bbox_ymax.toFixed(7),
            row.bbox_xmin.toFixed(7),
            row.bbox_xmax.toFixed(7),
          ],
          // boosted_score is negative (lower = better match + higher population)
          // Convert to 0-1 importance: more negative = higher importance
          importance: Math.max(0, Math.min(1, -row.boosted_score / 50)),
          type: row.type,
        };

        if (addressdetails && row.type === "address") {
          const parts = row.primary_name.split(", ");
          geocoderResult.address = {
            road: parts[0]?.replace(/^\d+\s+/, "") || undefined,
            house_number: parts[0]?.match(/^(\d+)/)?.[1] || undefined,
            city: row.city || parts[1] || undefined,
            state: row.state || undefined,
            postcode: row.postcode || undefined,
            country: "United States",
            country_code: "us",
          };
        }

        results.push(geocoderResult);
      }
    }
  }

  return results;
}

/**
 * Haversine distance between two points in km.
 */
function haversineDistance(
  lat1: number,
  lon1: number,
  lat2: number,
  lon2: number
): number {
  const R = 6371; // Earth radius in km
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

/**
 * Build hierarchy from overlapping division candidates.
 * Candidates are sorted by type priority (most specific first).
 */
function buildHierarchy(candidates: DivisionReverseRow[]): HierarchyEntry[] {
  const typePriority: Record<string, number> = {
    neighborhood: 1,
    macrohood: 2,
    locality: 3,
    localadmin: 4,
    county: 5,
    region: 6,
    country: 7,
  };

  return [...candidates]
    .sort((a, b) => (typePriority[a.subtype] || 0) - (typePriority[b.subtype] || 0))
    .map((div) => ({
      gers_id: div.gers_id,
      subtype: div.subtype,
      name: div.primary_name,
    }));
}

/**
 * Handle /reverse endpoint.
 * Returns divisions containing or near the given coordinate.
 */
async function handleReverse(
  request: Request,
  env: Env
): Promise<Response> {
  const url = new URL(request.url);

  // Parse parameters
  const lat = parseFloat(url.searchParams.get("lat") || "");
  const lon = parseFloat(url.searchParams.get("lon") || "");
  const format = url.searchParams.get("format") || "jsonv2";

  // Validate coordinates
  if (isNaN(lat) || isNaN(lon)) {
    return jsonResponse({ error: "lat and lon parameters required" }, 400);
  }
  if (lat < -90 || lat > 90 || lon < -180 || lon > 180) {
    return jsonResponse({ error: "Invalid coordinates" }, 400);
  }

  const db = env.DB_DIVISIONS_REVERSE;
  if (!db) {
    return jsonResponse({ error: "Reverse geocoding not available" }, 503);
  }

  try {
    // Find divisions whose bbox contains the point, sorted by area (smallest first)
    const stmt = db.prepare(`
      SELECT
        gers_id, subtype, primary_name,
        lat, lon,
        bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
        area, population, country, region, h3_cells
      FROM divisions_reverse
      WHERE bbox_xmin <= ?
        AND bbox_xmax >= ?
        AND bbox_ymin <= ?
        AND bbox_ymax >= ?
      ORDER BY area ASC
      LIMIT 50
    `);

    const result = await stmt.bind(lon, lon, lat, lat).all<DivisionReverseRow>();
    const candidates = result.results || [];

    if (candidates.length === 0) {
      return jsonResponse([]);
    }

    // Build hierarchy from overlapping candidates (sorted by specificity)
    const hierarchy = buildHierarchy(candidates);

    // Format results
    const results: ReverseGeocoderResult[] = candidates.map((div) => ({
      gers_id: div.gers_id,
      primary_name: div.primary_name,
      subtype: div.subtype,
      lat: div.lat,
      lon: div.lon,
      boundingbox: [div.bbox_ymin, div.bbox_ymax, div.bbox_xmin, div.bbox_xmax],
      distance_km: Math.round(haversineDistance(lat, lon, div.lat, div.lon) * 100) / 100,
      confidence: "bbox" as const,
      hierarchy,
    }));

    if (format === "geojson") {
      return jsonResponse({
        type: "FeatureCollection",
        features: results.map((r) => ({
          type: "Feature",
          id: r.gers_id,
          properties: {
            gers_id: r.gers_id,
            primary_name: r.primary_name,
            subtype: r.subtype,
            distance_km: r.distance_km,
            confidence: r.confidence,
            hierarchy: r.hierarchy,
          },
          geometry: {
            type: "Point",
            coordinates: [r.lon, r.lat],
          },
        })),
      });
    }

    return jsonResponse(results);
  } catch (e) {
    console.error("Reverse geocoding error:", e);
    return jsonResponse({ error: "Internal error" }, 500);
  }
}

/**
 * Handle /search endpoint.
 */
async function handleSearch(
  request: Request,
  env: Env
): Promise<Response> {
  const url = new URL(request.url);

  // Parse query parameters
  const q = url.searchParams.get("q") || "";
  const format = url.searchParams.get("format") || "jsonv2";
  const limit = Math.min(
    Math.max(1, parseInt(url.searchParams.get("limit") || "10")),
    40
  );
  const addressdetails = url.searchParams.get("addressdetails") === "1";
  // autocomplete: default to 1 (enabled) for prefix matching on last token
  const autocomplete = url.searchParams.get("autocomplete") !== "0";
  // const countrycodes = url.searchParams.get("countrycodes");
  // const viewbox = url.searchParams.get("viewbox");
  // const bounded = url.searchParams.get("bounded") === "1";

  if (!q.trim()) {
    return jsonResponse([]);
  }

  // Search both divisions and addresses in parallel
  const addressDbs = getAddressDatabases(env, q);

  const [divisionResults, addressResults] = await Promise.all([
    searchDivisions(env.DB_DIVISIONS, q, limit, autocomplete),
    searchAddresses(addressDbs, q, limit, addressdetails, autocomplete),
  ]);

  // Merge results: divisions first (higher priority), then addresses
  // Sort by importance (boosted score), take top N
  const results = [...divisionResults, ...addressResults]
    .sort((a, b) => b.importance - a.importance)
    .slice(0, limit);

  // Handle different output formats
  if (format === "geojson") {
    return jsonResponse({
      type: "FeatureCollection",
      features: results.map((r) => ({
        type: "Feature",
        id: r.gers_id,
        properties: {
          gers_id: r.gers_id,
          primary_name: r.primary_name,
          importance: r.importance,
          ...r.address,
        },
        bbox: r.boundingbox.map(parseFloat),
        geometry: {
          type: "Point",
          coordinates: [parseFloat(r.lon), parseFloat(r.lat)],
        },
      })),
    });
  }

  return jsonResponse(results);
}

/**
 * Create JSON response with CORS headers.
 */
function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    },
  });
}

/**
 * Main request handler.
 */
export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // Handle CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type",
        },
      });
    }

    const url = new URL(request.url);

    switch (url.pathname) {
      case "/search":
        return handleSearch(request, env);

      case "/reverse":
        return handleReverse(request, env);

      case "/":
        return jsonResponse({
          name: "Overture Geocoder",
          version: "0.2.0",
          endpoints: {
            search: "/search?q={query}",
            reverse: "/reverse?lat={lat}&lon={lon}",
          },
          documentation: "https://github.com/bradrichardson/overture-geocode",
        });

      default:
        return jsonResponse({ error: "Not found" }, 404);
    }
  },
};
