//! Coordinate -> country shard routing for reverse geocoding.
//!
//! Pure geometry and decision logic, deliberately free of any Cloudflare
//! Worker or STAC types so the production Worker, the CLI, and offline
//! evaluation all exercise identical routing. Callers adapt their own shard
//! metadata into `(shard_id, bbox)` pairs; this module owns the antimeridian
//! bbox math and the ambiguous-overlap fallthrough policy.

use serde::Serialize;

use crate::geo::haversine_distance;

/// Sentinel shard id for the global fallback shard. It is excluded from the
/// country bbox candidates so its world-spanning bbox never routes or creates
/// false ambiguity; unresolved/ambiguous coordinates fall through to it.
pub const HEAD_SHARD_ID: &str = "HEAD";

/// Cap on the diagnostic candidate ids surfaced for an ambiguous decision.
/// Candidate ids help reproduce aggregate-bbox overlaps, but the list is
/// bounded so a malformed collection cannot inflate a response or log.
const MAX_REVERSE_ROUTING_DIAGNOSTIC_CANDIDATES: usize = 8;

/// Bounded, coordinate-free diagnostics for reverse country selection.
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

/// Result of routing a coordinate: the ordered country shards to query (empty
/// means fall through to the global HEAD shard) plus the decision diagnostics.
pub struct ReverseRouteSelection {
    pub shards: Vec<String>,
    pub debug: ReverseRoutingDebug,
}

