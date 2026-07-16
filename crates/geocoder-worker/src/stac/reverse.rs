//! Reverse geocoding: coordinate -> country routing plus bbox/geometry helpers.

use geocoder_core::{geo::haversine_distance, ReverseResult};
use serde::Serialize;
use worker::*;

use super::catalog::{with_version_fallback, StacCollection};
use super::{not_found, ShardLoader};

const MAX_REVERSE_ROUTING_DIAGNOSTIC_CANDIDATES: usize = 8;

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

impl ShardLoader {
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
    fn select_reverse_shards(collection: &StacCollection, lat: f64, lon: f64) -> Vec<String> {
        Self::select_reverse_route(collection, lat, lon).shards
    }

    /// Reverse geocode a lat/lon coordinate.
    /// Falls back to older versions if the latest version's shards are unavailable.
    pub async fn reverse_geocode(&self, lat: f64, lon: f64) -> Result<ReverseSearchResult> {
        with_version_fallback!(self, "reverse", version, {
            self.try_reverse_geocode(version, lat, lon).await
        })
    }

    /// Attempt reverse geocode against a specific version.
    async fn try_reverse_geocode(
        &self,
        version: &str,
        lat: f64,
        lon: f64,
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

pub(crate) fn distance_to_bbox(lat: f64, lon: f64, bbox: &[f64; 4]) -> f64 {
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
    use crate::stac::catalog::collection_with_bboxes;

    #[test]
    fn test_reverse_selection_prefers_request_coordinates_to_ip_country() {
        let collection = collection_with_bboxes(&[
            ("JP", Some([122.0, 20.0, 154.0, 46.0])),
            ("US", Some([-125.0, 24.0, -66.0, 50.0])),
        ]);

        let shards = ShardLoader::select_reverse_shards(&collection, 35.68, 139.65);
        assert_eq!(shards, vec!["JP"]);
    }

    #[test]
    fn test_reverse_selection_ignores_ip_country_without_coordinate_evidence() {
        let collection = collection_with_bboxes(&[("US", None)]);

        let shards = ShardLoader::select_reverse_shards(&collection, 35.68, 139.65);
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

        let fallback = ShardLoader::select_reverse_shards(&collection, 45.0, -100.0);
        assert_eq!(
            fallback,
            Vec::<String>::new(),
            "ambiguous bboxes should fall through to HEAD"
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

        let shards = ShardLoader::select_reverse_shards(&collection, 42.3601, -71.0589);
        assert_eq!(
            shards,
            Vec::<String>::new(),
            "without a country hint the safer fallback is the global HEAD shard"
        );
    }

    #[test]
    fn test_nearby_noncontaining_bbox_does_not_create_false_ambiguity() {
        let collection = collection_with_bboxes(&[
            ("CA", Some([-141.0, 41.0, -52.0, 83.0])),
            ("US", Some([-125.0, 24.0, -66.0, 50.0])),
        ]);

        let shards = ShardLoader::select_reverse_shards(&collection, 40.7128, -74.0060);
        assert_eq!(shards, vec!["US"]);
    }

    #[test]
    fn test_tokyo_routes_to_jp_despite_overlapping_world_bbox() {
        let collection = collection_with_bboxes(&[
            ("JP", Some([122.0, 20.0, 154.0, 46.0])),
            ("RU", Some([-180.0, 41.0, 180.0, 82.0])),
            ("US", Some([-125.0, 24.0, -66.0, 50.0])),
        ]);

        let shards = ShardLoader::select_reverse_shards(&collection, 35.68, 139.69);
        assert_eq!(
            shards,
            vec!["JP"],
            "Tokyo should route to JP despite the overlapping world bbox"
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
        let shards = ShardLoader::select_reverse_shards(&collection, -16.0, 179.0);
        assert_eq!(shards, vec!["FJ"]);
        let shards_west = ShardLoader::select_reverse_shards(&collection, -16.0, -178.0);
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
}
