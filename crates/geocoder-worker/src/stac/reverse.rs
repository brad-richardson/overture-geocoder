//! Reverse geocoding: adapts STAC collection metadata into geocoder-core's
//! coordinate -> country routing, then queries the selected shards.
//!
//! The pure routing/geometry logic lives in [`geocoder_core::routing`] so the
//! CLI and offline evaluation exercise the exact production routing. This
//! module only maps the Worker-specific `StacCollection` into the primitive
//! `(shard_id, bbox)` pairs that core consumes.

use geocoder_core::routing::{ReverseRouteSelection, ReverseRoutingDebug, ReverseRoutingOutcome};
use geocoder_core::ReverseResult;
use worker::*;

use super::catalog::{with_version_fallback, StacCollection};
use super::{not_found, ShardLoader};

pub struct ReverseSearchResult {
    pub result: Option<ReverseResult>,
    pub version: String,
    pub routing: ReverseRoutingDebug,
}

impl ShardLoader {
    /// Adapt a reverse `StacCollection` into core's routing seam: iterate the
    /// embedded items as `(shard_id, bbox)` pairs and let core decide.
    fn select_reverse_route(
        collection: &StacCollection,
        lat: f64,
        lon: f64,
    ) -> ReverseRouteSelection {
        geocoder_core::routing::select_reverse_route(
            collection
                .items
                .iter()
                .map(|(shard_id, item)| (shard_id.as_str(), item.bbox.as_ref())),
            lat,
            lon,
        )
    }

    /// Reverse geocode a lat/lon coordinate.
    /// Falls back to older versions if the latest version's shards are unavailable.
    pub async fn reverse_geocode(&self, lat: f64, lon: f64) -> Result<ReverseSearchResult> {
        with_version_fallback!(self, "reverse", version, {
            self.try_reverse_geocode(version, lat, lon).await
        })
    }

    /// Reverse geocode one core release selected by the atomic v2 manifest.
    pub(crate) async fn reverse_geocode_version(
        &self,
        version: &str,
        lat: f64,
        lon: f64,
    ) -> Result<ReverseSearchResult> {
        self.try_reverse_geocode(version, lat, lon).await
    }

    /// Attempt reverse geocode against a specific version.
    async fn try_reverse_geocode(
        &self,
        version: &str,
        lat: f64,
        lon: f64,
    ) -> Result<ReverseSearchResult> {
        let reverse_collection = self.load_reverse_collection(version).await?;

        // Ambiguous or unresolved country bboxes fall through to HEAD. This avoids choosing
        // by bbox area or caller IP when disconnected territories inflate a
        // country's aggregate bbox. Exact country polygons/H3 remain future work.
        let mut routing = Self::select_reverse_route(&reverse_collection, lat, lon);
        for country in &routing.shards {
            match self
                .query_reverse_shard(version, country, &reverse_collection, lat, lon)
                .await
            {
                Ok(Some(result)) => {
                    return Ok(ReverseSearchResult {
                        result: Some(result),
                        version: version.to_string(),
                        routing: routing.debug,
                    })
                }
                Ok(None) => {
                    console_log!("No result in country {} reverse shard", country);
                }
                Err(e) => {
                    console_log!(
                        "Warning: country reverse shard {} unavailable: {:?}",
                        country,
                        e
                    );
                }
            }
        }

        let res = self
            .query_reverse_shard(version, "HEAD", &reverse_collection, lat, lon)
            .await?;
        routing.debug.outcome = if res.is_some() {
            ReverseRoutingOutcome::GlobalFallback
        } else {
            ReverseRoutingOutcome::Unresolved
        };
        Ok(ReverseSearchResult {
            result: res,
            version: version.to_string(),
            routing: routing.debug,
        })
    }

    async fn query_reverse_shard(
        &self,
        version: &str,
        shard_id: &str,
        collection: &StacCollection,
        lat: f64,
        lon: f64,
    ) -> Result<Option<ReverseResult>> {
        // Get item metadata from embedded items in reverse-collection.json
        let shard_href = self
            .get_embedded_item(collection, shard_id)
            .map(|item| item.href.clone())
            .ok_or_else(|| not_found(format!("reverse shard {} in collection", shard_id)))?;

        let shard_key = format!("{}/{}", version, shard_href.trim_start_matches("./"));
        let (db, _) = self.load_shard_db(&shard_key).await?;

        let result = db
            .reverse_geocode(lat, lon)
            .map_err(|e| Error::RustError(format!("Reverse geocode failed: {}", e)))?;

        Ok(result)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stac::catalog::collection_with_bboxes;
    use geocoder_core::routing::ReverseCountryDecision;

    // The exhaustive routing/geometry cases live in `geocoder_core::routing`.
    // These tests only cover the Worker adapter: that it reads STAC bboxes and
    // honors the HEAD skip while wiring the collection into core.

    #[test]
    fn adapter_routes_stac_collection_and_skips_head() {
        let collection = collection_with_bboxes(&[
            // A world-spanning HEAD must not route or add ambiguity.
            ("HEAD", Some([-180.0, -90.0, 180.0, 90.0])),
            ("JP", Some([122.0, 20.0, 154.0, 46.0])),
            ("US", Some([-125.0, 24.0, -66.0, 50.0])),
        ]);

        let route = ShardLoader::select_reverse_route(&collection, 35.68, 139.65);
        assert_eq!(route.shards, vec!["JP"]);
        assert_eq!(
            route.debug.country_decision,
            ReverseCountryDecision::UniqueBbox
        );
        assert_eq!(route.debug.selected_country, Some("JP".to_string()));
    }

    #[test]
    fn adapter_falls_through_on_ambiguous_overlap() {
        let collection = collection_with_bboxes(&[
            ("CA", Some([-141.0, 41.0, -52.0, 83.0])),
            ("US", Some([-125.0, 24.0, -66.0, 50.0])),
        ]);

        let route = ShardLoader::select_reverse_route(&collection, 45.0, -100.0);
        assert!(route.shards.is_empty());
        assert_eq!(
            route.debug.country_decision,
            ReverseCountryDecision::AmbiguousBbox
        );
        assert_eq!(route.debug.bbox_candidates, vec!["CA", "US"]);
    }
}
