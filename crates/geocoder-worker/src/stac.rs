//! STAC catalog loading and shard management with edge caching.

use bytes::Bytes;
use geocoder_core::{
    query::{apply_exact_match_bonus, apply_location_bias},
    Database, GeocoderQuery, GeocoderResult, IdLookupResult, LocationBias, ReverseResult,
};
use parquet::file::reader::{ChunkReader, FileReader, Length, SerializedFileReader};
use parquet::record::RowAccessor;
use serde::{Deserialize, Serialize};
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
const MAX_VERSION_ATTEMPTS: usize = 3; // Max versions to try (latest + fallbacks)
const NEGATIVE_CACHE_TTL: u64 = 30; // 30 seconds - avoids hammering R2 for missing objects

/// User location derived from Cloudflare request headers.
#[derive(Debug, Clone, Default)]
pub struct UserLocation {
    pub country: Option<String>,
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

/// Debug info about a loaded shard.
#[derive(Debug, Clone, Serialize)]
pub struct ShardDebugInfo {
    pub id: String,
    pub size_bytes: usize,
    pub record_count: u64,
}

/// Debug info about the search operation.
#[derive(Debug, Clone, Serialize)]
pub struct SearchDebugInfo {
    pub version: String,
    pub user_location: UserLocationDebug,
    pub shards_loaded: Vec<ShardDebugInfo>,
}

/// Serializable user location for debug output.
#[derive(Debug, Clone, Serialize)]
pub struct UserLocationDebug {
    pub country: Option<String>,
    pub region: Option<String>,
    pub lat: Option<f64>,
    pub lon: Option<f64>,
}

impl From<&UserLocation> for UserLocationDebug {
    fn from(loc: &UserLocation) -> Self {
        Self {
            country: loc.country.clone(),
            region: loc.region.clone(),
            lat: loc.lat,
            lon: loc.lon,
        }
    }
}

/// Search result with optional debug info.
pub struct SearchResult {
    pub results: Vec<GeocoderResult>,
    pub debug: Option<SearchDebugInfo>,
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
    #[serde(default)]
    sha256: Option<String>,
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
    #[serde(default)]
    sha256: Option<String>,
}

#[derive(Debug, Deserialize)]
struct StacAssets {
    data: StacAsset,
}

#[derive(Debug, Deserialize)]
struct StacAsset {
    href: String,
}

/// Check if an error indicates a missing resource that should trigger version fallback.
///
/// Only "not found" errors are retriable — these indicate a version whose data hasn't
/// been fully deployed yet. Operational errors (database corruption, query failures,
/// parse errors) are surfaced immediately to avoid silently serving stale data.
fn is_retriable_error(e: &Error) -> bool {
    let msg = format!("{:?}", e);
    msg.contains("not found")
}

/// Run an async operation with version fallback.
///
/// Tries each version in order. Errors matching `is_retriable_error` (missing resources)
/// trigger fallback to the next version. Non-retriable errors (corruption, query failures)
/// are returned immediately.
macro_rules! with_version_fallback {
    ($self:expr, $endpoint:expr, $version:ident, $body:expr) => {{
        let catalog = $self.load_catalog().await?;
        let versions = get_ordered_versions(&catalog);
        if versions.is_empty() {
            return Err(Error::RustError("No versions found in catalog".into()));
        }
        let mut last_error = None;
        for $version in &versions {
            let $version = $version.as_str();
            match $body {
                Ok(result) => {
                    if last_error.is_some() {
                        console_log!(
                            "Fallback to version {} succeeded for {}",
                            $version,
                            $endpoint
                        );
                    }
                    return Ok(result);
                }
                Err(e) if is_retriable_error(&e) => {
                    console_log!(
                        "Version {} not available for {}: {:?}, trying fallback",
                        $version,
                        $endpoint,
                        e
                    );
                    last_error = Some(e);
                }
                Err(e) => return Err(e),
            }
        }
        Err(last_error.unwrap_or_else(|| {
            Error::RustError(format!("No working version found for {}", $endpoint))
        }))
    }};
}

impl<'a> ShardLoader<'a> {
    pub fn new(env: &'a Env) -> Result<Self> {
        let bucket = env.bucket("SHARDS_BUCKET")?;
        let cache = Cache::default();
        Ok(Self { env, bucket, cache })
    }

