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

use geocoder_core::{query::apply_location_bias, Database, GeocoderQuery, LocationBias};
use std::path::Path;

const US_SHARD_PATH: &str = "../../shards/2026-01-02.0/shards/US.db";
const FIXTURE_PATH: &str = "../../tests/fixtures/divisions.db";

fn get_db() -> Option<Database> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join(US_SHARD_PATH);
    if path.exists() {
        Database::open(&path).ok()
    } else {
        None
    }
}

fn get_fixture_db() -> Database {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join(FIXTURE_PATH);
    Database::open(&path)
        .expect("Fixture DB should exist. Run: python scripts/create_test_fixture.py")
}

#[test]
fn test_search_new_york() {
    let Some(db) = get_db() else {
        eprintln!(
            "Skipping: US shard not found. Run: python scripts/build_shards.py --countries US"
        );
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

    assert!(
        !results.is_empty(),
        "Autocomplete should return results for 'bost'"
    );

    // Should find Boston with prefix match
    let has_boston = results.iter().any(|r| r.primary_name.contains("Boston"));
    assert!(has_boston, "Autocomplete should find Boston for 'bost'");
}

#[test]
fn test_location_bias_returns_many_results() {
    let Some(db) = get_db() else {
        eprintln!("Skipping: US shard not found");
        return;
    };

    // Request limit=5, but search should return more for bias to work with
    let query = GeocoderQuery::new("paris").with_limit(5);
    let results = db.search(&query).unwrap();

    // Database::search should return more than 5 results (up to 10x limit)
    // This allows location bias to elevate results that wouldn't make the initial cut
    assert!(
        results.len() > 5,
        "Search should return more than limit ({}) for bias to work with, got {}",
        5,
        results.len()
    );
}

#[test]
fn test_location_bias_us_elevates_us_results() {
    let Some(db) = get_db() else {
        eprintln!("Skipping: US shard not found");
        return;
    };

    // Search for a common name that exists in multiple countries
    let query = GeocoderQuery::new("springfield").with_limit(10);
    let mut results = db.search(&query).unwrap();

    // Apply US country bias
    apply_location_bias(&mut results, &LocationBias::Country("US".to_string()));
    results.truncate(10);

    // After bias, US results should be ranked higher
    let us_count_in_top_5 = results
        .iter()
        .take(5)
        .filter(|r| r.country.as_deref() == Some("US"))
        .count();

    assert!(
        us_count_in_top_5 >= 3,
        "With US bias, at least 3 of top 5 should be US results, got {}",
        us_count_in_top_5
    );
}

// =============================================================================
// Fixture-based tests (always run, use tests/fixtures/divisions.db)
// =============================================================================

#[test]
fn test_fixture_search_newyork_concatenated() {
    let db = get_fixture_db();

    // "newyork" should match NYC via the concatenated alias in search_text
    let query = GeocoderQuery::new("newyork").with_autocomplete(false);
    let results = db.search(&query).unwrap();

    assert!(
        !results.is_empty(),
        "Should return results for 'newyork' (concatenated form)"
    );
    assert!(
        results[0].primary_name.contains("New York"),
        "First result should be New York City, got: {}",
        results[0].primary_name
    );
}

#[test]
fn test_fixture_search_newyork_fallback() {
    let db = get_fixture_db();

    // Even without autocomplete, the single-token fallback should try prefix wildcard
    // "newyork" (7 chars >= 6) should trigger fallback if exact match fails
    let query = GeocoderQuery::new("newyork");
    let results = db.search(&query).unwrap();

    assert!(
        !results.is_empty(),
        "Should return results for 'newyork' via fallback"
    );
    let has_nyc = results.iter().any(|r| r.primary_name.contains("New York"));
    assert!(has_nyc, "Should find New York City for 'newyork'");
}

#[test]
fn test_fixture_search_abbreviation_st_louis() {
    let db = get_fixture_db();

    // "st louis" should match "Saint Louis" because the fixture includes "st" as a variant
    let query = GeocoderQuery::new("st louis");
    let results = db.search(&query).unwrap();

    assert!(!results.is_empty(), "Should return results for 'st louis'");
    let has_stlouis = results
        .iter()
        .any(|r| r.primary_name.contains("Saint Louis"));
    assert!(
        has_stlouis,
        "Should find Saint Louis for 'st louis', got: {:?}",
        results.iter().map(|r| &r.primary_name).collect::<Vec<_>>()
    );
}

#[test]
fn test_fixture_search_abbreviation_ft_worth() {
    let db = get_fixture_db();

    // "ft worth" should match "Fort Worth" because the fixture includes "ft" as a variant
    let query = GeocoderQuery::new("ft worth");
    let results = db.search(&query).unwrap();

    assert!(!results.is_empty(), "Should return results for 'ft worth'");
    let has_ftworth = results
        .iter()
        .any(|r| r.primary_name.contains("Fort Worth"));
    assert!(
        has_ftworth,
        "Should find Fort Worth for 'ft worth', got: {:?}",
        results.iter().map(|r| &r.primary_name).collect::<Vec<_>>()
    );
}

#[test]
fn test_fixture_search_spaced_still_works() {
    let db = get_fixture_db();

    // Normal spaced queries should still work correctly
    let query = GeocoderQuery::new("new york");
    let results = db.search(&query).unwrap();

    assert!(!results.is_empty(), "Should return results for 'new york'");
    assert!(
        results[0].primary_name.contains("New York"),
        "First result should be New York City for 'new york', got: {}",
        results[0].primary_name
    );
}

#[test]
fn test_fixture_search_boston_ranking() {
    let db = get_fixture_db();

    // Boston should rank city above neighborhoods due to population
    let query = GeocoderQuery::new("boston");
    let results = db.search(&query).unwrap();

    assert!(!results.is_empty(), "Should return results for 'boston'");
    assert!(
        results[0].primary_name.contains("Boston, MA"),
        "Boston city should rank first, got: {}",
        results[0].primary_name
    );
}
