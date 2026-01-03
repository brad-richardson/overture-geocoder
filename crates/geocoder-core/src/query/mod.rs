//! Query preparation and execution.

mod bias;
mod fts;
mod merge;

pub use bias::{apply_exact_match_bonus, apply_location_bias};
pub use fts::prepare_fts_query;
pub use merge::merge_results;

// =============================================================================
// Scoring constants
// =============================================================================

/// Multiplier for the natural log of population in boosted score calculation.
/// Higher values increase the ranking advantage of high-population places.
/// With 2.0, a city with 1M population gets ~27.6 points of boost (ln(1M) * 2.0).
pub const POPULATION_BOOST_MULTIPLIER: f64 = 2.0;

/// Penalty applied to places with no population data.
/// This gives a slight advantage to places with known population over unknowns.
/// Set to match the boost a place with population=1 would receive (ln(2) * 2.0 ≈ 1.4).
pub const MISSING_POPULATION_PENALTY: f64 = 2.0;

/// SQL query for searching divisions.
/// Note: BM25 scoring and population boost are computed in Rust for portability.
pub const SEARCH_DIVISIONS_SQL: &str = r#"
    SELECT
        d.rowid,
        d.gers_id,
        d.type,
        d.primary_name,
        d.lat,
        d.lon,
        d.bbox_xmin,
        d.bbox_ymin,
        d.bbox_xmax,
        d.bbox_ymax,
        d.population,
        d.country,
        d.region,
        bm25(divisions_fts) as bm25_score
    FROM divisions_fts
    JOIN divisions d ON divisions_fts.rowid = d.rowid
    WHERE divisions_fts MATCH ?1
    ORDER BY bm25_score
    LIMIT ?2
"#;

/// Calculate boosted score from BM25 and population.
/// Lower score = better match.
pub fn calculate_boosted_score(bm25_score: f64, population: Option<i64>) -> f64 {
    match population {
        Some(pop) if pop > 0 => {
            bm25_score - ((pop as f64 + 1.0).ln() * POPULATION_BOOST_MULTIPLIER)
        }
        _ => bm25_score - MISSING_POPULATION_PENALTY,
    }
}

/// SQL query for reverse geocoding (bbox containment).
pub const REVERSE_GEOCODE_SQL: &str = r#"
    SELECT
        gers_id,
        subtype,
        primary_name,
        lat,
        lon,
        bbox_xmin,
        bbox_ymin,
        bbox_xmax,
        bbox_ymax,
        area,
        population,
        country,
        region
    FROM divisions_reverse
    WHERE bbox_xmin <= ?1
      AND bbox_xmax >= ?1
      AND bbox_ymin <= ?2
      AND bbox_ymax >= ?2
    ORDER BY area ASC
    LIMIT 50
"#;
