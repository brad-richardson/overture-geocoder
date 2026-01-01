import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  OvertureGeocoder,
  GeocoderError,
  GeocoderTimeoutError,
  GeocoderNetworkError,
  geocode,
  lookup,
  clearCatalogCache,
} from "./index";

// Mock response data
const mockSearchResults = [
  {
    gers_id: "abc-123",
    display_name: "123 Main St, Boston, MA 02101",
    lat: "42.3601000",
    lon: "-71.0589000",
    boundingbox: ["42.3600000", "42.3602000", "-71.0590000", "-71.0588000"],
    importance: 0.85,
    type: "address",
  },
  {
    gers_id: "def-456",
    display_name: "456 Oak Ave, Boston, MA 02102",
    lat: "42.3611000",
    lon: "-71.0599000",
    boundingbox: ["42.3610000", "42.3612000", "-71.0600000", "-71.0598000"],
    importance: 0.75,
    type: "address",
  },
];

const mockSearchResultsWithAddress = [
  {
    ...mockSearchResults[0],
    address: {
      house_number: "123",
      road: "Main St",
      city: "Boston",
      state: "MA",
      postcode: "02101",
      country: "United States",
      country_code: "us",
    },
  },
];

const mockGeoJSONResponse = {
  type: "FeatureCollection",
  features: [
    {
      type: "Feature",
      id: "abc-123",
      properties: {
        gers_id: "abc-123",
        display_name: "123 Main St, Boston, MA 02101",
        importance: 0.85,
      },
      bbox: [42.36, 42.3602, -71.059, -71.0588],
      geometry: {
        type: "Point",
        coordinates: [-71.0589, 42.3601],
      },
    },
  ],
};

// Helper to create mock fetch
function createMockFetch(responseData: unknown, options: { status?: number; ok?: boolean } = {}) {
  const { status = 200, ok = true } = options;
  return vi.fn().mockResolvedValue({
    ok,
    status,
    statusText: ok ? "OK" : "Error",
    json: vi.fn().mockResolvedValue(responseData),
  });
}

