#!/usr/bin/env python3
"""Basic usage examples for the Overture Geocoder client.

Run with: python examples/basic_usage.py
"""

from overture_geocoder import OvertureGeocoder, geocode


def main():
    # =========================================================================
    # Quick functions (use default configuration)
    # =========================================================================

    print("=== Quick geocode function ===")
    quick_results = geocode("123 Main St, Boston, MA")
    print(f"Found: {len(quick_results)} results")
    if quick_results:
        print(f"First result: {quick_results[0].display_name}")

    # =========================================================================
    # Using the client class
    # =========================================================================

    print("\n=== OvertureGeocoder client ===")

    # Create a client with custom configuration
    with OvertureGeocoder(
        base_url="http://localhost:8787",  # Default
        timeout=10.0,  # 10 seconds
    ) as client:

        # Basic search
        print("\n--- Basic search ---")
        results = client.search("Boston City Hall")
        print(f"Search results: {len(results)}")
        for result in results[:3]:
            print(f"  - {result.display_name}")
            print(f"    Lat: {result.lat}, Lon: {result.lon}")
            print(f"    GERS ID: {result.gers_id}")

        # =====================================================================
        # Search options
        # =====================================================================

        print("\n--- Search with options ---")

        # Limit results
        limited = client.search("Main St", limit=5)
        print(f"Limited to 5 results: {len(limited)}")

        # Include address details
        with_address = client.search("123 Main St", addressdetails=True, limit=1)
        if with_address and with_address[0].address:
            addr = with_address[0].address
            print("Address breakdown:")
            print(f"  House number: {addr.house_number}")
            print(f"  Road: {addr.road}")
            print(f"  City: {addr.city}")
            print(f"  State: {addr.state}")
            print(f"  Postcode: {addr.postcode}")

        # Search within a bounding box
        in_box = client.search(
            "coffee",
            viewbox=(-71.1, 42.3, -71.0, 42.4),  # (lon1, lat1, lon2, lat2)
            bounded=True,
            limit=3,
        )
        print(f"Results in bounding box: {len(in_box)}")

        # =====================================================================
        # GeoJSON format
        # =====================================================================

        print("\n--- GeoJSON format ---")

        geojson = client.search_geojson("Boston Public Library")
        print(f"GeoJSON type: {geojson['type']}")
        print(f"Features: {len(geojson['features'])}")
        if geojson["features"]:
            feature = geojson["features"][0]
            print("First feature:")
            print(f"  ID: {feature['id']}")
            print(f"  Coordinates: {feature['geometry']['coordinates']}")

        # =====================================================================
        # Get full geometry by GERS ID (fetches directly from Overture S3)
        # =====================================================================

        print("\n--- Get full geometry by GERS ID ---")

        if results:
            gers_id = results[0].gers_id
            print(f"Fetching geometry for GERS ID: {gers_id}")

            # Get full geometry (requires overturemaps package)
            try:
                feature = client.get_geometry(gers_id)
                if feature:
                    print(f"Geometry type: {feature['geometry']['type']}")
                    print(f"Properties: {list(feature['properties'].keys())}")
            except ImportError as e:
                print(f"Skipping geometry fetch: {e}")

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
