import { describe, it, expect, vi, afterEach } from "vitest";
import {
  OvertureGeocoder,
  GeocoderError,
  GeocoderTimeoutError,
  GeocoderNetworkError,
  geocode,
} from "./index";

// ============================================================================
// Mock fixtures matching the REAL worker response shapes
// ============================================================================

// GET /search -> { results: [...] } with name/bbox (NOT primary_name/boundingbox)
const mockSearchResponse = {
  results: [
    {
      gers_id: "abc-123",
      name: "Boston",
      type: "locality",
      lat: 42.3601,
      lon: -71.0589,
      bbox: [-71.191, 42.227, -70.923, 42.397],
      importance: 0.85,
      country: "US",
      region: "US-MA",
    },
    {
      gers_id: "def-456",
      name: "Cambridge",
      type: "locality",
      lat: 42.3736,
      lon: -71.1097,
      bbox: [-71.161, 42.352, -71.064, 42.404],
      importance: 0.75,
    },
  ],
};

// GET /reverse -> SINGLE object (not an array)
const mockReverseResponse = {
  gers_id: "div-123",
  primary_name: "Back Bay",
  subtype: "neighborhood",
  lat: 42.3501,
  lon: -71.0789,
  boundingbox: [42.34, 42.36, -71.09, -71.07],
  distance_km: 0.1,
  confidence: "medium",
  hierarchy: [{ gers_id: "div-456", subtype: "locality", name: "Boston" }],
};

// GET /id/{gers_id}
const mockIdLookupResponse = {
  id: "0123abcd-0000-4000-8000-000000000000",
  bbox: { xmin: -71.191, ymin: 42.227, xmax: -70.923, ymax: 42.397 },
};

const mockGeoJSONResponse = {
  type: "FeatureCollection",
  features: [
    {
      type: "Feature",
      id: "abc-123",
      properties: {
        gers_id: "abc-123",
        name: "Boston",
        importance: 0.85,
        type: "locality",
      },
      bbox: [-71.191, 42.227, -70.923, 42.397],
      geometry: {
        type: "Point",
        coordinates: [-71.0589, 42.3601],
      },
    },
  ],
};

// ============================================================================
// Helpers
// ============================================================================

interface MockResponseOptions {
  status?: number;
  ok?: boolean;
  headers?: Record<string, string>;
}

function mockResponse(responseData: unknown, options: MockResponseOptions = {}) {
  const { status = 200, ok = status < 400, headers = {} } = options;
  return {
    ok,
    status,
    statusText: ok ? "OK" : "Error",
    headers: {
      get: (name: string) => headers[name] ?? headers[name.toLowerCase()] ?? null,
    },
    json: () => Promise.resolve(responseData),
  };
}