/// Route a coordinate to a country shard from candidate `(shard_id, bbox)`
/// pairs. The [`HEAD_SHARD_ID`] sentinel and any candidate without a bbox are
/// ignored. A single containing bbox resolves to that country; zero or more
/// than one fall through to the global HEAD shard (empty `shards`).
///
/// Bboxes are `[min_lon, min_lat, max_lon, max_lat]` and may cross the
/// antimeridian (min_lon > max_lon).
pub fn select_reverse_route<'a, I>(items: I, lat: f64, lon: f64) -> ReverseRouteSelection
where
    I: IntoIterator<Item = (&'a str, Option<&'a [f64; 4]>)>,
{
    let mut containing: Vec<String> = items
        .into_iter()
        .filter_map(|(shard_id, bbox)| {
            if shard_id == HEAD_SHARD_ID {
                return None;
            }
            let bbox = bbox?;
            if bbox_contains(lat, lon, bbox) {
                Some(shard_id.to_string())
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

/// Wrap a longitude into the [-180, 180] range.
pub fn normalize_lon(mut lon: f64) -> f64 {
    while lon > 180.0 {
        lon -= 360.0;
    }
    while lon < -180.0 {
        lon += 360.0;
    }
    lon
}

/// Whether `bbox` (`[min_lon, min_lat, max_lon, max_lat]`) contains the point,
/// handling antimeridian-crossing boxes where min_lon > max_lon.
pub fn bbox_contains(lat: f64, lon: f64, bbox: &[f64; 4]) -> bool {
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

/// Great-circle distance (km) from a point to the nearest edge of `bbox`,
/// zero when the point is inside. Handles antimeridian-crossing boxes.
pub fn distance_to_bbox(lat: f64, lon: f64, bbox: &[f64; 4]) -> f64 {
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

#[cfg(test)]
mod tests {
    use super::*;

    /// Route rows of `(shard_id, bbox)` through [`select_reverse_route`].
    fn route(rows: &[(&str, Option<[f64; 4]>)], lat: f64, lon: f64) -> ReverseRouteSelection {
        select_reverse_route(rows.iter().map(|(id, bbox)| (*id, bbox.as_ref())), lat, lon)
    }

    fn shards(rows: &[(&str, Option<[f64; 4]>)], lat: f64, lon: f64) -> Vec<String> {
        route(rows, lat, lon).shards
    }

    #[test]
    fn test_reverse_selection_prefers_request_coordinates_to_ip_country() {
        let rows = [
            ("JP", Some([122.0, 20.0, 154.0, 46.0])),
            ("US", Some([-125.0, 24.0, -66.0, 50.0])),
        ];
        assert_eq!(shards(&rows, 35.68, 139.65), vec!["JP"]);
    }

    #[test]
    fn test_reverse_selection_ignores_ip_country_without_coordinate_evidence() {
        let rows = [("US", None)];

        assert_eq!(shards(&rows, 35.68, 139.65), Vec::<String>::new());

        let route = route(&rows, 35.68, 139.65);
        assert_eq!(
            route.debug.country_decision,
            ReverseCountryDecision::Unresolved
        );
        assert_eq!(route.debug.outcome, ReverseRoutingOutcome::GlobalFallback);
        assert_eq!(route.debug.selected_country, None);
    }

    #[test]
    fn test_head_sentinel_is_never_a_candidate() {
        // A world-spanning HEAD bbox must not route or add ambiguity.
        let rows = [
            ("HEAD", Some([-180.0, -90.0, 180.0, 90.0])),
            ("JP", Some([122.0, 20.0, 154.0, 46.0])),
        ];
        let route = route(&rows, 35.68, 139.65);
        assert_eq!(route.shards, vec!["JP"]);
        assert_eq!(
            route.debug.country_decision,
            ReverseCountryDecision::UniqueBbox
        );
        assert_eq!(route.debug.bbox_candidate_count, 1);
    }

    #[test]
    fn test_overlapping_country_bboxes_fall_through_to_head() {
        let rows = [
            ("CA", Some([-141.0, 41.0, -52.0, 83.0])),
            ("US", Some([-125.0, 24.0, -66.0, 50.0])),
        ];

        assert_eq!(
            shards(&rows, 45.0, -100.0),
            Vec::<String>::new(),
            "ambiguous bboxes should fall through to HEAD"
        );

        let route = route(&rows, 45.0, -100.0);
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
        let rows = [
            ("BT", Some([88.7, 26.7, 92.2, 28.4])),
            ("CN", Some([73.5, 18.0, 135.1, 53.6])),
        ];

        let route = route(&rows, 27.4728, 89.6393);
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
        let route = select_reverse_route(
            rows.iter().map(|(id, bbox)| (id.as_str(), bbox.as_ref())),
            0.0,
            0.0,
        );
        assert_eq!(route.debug.bbox_candidate_count, 12);
        assert_eq!(route.debug.bbox_candidates.len(), 8);
        assert_eq!(route.debug.bbox_candidates.first().unwrap(), "C00");
        assert_eq!(route.debug.bbox_candidates.last().unwrap(), "C07");
    }

    #[test]
    fn test_boston_overlap_falls_through_to_head_regardless_of_cf_country() {
        let rows = [
            (
                "CA",
                Some([-141.0026607, 41.6765597, -52.6193663, 83.1370864]),
            ),
            // Disconnected US territories inflate this aggregate bbox, making
            // it much larger than CA even though Boston is in the US.
            ("US", Some([-179.2, -14.8, 179.8, 71.5])),
        ];

        assert_eq!(
            shards(&rows, 42.3601, -71.0589),
            Vec::<String>::new(),
            "without a country hint the safer fallback is the global HEAD shard"
        );
    }

    #[test]
    fn test_nearby_noncontaining_bbox_does_not_create_false_ambiguity() {
        let rows = [
            ("CA", Some([-141.0, 41.0, -52.0, 83.0])),
            ("US", Some([-125.0, 24.0, -66.0, 50.0])),
        ];

        assert_eq!(shards(&rows, 40.7128, -74.0060), vec!["US"]);
    }

    #[test]
    fn test_tokyo_routes_to_jp_despite_overlapping_world_bbox() {
        let rows = [
            ("JP", Some([122.0, 20.0, 154.0, 46.0])),
            ("RU", Some([-180.0, 41.0, 180.0, 82.0])),
            ("US", Some([-125.0, 24.0, -66.0, 50.0])),
        ];

        assert_eq!(
            shards(&rows, 35.68, 139.69),
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

        let rows = [
            ("FJ", Some([170.0, -20.0, -175.0, -12.0])),
            ("NZ", Some([165.0, -52.0, 180.0, -34.0])),
        ];
        assert_eq!(shards(&rows, -16.0, 179.0), vec!["FJ"]);
        assert_eq!(shards(&rows, -16.0, -178.0), vec!["FJ"]);
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
