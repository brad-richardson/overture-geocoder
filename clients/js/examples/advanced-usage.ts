/**
 * Advanced usage examples for the Overture Geocoder client.
 *
 * Demonstrates:
 * - Custom fetch implementation
 * - Request/response interceptors
 * - Retry configuration
 * - Error handling
 * - Logging and debugging
 *
 * Run with: npx tsx examples/advanced-usage.ts
 */

import {
  OvertureGeocoder,
  GeocoderError,
  GeocoderTimeoutError,
  GeocoderNetworkError,
  type OvertureGeocoderConfig,
} from "../src/index";

// =============================================================================
// Custom fetch with logging
// =============================================================================

function createLoggingFetch(): typeof fetch {
  return async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const start = Date.now();

    console.log(`[FETCH] ${init?.method || "GET"} ${url}`);

    try {
      const response = await fetch(input, init);
      const duration = Date.now() - start;
      console.log(`[FETCH] ${response.status} ${response.statusText} (${duration}ms)`);
      return response;
    } catch (error) {
      const duration = Date.now() - start;
      console.log(`[FETCH] ERROR after ${duration}ms:`, error);
      throw error;
    }
  };
}

// =============================================================================
// Request/response interceptors
// =============================================================================

async function interceptorsExample() {
  console.log("\n=== Interceptors Example ===");

  // Track all requests for debugging
  const requestLog: Array<{ url: string; timestamp: Date }> = [];

  const client = new OvertureGeocoder({
    baseUrl: "http://localhost:8787",

    // Request interceptor - runs before each request
    onRequest: (url, init) => {
      requestLog.push({ url, timestamp: new Date() });
      console.log(`[onRequest] Intercepted: ${url}`);

      // Add custom header
      return {
        ...init,
        headers: {
          ...init.headers,
          "X-Request-ID": `req-${Date.now()}`,
          "X-Client-Version": "1.0.0",
        },
      };
    },

    // Response interceptor - runs after each response
    onResponse: (response) => {
      console.log(`[onResponse] Status: ${response.status}`);
      console.log(`[onResponse] Headers: ${response.headers.get("content-type")}`);

      // You can modify or wrap the response here
      return response;
    },
  });

  try {
    const results = await client.search("Boston");
    console.log(`Found ${results.length} results`);
    console.log(`Total requests made: ${requestLog.length}`);
  } catch (error) {
    console.error("Search failed:", error);
  }
}

// =============================================================================
// Retry configuration
// =============================================================================

async function retryExample() {
  console.log("\n=== Retry Configuration Example ===");

  const client = new OvertureGeocoder({
    baseUrl: "http://localhost:8787",
    retries: 3, // Retry up to 3 times
    retryDelay: 1000, // Wait 1 second between retries
    timeout: 5000, // 5 second timeout per request

    // Log retry attempts
    onRequest: (url, init) => {
      console.log(`[Request] ${url}`);
      return init;
    },
  });

  try {
    const results = await client.search("test query");
    console.log(`Success! Found ${results.length} results`);
  } catch (error) {
    if (error instanceof GeocoderTimeoutError) {
      console.error("All retry attempts timed out");
    } else if (error instanceof GeocoderNetworkError) {
      console.error("Network error after retries:", error.message);
    } else if (error instanceof GeocoderError) {
      console.error(`API error (${error.status}):`, error.message);
    } else {
      throw error;
    }
  }
}

// =============================================================================
// Error handling
// =============================================================================

async function errorHandlingExample() {
  console.log("\n=== Error Handling Example ===");

  const client = new OvertureGeocoder({
    baseUrl: "http://localhost:8787",
    timeout: 5000,
    retries: 1,
  });

  try {
    const results = await client.search("123 Main St");
    console.log(`Found ${results.length} results`);
  } catch (error) {
    // Handle specific error types
    if (error instanceof GeocoderTimeoutError) {
      console.error("Request timed out. The server may be slow or unavailable.");
      console.error("Try increasing the timeout or checking your connection.");
    } else if (error instanceof GeocoderNetworkError) {
      console.error("Network error:", error.message);
      if (error.cause) {
        console.error("Underlying cause:", error.cause.message);
      }
    } else if (error instanceof GeocoderError) {
      console.error(`API error: ${error.status} - ${error.message}`);

      // Handle specific status codes
      switch (error.status) {
        case 400:
          console.error("Bad request - check your query parameters");
          break;
        case 404:
          console.error("Resource not found");
          break;
        case 429:
          console.error("Rate limited - slow down your requests");
          break;
        case 500:
          console.error("Server error - try again later");
          break;
      }
    } else {
      // Unknown error
      throw error;
    }
  }
}

// =============================================================================
// Custom headers for API keys
// =============================================================================

async function authenticationExample() {
  console.log("\n=== Custom Headers Example ===");

  const client = new OvertureGeocoder({
    baseUrl: "http://localhost:8787",
    headers: {
      // Add authentication header
      Authorization: `Bearer ${process.env.API_TOKEN || "your-api-key"}`,
      // Add custom tracking headers
      "X-Client-ID": "my-app",
      "X-Request-Source": "cli-example",
    },
  });

  try {
    const results = await client.search("Boston");
    console.log(`Found ${results.length} results`);
  } catch (error) {
    console.error("Error:", error);
  }
}

// =============================================================================
// Factory function for creating pre-configured clients
// =============================================================================

function createProductionClient(): OvertureGeocoder {
  return new OvertureGeocoder({
    baseUrl: process.env.GEOCODER_API_URL || "https://geocoder.example.com",
    timeout: 10000,
    retries: 2,
    retryDelay: 500,
    headers: {
      Authorization: `Bearer ${process.env.GEOCODER_API_KEY}`,
    },
    onRequest: (url, init) => {
      // Add request tracing
      const traceId = `trace-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      return {
        ...init,
        headers: {
          ...init.headers,
          "X-Trace-ID": traceId,
        },
      };
    },
  });
}

function createDevelopmentClient(): OvertureGeocoder {
  return new OvertureGeocoder({
    baseUrl: "http://localhost:8787",
    timeout: 30000, // Longer timeout for local dev
    retries: 0, // No retries in dev for faster feedback
    fetch: createLoggingFetch(), // Log all requests
  });
}

// =============================================================================
// Main
// =============================================================================

async function main() {
  console.log("=== Advanced Usage Examples ===\n");

  // Uncomment to run specific examples:
  await interceptorsExample();
  await errorHandlingExample();
  // await retryExample();
  // await authenticationExample();

  // Factory usage
  console.log("\n=== Factory Example ===");
  const client =
    process.env.NODE_ENV === "production"
      ? createProductionClient()
      : createDevelopmentClient();

  console.log("Using client with baseUrl:", client.getBaseUrl());

  try {
    const results = await client.search("Boston City Hall", { limit: 3 });
    console.log(`Found ${results.length} results`);
    for (const r of results) {
      console.log(`  - ${r.display_name}`);
    }
  } catch (error) {
    console.error("Search failed:", error);
  }
}

main().catch(console.error);
