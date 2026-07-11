//! Native SQLite database wrapper.
//!
//! Provides a high-level interface for querying SQLite geocoding shards.

use std::cell::OnceCell;
use std::path::Path;

use rusqlite::{params, Connection, OpenFlags};

use crate::error::Result;
use crate::geo::haversine_distance;
use crate::query::{
    alt_name_quality, match_quality, prepare_fts_query, NormalizedQuery, BM25_TIEBREAK_WEIGHT,
    REVERSE_CANDIDATES_PER_SUBTYPE, REVERSE_GEOCODE_RTREE_SQL, REVERSE_GEOCODE_RTREE_SQL_WKB,
    REVERSE_GEOCODE_SQL, REVERSE_GEOCODE_SQL_WKB, SEARCH_DIVISIONS_SQL,
    SEARCH_DIVISIONS_SQL_WEIGHTED, STATIC_IMPORTANCE_WEIGHT,
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
    /// Whether the reverse shard has the `divisions_reverse_rtree` index.
    has_rtree: OnceCell<bool>,
    /// Whether the forward shard carries a precomputed `importance` column
    /// (new schema with weighted multi-column FTS).
    has_static_importance: OnceCell<bool>,
    /// Whether the reverse shard has a `wkb` geometry column for exact containment.
    has_wkb: OnceCell<bool>,
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
            has_rtree: OnceCell::new(),
            has_static_importance: OnceCell::new(),
            has_wkb: OnceCell::new(),
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

    fn has_wkb(&self) -> bool {
        *self.has_wkb.get_or_init(|| {
            self.conn
                .prepare("SELECT wkb FROM divisions_reverse LIMIT 0")
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
            // chars, not bytes: a 2-3 char CJK/Cyrillic query is not "long"
            if tokens.len() == 1 && tokens[0].chars().count() >= 6 {
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
    /// - Legacy shards: population boost in the ORDER BY (`ln` is registered
    ///   as a Rust scalar function in [`Database::new`] when the build lacks
    ///   it, so the in-SQL boost is always available).
    ///
    /// Either way the in-SQL blend only protects the fetch LIMIT; the final
    /// ordering is the composed score computed here.
    fn execute_fts_search(
        &self,
        fts_query: &str,
        query: &GeocoderQuery,
    ) -> Result<Vec<GeocoderResult>> {
        let static_path = self.has_static_importance();
        let sql = if static_path {
            SEARCH_DIVISIONS_SQL_WEIGHTED
        } else {
            SEARCH_DIVISIONS_SQL
        };
        let mut stmt = self.conn.prepare_cached(sql)?;

        // Fetch more results than the final limit to allow bias to elevate
        // results that wouldn't otherwise make the cut. Re-clamp limit here:
        // the field is public, so `with_limit`'s clamp can be bypassed.
        let fetch_limit = (query.limit.clamp(1, 40) * 10).max(100) as i64;

        let rows = stmt.query_map(params![fts_query, fetch_limit], |row| {
            let population: Option<i64> = row.get(10)?;
            let search_name: Option<String> = if static_path { row.get(15)? } else { None };
            let (static_importance, text_score) = if static_path {
                // New schema: precomputed importance column + weighted bm25.
                (row.get::<_, f64>(13)?, row.get::<_, f64>(14)?)
            } else {
                // Legacy schema: derive static prominence from the
                // population-boosted BM25 score (the historical formula).
                // Clamped to the documented nominal 0-1 scale: the popboost
                // alone can push -score/50 past 1.0 for mega-cities, which
                // would let the 0.5-weighted static term exceed its ceiling.
                let boosted_score: f64 = row.get(13)?;
                ((-boosted_score / 50.0).clamp(0.0, 1.0), boosted_score)
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
                search_name,
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
        // is a small tiebreaker. Query normalization is hoisted out of the
        // per-row loop (hundreds of rows per shard).
        let normalized_query = NormalizedQuery::new(&query.text);
        let mut results: Vec<GeocoderResult> = division_rows
            .into_iter()
            .map(|row| {
                let bm25_norm = if best_text_score.is_finite() && best_text_score < 0.0 {
                    (row.text_score / best_text_score).clamp(0.0, 1.0)
                } else {
                    0.0 // degenerate: no negative scores to normalize against
                };
                // Best of the display-name ladder and the alternate-name rung
                // (exonyms like "moscow" for Москва live in search_name).
                let quality = match_quality(&row.primary_name, &normalized_query).max(
                    row.search_name
                        .as_deref()
                        .map(|names| alt_name_quality(names, &row.primary_name, &normalized_query))
                        .unwrap_or(0.0),
                );
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
    /// If the shard carries an optional `wkb` column (future build), exact
    /// polygon containment is applied after the bbox candidate prefilter.
    pub fn reverse_geocode(&self, lat: f64, lon: f64) -> Result<Option<ReverseResult>> {
        let use_wkb = self.has_wkb();
        let sql = match (self.has_rtree(), use_wkb) {
            (true, true) => REVERSE_GEOCODE_RTREE_SQL_WKB,
            (true, false) => REVERSE_GEOCODE_RTREE_SQL,
            (false, true) => REVERSE_GEOCODE_SQL_WKB,
            (false, false) => REVERSE_GEOCODE_SQL,
        };
        let mut stmt = self.conn.prepare_cached(sql)?;

        // lon is ?1, lat is ?2 (matching the SQL parameter order)
        let division_rows: Vec<ReverseDivisionRow> = if use_wkb {
            stmt.query_map(params![lon, lat, REVERSE_CANDIDATES_PER_SUBTYPE], |row| {
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
                    wkb: row.get(10)?,
                })
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?
        } else {
            stmt.query_map(params![lon, lat, REVERSE_CANDIDATES_PER_SUBTYPE], |row| {
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
                    wkb: None,
                })
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?
        };

        if division_rows.is_empty() {
            return Ok(None);
        }

        let filtered: Vec<ReverseDivisionRow> = if use_wkb {
            division_rows
                .into_iter()
                .filter(|row| {
                    if let Some(wkb) = &row.wkb {
                        point_in_wkb(wkb, lat, lon).unwrap_or(true)
                    } else {
                        true
                    }
                })
                .collect()
        } else {
            division_rows
        };

        if filtered.is_empty() {
            return Ok(None);
        }

        // Deduplicate by gers_id (for antimeridian-split divisions);
        // rows are sorted by area ascending.
        let mut seen_ids = HashSet::new();
        let deduped: Vec<ReverseDivisionRow> = filtered
            .into_iter()
            .filter(|row| seen_ids.insert(row.gers_id.clone()))
            .collect();

        let best_priority = deduped
            .iter()
            .filter_map(|r| DivisionType::parse(&r.subtype).map(|dt| dt.priority()))
            .min()
            .unwrap_or(10);

        let most_specific = if best_priority < 10 {
            let candidates: Vec<&ReverseDivisionRow> = deduped
                .iter()
                .filter(|r| {
                    DivisionType::parse(&r.subtype)
                        .map(|dt| dt.priority() == best_priority)
                        .unwrap_or(false)
                })
                .collect();
            let smallest_in_type = candidates
                .iter()
                .min_by(|a, b| {
                    a.area
                        .partial_cmp(&b.area)
                        .unwrap_or(std::cmp::Ordering::Equal)
                })
                .copied()
                .unwrap_or(&deduped[0]);
            candidates
                .iter()
                .filter(|r| r.area <= smallest_in_type.area * REVERSE_TIEBREAK_AREA_FACTOR)
                .min_by(|a, b| {
                    let da = haversine_distance(lat, lon, a.lat, a.lon);
                    let db = haversine_distance(lat, lon, b.lat, b.lon);
                    da.partial_cmp(&db).unwrap_or(std::cmp::Ordering::Equal)
                })
                .copied()
                .unwrap_or(smallest_in_type)
        } else {
            let smallest = &deduped[0];
            deduped
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
                .unwrap_or(smallest)
        };

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
    wkb: Option<Vec<u8>>,
}

fn read_u32_wkb(data: &[u8], offset: usize, little: bool) -> Option<u32> {
    if data.len() < offset + 4 {
        return None;
    }
    let b = &data[offset..offset + 4];
    Some(if little {
        u32::from_le_bytes([b[0], b[1], b[2], b[3]])
    } else {
        u32::from_be_bytes([b[0], b[1], b[2], b[3]])
    })
}

fn read_f64_wkb(data: &[u8], offset: usize, little: bool) -> Option<f64> {
    if data.len() < offset + 8 {
        return None;
    }
    let b = &data[offset..offset + 8];
    Some(if little {
        f64::from_le_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]])
    } else {
        f64::from_be_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]])
    })
}

fn point_in_ring(lon: f64, lat: f64, ring: &[(f64, f64)]) -> bool {
    let mut inside = false;
    let n = ring.len();
    if n < 3 {
        return false;
    }
    let mut j = n - 1;
    for i in 0..n {
        let (xi, yi) = ring[i];
        let (xj, yj) = ring[j];
        if (yi > lat) != (yj > lat) {
            let xinters = (lat - yi) * (xj - xi) / (yj - yi) + xi;
            if lon < xinters {
                inside = !inside;
            }
        }
        j = i;
    }
    inside
}

fn point_in_polygon_rings(lon: f64, lat: f64, rings: &[Vec<(f64, f64)>]) -> bool {
    if rings.is_empty() {
        return false;
    }
    if !point_in_ring(lon, lat, &rings[0]) {
        return false;
    }
    for hole in &rings[1..] {
        if point_in_ring(lon, lat, hole) {
            return false;
        }
    }
    true
}

fn point_in_wkb(wkb: &[u8], lat: f64, lon: f64) -> Option<bool> {
    if wkb.len() < 9 {
        return None;
    }
    let little = wkb[0] == 1;
    if wkb[0] != 0 && wkb[0] != 1 {
        return None;
    }
    let ty = read_u32_wkb(wkb, 1, little)?;
    let geom_type = ty % 1000;
    match geom_type {
        3 => {
            let mut offset = 5;
            let num_rings = read_u32_wkb(wkb, offset, little)? as usize;
            offset += 4;
            // Every ring needs at least its 4-byte point count. Bound the
            // attacker-controlled count before allocating or iterating.
            if num_rings > wkb.len().saturating_sub(offset) / 4 {
                return None;
            }
            let mut rings = Vec::with_capacity(num_rings);
            for _ in 0..num_rings {
                let num_points = read_u32_wkb(wkb, offset, little)? as usize;
                offset += 4;
                let points_len = num_points.checked_mul(16)?;
                let points_end = offset.checked_add(points_len)?;
                if wkb.len() < points_end {
                    return None;
                }
                let mut ring = Vec::with_capacity(num_points);
                for _ in 0..num_points {
                    let x = read_f64_wkb(wkb, offset, little)?;
                    offset += 8;
                    let y = read_f64_wkb(wkb, offset, little)?;
                    offset += 8;
                    ring.push((x, y));
                }
                rings.push(ring);
            }
            Some(point_in_polygon_rings(lon, lat, &rings))
        }
        6 => {
            let mut offset = 5;
            let num_polys = read_u32_wkb(wkb, offset, little)? as usize;
            offset += 4;
            // Each nested polygon needs at least byte order + type.
            if num_polys > wkb.len().saturating_sub(offset) / 5 {
                return None;
            }
            for _ in 0..num_polys {
                if wkb.len() < offset + 5 {
                    return None;
                }
                if wkb[offset] != 0 && wkb[offset] != 1 {
                    return None;
                }
                let little2 = wkb[offset] == 1;
                offset += 1;
                let ty2 = read_u32_wkb(wkb, offset, little2)?;
                offset += 4;
                if ty2 % 1000 != 3 {
                    return None;
                }
                let num_rings = read_u32_wkb(wkb, offset, little2)? as usize;
                offset += 4;
                if num_rings > wkb.len().saturating_sub(offset) / 4 {
                    return None;
                }
                let mut rings = Vec::with_capacity(num_rings);
                for _ in 0..num_rings {
                    let num_points = read_u32_wkb(wkb, offset, little2)? as usize;
                    offset += 4;
                    let points_len = num_points.checked_mul(16)?;
                    let points_end = offset.checked_add(points_len)?;
                    if wkb.len() < points_end {
                        return None;
                    }
                    let mut ring = Vec::with_capacity(num_points);
                    for _ in 0..num_points {
                        let x = read_f64_wkb(wkb, offset, little2)?;
                        offset += 8;
                        let y = read_f64_wkb(wkb, offset, little2)?;
                        offset += 8;
                        ring.push((x, y));
                    }
                    rings.push(ring);
                }
                if point_in_polygon_rings(lon, lat, &rings) {
                    return Some(true);
                }
            }
            Some(false)
        }
        _ => None,
    }
}

// Integration tests for Database are in crates/geocoder-core/tests/
// They require built shards: python scripts/build_shards.py --countries US

#[cfg(test)]
mod tests {
    use super::*;

    fn polygon_wkb(rings: &[&[(f64, f64)]], little: bool) -> Vec<u8> {
        let mut out = Vec::new();
        out.push(u8::from(little));
        if little {
            out.extend_from_slice(&3_u32.to_le_bytes());
            out.extend_from_slice(&(rings.len() as u32).to_le_bytes());
        } else {
            out.extend_from_slice(&3_u32.to_be_bytes());
            out.extend_from_slice(&(rings.len() as u32).to_be_bytes());
        }
        for ring in rings {
            if little {
                out.extend_from_slice(&(ring.len() as u32).to_le_bytes());
            } else {
                out.extend_from_slice(&(ring.len() as u32).to_be_bytes());
            }
            for (x, y) in *ring {
                if little {
                    out.extend_from_slice(&x.to_le_bytes());
                    out.extend_from_slice(&y.to_le_bytes());
                } else {
                    out.extend_from_slice(&x.to_be_bytes());
                    out.extend_from_slice(&y.to_be_bytes());
                }
            }
        }
        out
    }

    fn multipolygon_wkb(polygons: &[Vec<u8>]) -> Vec<u8> {
        let mut out = vec![1];
        out.extend_from_slice(&6_u32.to_le_bytes());
        out.extend_from_slice(&(polygons.len() as u32).to_le_bytes());
        for polygon in polygons {
            out.extend_from_slice(polygon);
        }
        out
    }

    #[test]
    fn point_in_wkb_handles_polygon_holes_and_endianness() {
        let outer = [
            (0.0, 0.0),
            (10.0, 0.0),
            (10.0, 10.0),
            (0.0, 10.0),
            (0.0, 0.0),
        ];
        let hole = [(4.0, 4.0), (6.0, 4.0), (6.0, 6.0), (4.0, 6.0), (4.0, 4.0)];
        for little in [true, false] {
            let wkb = polygon_wkb(&[&outer, &hole], little);
            assert_eq!(point_in_wkb(&wkb, 2.0, 2.0), Some(true));
            assert_eq!(point_in_wkb(&wkb, 5.0, 5.0), Some(false));
            assert_eq!(point_in_wkb(&wkb, 12.0, 12.0), Some(false));
        }
    }

    #[test]
    fn point_in_wkb_handles_multipolygon() {
        let left = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (0.0, 0.0)];
        let right = [
            (8.0, 8.0),
            (10.0, 8.0),
            (10.0, 10.0),
            (8.0, 10.0),
            (8.0, 8.0),
        ];
        let wkb = multipolygon_wkb(&[polygon_wkb(&[&left], true), polygon_wkb(&[&right], false)]);
        assert_eq!(point_in_wkb(&wkb, 1.0, 1.0), Some(true));
        assert_eq!(point_in_wkb(&wkb, 9.0, 9.0), Some(true));
        assert_eq!(point_in_wkb(&wkb, 5.0, 5.0), Some(false));
    }

    #[test]
    fn point_in_wkb_rejects_malformed_lengths_and_byte_order() {
        let mut huge_ring_count = vec![1];
        huge_ring_count.extend_from_slice(&3_u32.to_le_bytes());
        huge_ring_count.extend_from_slice(&u32::MAX.to_le_bytes());
        assert_eq!(point_in_wkb(&huge_ring_count, 0.0, 0.0), None);

        let mut huge_point_count = vec![1];
        huge_point_count.extend_from_slice(&3_u32.to_le_bytes());
        huge_point_count.extend_from_slice(&1_u32.to_le_bytes());
        huge_point_count.extend_from_slice(&u32::MAX.to_le_bytes());
        assert_eq!(point_in_wkb(&huge_point_count, 0.0, 0.0), None);

        let mut bad_nested_order = vec![1];
        bad_nested_order.extend_from_slice(&6_u32.to_le_bytes());
        bad_nested_order.extend_from_slice(&1_u32.to_le_bytes());
        bad_nested_order.extend_from_slice(&[2, 3, 0, 0, 0]);
        assert_eq!(point_in_wkb(&bad_nested_order, 0.0, 0.0), None);
    }

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

    /// The legacy query path applies the population boost via ln() in SQL;
    /// Database::new registers ln() when the bundled SQLite lacks it. If
    /// that ever breaks we want a test failure, not a runtime surprise.
    #[test]
    fn test_math_functions_available() {
        let db = db_with(FORWARD_SCHEMA);
        let ln2: f64 = db
            .conn
            .query_row("SELECT ln(2.0)", [], |row| row.get(0))
            .expect("ln() must be available (native or registered)");
        assert!((ln2 - 2.0_f64.ln()).abs() < 1e-12);
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

    /// New path: an exonym query ("moscow" for Москва) must reach the famous
    /// city through its alternate names instead of losing to an exact-named
    /// small homonym (Moscow, ID).
    #[test]
    fn test_new_schema_exonym_beats_homonym() {
        let mut setup = String::from(NEW_FORWARD_SCHEMA);
        setup.push_str(
            "INSERT INTO divisions VALUES (1, 'moscow-ru', 'locality', 'Москва, RU',
             55.75, 37.62, 37, 55, 38, 56, 13000000, 'RU', NULL, 0.92,
             'москва moscow moskva', '', 'Russia RU');\n",
        );
        insert_new_schema_row(&mut setup, 2, "moscow-id", "Moscow", 0.12);
        setup.push_str(NEW_FTS_SYNC);
        let db = db_with(&setup);

        let results = db.search(&GeocoderQuery::new("moscow")).unwrap();
        assert_eq!(results.len(), 2);
        assert_eq!(
            results[0].gers_id, "moscow-ru",
            "alt-name rung (0.95) + high importance must beat exact homonym"
        );
    }

    /// A world-famous multi-word city must NOT capture a query for one of
    /// its constituent words: search_name is a token bag, and before the
    /// primary-name exclusion "york" scored 0.95 against New York City,
    /// whose importance then buried the exact-named York.
    #[test]
    fn test_new_schema_primary_word_does_not_beat_exact_name() {
        let mut setup = String::from(NEW_FORWARD_SCHEMA);
        setup.push_str(
            "INSERT INTO divisions VALUES (1, 'nyc', 'locality', 'New York City',
             40.7, -74.0, -75, 40, -73, 41, 8500000, 'US', NULL, 0.95,
             'new york city nyc', '', 'United States US');\n",
        );
        insert_new_schema_row(&mut setup, 2, "york-uk", "York", 0.55);
        setup.push_str(NEW_FTS_SYNC);
        let db = db_with(&setup);

        let results = db.search(&GeocoderQuery::new("york")).unwrap();
        assert_eq!(results.len(), 2);
        assert_eq!(
            results[0].gers_id, "york-uk",
            "exact-named York must beat NYC's token-bag hit for 'york'"
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

    #[test]
    fn test_reverse_geocode_subtype_priority_over_area() {
        let schema = format!(
            r#"{REVERSE_SCHEMA}
            INSERT INTO divisions_reverse VALUES
              (1, 'county-1', 'county', 'Small County', 0.05, 0.05, 0.0, 0.0, 0.2, 0.2, 0.010, NULL, 'US', NULL),
              (2, 'loc-1', 'locality', 'Big Town', 0.05, 0.05, 0.0, 0.0, 0.3, 0.3, 0.012, NULL, 'US', NULL),
              (3, 'reg-1', 'region', 'State', 0.0, 0.0, -5.0, -5.0, 5.0, 5.0, 100.0, NULL, 'US', NULL);
            "#
        );
        let db = db_with(&schema);
        let result = db.reverse_geocode(0.05, 0.05).unwrap().unwrap();
        assert_eq!(
            result.gers_id, "loc-1",
            "locality should win over smaller county due to priority"
        );
    }

    #[test]
    fn test_reverse_geocode_same_subtype_distance_tiebreak() {
        let schema = format!(
            r#"{REVERSE_SCHEMA}
            INSERT INTO divisions_reverse VALUES
              (1, 'loc-far', 'locality', 'Far Town', 0.30, 0.30, 0.0, 0.0, 0.4, 0.4, 0.010, NULL, 'US', NULL),
              (2, 'loc-near', 'locality', 'Near Town', 0.06, 0.06, 0.0, 0.0, 0.5, 0.5, 0.012, NULL, 'US', NULL),
              (3, 'c-1', 'country', 'Country', 0.0, 0.0, -20.0, -20.0, 20.0, 20.0, 1600.0, NULL, 'US', NULL);
            "#
        );
        let db = db_with(&schema);
        let result = db.reverse_geocode(0.05, 0.05).unwrap().unwrap();
        assert_eq!(result.gers_id, "loc-near");
    }

    #[test]
    fn test_reverse_geocode_full_priority_chain() {
        let schema = format!(
            r#"{REVERSE_SCHEMA}
            INSERT INTO divisions_reverse VALUES
              (1, 'n-1', 'neighborhood', 'Hood', 0.05, 0.05, 0.0, 0.0, 0.1, 0.1, 0.001, NULL, 'US', NULL),
              (2, 'loc-1', 'locality', 'Town', 0.05, 0.05, 0.0, 0.0, 0.5, 0.5, 0.01, NULL, 'US', NULL),
              (3, 'county-1', 'county', 'County', 0.05, 0.05, -1.0, -1.0, 1.0, 1.0, 1.0, NULL, 'US', NULL),
              (4, 'reg-1', 'region', 'Region', 0.0, 0.0, -5.0, -5.0, 5.0, 5.0, 100.0, NULL, 'US', NULL),
              (5, 'c-1', 'country', 'Country', 0.0, 0.0, -20.0, -20.0, 20.0, 20.0, 1600.0, NULL, 'US', NULL);
            "#
        );
        let db = db_with(&schema);
        let result = db.reverse_geocode(0.05, 0.05).unwrap().unwrap();
        assert_eq!(result.gers_id, "n-1", "neighborhood highest priority");
    }
}
