//! Native SQLite database wrapper.
//!
//! Provides a high-level interface for querying SQLite geocoding shards.

use std::cell::OnceCell;
use std::path::Path;

use rusqlite::{params, Connection, OpenFlags};

use crate::error::Result;
use crate::geo::haversine_distance;
use crate::query::{
    calculate_boosted_score, match_quality, prepare_fts_query, BM25_TIEBREAK_WEIGHT,
    REVERSE_CANDIDATES_PER_SUBTYPE, REVERSE_GEOCODE_RTREE_SQL, REVERSE_GEOCODE_SQL,
    SEARCH_DIVISIONS_SQL, SEARCH_DIVISIONS_SQL_NO_MATH, SEARCH_DIVISIONS_SQL_WEIGHTED,
    STATIC_IMPORTANCE_WEIGHT,
};
use crate::types::{
    DivisionRow, DivisionType, GeocoderQuery, GeocoderResult, HierarchyEntry, ReverseResult,
};

use std::collections::HashSet;

/// When tie-breaking reverse geocode candidates by centroid distance, only
/// consider candidates whose area is within this factor of the smallest.
/// Prevents a distant-but-slightly-larger division from displacing a
/// genuinely containing one, while still correcting bbox-overlap false hits.
const REVERSE_TIEBREAK_AREA_FACTOR: f64 = 4.0;

/// A SQLite database connection for geocoding queries.
pub struct Database {
    conn: Connection,
    /// Whether the build supports SQL math functions (`ln`); probed lazily.
    has_math_fns: OnceCell<bool>,
    /// Whether the reverse shard has the `divisions_reverse_rtree` index.
    has_rtree: OnceCell<bool>,
    /// Whether the forward shard carries a precomputed `importance` column
    /// (new schema with weighted multi-column FTS).
    has_static_importance: OnceCell<bool>,
}

impl Database {
    fn new(conn: Connection) -> Result<Self> {
        // The bundled SQLite is compiled without SQLITE_ENABLE_MATH_FUNCTIONS,
        // so register ln() as a Rust scalar function (matching SQLite's
        // semantics: NULL for x <= 0). The boost-in-SQL search path depends
        // on it, on native and wasm32 alike.
        let has_native_ln = conn.query_row("SELECT ln(1.0)", [], |_| Ok(())).is_ok();
        if !has_native_ln {
            use rusqlite::functions::FunctionFlags;
            conn.create_scalar_function(
                "ln",
                1,
                FunctionFlags::SQLITE_UTF8
                    | FunctionFlags::SQLITE_DETERMINISTIC
                    | FunctionFlags::SQLITE_INNOCUOUS,
                |ctx| {
                    let x: f64 = ctx.get(0)?;
                    Ok(if x > 0.0 { Some(x.ln()) } else { None })
                },
            )?;
        }

        Ok(Self {
            conn,
            has_math_fns: OnceCell::new(),
            has_rtree: OnceCell::new(),
            has_static_importance: OnceCell::new(),
        })
    }

    /// Open a database from a file path.
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let conn = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;

        // Configure for read-only performance
        conn.execute_batch(
            "PRAGMA cache_size = -64000; -- 64MB
             PRAGMA mmap_size = 268435456; -- 256MB
             PRAGMA temp_store = MEMORY;",
        )?;

