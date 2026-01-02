//! STAC catalog loading and shard management.

use geocoder_core::{GeocoderQuery, GeocoderResult};
use serde::Deserialize;
use worker::*;

const CATALOG_CACHE_TTL: u64 = 300; // 5 minutes
const SHARD_CACHE_TTL: u64 = 3600; // 1 hour

/// Loads and caches shards from R2.
pub struct ShardLoader<'a> {
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
    id: String,
    links: Vec<StacLink>,
}

#[derive(Debug, Deserialize)]
struct StacItem {
    id: String,
    properties: StacItemProperties,
    assets: StacAssets,
}

#[derive(Debug, Deserialize)]
struct StacItemProperties {
    record_count: u64,
    size_bytes: u64,
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
        let collection = self.load_latest_collection(&catalog).await?;

        // Determine which shards to query
        let mut shard_ids = vec!["HEAD".to_string()];
        if let Some(country) = cf_country {
            if self.collection_has_shard(&collection, country) {
                shard_ids.push(country.to_string());
            }
        }

        // Load and query each shard
        let mut all_results = Vec::new();
        for shard_id in &shard_ids {
            match self.query_shard(&collection, shard_id, query).await {
                Ok(results) => all_results.extend(results),
                Err(e) => {
                    console_log!("Error querying shard {}: {:?}", shard_id, e);
                }
            }
        }

        // Merge and deduplicate results
        all_results.sort_by(|a, b| {
            b.importance
                .partial_cmp(&a.importance)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        // Deduplicate by gers_id
        let mut seen = std::collections::HashSet::new();
        all_results.retain(|r| seen.insert(r.gers_id.clone()));

        // Apply limit
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

    async fn load_latest_collection(&self, catalog: &StacCatalog) -> Result<StacCollection> {
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
            .ok_or_else(|| Error::RustError("Invalid collection href".into()))?;

        let key = format!("{}/collection.json", version);
        let obj = self
            .bucket
            .get(&key)
            .execute()
            .await?
            .ok_or_else(|| Error::RustError(format!("{} not found", key)))?;

        let bytes = obj.body().ok_or_else(|| Error::RustError("Empty collection".into()))?;
        let text = bytes.text().await?;

        serde_json::from_str(&text)
            .map_err(|e| Error::RustError(format!("Failed to parse collection: {}", e)))
    }

    fn collection_has_shard(&self, collection: &StacCollection, shard_id: &str) -> bool {
        collection
            .links
            .iter()
            .any(|l| l.rel == "item" && l.href.contains(&format!("/{}.json", shard_id)))
    }

    async fn query_shard(
        &self,
        collection: &StacCollection,
        shard_id: &str,
        _query: &GeocoderQuery,
    ) -> Result<Vec<GeocoderResult>> {
        // Load the shard item metadata
        let version = &collection.id.replace("geocoder-", "");
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

        // Query the shard
        // TODO: Implement actual SQLite query using WASM
        // For now, return empty results
        console_log!(
            "Would query shard {} ({} bytes, {} records)",
            shard_id,
            shard_bytes.len(),
            item.properties.record_count
        );

        Ok(vec![])
    }
}
