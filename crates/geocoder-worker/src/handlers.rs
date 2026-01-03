//! Request handlers for geocoding endpoints.

use geocoder_core::{GeocoderQuery, GeocoderResult, LocationBias};
use serde::Serialize;
use worker::*;

use crate::stac::ShardLoader;

/// Search request handler.
pub async fn handle_search(req: Request, ctx: RouteContext<()>) -> Result<Response> {
    let url = req.url()?;
    let params: std::collections::HashMap<String, String> = url
        .query_pairs()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();

    // Parse query parameters
    const MAX_QUERY_LENGTH: usize = 200;
    let q = match params.get("q") {
        Some(q) if !q.is_empty() && q.len() <= MAX_QUERY_LENGTH => q.clone(),
        Some(q) if q.len() > MAX_QUERY_LENGTH => {
            return Response::error(
                format!("Query too long: max {} characters", MAX_QUERY_LENGTH),
                400,
            )
        }
        _ => return Response::error("Missing required parameter: q", 400),
    };

    let limit: usize = params
        .get("limit")
        .and_then(|l| l.parse().ok())
        .unwrap_or(10)
        .min(40);

    let autocomplete = params
        .get("autocomplete")
        .map(|a| a == "1" || a == "true")
        .unwrap_or(true);

    let format = params
        .get("format")
        .map(|f| f.as_str())
        .unwrap_or("json");

    // Get country bias from Cloudflare headers
    let cf_country = req
        .headers()
        .get("CF-IPCountry")
        .ok()
        .flatten();

    // Build query with location bias
    let bias = match &cf_country {
        Some(country) => LocationBias::Country(country.clone()),
        None => LocationBias::None,
    };

    let query = GeocoderQuery::new(&q)
        .with_limit(limit)
        .with_autocomplete(autocomplete)
        .with_bias(bias);

    // Load shards and search
    let loader = ShardLoader::new(&ctx.env)?;
    let results = loader.search(&query, cf_country.as_deref()).await?;

    // Format response
    match format {
        "geojson" => to_geojson_response(&results),
        _ => to_json_response(&results),
    }
}

/// Reverse geocoding handler.
pub async fn handle_reverse(req: Request, ctx: RouteContext<()>) -> Result<Response> {
    let url = req.url()?;
    let params: std::collections::HashMap<String, String> = url
        .query_pairs()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();

    let lat: f64 = match params.get("lat").and_then(|l| l.parse().ok()) {
        Some(l) if (-90.0..=90.0).contains(&l) => l,
        Some(_) => return Response::error("lat must be between -90 and 90", 400),
        None => return Response::error("Missing or invalid parameter: lat", 400),
    };

    let lon: f64 = match params.get("lon").and_then(|l| l.parse().ok()) {
        Some(l) if (-180.0..=180.0).contains(&l) => l,
        Some(_) => return Response::error("lon must be between -180 and 180", 400),
        None => return Response::error("Missing or invalid parameter: lon", 400),
    };

    // Get country from Cloudflare headers
    let cf_country = req
        .headers()
        .get("CF-IPCountry")
        .ok()
        .flatten();

    // Load shards and reverse geocode
    let loader = ShardLoader::new(&ctx.env)?;
    let result = loader.reverse_geocode(lat, lon, cf_country.as_deref()).await?;

    match result {
        Some(r) => Response::from_json(&r),
        None => Response::error("No results found for coordinates", 404),
    }
}

#[derive(Serialize)]
struct ResultItem {
    gers_id: String,
    name: String,
    #[serde(rename = "type")]
    division_type: String,
    lat: f64,
    lon: f64,
    bbox: [f64; 4],
    importance: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    country: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    region: Option<String>,
}

fn to_json_response(results: &[GeocoderResult]) -> Result<Response> {
    let items: Vec<ResultItem> = results
        .iter()
        .map(|r| ResultItem {
            gers_id: r.gers_id.clone(),
            name: r.primary_name.clone(),
            division_type: r.division_type.clone(),
            lat: r.lat,
            lon: r.lon,
            bbox: r.bbox,
            importance: r.importance,
            country: r.country.clone(),
            region: r.region.clone(),
        })
        .collect();

    let response = serde_json::json!({
        "results": items,
    });

    Response::from_json(&response)
}

fn to_geojson_response(results: &[GeocoderResult]) -> Result<Response> {
    let features: Vec<serde_json::Value> = results
        .iter()
        .map(|r| {
            serde_json::json!({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [r.lon, r.lat]
                },
                "properties": {
                    "gers_id": r.gers_id,
                    "name": r.primary_name,
                    "type": r.division_type,
                    "importance": r.importance,
                    "country": r.country,
                    "region": r.region,
                },
                "bbox": r.bbox
            })
        })
        .collect();

    let geojson = serde_json::json!({
        "type": "FeatureCollection",
        "features": features
    });

    Response::from_json(&geojson)
}
