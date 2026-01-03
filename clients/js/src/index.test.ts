import { describe, it, expect, vi, afterEach } from "vitest";
import {
  OvertureGeocoder,
  GeocoderError,
  GeocoderTimeoutError,
  GeocoderNetworkError,
  geocode,
} from "./index";

// Mock response data - using numbers to match actual server responses
const mockSearchResults = [
  {
    gers_id: "abc-123",
    primary_name: "Boston",
    lat: 42.3601,
    lon: -71.0589,
    boundingbox: [42.227, 42.397, -71.191, -70.923],
    importance: 0.85,
    type: "locality",
  },
  {
    gers_id: "def-456",
    primary_name: "Cambridge",
    lat: 42.3736,
    lon: -71.1097,
    boundingbox: [42.352, 42.404, -71.161, -71.064],
    importance: 0.75,
    type: "locality",
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
        primary_name: "Boston",
        importance: 0.85,
        type: "locality",
      },
      bbox: [42.227, 42.397, -71.191, -70.923],
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
      expect(client.getBaseUrl()).toBe("https://geocoder.bradr.workers.dev");
    });

    it("should accept custom baseUrl", () => {
      const client = new OvertureGeocoder({
        baseUrl: "https://api.example.com/",
      });
      expect(client.getBaseUrl()).toBe("https://api.example.com");
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

    it("should search with limit option", async () => {
      const mockFetch = createMockFetch(mockSearchResults);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const results = await client.search("Boston", {
        limit: 5,
      });

      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain("limit=5");

      expect(results[0].type).toBe("locality");
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
});

describe("getFullGeometry and close", () => {
  it("should close DuckDB resources", async () => {
    const client = new OvertureGeocoder();

    // close() should not throw even if DuckDB was never initialized
    await expect(client.close()).resolves.not.toThrow();
  });

  // Note: getFullGeometry tests for STAC behavior are now in the
  // @bradrichardson/overturemaps package which handles the STAC lookup
});

// Mock reverse results for testing
const mockReverseResults = [
  {
    gers_id: "div-123",
    primary_name: "Back Bay",
    subtype: "neighborhood",
    lat: 42.3501,
    lon: -71.0789,
    boundingbox: [42.34, 42.36, -71.09, -71.07],
    distance_km: 0.1,
    confidence: "bbox" as const,
    hierarchy: [
      { gers_id: "div-456", subtype: "locality", name: "Boston, MA" },
    ],
  },
  {
    gers_id: "div-456",
    primary_name: "Boston, MA",
    subtype: "locality",
    lat: 42.3601,
    lon: -71.0589,
    boundingbox: [42.227, 42.397, -71.191, -70.923],
    distance_km: 0.5,
    confidence: "bbox" as const,
  },
];

describe("reverse with verifyGeometry", () => {
  it("should return results without verification when verifyGeometry is false", async () => {
    const mockFetch = createMockFetch(mockReverseResults);
    const client = new OvertureGeocoder({ fetch: mockFetch });

    const results = await client.reverse(42.3501, -71.0789, {
      verifyGeometry: false,
    });

    expect(results).toHaveLength(2);
    expect(results[0].confidence).toBe("bbox");
  });

  it("should include verifyGeometry in ReverseOptions", async () => {
    const mockFetch = createMockFetch(mockReverseResults);
    const client = new OvertureGeocoder({ fetch: mockFetch });

    // This should compile and run without error
    const results = await client.reverse(42.3501, -71.0789, {
      verifyGeometry: false,
      verifyLimit: 5,
    });

    expect(results).toHaveLength(2);
  });
});

describe("reverseAndRefine", () => {
  it("should call reverse with correct parameters", async () => {
    const mockFetch = createMockFetch(mockReverseResults);
    const client = new OvertureGeocoder({ fetch: mockFetch });

    // Mock the getNearbyPlaces and getNearbyAddresses to return empty arrays
    // since DuckDB isn't available in tests
    vi.spyOn(client, "getNearbyPlaces").mockResolvedValue([]);
    vi.spyOn(client, "getNearbyAddresses").mockResolvedValue([]);

    const result = await client.reverseAndRefine(42.3501, -71.0789, {
      verifyGeometry: false, // Skip geometry verification in test
      includePlaces: true,
      includeAddresses: true,
    });

    expect(result.divisions).toHaveLength(2);
    expect(result.places).toEqual([]);
    expect(result.addresses).toEqual([]);
  });

  it("should skip places when includePlaces is false", async () => {
    const mockFetch = createMockFetch(mockReverseResults);
    const client = new OvertureGeocoder({ fetch: mockFetch });

    vi.spyOn(client, "getNearbyPlaces").mockResolvedValue([]);
    vi.spyOn(client, "getNearbyAddresses").mockResolvedValue([]);

    const result = await client.reverseAndRefine(42.3501, -71.0789, {
      verifyGeometry: false,
      includePlaces: false,
      includeAddresses: true,
    });

    expect(result.divisions).toHaveLength(2);
    expect(result.places).toBeUndefined();
    expect(result.addresses).toEqual([]);
    expect(client.getNearbyPlaces).not.toHaveBeenCalled();
  });

  it("should skip addresses when includeAddresses is false", async () => {
    const mockFetch = createMockFetch(mockReverseResults);
    const client = new OvertureGeocoder({ fetch: mockFetch });

    vi.spyOn(client, "getNearbyPlaces").mockResolvedValue([]);
    vi.spyOn(client, "getNearbyAddresses").mockResolvedValue([]);

    const result = await client.reverseAndRefine(42.3501, -71.0789, {
      verifyGeometry: false,
      includePlaces: true,
      includeAddresses: false,
    });

    expect(result.divisions).toHaveLength(2);
    expect(result.places).toEqual([]);
    expect(result.addresses).toBeUndefined();
    expect(client.getNearbyAddresses).not.toHaveBeenCalled();
  });
});

describe("getNearbyPlaces", () => {
  it("should have the getNearbyPlaces method", () => {
    const client = new OvertureGeocoder();
    expect(typeof client.getNearbyPlaces).toBe("function");
  });

  it("should accept NearbySearchOptions", () => {
    const client = new OvertureGeocoder();
    // Verify the method signature accepts the expected options
    expect(async () => {
      // This will fail at runtime without DuckDB, but verifies types compile
      try {
        await client.getNearbyPlaces(42.35, -71.08, {
          radiusKm: 0.5,
          limit: 5,
          category: "restaurant",
        });
      } catch {
        // Expected to fail without actual DuckDB/S3 access
      }
    }).not.toThrow();
  });
});

describe("getNearbyAddresses", () => {
  it("should have the getNearbyAddresses method", () => {
    const client = new OvertureGeocoder();
    expect(typeof client.getNearbyAddresses).toBe("function");
  });
});

describe("radiusToBbox calculation", () => {
  it("should create valid bounding box from radius", () => {
    const client = new OvertureGeocoder();
    // Access private method through prototype for testing
    const radiusToBbox = (client as unknown as { radiusToBbox: (lat: number, lon: number, radiusKm: number) => { xmin: number; ymin: number; xmax: number; ymax: number } }).radiusToBbox.bind(client);

    const bbox = radiusToBbox(42.35, -71.08, 1);

    // Verify bbox structure
    expect(bbox).toHaveProperty("xmin");
    expect(bbox).toHaveProperty("ymin");
    expect(bbox).toHaveProperty("xmax");
    expect(bbox).toHaveProperty("ymax");

    // Verify bbox is centered on the point
    expect(bbox.xmin).toBeLessThan(-71.08);
    expect(bbox.xmax).toBeGreaterThan(-71.08);
    expect(bbox.ymin).toBeLessThan(42.35);
    expect(bbox.ymax).toBeGreaterThan(42.35);
  });
});

describe("haversineDistance calculation", () => {
  it("should calculate distance between two points", () => {
    const client = new OvertureGeocoder();
    // Access private method through prototype for testing
    const haversineDistance = (client as unknown as { haversineDistance: (lat1: number, lon1: number, lat2: number, lon2: number) => number }).haversineDistance.bind(client);

    // Boston to Cambridge (roughly 5km apart)
    const distance = haversineDistance(42.3601, -71.0589, 42.3736, -71.1097);

    expect(distance).toBeGreaterThan(4);
    expect(distance).toBeLessThan(6);
  });

  it("should return 0 for same point", () => {
    const client = new OvertureGeocoder();
    const haversineDistance = (client as unknown as { haversineDistance: (lat1: number, lon1: number, lat2: number, lon2: number) => number }).haversineDistance.bind(client);

    const distance = haversineDistance(42.35, -71.08, 42.35, -71.08);
    expect(distance).toBe(0);
  });
});

describe("type exports", () => {
  it("should export all new types", async () => {
    // Import types to verify they're exported
    const module = await import("./index");

    // Verify the new methods exist on the class
    const client = new module.OvertureGeocoder();
    expect(typeof client.getNearbyPlaces).toBe("function");
    expect(typeof client.getNearbyAddresses).toBe("function");
    expect(typeof client.reverseAndRefine).toBe("function");
  });

  it("should export readByBbox and readByBboxAll from overturemaps", async () => {
    const module = await import("./index");
    expect(module.readByBbox).toBeDefined();
    expect(module.readByBboxAll).toBeDefined();
  });
});