describe("OvertureGeocoder", () => {
  describe("constructor", () => {
    it("should use default configuration", () => {
      const client = new OvertureGeocoder();
      expect(client.getBaseUrl()).toBe("https://overture-geocoder.bradr.workers.dev");
      expect(client.getOvertureRelease()).toBe("2025-12-17.0");
    });

    it("should accept custom configuration", () => {
      const client = new OvertureGeocoder({
        baseUrl: "https://api.example.com/",
        overtureRelease: "2025-01-01.0",
      });
      expect(client.getBaseUrl()).toBe("https://api.example.com");
      expect(client.getOvertureRelease()).toBe("2025-01-01.0");
    });

    it("should strip trailing slash from baseUrl", () => {
      const client = new OvertureGeocoder({ baseUrl: "https://api.example.com/" });
      expect(client.getBaseUrl()).toBe("https://api.example.com");
    });
  });

  describe("search", () => {
    it("should search with query only", async () => {
      const mockFetch = createMockFetch(mockSearchResults);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const results = await client.search("123 Main St");

      expect(mockFetch).toHaveBeenCalledTimes(1);
      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain("/search?");
      expect(url).toContain("q=123+Main+St");
      expect(url).toContain("format=jsonv2");
      expect(url).toContain("limit=10");

      expect(results).toHaveLength(2);
      expect(results[0].gers_id).toBe("abc-123");
      expect(results[0].lat).toBe(42.3601);
      expect(results[0].lon).toBe(-71.0589);
    });

    it("should search with all options", async () => {
      const mockFetch = createMockFetch(mockSearchResultsWithAddress);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const results = await client.search("123 Main St", {
        limit: 5,
        countrycodes: "us,ca",
        viewbox: [-72, 41, -70, 43],
        bounded: true,
        addressdetails: true,
      });

      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain("limit=5");
      expect(url).toContain("countrycodes=us%2Cca");
      expect(url).toContain("viewbox=-72%2C41%2C-70%2C43");
      expect(url).toContain("bounded=1");
      expect(url).toContain("addressdetails=1");

      expect(results[0].address).toBeDefined();
      expect(results[0].address?.city).toBe("Boston");
    });

    it("should clamp limit to 1-40 range", async () => {
      const mockFetch = createMockFetch([]);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      await client.search("test", { limit: 100 });
      expect(mockFetch.mock.calls[0][0]).toContain("limit=40");

      await client.search("test", { limit: 0 });
      expect(mockFetch.mock.calls[1][0]).toContain("limit=1");

      await client.search("test", { limit: -5 });
      expect(mockFetch.mock.calls[2][0]).toContain("limit=1");
    });

    it("should include custom headers", async () => {
      const mockFetch = createMockFetch([]);
      const client = new OvertureGeocoder({
        fetch: mockFetch,
        headers: {
          "X-API-Key": "test-key",
          "X-Custom": "value",
        },
      });

      await client.search("test");

      const [, init] = mockFetch.mock.calls[0];
      expect(init.headers).toMatchObject({
        Accept: "application/json",
        "X-API-Key": "test-key",
        "X-Custom": "value",
      });
    });
  });

  describe("searchGeoJSON", () => {
    it("should return GeoJSON FeatureCollection", async () => {
      const mockFetch = createMockFetch(mockGeoJSONResponse);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const result = await client.searchGeoJSON("123 Main St");

      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain("format=geojson");

      expect(result.type).toBe("FeatureCollection");
      expect(result.features).toHaveLength(1);
      expect(result.features[0].geometry.type).toBe("Point");
    });
  });

  describe("lookup", () => {
    it("should lookup single GERS ID", async () => {
      const mockFetch = createMockFetch([mockSearchResults[0]]);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const results = await client.lookup("abc-123");

      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain("/lookup?");
      expect(url).toContain("gers_ids=abc-123");

      expect(results).toHaveLength(1);
      expect(results[0].gers_id).toBe("abc-123");
    });

    it("should lookup multiple GERS IDs", async () => {
      const mockFetch = createMockFetch(mockSearchResults);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const results = await client.lookup(["abc-123", "def-456"]);

      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain("gers_ids=abc-123%2Cdef-456");

      expect(results).toHaveLength(2);
    });

    it("should return empty array for empty input", async () => {
      const mockFetch = createMockFetch([]);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const results = await client.lookup([]);

      expect(mockFetch).not.toHaveBeenCalled();
      expect(results).toEqual([]);
    });
  });

  describe("lookupGeoJSON", () => {
    it("should return GeoJSON FeatureCollection", async () => {
      const mockFetch = createMockFetch(mockGeoJSONResponse);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const result = await client.lookupGeoJSON("abc-123");

      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain("format=geojson");

      expect(result.type).toBe("FeatureCollection");
    });

    it("should return empty FeatureCollection for empty input", async () => {
      const mockFetch = createMockFetch([]);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const result = await client.lookupGeoJSON([]);

      expect(mockFetch).not.toHaveBeenCalled();
      expect(result).toEqual({ type: "FeatureCollection", features: [] });
    });
  });

  describe("getGeometry", () => {
    it("should return first feature from lookup", async () => {
      const mockFetch = createMockFetch(mockGeoJSONResponse);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const result = await client.getGeometry("abc-123");

      expect(result).toBeDefined();
      expect(result?.id).toBe("abc-123");
      expect(result?.geometry.type).toBe("Point");
    });

    it("should return null when no results", async () => {
      const mockFetch = createMockFetch({ type: "FeatureCollection", features: [] });
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const result = await client.getGeometry("nonexistent");

      expect(result).toBeNull();
    });
  });

  describe("error handling", () => {
    it("should throw GeocoderError on 4xx response", async () => {
      const mockFetch = createMockFetch({ error: "Bad request" }, { status: 400, ok: false });
      const client = new OvertureGeocoder({ fetch: mockFetch });

      await expect(client.search("test")).rejects.toThrow(GeocoderError);
      await expect(client.search("test")).rejects.toMatchObject({
        status: 400,
        name: "GeocoderError",
      });
    });

    it("should throw GeocoderError on 5xx response without retries", async () => {
      const mockFetch = createMockFetch({ error: "Server error" }, { status: 500, ok: false });
      const client = new OvertureGeocoder({ fetch: mockFetch, retries: 0 });

      await expect(client.search("test")).rejects.toThrow(GeocoderError);
    });

    it("should throw GeocoderNetworkError on network failure", async () => {
      const mockFetch = vi.fn().mockRejectedValue(new Error("Network failure"));
      const client = new OvertureGeocoder({ fetch: mockFetch, retries: 0 });

      await expect(client.search("test")).rejects.toThrow(GeocoderNetworkError);
    });

    it("should throw GeocoderTimeoutError on timeout", async () => {
      const abortError = new Error("Aborted");
      abortError.name = "AbortError";
      const mockFetch = vi.fn().mockRejectedValue(abortError);
      const client = new OvertureGeocoder({ fetch: mockFetch, timeout: 100, retries: 0 });

      await expect(client.search("test")).rejects.toThrow(GeocoderTimeoutError);
    });
  });

  describe("retry behavior", () => {
    it("should retry on 5xx errors", async () => {
      let callCount = 0;
      const mockFetch = vi.fn().mockImplementation(() => {
        callCount++;
        if (callCount < 3) {
          return Promise.resolve({
            ok: false,
            status: 500,
            statusText: "Internal Server Error",
          });
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(mockSearchResults),
        });
      });

      const client = new OvertureGeocoder({
        fetch: mockFetch,
        retries: 3,
        retryDelay: 10,
      });

      const results = await client.search("test");

      expect(mockFetch).toHaveBeenCalledTimes(3);
      expect(results).toHaveLength(2);
    });

    it("should not retry on 4xx errors", async () => {
      const mockFetch = createMockFetch({ error: "Not found" }, { status: 404, ok: false });
      const client = new OvertureGeocoder({
        fetch: mockFetch,
        retries: 3,
        retryDelay: 10,
      });

      await expect(client.search("test")).rejects.toThrow(GeocoderError);
      expect(mockFetch).toHaveBeenCalledTimes(1);
    });

    it("should retry on network errors", async () => {
      let callCount = 0;
      const mockFetch = vi.fn().mockImplementation(() => {
        callCount++;
        if (callCount < 2) {
          return Promise.reject(new Error("Network error"));
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(mockSearchResults),
        });
      });

      const client = new OvertureGeocoder({
        fetch: mockFetch,
        retries: 2,
        retryDelay: 10,
      });

      const results = await client.search("test");

      expect(mockFetch).toHaveBeenCalledTimes(2);
      expect(results).toHaveLength(2);
    });

    it("should respect retryDelay between attempts", async () => {
      const delays: number[] = [];
      let lastCallTime = Date.now();

      const mockFetch = vi.fn().mockImplementation(() => {
        const now = Date.now();
        delays.push(now - lastCallTime);
        lastCallTime = now;
        return Promise.resolve({
          ok: false,
          status: 500,
          statusText: "Error",
        });
      });

      const client = new OvertureGeocoder({
        fetch: mockFetch,
        retries: 2,
        retryDelay: 50,
      });

      await expect(client.search("test")).rejects.toThrow();

      // First call should be immediate, subsequent calls should have ~50ms delay
      expect(delays[1]).toBeGreaterThanOrEqual(40);
      expect(delays[2]).toBeGreaterThanOrEqual(40);
    });
  });

  describe("interceptors", () => {
    it("should call onRequest interceptor", async () => {
      const mockFetch = createMockFetch([]);
      const onRequest = vi.fn((url: string, init: RequestInit) => ({
        ...init,
        headers: { ...init.headers, "X-Intercepted": "true" },
      }));

      const client = new OvertureGeocoder({ fetch: mockFetch, onRequest });

      await client.search("test");

      expect(onRequest).toHaveBeenCalledTimes(1);
      expect(onRequest).toHaveBeenCalledWith(
        expect.stringContaining("/search?"),
        expect.objectContaining({ method: "GET" })
      );

      const [, init] = mockFetch.mock.calls[0];
      expect(init.headers["X-Intercepted"]).toBe("true");
    });

    it("should call onResponse interceptor", async () => {
      const originalResponse = {
        ok: true,
        status: 200,
        json: () => Promise.resolve(mockSearchResults),
      };
      const mockFetch = vi.fn().mockResolvedValue(originalResponse);

      const modifiedResponse = {
        ok: true,
        status: 200,
        json: () => Promise.resolve([{ ...mockSearchResults[0], modified: true }]),
      };
      const onResponse = vi.fn().mockReturnValue(modifiedResponse);

      const client = new OvertureGeocoder({ fetch: mockFetch, onResponse });

      await client.search("test");

      expect(onResponse).toHaveBeenCalledTimes(1);
      expect(onResponse).toHaveBeenCalledWith(originalResponse);
    });

    it("should support async interceptors", async () => {
      const mockFetch = createMockFetch(mockSearchResults);
      const onRequest = vi.fn(async (url: string, init: RequestInit) => {
        await new Promise((r) => setTimeout(r, 10));
        return { ...init, headers: { ...init.headers, "X-Async": "true" } };
      });

      const client = new OvertureGeocoder({ fetch: mockFetch, onRequest });

      await client.search("test");

      const [, init] = mockFetch.mock.calls[0];
      expect(init.headers["X-Async"]).toBe("true");
    });
  });

  describe("timeout", () => {
    it("should abort request after timeout", async () => {
      vi.useFakeTimers();

      const mockFetch = vi.fn().mockImplementation(
        (url: string, init: RequestInit) =>
          new Promise((resolve, reject) => {
            init.signal?.addEventListener("abort", () => {
              const error = new Error("Aborted");
              error.name = "AbortError";
              reject(error);
            });
          })
      );

      const client = new OvertureGeocoder({
        fetch: mockFetch,
        timeout: 1000,
        retries: 0,
      });

      const searchPromise = client.search("test");

      vi.advanceTimersByTime(1001);

      await expect(searchPromise).rejects.toThrow(GeocoderTimeoutError);

      vi.useRealTimers();
    });
  });
});

