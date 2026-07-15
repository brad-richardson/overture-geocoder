//! STAC catalog loading and shard management with edge caching.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use bytes::Bytes;
use geocoder_core::{
    geo::haversine_distance, query::apply_location_bias, Database, GeocoderQuery, GeocoderResult,
    IdLocatorMetadata, IdLookupResult, LocationBias, ReverseResult,
};
use parquet::file::reader::{ChunkReader, FileReader, Length, SerializedFileReader};
use parquet::record::RowAccessor;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use worker::*;

use crate::address_pages::{
    decode_useful_gzip_range, parse_useful_gzip_header, AddressPageIndex, AddressPageRecord,
    MAX_INDEX_BYTES,
};

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
const MAX_LOCATION_SHARDS: usize = 2;
const MAX_ROUTER_SHARDS: usize = 2;
// Total extra shards beyond HEAD, including opt-in Places. Previously HEAD+2
// (nearby only); now HEAD+3 to accommodate suffix + router + nearby while still bounding
// worst-case first-touch cost (additional R2 fetch + deserialize). Tail
// latency increase is acceptable for the recall improvement.
const MAX_EXTRA_SHARDS: usize = 3;
const MAX_PLACES_SHARDS: usize = 2;
const MAX_REVERSE_ROUTING_DIAGNOSTIC_CANDIDATES: usize = 8;
const MAX_VERSION_ATTEMPTS: usize = 4; // Max versions to try (latest + fallbacks); 4 keeps
                                       // the newest complete id-index reachable while fresher versions still
                                       // build. Retention (rebuild-r2-shards.yml) keeps only the newest 2
                                       // versions, so attempts beyond that usually have no candidates — the
                                       // headroom only pays off in transient windows (e.g. a stale cached
                                       // catalog still offering a just-pruned version).
const NEGATIVE_CACHE_TTL: u64 = 30; // 30 seconds - avoids hammering R2 for missing objects

// Isolate-level (in-memory) cache limits. Workers isolates persist across
// requests; keeping deserialized shard databases in memory lets warm
// requests skip the Cache API round trip and the SQLite deserialize copy.
const DB_CACHE_MAX_BYTES: usize = 64 * 1024 * 1024;
const DB_CACHE_MAX_ENTRIES: usize = 4;
const LOCATOR_DICTIONARY_CACHE_MAX_ENTRIES: usize = 2;
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
    static ROUTER_CACHE: RefCell<Vec<(String, Rc<RouterDb>, usize)>> =
        const { RefCell::new(Vec::new()) };
    /// Parsed immutable locator dictionaries. Raw JSON is separately edge-
    /// cached; this avoids reparsing and reallocating ~100 KiB on every hit.
    static LOCATOR_DICTIONARY_CACHE: RefCell<Vec<(String, Rc<LocatorDictionary>)>> =
        const { RefCell::new(Vec::new()) };
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
    pub version: String,
}

pub struct ReverseSearchResult {
    pub result: Option<ReverseResult>,
    pub version: String,
    pub routing: ReverseRoutingDebug,
}

