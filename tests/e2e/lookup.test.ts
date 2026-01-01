/**
 * E2E Tests for /lookup endpoint
 */

import { describe, it, expect, beforeAll } from 'vitest';
import { getBaseUrl } from './setup';

// Known GERS IDs from the test fixture
const BOSTON_GERS_ID = '5df2793f-5a0a-4fcf-bd3c-7edb8cc495d8';
const CAMBRIDGE_GERS_ID = 'e66a9243-9cc5-40a8-a44e-363f9721113f';
const WORCESTER_COUNTY_GERS_ID = '5fc7bdb6-37b6-4759-94df-e94fef6571fa';

describe('/lookup endpoint', () => {
  let baseUrl: string;

  beforeAll(() => {
    baseUrl = getBaseUrl();
  });

  describe('single GERS ID lookup', () => {
    it('should return result for valid GERS ID', async () => {
      const response = await fetch(`${baseUrl}/lookup?gers_ids=${BOSTON_GERS_ID}`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBe(1);
      expect(results[0].gers_id).toBe(BOSTON_GERS_ID);
      expect(results[0].display_name).toBe('Boston, MA');
      expect(results[0].type).toBe('locality');
    });

    it('should return all required fields', async () => {
      const response = await fetch(`${baseUrl}/lookup?gers_ids=${BOSTON_GERS_ID}`);
      const results = await response.json();

      const result = results[0];
      expect(result.gers_id).toBeDefined();
      expect(result.display_name).toBeDefined();
      expect(result.lat).toBeDefined();
      expect(result.lon).toBeDefined();
      expect(result.boundingbox).toBeDefined();
      expect(result.importance).toBeDefined();
      expect(result.type).toBeDefined();
    });
  });

  describe('multiple GERS IDs', () => {
    it('should return multiple results for multiple IDs', async () => {
      const ids = [BOSTON_GERS_ID, CAMBRIDGE_GERS_ID].join(',');
      const response = await fetch(`${baseUrl}/lookup?gers_ids=${ids}`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBe(2);

      const gersIds = results.map((r: { gers_id: string }) => r.gers_id);
      expect(gersIds).toContain(BOSTON_GERS_ID);
      expect(gersIds).toContain(CAMBRIDGE_GERS_ID);
    });

    it('should return results for all valid IDs in a mixed request', async () => {
      const ids = [BOSTON_GERS_ID, WORCESTER_COUNTY_GERS_ID].join(',');
      const response = await fetch(`${baseUrl}/lookup?gers_ids=${ids}`);

      const results = await response.json();
      expect(results.length).toBe(2);
    });
  });

  describe('invalid GERS ID', () => {
    it('should return empty array for non-existent ID', async () => {
      const fakeId = '00000000-0000-0000-0000-000000000000';
      const response = await fetch(`${baseUrl}/lookup?gers_ids=${fakeId}`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results).toEqual([]);
    });

    it('should return only valid results when mixed with invalid IDs', async () => {
      const fakeId = '00000000-0000-0000-0000-000000000000';
      const ids = [BOSTON_GERS_ID, fakeId].join(',');
      const response = await fetch(`${baseUrl}/lookup?gers_ids=${ids}`);

      const results = await response.json();
      expect(results.length).toBe(1);
      expect(results[0].gers_id).toBe(BOSTON_GERS_ID);
    });
  });

  describe('empty request', () => {
    it('should return empty array for empty gers_ids', async () => {
      const response = await fetch(`${baseUrl}/lookup?gers_ids=`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results).toEqual([]);
    });

    it('should return empty array for missing gers_ids parameter', async () => {
      const response = await fetch(`${baseUrl}/lookup`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results).toEqual([]);
    });
  });

  describe('GeoJSON format', () => {
    it('should return FeatureCollection for format=geojson', async () => {
      const response = await fetch(`${baseUrl}/lookup?gers_ids=${BOSTON_GERS_ID}&format=geojson`);
      expect(response.ok).toBe(true);

      const geojson = await response.json();
      expect(geojson.type).toBe('FeatureCollection');
      expect(Array.isArray(geojson.features)).toBe(true);
      expect(geojson.features.length).toBe(1);
    });

    it('should have proper Feature structure', async () => {
      const response = await fetch(`${baseUrl}/lookup?gers_ids=${BOSTON_GERS_ID}&format=geojson`);
      const geojson = await response.json();

      const feature = geojson.features[0];
      expect(feature.type).toBe('Feature');
      expect(feature.id).toBe(BOSTON_GERS_ID);
      expect(feature.properties).toBeDefined();
      expect(feature.geometry).toBeDefined();
      expect(feature.geometry.type).toBe('Point');
      expect(feature.geometry.coordinates.length).toBe(2);
    });

    it('should include gers_id in properties', async () => {
      const response = await fetch(`${baseUrl}/lookup?gers_ids=${BOSTON_GERS_ID}&format=geojson`);
      const geojson = await response.json();

      const feature = geojson.features[0];
      expect(feature.properties.gers_id).toBe(BOSTON_GERS_ID);
      expect(feature.properties.display_name).toBe('Boston, MA');
    });
  });

  describe('alternative parameter name', () => {
    it('should accept "ids" as alternative to "gers_ids"', async () => {
      const response = await fetch(`${baseUrl}/lookup?ids=${BOSTON_GERS_ID}`);
      expect(response.ok).toBe(true);

      const results = await response.json();
      expect(results.length).toBe(1);
      expect(results[0].gers_id).toBe(BOSTON_GERS_ID);
    });
  });
});
