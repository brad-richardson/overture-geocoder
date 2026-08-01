//! The one total order for division results.
//!
//! Three sites sorted division results independently, all three spelled
//! `b.importance.partial_cmp(&a.importance).unwrap_or(Ordering::Equal)`:
//! `merge::merge_results`, `bias::apply_location_bias`, and the live
//! `stac::forward` search. Two defects, both of them shared:
//!
//! 1. **The comparator is not a total order.** `partial_cmp` returns `None`
//!    for NaN, and `unwrap_or(Equal)` then claims a NaN is *equal to every
//!    other score*. That breaks the transitivity `sort_by` requires, so the
//!    resulting order is unspecified — and one NaN importance corrupts the
//!    ranking of everything around it rather than merely misplacing itself.
//! 2. **Equal scores had no tie-break at all.** `sort_by` is stable, so ties
//!    fell back to arrival order — which is HEAD shard first, then whichever
//!    country shards happened to load, in whatever order their futures
//!    resolved. Identical queries could rank identically-scored places
//!    differently between requests.
//!
//! This is P6(a) from `docs/ranking-research.md:93-96` plus the determinism
//! that has to come with it. P6(b), the relative cutoff, is deliberately NOT
//! implemented here — see `docs/plans/2026-07-31-search-quality-and-street-layer.md`.

use std::cmp::Ordering;

use crate::types::GeocoderResult;

/// Map a score onto the finite line so ordering is total.
///
/// A non-finite importance is treated as the WORST possible score rather than
/// propagated. `f64::total_cmp` alone would rank a positive NaN above every
/// real score and put corrupt data at rank 1.
fn score_key(value: f64) -> f64 {
    if value.is_finite() {
        value
    } else {
        f64::NEG_INFINITY
    }
}

/// Longitude/latitude extent of a result's bbox, or 0.0 when it is unusable.
///
/// Deliberately the raw degree product and not a projected area: this is only
/// ever a tie-break between two results that already scored identically, and a
/// cos(latitude) correction cannot change which of them is larger except for
/// pairs that straddle very different latitudes at near-identical extents.
fn bbox_extent(result: &GeocoderResult) -> f64 {
    let [min_lon, min_lat, max_lon, max_lat] = result.bbox;
    let width = max_lon - min_lon;
    let height = max_lat - min_lat;
    if width.is_finite() && height.is_finite() && width > 0.0 && height > 0.0 {
        width * height
    } else {
        0.0
    }
}

/// Total ordering for division results, best first.
///
/// `importance` DESC, then Nominatim's bbox-area tie-break (larger wins), then
/// population DESC, then `gers_id` ASC. The final key is unique, so the order
/// is total and identical inputs always produce identical output regardless of
/// which shard returned what first.
pub fn compare_results(left: &GeocoderResult, right: &GeocoderResult) -> Ordering {
    score_key(right.importance)
        .total_cmp(&score_key(left.importance))
        .then_with(|| bbox_extent(right).total_cmp(&bbox_extent(left)))
        .then_with(|| {
            right
                .population
                .unwrap_or(0)
                .cmp(&left.population.unwrap_or(0))
        })
        .then_with(|| left.gers_id.cmp(&right.gers_id))
}

