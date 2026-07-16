//! Forward text router: token -> candidate shard lookup and its R2 loader.

use std::cell::RefCell;
use std::rc::Rc;

use worker::*;

use super::cache::IMMUTABLE_CACHE_TTL;
use super::ShardLoader;

const MAX_ROUTER_SHARDS: usize = 2;

thread_local! {
    static ROUTER_CACHE: RefCell<Vec<(String, Rc<RouterDb>, usize)>> =
        const { RefCell::new(Vec::new()) };
}

pub struct RouterDb {
    conn: rusqlite::Connection,
}

impl RouterDb {
    pub fn from_bytes(bytes: &[u8]) -> std::result::Result<Self, String> {
        use rusqlite::MAIN_DB;
        use std::io::Cursor;
        let mut conn = rusqlite::Connection::open_in_memory().map_err(|e| e.to_string())?;
        conn.deserialize_read_exact(MAIN_DB, Cursor::new(bytes), bytes.len(), true)
            .map_err(|e| e.to_string())?;
        conn.execute_batch("PRAGMA temp_store = MEMORY;")
            .map_err(|e| e.to_string())?;
        Ok(Self { conn })
    }

    fn fold_diacritic(c: char) -> char {
        match c {
            'à' | 'á' | 'â' | 'ã' | 'ä' | 'å' => 'a',
            'è' | 'é' | 'ê' | 'ë' => 'e',
            'ì' | 'í' | 'î' | 'ï' => 'i',
            'ò' | 'ó' | 'ô' | 'õ' | 'ö' | 'ø' => 'o',
            'ù' | 'ú' | 'û' | 'ü' => 'u',
            'ç' => 'c',
            'ñ' => 'n',
            'ý' | 'ÿ' => 'y',
            // Greek final sigma -> medial sigma. `char::to_lowercase` is
            // context-free and lowercases an uppercase sigma to the medial
            // form, but a pre-lowercased final sigma stays final; folding it
            // here converges both. Kept in lockstep with the Python builder's
            // _ROUTER_FOLD_TABLE in scripts/build_shards.py.
            'ς' => 'σ',
            _ => c,
        }
    }

    fn normalize_token(s: &str) -> String {
        s.trim()
            .chars()
            .flat_map(char::to_lowercase)
            .map(Self::fold_diacritic)
            .collect()
    }

    fn tokenize_query(query: &str) -> Vec<String> {
        let normalized = Self::normalize_token(query);
        normalized
            .split(|c: char| !c.is_alphanumeric())
            .filter(|t| t.chars().count() >= 3)
            .filter(|t| t.chars().any(|c| c.is_alphabetic()))
            .map(|s| s.to_string())
            .collect()
    }

    pub fn lookup_shards(&self, query: &str) -> Vec<String> {
        let tokens = Self::tokenize_query(query);
        if tokens.is_empty() {
            return Vec::new();
        }
        let mut scores: std::collections::HashMap<String, f64> = std::collections::HashMap::new();
        let total = tokens.len() as f64;
        for (idx, token) in tokens.iter().enumerate() {
            let weight = 1.0 + (idx as f64 / total) * 0.5;
            if let Ok(mut stmt) = self.conn.prepare(
                "SELECT shard_id, max_importance FROM router WHERE token = ?1 ORDER BY max_importance DESC, shard_id ASC LIMIT 4",
            ) {
                if let Ok(rows) = stmt.query_map([token], |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, f64>(1)?))
                }) {
                    for row in rows.flatten() {
                        let (shard_id, imp) = row;
                        *scores.entry(shard_id).or_insert(0.0) += imp * weight;
                    }
                }
            }
        }
        if scores.is_empty() {
            for (idx, token) in tokens.iter().enumerate() {
                let weight = 1.0 + (idx as f64 / total) * 0.5;
                let pattern = format!("{}%", token);
                if let Ok(mut stmt) = self.conn.prepare(
                    "SELECT shard_id, max_importance FROM router WHERE token LIKE ?1 ORDER BY max_importance DESC, shard_id ASC LIMIT 3",
                ) {
                    if let Ok(rows) = stmt.query_map([pattern], |row| {
                        Ok((row.get::<_, String>(0)?, row.get::<_, f64>(1)?))
                    }) {
                        for row in rows.flatten() {
                            let (shard_id, imp) = row;
                            *scores.entry(shard_id).or_insert(0.0) += imp * weight * 0.8;
                        }
                    }
                }
            }
        }
        let mut ranked: Vec<(String, f64)> = scores.into_iter().collect();
        // Score descending, then shard_id ascending as a deterministic
        // tiebreak: a locality contributing identical importance to its
        // country and region shards must not let the 2-shard cap pick
        // engine/hash-order-dependent winners.
        ranked.sort_by(|a, b| {
            b.1.partial_cmp(&a.1)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| a.0.cmp(&b.0))
        });
        ranked
            .into_iter()
            .take(MAX_ROUTER_SHARDS)
            .map(|(id, _)| id)
            .collect()
    }
}

