//! STAC catalog loading and shard management with edge caching.

use geocoder_core::{
    query::apply_location_bias, Database, GeocoderQuery, GeocoderResult, LocationBias,
    ReverseResult,
};
use serde::Deserialize;
use worker::*;

// Cache TTLs for different resource types
const CATALOG_CACHE_TTL: u64 = 300; // 5 minutes - need fresh version pointers
const COLLECTION_CACHE_TTL: u64 = 300; // 5 minutes - contains shard list
const SHARD_CACHE_TTL: u64 = 3600; // 1 hour - versioned paths = natural invalidation

// Cache key prefix (uses custom domain for Cache API to work)
const CACHE_PREFIX: &str = "https://geocoder.bradr.dev/__cache/";

// Shard selection constants
const NEARBY_THRESHOLD_KM: f64 = 200.0; // Include shards within this distance
const MAX_LOCATION_SHARDS: usize = 4; // Max shards to load (excluding HEAD)

/// User location derived from Cloudflare request headers.
#[derive(Debug, Clone, Default)]
pub struct UserLocation {
    pub country: Option<String>,
    #[allow(dead_code)]
    pub region: Option<String>,
    pub lat: Option<f64>,
    pub lon: Option<f64>,
}

impl UserLocation {
    /// Extract location from Cloudflare request headers.
    pub fn from_request(req: &Request) -> Self {
        let headers = req.headers();
        Self {
            country: headers.get("CF-IPCountry").ok().flatten(),
            region: headers.get("CF-Region").ok().flatten(),
            lat: headers
                .get("CF-IPLatitude")
                .ok()
                .flatten()
                .and_then(|s| s.parse().ok()),
            lon: headers
                .get("CF-IPLongitude")
                .ok()
                .flatten()
                .and_then(|s| s.parse().ok()),
        }
    }

    /// Check if we have coordinates.
    #[allow(dead_code)]
    pub fn has_coordinates(&self) -> bool {
        self.lat.is_some() && self.lon.is_some()
    }
}

/// Loads and caches shards from R2 with edge caching via Cache API.
pub struct ShardLoader<'a> {
    #[allow(dead_code)]
    env: &'a Env,
    bucket: Bucket,
    cache: Cache,
}

#[derive(Debug, Deserialize)]
struct StacCatalog {
    links: Vec<StacLink>,
}

#[derive(Debug, Deserialize)]
struct StacLink {
    rel: String,
    href: String,
    #[serde(default)]
    latest: bool,
}

/// Embedded item metadata in collection.json
#[derive(Debug, Deserialize)]
struct EmbeddedItem {
    record_count: u64,
    #[allow(dead_code)]
    size_bytes: u64,
    #[allow(dead_code)]
    sha256: String,
    href: String,
    /// Bounding box [min_lon, min_lat, max_lon, max_lat] for proximity queries
    #[serde(default)]
    bbox: Option<[f64; 4]>,
    /// Parent country code for region shards (e.g., "CN" for "CN-GD")
    #[serde(default)]
    #[allow(dead_code)]
    parent_country: Option<String>,
}

#[derive(Debug, Deserialize)]
struct StacCollection {
    #[allow(dead_code)]
    id: String,
    /// Embedded items (new format) - keyed by shard ID (e.g., "US", "HEAD")
    #[serde(default)]
    items: std::collections::HashMap<String, EmbeddedItem>,
    /// Legacy links to individual item files
    links: Vec<StacLink>,
    /// Countries that have been split into region shards
    /// e.g., {"CN": ["CN-GD", "CN-BJ", ...], "IN": [...]}
    #[serde(default)]
    region_sharded: std::collections::HashMap<String, Vec<String>>,
}

/// Legacy STAC item format (for backward compatibility with old catalogs)
#[derive(Debug, Deserialize)]
struct StacItem {
    #[allow(dead_code)]
    id: String,
    properties: StacItemProperties,
    assets: StacAssets,
}

#[derive(Debug, Deserialize)]
struct StacItemProperties {
    record_count: u64,
    #[allow(dead_code)]
    size_bytes: u64,
    #[allow(dead_code)]
    sha256: String,
}

