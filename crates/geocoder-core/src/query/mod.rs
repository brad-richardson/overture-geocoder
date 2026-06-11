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

/// SQL query for searching divisions, with the population boost applied
/// in SQL so `ORDER BY ... LIMIT` ranks on the final score. Computing the
/// boost after the LIMIT (the old approach) could permanently drop a
/// high-population match that raw BM25 ranked below the fetch window.
///
/// Requires SQLite built with math functions (`ln`); see
/// [`SEARCH_DIVISIONS_SQL_NO_MATH`] for the fallback.
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
        bm25(divisions_fts)
            - CASE
                WHEN d.population > 0 THEN ln(d.population + 1.0) * 2.0
                ELSE 2.0
              END as boosted_score
    FROM divisions_fts
    JOIN divisions d ON divisions_fts.rowid = d.rowid
    WHERE divisions_fts MATCH ?1
    ORDER BY boosted_score
    LIMIT ?2
"#;

/// Fallback search SQL for SQLite builds without math functions.
/// Returns raw BM25 in the score column; the caller applies the
/// population boost in Rust (which can drop results beyond the LIMIT).
pub const SEARCH_DIVISIONS_SQL_NO_MATH: &str = r#"
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

/// Maximum candidate rows fetched per subtype during reverse geocoding.
/// Keeps every hierarchy level represented (the old flat `LIMIT 50` could
/// truncate region/country in dense areas) while bounding the result set.
pub const REVERSE_CANDIDATES_PER_SUBTYPE: u32 = 8;

/// SQL query for reverse geocoding via the R*Tree spatial index
/// (`divisions_reverse_rtree`, present in shards built after 2026-06).
///
/// The R*Tree stores bboxes as 32-bit floats rounded outward, so the join
/// re-checks exact containment against the attribute table. The window
/// function keeps the N smallest divisions per subtype so parent levels
/// (region, country) always survive into the result.
pub const REVERSE_GEOCODE_RTREE_SQL: &str = r#"
    WITH candidates AS (
        SELECT
            d.gers_id,
            d.subtype,
            d.primary_name,
            d.lat,
            d.lon,
            d.bbox_xmin,
            d.bbox_ymin,
            d.bbox_xmax,
            d.bbox_ymax,
            d.area,
            ROW_NUMBER() OVER (PARTITION BY d.subtype ORDER BY d.area ASC) AS rn
        FROM divisions_reverse_rtree r
        JOIN divisions_reverse d ON d.rowid = r.id
        WHERE r.xmin <= ?1 AND r.xmax >= ?1
          AND r.ymin <= ?2 AND r.ymax >= ?2
          AND d.bbox_xmin <= ?1 AND d.bbox_xmax >= ?1
          AND d.bbox_ymin <= ?2 AND d.bbox_ymax >= ?2
    )
    SELECT
        gers_id, subtype, primary_name, lat, lon,
        bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax, area
    FROM candidates
    WHERE rn <= ?3
    ORDER BY area ASC
"#;

/// Fallback reverse geocoding SQL for shards without the R*Tree table.
/// The bbox B-tree index only serves the leading column, so this scans
/// a large fraction of the table — acceptable only for legacy shards.
pub const REVERSE_GEOCODE_SQL: &str = r#"
    WITH candidates AS (
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
            ROW_NUMBER() OVER (PARTITION BY subtype ORDER BY area ASC) AS rn
        FROM divisions_reverse
        WHERE bbox_xmin <= ?1
          AND bbox_xmax >= ?1
          AND bbox_ymin <= ?2
          AND bbox_ymax >= ?2
    )
    SELECT
        gers_id, subtype, primary_name, lat, lon,
        bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax, area
    FROM candidates
    WHERE rn <= ?3
    ORDER BY area ASC
"#;
