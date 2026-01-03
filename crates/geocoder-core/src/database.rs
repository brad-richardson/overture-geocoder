//! Native SQLite database wrapper.
//!
//! Provides a high-level interface for querying SQLite geocoding shards.

use std::path::Path;

use rusqlite::{Connection, OpenFlags};

use crate::error::Result;
use crate::query::{calculate_boosted_score, prepare_fts_query, REVERSE_GEOCODE_SQL, SEARCH_DIVISIONS_SQL};
use crate::types::{DivisionRow, DivisionType, GeocoderQuery, GeocoderResult, HierarchyEntry, ReverseResult};

use std::collections::HashSet;

/// A SQLite database connection for geocoding queries.
pub struct Database {
    conn: Connection,
    /// Temp file path to clean up on drop (only used by `from_bytes`).
    temp_file: Option<std::path::PathBuf>,
}

impl Drop for Database {
    fn drop(&mut self) {
        if let Some(path) = &self.temp_file {
            let _ = std::fs::remove_file(path);
        }
    }
}

impl Database {
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

        Ok(Self {
            conn,
            temp_file: None,
        })
    }

    /// Open a database from bytes.
    ///
    /// On native: creates a temp file (cleaned up when dropped).
    /// On WASM: uses SQLite's in-memory deserialize.
    #[cfg(not(target_arch = "wasm32"))]
    pub fn from_bytes(bytes: &[u8]) -> Result<Self> {
        // Write bytes to a temp file (cleaned up on Drop)
        let temp_path = std::env::temp_dir().join(format!("geocoder-{}.db", uuid_v4()));
        std::fs::write(&temp_path, bytes)?;

        let conn = Connection::open_with_flags(
            &temp_path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;

        // Configure for read-only performance
        conn.execute_batch(
            "PRAGMA cache_size = -64000; -- 64MB
             PRAGMA mmap_size = 268435456; -- 256MB
             PRAGMA temp_store = MEMORY;",
        )?;

        Ok(Self {
            conn,
            temp_file: Some(temp_path),
        })
    }

    /// Open a database from bytes (WASM version).
    ///
    /// Uses rusqlite's deserialize API to load the database from bytes.
    /// Note: The database must NOT be in WAL mode (use journal_mode=DELETE when building).
    #[cfg(target_arch = "wasm32")]
    pub fn from_bytes(bytes: &[u8]) -> Result<Self> {
        use rusqlite::MAIN_DB;
        use std::io::Cursor;

        // Open an in-memory database first
        let mut conn = Connection::open_in_memory()?;

        // Deserialize the bytes into this connection
        // This uses rusqlite's serialize feature which wraps sqlite3_deserialize
        conn.deserialize_read_exact(
            MAIN_DB,
            Cursor::new(bytes),
            bytes.len(),
            true, // read_only
        )?;

        // Configure for read-only performance
        conn.execute_batch("PRAGMA temp_store = MEMORY;")?;

        Ok(Self {
            conn,
            temp_file: None,
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

        let mut stmt = self.conn.prepare_cached(SEARCH_DIVISIONS_SQL)?;

        // Fetch more results than the final limit to allow bias to elevate
        // results that wouldn't otherwise make the cut.
        let fetch_limit = (query.limit * 10).max(100);

        let rows = stmt.query_map([&fts_query, &fetch_limit.to_string()], |row| {
            let population: Option<i64> = row.get(10)?;
            let bm25_score: f64 = row.get(13)?;
            let boosted_score = calculate_boosted_score(bm25_score, population);

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
                boosted_score,
            })
        })?;

        // Collect rows, propagating any SQLite errors instead of silently dropping them
        let division_rows: Vec<DivisionRow> = rows.collect::<std::result::Result<Vec<_>, _>>()?;

        // Convert to results and re-sort by boosted score (population boost affects ordering)
        let mut results: Vec<GeocoderResult> = division_rows
            .into_iter()
            .map(|row| row.into_result())
            .collect();

        // Sort by importance (descending) since population boost changes ranking
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
        let result: std::result::Result<String, _> = self.conn.query_row(
            "SELECT value FROM metadata WHERE key = ?1",
            [key],
            |row| row.get(0),
        );

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
    pub fn reverse_geocode(&self, lat: f64, lon: f64) -> Result<Option<ReverseResult>> {
        let mut stmt = self.conn.prepare_cached(REVERSE_GEOCODE_SQL)?;

        // Query for divisions whose bbox contains this point
        // lon is ?1, lat is ?2 (matching the SQL parameter order)
        let rows = stmt.query_map([lon, lat], |row| {
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
                population: row.get(10)?,
                country: row.get(11)?,
                region: row.get(12)?,
            })
        })?;

        let division_rows: Vec<ReverseDivisionRow> = rows.collect::<std::result::Result<Vec<_>, _>>()?;

        if division_rows.is_empty() {
            return Ok(None);
        }

        // Deduplicate by gers_id (for antimeridian-split divisions)
        let mut seen_ids = HashSet::new();
        let deduped: Vec<ReverseDivisionRow> = division_rows
            .into_iter()
            .filter(|row| seen_ids.insert(row.gers_id.clone()))
            .collect();

        // First row is the most specific (smallest area) due to ORDER BY area ASC
        let most_specific = &deduped[0];

        // Build hierarchy by collecting one of each subtype
        let mut hierarchy = Vec::new();
        let mut seen_subtypes = HashSet::new();

        for row in &deduped {
            if let Some(div_type) = DivisionType::from_str(&row.subtype) {
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
            DivisionType::from_str(&h.subtype)
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
    population: Option<i64>,
    country: Option<String>,
    region: Option<String>,
}

/// Calculate distance between two points using Haversine formula.
fn haversine_distance(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    const EARTH_RADIUS_KM: f64 = 6371.0;

    let lat1_rad = lat1.to_radians();
    let lat2_rad = lat2.to_radians();
    let delta_lat = (lat2 - lat1).to_radians();
    let delta_lon = (lon2 - lon1).to_radians();

    let a = (delta_lat / 2.0).sin().powi(2)
        + lat1_rad.cos() * lat2_rad.cos() * (delta_lon / 2.0).sin().powi(2);
    let c = 2.0 * a.sqrt().atan2((1.0 - a).sqrt());

    EARTH_RADIUS_KM * c
}

/// Generate a simple UUID v4 for temp file names.
fn uuid_v4() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};

    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();

    format!("{:032x}", timestamp)
}

// Integration tests for Database are in crates/geocoder-core/tests/
// They require built shards: python scripts/build_shards.py --countries US
