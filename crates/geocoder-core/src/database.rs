//! Native SQLite database wrapper.
//!
//! Provides a high-level interface for querying SQLite geocoding shards.

use std::path::Path;

use rusqlite::{Connection, OpenFlags};

use crate::error::Result;
use crate::query::{calculate_boosted_score, prepare_fts_query, SEARCH_DIVISIONS_SQL};
use crate::types::{DivisionRow, GeocoderQuery, GeocoderResult};

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
    /// Uses SQLite's deserialize to load the database directly into memory
    /// without filesystem access.
    #[cfg(target_arch = "wasm32")]
    pub fn from_bytes(bytes: &[u8]) -> Result<Self> {
        use rusqlite::ffi;
        use std::ffi::CString;
        use std::ptr;

        // Open an in-memory database
        let conn = Connection::open_in_memory()?;

        // Allocate SQLite memory and copy the bytes
        let sz = bytes.len();
        let ptr = unsafe { ffi::sqlite3_malloc64(sz as u64) as *mut u8 };
        if ptr.is_null() {
            return Err(crate::error::Error::Database("Failed to allocate SQLite memory".into()));
        }
        unsafe {
            ptr::copy_nonoverlapping(bytes.as_ptr(), ptr, sz);
        }

        // Deserialize into the main database
        let schema = CString::new("main").unwrap();
        let rc = unsafe {
            ffi::sqlite3_deserialize(
                conn.handle(),
                schema.as_ptr(),
                ptr,
                sz as i64,
                sz as i64,
                ffi::SQLITE_DESERIALIZE_FREEONCLOSE | ffi::SQLITE_DESERIALIZE_READONLY,
            )
        };

        if rc != ffi::SQLITE_OK {
            // Memory will be freed by SQLite due to FREEONCLOSE flag
            return Err(crate::error::Error::Database(
                format!("sqlite3_deserialize failed: {}", rc)
            ));
        }

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
