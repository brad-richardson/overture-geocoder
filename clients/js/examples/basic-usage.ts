/**
 * Basic usage examples for the Overture Geocoder client.
 *
 * Run with: npx tsx examples/basic-usage.ts
 */

import { OvertureGeocoder, geocode, lookup } from "../src/index";

async function main() {
  // ==========================================================================
  // Quick functions (use default configuration)
  // ==========================================================================

  console.log("=== Quick geocode function ===");
  const quickResults = await geocode("123 Main St, Boston, MA");
  console.log("Found:", quickResults.length, "results");
  if (quickResults.length > 0) {
    console.log("First result:", quickResults[0].display_name);
  }

  // ==========================================================================
  // Using the client class
  // ==========================================================================

  console.log("\n=== OvertureGeocoder client ===");

  // Create a client with custom configuration
  const client = new OvertureGeocoder({
    baseUrl: "http://localhost:8787", // Default
    timeout: 10000, // 10 seconds
  });

  // Basic search
  console.log("\n--- Basic search ---");
  const results = await client.search("Boston City Hall");
  console.log("Search results:", results.length);
  for (const result of results.slice(0, 3)) {
    console.log(`  - ${result.display_name}`);
    console.log(`    Lat: ${result.lat}, Lon: ${result.lon}`);
    console.log(`    GERS ID: ${result.gers_id}`);
  }

  // ==========================================================================
  // Search options
  // ==========================================================================

  console.log("\n--- Search with options ---");

  // Limit results
  const limited = await client.search("Main St", { limit: 5 });
  console.log("Limited to 5 results:", limited.length);

  // Include address details
  const withAddress = await client.search("123 Main St", {
    addressdetails: true,
    limit: 1,
  });
  if (withAddress.length > 0 && withAddress[0].address) {
    console.log("Address breakdown:");
    console.log("  House number:", withAddress[0].address.house_number);
    console.log("  Road:", withAddress[0].address.road);
    console.log("  City:", withAddress[0].address.city);
    console.log("  State:", withAddress[0].address.state);
    console.log("  Postcode:", withAddress[0].address.postcode);
  }

  // Search within a bounding box
  const inBox = await client.search("coffee", {
    viewbox: [-71.1, 42.3, -71.0, 42.4], // [lon1, lat1, lon2, lat2]
    bounded: true,
    limit: 3,
  });
  console.log("Results in bounding box:", inBox.length);

  // ==========================================================================
  // GeoJSON format
  // ==========================================================================

  console.log("\n--- GeoJSON format ---");

  const geojson = await client.searchGeoJSON("Boston Public Library");
  console.log("GeoJSON type:", geojson.type);
  console.log("Features:", geojson.features.length);
  if (geojson.features.length > 0) {
    const feature = geojson.features[0];
    console.log("First feature:");
    console.log("  ID:", feature.id);
    console.log("  Coordinates:", feature.geometry.coordinates);
  }

  // ==========================================================================
  // Lookup by GERS ID
  // ==========================================================================

  console.log("\n--- Lookup by GERS ID ---");

  if (results.length > 0) {
    const gersId = results[0].gers_id;
    console.log("Looking up GERS ID:", gersId);

    // Single lookup
    const lookupResult = await client.lookup(gersId);
    console.log("Lookup result:", lookupResult[0]?.display_name);

    // Multiple lookups
    if (results.length > 1) {
      const ids = results.slice(0, 3).map((r) => r.gers_id);
      const multiLookup = await client.lookup(ids);
      console.log("Multi-lookup results:", multiLookup.length);
    }

    // Get geometry
    const geometry = await client.getGeometry(gersId);
    if (geometry) {
      console.log("Geometry type:", geometry.geometry.type);
      console.log("Coordinates:", geometry.geometry.coordinates);
    }
  }

  console.log("\n=== Done ===");
}

main().catch(console.error);
