//! Core types for the geocoder.

use std::collections::HashSet;

use serde::{Deserialize, Serialize};

fn normalize_type_name(s: &str) -> String {
    let lower = s.trim().to_lowercase();
    if lower == "neighbourhood" {
        "neighborhood".to_string()
    } else {
        lower
    }
}

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
    /// Optional allowed division types (lowercased, normalized). None means no filtering.
    pub allowed_types: Option<HashSet<String>>,
}

impl GeocoderQuery {
    /// Create a new query with default settings.
    pub fn new(text: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            limit: 10,
            autocomplete: true,
            bias: LocationBias::None,
            allowed_types: None,
        }
    }

    /// Set the result limit.
    pub fn with_limit(mut self, limit: usize) -> Self {
        self.limit = limit.clamp(1, 40);
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

    pub fn with_allowed_types(mut self, types: Option<HashSet<String>>) -> Self {
        self.allowed_types = types.map(|set| {
            set.into_iter()
                .map(|s| normalize_type_name(&s))
                .filter(|s| !s.is_empty())
                .collect()
        });
        self
    }

    pub fn includes_place(&self) -> bool {
        self.allowed_types
            .as_ref()
            .is_some_and(|s| s.contains("place"))
    }

    pub fn is_type_allowed(&self, division_type: &str) -> bool {
        match &self.allowed_types {
            None => true,
            Some(set) => set.contains(&normalize_type_name(division_type)),
        }
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
    Full { country: String, lat: f64, lon: f64 },
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
    /// Composed ranking score (higher is better):
    /// `match_quality + 0.5 * static_importance + 0.2 * bm25_norm`,
    /// nominally 0-1.7 before location bias (which adds up to ~0.3 more).
    /// Kept unclamped through the bias/merge pipeline so bonuses can still
    /// reorder saturated results; serializers scale by 1/2 and clamp to 0-1
    /// for display.
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
    /// The row's searchable names (`;`-separated primary + alternates/
    /// exonyms), carried through from [`DivisionRow::search_name`] so callers
    /// can tell *which* name matched. `None` on legacy shards.
    ///
    /// Never serialized: this is an internal matching aid, not part of the
    /// public result shape.
    #[serde(skip)]
    pub search_name: Option<String>,
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
    /// Text relevance score from FTS (lower / more negative is better).
    /// New shards: weighted `bm25()`. Legacy shards: population-boosted BM25.
    pub text_score: f64,
    /// Static prominence on a nominal 0-1 scale. New shards: the precomputed
    /// `importance` column. Legacy shards: derived from the population-boosted
    /// BM25 score (`(-score / 50).max(0)`).
    pub static_importance: f64,
    /// Space-joined searchable names (primary + alternates/exonyms), used by
    /// the alt-name match-quality rung. New shards only; None on legacy.
    pub search_name: Option<String>,
}

impl DivisionRow {
    /// Convert to a geocoder result with the given composed importance score.
    pub fn into_result(self, importance: f64) -> GeocoderResult {
        GeocoderResult {
            gers_id: self.gers_id,
            primary_name: self.primary_name,
            lat: self.lat,
            lon: self.lon,
            // GeoJSON bbox order: [min_lon, min_lat, max_lon, max_lat]
            bbox: [
                self.bbox_xmin,
                self.bbox_ymin,
                self.bbox_xmax,
                self.bbox_ymax,
            ],
            importance,
            division_type: self.division_type,
            country: self.country,
            region: self.region,
            population: self.population,
            search_name: self.search_name,
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
    pub fn parse(s: &str) -> Option<Self> {
        match s.to_lowercase().as_str() {
            "country" => Some(Self::Country),
            "region" => Some(Self::Region),
            "county" => Some(Self::County),
            "localadmin" => Some(Self::LocalAdmin),
            "locality" => Some(Self::Locality),
            "neighborhood" | "neighbourhood" => Some(Self::Neighborhood),
            "macrohood" => Some(Self::Macrohood),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    #[test]
    fn test_allowed_types_case_insensitive_and_normalization() {
        let set: HashSet<String> = ["LOCALITY", "Country", "neighbourhood", "PLACE"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let q = GeocoderQuery::new("test").with_allowed_types(Some(set));
        assert!(q.is_type_allowed("locality"));
        assert!(q.is_type_allowed("COUNTRY"));
        assert!(q.is_type_allowed("neighborhood"));
        assert!(q.is_type_allowed("neighbourhood"));
        assert!(q.is_type_allowed("place"));
        assert!(!q.is_type_allowed("county"));
        assert!(q.includes_place());
    }

    #[test]
    fn test_allowed_types_no_filter_allows_all() {
        let q = GeocoderQuery::new("test");
        assert!(q.is_type_allowed("locality"));
        assert!(q.is_type_allowed("place"));
        assert!(!q.includes_place());
    }

    #[test]
    fn test_allowed_types_excludes_place_by_default() {
        let default_types: HashSet<String> = [
            "country",
            "region",
            "county",
            "localadmin",
            "locality",
            "neighborhood",
            "macrohood",
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        let q = GeocoderQuery::new("test").with_allowed_types(Some(default_types));
        assert!(!q.includes_place());
        assert!(q.is_type_allowed("locality"));
        assert!(!q.is_type_allowed("place"));
    }

    #[test]
    fn test_allowed_types_place_only() {
        let set: HashSet<String> = ["place"].iter().map(|s| s.to_string()).collect();
        let q = GeocoderQuery::new("starbucks").with_allowed_types(Some(set));
        assert!(q.includes_place());
        assert!(q.is_type_allowed("place"));
        assert!(!q.is_type_allowed("locality"));
    }

    #[test]
    fn test_division_type_parse_synonyms() {
        assert_eq!(
            DivisionType::parse("neighbourhood"),
            Some(DivisionType::Neighborhood)
        );
        assert_eq!(
            DivisionType::parse("NEIGHBORHOOD"),
            Some(DivisionType::Neighborhood)
        );
        assert_eq!(DivisionType::parse("place"), None);
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

/// Bounding box matching Overture schema.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BBox {
    pub xmin: f64,
    pub ymin: f64,
    pub xmax: f64,
    pub ymax: f64,
}

/// Result of a GERS ID lookup (ID -> bbox).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IdLookupResult {
    /// Overture GERS ID.
    pub id: String,
    pub bbox: BBox,
    /// Format-v3 locator fields. Flattening keeps the HTTP response additive;
    /// legacy shards leave this as None and serialize exactly as `{id, bbox}`.
    #[serde(flatten)]
    pub locator: Option<IdLocatorMetadata>,
}

/// Optional full-record locator metadata expanded from ID-index format v3.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct IdLocatorMetadata {
    pub feature_type: Option<String>,
    pub theme: Option<String>,
    pub filename: Option<String>,
    pub last_seen_release: Option<String>,
    pub registry_member: bool,
    pub exists_in_current_release: bool,
    pub overture_path: Option<String>,
}

#[cfg(test)]
mod id_lookup_tests {
    use super::*;

    fn bbox() -> BBox {
        BBox {
            xmin: -1.0,
            ymin: -2.0,
            xmax: 1.0,
            ymax: 2.0,
        }
    }

    #[test]
    fn v1_id_result_serializes_without_locator_keys() {
        let value = serde_json::to_value(IdLookupResult {
            id: "abc".into(),
            bbox: bbox(),
            locator: None,
        })
        .unwrap();
        assert_eq!(value.as_object().unwrap().len(), 2);
        assert_eq!(value["id"], "abc");
        assert!(value.get("filename").is_none());
    }

    #[test]
    fn v3_id_result_serializes_nullable_flat_locator_keys() {
        let value = serde_json::to_value(IdLookupResult {
            id: "abc".into(),
            bbox: bbox(),
            locator: Some(IdLocatorMetadata {
                feature_type: None,
                theme: None,
                filename: None,
                last_seen_release: Some("2026-05-20.0".into()),
                registry_member: true,
                exists_in_current_release: false,
                overture_path: None,
            }),
        })
        .unwrap();
        assert!(value["filename"].is_null());
        assert!(value["feature_type"].is_null());
        assert_eq!(value["registry_member"], true);
        assert_eq!(value["exists_in_current_release"], false);
    }
}