        Self::new(conn)
    }

    /// Open a database from bytes.
    ///
    /// Uses rusqlite's deserialize API (sqlite3_deserialize) on all targets,
    /// so the shard is served entirely from memory — no temp file.
    /// Note: The database must NOT be in WAL mode (use journal_mode=DELETE
    /// when building).
    pub fn from_bytes(bytes: &[u8]) -> Result<Self> {
        use rusqlite::MAIN_DB;
        use std::io::Cursor;

        let mut conn = Connection::open_in_memory()?;
        conn.deserialize_read_exact(
            MAIN_DB,
            Cursor::new(bytes),
            bytes.len(),
            true, // read_only
        )?;

        conn.execute_batch("PRAGMA temp_store = MEMORY;")?;

        Self::new(conn)
    }

    /// Whether this SQLite build provides math functions (`ln` etc.).
    fn has_math_fns(&self) -> bool {
        *self.has_math_fns.get_or_init(|| {
            self.conn
                .query_row("SELECT ln(1.0)", [], |_| Ok(()))
                .is_ok()
        })
    }

    /// Whether the forward shard has the precomputed `importance` column
    /// (new schema: static prominence + weighted multi-column FTS). Probed
    /// by preparing a statement against the column; legacy shards fail the
    /// prepare and take the population-boost path.
    fn has_static_importance(&self) -> bool {
        *self.has_static_importance.get_or_init(|| {
            self.conn
                .prepare("SELECT importance FROM divisions LIMIT 0")
                .is_ok()
        })
    }

    /// Whether the reverse shard contains the R*Tree spatial index.
    fn has_rtree(&self) -> bool {
        *self.has_rtree.get_or_init(|| {
            self.conn
                .query_row(
                    "SELECT 1 FROM sqlite_master WHERE name = 'divisions_reverse_rtree'",
                    [],
                    |_| Ok(()),
                )
                .is_ok()
        })
    }

    /// Search for divisions matching the query.
    ///
    /// Returns up to `limit * 10` results (minimum 100) sorted by importance.
    /// Callers should apply location bias and then truncate to the desired limit.
    /// This allows bias to elevate results that wouldn't make the initial top N.
    pub fn search(&self, query: &GeocoderQuery) -> Result<Vec<GeocoderResult>> {
        let fts_query = prepare_fts_query(&query.text, query.autocomplete);

        if fts_query.is_empty() {
            return Ok(vec![]);
        }

        let results = self.execute_fts_search(&fts_query, query)?;

        // Single-token fallback: if AND returned nothing and query is one long token,
        // retry with prefix wildcard to match compact aliases (e.g., "newyork" → "newyork"*)
        if results.is_empty() {
            let tokens: Vec<&str> = query.text.split_whitespace().collect();
            if tokens.len() == 1 && tokens[0].len() >= 6 {
                let fallback_query = prepare_fts_query(&query.text, true);
                if fallback_query != fts_query {
                    let mut fallback = self.execute_fts_search(&fallback_query, query)?;
                    for r in &mut fallback {
                        r.importance *= 0.8;
                    }
                    return Ok(fallback);
                }
            }
        }

        Ok(results)
    }

    /// Execute an FTS5 search and return results scored with the composed
    /// ranking: `match_quality + 0.5 * static_importance + 0.2 * bm25_norm`.
    ///
    /// The SQL is schema-detected per shard:
    /// - New shards (precomputed `importance` column + weighted multi-column
    ///   FTS): blend text score with static importance in the ORDER BY.
    /// - Legacy shards: population boost in the ORDER BY when the build has
    ///   math functions, else raw BM25 with the boost applied in Rust.
    ///
    /// Either way the in-SQL blend only protects the fetch LIMIT; the final
    /// ordering is the composed score computed here.
    fn execute_fts_search(
        &self,
        fts_query: &str,
        query: &GeocoderQuery,
    ) -> Result<Vec<GeocoderResult>> {
        let static_path = self.has_static_importance();
        // Legacy path: population boost applied inside the SQL ORDER BY when
        // the build has math functions, so high-population matches can't be
        // cut off by the fetch LIMIT before the boost is applied.
        let in_sql_boost = !static_path && self.has_math_fns();
        let sql = if static_path {
            SEARCH_DIVISIONS_SQL_WEIGHTED
        } else if in_sql_boost {
            SEARCH_DIVISIONS_SQL
        } else {
            SEARCH_DIVISIONS_SQL_NO_MATH
        };
        let mut stmt = self.conn.prepare_cached(sql)?;

        // Fetch more results than the final limit to allow bias to elevate
        // results that wouldn't otherwise make the cut. Re-clamp limit here:
        // the field is public, so `with_limit`'s clamp can be bypassed.
        let fetch_limit = (query.limit.clamp(1, 40) * 10).max(100) as i64;

        let rows = stmt.query_map(params![fts_query, fetch_limit], |row| {
            let population: Option<i64> = row.get(10)?;
            let (static_importance, text_score) = if static_path {
                // New schema: precomputed importance column + weighted bm25.
                (row.get::<_, f64>(13)?, row.get::<_, f64>(14)?)
            } else {
                // Legacy schema: derive static prominence from the
                // population-boosted BM25 score (the historical formula).
                let score: f64 = row.get(13)?;
                let boosted_score = if in_sql_boost {
                    score
                } else {
                    calculate_boosted_score(score, population)
                };
                ((-boosted_score / 50.0).max(0.0), boosted_score)
            };

            Ok(DivisionRow {
                rowid: row.get(0)?,
                gers_id: row.get(1)?,
                division_type: row.get(2)?,
                primary_name: row.get(3)?,
                lat: row.get(4)?,
                lon: row.get(5)?,
                bbox_xmin: row.get(6)?,
                bbox_ymin: row.get(7)?,
                bbox_xmax: row.get(8)?,
                bbox_ymax: row.get(9)?,
                population,
                country: row.get(11)?,
                region: row.get(12)?,
                text_score,
                static_importance,
            })
        })?;

        // Collect rows, propagating any SQLite errors instead of silently dropping them
        let division_rows: Vec<DivisionRow> = rows.collect::<std::result::Result<Vec<_>, _>>()?;

        // BM25-style scores are negative (more negative = better); normalize
        // by the best (most negative) score in this shard's result set so the
        // tiebreaker is comparable across shards with different IDF stats.
        let best_text_score = division_rows
            .iter()
            .map(|r| r.text_score)
            .fold(f64::INFINITY, f64::min);

        // Compose the final score per result (P3): match quality dominates,
        // static importance disambiguates same-name places, normalized BM25
        // is a small tiebreaker.
        let mut results: Vec<GeocoderResult> = division_rows
            .into_iter()
            .map(|row| {
                let bm25_norm = if best_text_score.is_finite() && best_text_score < 0.0 {
                    (row.text_score / best_text_score).clamp(0.0, 1.0)
                } else {
                    0.0 // degenerate: no negative scores to normalize against
                };
                let quality = match_quality(&row.primary_name, &query.text);
                let importance = quality
                    + STATIC_IMPORTANCE_WEIGHT * row.static_importance
                    + BM25_TIEBREAK_WEIGHT * bm25_norm;
                row.into_result(importance)
            })
            .collect();

        // Sort by composed importance (descending)
        results.sort_by(|a, b| {
            b.importance
                .partial_cmp(&a.importance)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        // Don't truncate here - let caller apply bias first, then truncate
        Ok(results)
    }

    /// Get the number of records in the divisions table.
    pub fn count(&self) -> Result<u64> {
        let count: i64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM divisions", [], |row| row.get(0))?;
        Ok(count as u64)
    }

    /// Get metadata value by key.
    pub fn get_metadata(&self, key: &str) -> Result<Option<String>> {
        let result: std::result::Result<String, _> =
            self.conn
                .query_row("SELECT value FROM metadata WHERE key = ?1", [key], |row| {
                    row.get(0)
                });

        match result {
            Ok(value) => Ok(Some(value)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// Reverse geocode a lat/lon coordinate.
    ///
    /// Returns the most specific division containing the point, along with
    /// a hierarchy of parent divisions.
    ///
    /// This method expects a reverse geocoding shard (divisions_reverse table).
    /// Shards built with the `divisions_reverse_rtree` spatial index use an
    /// indexed lookup; legacy shards fall back to a bbox range scan.
    pub fn reverse_geocode(&self, lat: f64, lon: f64) -> Result<Option<ReverseResult>> {
        let sql = if self.has_rtree() {
            REVERSE_GEOCODE_RTREE_SQL
        } else {
            REVERSE_GEOCODE_SQL
        };
        let mut stmt = self.conn.prepare_cached(sql)?;

        // lon is ?1, lat is ?2 (matching the SQL parameter order)
        let rows = stmt.query_map(params![lon, lat, REVERSE_CANDIDATES_PER_SUBTYPE], |row| {
            Ok(ReverseDivisionRow {
                gers_id: row.get(0)?,
                subtype: row.get(1)?,
                primary_name: row.get(2)?,
                lat: row.get(3)?,
                lon: row.get(4)?,
                bbox_xmin: row.get(5)?,
                bbox_ymin: row.get(6)?,
                bbox_xmax: row.get(7)?,
                bbox_ymax: row.get(8)?,
                area: row.get(9)?,
            })
        })?;

        let division_rows: Vec<ReverseDivisionRow> =
            rows.collect::<std::result::Result<Vec<_>, _>>()?;

        if division_rows.is_empty() {
            return Ok(None);
        }

        // Deduplicate by gers_id (for antimeridian-split divisions);
        // rows are sorted by area ascending.
        let mut seen_ids = HashSet::new();
        let deduped: Vec<ReverseDivisionRow> = division_rows
            .into_iter()
            .filter(|row| seen_ids.insert(row.gers_id.clone()))
            .collect();

        // Bbox containment is not polygon containment: in dense areas many
        // sibling divisions' bboxes overlap the point. Instead of blindly
        // taking the smallest bbox, tie-break candidates of the same subtype
        // and comparable area by centroid distance to the query point.
        let smallest = &deduped[0];
        let most_specific = deduped
            .iter()
            .filter(|row| {
                row.subtype == smallest.subtype
                    && row.area <= smallest.area * REVERSE_TIEBREAK_AREA_FACTOR
            })
            .min_by(|a, b| {
                let da = haversine_distance(lat, lon, a.lat, a.lon);
                let db = haversine_distance(lat, lon, b.lat, b.lon);
                da.partial_cmp(&db).unwrap_or(std::cmp::Ordering::Equal)
            })
            .unwrap_or(smallest);

        // Build hierarchy: the chosen division for its own subtype, plus the
        // smallest-area division of every other subtype.
        let mut hierarchy = Vec::new();
        let mut seen_subtypes = HashSet::new();

        if let Some(div_type) = DivisionType::parse(&most_specific.subtype) {
            seen_subtypes.insert(div_type);
            hierarchy.push(HierarchyEntry {
                gers_id: most_specific.gers_id.clone(),
                subtype: most_specific.subtype.clone(),
                name: most_specific.primary_name.clone(),
            });
        }

        for row in &deduped {
            if let Some(div_type) = DivisionType::parse(&row.subtype) {
                if seen_subtypes.insert(div_type) {
                    hierarchy.push(HierarchyEntry {
                        gers_id: row.gers_id.clone(),
                        subtype: row.subtype.clone(),
                        name: row.primary_name.clone(),
                    });
                }
            }
        }

        // Sort hierarchy by priority (most specific first)
        hierarchy.sort_by_key(|h| {
            DivisionType::parse(&h.subtype)
                .map(|dt| dt.priority())
                .unwrap_or(10)
        });

        // Calculate distance from query point to division centroid
        let distance_km = haversine_distance(lat, lon, most_specific.lat, most_specific.lon);

        // Determine confidence based on area
        let confidence = if most_specific.area < 0.01 {
            "high"
        } else if most_specific.area < 0.1 {
            "medium"
        } else {
            "low"
        };

        Ok(Some(ReverseResult {
            gers_id: most_specific.gers_id.clone(),
            primary_name: most_specific.primary_name.clone(),
            subtype: most_specific.subtype.clone(),
            lat: most_specific.lat,
            lon: most_specific.lon,
            bbox: [
                most_specific.bbox_xmin,
                most_specific.bbox_ymin,
                most_specific.bbox_xmax,
                most_specific.bbox_ymax,
            ],
            distance_km,
            confidence: confidence.to_string(),
            hierarchy,
        }))
    }
}

/// Raw reverse geocoding row from the database.
#[derive(Debug, Clone)]
struct ReverseDivisionRow {
    gers_id: String,
    subtype: String,
    primary_name: String,
    lat: f64,
    lon: f64,
    bbox_xmin: f64,
    bbox_ymin: f64,
    bbox_xmax: f64,
    bbox_ymax: f64,
    area: f64,
}

// Integration tests for Database are in crates/geocoder-core/tests/
// They require built shards: python scripts/build_shards.py --countries US

#[cfg(test)]
mod tests {
    use super::*;

    fn db_with(schema_and_data: &str) -> Database {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(schema_and_data).unwrap();
        Database::new(conn).unwrap()
    }

    const FORWARD_SCHEMA: &str = r#"
        CREATE TABLE divisions (
            rowid INTEGER PRIMARY KEY,
            gers_id TEXT, type TEXT, primary_name TEXT,
            lat REAL, lon REAL,
            bbox_xmin REAL, bbox_ymin REAL, bbox_xmax REAL, bbox_ymax REAL,
            population INTEGER, country TEXT, region TEXT
        );
        CREATE VIRTUAL TABLE divisions_fts USING fts5(
            primary_name, content='divisions', content_rowid='rowid'
        );
    "#;

    /// New forward shard schema: precomputed static `importance` column plus
    /// the weighted three-column FTS index (see the schema contract in
    /// scripts/build_shards.py).
    const NEW_FORWARD_SCHEMA: &str = r#"
        CREATE TABLE divisions (
            rowid INTEGER PRIMARY KEY,
            gers_id TEXT, type TEXT, primary_name TEXT,
            lat REAL, lon REAL,
            bbox_xmin REAL, bbox_ymin REAL, bbox_xmax REAL, bbox_ymax REAL,
            population INTEGER, country TEXT, region TEXT,
            importance REAL NOT NULL,
            search_name TEXT, search_alias TEXT, search_context TEXT
        );
        CREATE VIRTUAL TABLE divisions_fts USING fts5(
            search_name, search_alias, search_context,
            content='divisions', content_rowid='rowid',
            tokenize='unicode61 remove_diacritics 2',
            prefix='2 3 4'
        );
    "#;

    /// Populate the new-schema FTS index from the content table.
    const NEW_FTS_SYNC: &str =
        "INSERT INTO divisions_fts(rowid, search_name, search_alias, search_context)
         SELECT rowid, search_name, search_alias, search_context FROM divisions;";

    const REVERSE_SCHEMA: &str = r#"
        CREATE TABLE divisions_reverse (
            rowid INTEGER PRIMARY KEY,
            gers_id TEXT NOT NULL, subtype TEXT NOT NULL, primary_name TEXT NOT NULL,
            lat REAL NOT NULL, lon REAL NOT NULL,
            bbox_xmin REAL NOT NULL, bbox_ymin REAL NOT NULL,
            bbox_xmax REAL NOT NULL, bbox_ymax REAL NOT NULL,
            area REAL NOT NULL, population INTEGER, country TEXT, region TEXT
        );
    "#;

    /// The deployed query path depends on SQL math functions; if the bundled
    /// SQLite ever drops them, we want a test failure, not a fallback surprise.
    #[test]
    fn test_math_functions_available() {
        let db = db_with(FORWARD_SCHEMA);
        assert!(
            db.has_math_fns(),
            "bundled SQLite lacks ln(); the boost-in-SQL search path is disabled"
        );
    }

    /// A high-population city must outrank low-population matches even when
    /// raw BM25 scores are identical — the boost has to happen before LIMIT.
    #[test]
    fn test_population_boost_in_sql_ordering() {
        let mut setup = String::from(FORWARD_SCHEMA);
        // Many identical-BM25 rows; only one has a large population.
        for i in 0..30 {
            setup.push_str(&format!(
                "INSERT INTO divisions VALUES ({i}, 'id-{i}', 'locality', 'Springfield',
                 0.0, 0.0, -1, -1, 1, 1, {pop}, 'US', NULL);\n",
                pop = if i == 29 { 1_000_000 } else { 100 }
            ));
        }
        setup.push_str("INSERT INTO divisions_fts(rowid, primary_name) SELECT rowid, primary_name FROM divisions;");
        let db = db_with(&setup);

        let query = GeocoderQuery::new("springfield").with_limit(5);
        let results = db.search(&query).unwrap();
        assert_eq!(
            results[0].gers_id, "id-29",
            "high-population match should rank first"
        );
        // Legacy path end-to-end: composed score = match_quality (1.0 for an
        // exact name match) + derived static importance + bm25 tiebreaker.
        assert!(
            results[0].importance >= 1.0,
            "exact match should carry full match quality, got {}",
            results[0].importance
        );
    }

    /// Schema detection: the `importance` probe must distinguish old shards
    /// (no column) from new shards (precomputed static importance).
    #[test]
    fn test_static_importance_schema_detection() {
        let old = db_with(FORWARD_SCHEMA);
        assert!(
            !old.has_static_importance(),
            "legacy schema must take the population-boost path"
        );

        let new = db_with(NEW_FORWARD_SCHEMA);
        assert!(
            new.has_static_importance(),
            "new schema must take the static-importance path"
        );
    }

    fn insert_new_schema_row(
        setup: &mut String,
        rowid: i64,
        gers_id: &str,
        name: &str,
        importance: f64,
    ) {
        setup.push_str(&format!(
            "INSERT INTO divisions VALUES ({rowid}, '{gers_id}', 'locality', '{name}',
             0.0, 0.0, -1, -1, 1, 1, NULL, 'US', NULL, {importance},
             '{name}', '', 'United States US');\n",
        ));
    }

    /// New path: with identical names (equal match quality), precomputed
    /// static importance decides the order.
    #[test]
    fn test_new_schema_static_importance_ordering() {
        let mut setup = String::from(NEW_FORWARD_SCHEMA);
        // Low-importance Paris first so raw insertion order can't pass the test.
        insert_new_schema_row(&mut setup, 1, "paris-low", "Paris", 0.15);
        insert_new_schema_row(&mut setup, 2, "paris-high", "Paris", 0.92);
        setup.push_str(NEW_FTS_SYNC);
        let db = db_with(&setup);

        let results = db.search(&GeocoderQuery::new("paris")).unwrap();
        assert_eq!(results.len(), 2);
        assert_eq!(
            results[0].gers_id, "paris-high",
            "higher static importance must win between identical names"
        );
        assert!(results[0].importance > results[1].importance);
    }

    /// New path: the match-quality ladder orders exact match above
    /// word-boundary prefix above bare prefix, given equal importance.
    #[test]
    fn test_new_schema_match_quality_ladder() {
        let mut setup = String::from(NEW_FORWARD_SCHEMA);
        // Insert in reverse of the expected order.
        insert_new_schema_row(&mut setup, 1, "parisville", "Parisville", 0.4);
        insert_new_schema_row(&mut setup, 2, "paris-township", "Paris Township", 0.4);
        insert_new_schema_row(&mut setup, 3, "paris", "Paris", 0.4);
        setup.push_str(NEW_FTS_SYNC);
        let db = db_with(&setup);

        let results = db.search(&GeocoderQuery::new("paris")).unwrap();
        let order: Vec<&str> = results.iter().map(|r| r.gers_id.as_str()).collect();
        assert_eq!(
            order,
            vec!["paris", "paris-township", "parisville"],
            "exact > word-boundary prefix > bare prefix"
        );
    }

    fn insert_reverse_rows(extra: &str) -> String {
        // Two overlapping neighborhoods: "wrong" has the smaller bbox but its
        // centroid is far from the query point (0.05, 0.05); "right" contains
        // the point with a nearby centroid and comparable area.
        // Plus locality/region/country parents.
        format!(
            r#"{REVERSE_SCHEMA}
            INSERT INTO divisions_reverse VALUES
              (1, 'n-wrong', 'neighborhood', 'Wrongville', 0.30, 0.30, 0.0, 0.0, 0.32, 0.32, 0.010, NULL, 'US', NULL),
              (2, 'n-right', 'neighborhood', 'Rightville', 0.06, 0.06, 0.0, 0.0, 0.40, 0.40, 0.012, NULL, 'US', NULL),
              (3, 'loc-1', 'locality', 'Big City', 0.2, 0.2, -0.5, -0.5, 0.5, 0.5, 1.0, NULL, 'US', NULL),
              (4, 'reg-1', 'region', 'Stateland', 0.0, 0.0, -5.0, -5.0, 5.0, 5.0, 100.0, NULL, 'US', NULL),
              (5, 'c-1', 'country', 'Countryland', 0.0, 0.0, -20.0, -20.0, 20.0, 20.0, 1600.0, NULL, 'US', NULL);
            {extra}"#
        )
    }

    #[test]
    fn test_reverse_geocode_legacy_scan() {
        let db = db_with(&insert_reverse_rows(""));
        assert!(!db.has_rtree());
        let result = db.reverse_geocode(0.05, 0.05).unwrap().unwrap();
        // Distance tie-break should pick Rightville despite Wrongville's
        // slightly smaller bbox.
        assert_eq!(result.gers_id, "n-right");
        // Hierarchy must include all parent levels (old LIMIT 50 could drop them).
        let subtypes: Vec<&str> = result
            .hierarchy
            .iter()
            .map(|h| h.subtype.as_str())
            .collect();
        assert_eq!(
            subtypes,
            vec!["neighborhood", "locality", "region", "country"]
        );
        assert_eq!(result.hierarchy[0].gers_id, "n-right");
    }

    #[test]
    fn test_reverse_geocode_rtree_path() {
        let extra = r#"
            CREATE VIRTUAL TABLE divisions_reverse_rtree USING rtree(
                id, xmin, xmax, ymin, ymax
            );
            INSERT INTO divisions_reverse_rtree
                SELECT rowid, bbox_xmin, bbox_xmax, bbox_ymin, bbox_ymax
                FROM divisions_reverse;
        "#;
        let db = db_with(&insert_reverse_rows(extra));
        assert!(db.has_rtree());
        let result = db.reverse_geocode(0.05, 0.05).unwrap().unwrap();
        assert_eq!(result.gers_id, "n-right");
        assert_eq!(result.hierarchy.len(), 4);

        // A point outside everything returns None.
        assert!(db.reverse_geocode(60.0, 60.0).unwrap().is_none());
    }
}
