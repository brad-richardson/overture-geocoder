//! STAC catalog loading and shard management.

use geocoder_core::{query::apply_location_bias, Database, GeocoderQuery, GeocoderResult, LocationBias};
use serde::Deserialize;
use worker::*;

// TODO: Implement edge caching using Cloudflare Cache API
// These TTLs will be used when caching is implemented to reduce R2 fetches.
#[allow(dead_code)]
const CATALOG_CACHE_TTL: u64 = 300; // 5 minutes
#[allow(dead_code)]
const SHARD_CACHE_TTL: u64 = 3600; // 1 hour

/// Loads and caches shards from R2.
pub struct ShardLoader<'a> {
    #[allow(dead_code)] // Reserved for future caching implementation
    env: &'a Env,
    bucket: Bucket,
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

#[derive(Debug, Deserialize)]
struct StacCollection {
    #[allow(dead_code)] // Available for future use (e.g., version display)
    id: String,
    links: Vec<StacLink>,
}

#[derive(Debug, Deserialize)]
struct StacItem {
    #[allow(dead_code)] // Available for future use (e.g., shard identification)
    id: String,
    properties: StacItemProperties,
    assets: StacAssets,
}

#[derive(Debug, Deserialize)]
struct StacItemProperties {
    record_count: u64,
    #[allow(dead_code)] // Available for cache validation
    size_bytes: u64,
    #[allow(dead_code)] // Available for integrity verification
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
        Ok(Self { env, bucket })
    }

    /// Search across HEAD and country shards.
    pub async fn search(
        &self,
        query: &GeocoderQuery,
        cf_country: Option<&str>,
    ) -> Result<Vec<GeocoderResult>> {
        // Load STAC catalog to find shards
        let catalog = self.load_catalog().await?;
        let (version, collection) = self.load_latest_collection(&catalog).await?;

        // Query HEAD shard (required - fail if unavailable)
        let head_results = self.query_shard(&version, "HEAD", query).await?;
        let mut all_results = head_results;

        // Query country shard if available (optional - log errors but continue)
        if let Some(country) = cf_country {
            if self.collection_has_shard(&collection, country) {
                match self.query_shard(&version, country, query).await {
                    Ok(results) => all_results.extend(results),
                    Err(e) => {
                        console_log!("Warning: country shard {} unavailable: {:?}", country, e);
                    }
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

    async fn load_catalog(&self) -> Result<StacCatalog> {
        let obj = self
            .bucket
            .get("catalog.json")
            .execute()
            .await?
            .ok_or_else(|| Error::RustError("catalog.json not found".into()))?;

        let bytes = obj.body().ok_or_else(|| Error::RustError("Empty catalog".into()))?;
        let text = bytes.text().await?;

        serde_json::from_str(&text)
            .map_err(|e| Error::RustError(format!("Failed to parse catalog: {}", e)))
    }

    /// Load the latest collection and return it along with its version string.
    async fn load_latest_collection(&self, catalog: &StacCatalog) -> Result<(String, StacCollection)> {
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
        let obj = self
            .bucket
            .get(&key)
            .execute()
            .await?
            .ok_or_else(|| Error::RustError(format!("{} not found", key)))?;

        let bytes = obj.body().ok_or_else(|| Error::RustError("Empty collection".into()))?;
        let text = bytes.text().await?;

        let collection: StacCollection = serde_json::from_str(&text)
            .map_err(|e| Error::RustError(format!("Failed to parse collection: {}", e)))?;

        Ok((version, collection))
    }

    fn collection_has_shard(&self, collection: &StacCollection, shard_id: &str) -> bool {
        collection
            .links
            .iter()
            .any(|l| l.rel == "item" && l.href.contains(&format!("/{}.json", shard_id)))
    }

    async fn query_shard(
        &self,
        version: &str,
        shard_id: &str,
        query: &GeocoderQuery,
    ) -> Result<Vec<GeocoderResult>> {
        // Load the shard item metadata
        let item_key = format!("{}/items/{}.json", version, shard_id);

        let item_obj = self
            .bucket
            .get(&item_key)
            .execute()
            .await?
            .ok_or_else(|| Error::RustError(format!("Item {} not found", item_key)))?;

        let item_bytes = item_obj.body().ok_or_else(|| Error::RustError("Empty item".into()))?;
        let item_text = item_bytes.text().await?;
        let item: StacItem = serde_json::from_str(&item_text)
            .map_err(|e| Error::RustError(format!("Failed to parse item: {}", e)))?;

        // Load the actual shard database
        let shard_href = &item.assets.data.href;
        let shard_key = format!("{}/{}", version, shard_href.trim_start_matches("./"));

        let shard_obj = self
            .bucket
            .get(&shard_key)
            .execute()
            .await?
            .ok_or_else(|| Error::RustError(format!("Shard {} not found", shard_key)))?;

        let shard_bytes = shard_obj
            .body()
            .ok_or_else(|| Error::RustError("Empty shard".into()))?
            .bytes()
            .await?;

        console_log!(
            "Loading shard {} ({} bytes, {} records)",
            shard_id,
            shard_bytes.len(),
            item.properties.record_count
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
