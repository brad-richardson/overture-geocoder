//! STAC catalog loading and shard management with edge caching.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use bytes::Bytes;
use geocoder_core::{
    geo::haversine_distance, query::apply_location_bias, Database, GeocoderQuery, GeocoderResult,
    IdLookupResult, LocationBias, ReverseResult,
};
use parquet::file::reader::{ChunkReader, FileReader, Length, SerializedFileReader};
use parquet::record::RowAccessor;
use serde::{Deserialize, Serialize};
use worker::*;

// Cache TTLs for different resource types
const CATALOG_CACHE_TTL: u64 = 300; // 5 minutes - need fresh version pointers
                                    // SQLite shards and collection JSON under a {version}/ prefix are never
                                    // rewritten (versioned paths = natural invalidation), so cache them at
                                    // the edge for a week.
const IMMUTABLE_CACHE_TTL: u64 = 7 * 24 * 3600;
// The id-index is NOT immutable: patch-id-stage rebuilds
// {version}/id-index/*.parquet and re-uploads id-meta.json in place with no
// cache purge. A stale cached footer combined with a rewritten file yields
// garbage range reads (non-retriable 500s), so bound the exposure to an
// hour instead of a week.
const ID_INDEX_CACHE_TTL: u64 = 3600;

// Cache key prefix (uses custom domain for Cache API to work)
const CACHE_PREFIX: &str = "https://geocoder.bradr.dev/__cache/";

// Shard selection constants
const NEARBY_THRESHOLD_KM: f64 = 200.0; // Include shards within this distance
                                        // Max shards to load (excluding HEAD). Capped at 2: the user's own location
                                        // shard is always the closest, and every extra shard multiplies the
                                        // worst-case first-touch cost (R2 fetch + deserialize of a multi-MB file)
                                        // that dominates tail latency.
const MAX_LOCATION_SHARDS: usize = 2;
const MAX_VERSION_ATTEMPTS: usize = 4; // Max versions to try (latest + fallbacks); 4 keeps
                                       // the newest complete id-index reachable while fresher versions still
                                       // build. Retention (rebuild-r2-shards.yml) keeps only 2 versions on
                                       // the monthly cadence, so attempts beyond that usually have no
                                       // candidates — the headroom only pays off when extra versions exist
                                       // (e.g. same-day rebuilds).
const NEGATIVE_CACHE_TTL: u64 = 30; // 30 seconds - avoids hammering R2 for missing objects

// Isolate-level (in-memory) cache limits. Workers isolates persist across
// requests; keeping deserialized shard databases in memory lets warm
// requests skip the Cache API round trip and the SQLite deserialize copy.
const DB_CACHE_MAX_BYTES: usize = 64 * 1024 * 1024;
const DB_CACHE_MAX_ENTRIES: usize = 4;
// Catalog/collection JSON memo TTL. Short: this bounds how stale the
// version pointer can be within one isolate.
const TEXT_MEMO_TTL_MS: u64 = 60_000;

thread_local! {
    /// Deserialized shard databases keyed by versioned R2 key (immutable
    /// content). Vec ordered LRU-last; evicted by byte budget + entry count.
    static DB_CACHE: RefCell<Vec<(String, Rc<Database>, usize)>> =
        const { RefCell::new(Vec::new()) };
    /// Small JSON texts (catalog/collections/id-meta) with expiry timestamps.
    static TEXT_MEMO: RefCell<HashMap<String, (Option<String>, u64)>> =
        RefCell::new(HashMap::new());
}

/// Sentinel marking missing-resource errors. Version fallback and the
/// handlers' 503 mapping key off this exact marker rather than matching
/// arbitrary error prose (which risked false positives/negatives).
pub const NOT_FOUND_SENTINEL: &str = "[not-found]";

/// Build a missing-resource error that triggers version fallback.
fn not_found(what: impl std::fmt::Display) -> Error {
    Error::RustError(format!("{} {}", NOT_FOUND_SENTINEL, what))
}

