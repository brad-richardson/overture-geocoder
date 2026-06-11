//! Shared geographic math.

/// Mean Earth radius in kilometers.
pub const EARTH_RADIUS_KM: f64 = 6371.0;

/// Calculate the haversine distance between two points in kilometers.
///
/// Uses the atan2 form, which is numerically stable for antipodal points.
pub fn haversine_distance(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let lat1_rad = lat1.to_radians();
    let lat2_rad = lat2.to_radians();
    let delta_lat = (lat2 - lat1).to_radians();
    let delta_lon = (lon2 - lon1).to_radians();

    let a = (delta_lat / 2.0).sin().powi(2)
        + lat1_rad.cos() * lat2_rad.cos() * (delta_lon / 2.0).sin().powi(2);
    let c = 2.0 * a.sqrt().atan2((1.0 - a).sqrt());

    EARTH_RADIUS_KM * c
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_known_distance() {
        // New York to Los Angeles: ~3940 km
        let distance = haversine_distance(40.71, -74.01, 34.05, -118.24);
        assert!((distance - 3940.0).abs() < 100.0);
    }

    #[test]
    fn test_same_point() {
        assert!(haversine_distance(40.71, -74.01, 40.71, -74.01) < 0.01);
    }

    #[test]
    fn test_antipodal() {
        // Antipodal points: half the Earth's circumference, ~20015 km
        let distance = haversine_distance(0.0, 0.0, 0.0, 180.0);
        assert!((distance - 20015.0).abs() < 5.0);
    }
}