#[derive(Debug, Deserialize)]
struct StacAssets {
    data: StacAsset,
}

#[derive(Debug, Deserialize)]
struct StacAsset {
    href: String,
}

impl<'a> ShardLoader<'a> {
    pub fn new(env: &'a Env) -> Result<Self> {
        let bucket = env.bucket("SHARDS_BUCKET")?;
        let cache = Cache::default();
        Ok(Self { env, bucket, cache })
    }

    /// Fetch from R2 with edge caching via Cache API.
    async fn cached_get(&self, key: &str, ttl: u64) -> Result<Option<Vec<u8>>> {
        let cache_key = format!("{}{}", CACHE_PREFIX, key);

        // Try cache first
        let request = Request::new(&cache_key, Method::Get)?;
        if let Some(mut response) = self.cache.get(&request, false).await? {
            console_log!("Cache HIT: {}", key);
            let bytes = response.bytes().await?;
            return Ok(Some(bytes));
        }

        console_log!("Cache MISS: {}", key);

        // Fetch from R2
        let obj = self.bucket.get(key).execute().await?;
        if let Some(obj) = obj {
            let body = obj
                .body()
                .ok_or_else(|| Error::RustError("Empty object".into()))?;
            let bytes = body.bytes().await?;

            // Store in cache with TTL (non-blocking via waitUntil would be ideal, but for now inline)
            let headers = Headers::new();
            headers.set("Cache-Control", &format!("s-maxage={}", ttl))?;
            headers.set("Content-Type", "application/octet-stream")?;

            let cache_response = Response::from_bytes(bytes.clone())?.with_headers(headers);
            let cache_request = Request::new(&cache_key, Method::Get)?;

            // Put in cache (best effort, don't fail the request if caching fails)
            if let Err(e) = self.cache.put(&cache_request, cache_response).await {
                console_log!("Cache PUT failed for {}: {:?}", key, e);
            }

            return Ok(Some(bytes));
        }

        Ok(None)
    }

    /// Fetch text from R2 with caching.
    async fn cached_get_text(&self, key: &str, ttl: u64) -> Result<Option<String>> {
        match self.cached_get(key, ttl).await? {
            Some(bytes) => {
                let text = String::from_utf8(bytes)
                    .map_err(|e| Error::RustError(format!("Invalid UTF-8: {}", e)))?;
                Ok(Some(text))
            }
            None => Ok(None),
        }
    }