describe("convenience functions", () => {
  it("geocode should use default client", async () => {
    const mockFetch = createMockFetch(mockSearchResults);
    vi.stubGlobal("fetch", mockFetch);

    const results = await geocode("123 Main St");

    expect(results).toHaveLength(2);

    vi.unstubAllGlobals();
  });

  it("lookup should use default client", async () => {
    const mockFetch = createMockFetch([mockSearchResults[0]]);
    vi.stubGlobal("fetch", mockFetch);

    const results = await lookup("abc-123");

    expect(results).toHaveLength(1);

    vi.unstubAllGlobals();
  });
});

// Mock STAC catalog for geometry tests
const mockStacCatalog = {
  id: "overturemaps",
  type: "Catalog",
  links: [],
  registry: {
    path: "release/2025-12-17.0/gers",
    manifest: [
      ["000.parquet", "0fffffff-ffff-ffff-ffff-ffffffffffff"],
      ["001.parquet", "1fffffff-ffff-ffff-ffff-ffffffffffff"],
      ["00f.parquet", "ffffffff-ffff-ffff-ffff-ffffffffffff"],
    ] as [string, string][],
  },
};

describe("getFullGeometry", () => {
  beforeEach(() => {
    clearCatalogCache();
  });

  it("should return null when GERS ID is not in manifest", async () => {
    // Catalog with limited range - ID starting with 'z' won't be found
    const limitedCatalog = {
      ...mockStacCatalog,
      registry: {
        path: "release/2025-12-17.0/gers",
        manifest: [["000.parquet", "0fffffff-ffff-ffff-ffff-ffffffffffff"]] as [string, string][],
      },
    };

    const mockFetch = vi.fn().mockImplementation((url: string) => {
      if (url.includes("stac.overturemaps.org")) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(limitedCatalog),
        });
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve([]),
      });
    });

    const client = new OvertureGeocoder({ fetch: mockFetch });

    // This ID starts with 'f' which is beyond the max 'z' in the manifest
    const result = await client.getFullGeometry("f0000000-0000-0000-0000-000000000000");
    expect(result).toBeNull();
  });

  it("should throw when GERS registry not in catalog", async () => {
    const catalogWithoutRegistry = {
      id: "overturemaps",
      type: "Catalog",
      links: [],
      // No registry field
    };

    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve(catalogWithoutRegistry),
    });

    const client = new OvertureGeocoder({ fetch: mockFetch });

    await expect(
      client.getFullGeometry("abc-123")
    ).rejects.toThrow("GERS registry not found in STAC catalog");
  });

  it("should close DuckDB resources", async () => {
    const client = new OvertureGeocoder();

    // close() should not throw even if DuckDB was never initialized
    await expect(client.close()).resolves.not.toThrow();
  });
});