function createMockFetch(responseData: unknown, options: MockResponseOptions = {}) {
  return vi.fn().mockResolvedValue(mockResponse(responseData, options));
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// ============================================================================
// Tests
// ============================================================================

describe("OvertureGeocoder", () => {
  describe("constructor", () => {
    it("should use default configuration", () => {
      const client = new OvertureGeocoder();
      expect(client.getBaseUrl()).toBe("https://geocoder.bradr.dev");
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
    it("should search with query only and parse wrapped results", async () => {
      const mockFetch = createMockFetch(mockSearchResponse);
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
      expect(results[0].primary_name).toBe("Boston"); // mapped from "name"
      expect(results[0].lat).toBe(42.3601);
      expect(results[0].lon).toBe(-71.0589);
      expect(results[0].boundingbox).toEqual([-71.191, 42.227, -70.923, 42.397]); // mapped from "bbox"
      expect(results[0].importance).toBe(0.85);
      expect(results[0].type).toBe("locality");
    });

    it("should plumb through country and region when present", async () => {
      const mockFetch = createMockFetch(mockSearchResponse);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const results = await client.search("Boston");

      expect(results[0].country).toBe("US");
      expect(results[0].region).toBe("US-MA");
      // Second result omits them
      expect(results[1].country).toBeUndefined();
      expect(results[1].region).toBeUndefined();
    });

    it("should search with limit option", async () => {
      const mockFetch = createMockFetch(mockSearchResponse);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const results = await client.search("Boston", {
        limit: 5,
      });

      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain("limit=5");

      expect(results[0].type).toBe("locality");
    });

    it("should clamp limit to 1-40 range", async () => {
      const mockFetch = createMockFetch({ results: [] });
      const client = new OvertureGeocoder({ fetch: mockFetch });

      await client.search("test", { limit: 100 });
      expect(mockFetch.mock.calls[0][0]).toContain("limit=40");

      await client.search("test", { limit: 0 });
      expect(mockFetch.mock.calls[1][0]).toContain("limit=1");

      await client.search("test", { limit: -5 });
      expect(mockFetch.mock.calls[2][0]).toContain("limit=1");
    });

    it("should return a FeatureCollection for format geojson", async () => {
      const mockFetch = createMockFetch(mockGeoJSONResponse);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const result = await client.search("Boston", { format: "geojson" });

      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain("format=geojson");
      expect(result.type).toBe("FeatureCollection");
      expect(result.features).toHaveLength(1);
    });

    it("should include custom headers", async () => {
      const mockFetch = createMockFetch({ results: [] });
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

  describe("reverse", () => {
    it("should normalize the single response object into an array", async () => {
      const mockFetch = createMockFetch(mockReverseResponse);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const results = await client.reverse(42.3501, -71.0789);

      const [url] = mockFetch.mock.calls[0];
      expect(url).toContain("/reverse?");
      expect(url).toContain("lat=42.3501");
      expect(url).toContain("lon=-71.0789");

      expect(results).toHaveLength(1);
      expect(results[0].gers_id).toBe("div-123");
      expect(results[0].primary_name).toBe("Back Bay");
      expect(results[0].subtype).toBe("neighborhood");
      expect(results[0].distance_km).toBe(0.1);
      expect(results[0].confidence).toBe("medium");
    });

    it("should always provide hierarchy (non-optional)", async () => {
      const mockFetch = createMockFetch(mockReverseResponse);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const results = await client.reverse(42.3501, -71.0789);

      expect(results[0].hierarchy).toEqual([
        { gers_id: "div-456", subtype: "locality", name: "Boston" },
      ]);

      // Even if the server omitted it, hierarchy should be an empty array
      const { hierarchy: _omitted, ...withoutHierarchy } = mockReverseResponse;
      const client2 = new OvertureGeocoder({ fetch: createMockFetch(withoutHierarchy) });
      const results2 = await client2.reverse(42.3501, -71.0789);
      expect(results2[0].hierarchy).toEqual([]);
    });

    it.each(["high", "medium", "low"] as const)(
      "should pass through confidence value %s",
      async (confidence) => {
        const mockFetch = createMockFetch({ ...mockReverseResponse, confidence });
        const client = new OvertureGeocoder({ fetch: mockFetch });

        const results = await client.reverse(42.3501, -71.0789);
        expect(results[0].confidence).toBe(confidence);
      }
    );

    it("should return an empty array on 404 (nothing found)", async () => {
      const mockFetch = createMockFetch({ error: "Not found" }, { status: 404 });
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const results = await client.reverse(0, 0);
      expect(results).toEqual([]);
    });

    it("should upgrade confidence to high when geometry is verified", async () => {
      const mockFetch = createMockFetch(mockReverseResponse);
      const client = new OvertureGeocoder({ fetch: mockFetch });
      vi.spyOn(client, "verifyContainsPoint").mockResolvedValue(true);

      const results = await client.reverse(42.3501, -71.0789, {
        verifyGeometry: true,
      });

      expect(client.verifyContainsPoint).toHaveBeenCalledWith(
        "div-123",
        42.3501,
        -71.0789
      );
      expect(results).toHaveLength(1);
      expect(results[0].confidence).toBe("high");
    });

    it("should drop results when geometry verification rejects the point", async () => {
      const mockFetch = createMockFetch(mockReverseResponse);
      const client = new OvertureGeocoder({ fetch: mockFetch });
      vi.spyOn(client, "verifyContainsPoint").mockResolvedValue(false);

      const results = await client.reverse(42.3501, -71.0789, {
        verifyGeometry: true,
      });

      expect(results).toHaveLength(0);
    });

    it("should return results without verification when verifyGeometry is false", async () => {
      const mockFetch = createMockFetch(mockReverseResponse);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const results = await client.reverse(42.3501, -71.0789, {
        verifyGeometry: false,
        verifyLimit: 5,
      });

      expect(results).toHaveLength(1);
      expect(results[0].confidence).toBe("medium");
    });
  });

  describe("lookupId", () => {
    it("should return the lookup result on success", async () => {
      const mockFetch = createMockFetch(mockIdLookupResponse);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const result = await client.lookupId(mockIdLookupResponse.id);

      const [url] = mockFetch.mock.calls[0];
      expect(url).toBe(
        `https://geocoder.bradr.dev/id/${mockIdLookupResponse.id}`
      );
      expect(result).not.toBeNull();
      expect(result!.id).toBe(mockIdLookupResponse.id);
      expect(result!.bbox).toEqual({
        xmin: -71.191,
        ymin: 42.227,
        xmax: -70.923,
        ymax: 42.397,
      });
    });

    it("should return null on 404 (unknown ID)", async () => {
      const mockFetch = createMockFetch({ error: "Not found" }, { status: 404 });
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const result = await client.lookupId("ffffffff-0000-4000-8000-000000000000");
      expect(result).toBeNull();
    });

    it("should throw with status on 503 (index unavailable)", async () => {
      const mockFetch = createMockFetch(
        { error: "ID index unavailable" },
        { status: 503 }
      );
      const client = new OvertureGeocoder({ fetch: mockFetch, retries: 0 });

      await expect(client.lookupId("abc")).rejects.toMatchObject({
        name: "GeocoderError",
        status: 503,
      });
    });

    it("should URL-encode the GERS ID", async () => {
      const mockFetch = createMockFetch(mockIdLookupResponse);
      const client = new OvertureGeocoder({ fetch: mockFetch });

      await client.lookupId("weird/id?x");

      const [url] = mockFetch.mock.calls[0];
      expect(url).toBe("https://geocoder.bradr.dev/id/weird%2Fid%3Fx");
    });
  });

  describe("health", () => {
    it("should return health status", async () => {
      const mockFetch = createMockFetch({ status: "ok", version: "2026-02-25.0" });
      const client = new OvertureGeocoder({ fetch: mockFetch });

      const result = await client.health();

      const [url] = mockFetch.mock.calls[0];
      expect(url).toBe("https://geocoder.bradr.dev/health");
      expect(result.status).toBe("ok");
      expect(result.version).toBe("2026-02-25.0");
    });

    it("should return the error payload when service is unhealthy", async () => {
      const mockFetch = createMockFetch(
        { status: "error", error: "catalog unavailable" },
        { status: 503 }
      );
      const client = new OvertureGeocoder({ fetch: mockFetch, retries: 0 });

      const result = await client.health();
      expect(result.status).toBe("error");
      expect(result.error).toBe("catalog unavailable");
    });
  });

  describe("error handling", () => {
    it("should throw GeocoderError on 4xx response", async () => {
      const mockFetch = createMockFetch({ error: "Bad request" }, { status: 400 });
      const client = new OvertureGeocoder({ fetch: mockFetch });

      await expect(client.search("test")).rejects.toThrow(GeocoderError);
      await expect(client.search("test")).rejects.toMatchObject({
        status: 400,
        name: "GeocoderError",
      });
    });

    it("should throw GeocoderError on 5xx response without retries", async () => {
      const mockFetch = createMockFetch({ error: "Server error" }, { status: 500 });
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
          return Promise.resolve(mockResponse(null, { status: 500 }));
        }
        return Promise.resolve(mockResponse(mockSearchResponse));
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
      const mockFetch = createMockFetch({ error: "Not found" }, { status: 400 });
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
        return Promise.resolve(mockResponse(mockSearchResponse));
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

    it("should retry 429 honoring Retry-After", async () => {
      vi.useFakeTimers();
      let callCount = 0;
      const mockFetch = vi.fn().mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return Promise.resolve(
            mockResponse(null, { status: 429, headers: { "Retry-After": "2" } })
          );
        }
        return Promise.resolve(mockResponse(mockSearchResponse));
      });

      const client = new OvertureGeocoder({
        fetch: mockFetch,
        retries: 1,
        retryDelay: 999999, // would stall the test if Retry-After were ignored
      });

      const promise = client.search("test");
      // Not retried before Retry-After elapses
      await vi.advanceTimersByTimeAsync(1500);
      expect(mockFetch).toHaveBeenCalledTimes(1);

      await vi.advanceTimersByTimeAsync(600);
      const results = await promise;

      expect(mockFetch).toHaveBeenCalledTimes(2);
      expect(results).toHaveLength(2);
    });

    it("should cap Retry-After delay at 30 seconds", async () => {
      vi.useFakeTimers();
      let callCount = 0;
      const mockFetch = vi.fn().mockImplementation(() => {
        callCount++;
        if (callCount === 1) {
          return Promise.resolve(
            mockResponse(null, { status: 429, headers: { "Retry-After": "120" } })
          );
        }
        return Promise.resolve(mockResponse(mockSearchResponse));
      });

      const client = new OvertureGeocoder({ fetch: mockFetch, retries: 1 });

      const promise = client.search("test");
      // Should retry after at most 30s despite Retry-After: 120
      await vi.advanceTimersByTimeAsync(30000);
      const results = await promise;

      expect(mockFetch).toHaveBeenCalledTimes(2);
      expect(results).toHaveLength(2);
    });

    it("should not retry 429 when no retries remain", async () => {
      const mockFetch = createMockFetch(null, {
        status: 429,
        headers: { "Retry-After": "60" },
      });
      const client = new OvertureGeocoder({ fetch: mockFetch, retries: 0 });

      await expect(client.search("test")).rejects.toMatchObject({
        name: "GeocoderError",
        status: 429,
      });
      expect(mockFetch).toHaveBeenCalledTimes(1);
    });

    it("should use exponential backoff with jitter between 5xx attempts", async () => {
      const callTimes: number[] = [];
      const mockFetch = vi.fn().mockImplementation(() => {
        callTimes.push(Date.now());
        return Promise.resolve(mockResponse(null, { status: 500 }));
      });

      const client = new OvertureGeocoder({
        fetch: mockFetch,
        retries: 2,
        retryDelay: 50,
      });

      await expect(client.search("test")).rejects.toThrow();
      expect(mockFetch).toHaveBeenCalledTimes(3);

      // attempt 0 backoff: jittered 25-50ms, attempt 1 backoff: jittered 50-100ms
      const delay1 = callTimes[1] - callTimes[0];
      const delay2 = callTimes[2] - callTimes[1];
      expect(delay1).toBeGreaterThanOrEqual(20);
      expect(delay1).toBeLessThan(150);
      expect(delay2).toBeGreaterThanOrEqual(45);
      expect(delay2).toBeLessThan(250);
    });
  });

  describe("interceptors", () => {
    it("should call onRequest interceptor", async () => {
      const mockFetch = createMockFetch({ results: [] });
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
      const originalResponse = mockResponse(mockSearchResponse);
      const mockFetch = vi.fn().mockResolvedValue(originalResponse);

      const modifiedResponse = mockResponse({
        results: [{ ...mockSearchResponse.results[0], modified: true }],
      });
      const onResponse = vi.fn().mockReturnValue(modifiedResponse);

      const client = new OvertureGeocoder({ fetch: mockFetch, onResponse });

      await client.search("test");

      expect(onResponse).toHaveBeenCalledTimes(1);
      expect(onResponse).toHaveBeenCalledWith(originalResponse);
    });

    it("should support async interceptors", async () => {
      const mockFetch = createMockFetch(mockSearchResponse);
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
    const mockFetch = createMockFetch(mockSearchResponse);
    vi.stubGlobal("fetch", mockFetch);

    const results = await geocode("123 Main St");

    expect(results).toHaveLength(2);

    vi.unstubAllGlobals();
  });
});

describe("optional geometry dependency", () => {
  it("close should be a no-op when the geometry module was never loaded", async () => {
    const client = new OvertureGeocoder();

    await expect(client.close()).resolves.not.toThrow();
  });

  it("should expose the geometry methods", () => {
    const client = new OvertureGeocoder();
    expect(typeof client.getFullGeometry).toBe("function");
    expect(typeof client.verifyContainsPoint).toBe("function");
    expect(typeof client.getNearbyPlaces).toBe("function");
    expect(typeof client.getNearbyAddresses).toBe("function");
  });
});

describe("reverseAndRefine", () => {
  it("should call reverse with correct parameters", async () => {
    const mockFetch = createMockFetch(mockReverseResponse);
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

    expect(result.divisions).toHaveLength(1);
    expect(result.places).toEqual([]);
    expect(result.addresses).toEqual([]);
  });

  it("should skip places when includePlaces is false", async () => {
    const mockFetch = createMockFetch(mockReverseResponse);
    const client = new OvertureGeocoder({ fetch: mockFetch });

    vi.spyOn(client, "getNearbyPlaces").mockResolvedValue([]);
    vi.spyOn(client, "getNearbyAddresses").mockResolvedValue([]);

    const result = await client.reverseAndRefine(42.3501, -71.0789, {
      verifyGeometry: false,
      includePlaces: false,
      includeAddresses: true,
    });

    expect(result.divisions).toHaveLength(1);
    expect(result.places).toBeUndefined();
    expect(result.addresses).toEqual([]);
    expect(client.getNearbyPlaces).not.toHaveBeenCalled();
  });

  it("should skip addresses when includeAddresses is false", async () => {
    const mockFetch = createMockFetch(mockReverseResponse);
    const client = new OvertureGeocoder({ fetch: mockFetch });

    vi.spyOn(client, "getNearbyPlaces").mockResolvedValue([]);
    vi.spyOn(client, "getNearbyAddresses").mockResolvedValue([]);

    const result = await client.reverseAndRefine(42.3501, -71.0789, {
      verifyGeometry: false,
      includePlaces: true,
      includeAddresses: false,
    });

    expect(result.divisions).toHaveLength(1);
    expect(result.places).toEqual([]);
    expect(result.addresses).toBeUndefined();
    expect(client.getNearbyAddresses).not.toHaveBeenCalled();
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
  it("should expose client methods", async () => {
    const module = await import("./index");

    const client = new module.OvertureGeocoder();
    expect(typeof client.lookupId).toBe("function");
    expect(typeof client.health).toBe("function");
    expect(typeof client.getNearbyPlaces).toBe("function");
    expect(typeof client.getNearbyAddresses).toBe("function");
    expect(typeof client.reverseAndRefine).toBe("function");
  });
});
