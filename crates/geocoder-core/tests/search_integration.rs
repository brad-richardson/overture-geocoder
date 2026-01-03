//! Integration tests for geocoder search.
//!
//! These tests require built shards. To run:
//!
//! ```bash
//! # Build the US shard
//! python scripts/build_shards.py --countries US
//!
//! # Run integration tests
//! cargo test --test search_integration
//! ```

use geocoder_core::{Database, GeocoderQuery};
use std::path::Path;

const US_SHARD_PATH: &str = "../../shards/2026-01-02.0/shards/US.db";

fn get_db() -> Option<Database> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join(US_SHARD_PATH);
    if path.exists() {
        Database::open(&path).ok()
    } else {
        None
    }
}

#[test]
fn test_search_new_york() {
    let Some(db) = get_db() else {
        eprintln!("Skipping: US shard not found. Run: python scripts/build_shards.py --countries US");
        return;
    };

    let query = GeocoderQuery::new("new york");
    let results = db.search(&query).unwrap();

    assert!(!results.is_empty(), "Should return results for 'new york'");

    // City of New York should be #1 due to population boost
    let first = &results[0];
    assert!(
        first.primary_name.contains("New York"),
        "First result should contain 'New York', got: {}",
        first.primary_name
    );
    assert_eq!(
        first.country.as_deref(),
        Some("US"),
        "First result should be in US"
    );
    // NYC has ~8.4M population
    assert!(
        first.importance > 0.7,
        "NYC should have high importance, got: {}",
        first.importance
    );
}

#[test]
fn test_search_short_name_nyc() {
    let Some(db) = get_db() else {
        eprintln!("Skipping: US shard not found");
        return;
    };

    let query = GeocoderQuery::new("nyc");
    let results = db.search(&query).unwrap();

    assert!(!results.is_empty(), "Should return results for 'nyc'");
    assert!(
        results[0].primary_name.contains("New York"),
        "NYC should match City of New York"
    );
}

#[test]
fn test_search_alternate_name_big_apple() {
    let Some(db) = get_db() else {
        eprintln!("Skipping: US shard not found");
        return;
    };

    let query = GeocoderQuery::new("big apple");
    let results = db.search(&query).unwrap();

    assert!(!results.is_empty(), "Should return results for 'big apple'");
    assert!(
        results[0].primary_name.contains("New York"),
        "Big Apple should match City of New York"
    );
}

#[test]
fn test_search_boston() {
    let Some(db) = get_db() else {
        eprintln!("Skipping: US shard not found");
        return;
    };

    let query = GeocoderQuery::new("boston");
    let results = db.search(&query).unwrap();

    assert!(!results.is_empty(), "Should return results for 'boston'");

    // Boston, MA should be first due to population
    let first = &results[0];
    assert!(
        first.primary_name.contains("Boston"),
        "First result should contain 'Boston'"
    );
    assert_eq!(
        first.region.as_deref(),
        Some("US-MA"),
        "First result should be Boston, MA"
    );
}

#[test]
fn test_autocomplete() {
    let Some(db) = get_db() else {
        eprintln!("Skipping: US shard not found");
        return;
    };

    let mut query = GeocoderQuery::new("bost");
    query.autocomplete = true;
    let results = db.search(&query).unwrap();

    assert!(!results.is_empty(), "Autocomplete should return results for 'bost'");

    // Should find Boston with prefix match
    let has_boston = results.iter().any(|r| r.primary_name.contains("Boston"));
    assert!(has_boston, "Autocomplete should find Boston for 'bost'");
}