/// Bounded, coordinate-free diagnostics for reverse country selection.
///
/// Candidate IDs are useful for reproducing aggregate-bbox overlaps, but the
/// list is capped so a malformed collection cannot inflate a response or log.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ReverseRoutingDebug {
    pub country_decision: ReverseCountryDecision,
    pub outcome: ReverseRoutingOutcome,
    pub bbox_candidate_count: usize,
    pub bbox_candidates: Vec<String>,
    pub selected_country: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ReverseCountryDecision {
    UniqueBbox,
    AmbiguousBbox,
    Unresolved,
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ReverseRoutingOutcome {
    CountryShard,
    GlobalFallback,
    Unresolved,
}

struct ReverseRouteSelection {
    shards: Vec<String>,
    debug: ReverseRoutingDebug,
}

pub struct IdSearchResult {
    pub result: Option<IdLookupResult>,
    pub version: String,
}

#[derive(Debug, Clone)]
struct IdIndexConfig {
    prefix_len: usize,
    format_version: u32,
    overture_release: Option<String>,
    locator_dictionary: Option<LocatorDictionaryReference>,
}

#[derive(Debug, Clone, Deserialize)]
struct LocatorDictionaryReference {
    href: String,
    sha256: String,
    size_bytes: usize,
    dictionary_version: u32,
    source_files_count: usize,
    last_seen_releases_count: usize,
    source_file_id_bounds: Option<[u32; 2]>,
    last_seen_release_id_bounds: Option<[u32; 2]>,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
struct SourceFileEntry {
    theme: String,
    feature_type: String,
    filename: String,
}

#[derive(Debug, Clone, Deserialize)]
struct LocatorDictionary {
    format_version: u32,
    dictionary_version: u32,
    overture_release: String,
    type_theme_map: TypeThemeMap,
    source_files: Vec<SourceFileEntry>,
    last_seen_releases: Vec<String>,
    source_files_count: usize,
    last_seen_releases_count: usize,
    source_file_id_bounds: Option<[u32; 2]>,
    last_seen_release_id_bounds: Option<[u32; 2]>,
}

#[derive(Debug, Clone, Deserialize)]
struct TypeThemeMap {
    version: u32,
    types: HashMap<String, String>,
}

fn parse_id_index_config(text: &str) -> std::result::Result<IdIndexConfig, String> {
    let root: serde_json::Value =
        serde_json::from_str(text).map_err(|e| format!("invalid JSON: {e}"))?;
    let values = root.get("summaries").unwrap_or(&root);
    let prefix_len = values
        .get("prefix_len")
        .and_then(|v| v.as_u64())
        .ok_or_else(|| "missing integer prefix_len".to_string())? as usize;
    let format_version = match values.get("format_version") {
        None => 1,
        Some(value) => value
            .as_u64()
            .and_then(|value| u32::try_from(value).ok())
            .ok_or_else(|| "format_version must be an unsigned 32-bit integer".to_string())?,
    };

    let (overture_release, locator_dictionary) = if format_version == 3 {
        let release = values
            .get("overture_release")
            .and_then(|value| value.as_str())
            .filter(|value| !value.is_empty())
            .ok_or_else(|| "format v3 requires overture_release".to_string())?
            .to_string();
        let reference: LocatorDictionaryReference = serde_json::from_value(
            values
                .get("locator_dictionary")
                .cloned()
                .ok_or_else(|| "format v3 requires locator_dictionary".to_string())?,
        )
        .map_err(|e| format!("invalid locator dictionary reference: {e}"))?;
        validate_dictionary_reference(&reference)?;
        (Some(release), Some(reference))
    } else if format_version == 1 {
        (None, None)
    } else {
        return Err(format!(
            "unsupported ID-index format_version {format_version}"
        ));
    };

    Ok(IdIndexConfig {
        prefix_len,
        format_version,
        overture_release,
        locator_dictionary,
    })
}

fn validate_dictionary_reference(
    reference: &LocatorDictionaryReference,
) -> std::result::Result<(), String> {
    let expected_href = format!("./id-locator-dictionary-{}.json", reference.sha256);
    if reference.dictionary_version != 1
        || reference.sha256.len() != 64
        || !reference
            .sha256
            .bytes()
            .all(|byte| b"0123456789abcdef".contains(&byte))
        || reference.href != expected_href
        || reference.size_bytes == 0
        || reference.size_bytes > 1024 * 1024
        || reference.source_files_count > 65_535
        || reference.last_seen_releases_count > 65_535
    {
        return Err("malformed locator dictionary reference".to_string());
    }
    validate_bounds(
        reference.source_file_id_bounds,
        reference.source_files_count,
    )?;
    validate_bounds(
        reference.last_seen_release_id_bounds,
        reference.last_seen_releases_count,
    )?;
    Ok(())
}

fn validate_bounds(bounds: Option<[u32; 2]>, count: usize) -> std::result::Result<(), String> {
    let expected = if count == 0 {
        None
    } else {
        Some([1, count as u32])
    };
    if bounds != expected {
        return Err("locator dictionary bounds/count mismatch".to_string());
    }
    Ok(())
}

const MAX_PARQUET_FOOTER_SIZE: usize = 16 * 1024 * 1024;

fn parquet_footer_metadata_len(tail_bytes: &[u8]) -> std::result::Result<usize, String> {
    if tail_bytes.len() < 8 {
        return Err("shard too small for parquet footer".to_string());
    }
    let trailer_start = tail_bytes.len() - 8;
    if &tail_bytes[trailer_start + 4..] != b"PAR1" {
        return Err("invalid parquet magic".to_string());
    }
    Ok(u32::from_le_bytes(
        tail_bytes[trailer_start..trailer_start + 4]
            .try_into()
            .expect("four-byte parquet footer length"),
    ) as usize)
}

/// Validate a footer length read from Parquet's final eight bytes and decide
/// whether the Worker's initial suffix needs one exact-size retry.
///
/// `tail_len` is the number of bytes actually returned by the initial suffix
/// request (which can be smaller than 32 KiB for a small object).
fn footer_retry_size(
    file_size: u64,
    tail_len: usize,
    metadata_len: usize,
) -> std::result::Result<Option<u64>, String> {
    let footer_size = metadata_len
        .checked_add(8)
        .ok_or_else(|| "parquet footer length overflow".to_string())?;
    if tail_len as u64 > file_size
        || metadata_len > MAX_PARQUET_FOOTER_SIZE
        || footer_size as u64 > file_size
    {
        return Err(format!(
            "implausible parquet footer length {metadata_len}B for {file_size}B file"
        ));
    }
    Ok((footer_size > tail_len).then_some(footer_size as u64))
}

fn validate_footer_retry_response(
    expected_file_size: u64,
    expected_metadata_len: usize,
    actual_file_size: u64,
    tail_bytes: &[u8],
) -> std::result::Result<(), String> {
    if actual_file_size != expected_file_size {
        return Err("parquet object changed size between footer reads".to_string());
    }
    let actual_metadata_len = parquet_footer_metadata_len(tail_bytes)?;
    if actual_metadata_len != expected_metadata_len {
        return Err("parquet footer changed between suffix reads".to_string());
    }
    if footer_retry_size(actual_file_size, tail_bytes.len(), actual_metadata_len)?.is_some() {
        return Err("exact parquet footer retry returned too few bytes".to_string());
    }
    Ok(())
}

fn compact_locator_ids(
    row: &parquet::record::Row,
    format_version: u32,
) -> Option<(Option<u16>, Option<u16>, bool)> {
    if format_version != 3 || row.len() < 8 {
        return None;
    }
    let source_raw = row.get_int(5).ok();
    let release_raw = row.get_int(6).ok();
    // Check physical nullity before conversion. Otherwise a corrupt negative
    // or overflowing value could be discarded and make a both-present row
    // look like a valid current or historical locator.
    if source_raw.is_some() == release_raw.is_some() {
        return None;
    }
    let source_file_id = source_raw
        .and_then(|value| u16::try_from(value).ok())
        .filter(|value| *value != 0);
    let last_seen_release_id = release_raw
        .and_then(|value| u16::try_from(value).ok())
        .filter(|value| *value != 0);
    if source_file_id.is_none() && last_seen_release_id.is_none() {
        return None;
    }
    let registry_member = row.get_bool(7).ok()?;
    Some((source_file_id, last_seen_release_id, registry_member))
}

fn validate_locator_dictionary(
    dictionary: &LocatorDictionary,
    reference: &LocatorDictionaryReference,
    expected_release: &str,
) -> std::result::Result<(), String> {
    if dictionary.format_version != 3
        || dictionary.dictionary_version != 1
        || dictionary.overture_release != expected_release
        || dictionary.type_theme_map.version != 1
        || dictionary.source_files.len() != dictionary.source_files_count
        || dictionary.last_seen_releases.len() != dictionary.last_seen_releases_count
        || dictionary.source_files_count != reference.source_files_count
        || dictionary.last_seen_releases_count != reference.last_seen_releases_count
    {
        return Err("locator dictionary contract mismatch".to_string());
    }
    validate_bounds(
        dictionary.source_file_id_bounds,
        dictionary.source_files_count,
    )?;
    validate_bounds(
        dictionary.last_seen_release_id_bounds,
        dictionary.last_seen_releases_count,
    )?;
    if dictionary.source_file_id_bounds != reference.source_file_id_bounds
        || dictionary.last_seen_release_id_bounds != reference.last_seen_release_id_bounds
    {
        return Err("locator dictionary reference bounds mismatch".to_string());
    }
    if dictionary
        .source_files
        .windows(2)
        .any(|pair| pair[0] >= pair[1])
        || dictionary
            .last_seen_releases
            .windows(2)
            .any(|pair| pair[0] >= pair[1])
    {
        return Err("locator dictionaries are not strictly sorted and unique".to_string());
    }
    for entry in &dictionary.source_files {
        if entry.filename.is_empty()
            || entry.filename.len() > 255
            || entry.filename.contains('/')
            || entry.filename.contains('\\')
            || !entry.filename.ends_with(".parquet")
            || dictionary.type_theme_map.types.get(&entry.feature_type) != Some(&entry.theme)
        {
            return Err("invalid source-file dictionary entry".to_string());
        }
    }
    if dictionary.last_seen_releases.iter().any(String::is_empty) {
        return Err("invalid last-seen release dictionary entry".to_string());
    }
    Ok(())
}

fn build_locator_metadata(
    source_file_id: Option<u16>,
    last_seen_release_id: Option<u16>,
    registry_member: bool,
    dictionary: &LocatorDictionary,
) -> Option<IdLocatorMetadata> {
    let (
        feature_type,
        theme,
        filename,
        last_seen_release,
        exists_in_current_release,
        overture_path,
    ) = if let Some(id) = source_file_id {
        let entry = dictionary.source_files.get(usize::from(id) - 1)?;
        let path = format!(
            "release/{}/theme={}/type={}/{}",
            dictionary.overture_release, entry.theme, entry.feature_type, entry.filename
        );
        (
            Some(entry.feature_type.clone()),
            Some(entry.theme.clone()),
            Some(entry.filename.clone()),
            Some(dictionary.overture_release.clone()),
            true,
            Some(path),
        )
    } else {
        let id = last_seen_release_id?;
        let release = dictionary
            .last_seen_releases
            .get(usize::from(id) - 1)?
            .clone();
        (None, None, None, Some(release), false, None)
    };

    Some(IdLocatorMetadata {
        feature_type,
        theme,
        filename,
        last_seen_release,
        registry_member,
        exists_in_current_release,
        overture_path,
    })
}

/// Loads and caches shards from R2 with edge caching via Cache API.
pub struct ShardLoader {
    bucket: Bucket,
    cache: Cache,
    /// R2 catalog object. Production always uses the root catalog; an
    /// explicitly smoke-scoped override lets preview Workers exercise an
    /// isolated fixed-prefix catalog without making it discoverable live.
    catalog_key: String,
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

pub struct RouterDb {
    conn: rusqlite::Connection,
}

impl RouterDb {
    pub fn from_bytes(bytes: &[u8]) -> std::result::Result<Self, String> {
        use rusqlite::MAIN_DB;
        use std::io::Cursor;
        let mut conn = rusqlite::Connection::open_in_memory().map_err(|e| e.to_string())?;
        conn.deserialize_read_exact(MAIN_DB, Cursor::new(bytes), bytes.len(), true)
            .map_err(|e| e.to_string())?;
        conn.execute_batch("PRAGMA temp_store = MEMORY;")
            .map_err(|e| e.to_string())?;
        Ok(Self { conn })
    }

    fn fold_diacritic(c: char) -> char {
        match c {
            'à' | 'á' | 'â' | 'ã' | 'ä' | 'å' => 'a',
            'è' | 'é' | 'ê' | 'ë' => 'e',
            'ì' | 'í' | 'î' | 'ï' => 'i',
            'ò' | 'ó' | 'ô' | 'õ' | 'ö' | 'ø' => 'o',
            'ù' | 'ú' | 'û' | 'ü' => 'u',
            'ç' => 'c',
            'ñ' => 'n',
            'ý' | 'ÿ' => 'y',
            _ => c,
        }
    }

    fn normalize_token(s: &str) -> String {
        s.trim()
            .chars()
            .flat_map(char::to_lowercase)
            .map(Self::fold_diacritic)
            .collect()
    }

    fn tokenize_query(query: &str) -> Vec<String> {
        let normalized = Self::normalize_token(query);
        normalized
            .split(|c: char| !c.is_alphanumeric())
            .filter(|t| t.chars().count() >= 3)
            .filter(|t| t.chars().any(|c| c.is_alphabetic()))
            .map(|s| s.to_string())
            .collect()
    }

    pub fn lookup_shards(&self, query: &str) -> Vec<String> {
        let tokens = Self::tokenize_query(query);
        if tokens.is_empty() {
            return Vec::new();
        }
        let mut scores: std::collections::HashMap<String, f64> = std::collections::HashMap::new();
        let total = tokens.len() as f64;
        for (idx, token) in tokens.iter().enumerate() {
            let weight = 1.0 + (idx as f64 / total) * 0.5;
            if let Ok(mut stmt) = self.conn.prepare(
                "SELECT shard_id, max_importance FROM router WHERE token = ?1 ORDER BY max_importance DESC LIMIT 4",
            ) {
                if let Ok(rows) = stmt.query_map([token], |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, f64>(1)?))
                }) {
                    for row in rows.flatten() {
                        let (shard_id, imp) = row;
                        *scores.entry(shard_id).or_insert(0.0) += imp * weight;
                    }
                }
            }
        }
        if scores.is_empty() {
            for (idx, token) in tokens.iter().enumerate() {
                let weight = 1.0 + (idx as f64 / total) * 0.5;
                let pattern = format!("{}%", token);
                if let Ok(mut stmt) = self.conn.prepare(
                    "SELECT shard_id, max_importance FROM router WHERE token LIKE ?1 ORDER BY max_importance DESC LIMIT 3",
                ) {
                    if let Ok(rows) = stmt.query_map([pattern], |row| {
                        Ok((row.get::<_, String>(0)?, row.get::<_, f64>(1)?))
                    }) {
                        for row in rows.flatten() {
                            let (shard_id, imp) = row;
                            *scores.entry(shard_id).or_insert(0.0) += imp * weight * 0.8;
                        }
                    }
                }
            }
        }
        let mut ranked: Vec<(String, f64)> = scores.into_iter().collect();
        ranked.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        ranked
            .into_iter()
            .take(MAX_ROUTER_SHARDS)
            .map(|(id, _)| id)
            .collect()
    }
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
        let versions = get_ordered_versions(&catalog, &$self.catalog_key);
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

/// Resolve the catalog object without allowing a deployed production Worker
/// to be redirected. The override is deliberately narrower than a general R2
/// key: only the fixed smoke-family prefixes used by merge-only workflows are
/// accepted, and only when the Worker declares a smoke/preview environment.
fn resolve_catalog_key(
    environment: Option<&str>,
    override_key: Option<&str>,
) -> std::result::Result<String, String> {
    let Some(key) = override_key else {
        return Ok("catalog.json".to_string());
    };
    if !matches!(environment, Some("smoke" | "preview")) {
        return Err(
            "CATALOG_KEY_OVERRIDE is allowed only in smoke or preview environments".to_string(),
        );
    }
    let valid_family = key == "smoketest-id/catalog.json" || key == "smoketest-shards/catalog.json";
    if !valid_family {
        return Err("CATALOG_KEY_OVERRIDE must name a fixed smoketest family catalog".to_string());
    }
    Ok(key.to_string())
}

impl ShardLoader {
    pub fn new(env: &Env) -> Result<Self> {
        let bucket = env.bucket("SHARDS_BUCKET")?;
        let cache = Cache::default();
        let environment = env.var("ENVIRONMENT").ok().map(|value| value.to_string());
        let override_key = env
            .var("CATALOG_KEY_OVERRIDE")
            .ok()
            .map(|value| value.to_string());
        let catalog_key = resolve_catalog_key(environment.as_deref(), override_key.as_deref())
            .map_err(Error::RustError)?;
        Ok(Self {
            bucket,
            cache,
            catalog_key,
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

    /// Health check: verify catalog, latest version, and that required
    /// versioned assets exist. Response shape stays {"status":"ok","version":...}.
    pub async fn check_health(&self) -> Result<String> {
        let catalog = self.load_catalog().await?;
        let versions = get_ordered_versions(&catalog, &self.catalog_key);
        if versions.is_empty() {
            return Err(Error::RustError("No versions found in catalog".into()));
        }
        let latest = versions[0].clone();

        let collection_key = format!("{}/collection.json", latest);
        let collection_text = self
            .memoized_get_text(&collection_key, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(&collection_key))?;
        serde_json::from_str::<StacCollection>(&collection_text)
            .map_err(|e| Error::RustError(format!("Invalid {}: {}", collection_key, e)))?;

        let reverse_key = format!("{}/reverse-collection.json", latest);
        let reverse_text = self
            .memoized_get_text(&reverse_key, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(&reverse_key))?;
        serde_json::from_str::<StacCollection>(&reverse_text)
            .map_err(|e| Error::RustError(format!("Invalid {}: {}", reverse_key, e)))?;

        let id_meta_key = format!("{}/id-meta.json", latest);
        let id_collection_key = format!("{}/id-collection.json", latest);
        let id_meta_text = self
            .memoized_get_text(&id_meta_key, ID_INDEX_CACHE_TTL)
            .await?;
        let has_id_meta = if let Some(ref text) = id_meta_text {
            serde_json::from_str::<serde_json::Value>(text)
                .map_err(|e| Error::RustError(format!("Invalid {}: {}", id_meta_key, e)))?;
            true
        } else {
            false
        };
        let has_id_collection = if has_id_meta {
            true
        } else {
            match self
                .memoized_get_text(&id_collection_key, ID_INDEX_CACHE_TTL)
                .await?
            {
                Some(text) => {
                    if text.find("\"prefix_len\"").is_none() {
                        serde_json::from_str::<serde_json::Value>(&text).map_err(|e| {
                            Error::RustError(format!("Invalid {}: {}", id_collection_key, e))
                        })?;
                    }
                    true
                }
                None => false,
            }
        };
        if !has_id_collection {
            return Err(not_found(format!(
                "id-index metadata for version {} (checked {} and {})",
                latest, id_meta_key, id_collection_key
            )));
        }

        Ok(latest)
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

    /// Read at most `max_bytes` from the start of an object and only cache the
    /// result after proving the object did not fill a `max + 1` sentinel range.
    /// This prevents a corrupt index from being fully materialized by
    /// `cached_get` before its size cap can be checked.
    async fn cached_bounded_prefix_read(
        &self,
        key: &str,
        max_bytes: usize,
        ttl: u64,
    ) -> Result<Option<Bytes>> {
        let sentinel_length = max_bytes
            .checked_add(1)
            .ok_or_else(|| Error::RustError("Bounded prefix length overflow".into()))?;
        let cache_key = format!("{}{}__bounded-prefix-{}", CACHE_PREFIX, key, max_bytes);
        let request = Request::new(&cache_key, Method::Get)?;
        if let Some(mut response) = self.cache.get(&request, false).await? {
            let bytes = response.bytes().await?;
            if bytes.is_empty() {
                return Ok(None);
            }
            if bytes.len() > max_bytes {
                return Err(Error::RustError("Cached bounded prefix exceeds cap".into()));
            }
            return Ok(Some(Bytes::from(bytes)));
        }
        let obj = self
            .bucket
            .get(key)
            .range(worker::Range::OffsetWithLength {
                offset: 0,
                length: sentinel_length as u64,
            })
            .execute()
            .await?;
        let Some(obj) = obj else {
            self.cache_put_bytes_background(cache_key, Bytes::new(), NEGATIVE_CACHE_TTL)
                .await;
            return Ok(None);
        };
        let body = obj
            .body()
            .ok_or_else(|| Error::RustError("Empty bounded-prefix body".into()))?;
        let bytes = Bytes::from(body.bytes().await?);
        if bytes.len() > max_bytes {
            return Err(Error::RustError(format!(
                "R2 object {} exceeds bounded prefix cap",
                key
            )));
        }
        self.cache_put_bytes_background(cache_key, bytes.clone(), ttl)
            .await;
        Ok(Some(bytes))
    }

    /// Experimental exact-address storage path.
    ///
    /// The caller supplies immutable versioned object keys and an already
    /// normalized eight-field address key. The small side index is edge-cached,
    /// then exactly one group-aligned gzip page is range-read and decoded under
    /// the hard limits in `address_pages`. This is deliberately not routed yet:
    /// the spike must measure real Worker/R2 latency before becoming an API.
    pub(crate) async fn lookup_address_page_spike(
        &self,
        index_key: &str,
        data_key: &str,
        lookup_key: &[String; 8],
    ) -> Result<Vec<AddressPageRecord>> {
        let index_bytes = self
            .cached_bounded_prefix_read(index_key, MAX_INDEX_BYTES, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(index_key))?;
        let index = AddressPageIndex::parse(&index_bytes)
            .map_err(|error| Error::RustError(format!("Invalid address page index: {error}")))?;
        let Some(extent) = index.find(lookup_key).cloned() else {
            return Ok(Vec::new());
        };

        // Validate the object envelope independently from the index. A 4 KiB
        // immutable range is enough for the producer's capped JSON header and
        // is edge-cached separately from candidate pages.
        let header = self
            .cached_range_read(data_key, 0, 4096)
            .await?
            .ok_or_else(|| not_found(data_key))?;
        parse_useful_gzip_header(&header)
            .map_err(|error| Error::RustError(format!("Invalid address page data: {error}")))?;

        let page = self
            .cached_range_read(data_key, extent.offset, extent.length)
            .await?
            .ok_or_else(|| not_found(format!("{} range", data_key)))?;
        if page.len() as u64 != extent.length {
            return Err(Error::RustError(
                "Address page range length differs from index".into(),
            ));
        }
        decode_useful_gzip_range(&page, extent.rows, lookup_key)
            .map_err(|error| Error::RustError(format!("Invalid address page: {error}")))
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

    fn country_name_to_code(name: &str) -> Option<&'static str> {
        match name {
            "afghanistan" => Some("AF"),
            "albania" => Some("AL"),
            "algeria" => Some("DZ"),
            "argentina" => Some("AR"),
            "australia" => Some("AU"),
            "austria" => Some("AT"),
            "bangladesh" => Some("BD"),
            "belgium" => Some("BE"),
            "brazil" => Some("BR"),
            "bulgaria" => Some("BG"),
            "canada" => Some("CA"),
            "chile" => Some("CL"),
            "china" => Some("CN"),
            "colombia" => Some("CO"),
            "croatia" => Some("HR"),
            "czech" => Some("CZ"),
            "czechia" => Some("CZ"),
            "denmark" => Some("DK"),
            "egypt" => Some("EG"),
            "finland" => Some("FI"),
            "france" => Some("FR"),
            "germany" => Some("DE"),
            "deutschland" => Some("DE"),
            "greece" => Some("GR"),
            "hungary" => Some("HU"),
            "india" => Some("IN"),
            "indonesia" => Some("ID"),
            "iran" => Some("IR"),
            "iraq" => Some("IQ"),
            "ireland" => Some("IE"),
            "israel" => Some("IL"),
            "italy" => Some("IT"),
            "italia" => Some("IT"),
            "japan" => Some("JP"),
            "mexico" => Some("MX"),
            "netherlands" => Some("NL"),
            "norway" => Some("NO"),
            "pakistan" => Some("PK"),
            "poland" => Some("PL"),
            "portugal" => Some("PT"),
            "russia" => Some("RU"),
            "spain" => Some("ES"),
            "sweden" => Some("SE"),
            "switzerland" => Some("CH"),
            "turkey" => Some("TR"),
            "uk" => Some("GB"),
            "britain" => Some("GB"),
            "united kingdom" => Some("GB"),
            "usa" => Some("US"),
            "america" => Some("US"),
            "united states" => Some("US"),
            _ => None,
        }
    }

    fn select_shards_by_country_suffix(
        query_text: &str,
        collection: &StacCollection,
    ) -> Vec<String> {
        let lower = query_text.to_lowercase();
        let candidate = if let Some(pos) = lower.rfind(',') {
            lower[pos + 1..].trim().to_string()
        } else {
            lower
                .split_whitespace()
                .last()
                .unwrap_or("")
                .trim()
                .to_string()
        };
        if candidate.is_empty() {
            return Vec::new();
        }
        let code_opt = if candidate.len() == 2 && candidate.chars().all(|c| c.is_ascii_alphabetic())
        {
            Some(candidate.to_uppercase())
        } else {
            Self::country_name_to_code(&candidate).map(|s| s.to_string())
        };
        if let Some(code) = code_opt {
            if Self::collection_has_shard(collection, &code) {
                return vec![code];
            }
            if let Some(regions) = collection.region_sharded.get(&code) {
                return vec![regions.first().cloned().unwrap_or(code)];
            }
        }
        Vec::new()
    }

    fn select_shards_from_router(
        &self,
        router: &RouterDb,
        query_text: &str,
        collection: &StacCollection,
    ) -> Vec<String> {
        let raw = router.lookup_shards(query_text);
        let mut filtered: Vec<String> = Vec::new();
        for sid in raw {
            if sid == "HEAD" {
                continue;
            }
            if Self::collection_has_shard(collection, &sid) {
                if !filtered.contains(&sid) {
                    filtered.push(sid);
                }
            } else if let Some(regions) = collection.region_sharded.get(&sid) {
                if let Some(first) = regions.first() {
                    if !filtered.contains(first) {
                        filtered.push(first.clone());
                    }
                }
            } else if let Some((country, _)) = sid.split_once('-') {
                if Self::collection_has_shard(collection, country) {
                    let cs = country.to_string();
                    if !filtered.contains(&cs) {
                        filtered.push(cs);
                    }
                } else if let Some(regions) = collection.region_sharded.get(country) {
                    if let Some(first) = regions.first() {
                        if !filtered.contains(first) {
                            filtered.push(first.clone());
                        }
                    }
                }
            }
        }
        filtered
    }

    fn is_places_shard(shard_id: &str) -> bool {
        shard_id.ends_with("-places")
    }

    /// Select Places shards only when an existing division route or explicit
    /// caller region justifies them.  An unroutable Places query must not fall
    /// through to an arbitrary prototype region (historically US-CA).
    fn select_places_shards(
        collection: &StacCollection,
        division_shards: &[String],
        user_location: &UserLocation,
        remaining_extra_budget: usize,
    ) -> Vec<String> {
        if remaining_extra_budget == 0 {
            return Vec::new();
        }
        let mut places = Vec::new();
        for shard_id in division_shards {
            if shard_id == "HEAD" {
                continue;
            }
            let places_id = format!("{}-places", shard_id);
            if Self::collection_has_shard(collection, &places_id)
                && !division_shards.contains(&places_id)
                && !places.contains(&places_id)
            {
                places.push(places_id);
            }
        }

        if let (Some(country), Some(region_code)) =
            (&user_location.country, &user_location.region_code)
        {
            let places_id = format!("{}-{}-places", country, region_code);
            if Self::collection_has_shard(collection, &places_id)
                && !division_shards.contains(&places_id)
                && !places.contains(&places_id)
            {
                places.push(places_id);
            }
        }

        places.truncate(MAX_PLACES_SHARDS.min(remaining_extra_budget));
        places
    }

    fn allocate_extra_shards(
        collection: &StacCollection,
        generic_candidates: &[String],
        user_location: &UserLocation,
        includes_place: bool,
    ) -> (Vec<String>, Vec<String>) {
        let mut routing_ids = vec!["HEAD".to_string()];
        routing_ids.extend(generic_candidates.iter().cloned());
        let has_places = includes_place
            && !Self::select_places_shards(
                collection,
                &routing_ids,
                user_location,
                MAX_PLACES_SHARDS,
            )
            .is_empty();
        let generic_limit = MAX_EXTRA_SHARDS.saturating_sub(usize::from(has_places));
        let generic: Vec<String> = generic_candidates
            .iter()
            .take(generic_limit)
            .cloned()
            .collect();
        let remaining = MAX_EXTRA_SHARDS.saturating_sub(generic.len());
        let places = if includes_place {
            Self::select_places_shards(collection, &routing_ids, user_location, remaining)
        } else {
            Vec::new()
        };
        (generic, places)
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

        let suffix_shards = Self::select_shards_by_country_suffix(&query.text, &collection);
        if !suffix_shards.is_empty() {
            console_log!(
                "Suffix heuristic selected: {:?} for query {:?}",
                suffix_shards,
                query.text
            );
        }

        let router_shards = match self.load_router_db(version).await {
            Ok(Some(router)) => {
                let shards = self.select_shards_from_router(&router, &query.text, &collection);
                if !shards.is_empty() {
                    console_log!(
                        "Router selected shards: {:?} for query {:?}",
                        shards,
                        query.text
                    );
                }
                shards
            }
            Ok(None) => {
                console_log!(
                    "Router DB not available for version {}, using suffix only",
                    version
                );
                Vec::new()
            }
            Err(e) => {
                console_log!("Router load failed: {:?}, using suffix only", e);
                Vec::new()
            }
        };

        let nearby_shards = self.select_nearby_shards(&collection, user_location);
        console_log!("Nearby shards: {:?}", nearby_shards);

        let includes_place = query.includes_place();

        let mut seen = std::collections::HashSet::new();
        seen.insert("HEAD".to_string());
        let mut generic_candidates: Vec<String> = Vec::new();
        for sid in suffix_shards
            .iter()
            .chain(router_shards.iter())
            .chain(nearby_shards.iter())
        {
            // Places use one dedicated derivation path below. Allowing them
            // into this generic budget would let proximity-selected items
            // bypass MAX_PLACES_SHARDS and inflate concurrent cold loads.
            if Self::is_places_shard(sid) {
                continue;
            }
            if seen.insert(sid.clone()) {
                generic_candidates.push(sid.clone());
            }
        }
        let (extra, places_extra) = Self::allocate_extra_shards(
            &collection,
            &generic_candidates,
            user_location,
            includes_place,
        );
        console_log!("Final extra shards: {:?}", extra);

        let mut shard_ids = vec!["HEAD".to_string()];
        shard_ids.extend(extra.clone());

        if !places_extra.is_empty() {
            console_log!("Places extra shards: {:?}", places_extra);
        }

        let mut all_shard_ids = shard_ids.clone();
        all_shard_ids.extend(places_extra.clone());

        let outcomes = futures::future::join_all(
            all_shard_ids
                .iter()
                .map(|shard_id| self.query_shard_with_info(version, shard_id, &collection, query)),
        )
        .await;

        let mut all_results = Vec::new();
        let mut shards_loaded = Vec::new();
        for (shard_id, outcome) in all_shard_ids.iter().zip(outcomes) {
            match outcome {
                Ok((results, info)) => {
                    all_results.extend(results);
                    if include_debug {
                        shards_loaded.push(info);
                    }
                }
                Err(e) if shard_id == "HEAD" => return Err(e),
                Err(e) => {
                    console_log!("Warning: shard {} unavailable: {:?}", shard_id, e);
                }
            }
        }

        if let Some(allowed) = &query.allowed_types {
            if !allowed.is_empty() {
                all_results.retain(|r| {
                    let t = r.division_type.to_lowercase();
                    let normalized = if t == "neighbourhood" {
                        "neighborhood".to_string()
                    } else {
                        t
                    };
                    allowed.contains(&normalized)
                });
            }
        }

        all_results.sort_by(|a, b| {
            b.importance
                .partial_cmp(&a.importance)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        let mut seen = std::collections::HashSet::new();
        all_results.retain(|r| seen.insert(r.gers_id.clone()));

        if !matches!(query.bias, LocationBias::None) {
            apply_location_bias(&mut all_results, &query.bias);
        }

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
            version: version.to_string(),
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
            return Self::select_shards_by_proximity(collection, lat, lon);
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
    fn select_shards_by_proximity(collection: &StacCollection, lat: f64, lon: f64) -> Vec<String> {
        // Collect all shards with their distances
        let mut candidates: Vec<(String, f64)> = collection
            .items
            .iter()
            .filter_map(|(shard_id, item)| {
                // Skip HEAD (queried separately)
                if shard_id == "HEAD" || Self::is_places_shard(shard_id) {
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
        candidates.sort_by(|a, b| {
            a.1.partial_cmp(&b.1)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| a.0.cmp(&b.0))
        });

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
        if Self::collection_has_shard(collection, country) {
            return vec![country.to_string()];
        }

        Vec::new()
    }

    fn select_reverse_route(
        collection: &StacCollection,
        lat: f64,
        lon: f64,
    ) -> ReverseRouteSelection {
        let mut containing: Vec<String> = collection
            .items
            .iter()
            .filter_map(|(shard_id, item)| {
                if shard_id == "HEAD" {
                    return None;
                }
                let bbox = item.bbox.as_ref()?;
                if bbox_contains(lat, lon, bbox) {
                    Some(shard_id.clone())
                } else {
                    None
                }
            })
            .collect();
        containing.sort();

        let bbox_candidate_count = containing.len();
        let bbox_candidates = containing
            .iter()
            .take(MAX_REVERSE_ROUTING_DIAGNOSTIC_CANDIDATES)
            .cloned()
            .collect();

        if containing.is_empty() {
            // The request coordinates, not the caller's network location, own
            // reverse routing. An IP country cannot safely resolve a water
            // point, missing bbox, or coordinate in another country.
            return ReverseRouteSelection {
                shards: Vec::new(),
                debug: ReverseRoutingDebug {
                    country_decision: ReverseCountryDecision::Unresolved,
                    outcome: ReverseRoutingOutcome::GlobalFallback,
                    bbox_candidate_count,
                    bbox_candidates,
                    selected_country: None,
                },
            };
        }

        if containing.len() == 1 {
            let selected_country = containing.remove(0);
            return ReverseRouteSelection {
                shards: vec![selected_country.clone()],
                debug: ReverseRoutingDebug {
                    country_decision: ReverseCountryDecision::UniqueBbox,
                    outcome: ReverseRoutingOutcome::CountryShard,
                    bbox_candidate_count,
                    bbox_candidates,
                    selected_country: Some(selected_country),
                },
            };
        }

        // Country bboxes aggregate disconnected territories and can be much
        // larger than the mainland they represent. Neither bbox area nor the
        // caller's IP country can safely resolve an overlap: a Canadian user
        // asking about Boston must not be routed to Canada, and the aggregate
        // US bbox can span nearly the whole longitude range. Fall through to
        // the global HEAD shard, whose more-specific admin candidates provide
        // a safer result until exact country polygons/H3 are available.
        ReverseRouteSelection {
            shards: Vec::new(),
            debug: ReverseRoutingDebug {
                country_decision: ReverseCountryDecision::AmbiguousBbox,
                outcome: ReverseRoutingOutcome::GlobalFallback,
                bbox_candidate_count,
                bbox_candidates,
                selected_country: None,
            },
        }
    }

    #[cfg(test)]
    fn select_reverse_shards(
        collection: &StacCollection,
        lat: f64,
        lon: f64,
        _cf_country: Option<&str>,
    ) -> Vec<String> {
        Self::select_reverse_route(collection, lat, lon).shards
    }

    /// Reverse geocode a lat/lon coordinate.
    /// Falls back to older versions if the latest version's shards are unavailable.
    pub async fn reverse_geocode(
        &self,
        lat: f64,
        lon: f64,
        _cf_country: Option<&str>,
    ) -> Result<ReverseSearchResult> {
        with_version_fallback!(self, "reverse", version, {
            self.try_reverse_geocode(version, lat, lon, _cf_country)
                .await
        })
    }

    /// Attempt reverse geocode against a specific version.
    async fn try_reverse_geocode(
        &self,
        version: &str,
        lat: f64,
        lon: f64,
        _cf_country: Option<&str>,
    ) -> Result<ReverseSearchResult> {
        let reverse_collection = self.load_reverse_collection(version).await?;

        // Ambiguous or unresolved country bboxes fall through to HEAD. This avoids choosing
        // by bbox area or caller IP when disconnected territories inflate a
        // country's aggregate bbox. Exact country polygons/H3 remain future work.
        let mut routing = Self::select_reverse_route(&reverse_collection, lat, lon);
        for country in &routing.shards {
            match self
                .query_reverse_shard(version, country, &reverse_collection, lat, lon)
                .await
            {
                Ok(Some(result)) => {
                    return Ok(ReverseSearchResult {
                        result: Some(result),
                        version: version.to_string(),
                        routing: routing.debug,
                    })
                }
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

        let res = self
            .query_reverse_shard(version, "HEAD", &reverse_collection, lat, lon)
            .await?;
        routing.debug.outcome = if res.is_some() {
            ReverseRoutingOutcome::GlobalFallback
        } else {
            ReverseRoutingOutcome::Unresolved
        };
        Ok(ReverseSearchResult {
            result: res,
            version: version.to_string(),
            routing: routing.debug,
        })
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

    async fn load_router_db(&self, version: &str) -> Result<Option<Rc<RouterDb>>> {
        for key in [
            format!("{}/router.db", version),
            format!("{}/shards/router.db", version),
        ] {
            let cached = ROUTER_CACHE.with(|c| {
                let mut cache = c.borrow_mut();
                cache.iter().position(|(k, _, _)| k == &key).map(|pos| {
                    let entry = cache.remove(pos);
                    let hit = Rc::clone(&entry.1);
                    cache.push(entry);
                    hit
                })
            });
            if let Some(db) = cached {
                console_log!("Router cache HIT: {}", key);
                return Ok(Some(db));
            }

            if let Some(bytes) = self.cached_get(&key, IMMUTABLE_CACHE_TTL).await? {
                let size = bytes.len();
                let router = Rc::new(RouterDb::from_bytes(&bytes).map_err(|e| {
                    Error::RustError(format!("Failed to open router {}: {}", key, e))
                })?);
                drop(bytes);
                ROUTER_CACHE.with(|c| {
                    let mut cache = c.borrow_mut();
                    if !cache.iter().any(|(k, _, _)| k == &key) {
                        cache.push((key.clone(), Rc::clone(&router), size));
                        while cache.len() > 2 {
                            let (evicted, _, _) = cache.remove(0);
                            console_log!("Router cache evict: {}", evicted);
                        }
                    }
                });
                console_log!("Router DB loaded: {} ({}B)", key, size);
                return Ok(Some(router));
            }
        }
        console_log!("Router DB not found for version {}", version);
        Ok(None)
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
    pub async fn lookup_id(&self, gers_id: &str) -> Result<IdSearchResult> {
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
    async fn try_lookup_id(&self, version: &str, gers_id: &str) -> Result<IdSearchResult> {
        let id_config = self.load_id_index_config(version).await?;
        let prefix_len = id_config.prefix_len;

        let hex_id: String = gers_id.replace('-', "").to_lowercase();
        let Some(prefix) = hex_id.get(..prefix_len) else {
            return Ok(IdSearchResult {
                result: None,
                version: version.to_string(),
            });
        };
        let shard_key = format!("{}/id-index/{}.parquet", version, prefix);

        let target = match parse_uuid_bytes(gers_id) {
            Some(t) => t,
            None => {
                return Ok(IdSearchResult {
                    result: None,
                    version: version.to_string(),
                })
            }
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
        let metadata_len = parquet_footer_metadata_len(&tail_bytes).map_err(|reason| {
            Error::RustError(format!(
                "Invalid parquet footer for {}: {}",
                shard_key, reason
            ))
        })?;
        // Sanity-cap before acting on the length: a corrupt (or stale-cached)
        // 4-byte footer field of up to ~4 GB would otherwise trigger a
        // whole-file suffix fetch, buffered in memory and edge-cached.
        let footer_retry =
            footer_retry_size(file_size, tail_bytes.len(), metadata_len).map_err(|reason| {
                Error::RustError(format!(
                    "Invalid parquet footer for {}: {}",
                    shard_key, reason
                ))
            })?;
        if let Some(footer_size) = footer_retry {
            // Footer larger than the default suffix window: re-read with the
            // exact size (cached under a size-specific key).
            console_log!(
                "Footer {}B exceeds {}B window for {}, re-reading",
                footer_size,
                tail_bytes.len(),
                shard_key
            );
            tail_bytes = match self.cached_suffix_read(&shard_key, footer_size).await? {
                Some((retry_file_size, bytes)) => {
                    validate_footer_retry_response(
                        file_size,
                        metadata_len,
                        retry_file_size,
                        &bytes,
                    )
                    .map_err(|reason| {
                        Error::RustError(format!(
                            "Invalid parquet footer retry for {}: {}",
                            shard_key, reason
                        ))
                    })?;
                    bytes
                }
                None => return Err(not_found(format!("id-index shard {}", shard_key))),
            };
        }
        let tail_len = tail_bytes.len() as u64;
        let tail_offset = file_size.checked_sub(tail_len).ok_or_else(|| {
            Error::RustError(format!(
                "Parquet suffix for {} exceeds object size",
                shard_key
            ))
        })?;
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
            None => {
                return Ok(IdSearchResult {
                    result: None,
                    version: version.to_string(),
                })
            }
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
            if id_bytes.data() > target.as_slice() {
                return Ok(IdSearchResult {
                    result: None,
                    version: version.to_string(),
                });
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
                let locator = if let Some((source_file_id, last_seen_release_id, registry_member)) =
                    compact_locator_ids(&row, id_config.format_version)
                {
                    match self.load_locator_dictionary(version, &id_config).await {
                        Ok(dictionary) => build_locator_metadata(
                            source_file_id,
                            last_seen_release_id,
                            registry_member,
                            &dictionary,
                        ),
                        Err(error) => {
                            console_log!(
                                "ID locator unavailable for {}: {:?}; returning legacy bbox",
                                gers_id,
                                error
                            );
                            None
                        }
                    }
                } else {
                    None
                };
                return Ok(IdSearchResult {
                    result: Some(IdLookupResult {
                        id: gers_id.to_string(),
                        bbox: geocoder_core::BBox {
                            xmin: bbox_xmin,
                            ymin: bbox_ymin,
                            xmax: bbox_xmax,
                            ymax: bbox_ymax,
                        },
                        locator,
                    }),
                    version: version.to_string(),
                });
            }
        }
        Ok(IdSearchResult {
            result: None,
            version: version.to_string(),
        })
    }

    /// Load the ID index prefix_len from a small metadata file.
    /// Falls back to id-collection.json summaries if id-meta.json doesn't exist.
    async fn load_id_index_config(&self, version: &str) -> Result<IdIndexConfig> {
        // Try tiny metadata file first (avoids loading multi-MB collection).
        // id-index TTL: patch runs re-upload these files in place.
        let meta_key = format!("{}/id-meta.json", version);
        if let Some(text) = self
            .memoized_get_text(&meta_key, ID_INDEX_CACHE_TTL)
            .await?
        {
            return parse_id_index_config(&text)
                .map_err(|e| not_found(format!("invalid id-index metadata {}: {}", meta_key, e)));
        }

        // Fallback: load id-collection.json. Its v3 fields live under
        // summaries; legacy collections produce a format-v1 config.
        let key = format!("{}/id-collection.json", version);
        if let Some(text) = self.memoized_get_text(&key, ID_INDEX_CACHE_TTL).await? {
            return parse_id_index_config(&text)
                .map_err(|e| not_found(format!("invalid id-index metadata {}: {}", key, e)));
        }

        // Both metadata files missing: the id-index isn't deployed for this
        // version. Surface a retriable not-found (so version fallback engages)
        // rather than guessing a shard layout and returning clean 404s.
        Err(not_found(format!(
            "id-index metadata for version {}",
            version
        )))
    }

    async fn load_locator_dictionary(
        &self,
        version: &str,
        config: &IdIndexConfig,
    ) -> Result<Rc<LocatorDictionary>> {
        let reference = config.locator_dictionary.as_ref().ok_or_else(|| {
            Error::RustError("ID-index format has no locator dictionary reference".into())
        })?;
        let expected_release = config
            .overture_release
            .as_deref()
            .ok_or_else(|| Error::RustError("ID-index format has no Overture release".into()))?;
        let href = reference.href.trim_start_matches("./");
        let key = format!("{}/{}", version, href);
        if let Some(cached) = LOCATOR_DICTIONARY_CACHE.with(|cache| {
            let mut cache = cache.borrow_mut();
            let position = cache
                .iter()
                .position(|(cached_key, _)| cached_key == &key)?;
            let entry = cache.remove(position);
            let result = Rc::clone(&entry.1);
            cache.push(entry);
            Some(result)
        }) {
            return Ok(cached);
        }
        let text = self
            .memoized_get_text(&key, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(format!("ID locator dictionary {}", key)))?;
        if text.len() > 1024 * 1024 {
            return Err(Error::RustError(
                "ID locator dictionary exceeds 1 MiB".into(),
            ));
        }
        if text.len() != reference.size_bytes {
            return Err(Error::RustError(format!(
                "ID locator dictionary size mismatch for {}",
                key
            )));
        }
        let actual_sha256 = format!("{:x}", Sha256::digest(text.as_bytes()));
        if actual_sha256 != reference.sha256 {
            return Err(Error::RustError(format!(
                "ID locator dictionary checksum mismatch for {}",
                key
            )));
        }
        let dictionary: LocatorDictionary = serde_json::from_str(&text).map_err(|error| {
            Error::RustError(format!("Invalid ID locator dictionary {}: {}", key, error))
        })?;
        validate_locator_dictionary(&dictionary, reference, expected_release).map_err(|error| {
            Error::RustError(format!("Invalid ID locator dictionary {}: {}", key, error))
        })?;
        let dictionary = Rc::new(dictionary);
        LOCATOR_DICTIONARY_CACHE.with(|cache| {
            let mut cache = cache.borrow_mut();
            cache.push((key, Rc::clone(&dictionary)));
            if cache.len() > LOCATOR_DICTIONARY_CACHE_MAX_ENTRIES {
                cache.remove(0);
            }
        });
        Ok(dictionary)
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
            .memoized_get_text(&self.catalog_key, CATALOG_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(&self.catalog_key))?;

        serde_json::from_str(&text).map_err(|e| {
            Error::RustError(format!(
                "Failed to parse catalog {}: {}",
                self.catalog_key, e
            ))
        })
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

    fn collection_has_shard(collection: &StacCollection, shard_id: &str) -> bool {
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
fn child_version(catalog_key: &str, href: &str) -> Option<String> {
    let relative = href.trim_start_matches("./");
    if relative.is_empty() {
        return None;
    }
    if let Some((version, _)) = relative.split_once('/') {
        return (!version.is_empty()).then(|| version.to_string());
    }
    let catalog_parent = catalog_key
        .rsplit_once('/')
        .map(|(parent, _)| parent)
        .unwrap_or("");
    if !catalog_parent.is_empty() {
        return catalog_parent
            .rsplit('/')
            .next()
            .filter(|version| !version.is_empty())
            .map(str::to_string);
    }

    // Preserve root-catalog behavior exactly. The nested preview catalog is
    // the only catalog whose child href intentionally omits a version.
    Some(relative.to_string())
}

fn get_ordered_versions(catalog: &StacCatalog, catalog_key: &str) -> Vec<String> {
    let mut latest = None;
    let mut others: Vec<String> = Vec::new();

    for link in &catalog.links {
        if link.rel != "child" {
            continue;
        }
        let Some(version) = child_version(catalog_key, &link.href) else {
            continue;
        };
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

fn normalize_lon(mut lon: f64) -> f64 {
    while lon > 180.0 {
        lon -= 360.0;
    }
    while lon < -180.0 {
        lon += 360.0;
    }
    lon
}

fn bbox_contains(lat: f64, lon: f64, bbox: &[f64; 4]) -> bool {
    let [min_lon, min_lat, max_lon, max_lat] = *bbox;
    if lat < min_lat || lat > max_lat {
        return false;
    }
    let lon = normalize_lon(lon);
    let min_lon_n = normalize_lon(min_lon);
    let max_lon_n = normalize_lon(max_lon);
    if min_lon_n <= max_lon_n {
        lon >= min_lon_n && lon <= max_lon_n
    } else {
        lon >= min_lon_n || lon <= max_lon_n
    }
}

#[cfg(test)]
fn bbox_area_deg2(bbox: &[f64; 4]) -> f64 {
    let [min_lon, min_lat, max_lon, max_lat] = *bbox;
    let height = (max_lat - min_lat).abs();
    let width = if min_lon <= max_lon {
        (max_lon - min_lon).abs()
    } else {
        (180.0 - min_lon) + (max_lon + 180.0)
    };
    width * height
}

fn distance_to_bbox(lat: f64, lon: f64, bbox: &[f64; 4]) -> f64 {
    if bbox_contains(lat, lon, bbox) {
        return 0.0;
    }
    let [min_lon, min_lat, max_lon, max_lat] = *bbox;
    let closest_lat = lat.clamp(min_lat, max_lat);
    let closest_lon = if min_lon <= max_lon {
        lon.clamp(min_lon, max_lon)
    } else {
        let nlon = normalize_lon(lon);
        let min_n = normalize_lon(min_lon);
        let max_n = normalize_lon(max_lon);
        if nlon > max_n && nlon < min_n {
            let dist_to_min = (min_n - nlon).abs();
            let dist_to_max = (nlon - max_n).abs();
            if dist_to_min < dist_to_max {
                min_lon
            } else {
                max_lon
            }
        } else {
            lon.clamp(min_lon, max_lon)
        }
    };
    haversine_distance(lat, lon, closest_lat, closest_lon)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn catalog_key_defaults_to_production_root() {
        assert_eq!(
            resolve_catalog_key(Some("production"), None).unwrap(),
            "catalog.json"
        );
        assert_eq!(resolve_catalog_key(None, None).unwrap(), "catalog.json");
    }

    #[test]
    fn catalog_override_is_fixed_prefix_and_preview_only() {
        assert_eq!(
            resolve_catalog_key(Some("smoke"), Some("smoketest-id/catalog.json")).unwrap(),
            "smoketest-id/catalog.json"
        );
        assert_eq!(
            resolve_catalog_key(Some("preview"), Some("smoketest-shards/catalog.json")).unwrap(),
            "smoketest-shards/catalog.json"
        );
        assert!(
            resolve_catalog_key(Some("production"), Some("smoketest-id/catalog.json")).is_err()
        );
        assert!(resolve_catalog_key(Some("smoke"), Some("catalog.json")).is_err());
        assert!(resolve_catalog_key(Some("smoke"), Some("smoketest-id/../catalog.json")).is_err());
    }
    use parquet::record::{Field, Row};

    const DICTIONARY_SHA: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    fn dictionary_reference() -> LocatorDictionaryReference {
        LocatorDictionaryReference {
            href: format!("./id-locator-dictionary-{DICTIONARY_SHA}.json"),
            sha256: DICTIONARY_SHA.to_string(),
            size_bytes: 512,
            dictionary_version: 1,
            source_files_count: 1,
            last_seen_releases_count: 1,
            source_file_id_bounds: Some([1, 1]),
            last_seen_release_id_bounds: Some([1, 1]),
        }
    }

    fn locator_dictionary() -> LocatorDictionary {
        LocatorDictionary {
            format_version: 3,
            dictionary_version: 1,
            overture_release: "2026-06-17.0".to_string(),
            type_theme_map: TypeThemeMap {
                version: 1,
                types: HashMap::from([("address".to_string(), "addresses".to_string())]),
            },
            source_files: vec![SourceFileEntry {
                theme: "addresses".to_string(),
                feature_type: "address".to_string(),
                filename: "part-00001.zstd.parquet".to_string(),
            }],
            last_seen_releases: vec!["2026-05-20.0".to_string()],
            source_files_count: 1,
            last_seen_releases_count: 1,
            source_file_id_bounds: Some([1, 1]),
            last_seen_release_id_bounds: Some([1, 1]),
        }
    }

    fn locator_row(source_id: Field, release_id: Field, registry_member: bool) -> Row {
        let mut fields: Vec<_> = (0..5)
            .map(|index| (format!("bbox_{index}"), Field::Null))
            .collect();
        fields.extend([
            ("source_file_id".to_string(), source_id),
            ("last_seen_release_id".to_string(), release_id),
            ("registry_member".to_string(), Field::Bool(registry_member)),
        ]);
        Row::new(fields)
    }

    #[test]
    fn test_v1_id_metadata_stays_legacy() {
        let config =
            parse_id_index_config(r#"{"prefix_len":3,"overture_release":"2026-06-17.0"}"#).unwrap();
        assert_eq!(config.format_version, 1);
        assert!(config.overture_release.is_none());
        assert!(config.locator_dictionary.is_none());
    }

    #[test]
    fn test_v3_collection_and_dictionary_build_current_release_path() {
        let text = format!(
            r#"{{"summaries":{{"prefix_len":3,"format_version":3,
                "overture_release":"2026-06-17.0",
                "locator_dictionary":{{"href":"./id-locator-dictionary-{DICTIONARY_SHA}.json",
                "sha256":"{DICTIONARY_SHA}","size_bytes":512,"dictionary_version":1,
                "source_files_count":1,"last_seen_releases_count":1,
                "source_file_id_bounds":[1,1],"last_seen_release_id_bounds":[1,1]}}}}}}"#
        );
        let config = parse_id_index_config(&text).unwrap();
        assert!(config.locator_dictionary.is_some());
        let dictionary = locator_dictionary();
        validate_locator_dictionary(&dictionary, &dictionary_reference(), "2026-06-17.0").unwrap();
        let locator = build_locator_metadata(Some(1), None, false, &dictionary).unwrap();
        assert_eq!(locator.theme.as_deref(), Some("addresses"));
        assert!(locator.exists_in_current_release);
        assert_eq!(
            locator.overture_path.as_deref(),
            Some("release/2026-06-17.0/theme=addresses/type=address/part-00001.zstd.parquet")
        );
    }

    #[test]
    fn test_v3_historical_id_has_no_current_path() {
        let locator = build_locator_metadata(None, Some(1), true, &locator_dictionary()).unwrap();
        assert!(!locator.exists_in_current_release);
        assert!(locator.filename.is_none());
        assert!(locator.overture_path.is_none());
        assert_eq!(locator.last_seen_release.as_deref(), Some("2026-05-20.0"));
    }

    #[test]
    fn test_v3_invalid_or_out_of_range_ids_have_no_locator() {
        let dictionary = locator_dictionary();
        assert!(build_locator_metadata(None, None, true, &dictionary).is_none());
        assert!(build_locator_metadata(Some(2), None, true, &dictionary).is_none());
        assert!(build_locator_metadata(None, Some(2), true, &dictionary).is_none());
    }

    #[test]
    fn test_v3_metadata_requires_supported_complete_contract() {
        for text in [
            r#"{"prefix_len":3,"format_version":3}"#,
            r#"{"prefix_len":3,"format_version":"2"}"#,
            r#"{"prefix_len":3,"format_version":null}"#,
            r#"{"prefix_len":3,"format_version":2.0}"#,
            r#"{"prefix_len":3,"format_version":2}"#,
        ] {
            assert!(parse_id_index_config(text).is_err(), "accepted {text}");
        }
        let mut reference = dictionary_reference();
        reference.href = format!("./id-locator-dictionary-{}-extra.json", reference.sha256);
        assert!(validate_dictionary_reference(&reference).is_err());
    }

    #[test]
    fn test_v3_compact_row_validation_fails_closed() {
        let short = Row::new(
            (0..5)
                .map(|index| (format!("legacy_{index}"), parquet::record::Field::Null))
                .collect(),
        );
        assert!(compact_locator_ids(&short, 3).is_none());
        assert_eq!(
            compact_locator_ids(&locator_row(Field::Int(1), Field::Null, true), 3),
            Some((Some(1), None, true))
        );
        assert_eq!(
            compact_locator_ids(&locator_row(Field::Null, Field::Int(1), true), 3),
            Some((None, Some(1), true))
        );
        assert!(compact_locator_ids(&locator_row(Field::Int(0), Field::Null, true), 3).is_none());
        assert!(
            compact_locator_ids(&locator_row(Field::Int(-1), Field::Int(1), true), 3).is_none()
        );
        assert!(
            compact_locator_ids(&locator_row(Field::Int(1), Field::Int(70_000), true), 3).is_none()
        );
        assert!(compact_locator_ids(&locator_row(Field::Int(1), Field::Int(1), true), 3).is_none());
        assert!(compact_locator_ids(&locator_row(Field::Null, Field::Null, true), 3).is_none());
    }

    #[test]
    fn test_footer_retry_decision_covers_initial_and_exact_retry_paths() {
        assert_eq!(footer_retry_size(1_000_000, 32_768, 7_313), Ok(None));
        assert_eq!(
            footer_retry_size(1_000_000, 32_768, 39_992),
            Ok(Some(40_000))
        );
        // R2 returns the complete small file even though 32 KiB was requested.
        assert_eq!(footer_retry_size(10_000, 10_000, 8_992), Ok(None));
    }

    #[test]
    fn test_footer_retry_rejects_corrupt_or_implausible_lengths() {
        assert!(footer_retry_size(1_000, 1_000, 1_001).is_err());
        assert!(footer_retry_size(
            (MAX_PARQUET_FOOTER_SIZE + 9) as u64,
            32_768,
            MAX_PARQUET_FOOTER_SIZE + 1,
        )
        .is_err());
        assert!(footer_retry_size(u64::MAX, 32_768, usize::MAX).is_err());
        assert!(footer_retry_size(1_000, 1_001, 100).is_err());
    }

    #[test]
    fn test_footer_retry_rejects_mixed_object_generations() {
        fn footer(metadata_len: u32, magic: &[u8; 4]) -> Vec<u8> {
            let mut bytes = vec![0; metadata_len as usize];
            bytes.extend_from_slice(&metadata_len.to_le_bytes());
            bytes.extend_from_slice(magic);
            bytes
        }

        let expected = footer(40_000, b"PAR1");
        assert_eq!(
            validate_footer_retry_response(1_000_000, 40_000, 1_000_000, &expected),
            Ok(())
        );
        assert!(validate_footer_retry_response(1_000_000, 40_000, 1_000_001, &expected,).is_err());
        assert!(validate_footer_retry_response(
            1_000_000,
            40_000,
            1_000_000,
            &footer(39_999, b"PAR1"),
        )
        .is_err());
        assert!(validate_footer_retry_response(
            1_000_000,
            40_000,
            1_000_000,
            &footer(40_000, b"NOPE"),
        )
        .is_err());
    }

    fn collection_with_bboxes(rows: &[(&str, Option<[f64; 4]>)]) -> StacCollection {
        let items = rows
            .iter()
            .map(|(id, bbox)| {
                (
                    (*id).to_string(),
                    EmbeddedItem {
                        record_count: 0,
                        size_bytes: 0,
                        sha256: None,
                        href: format!("reverse/{}.db", id),
                        bbox: *bbox,
                        parent_country: None,
                    },
                )
            })
            .collect();

        StacCollection {
            id: "test".to_string(),
            items,
            links: vec![],
            region_sharded: std::collections::HashMap::new(),
        }
    }

    #[test]
    fn test_reverse_selection_prefers_request_coordinates_to_ip_country() {
        let collection = collection_with_bboxes(&[
            ("JP", Some([122.0, 20.0, 154.0, 46.0])),
            ("US", Some([-125.0, 24.0, -66.0, 50.0])),
        ]);

        let shards = ShardLoader::select_reverse_shards(&collection, 35.68, 139.65, Some("US"));
        assert_eq!(shards, vec!["JP"]);
    }

    #[test]
    fn test_reverse_selection_ignores_ip_country_without_coordinate_evidence() {
        let collection = collection_with_bboxes(&[("US", None)]);

        let shards = ShardLoader::select_reverse_shards(&collection, 35.68, 139.65, Some("US"));
        assert_eq!(shards, Vec::<String>::new());

        let route = ShardLoader::select_reverse_route(&collection, 35.68, 139.65);
        assert_eq!(
            route.debug.country_decision,
            ReverseCountryDecision::Unresolved
        );
        assert_eq!(route.debug.outcome, ReverseRoutingOutcome::GlobalFallback);
        assert_eq!(route.debug.selected_country, None);
    }

    #[test]
    fn test_overlapping_country_bboxes_fall_through_to_head() {
        let collection = collection_with_bboxes(&[
            ("CA", Some([-141.0, 41.0, -52.0, 83.0])),
            ("US", Some([-125.0, 24.0, -66.0, 50.0])),
        ]);

        let fallback = ShardLoader::select_reverse_shards(&collection, 45.0, -100.0, Some("US"));
        assert_eq!(fallback, Vec::<String>::new());

        let no_ip = ShardLoader::select_reverse_shards(&collection, 45.0, -100.0, None);
        assert_eq!(
            no_ip,
            Vec::<String>::new(),
            "ambiguous bboxes without an IP tie-breaker should fall through to HEAD"
        );

        let foreign_ip = ShardLoader::select_reverse_shards(&collection, 45.0, -100.0, Some("JP"));
        assert_eq!(
            foreign_ip,
            Vec::<String>::new(),
            "a non-containing IP country must not decide the overlap"
        );

        let route = ShardLoader::select_reverse_route(&collection, 45.0, -100.0);
        assert_eq!(
            route.debug.country_decision,
            ReverseCountryDecision::AmbiguousBbox
        );
        assert_eq!(route.debug.bbox_candidate_count, 2);
        assert_eq!(route.debug.bbox_candidates, vec!["CA", "US"]);
        assert_eq!(route.debug.selected_country, None);
    }

    #[test]
    fn test_thimphu_aggregate_bbox_overlap_is_ambiguous_not_china() {
        let collection = collection_with_bboxes(&[
            ("BT", Some([88.7, 26.7, 92.2, 28.4])),
            ("CN", Some([73.5, 18.0, 135.1, 53.6])),
        ]);

        let route = ShardLoader::select_reverse_route(&collection, 27.4728, 89.6393);
        assert!(route.shards.is_empty());
        assert_eq!(
            route.debug.country_decision,
            ReverseCountryDecision::AmbiguousBbox
        );
        assert_eq!(route.debug.bbox_candidates, vec!["BT", "CN"]);
        assert_eq!(route.debug.selected_country, None);
    }

    #[test]
    fn test_reverse_routing_diagnostics_are_bounded_and_stably_sorted() {
        let rows: Vec<(String, Option<[f64; 4]>)> = (0..12)
            .rev()
            .map(|index| (format!("C{index:02}"), Some([-1.0, -1.0, 1.0, 1.0])))
            .collect();
        let borrowed: Vec<(&str, Option<[f64; 4]>)> =
            rows.iter().map(|(id, bbox)| (id.as_str(), *bbox)).collect();
        let collection = collection_with_bboxes(&borrowed);

        let route = ShardLoader::select_reverse_route(&collection, 0.0, 0.0);
        assert_eq!(route.debug.bbox_candidate_count, 12);
        assert_eq!(route.debug.bbox_candidates.len(), 8);
        assert_eq!(route.debug.bbox_candidates.first().unwrap(), "C00");
        assert_eq!(route.debug.bbox_candidates.last().unwrap(), "C07");
    }

    #[test]
    fn test_boston_overlap_falls_through_to_head_regardless_of_cf_country() {
        let collection = collection_with_bboxes(&[
            (
                "CA",
                Some([-141.0026607, 41.6765597, -52.6193663, 83.1370864]),
            ),
            // Disconnected US territories inflate this aggregate bbox, making
            // it much larger than CA even though Boston is in the US.
            ("US", Some([-179.2, -14.8, 179.8, 71.5])),
        ]);

        let shards = ShardLoader::select_reverse_shards(&collection, 42.3601, -71.0589, Some("US"));
        assert_eq!(shards, Vec::<String>::new());

        let no_ip = ShardLoader::select_reverse_shards(&collection, 42.3601, -71.0589, None);
        assert_eq!(
            no_ip,
            Vec::<String>::new(),
            "without a country hint the safer fallback is the global HEAD shard"
        );

        let foreign_ip =
            ShardLoader::select_reverse_shards(&collection, 42.3601, -71.0589, Some("CA"));
        assert_eq!(foreign_ip, Vec::<String>::new());
    }

    #[test]
    fn test_nearby_noncontaining_bbox_does_not_create_false_ambiguity() {
        let collection = collection_with_bboxes(&[
            ("CA", Some([-141.0, 41.0, -52.0, 83.0])),
            ("US", Some([-125.0, 24.0, -66.0, 50.0])),
        ]);

        let shards = ShardLoader::select_reverse_shards(&collection, 40.7128, -74.0060, Some("JP"));
        assert_eq!(shards, vec!["US"]);
    }

    #[test]
    fn test_tokyo_routes_to_jp_despite_overlapping_world_bbox() {
        let collection = collection_with_bboxes(&[
            ("JP", Some([122.0, 20.0, 154.0, 46.0])),
            ("RU", Some([-180.0, 41.0, 180.0, 82.0])),
            ("US", Some([-125.0, 24.0, -66.0, 50.0])),
        ]);

        let shards = ShardLoader::select_reverse_shards(&collection, 35.68, 139.69, Some("US"));
        assert_eq!(shards, vec!["JP"], "Tokyo should route to JP not US IP");

        let no_ip = ShardLoader::select_reverse_shards(&collection, 35.68, 139.69, None);
        assert_eq!(
            no_ip,
            vec!["JP"],
            "Tokyo should route to JP even without IP"
        );
    }

    #[test]
    fn test_antimeridian_bbox_contains() {
        let bbox_crossing = [170.0, -10.0, -170.0, 10.0];
        assert!(bbox_contains(0.0, 175.0, &bbox_crossing));
        assert!(bbox_contains(0.0, -175.0, &bbox_crossing));
        assert!(!bbox_contains(0.0, 0.0, &bbox_crossing));
        assert!(!bbox_contains(20.0, 175.0, &bbox_crossing));

        let collection = collection_with_bboxes(&[
            ("FJ", Some([170.0, -20.0, -175.0, -12.0])),
            ("NZ", Some([165.0, -52.0, 180.0, -34.0])),
        ]);
        let shards = ShardLoader::select_reverse_shards(&collection, -16.0, 179.0, None);
        assert_eq!(shards, vec!["FJ"]);
        let shards_west = ShardLoader::select_reverse_shards(&collection, -16.0, -178.0, None);
        assert_eq!(shards_west, vec!["FJ"]);
    }

    #[test]
    fn test_bbox_area_antimeridian() {
        let normal = [-125.0, 24.0, -66.0, 50.0];
        let crossing = [170.0, -10.0, -170.0, 10.0];
        assert!((bbox_area_deg2(&normal) - 59.0 * 26.0).abs() < 1e-6);
        assert!((bbox_area_deg2(&crossing) - 20.0 * 20.0).abs() < 1e-6);
    }

    #[test]
    fn test_distance_to_bbox_antimeridian() {
        let bbox = [170.0, -10.0, -170.0, 10.0];
        assert_eq!(distance_to_bbox(0.0, 175.0, &bbox), 0.0);
        assert_eq!(distance_to_bbox(0.0, -175.0, &bbox), 0.0);
        let outside = distance_to_bbox(0.0, 0.0, &bbox);
        assert!(outside > 1000.0);
    }

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

        let versions = get_ordered_versions(&catalog, "catalog.json");
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

        let versions = get_ordered_versions(&catalog, "catalog.json");
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

        let versions = get_ordered_versions(&catalog, "catalog.json");
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

        let versions = get_ordered_versions(&catalog, "catalog.json");
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

        let versions = get_ordered_versions(&catalog, "catalog.json");
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

        let versions = get_ordered_versions(&catalog, "catalog.json");
        assert!(versions.is_empty());
    }

    #[test]
    fn test_get_ordered_versions_uses_nested_catalog_parent_for_bare_child() {
        let catalog = StacCatalog {
            links: vec![StacLink {
                rel: "child".to_string(),
                href: "./id-collection.json".to_string(),
                latest: true,
            }],
        };

        let versions = get_ordered_versions(&catalog, "smoketest-id/catalog.json");
        assert_eq!(versions, vec!["smoketest-id"]);
    }

    fn build_test_router() -> RouterDb {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE router(token TEXT NOT NULL, shard_id TEXT NOT NULL, max_importance REAL NOT NULL, PRIMARY KEY(token, shard_id));",
        )
        .unwrap();
        let data = vec![
            ("toulouse", "FR", 0.8),
            ("toulouse", "US-TX", 0.2),
            ("france", "FR", 0.9),
            ("berlin", "DE", 0.85),
            ("guangzhou", "CN-GD", 0.7),
        ];
        for (token, shard, imp) in data {
            conn.execute(
                "INSERT INTO router VALUES (?1, ?2, ?3)",
                rusqlite::params![token, shard, imp],
            )
            .unwrap();
        }
        RouterDb { conn }
    }

    #[test]
    fn test_router_lookup_exact_token() {
        let router = build_test_router();
        let shards = router.lookup_shards("Toulouse");
        assert!(shards.contains(&"FR".to_string()));
    }

    #[test]
    fn test_router_lookup_qualified_query() {
        let router = build_test_router();
        let shards = router.lookup_shards("Toulouse, France");
        assert_eq!(shards[0], "FR");
    }

    #[test]
    fn test_router_lookup_prefix() {
        let router = build_test_router();
        let shards = router.lookup_shards("toulou");
        assert!(shards.contains(&"FR".to_string()));
    }

    #[test]
    fn test_country_suffix_parsing() {
        let collection = collection_with_bboxes(&[
            ("FR", Some([-5.0, 41.0, 9.0, 51.0])),
            ("DE", Some([5.0, 47.0, 15.0, 55.0])),
        ]);
        let shards = ShardLoader::select_shards_by_country_suffix("Paris, France", &collection);
        assert_eq!(shards, vec!["FR"]);
        let shards2 = ShardLoader::select_shards_by_country_suffix("Berlin, DE", &collection);
        assert_eq!(shards2, vec!["DE"]);
    }

    #[test]
    fn test_router_shard_filtering() {
        let collection = collection_with_bboxes(&[
            ("FR", Some([-5.0, 41.0, 9.0, 51.0])),
            ("US-TX", Some([-106.0, 25.0, -93.0, 36.0])),
        ]);
        let router = build_test_router();
        let raw = router.lookup_shards("Toulouse");
        let filtered: Vec<String> = raw
            .into_iter()
            .filter(|sid| ShardLoader::collection_has_shard(&collection, sid))
            .collect();
        assert!(filtered.contains(&"FR".to_string()));
    }

    #[test]
    fn test_places_shards_require_a_justified_region() {
        let collection = collection_with_bboxes(&[
            ("HEAD", Some([-180.0, -90.0, 180.0, 90.0])),
            ("US-CA", Some([-124.5, 32.5, -114.0, 42.1])),
            ("US-CA-places", Some([-124.5, 32.5, -114.0, 42.1])),
            ("US-NY-places", Some([-80.0, 40.0, -71.0, 45.1])),
        ]);

        // Merely having a prototype Places shard in the collection is not a
        // reason to search it for an otherwise unroutable global query.
        let no_route = ShardLoader::select_places_shards(
            &collection,
            &["HEAD".to_string()],
            &UserLocation::default(),
            MAX_EXTRA_SHARDS,
        );
        assert!(no_route.is_empty());

        let division_route = ShardLoader::select_places_shards(
            &collection,
            &["HEAD".to_string(), "US-CA".to_string()],
            &UserLocation::default(),
            MAX_EXTRA_SHARDS,
        );
        assert_eq!(division_route, vec!["US-CA-places".to_string()]);

        let caller_region = UserLocation {
            country: Some("US".to_string()),
            region_code: Some("NY".to_string()),
            ..Default::default()
        };
        let location_route = ShardLoader::select_places_shards(
            &collection,
            &["HEAD".to_string()],
            &caller_region,
            MAX_EXTRA_SHARDS,
        );
        assert_eq!(location_route, vec!["US-NY-places".to_string()]);
    }

    #[test]
    fn test_places_never_enter_generic_proximity_selection() {
        let collection = collection_with_bboxes(&[
            ("HEAD", Some([-180.0, -90.0, 180.0, 90.0])),
            ("US-CA", Some([-124.5, 32.5, -114.0, 42.1])),
            ("US-CA-places", Some([-124.5, 32.5, -114.0, 42.1])),
            ("US-NV", Some([-120.1, 35.0, -114.0, 42.1])),
        ]);

        let selected = ShardLoader::select_shards_by_proximity(&collection, 37.7, -122.4);
        assert_eq!(selected, vec!["US-CA".to_string()]);
        assert!(selected
            .iter()
            .all(|shard| !ShardLoader::is_places_shard(shard)));
    }

    #[test]
    fn test_places_have_one_total_selection_cap() {
        let collection = collection_with_bboxes(&[
            ("HEAD", Some([-180.0, -90.0, 180.0, 90.0])),
            ("US-CA", Some([-124.5, 32.5, -114.0, 42.1])),
            ("US-NV", Some([-120.1, 35.0, -114.0, 42.1])),
            ("US-AZ", Some([-114.9, 31.0, -109.0, 37.1])),
            ("US-CA-places", Some([-124.5, 32.5, -114.0, 42.1])),
            ("US-NV-places", Some([-120.1, 35.0, -114.0, 42.1])),
            ("US-AZ-places", Some([-114.9, 31.0, -109.0, 37.1])),
        ]);

        let divisions = vec![
            "HEAD".to_string(),
            "US-CA".to_string(),
            "US-NV".to_string(),
            "US-AZ".to_string(),
        ];
        let places = ShardLoader::select_places_shards(
            &collection,
            &divisions,
            &UserLocation::default(),
            MAX_EXTRA_SHARDS,
        );
        assert_eq!(places.len(), MAX_PLACES_SHARDS);
        assert_eq!(
            places,
            vec!["US-CA-places".to_string(), "US-NV-places".to_string()]
        );
    }

    #[test]
    fn test_places_share_the_total_beyond_head_budget() {
        let collection = collection_with_bboxes(&[
            ("HEAD", Some([-180.0, -90.0, 180.0, 90.0])),
            ("US-CA", Some([-124.5, 32.5, -114.0, 42.1])),
            ("US-NV", Some([-120.1, 35.0, -114.0, 42.1])),
            ("US-AZ", Some([-114.9, 31.0, -109.0, 37.1])),
            ("US-CA-places", Some([-124.5, 32.5, -114.0, 42.1])),
            ("US-NV-places", Some([-120.1, 35.0, -114.0, 42.1])),
            ("US-AZ-places", Some([-114.9, 31.0, -109.0, 37.1])),
        ]);
        let divisions = vec![
            "HEAD".to_string(),
            "US-CA".to_string(),
            "US-NV".to_string(),
            "US-AZ".to_string(),
        ];

        for (generic_count, expected_places) in [(3, 0), (2, 1), (1, 2), (0, 2)] {
            let remaining = MAX_EXTRA_SHARDS.saturating_sub(generic_count);
            let places = ShardLoader::select_places_shards(
                &collection,
                &divisions,
                &UserLocation::default(),
                remaining,
            );
            assert_eq!(places.len(), expected_places);
            assert!(generic_count + places.len() <= MAX_EXTRA_SHARDS);
            assert!(places.len() <= MAX_PLACES_SHARDS);
        }
    }

    #[test]
    fn test_explicit_places_request_reserves_a_slot_under_full_generic_budget() {
        let collection = collection_with_bboxes(&[
            ("HEAD", Some([-180.0, -90.0, 180.0, 90.0])),
            ("US-CA", Some([-124.5, 32.5, -114.0, 42.1])),
            ("US-NV", Some([-120.1, 35.0, -114.0, 42.1])),
            ("US-AZ", Some([-114.9, 31.0, -109.0, 37.1])),
            ("US-CA-places", Some([-124.5, 32.5, -114.0, 42.1])),
        ]);
        let candidates = vec![
            "US-CA".to_string(),
            "US-NV".to_string(),
            "US-AZ".to_string(),
        ];
        let (generic, places) = ShardLoader::allocate_extra_shards(
            &collection,
            &candidates,
            &UserLocation::default(),
            true,
        );
        assert_eq!(generic.len(), 2);
        assert_eq!(places, vec!["US-CA-places".to_string()]);
        assert!(generic.len() + places.len() <= MAX_EXTRA_SHARDS);
    }
}