/// User location derived from Cloudflare request headers.
#[derive(Debug, Clone, Default)]
pub struct UserLocation {
    pub country: Option<String>,
    pub region: Option<String>,
    /// ISO 3166-2 region code (CF-Region-Code), e.g. "GD" for Guangdong.
    /// Region shard IDs are "{country}-{region_code}", so this picks the
    /// user's own shard for region-sharded countries.
    pub region_code: Option<String>,
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
            region_code: headers.get("CF-Region-Code").ok().flatten(),
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
pub struct ShardLoader {
    bucket: Bucket,
    cache: Cache,
    /// Execution context for background cache writes via waitUntil.
    /// When absent, cache writes happen inline (slower, but correct).
    ctx: Option<Rc<Context>>,
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
/// Only missing-resource errors (built via [`not_found`]) are retriable — these
/// indicate a version whose data hasn't been fully deployed yet. Operational
/// errors (database corruption, query failures, parse errors) are surfaced
/// immediately to avoid silently serving stale data.
fn is_retriable_error(e: &Error) -> bool {
    format!("{:?}", e).contains(NOT_FOUND_SENTINEL)
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

impl ShardLoader {
    pub fn new(env: &Env) -> Result<Self> {
        let bucket = env.bucket("SHARDS_BUCKET")?;
        let cache = Cache::default();
        Ok(Self {
            bucket,
            cache,
            ctx: None,
        })
    }

    /// Create a loader that performs cache writes in the background via
    /// `waitUntil`, keeping multi-MB cache.put calls off the critical path.
    pub fn with_context(env: &Env, ctx: Rc<Context>) -> Result<Self> {
        let mut loader = Self::new(env)?;
        loader.ctx = Some(ctx);
        Ok(loader)
    }

    /// Write bytes to the edge cache, off the critical path via `waitUntil`
    /// when an execution context is available (best effort either way;
    /// inline await otherwise). Takes `Bytes` so the caller only pays a
    /// refcount bump: the full body copy a `Response` requires happens
    /// inside the deferred future, not before returning to the user.
    async fn cache_put_bytes_background(&self, cache_key: String, bytes: Bytes, ttl: u64) {
        let put = async move {
            let result: Result<()> = async {
                let headers = Headers::new();
                headers.set("Cache-Control", &format!("s-maxage={}", ttl))?;
                headers.set("Content-Type", "application/octet-stream")?;
                let response = Response::from_bytes(bytes.to_vec())?.with_headers(headers);
                let request = Request::new(&cache_key, Method::Get)?;
                Cache::default().put(&request, response).await
            }
            .await;
            if let Err(e) = result {
                console_log!("Cache PUT failed for {}: {:?}", cache_key, e);
            }
        };
        match &self.ctx {
            Some(ctx) => ctx.wait_until(put),
            None => put.await,
        }
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
    async fn cached_get(&self, key: &str, ttl: u64) -> Result<Option<Bytes>> {
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
            return Ok(Some(Bytes::from(bytes)));
        }

        console_log!("Cache MISS: {}", key);

        // Fetch from R2
        let obj = self.bucket.get(key).execute().await?;
        if let Some(obj) = obj {
            let body = obj
                .body()
                .ok_or_else(|| Error::RustError("Empty object".into()))?;
            // Bytes: the cache write below only bumps a refcount here; the
            // multi-MB body copy happens inside the deferred future.
            let bytes = Bytes::from(body.bytes().await?);
            self.cache_put_bytes_background(cache_key, bytes.clone(), ttl)
                .await;
            return Ok(Some(bytes));
        }

        // Cache the negative result (empty body sentinel) with a short TTL to avoid
        // repeated R2 GETs for objects that don't exist yet during deployments
        self.cache_put_bytes_background(cache_key, Bytes::new(), NEGATIVE_CACHE_TTL)
            .await;

        Ok(None)
    }

    /// Range-read part of an R2 object with edge caching (id-index TTL:
    /// patch runs can rewrite these files in place).
    /// Used for parquet row groups, which are re-read often by ID lookups.
    async fn cached_range_read(
        &self,
        key: &str,
        offset: u64,
        length: u64,
    ) -> Result<Option<Bytes>> {
        let cache_key = format!("{}{}__r{}-{}", CACHE_PREFIX, key, offset, length);

        let request = Request::new(&cache_key, Method::Get)?;
        if let Some(mut response) = self.cache.get(&request, false).await? {
            let bytes = response.bytes().await?;
            if bytes.is_empty() {
                return Ok(None);
            }
            console_log!("Cache HIT range: {} ({}..{})", key, offset, offset + length);
            return Ok(Some(Bytes::from(bytes)));
        }

        let obj = self
            .bucket
            .get(key)
            .range(worker::Range::OffsetWithLength { offset, length })
            .execute()
            .await?;
        let Some(obj) = obj else {
            self.cache_put_bytes_background(cache_key, Bytes::new(), NEGATIVE_CACHE_TTL)
                .await;
            return Ok(None);
        };
        let body = obj
            .body()
            .ok_or_else(|| Error::RustError("Empty range body".into()))?;
        let bytes = Bytes::from(body.bytes().await?);
        self.cache_put_bytes_background(cache_key, bytes.clone(), ID_INDEX_CACHE_TTL)
            .await;

        Ok(Some(bytes))
    }

    /// Fetch small JSON text with an isolate-level memo in front of the edge
    /// cache. Saves a Cache API round trip per request for catalog/collection
    /// metadata; the memo TTL bounds staleness within an isolate.
    async fn memoized_get_text(&self, key: &str, ttl: u64) -> Result<Option<String>> {
        let now = Date::now().as_millis();
        let memoized = TEXT_MEMO.with(|memo| {
            memo.borrow()
                .get(key)
                .filter(|(_, expires)| *expires > now)
                .map(|(text, _)| text.clone())
        });
        if let Some(text) = memoized {
            return Ok(text);
        }

        let text = self.cached_get_text(key, ttl).await?;
        TEXT_MEMO.with(|memo| {
            let mut memo = memo.borrow_mut();
            // Bound the memo: drop expired entries when it grows.
            if memo.len() > 64 {
                memo.retain(|_, (_, expires)| *expires > now);
            }
            memo.insert(key.to_string(), (text.clone(), now + TEXT_MEMO_TTL_MS));
        });
        Ok(text)
    }

    /// Fetch text from R2 with caching.
    async fn cached_get_text(&self, key: &str, ttl: u64) -> Result<Option<String>> {
        match self.cached_get(key, ttl).await? {
            Some(bytes) => {
                let text = std::str::from_utf8(&bytes)
                    .map_err(|e| Error::RustError(format!("Invalid UTF-8: {}", e)))?;
                Ok(Some(text.to_owned()))
            }
            None => Ok(None),
        }
    }

    /// Fetch parquet suffix (footer) from R2 with edge caching.
    ///
    /// Returns (file_size, tail_bytes) on success, None if the object doesn't exist.
    /// The cache value is: 8 bytes (file_size as u64 LE) + raw suffix bytes.
    /// `suffix_size` defaults to 32KB which covers typical parquet footers;
    /// callers retry with a larger size when the footer overflows it.
    async fn cached_suffix_read(
        &self,
        key: &str,
        suffix_size: u64,
    ) -> Result<Option<(u64, Bytes)>> {
        let cache_key = format!("{}{}__suffix{}", CACHE_PREFIX, key, suffix_size);

        // Try cache first
        let request = Request::new(&cache_key, Method::Get)?;
        if let Some(mut response) = self.cache.get(&request, false).await? {
            let bytes = response.bytes().await?;
            if bytes.is_empty() {
                console_log!("Cache HIT (negative suffix): {}", key);
                return Ok(None);
            }
            if bytes.len() > 8 {
                let file_size = u64::from_le_bytes(bytes[..8].try_into().unwrap());
                let tail_bytes = Bytes::from(bytes[8..].to_vec());
                console_log!("Cache HIT suffix: {} ({}B)", key, tail_bytes.len());
                return Ok(Some((file_size, tail_bytes)));
            }
        }

        console_log!("Cache MISS suffix: {}", key);

        // Fetch suffix from R2
        let obj = self
            .bucket
            .get(key)
            .range(worker::Range::Suffix {
                suffix: suffix_size,
            })
            .execute()
            .await?;
        let obj = match obj {
            Some(o) => o,
            None => {
                // Negative cache
                self.cache_put_bytes_background(cache_key, Bytes::new(), NEGATIVE_CACHE_TTL)
                    .await;
                return Ok(None);
            }
        };
        let file_size = obj.size();
        let body = obj
            .body()
            .ok_or_else(|| Error::RustError("Empty body on suffix read".into()))?;
        let tail_bytes = Bytes::from(body.bytes().await?);

        // Store in cache: 8 bytes file_size + suffix bytes
        let mut cache_bytes = Vec::with_capacity(8 + tail_bytes.len());
        cache_bytes.extend_from_slice(&file_size.to_le_bytes());
        cache_bytes.extend_from_slice(&tail_bytes);
        self.cache_put_bytes_background(cache_key, Bytes::from(cache_bytes), ID_INDEX_CACHE_TTL)
            .await;

        Ok(Some((file_size, tail_bytes)))
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

        // Select nearby shards based on user location
        let nearby_shards = self.select_nearby_shards(&collection, user_location);
        console_log!("Selected shards: {:?}", nearby_shards);

        // Load and query HEAD + nearby shards concurrently so the network
        // fetches overlap instead of summing (the SQLite queries themselves
        // are CPU work and serialize naturally on the single wasm thread).
        let mut shard_ids = vec!["HEAD".to_string()];
        shard_ids.extend(nearby_shards);
        let outcomes = futures::future::join_all(
            shard_ids
                .iter()
                .map(|shard_id| self.query_shard_with_info(version, shard_id, &collection, query)),
        )
        .await;

        let mut all_results = Vec::new();
        let mut shards_loaded = Vec::new();
        for (shard_id, outcome) in shard_ids.iter().zip(outcomes) {
            match outcome {
                Ok((results, info)) => {
                    all_results.extend(results);
                    if include_debug {
                        shards_loaded.push(info);
                    }
                }
                // HEAD shard is required - failure triggers version fallback
                Err(e) if shard_id == "HEAD" => return Err(e),
                Err(e) => {
                    console_log!("Warning: shard {} unavailable: {:?}", shard_id, e);
                }
            }
        }

        // Sort by composed importance before deduplication. Match quality
        // ("Paris" above "Parish") is already part of the per-result score
        // computed in Database::search, so no separate exact-match bonus.
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
            return self.select_shards_for_country(
                collection,
                country,
                user_location.region_code.as_deref(),
            );
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
    fn select_shards_for_country(
        &self,
        collection: &StacCollection,
        country: &str,
        region_code: Option<&str>,
    ) -> Vec<String> {
        // Check if country is region-sharded
        if let Some(regions) = collection.region_sharded.get(country) {
            // Prefer the user's own region shard: the region list is build
            // order, so taking the first N without this would load ~2 of
            // ~30 arbitrary regions and miss the user's own almost always.
            if let Some(code) = region_code {
                let want = format!("{}-{}", country, code);
                if let Some(shard) = regions.iter().find(|r| **r == want) {
                    return vec![shard.clone()];
                }
            }
            // Region unknown: load the first shards up to the cap (HEAD
            // still covers prominent places).
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

    /// Load a shard database, preferring the isolate-level cache, then the
    /// edge cache, then R2. Returns the database and its serialized size.
    async fn load_shard_db(&self, shard_key: &str) -> Result<(Rc<Database>, usize)> {
        let cached = DB_CACHE.with(|c| {
            let mut cache = c.borrow_mut();
            cache
                .iter()
                .position(|(k, _, _)| k == shard_key)
                .map(|pos| {
                    // Move to MRU position (end of the Vec)
                    let entry = cache.remove(pos);
                    let hit = (Rc::clone(&entry.1), entry.2);
                    cache.push(entry);
                    hit
                })
        });
        if let Some((db, size)) = cached {
            console_log!("DB cache HIT: {}", shard_key);
            return Ok((db, size));
        }

        let shard_bytes = self
            .cached_get(shard_key, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(format!("shard {}", shard_key)))?;
        let size = shard_bytes.len();

        let db = Rc::new(Database::from_bytes(&shard_bytes).map_err(|e| {
            Error::RustError(format!(
                "Failed to open shard database {}: {}",
                shard_key, e
            ))
        })?);
        drop(shard_bytes);

        DB_CACHE.with(|c| {
            let mut cache = c.borrow_mut();
            // A concurrent request that missed at the same time may have
            // inserted this key while we awaited the fetch; a duplicate
            // entry would double-count against the byte budget.
            if !cache.iter().any(|(k, _, _)| k == shard_key) {
                cache.push((shard_key.to_string(), Rc::clone(&db), size));
                // Evict from the LRU end; always keep the entry just inserted.
                while cache.len() > 1
                    && (cache.len() > DB_CACHE_MAX_ENTRIES
                        || cache.iter().map(|(_, _, s)| *s).sum::<usize>() > DB_CACHE_MAX_BYTES)
                {
                    let (evicted, _, _) = cache.remove(0);
                    console_log!("DB cache evict: {}", evicted);
                }
            }
        });

        Ok((db, size))
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
        let shard_href = self
            .get_embedded_item(collection, shard_id)
            .map(|item| item.href.clone())
            .ok_or_else(|| not_found(format!("reverse shard {} in collection", shard_id)))?;

        let shard_key = format!("{}/{}", version, shard_href.trim_start_matches("./"));
        let (db, _) = self.load_shard_db(&shard_key).await?;

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

        // Compute shard prefix from GERS ID (remove hyphens, take first N hex
        // chars). get() rather than slicing: a multi-byte character in the
        // path param would otherwise panic on a non-char boundary and abort
        // the whole wasm isolate.
        let hex_id: String = gers_id.replace('-', "").to_lowercase();
        let Some(prefix) = hex_id.get(..prefix_len) else {
            return Ok(None);
        };
        let shard_key = format!("{}/id-index/{}.parquet", version, prefix);

        // Parse target UUID early so we can fail fast
        let target = match parse_uuid_bytes(gers_id) {
            Some(t) => t,
            None => return Ok(None),
        };

        // Step 1: Suffix read to get footer + file size (cached at edge).
        // Missing shard is reported as a retriable error (not Ok(None)) so the
        // version-fallback macro retries the prior version. This handles the
        // window where catalog.json points at a new version whose id-index
        // parquets haven't finished uploading yet.
        const FOOTER_SUFFIX_SIZE: u64 = 32768;
        let (file_size, mut tail_bytes) = match self
            .cached_suffix_read(&shard_key, FOOTER_SUFFIX_SIZE)
            .await?
        {
            Some(result) => result,
            None => return Err(not_found(format!("id-index shard {}", shard_key))),
        };

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
        // Sanity-cap before acting on the length: a corrupt (or stale-cached)
        // 4-byte footer field of up to ~4 GB would otherwise trigger a
        // whole-file suffix fetch, buffered in memory and edge-cached.
        const MAX_FOOTER_SIZE: usize = 16 * 1024 * 1024;
        if metadata_len > MAX_FOOTER_SIZE || (metadata_len + 8) as u64 > file_size {
            return Err(Error::RustError(format!(
                "Implausible parquet footer length {}B for {} ({}B file)",
                metadata_len, shard_key, file_size
            )));
        }
        if metadata_len + 8 > tail_bytes.len() {
            // Footer larger than the default suffix window: re-read with the
            // exact size (cached under a size-specific key).
            console_log!(
                "Footer {}B exceeds {}B window for {}, re-reading",
                metadata_len + 8,
                tail_bytes.len(),
                shard_key
            );
            tail_bytes = match self
                .cached_suffix_read(&shard_key, (metadata_len + 8) as u64)
                .await?
            {
                Some((_, bytes)) => bytes,
                None => return Err(not_found(format!("id-index shard {}", shard_key))),
            };
            if metadata_len + 8 > tail_bytes.len() {
                return Err(Error::RustError(format!(
                    "Footer too large ({} bytes), fetched {}",
                    metadata_len + 8,
                    tail_bytes.len()
                )));
            }
        }
        let tail_len = tail_bytes.len() as u64;
        let tail_offset = file_size - tail_len;
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
            // Range read just this row group, edge-cached: bulk ID resolvers
            // hit the same row group repeatedly and shouldn't re-pay R2.
            self.cached_range_read(&shard_key, rg_start, rg_length)
                .await?
                .ok_or_else(|| not_found(format!("id-index shard {}", shard_key)))?
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
            // Rows are sorted by UUID; once past the target it can't appear.
            if id_bytes.data() > target.as_slice() {
                return Ok(None);
            }
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
        // Try tiny metadata file first (avoids loading multi-MB collection).
        // id-index TTL: patch runs re-upload these files in place.
        let meta_key = format!("{}/id-meta.json", version);
        if let Some(text) = self
            .memoized_get_text(&meta_key, ID_INDEX_CACHE_TTL)
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
        if let Some(text) = self.memoized_get_text(&key, ID_INDEX_CACHE_TTL).await? {
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

        // Both metadata files missing: the id-index isn't deployed for this
        // version. Surface a retriable not-found (so version fallback engages)
        // rather than guessing a shard layout and returning clean 404s.
        Err(not_found(format!(
            "id-index metadata for version {}",
            version
        )))
    }

    /// Load the reverse collection for a given version.
    async fn load_reverse_collection(&self, version: &str) -> Result<StacCollection> {
        let key = format!("{}/reverse-collection.json", version);
        let text = self
            .memoized_get_text(&key, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(&key))?;

        serde_json::from_str(&text)
            .map_err(|e| Error::RustError(format!("Failed to parse reverse collection: {}", e)))
    }

    async fn load_catalog(&self) -> Result<StacCatalog> {
        let text = self
            .memoized_get_text("catalog.json", CATALOG_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found("catalog.json"))?;

        serde_json::from_str(&text)
            .map_err(|e| Error::RustError(format!("Failed to parse catalog: {}", e)))
    }

    /// Load a forward collection for a specific version.
    async fn load_collection(&self, version: &str) -> Result<StacCollection> {
        let key = format!("{}/collection.json", version);
        let text = self
            .memoized_get_text(&key, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(&key))?;

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
                    .cached_get_text(&item_key, IMMUTABLE_CACHE_TTL)
                    .await?
                    .ok_or_else(|| not_found(format!("item {}", item_key)))?;

                let item: StacItem = serde_json::from_str(&item_text)
                    .map_err(|e| Error::RustError(format!("Failed to parse item: {}", e)))?;

                (item.assets.data.href.clone(), item.properties.record_count)
            };

        // Load the shard database (isolate cache -> edge cache -> R2)
        let shard_key = format!("{}/{}", version, shard_href.trim_start_matches("./"));
        let (db, shard_size) = self.load_shard_db(&shard_key).await?;

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

    // Sort non-latest versions descending. The .N suffix compares
    // numerically: plain string order would rank 2026-02-25.9 above
    // 2026-02-25.10.
    others.sort_unstable_by(|a, b| version_sort_key(b).cmp(&version_sort_key(a)));

    let mut versions = Vec::new();
    if let Some(v) = latest {
        versions.push(v);
    }
    versions.extend(others);
    versions.truncate(MAX_VERSION_ATTEMPTS);
    versions
}

/// Sort key for "{YYYY-MM-DD}.{N}" version strings: ISO date part compares
/// lexicographically, the .N suffix numerically. Version strings without a
/// numeric suffix compare whole-string with suffix 0.
fn version_sort_key(version: &str) -> (&str, u64) {
    match version.rsplit_once('.') {
        Some((date, n)) => match n.parse::<u64>() {
            Ok(n) => (date, n),
            Err(_) => (version, 0),
        },
        None => (version, 0),
    }
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
        // Anything built via not_found() must trigger version fallback —
        // including the id-index case, where a version's parquet hasn't
        // been uploaded yet.
        assert!(is_retriable_error(&not_found(
            "2026-02-25.0/collection.json"
        )));
        assert!(is_retriable_error(&not_found("shard 2026-02-25.0/HEAD.db")));
        assert!(is_retriable_error(&not_found(
            "id-index shard 2026-04-25.0/id-index/abc.parquet"
        )));
    }

    #[test]
    fn test_is_retriable_error_operational() {
        // Operational errors should NOT be retriable — even when their
        // prose happens to contain "not found".
        let e = Error::RustError("Failed to open shard database: corrupt header".into());
        assert!(!is_retriable_error(&e));

        let e = Error::RustError("Search failed: FTS5 error".into());
        assert!(!is_retriable_error(&e));

        let e = Error::RustError("Failed to parse collection: invalid JSON".into());
        assert!(!is_retriable_error(&e));

        let e = Error::RustError("R2 backend error: object not found in cache tier".into());
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
    fn test_get_ordered_versions_numeric_suffix_order() {
        // Lexicographic order would rank .9 above .10.
        let catalog = StacCatalog {
            links: ["./2026-02-25.9/x", "./2026-02-25.10/x", "./2026-02-25.2/x"]
                .iter()
                .map(|href| StacLink {
                    rel: "child".to_string(),
                    href: href.to_string(),
                    latest: false,
                })
                .collect(),
        };

        let versions = get_ordered_versions(&catalog);
        assert_eq!(
            versions,
            vec!["2026-02-25.10", "2026-02-25.9", "2026-02-25.2"]
        );
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
