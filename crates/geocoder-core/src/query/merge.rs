//! Multi-shard result merging.
//!
//! Combines results from HEAD and country shards, handling deduplication.

use std::collections::HashSet;

use crate::types::GeocoderResult;

/// Merge results from multiple shards, deduplicating by gers_id.
///
/// HEAD shard results are processed first for global disambiguation.
/// Country shard results fill in with local specificity.
/// Results are sorted by importance and truncated to the limit.
///
/// # Arguments
///
/// * `head_results` - Results from the HEAD shard (global/important places)
/// * `country_results` - Results from country-specific shards
/// * `limit` - Maximum number of results to return
pub fn merge_results(
    head_results: Vec<GeocoderResult>,
    country_results: Vec<Vec<GeocoderResult>>,
    limit: usize,
) -> Vec<GeocoderResult> {
    let mut seen_ids = HashSet::with_capacity(limit * 2);
    let mut merged = Vec::with_capacity(limit * 2);

    // First pass: include all HEAD results (global/important places)
    for result in head_results {
        if seen_ids.insert(result.gers_id.clone()) {
            merged.push(result);
        }
    }

    // Second pass: add country results not already seen
    for results in country_results {
        for result in results {
            if seen_ids.insert(result.gers_id.clone()) {
                merged.push(result);
            }
        }
    }

    // Sort by importance (descending)
    merged.sort_by(|a, b| {
        b.importance
            .partial_cmp(&a.importance)
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    // Truncate to limit
    merged.truncate(limit);
    merged
}

/// Merge results from a single shard query (no deduplication needed).
///
/// Just sorts by importance and truncates.
#[allow(dead_code)]
pub fn merge_single(results: Vec<GeocoderResult>, limit: usize) -> Vec<GeocoderResult> {
    let mut results = results;
    results.sort_by(|a, b| {
        b.importance
            .partial_cmp(&a.importance)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    results.truncate(limit);
    results
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_result(id: &str, name: &str, importance: f64) -> GeocoderResult {
        GeocoderResult {
            gers_id: id.to_string(),
            primary_name: name.to_string(),
            lat: 0.0,
            lon: 0.0,
            bbox: [0.0, 0.0, 0.0, 0.0],
            importance,
            division_type: "locality".to_string(),
            country: None,
            region: None,
            population: None,
        }
    }

    #[test]
    fn test_merge_deduplication() {
        let head = vec![
            make_result("id-1", "Paris, FR", 0.9),
            make_result("id-2", "London, GB", 0.85),
        ];

        let country = vec![
            vec![
                make_result("id-1", "Paris, FR (dup)", 0.7), // Duplicate, should be ignored
                make_result("id-3", "Lyon, FR", 0.6),
            ],
        ];

        let merged = merge_results(head, country, 10);

        assert_eq!(merged.len(), 3);
        // id-1 should only appear once (from HEAD)
        assert_eq!(merged[0].gers_id, "id-1");
        assert_eq!(merged[0].primary_name, "Paris, FR"); // Original name, not "dup"
    }

    #[test]
    fn test_merge_sorting() {
        let head = vec![
            make_result("id-1", "A", 0.5),
        ];

        let country = vec![
            vec![
                make_result("id-2", "B", 0.9),
                make_result("id-3", "C", 0.3),
            ],
        ];

        let merged = merge_results(head, country, 10);

        // Should be sorted by importance: B (0.9), A (0.5), C (0.3)
        assert_eq!(merged[0].primary_name, "B");
        assert_eq!(merged[1].primary_name, "A");
        assert_eq!(merged[2].primary_name, "C");
    }

    #[test]
    fn test_merge_limit() {
        let head = vec![
            make_result("id-1", "A", 0.9),
            make_result("id-2", "B", 0.8),
            make_result("id-3", "C", 0.7),
        ];

        let country = vec![
            vec![
                make_result("id-4", "D", 0.6),
                make_result("id-5", "E", 0.5),
            ],
        ];

        let merged = merge_results(head, country, 3);

        assert_eq!(merged.len(), 3);
        assert_eq!(merged[0].primary_name, "A");
        assert_eq!(merged[1].primary_name, "B");
        assert_eq!(merged[2].primary_name, "C");
    }

    #[test]
    fn test_merge_empty() {
        let merged = merge_results(vec![], vec![], 10);
        assert!(merged.is_empty());
    }
}
