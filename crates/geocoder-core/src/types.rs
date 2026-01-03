//! Core types for the geocoder.

use serde::{Deserialize, Serialize};

/// Query parameters for forward geocoding.
#[derive(Debug, Clone)]
pub struct GeocoderQuery {
    /// Search text (e.g., "Boston, MA").
    pub text: String,
    /// Maximum number of results to return.
    pub limit: usize,
    /// Enable autocomplete (prefix matching on last token).
    pub autocomplete: bool,
    /// Location bias for ranking.
    pub bias: LocationBias,
}

impl GeocoderQuery {
    /// Create a new query with default settings.
    pub fn new(text: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            limit: 10,
            autocomplete: true,
            bias: LocationBias::None,
        }
    }

    /// Set the result limit.
    pub fn with_limit(mut self, limit: usize) -> Self {
        self.limit = limit.min(40).max(1);
        self
    }

    /// Set autocomplete mode.
    pub fn with_autocomplete(mut self, autocomplete: bool) -> Self {
        self.autocomplete = autocomplete;
        self
    }

    /// Set location bias.
    pub fn with_bias(mut self, bias: LocationBias) -> Self {
        self.bias = bias;
        self
    }
}

/// Location bias for ranking results.
#[derive(Debug, Clone, Default)]
pub enum LocationBias {
    /// No location bias.
    #[default]
    None,
    /// Bias towards a specific country.
    Country(String),
    /// Bias towards specific coordinates.
    Coordinates { lat: f64, lon: f64 },
    /// Bias towards both country and coordinates.
    Full {
        country: String,
        lat: f64,
        lon: f64,
    },
}

/// A geocoding result.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GeocoderResult {
    /// Overture GERS ID (stable identifier).
    pub gers_id: String,
    /// Display name (e.g., "Boston, MA").
    pub primary_name: String,
    /// Latitude of the centroid.
    pub lat: f64,
    /// Longitude of the centroid.
    pub lon: f64,
    /// Bounding box in GeoJSON order: [min_lon, min_lat, max_lon, max_lat].
    #[serde(rename = "boundingbox")]
    pub bbox: [f64; 4],
    /// Importance score (0-1, higher is more important).
    pub importance: f64,
    /// Division type (locality, county, etc.).
    #[serde(rename = "type")]
    pub division_type: String,
    /// Country code (ISO 3166-1 alpha-2).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub country: Option<String>,
    /// Region code (e.g., "US-MA").
    #[serde(skip_serializing_if = "Option::is_none")]
    pub region: Option<String>,
    /// Population (if available).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub population: Option<i64>,
}

/// Raw division row from the database.
#[derive(Debug, Clone)]
pub struct DivisionRow {
    pub rowid: i64,
    pub gers_id: String,
    pub division_type: String,
    pub primary_name: String,
    pub lat: f64,
    pub lon: f64,
    pub bbox_xmin: f64,
    pub bbox_ymin: f64,
    pub bbox_xmax: f64,
    pub bbox_ymax: f64,
    pub population: Option<i64>,
    pub country: Option<String>,
    pub region: Option<String>,
    /// BM25 + population boosted score (lower is better).
    pub boosted_score: f64,
}

impl DivisionRow {
    /// Convert to a geocoder result.
    pub fn into_result(self) -> GeocoderResult {
        GeocoderResult {
            gers_id: self.gers_id,
            primary_name: self.primary_name,
            lat: self.lat,
            lon: self.lon,
            // GeoJSON bbox order: [min_lon, min_lat, max_lon, max_lat]
            bbox: [self.bbox_xmin, self.bbox_ymin, self.bbox_xmax, self.bbox_ymax],
            // Convert boosted score to importance (0-1 scale).
            // More negative score = higher importance.
            importance: (-self.boosted_score / 50.0).clamp(0.0, 1.0),
            division_type: self.division_type,
            country: self.country,
            region: self.region,
            population: self.population,
        }
    }
}

/// Division type classification.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DivisionType {
    Country,
    Region,
    County,
    LocalAdmin,
    Locality,
    Neighborhood,
    Macrohood,
}

impl DivisionType {
    /// Get the priority for hierarchy building (lower = more specific).
    pub fn priority(self) -> u8 {
        match self {
            Self::Neighborhood => 1,
            Self::Macrohood => 2,
            Self::Locality => 3,
            Self::LocalAdmin => 4,
            Self::County => 5,
            Self::Region => 6,
            Self::Country => 7,
        }
    }

    /// Parse from string.
    pub fn from_str(s: &str) -> Option<Self> {
        match s.to_lowercase().as_str() {
            "country" => Some(Self::Country),
            "region" => Some(Self::Region),
            "county" => Some(Self::County),
            "localadmin" => Some(Self::LocalAdmin),
            "locality" => Some(Self::Locality),
            "neighborhood" => Some(Self::Neighborhood),
            "macrohood" => Some(Self::Macrohood),
            _ => None,
        }
    }
}

/// A reverse geocoding result.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReverseResult {
    /// Overture GERS ID.
    pub gers_id: String,
    /// Display name.
    pub primary_name: String,
    /// Division subtype.
    pub subtype: String,
    /// Latitude of the centroid.
    pub lat: f64,
    /// Longitude of the centroid.
    pub lon: f64,
    /// Bounding box in GeoJSON order: [min_lon, min_lat, max_lon, max_lat].
    #[serde(rename = "boundingbox")]
    pub bbox: [f64; 4],
    /// Distance from query point in kilometers.
    pub distance_km: f64,
    /// Confidence indicator.
    pub confidence: String,
    /// Administrative hierarchy.
    pub hierarchy: Vec<HierarchyEntry>,
}

/// An entry in the administrative hierarchy.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HierarchyEntry {
    /// Overture GERS ID.
    pub gers_id: String,
    /// Division subtype.
    pub subtype: String,
    /// Display name.
    pub name: String,
}