    /// Search across HEAD and nearby shards based on user location.
    pub async fn search(
        &self,
        query: &GeocoderQuery,
        user_location: &UserLocation,
    ) -> Result<Vec<GeocoderResult>> {
        // Load STAC catalog to find shards
        let catalog = self.load_catalog().await?;
        let (version, collection) = self.load_latest_collection(&catalog).await?;

        // Query HEAD shard (required - fail if unavailable)
        let head_results = self
            .query_shard(&version, "HEAD", &collection, query)
            .await?;
        let mut all_results = head_results;

        // Select nearby shards based on user location
        let nearby_shards = self.select_nearby_shards(&collection, user_location);
        console_log!("Selected shards: {:?}", nearby_shards);

        // Query each nearby shard
        for shard_id in nearby_shards {
            match self
                .query_shard(&version, &shard_id, &collection, query)
                .await
            {
                Ok(results) => all_results.extend(results),
                Err(e) => {
                    console_log!("Warning: shard {} unavailable: {:?}", shard_id, e);
                }
            }
        }

        // Sort by importance before deduplication
        all_results.sort_by(|a, b| {
            b.importance
                .partial_cmp(&a.importance)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        // Deduplicate by gers_id (keep highest importance)
        let mut seen = std::collections::HashSet::new();
        all_results.retain(|r| seen.insert(r.gers_id.clone()));

        // Apply location bias (can elevate results from country shard)
        if !matches!(query.bias, LocationBias::None) {
            apply_location_bias(&mut all_results, &query.bias);
        }

        // Truncate to requested limit after bias is applied
        all_results.truncate(query.limit);

        Ok(all_results)
    }

    /// Select shards to query based on user location and proximity.
    fn select_nearby_shards(
        &self,
        collection: &StacCollection,
        user_location: &UserLocation,
    ) -> Vec<String> {
        // If we have coordinates, use proximity-based selection
        if let (Some(lat), Some(lon)) = (user_location.lat, user_location.lon) {
            return self.select_shards_by_proximity(collection, lat, lon);
        }

        // Fallback: use country code if available
        if let Some(country) = &user_location.country {
            return self.select_shards_for_country(collection, country);
        }

        // No location info - return empty (only HEAD will be queried)
        Vec::new()
    }

    /// Select shards by proximity to coordinates.
    fn select_shards_by_proximity(
        &self,
        collection: &StacCollection,
        lat: f64,
        lon: f64,
    ) -> Vec<String> {
        // Collect all shards with their distances
        let mut candidates: Vec<(String, f64)> = collection
            .items
            .iter()
            .filter_map(|(shard_id, item)| {
                // Skip HEAD (queried separately)
                if shard_id == "HEAD" {
                    return None;
                }

                // Need bbox for distance calculation
                let bbox = item.bbox.as_ref()?;
                let distance = distance_to_bbox(lat, lon, bbox);

                // Only include shards within threshold
                if distance <= NEARBY_THRESHOLD_KM {
                    Some((shard_id.clone(), distance))
                } else {
                    None
                }
            })
            .collect();

        // Sort by distance (closest first) - ensures user's actual location is never excluded
        candidates.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));

        // Take closest shards up to limit
        candidates
            .into_iter()
            .take(MAX_LOCATION_SHARDS)
            .map(|(id, _)| id)
            .collect()
    }

    /// Select shards for a country (fallback when no coordinates available).
    fn select_shards_for_country(&self, collection: &StacCollection, country: &str) -> Vec<String> {
        // Check if country is region-sharded
        if let Some(regions) = collection.region_sharded.get(country) {
            // Country is split into regions - load all of them (up to limit)
            return regions.iter().take(MAX_LOCATION_SHARDS).cloned().collect();
        }

        // Country has a single shard
        if self.collection_has_shard(collection, country) {
            return vec![country.to_string()];
        }

        Vec::new()
    }

    /// Reverse geocode a lat/lon coordinate.
    pub async fn reverse_geocode(
        &self,
        lat: f64,
        lon: f64,
        cf_country: Option<&str>,
    ) -> Result<Option<ReverseResult>> {
        // Load STAC catalog to find reverse shards
        let catalog = self.load_catalog().await?;
        let (version, _collection) = self.load_latest_collection(&catalog).await?;

        // Try country shard first if available (more specific data)
        if let Some(country) = cf_country {
            match self.query_reverse_shard(&version, country, lat, lon).await {
                Ok(Some(result)) => return Ok(Some(result)),
                Ok(None) => {
                    console_log!("No result in country {} reverse shard", country);
                }
                Err(e) => {
                    console_log!(
                        "Warning: country reverse shard {} unavailable: {:?}",
                        country,
                        e
                    );
                }
            }
        }

        // Fall back to HEAD shard
        self.query_reverse_shard(&version, "HEAD", lat, lon).await
    }

    async fn query_reverse_shard(
        &self,
        version: &str,
        shard_id: &str,
        lat: f64,
        lon: f64,
    ) -> Result<Option<ReverseResult>> {
        // Load the reverse shard item metadata (cached)
        let item_key = format!("{}/reverse-items/{}.json", version, shard_id);
        let item_text = self
            .cached_get_text(&item_key, SHARD_CACHE_TTL)
            .await?
            .ok_or_else(|| Error::RustError(format!("Reverse item {} not found", item_key)))?;

        let item: StacItem = serde_json::from_str(&item_text)
            .map_err(|e| Error::RustError(format!("Failed to parse item: {}", e)))?;

        // Load the actual reverse shard database (cached)
        let shard_href = &item.assets.data.href;
        let shard_key = format!("{}/{}", version, shard_href.trim_start_matches("./"));

        let shard_bytes = self
            .cached_get(&shard_key, SHARD_CACHE_TTL)
            .await?
            .ok_or_else(|| Error::RustError(format!("Reverse shard {} not found", shard_key)))?;

        console_log!(
            "Loading reverse shard {} ({} bytes, {} records)",
            shard_id,
            shard_bytes.len(),
            item.properties.record_count
        );

        // Open the SQLite database from bytes and query it
        let db = Database::from_bytes(&shard_bytes).map_err(|e| {
            Error::RustError(format!("Failed to open reverse shard database: {}", e))
        })?;

        let result = db
            .reverse_geocode(lat, lon)
            .map_err(|e| Error::RustError(format!("Reverse geocode failed: {}", e)))?;

        Ok(result)
    }

