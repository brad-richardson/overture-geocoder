//! Location bias for ranking results.
//!
//! Applies country and/or proximity-based boosting to search results.

use crate::types::{GeocoderResult, LocationBias};

// =============================================================================
// Bias constants
// =============================================================================

/// Importance boost for results in the same country (country-only bias).
/// Applied when using `LocationBias::Country`.
const COUNTRY_ONLY_BOOST: f64 = 0.15;

/// Importance boost for results in the same country (combined with coordinates).
/// Lower than `COUNTRY_ONLY_BOOST` since distance also contributes.
const COUNTRY_WITH_COORDS_BOOST: f64 = 0.1;

/// Maximum importance boost from proximity.
/// Applied to results at the exact bias coordinates.
const MAX_DISTANCE_BOOST: f64 = 0.1;

/// Distance decay reference in kilometers.
/// Results within this distance receive significant proximity boost.
/// The boost halves at this distance from the bias point.
const DISTANCE_DECAY_REFERENCE_KM: f64 = 100.0;

/// Apply location bias to search results.
///
/// Boosts results near the user's location (from country code or coordinates).
/// Re-sorts results by adjusted importance.
pub fn apply_location_bias(results: &mut [GeocoderResult], bias: &LocationBias) {
    if matches!(bias, LocationBias::None) {
        return;
    }

    for result in results.iter_mut() {
        let boost = calculate_bias_boost(result, bias);
        result.importance = (result.importance + boost).clamp(0.0, 1.0);
    }

    // Re-sort by adjusted importance (descending)
    results.sort_by(|a, b| {
        b.importance
            .partial_cmp(&a.importance)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
}

/// Calculate the bias boost for a single result.
fn calculate_bias_boost(result: &GeocoderResult, bias: &LocationBias) -> f64 {
    match bias {
        LocationBias::None => 0.0,

        LocationBias::Country(code) => {
            // Boost if result is in the same country
            if result.country.as_deref() == Some(code) {
                COUNTRY_ONLY_BOOST
            } else {
                0.0
            }
        }

        LocationBias::Coordinates { lat, lon } => {
            // Distance-based decay
            let distance_km = haversine_distance(*lat, *lon, result.lat, result.lon);
            distance_decay(distance_km)
        }

        LocationBias::Full { country, lat, lon } => {
            // Combine country boost with distance decay
            let country_boost = if result.country.as_deref() == Some(country) {
                COUNTRY_WITH_COORDS_BOOST
            } else {
                0.0
            };
            let distance_km = haversine_distance(*lat, *lon, result.lat, result.lon);
            let distance_boost = distance_decay(distance_km);

            country_boost + distance_boost
        }
    }
}

/// Calculate distance decay boost.
///
/// Results within `DISTANCE_DECAY_REFERENCE_KM` get significant boost, decaying with distance.
fn distance_decay(distance_km: f64) -> f64 {
    // Decay factor: closer = higher boost
    // Results within DISTANCE_DECAY_REFERENCE_KM get most of the boost
    MAX_DISTANCE_BOOST / (1.0 + distance_km / DISTANCE_DECAY_REFERENCE_KM)
}

/// Calculate the haversine distance between two points in kilometers.
pub fn haversine_distance(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    const EARTH_RADIUS_KM: f64 = 6371.0;

    let d_lat = (lat2 - lat1).to_radians();
    let d_lon = (lon2 - lon1).to_radians();

    let lat1_rad = lat1.to_radians();
    let lat2_rad = lat2.to_radians();

    let a = (d_lat / 2.0).sin().powi(2)
        + lat1_rad.cos() * lat2_rad.cos() * (d_lon / 2.0).sin().powi(2);

    let c = 2.0 * a.sqrt().asin();

    EARTH_RADIUS_KM * c
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_result(name: &str, country: Option<&str>, lat: f64, lon: f64) -> GeocoderResult {
        GeocoderResult {
            gers_id: format!("test-{}", name),
            primary_name: name.to_string(),
            lat,
            lon,
            // GeoJSON bbox: [min_lon, min_lat, max_lon, max_lat]
            bbox: [lon - 0.1, lat - 0.1, lon + 0.1, lat + 0.1],
            importance: 0.5,
            division_type: "locality".to_string(),
            country: country.map(String::from),
            region: None,
            population: None,
        }
    }

    #[test]
    fn test_country_bias() {
        let mut results = vec![
            make_result("Paris, TX", Some("US"), 33.66, -95.55),
            make_result("Paris, FR", Some("FR"), 48.86, 2.35),
        ];

        // Bias towards France
        apply_location_bias(&mut results, &LocationBias::Country("FR".to_string()));

        // Paris, FR should now be first
        assert_eq!(results[0].primary_name, "Paris, FR");
        assert!(results[0].importance > results[1].importance);
    }

    #[test]
    fn test_coordinate_bias() {
        let mut results = vec![
            make_result("Paris, TX", Some("US"), 33.66, -95.55),
            make_result("Paris, FR", Some("FR"), 48.86, 2.35),
        ];

        // Bias towards coordinates in France (near Paris)
        apply_location_bias(
            &mut results,
            &LocationBias::Coordinates {
                lat: 48.85,
                lon: 2.34,
            },
        );

        // Paris, FR should now be first (much closer)
        assert_eq!(results[0].primary_name, "Paris, FR");
    }

    #[test]
    fn test_haversine_distance() {
        // New York to Los Angeles: ~3940 km
        let distance = haversine_distance(40.71, -74.01, 34.05, -118.24);
        assert!((distance - 3940.0).abs() < 100.0);

        // Same point: 0 km
        let distance = haversine_distance(40.71, -74.01, 40.71, -74.01);
        assert!(distance < 0.01);
    }

    #[test]
    fn test_no_bias() {
        let mut results = vec![
            make_result("A", None, 0.0, 0.0),
            make_result("B", None, 0.0, 0.0),
        ];
        let original_order: Vec<_> = results.iter().map(|r| r.primary_name.clone()).collect();

        apply_location_bias(&mut results, &LocationBias::None);

        let new_order: Vec<_> = results.iter().map(|r| r.primary_name.clone()).collect();
        assert_eq!(original_order, new_order);
    }
}