    /// Lightweight health check: verify catalog loads and latest version exists.
    pub async fn check_health(&self) -> Result<String> {
        let catalog = self.load_catalog().await?;
        let versions = get_ordered_versions(&catalog);
        if versions.is_empty() {
            return Err(Error::RustError("No versions found in catalog".into()));
        }
        Ok(versions[0].clone())
    }

    /// Fetch from R2 with edge caching via Cache API.
    ///
    /// Caches both positive results (with the caller's TTL) and negative results
    /// (object not found, with a short TTL) to avoid hammering R2 during deployments.
    async fn cached_get(&self, key: &str, ttl: u64) -> Result<Option<Vec<u8>>> {
        let cache_key = format!("{}{}", CACHE_PREFIX, key);

        // Try cache first
        let request = Request::new(&cache_key, Method::Get)?;
        if let Some(mut response) = self.cache.get(&request, false).await? {
            let bytes = response.bytes().await?;
            // Empty body is our negative-cache sentinel (real R2 objects are never empty)
            if bytes.is_empty() {
                console_log!("Cache HIT (negative): {}", key);
                return Ok(None);
            }
            console_log!("Cache HIT: {}", key);
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

        // Cache the negative result (empty body sentinel) with a short TTL to avoid
        // repeated R2 GETs for objects that don't exist yet during deployments
        let neg_headers = Headers::new();
        neg_headers.set("Cache-Control", &format!("s-maxage={}", NEGATIVE_CACHE_TTL))?;
        neg_headers.set("Content-Type", "application/octet-stream")?;
        let neg_response = Response::from_bytes(vec![])?.with_headers(neg_headers);
        let neg_request = Request::new(&cache_key, Method::Get)?;
        if let Err(e) = self.cache.put(&neg_request, neg_response).await {
            console_log!("Negative cache PUT failed for {}: {:?}", key, e);
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
    /// Falls back to older versions if the latest version's shards are unavailable.
    pub async fn search(
        &self,
        query: &GeocoderQuery,
        user_location: &UserLocation,
        include_debug: bool,
    ) -> Result<SearchResult> {
        with_version_fallback!(self, "search", version, {
            self.try_search(version, query, user_location, include_debug)
                .await
        })
    }

    /// Attempt search against a specific version.
    async fn try_search(
        &self,
        version: &str,
        query: &GeocoderQuery,
        user_location: &UserLocation,
        include_debug: bool,
    ) -> Result<SearchResult> {
        let collection = self.load_collection(version).await?;

        // Track loaded shards for debug
        let mut shards_loaded = Vec::new();

        // Query HEAD shard (required - fail triggers version fallback)
        let (head_results, head_info) = self
            .query_shard_with_info(version, "HEAD", &collection, query)
            .await?;
        let mut all_results = head_results;
        if include_debug {
            shards_loaded.push(head_info);
        }

        // Select nearby shards based on user location
        let nearby_shards = self.select_nearby_shards(&collection, user_location);
        console_log!("Selected shards: {:?}", nearby_shards);

        // Query each nearby shard (non-fatal failures)
        for shard_id in nearby_shards {
            match self
                .query_shard_with_info(version, &shard_id, &collection, query)
                .await
            {
                Ok((results, info)) => {
                    all_results.extend(results);
                    if include_debug {
                        shards_loaded.push(info);
                    }
                }
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

        // Apply exact match bonus (helps "Paris" rank above "Parish")
        apply_exact_match_bonus(&mut all_results, &query.text);

        // Apply location bias (can elevate results from country shard)
        if !matches!(query.bias, LocationBias::None) {
            apply_location_bias(&mut all_results, &query.bias);
        }

        // Truncate to requested limit after bias is applied
        all_results.truncate(query.limit);

        // Build debug info if requested
        let debug = if include_debug {
            Some(SearchDebugInfo {
                version: version.to_string(),
                user_location: user_location.into(),
                shards_loaded,
            })
        } else {
            None
        };

        Ok(SearchResult {
            results: all_results,
            debug,
        })
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
    /// Falls back to older versions if the latest version's shards are unavailable.
    pub async fn reverse_geocode(
        &self,
        lat: f64,
        lon: f64,
        cf_country: Option<&str>,
    ) -> Result<Option<ReverseResult>> {
        with_version_fallback!(self, "reverse", version, {
            self.try_reverse_geocode(version, lat, lon, cf_country)
                .await
        })
    }

    /// Attempt reverse geocode against a specific version.
    async fn try_reverse_geocode(
        &self,
        version: &str,
        lat: f64,
        lon: f64,
        cf_country: Option<&str>,
    ) -> Result<Option<ReverseResult>> {
        let reverse_collection = self.load_reverse_collection(version).await?;

        // Try country shard first if available (more specific data)
        if let Some(country) = cf_country {
            match self
                .query_reverse_shard(version, country, &reverse_collection, lat, lon)
                .await
            {
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
        self.query_reverse_shard(version, "HEAD", &reverse_collection, lat, lon)
            .await
    }

    async fn query_reverse_shard(
        &self,
        version: &str,
        shard_id: &str,
        collection: &StacCollection,
        lat: f64,
        lon: f64,
    ) -> Result<Option<ReverseResult>> {
        // Get item metadata from embedded items in reverse-collection.json
        let (shard_href, record_count) =
            if let Some(item) = self.get_embedded_item(collection, shard_id) {
                (item.href.clone(), item.record_count)
            } else {
                return Err(Error::RustError(format!(
                    "Reverse shard {} not found in collection",
                    shard_id
                )));
            };

        // Load the actual reverse shard database (cached)
        let shard_key = format!("{}/{}", version, shard_href.trim_start_matches("./"));

        let shard_bytes = self
            .cached_get(&shard_key, SHARD_CACHE_TTL)
            .await?
            .ok_or_else(|| Error::RustError(format!("Reverse shard {} not found", shard_key)))?;

        console_log!(
            "Loading reverse shard {} ({} bytes, {} records)",
            shard_id,
            shard_bytes.len(),
            record_count
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

    /// Look up a GERS ID to get its bounding box from a parquet shard.
    /// Falls back to older versions if the latest version's index is unavailable.
    pub async fn lookup_id(&self, gers_id: &str) -> Result<Option<IdLookupResult>> {
        with_version_fallback!(self, "id_lookup", version, {
            self.try_lookup_id(version, gers_id).await
        })
    }

    /// Attempt ID lookup against a specific version using range reads.
    ///
    /// Instead of fetching the entire parquet shard (~28 MB for prefix-len 3),
    /// this reads only the footer metadata + one matching row group (~1-3 MB):
    /// 1. Suffix read (32 KB) → get file size + parquet footer
    /// 2. Parse footer → find row group containing the target UUID via min/max stats
    /// 3. Range read just that row group's column data
    async fn try_lookup_id(&self, version: &str, gers_id: &str) -> Result<Option<IdLookupResult>> {
        let prefix_len = self.load_id_prefix_len(version).await?;

        // Compute shard prefix from GERS ID (remove hyphens, take first N hex chars)
        let hex_id: String = gers_id.replace('-', "").to_lowercase();
        if hex_id.len() < prefix_len {
            return Ok(None);
        }
        let prefix = &hex_id[..prefix_len];
        let shard_key = format!("{}/id-index/{}.parquet", version, prefix);

        // Parse target UUID early so we can fail fast
        let target = match parse_uuid_bytes(gers_id) {
            Some(t) => t,
            None => return Ok(None),
        };

        // Step 1: Suffix read to get footer + file size in one R2 call.
        // 32 KB covers any reasonable footer (~10 row groups × 5 columns ≈ 12 KB metadata).
        const FOOTER_SUFFIX_SIZE: u64 = 32768;
        let obj = self
            .bucket
            .get(&shard_key)
            .range(worker::Range::Suffix {
                suffix: FOOTER_SUFFIX_SIZE,
            })
            .execute()
            .await?;
        let obj = match obj {
            Some(o) => o,
            None => return Ok(None),
        };
        let file_size = obj.size();
        let body = obj
            .body()
            .ok_or_else(|| Error::RustError("Empty body on suffix read".into()))?;
        let tail_bytes = Bytes::from(body.bytes().await?);
        let tail_len = tail_bytes.len() as u64;
        let tail_offset = file_size - tail_len;

        // Step 2: Parse parquet footer from the tail bytes
        if tail_bytes.len() < 8 {
            return Err(Error::RustError("Shard too small for parquet".into()));
        }
        let footer_8_start = tail_bytes.len() - 8;
        let magic = &tail_bytes[footer_8_start + 4..];
        if magic != b"PAR1" {
            return Err(Error::RustError("Invalid parquet magic".into()));
        }
        let metadata_len = u32::from_le_bytes(
            tail_bytes[footer_8_start..footer_8_start + 4]
                .try_into()
                .unwrap(),
        ) as usize;
        if metadata_len + 8 > tail_bytes.len() {
            return Err(Error::RustError(format!(
                "Footer too large ({} bytes), only fetched {}",
                metadata_len + 8,
                tail_bytes.len()
            )));
        }
        let metadata_start = tail_bytes.len() - 8 - metadata_len;
        let metadata = parquet::file::metadata::ParquetMetaDataReader::decode_metadata(
            &tail_bytes[metadata_start..metadata_start + metadata_len],
        )
        .map_err(|e| Error::RustError(format!("Bad parquet metadata: {}", e)))?;

        // Step 3: Find matching row group via UUID column min/max statistics
        let num_row_groups = metadata.num_row_groups();
        let mut matching_rg: Option<usize> = None;
        for rg_idx in 0..num_row_groups {
            let rg_meta = metadata.row_group(rg_idx);
            if let Some(stats) = rg_meta.column(0).statistics() {
                if let (Some(min), Some(max)) = (stats.min_bytes_opt(), stats.max_bytes_opt()) {
                    if target.as_slice() >= min && target.as_slice() <= max {
                        matching_rg = Some(rg_idx);
                        break;
                    }
                }
            }
        }
        let rg_idx = match matching_rg {
            Some(idx) => idx,
            None => return Ok(None),
        };

        // Step 4: Compute byte range for the matching row group's columns
        let rg_meta = metadata.row_group(rg_idx);
        let num_columns = rg_meta.num_columns();
        let mut rg_start = u64::MAX;
        let mut rg_end = 0u64;
        for col_idx in 0..num_columns {
            let col_meta = rg_meta.column(col_idx);
            let col_offset = col_meta
                .dictionary_page_offset()
                .map(|o| o as u64)
                .unwrap_or(col_meta.data_page_offset() as u64);
            let col_end = col_offset + col_meta.compressed_size() as u64;
            rg_start = rg_start.min(col_offset);
            rg_end = rg_end.max(col_end);
        }
        let rg_length = rg_end - rg_start;

        console_log!(
            "ID lookup: shard={} file={}B rg={}/{} range={}..{} ({}B)",
            prefix,
            file_size,
            rg_idx,
            num_row_groups,
            rg_start,
            rg_end,
            rg_length
        );

        // Step 5: Fetch row group data (may already be in our tail buffer)
        let rg_bytes = if rg_start >= tail_offset && rg_end <= tail_offset + tail_bytes.len() as u64
        {
            // Row group is within our already-fetched tail (small file or last row group)
            let local_start = (rg_start - tail_offset) as usize;
            tail_bytes.slice(local_start..local_start + rg_length as usize)
        } else {
            // Range read just this row group from R2
            let obj = self
                .bucket
                .get(&shard_key)
                .range(worker::Range::OffsetWithLength {
                    offset: rg_start,
                    length: rg_length,
                })
                .execute()
                .await?
                .ok_or_else(|| Error::RustError("Row group fetch failed".into()))?;
            let body = obj
                .body()
                .ok_or_else(|| Error::RustError("Empty row group body".into()))?;
            Bytes::from(body.bytes().await?)
        };

        // Step 6: Build a RangeChunkReader backed by our pre-fetched ranges,
        // then use the standard parquet reader to iterate the matching row group.
        let chunk_reader = RangeChunkReader {
            file_size,
            chunks: vec![(rg_start, rg_bytes), (tail_offset, tail_bytes)],
        };
        let file_reader = SerializedFileReader::new(chunk_reader)
            .map_err(|e| Error::RustError(format!("Failed to create reader: {}", e)))?;
        let rg_reader = file_reader
            .get_row_group(rg_idx)
            .map_err(|e| Error::RustError(format!("Failed to read row group: {}", e)))?;
        let iter = rg_reader
            .get_row_iter(None)
            .map_err(|e| Error::RustError(format!("Failed to iterate: {}", e)))?;
        for row in iter {
            let row = row.map_err(|e| Error::RustError(format!("Row read error: {}", e)))?;
            let id_bytes = row
                .get_bytes(0)
                .map_err(|e| Error::RustError(format!("Bad UUID column: {}", e)))?;
            if id_bytes.data() == target.as_slice() {
                let bbox_xmin = row
                    .get_float(1)
                    .map_err(|e| Error::RustError(format!("Bad bbox: {}", e)))?
                    as f64;
                let bbox_ymin = row
                    .get_float(2)
                    .map_err(|e| Error::RustError(format!("Bad bbox: {}", e)))?
                    as f64;
                let bbox_xmax = row
                    .get_float(3)
                    .map_err(|e| Error::RustError(format!("Bad bbox: {}", e)))?
                    as f64;
                let bbox_ymax = row
                    .get_float(4)
                    .map_err(|e| Error::RustError(format!("Bad bbox: {}", e)))?
                    as f64;
                return Ok(Some(IdLookupResult {
                    id: gers_id.to_string(),
                    bbox: geocoder_core::BBox {
                        xmin: bbox_xmin,
                        ymin: bbox_ymin,
                        xmax: bbox_xmax,
                        ymax: bbox_ymax,
                    },
                }));
            }
        }
        Ok(None)
    }

    /// Load the ID index prefix_len from a small metadata file.
    /// Falls back to id-collection.json summaries if id-meta.json doesn't exist.
    async fn load_id_prefix_len(&self, version: &str) -> Result<usize> {
        // Try tiny metadata file first (avoids loading multi-MB collection)
        let meta_key = format!("{}/id-meta.json", version);
        if let Some(text) = self
            .cached_get_text(&meta_key, COLLECTION_CACHE_TTL)
            .await?
        {
            #[derive(Deserialize)]
            struct IdMeta {
                #[serde(default)]
                prefix_len: Option<u32>,
            }
            if let Ok(meta) = serde_json::from_str::<IdMeta>(&text) {
                if let Some(n) = meta.prefix_len {
                    return Ok(n as usize);
                }
            }
        }

        // Fallback: load id-collection.json and extract prefix_len via string search
        let key = format!("{}/id-collection.json", version);
        if let Some(text) = self.cached_get_text(&key, COLLECTION_CACHE_TTL).await? {
            if let Some(pos) = text.find("\"prefix_len\"") {
                let rest = &text[pos + "\"prefix_len\"".len()..];
                let rest = rest
                    .trim_start()
                    .strip_prefix(':')
                    .unwrap_or(rest)
                    .trim_start();
                let num_end = rest
                    .find(|c: char| !c.is_ascii_digit())
                    .unwrap_or(rest.len());
                if let Ok(n) = rest[..num_end].parse::<usize>() {
                    return Ok(n);
                }
            }
        }

        Ok(3) // default
    }

    /// Load the reverse collection for a given version.
    async fn load_reverse_collection(&self, version: &str) -> Result<StacCollection> {
        let key = format!("{}/reverse-collection.json", version);
        let text = self
            .cached_get_text(&key, COLLECTION_CACHE_TTL)
            .await?
            .ok_or_else(|| Error::RustError(format!("{} not found", key)))?;

        serde_json::from_str(&text)
            .map_err(|e| Error::RustError(format!("Failed to parse reverse collection: {}", e)))
    }

    async fn load_catalog(&self) -> Result<StacCatalog> {
        let text = self
            .cached_get_text("catalog.json", CATALOG_CACHE_TTL)
            .await?
            .ok_or_else(|| Error::RustError("catalog.json not found".into()))?;

        serde_json::from_str(&text)
            .map_err(|e| Error::RustError(format!("Failed to parse catalog: {}", e)))
    }

    /// Load a forward collection for a specific version.
    async fn load_collection(&self, version: &str) -> Result<StacCollection> {
        let key = format!("{}/collection.json", version);
        let text = self
            .cached_get_text(&key, COLLECTION_CACHE_TTL)
            .await?
            .ok_or_else(|| Error::RustError(format!("{} not found", key)))?;

        serde_json::from_str(&text)
            .map_err(|e| Error::RustError(format!("Failed to parse collection: {}", e)))
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

    /// Query a shard and return both results and debug info.
    async fn query_shard_with_info(
        &self,
        version: &str,
        shard_id: &str,
        collection: &StacCollection,
        query: &GeocoderQuery,
    ) -> Result<(Vec<GeocoderResult>, ShardDebugInfo)> {
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

        let shard_size = shard_bytes.len();

        console_log!(
            "Loading shard {} ({} bytes, {} records)",
            shard_id,
            shard_size,
            record_count
        );

        // Open the SQLite database from bytes and query it
        let db = Database::from_bytes(&shard_bytes)
            .map_err(|e| Error::RustError(format!("Failed to open shard database: {}", e)))?;

        let results = db
            .search(query)
            .map_err(|e| Error::RustError(format!("Search failed: {}", e)))?;

        let debug_info = ShardDebugInfo {
            id: shard_id.to_string(),
            size_bytes: shard_size,
            record_count,
        };

        Ok((results, debug_info))
    }
}

/// Extract ordered versions from catalog (latest first, then descending by version string).
///
/// Returns up to `MAX_VERSION_ATTEMPTS` versions so the caller can try each
/// in order until one succeeds.
fn get_ordered_versions(catalog: &StacCatalog) -> Vec<String> {
    let mut latest = None;
    let mut others: Vec<String> = Vec::new();

    for link in &catalog.links {
        if link.rel != "child" {
            continue;
        }
        let version = link
            .href
            .trim_start_matches("./")
            .split('/')
            .next()
            .unwrap_or("")
            .to_string();
        if version.is_empty() {
            continue;
        }
        if link.latest {
            latest = Some(version);
        } else {
            others.push(version);
        }
    }

    // Sort non-latest versions descending (date-based strings sort naturally)
    others.sort_unstable_by(|a, b| b.cmp(a));

    let mut versions = Vec::new();
    if let Some(v) = latest {
        versions.push(v);
    }
    versions.extend(others);
    versions.truncate(MAX_VERSION_ATTEMPTS);
    versions
}

/// Parse a GERS ID string into 16 UUID bytes.
///
/// Accepts both hyphenated ("08b2a100-d664-...") and plain ("08b2a100d664...") formats.
fn parse_uuid_bytes(gers_id: &str) -> Option<[u8; 16]> {
    let hex: String = gers_id.chars().filter(|c| *c != '-').collect();
    if hex.len() != 32 {
        return None;
    }
    let mut bytes = [0u8; 16];
    for i in 0..16 {
        bytes[i] = u8::from_str_radix(&hex[i * 2..i * 2 + 2], 16).ok()?;
    }
    Some(bytes)
}

/// A ChunkReader backed by pre-fetched byte ranges from R2.
///
/// Allows the standard parquet reader to operate on non-contiguous file regions
/// (e.g., a footer from the end of file + a single row group from the middle)
/// without fetching the entire file.
struct RangeChunkReader {
    file_size: u64,
    /// Pre-fetched ranges: (absolute_offset_in_file, data)
    chunks: Vec<(u64, Bytes)>,
}

impl Length for RangeChunkReader {
    fn len(&self) -> u64 {
        self.file_size
    }
}

impl ChunkReader for RangeChunkReader {
    type T = std::io::Cursor<Bytes>;

    fn get_read(&self, start: u64) -> parquet::errors::Result<Self::T> {
        for (offset, data) in &self.chunks {
            if start >= *offset && start < *offset + data.len() as u64 {
                let local_start = (start - *offset) as usize;
                return Ok(std::io::Cursor::new(data.slice(local_start..)));
            }
        }
        Err(parquet::errors::ParquetError::General(format!(
            "Range not pre-fetched: offset {}",
            start
        )))
    }

    fn get_bytes(&self, start: u64, length: usize) -> parquet::errors::Result<Bytes> {
        let end = start + length as u64;
        for (offset, data) in &self.chunks {
            let chunk_end = *offset + data.len() as u64;
            if start >= *offset && end <= chunk_end {
                let local_start = (start - *offset) as usize;
                return Ok(data.slice(local_start..local_start + length));
            }
        }
        Err(parquet::errors::ParquetError::General(format!(
            "Range not pre-fetched: {}..{}",
            start, end
        )))
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

    #[test]
    fn test_parse_uuid_bytes_hyphenated() {
        let bytes = parse_uuid_bytes("08b2a100-d664-7fff-0200-a44bcea04b76").unwrap();
        assert_eq!(
            bytes,
            [
                0x08, 0xb2, 0xa1, 0x00, 0xd6, 0x64, 0x7f, 0xff, 0x02, 0x00, 0xa4, 0x4b, 0xce, 0xa0,
                0x4b, 0x76
            ]
        );
    }

    #[test]
    fn test_parse_uuid_bytes_plain() {
        let bytes = parse_uuid_bytes("08b2a100d6647fff0200a44bcea04b76").unwrap();
        assert_eq!(
            bytes,
            [
                0x08, 0xb2, 0xa1, 0x00, 0xd6, 0x64, 0x7f, 0xff, 0x02, 0x00, 0xa4, 0x4b, 0xce, 0xa0,
                0x4b, 0x76
            ]
        );
    }

    #[test]
    fn test_parse_uuid_bytes_invalid() {
        assert!(parse_uuid_bytes("too-short").is_none());
        assert!(parse_uuid_bytes("").is_none());
        assert!(parse_uuid_bytes("zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz").is_none());
    }

    #[test]
    fn test_is_retriable_error_not_found() {
        let e = Error::RustError("2026-02-25.0/collection.json not found".into());
        assert!(is_retriable_error(&e));

        let e = Error::RustError("Shard 2026-02-25.0/HEAD.db not found".into());
        assert!(is_retriable_error(&e));

        let e = Error::RustError("Item 2026-02-25.0/items/US.json not found".into());
        assert!(is_retriable_error(&e));
    }

    #[test]
    fn test_is_retriable_error_operational() {
        // Operational errors should NOT be retriable
        let e = Error::RustError("Failed to open shard database: corrupt header".into());
        assert!(!is_retriable_error(&e));

        let e = Error::RustError("Search failed: FTS5 error".into());
        assert!(!is_retriable_error(&e));

        let e = Error::RustError("Failed to parse collection: invalid JSON".into());
        assert!(!is_retriable_error(&e));

        let e = Error::RustError("Reverse geocode failed: query error".into());
        assert!(!is_retriable_error(&e));
    }

    #[test]
    fn test_get_ordered_versions_latest_first() {
        let catalog = StacCatalog {
            links: vec![
                StacLink {
                    rel: "self".to_string(),
                    href: "./catalog.json".to_string(),
                    latest: false,
                },
                StacLink {
                    rel: "child".to_string(),
                    href: "./2025-12-25.0/collection.json".to_string(),
                    latest: false,
                },
                StacLink {
                    rel: "child".to_string(),
                    href: "./2026-02-25.0/collection.json".to_string(),
                    latest: true,
                },
                StacLink {
                    rel: "child".to_string(),
                    href: "./2026-01-25.0/collection.json".to_string(),
                    latest: false,
                },
            ],
        };

        let versions = get_ordered_versions(&catalog);
        assert_eq!(
            versions,
            vec!["2026-02-25.0", "2026-01-25.0", "2025-12-25.0"]
        );
    }

    #[test]
    fn test_get_ordered_versions_truncates_to_max() {
        let catalog = StacCatalog {
            links: vec![
                StacLink {
                    rel: "child".to_string(),
                    href: "./2025-10-25.0/collection.json".to_string(),
                    latest: false,
                },
                StacLink {
                    rel: "child".to_string(),
                    href: "./2025-11-25.0/collection.json".to_string(),
                    latest: false,
                },
                StacLink {
                    rel: "child".to_string(),
                    href: "./2025-12-25.0/collection.json".to_string(),
                    latest: false,
                },
                StacLink {
                    rel: "child".to_string(),
                    href: "./2026-01-25.0/collection.json".to_string(),
                    latest: false,
                },
                StacLink {
                    rel: "child".to_string(),
                    href: "./2026-02-25.0/collection.json".to_string(),
                    latest: true,
                },
            ],
        };

        let versions = get_ordered_versions(&catalog);
        assert_eq!(versions.len(), MAX_VERSION_ATTEMPTS);
        assert_eq!(versions[0], "2026-02-25.0"); // latest first
        assert_eq!(versions[1], "2026-01-25.0"); // then descending
        assert_eq!(versions[2], "2025-12-25.0");
    }

    #[test]
    fn test_get_ordered_versions_no_latest_flag() {
        let catalog = StacCatalog {
            links: vec![
                StacLink {
                    rel: "child".to_string(),
                    href: "./2026-01-25.0/collection.json".to_string(),
                    latest: false,
                },
                StacLink {
                    rel: "child".to_string(),
                    href: "./2026-02-25.0/collection.json".to_string(),
                    latest: false,
                },
            ],
        };

        let versions = get_ordered_versions(&catalog);
        // No latest flag, so just sorted descending
        assert_eq!(versions, vec!["2026-02-25.0", "2026-01-25.0"]);
    }

    #[test]
    fn test_get_ordered_versions_single_version() {
        let catalog = StacCatalog {
            links: vec![StacLink {
                rel: "child".to_string(),
                href: "./2026-02-25.0/collection.json".to_string(),
                latest: true,
            }],
        };

        let versions = get_ordered_versions(&catalog);
        assert_eq!(versions, vec!["2026-02-25.0"]);
    }

    #[test]
    fn test_get_ordered_versions_empty() {
        let catalog = StacCatalog {
            links: vec![StacLink {
                rel: "self".to_string(),
                href: "./catalog.json".to_string(),
                latest: false,
            }],
        };

        let versions = get_ordered_versions(&catalog);
        assert!(versions.is_empty());
    }
}