    async fn load_catalog(&self) -> Result<StacCatalog> {
        let text = self
            .cached_get_text("catalog.json", CATALOG_CACHE_TTL)
            .await?
            .ok_or_else(|| Error::RustError("catalog.json not found".into()))?;

        serde_json::from_str(&text)
            .map_err(|e| Error::RustError(format!("Failed to parse catalog: {}", e)))
    }

    /// Load the latest collection and return it along with its version string.
    async fn load_latest_collection(
        &self,
        catalog: &StacCatalog,
    ) -> Result<(String, StacCollection)> {
        // Find the link marked as latest
        let latest_link = catalog
            .links
            .iter()
            .find(|l| l.rel == "child" && l.latest)
            .ok_or_else(|| Error::RustError("No latest collection found".into()))?;

        // Extract version from href (e.g., "./2026-01-02.0/collection.json")
        let version = latest_link
            .href
            .trim_start_matches("./")
            .split('/')
            .next()
            .ok_or_else(|| Error::RustError("Invalid collection href".into()))?
            .to_string();

        let key = format!("{}/collection.json", version);
        let text = self
            .cached_get_text(&key, COLLECTION_CACHE_TTL)
            .await?
            .ok_or_else(|| Error::RustError(format!("{} not found", key)))?;

        let collection: StacCollection = serde_json::from_str(&text)
            .map_err(|e| Error::RustError(format!("Failed to parse collection: {}", e)))?;

        Ok((version, collection))
    }

    fn collection_has_shard(&self, collection: &StacCollection, shard_id: &str) -> bool {
        // Check embedded items first (new format)
        if collection.items.contains_key(shard_id) {
            return true;
        }
        // Fall back to legacy links check
        collection
            .links
            .iter()
            .any(|l| l.rel == "item" && l.href.contains(&format!("/{}.json", shard_id)))
    }

    /// Get embedded item metadata from collection, or return None if not found.
    fn get_embedded_item<'b>(
        &self,
        collection: &'b StacCollection,
        shard_id: &str,
    ) -> Option<&'b EmbeddedItem> {
        collection.items.get(shard_id)
    }

    async fn query_shard(
        &self,
        version: &str,
        shard_id: &str,
        collection: &StacCollection,
        query: &GeocoderQuery,
    ) -> Result<Vec<GeocoderResult>> {
        // Get item metadata from embedded items (new format) or fall back to separate file
        let (shard_href, record_count) =
            if let Some(item) = self.get_embedded_item(collection, shard_id) {
                (item.href.clone(), item.record_count)
            } else {
                // Legacy: load from separate item file
                let item_key = format!("{}/items/{}.json", version, shard_id);
                let item_text = self
                    .cached_get_text(&item_key, SHARD_CACHE_TTL)
                    .await?
                    .ok_or_else(|| Error::RustError(format!("Item {} not found", item_key)))?;

                let item: StacItem = serde_json::from_str(&item_text)
                    .map_err(|e| Error::RustError(format!("Failed to parse item: {}", e)))?;

                (item.assets.data.href.clone(), item.properties.record_count)
            };

        // Load the actual shard database (cached)
        let shard_key = format!("{}/{}", version, shard_href.trim_start_matches("./"));

        let shard_bytes = self
            .cached_get(&shard_key, SHARD_CACHE_TTL)
            .await?
            .ok_or_else(|| Error::RustError(format!("Shard {} not found", shard_key)))?;

        console_log!(
            "Loading shard {} ({} bytes, {} records)",
            shard_id,
            shard_bytes.len(),
            record_count
        );

        // Open the SQLite database from bytes and query it
        let db = Database::from_bytes(&shard_bytes)
            .map_err(|e| Error::RustError(format!("Failed to open shard database: {}", e)))?;

        let results = db
            .search(query)
            .map_err(|e| Error::RustError(format!("Search failed: {}", e)))?;

        Ok(results)
    }
}