/// Sort division results into `compare_results` order, best first.
pub fn sort_results(results: &mut [GeocoderResult]) {
    results.sort_by(compare_results);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn result(
        id: &str,
        importance: f64,
        bbox: [f64; 4],
        population: Option<i64>,
    ) -> GeocoderResult {
        GeocoderResult {
            gers_id: id.to_string(),
            primary_name: id.to_string(),
            lat: 0.0,
            lon: 0.0,
            bbox,
            importance,
            division_type: "locality".to_string(),
            country: None,
            region: None,
            population,
        }
    }

    const SMALL: [f64; 4] = [0.0, 0.0, 0.1, 0.1];
    const LARGE: [f64; 4] = [0.0, 0.0, 2.0, 2.0];

    #[test]
    fn importance_still_dominates_every_tie_break() {
        let mut results = vec![
            result("small-but-better", 0.9, SMALL, None),
            result("large-but-worse", 0.5, LARGE, Some(9_000_000)),
        ];
        sort_results(&mut results);
        assert_eq!(results[0].gers_id, "small-but-better");
    }

    #[test]
    fn equal_scores_prefer_the_larger_bbox() {
        let mut results = vec![
            result("hamlet", 0.7, SMALL, None),
            result("metropolis", 0.7, LARGE, None),
        ];
        sort_results(&mut results);
        assert_eq!(results[0].gers_id, "metropolis");
    }

    #[test]
    fn population_breaks_a_bbox_tie() {
        let mut results = vec![
            result("empty", 0.7, SMALL, Some(10)),
            result("populous", 0.7, SMALL, Some(5_000_000)),
        ];
        sort_results(&mut results);
        assert_eq!(results[0].gers_id, "populous");
    }

    /// The defect this file exists for: identical scores used to fall back to
    /// arrival order, so the answer depended on which shard resolved first.
    #[test]
    fn identical_results_rank_identically_regardless_of_input_order() {
        let a = result("aaa", 0.7, SMALL, Some(100));
        let b = result("bbb", 0.7, SMALL, Some(100));
        let c = result("ccc", 0.7, SMALL, Some(100));

        let mut forward = vec![a.clone(), b.clone(), c.clone()];
        let mut reverse = vec![c, b, a];
        sort_results(&mut forward);
        sort_results(&mut reverse);

        let ids = |v: &[GeocoderResult]| v.iter().map(|r| r.gers_id.clone()).collect::<Vec<_>>();
        assert_eq!(ids(&forward), ids(&reverse));
        assert_eq!(ids(&forward), vec!["aaa", "bbb", "ccc"]);
    }

    /// A NaN score must sink, and must not disturb the results around it. Under
    /// the old comparator NaN claimed equality with everything, so the order of
    /// the *other* results became unspecified too.
    #[test]
    fn a_non_finite_score_sinks_and_corrupts_nothing() {
        let mut results = vec![
            result("nan", f64::NAN, LARGE, Some(9_000_000)),
            result("good", 0.9, SMALL, None),
            result("infinite", f64::INFINITY, LARGE, None),
            result("mid", 0.5, SMALL, None),
        ];
        sort_results(&mut results);
        assert_eq!(results[0].gers_id, "good");
        assert_eq!(results[1].gers_id, "mid");
        // Both non-finite scores sink to the same key, and the ordinary
        // tie-breaks then order them: equal bbox, so population decides and
        // "nan" (9,000,000) precedes "infinite" (none). Sunk does not mean
        // unordered -- the order is still total.
        assert_eq!(results[2].gers_id, "nan");
        assert_eq!(results[3].gers_id, "infinite");
    }

    /// `sort_by` demands a total order. Verify the comparator is antisymmetric
    /// and transitive over a set that includes the pathological values.
    #[test]
    fn the_comparator_is_a_total_order() {
        let items = vec![
            result("a", 0.9, SMALL, Some(5)),
            result("b", 0.9, LARGE, Some(5)),
            result("c", f64::NAN, SMALL, None),
            result("d", 0.5, SMALL, None),
            result("e", 0.9, SMALL, Some(50)),
        ];
        for left in &items {
            for right in &items {
                assert_eq!(
                    compare_results(left, right),
                    compare_results(right, left).reverse(),
                    "antisymmetry: {} vs {}",
                    left.gers_id,
                    right.gers_id
                );
            }
        }
        for x in &items {
            for y in &items {
                for z in &items {
                    if compare_results(x, y) == Ordering::Less
                        && compare_results(y, z) == Ordering::Less
                    {
                        assert_eq!(
                            compare_results(x, z),
                            Ordering::Less,
                            "transitivity: {} < {} < {}",
                            x.gers_id,
                            y.gers_id,
                            z.gers_id
                        );
                    }
                }
            }
        }
    }
}
