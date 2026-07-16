//! Forward search: user-location handling and shard selection
//! (suffix / router / proximity / places) across HEAD and extra shards.

use geocoder_core::{query::apply_location_bias, GeocoderQuery, GeocoderResult, LocationBias};
use serde::Serialize;
use worker::*;

use super::cache::IMMUTABLE_CACHE_TTL;
use super::catalog::{with_version_fallback, StacCollection, StacItem};
use super::reverse::distance_to_bbox;
use super::router_db::RouterDb;
use super::{not_found, ShardLoader};

// Shard selection constants
const NEARBY_THRESHOLD_KM: f64 = 200.0; // Include shards within this distance
const MAX_LOCATION_SHARDS: usize = 2;
// Total extra shards beyond HEAD, including opt-in Places. Previously HEAD+2
// (nearby only); now HEAD+3 to accommodate suffix + router + nearby while still bounding
// worst-case first-touch cost (additional R2 fetch + deserialize). Tail
// latency increase is acceptable for the recall improvement.
const MAX_EXTRA_SHARDS: usize = 3;
const MAX_PLACES_SHARDS: usize = 2;

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

impl ShardLoader {
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stac::catalog::collection_with_bboxes;

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