/// Calculate the minimum distance from a point to a bounding box in kilometers.
/// Returns 0 if the point is inside the bbox.
fn distance_to_bbox(lat: f64, lon: f64, bbox: &[f64; 4]) -> f64 {
    let [min_lon, min_lat, max_lon, max_lat] = *bbox;

    // Check if point is inside bbox
    if lon >= min_lon && lon <= max_lon && lat >= min_lat && lat <= max_lat {
        return 0.0;
    }

    // Find closest point on bbox boundary
    let closest_lon = lon.clamp(min_lon, max_lon);
    let closest_lat = lat.clamp(min_lat, max_lat);

    // Calculate haversine distance
    haversine_distance(lat, lon, closest_lat, closest_lon)
}

/// Calculate the haversine distance between two points in kilometers.
fn haversine_distance(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    const EARTH_RADIUS_KM: f64 = 6371.0;

    let lat1_rad = lat1.to_radians();
    let lat2_rad = lat2.to_radians();
    let delta_lat = (lat2 - lat1).to_radians();
    let delta_lon = (lon2 - lon1).to_radians();

    let a = (delta_lat / 2.0).sin().powi(2)
        + lat1_rad.cos() * lat2_rad.cos() * (delta_lon / 2.0).sin().powi(2);
    let c = 2.0 * a.sqrt().asin();

    EARTH_RADIUS_KM * c
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_haversine_distance() {
        // Boston to New York (~306 km)
        let dist = haversine_distance(42.3601, -71.0589, 40.7128, -74.0060);
        assert!((dist - 306.0).abs() < 5.0, "Distance was {}", dist);

        // Same point
        let dist = haversine_distance(42.3601, -71.0589, 42.3601, -71.0589);
        assert!(dist < 0.01, "Distance was {}", dist);
    }

    #[test]
    fn test_distance_to_bbox_inside() {
        // Point inside bbox
        let bbox = [-74.0, 40.0, -71.0, 43.0]; // Roughly covers NYC to Boston
        let dist = distance_to_bbox(41.0, -72.0, &bbox);
        assert_eq!(dist, 0.0);
    }

    #[test]
    fn test_distance_to_bbox_outside() {
        // Point outside bbox (Los Angeles to NYC bbox)
        let bbox = [-74.0, 40.0, -71.0, 43.0];
        let dist = distance_to_bbox(34.0522, -118.2437, &bbox); // LA
        assert!(dist > 3000.0, "Distance was {}", dist); // Should be ~3900 km
    }

    #[test]
    fn test_distance_to_bbox_edge() {
        // Point just outside bbox edge
        let bbox = [-74.0, 40.0, -71.0, 43.0];
        let dist = distance_to_bbox(40.0, -75.0, &bbox); // 1 degree west
        assert!(dist > 0.0 && dist < 100.0, "Distance was {}", dist);
    }

    #[test]
    fn test_user_location_default() {
        let loc = UserLocation::default();
        assert!(loc.country.is_none());
        assert!(loc.region.is_none());
        assert!(loc.lat.is_none());
        assert!(loc.lon.is_none());
    }

    #[test]
    fn test_user_location_has_coordinates() {
        let loc = UserLocation {
            lat: Some(42.0),
            lon: Some(-71.0),
            ..Default::default()
        };
        assert!(loc.has_coordinates());

        let loc_no_coords = UserLocation::default();
        assert!(!loc_no_coords.has_coordinates());
    }
}