impl ShardLoader {
    pub(crate) async fn load_router_db(&self, version: &str) -> Result<Option<Rc<RouterDb>>> {
        for key in [
            format!("{}/router.db", version),
            format!("{}/shards/router.db", version),
        ] {
            let cached = ROUTER_CACHE.with(|c| {
                let mut cache = c.borrow_mut();
                cache.iter().position(|(k, _, _)| k == &key).map(|pos| {
                    let entry = cache.remove(pos);
                    let hit = Rc::clone(&entry.1);
                    cache.push(entry);
                    hit
                })
            });
            if let Some(db) = cached {
                console_log!("Router cache HIT: {}", key);
                return Ok(Some(db));
            }

            if let Some(bytes) = self.cached_get(&key, IMMUTABLE_CACHE_TTL).await? {
                let size = bytes.len();
                let router = Rc::new(RouterDb::from_bytes(&bytes).map_err(|e| {
                    Error::RustError(format!("Failed to open router {}: {}", key, e))
                })?);
                drop(bytes);
                ROUTER_CACHE.with(|c| {
                    let mut cache = c.borrow_mut();
                    if !cache.iter().any(|(k, _, _)| k == &key) {
                        cache.push((key.clone(), Rc::clone(&router), size));
                        while cache.len() > 2 {
                            let (evicted, _, _) = cache.remove(0);
                            console_log!("Router cache evict: {}", evicted);
                        }
                    }
                });
                console_log!("Router DB loaded: {} ({}B)", key, size);
                return Ok(Some(router));
            }
        }
        console_log!("Router DB not found for version {}", version);
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stac::catalog::collection_with_bboxes;

    fn build_test_router() -> RouterDb {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE router(token TEXT NOT NULL, shard_id TEXT NOT NULL, max_importance REAL NOT NULL, PRIMARY KEY(token, shard_id));",
        )
        .unwrap();
        let data = vec![
            ("toulouse", "FR", 0.8),
            ("toulouse", "US-TX", 0.2),
            ("france", "FR", 0.9),
            ("berlin", "DE", 0.85),
            ("guangzhou", "CN-GD", 0.7),
        ];
        for (token, shard, imp) in data {
            conn.execute(
                "INSERT INTO router VALUES (?1, ?2, ?3)",
                rusqlite::params![token, shard, imp],
            )
            .unwrap();
        }
        RouterDb { conn }
    }

    #[test]
    fn test_router_lookup_exact_token() {
        let router = build_test_router();
        let shards = router.lookup_shards("Toulouse");
        assert!(shards.contains(&"FR".to_string()));
    }

    #[test]
    fn test_router_lookup_qualified_query() {
        let router = build_test_router();
        let shards = router.lookup_shards("Toulouse, France");
        assert_eq!(shards[0], "FR");
    }

    #[test]
    fn test_router_lookup_prefix() {
        let router = build_test_router();
        let shards = router.lookup_shards("toulou");
        assert!(shards.contains(&"FR".to_string()));
    }

    #[test]
    fn test_normalize_matches_builder_fixture() {
        // Shared contract with scripts/build_shards.py `_router_normalize`;
        // both sides must agree byte-for-byte on router token text.
        let fixture = include_str!("../../../../tests/fixtures/router_normalization_cases.json");
        let cases: Vec<serde_json::Value> = serde_json::from_str(fixture).unwrap();
        assert!(!cases.is_empty());
        for case in &cases {
            let input = case["input"].as_str().unwrap();
            let expected = case["normalized"].as_str().unwrap();
            assert_eq!(
                RouterDb::normalize_token(input),
                expected,
                "normalization diverged for {input:?}"
            );
        }
    }

    #[test]
    fn test_router_tie_selection_is_deterministic() {
        // A locality that contributes identical importance to two shards (its
        // country and its region) leaves the 2-shard cap to a tie. The
        // ORDER BY + comparator tiebreak must pick the same shard every run,
        // regardless of insertion or hash order.
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE router(token TEXT NOT NULL, shard_id TEXT NOT NULL, max_importance REAL NOT NULL, PRIMARY KEY(token, shard_id));",
        )
        .unwrap();
        // Insert the higher shard_id first so a stable result cannot come from
        // insertion order alone.
        for (token, shard, imp) in [
            ("springfield", "US-OR", 0.5_f64),
            ("springfield", "US-IL", 0.5_f64),
        ] {
            conn.execute(
                "INSERT INTO router VALUES (?1, ?2, ?3)",
                rusqlite::params![token, shard, imp],
            )
            .unwrap();
        }
        let router = RouterDb { conn };
        let shards = router.lookup_shards("Springfield");
        assert_eq!(shards, vec!["US-IL".to_string(), "US-OR".to_string()]);
        // Stable across repeated lookups.
        for _ in 0..8 {
            assert_eq!(router.lookup_shards("Springfield"), shards);
        }
    }

    #[test]
    fn test_router_shard_filtering() {
        let collection = collection_with_bboxes(&[
            ("FR", Some([-5.0, 41.0, 9.0, 51.0])),
            ("US-TX", Some([-106.0, 25.0, -93.0, 36.0])),
        ]);
        let router = build_test_router();
        let raw = router.lookup_shards("Toulouse");
        let filtered: Vec<String> = raw
            .into_iter()
            .filter(|sid| ShardLoader::collection_has_shard(&collection, sid))
            .collect();
        assert!(filtered.contains(&"FR".to_string()));
    }
}
