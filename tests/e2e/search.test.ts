/**
 * E2E Tests for /search endpoint
 */

import { describe, it, expect, beforeAll } from 'vitest';
import { getBaseUrl } from './setup';

describe('/search endpoint', () => {
  let baseUrl: string;

  beforeAll(() => {
    baseUrl = getBaseUrl();
  });

  describe('division ranking', () => {
    it('should rank Boston city first for "boston" query', async () => {
      const response = await fetch(`${baseUrl}/search?q=boston&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);

      // First result should be Boston city (locality with population)
      expect(results[0].primary_name).toBe('Boston, MA');
      expect(results[0].type).toBe('locality');
    });

    it('should return Cambridge results for "cambridge" query', async () => {
      const response = await fetch(`${baseUrl}/search?q=cambridge&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);

      // First result should be a Cambridge locality (UK has higher pop than MA)
      expect(results[0].primary_name).toContain('Cambridge');
      expect(results[0].type).toBe('locality');
    });

    it('should rank Worcester County above Worcester city (higher population)', async () => {
      const response = await fetch(`${baseUrl}/search?q=worcester&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(1);

      // First should be county (pop ~862K), second should be city (pop ~206K)
      expect(results[0].type).toBe('county');
      expect(results[0].primary_name).toContain('Worcester');

      // Find the locality in results
      const city = results.find((r: { type: string }) => r.type === 'locality');
      expect(city).toBeDefined();
      expect(city.primary_name).toContain('Worcester');
    });
  });

  describe('division types', () => {
    it('should return various division types for "boston"', async () => {
      const response = await fetch(`${baseUrl}/search?q=boston&limit=10`);
      const results = await response.json();

      const types = new Set(results.map((r: { type: string }) => r.type));

      // Should have at least locality and macrohood types
      expect(types.has('locality')).toBe(true);
      expect(types.has('macrohood')).toBe(true);
    });

    it('should include type field on all results', async () => {
      const response = await fetch(`${baseUrl}/search?q=boston`);
      const results = await response.json();

      for (const result of results) {
        expect(result.type).toBeDefined();
        expect(typeof result.type).toBe('string');
      }
    });
  });

  // Address search tests are skipped until DB_MA is deployed
  describe.skip('address search', () => {
    it('should return address type for address queries', async () => {
      const response = await fetch(`${baseUrl}/search?q=123+main&limit=10`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);

      // Should have address type results
      const addresses = results.filter((r: { type: string }) => r.type === 'address');
      expect(addresses.length).toBeGreaterThan(0);
    });

    it('should have proper primary_name format for addresses', async () => {
      const response = await fetch(`${baseUrl}/search?q=123+main&limit=5`);
      const results = await response.json();

      const addresses = results.filter((r: { type: string }) => r.type === 'address');
      expect(addresses.length).toBeGreaterThan(0);

      // Address primary_name should contain street, city, state, postcode
      for (const addr of addresses) {
        expect(addr.primary_name).toMatch(/\d+.*,.*,.*MA.*\d{5}/);
      }
    });
  });

  describe('response format', () => {
    it('should include all required fields', async () => {
      const response = await fetch(`${baseUrl}/search?q=boston&limit=1`);
      const results = await response.json();

      expect(results.length).toBe(1);
      const result = results[0];

      // Required fields
      expect(result.gers_id).toBeDefined();
      expect(result.primary_name).toBeDefined();
      expect(result.lat).toBeDefined();
      expect(result.lon).toBeDefined();
      expect(result.boundingbox).toBeDefined();
      expect(result.importance).toBeDefined();
      expect(result.type).toBeDefined();
    });

    it('should have lat/lon as strings with 7 decimal places', async () => {
      const response = await fetch(`${baseUrl}/search?q=boston&limit=1`);
      const results = await response.json();

      const result = results[0];
      expect(typeof result.lat).toBe('string');
      expect(typeof result.lon).toBe('string');

      // Should have 7 decimal places
      expect(result.lat).toMatch(/^-?\d+\.\d{7}$/);
      expect(result.lon).toMatch(/^-?\d+\.\d{7}$/);
    });

    it('should have boundingbox with 4 elements', async () => {
      const response = await fetch(`${baseUrl}/search?q=boston&limit=1`);
      const results = await response.json();

      const result = results[0];
      expect(Array.isArray(result.boundingbox)).toBe(true);
      expect(result.boundingbox.length).toBe(4);

      // All elements should be numeric strings
      for (const coord of result.boundingbox) {
        expect(typeof coord).toBe('string');
        expect(parseFloat(coord)).not.toBeNaN();
      }
    });

    it('should have importance between 0 and 1', async () => {
      const response = await fetch(`${baseUrl}/search?q=boston&limit=10`);
      const results = await response.json();

      for (const result of results) {
        expect(result.importance).toBeGreaterThanOrEqual(0);
        expect(result.importance).toBeLessThanOrEqual(1);
      }
    });
  });

  describe('limit parameter', () => {
    it('should respect limit parameter', async () => {
      const response = await fetch(`${baseUrl}/search?q=boston&limit=3`);
      const results = await response.json();

      expect(results.length).toBeLessThanOrEqual(3);
    });

    it('should cap limit at 40', async () => {
      const response = await fetch(`${baseUrl}/search?q=main&limit=100`);
      const results = await response.json();

      // Even with limit=100, should return at most 40
      expect(results.length).toBeLessThanOrEqual(40);
    });

    it('should default to 10 results', async () => {
      const response = await fetch(`${baseUrl}/search?q=main`);
      const results = await response.json();

      expect(results.length).toBeLessThanOrEqual(10);
    });
  });

  describe('GeoJSON format', () => {
    it('should return FeatureCollection for format=geojson', async () => {
      const response = await fetch(`${baseUrl}/search?q=boston&format=geojson&limit=3`);
      expect(response.ok).toBe(true);

      const geojson = await response.json();
      expect(geojson.type).toBe('FeatureCollection');
      expect(Array.isArray(geojson.features)).toBe(true);
    });

    it('should have proper Feature structure', async () => {
      const response = await fetch(`${baseUrl}/search?q=boston&format=geojson&limit=1`);
      const geojson = await response.json();

      expect(geojson.features.length).toBe(1);
      const feature = geojson.features[0];

      expect(feature.type).toBe('Feature');
      expect(feature.id).toBeDefined();
      expect(feature.properties).toBeDefined();
      expect(feature.geometry).toBeDefined();
      expect(feature.geometry.type).toBe('Point');
      expect(Array.isArray(feature.geometry.coordinates)).toBe(true);
      expect(feature.geometry.coordinates.length).toBe(2);
    });
  });

  describe('empty query', () => {
    it('should return empty array for empty query', async () => {
      const response = await fetch(`${baseUrl}/search?q=`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results).toEqual([]);
    });
  });

  describe('no results', () => {
    it('should return empty array for query with no matches', async () => {
      const response = await fetch(`${baseUrl}/search?q=xyznonexistent12345`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results).toEqual([]);
    });
  });

  describe('compound queries with state/region', () => {
    it('should find divisions with state abbreviation qualifier', async () => {
      const response = await fetch(`${baseUrl}/search?q=Boston%2C+MA&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toContain('Boston');
    });

    it('should find divisions with state abbreviation (no comma)', async () => {
      const response = await fetch(`${baseUrl}/search?q=Boston+MA&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toContain('Boston');
    });

    it('should find divisions with full state name', async () => {
      const response = await fetch(`${baseUrl}/search?q=Boston+Massachusetts&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toContain('Boston');
    });

    it('should find divisions with country qualifier', async () => {
      const response = await fetch(`${baseUrl}/search?q=Boston+US&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toContain('Boston');
    });
  });

  describe('common query patterns - international cities', () => {
    it('should find Paris for "paris" query', async () => {
      const response = await fetch(`${baseUrl}/search?q=paris&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toBe('Paris');
    });

    it('should find Paris for "paris france" query', async () => {
      const response = await fetch(`${baseUrl}/search?q=paris+france&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toBe('Paris');
    });

    it('should find London for "london" query', async () => {
      const response = await fetch(`${baseUrl}/search?q=london&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toBe('London');
    });

    it('should find London for "london uk" query', async () => {
      const response = await fetch(`${baseUrl}/search?q=london+uk&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toBe('London');
    });

    it('should find Tokyo for "tokyo" query', async () => {
      const response = await fetch(`${baseUrl}/search?q=tokyo&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toBe('Tokyo');
    });

    it('should find Tokyo for "tokyo japan" query', async () => {
      const response = await fetch(`${baseUrl}/search?q=tokyo+japan&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toBe('Tokyo');
    });
  });

  describe('common query patterns - alternate names', () => {
    it('should find NYC for "nyc" query', async () => {
      const response = await fetch(`${baseUrl}/search?q=nyc&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toContain('New York');
    });

    it('should find NYC for "new york city" query', async () => {
      const response = await fetch(`${baseUrl}/search?q=new+york+city&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toContain('New York');
    });
  });

  describe('common query patterns - disambiguation', () => {
    it('should find Cambridge MA first for "cambridge ma" query', async () => {
      const response = await fetch(`${baseUrl}/search?q=cambridge+ma&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      // Should prefer Cambridge MA over Cambridge UK when "ma" is specified
      expect(results[0].primary_name).toBe('Cambridge, MA');
    });

    it('should find Cambridge UK for "cambridge uk" query', async () => {
      const response = await fetch(`${baseUrl}/search?q=cambridge+uk&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      // Should prefer Cambridge UK when "uk" is specified
      expect(results[0].primary_name).toBe('Cambridge');
      expect(results[0].gers_id).toBe('cambridge-uk-001');
    });
  });

  describe('common query patterns - case insensitivity', () => {
    it('should find Boston for "BOSTON" query (uppercase)', async () => {
      const response = await fetch(`${baseUrl}/search?q=BOSTON&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toBe('Boston, MA');
    });

    it('should find Paris for "PARIS FRANCE" query (uppercase)', async () => {
      const response = await fetch(`${baseUrl}/search?q=PARIS+FRANCE&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toBe('Paris');
    });
  });

  describe('common query patterns - prefix/partial matches', () => {
    it('should find Boston for "bost" prefix query', async () => {
      const response = await fetch(`${baseUrl}/search?q=bost&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toContain('Boston');
    });

    it('should find Paris for "par" prefix query', async () => {
      const response = await fetch(`${baseUrl}/search?q=par&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toBe('Paris');
    });

    it('should find NYC for "new yor" prefix query', async () => {
      const response = await fetch(`${baseUrl}/search?q=new+yor&limit=5`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBeGreaterThan(0);
      expect(results[0].primary_name).toContain('New York');
    });
  });
});
