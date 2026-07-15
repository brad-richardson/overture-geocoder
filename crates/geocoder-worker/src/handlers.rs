//! Request handlers for geocoding endpoints.

use std::collections::HashSet;

use geocoder_core::{GeocoderQuery, GeocoderResult, LocationBias, ReverseResult};
use serde::Serialize;
use worker::*;

use crate::stac::{
    ReverseRoutingDebug, SearchDebugInfo, ShardLoader, UserLocation, NOT_FOUND_SENTINEL,
};

const MAX_QUERY_LENGTH: usize = 200;
const MIN_AUTOCOMPLETE_QUERY_CHARS: usize = 2;
const MAX_TOKEN_COUNT: usize = 10;
const ADDRESS_SPIKE_INDEX_KEY: &str = "smoketest-address/useful_gzip.idx";
const ADDRESS_SPIKE_DATA_KEY: &str = "smoketest-address/useful_gzip.bin";

fn is_id_index_unavailable(err_msg: &str) -> bool {
    err_msg.contains(NOT_FOUND_SENTINEL) && err_msg.contains("id-index")
}

const DEFAULT_DIVISION_TYPES: &[&str] = &[
    "country",
    "region",
    "county",
    "localadmin",
    "locality",
    "neighborhood",
    "macrohood",
];

fn normalize_type_token(s: &str) -> String {
    let lower = s.trim().to_lowercase();
    if lower == "neighbourhood" {
        "neighborhood".to_string()
    } else {
        lower
    }
}

fn parse_types_param(raw: Option<&String>) -> HashSet<String> {
    match raw {
        Some(s) if !s.trim().is_empty() => {
            let parsed: HashSet<String> = s
                .split(',')
                .map(normalize_type_token)
                .filter(|t| !t.is_empty())
                .collect();
            if parsed.is_empty() {
                DEFAULT_DIVISION_TYPES
                    .iter()
                    .map(|t| t.to_string())
                    .collect()
            } else {
                parsed
            }
        }
        _ => DEFAULT_DIVISION_TYPES
            .iter()
            .map(|t| t.to_string())
            .collect(),
    }
}

/// Isolated smoke-only entry point for the experimental address page reader.
/// Production and unrelated preview environments receive a plain 404.
pub async fn handle_address_page_spike(
    _req: Request,
    ctx: RouteContext<std::rc::Rc<Context>>,
) -> Result<Response> {
    if ctx
        .env
        .var("ENVIRONMENT")
        .ok()
        .is_none_or(|value| value.to_string() != "address-smoke")
    {
        return Response::error("Not found", 404);
    }
    let key = [
        "us",
        "ma",
        "stoneham",
        "stoneham",
        "02180",
        "main street",
        "10",
        "",
    ]
    .map(str::to_string);
    let loader = ShardLoader::with_context(&ctx.env, ctx.data.clone())?;
    let records = loader
        .lookup_address_page_spike(ADDRESS_SPIKE_INDEX_KEY, ADDRESS_SPIKE_DATA_KEY, &key)
        .await?;
    let body = serde_json::json!({
        "schema": "overture-address-worker-spike-v1",
        "candidate_count": records.len(),
        "first": records.first(),
        "last_id": records.last().map(|record| record.id.as_str()),
    });
    let mut response = Response::from_json(&body)?;
    response.headers_mut().set("Cache-Control", "no-store")?;
    Ok(response)
}

/// Search request handler.
pub async fn handle_search(
    req: Request,
    ctx: RouteContext<std::rc::Rc<Context>>,
) -> Result<Response> {
    let url = req.url()?;
    let params: std::collections::HashMap<String, String> = url
        .query_pairs()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();

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

    if q.split_whitespace().count() > MAX_TOKEN_COUNT {
        return Response::error(format!("Too many tokens: max {}", MAX_TOKEN_COUNT), 400);
    }

    let limit: usize = params
        .get("limit")
        .and_then(|l| l.parse().ok())
        .unwrap_or(10)
        .min(40);

    let autocomplete = params
        .get("autocomplete")
        .map(|a| a == "1" || a == "true")
        .unwrap_or(true);

    let format = params.get("format").map(|f| f.as_str()).unwrap_or("json");

    if autocomplete && q.trim().chars().count() < MIN_AUTOCOMPLETE_QUERY_CHARS {
        return Response::error(
            "Query too short: minimum 2 characters for autocomplete",
            400,
        );
    }

    let include_debug = params
        .get("debug")
        .map(|d| d == "1" || d == "true")
        .unwrap_or(false);

    let user_location = UserLocation::from_request(&req);

    let user_location = if params.contains_key("lat") && params.contains_key("lon") {
        UserLocation {
            lat: params.get("lat").and_then(|l| l.parse().ok()),
            lon: params.get("lon").and_then(|l| l.parse().ok()),
            ..user_location
        }
    } else {
        user_location
    };

    let bias = match (&user_location.country, user_location.lat, user_location.lon) {
        (Some(country), Some(lat), Some(lon)) => LocationBias::Full {
            country: country.clone(),
            lat,
            lon,
        },
        (Some(country), None, None) => LocationBias::Country(country.clone()),
        (None, Some(lat), Some(lon)) => LocationBias::Coordinates { lat, lon },
        _ => LocationBias::None,
    };

    let allowed_types = parse_types_param(params.get("types"));

    let query = GeocoderQuery::new(&q)
        .with_limit(limit)
        .with_autocomplete(autocomplete)
        .with_bias(bias)
        .with_allowed_types(Some(allowed_types));

    let loader = ShardLoader::with_context(&ctx.env, ctx.data.clone())?;
    let search_result = loader.search(&query, &user_location, include_debug).await?;

    match format {
        "geojson" => to_geojson_response(&search_result.results, &search_result.version),
        _ => to_json_response(
            &search_result.results,
            search_result.debug,
            &search_result.version,
        ),
    }
}

pub async fn handle_reverse(
    req: Request,
    ctx: RouteContext<std::rc::Rc<Context>>,
) -> Result<Response> {
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

    let format = params.get("format").map(|f| f.as_str()).unwrap_or("json");
    let include_debug = params
        .get("debug")
        .map(|d| d == "1" || d == "true")
        .unwrap_or(false);

    let cf_country = req.headers().get("CF-IPCountry").ok().flatten();

    let loader = ShardLoader::with_context(&ctx.env, ctx.data.clone())?;
    let search = loader
        .reverse_geocode(lat, lon, cf_country.as_deref())
        .await?;

    match search.result {
        Some(r) => match format {
            "geojson" => reverse_to_geojson_response(&r, &search.version),
            _ => {
                let body = reverse_json_body(&r, &search.version, &search.routing, include_debug);
                let mut resp = Response::from_json(&body)?;
                resp.headers_mut()
                    .set("Content-Type", "application/json; charset=utf-8")?;
                resp.headers_mut().set("X-Data-Version", &search.version)?;
                Ok(resp)
            }
        },
        None if include_debug => {
            let body = reverse_not_found_json(&search.version, &search.routing);
            let mut resp = Response::from_json(&body)?.with_status(404);
            resp.headers_mut()
                .set("Content-Type", "application/json; charset=utf-8")?;
            resp.headers_mut().set("X-Data-Version", &search.version)?;
            Ok(resp)
        }
        None => Response::error("No results found for coordinates", 404),
    }
}

fn reverse_json_body(
    result: &ReverseResult,
    data_version: &str,
    routing: &ReverseRoutingDebug,
    include_debug: bool,
) -> serde_json::Value {
    let mut body = serde_json::to_value(result).unwrap_or(serde_json::json!({}));
    if let Some(obj) = body.as_object_mut() {
        obj.insert(
            "data_version".to_string(),
            serde_json::Value::String(data_version.to_string()),
        );
        if include_debug {
            obj.insert(
                "debug".to_string(),
                serde_json::json!({ "country_routing": routing }),
            );
        }
    }
    body
}

fn reverse_not_found_json(data_version: &str, routing: &ReverseRoutingDebug) -> serde_json::Value {
    serde_json::json!({
        "error": "No results found for coordinates",
        "data_version": data_version,
        "debug": { "country_routing": routing },
    })
}

pub async fn handle_id_lookup(
    _req: Request,
    ctx: RouteContext<std::rc::Rc<Context>>,
) -> Result<Response> {
    let gers_id = ctx
        .param("gers_id")
        .ok_or_else(|| Error::RustError("Missing gers_id parameter".into()))?
        .to_string();

    if gers_id.len() < 2 || gers_id.len() > 64 {
        return Response::error("Invalid GERS ID: must be 2-64 characters", 400);
    }

    let loader = ShardLoader::with_context(&ctx.env, ctx.data.clone())?;
    let result = loader.lookup_id(&gers_id).await;

    match result {
        Ok(search) => match search.result {
            Some(r) => {
                let mut body = serde_json::to_value(&r).unwrap_or(serde_json::json!({}));
                if let Some(obj) = body.as_object_mut() {
                    obj.insert(
                        "data_version".to_string(),
                        serde_json::Value::String(search.version.clone()),
                    );
                }
                let mut resp = Response::from_json(&body)?;
                resp.headers_mut()
                    .set("Content-Type", "application/json; charset=utf-8")?;
                resp.headers_mut()
                    .set("Cache-Control", "public, max-age=86400")?;
                resp.headers_mut().set("X-Data-Version", &search.version)?;
                Ok(resp)
            }
            None => Response::error("GERS ID not found", 404),
        },
        Err(e) => {
            let err_msg = format!("{:?}", e);
            if is_id_index_unavailable(&err_msg) {
                return Response::error("ID index not available", 503);
            }
            Err(e)
        }
    }
}

pub async fn handle_health(
    _req: Request,
    ctx: RouteContext<std::rc::Rc<Context>>,
) -> Result<Response> {
    let loader = ShardLoader::with_context(&ctx.env, ctx.data.clone())?;
    match loader.check_health().await {
        Ok(version) => {
            let json = serde_json::json!({
                "status": "ok",
                "version": version,
            });
            let mut resp = Response::from_json(&json)?;
            resp.headers_mut()
                .set("Content-Type", "application/json; charset=utf-8")?;
            Ok(resp)
        }
        Err(e) => {
            let json = serde_json::json!({
                "status": "error",
                "error": e.to_string(),
            });
            let mut resp = Response::from_json(&json)?;
            resp = resp.with_status(503);
            resp.headers_mut()
                .set("Content-Type", "application/json; charset=utf-8")?;
            Ok(resp)
        }
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

fn to_json_response(
    results: &[GeocoderResult],
    debug: Option<SearchDebugInfo>,
    data_version: &str,
) -> Result<Response> {
    let items: Vec<ResultItem> = results
        .iter()
        .map(|r| ResultItem {
            gers_id: r.gers_id.clone(),
            name: r.primary_name.clone(),
            division_type: r.division_type.clone(),
            lat: r.lat,
            lon: r.lon,
            bbox: r.bbox,
            importance: (r.importance / 2.0).clamp(0.0, 1.0),
            country: r.country.clone(),
            region: r.region.clone(),
        })
        .collect();

    let response = match debug {
        Some(debug_info) => serde_json::json!({
            "results": items,
            "debug": debug_info,
            "data_version": data_version,
        }),
        None => serde_json::json!({
            "results": items,
            "data_version": data_version,
        }),
    };

    let mut resp = Response::from_json(&response)?;
    resp.headers_mut()
        .set("Content-Type", "application/json; charset=utf-8")?;
    resp.headers_mut().set("X-Data-Version", data_version)?;
    Ok(resp)
}

fn to_geojson_response(results: &[GeocoderResult], data_version: &str) -> Result<Response> {
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
                    "importance": (r.importance / 2.0).clamp(0.0, 1.0),
                    "country": r.country,
                    "region": r.region,
                },
                "bbox": r.bbox
            })
        })
        .collect();

    let geojson = serde_json::json!({
        "type": "FeatureCollection",
        "features": features,
        "data_version": data_version,
    });

    let mut resp = Response::from_json(&geojson)?;
    resp.headers_mut()
        .set("Content-Type", "application/geo+json; charset=utf-8")?;
    resp.headers_mut().set("X-Data-Version", data_version)?;
    Ok(resp)
}

fn reverse_to_geojson_response(result: &ReverseResult, data_version: &str) -> Result<Response> {
    let geojson = serde_json::json!({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [result.lon, result.lat]
            },
            "properties": {
                "gers_id": result.gers_id,
                "name": result.primary_name,
                "subtype": result.subtype,
                "distance_km": result.distance_km,
                "confidence": result.confidence,
                "hierarchy": result.hierarchy,
            },
            "bbox": result.bbox
        }],
        "data_version": data_version,
    });

    let mut resp = Response::from_json(&geojson)?;
    resp.headers_mut()
        .set("Content-Type", "application/geo+json; charset=utf-8")?;
    resp.headers_mut().set("X-Data-Version", data_version)?;
    Ok(resp)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stac::{ReverseCountryDecision, ReverseRoutingOutcome};
    use geocoder_core::types::HierarchyEntry;

    fn reverse_routing_fixture() -> ReverseRoutingDebug {
        ReverseRoutingDebug {
            country_decision: ReverseCountryDecision::AmbiguousBbox,
            outcome: ReverseRoutingOutcome::GlobalFallback,
            bbox_candidate_count: 2,
            bbox_candidates: vec!["CA".to_string(), "US".to_string()],
            selected_country: None,
        }
    }

    #[test]
    fn test_parse_types_default_when_missing() {
        let parsed = parse_types_param(None);
        assert!(!parsed.contains("place"));
        assert!(parsed.contains("locality"));
        assert!(parsed.contains("country"));
        assert!(parsed.contains("neighborhood"));
    }

    #[test]
    fn test_parse_types_empty_string_defaults() {
        let s = "".to_string();
        let parsed = parse_types_param(Some(&s));
        assert!(!parsed.contains("place"));
        assert!(parsed.contains("locality"));
    }

    #[test]
    fn test_parse_types_place_only() {
        let s = "place".to_string();
        let parsed = parse_types_param(Some(&s));
        assert_eq!(parsed.len(), 1);
        assert!(parsed.contains("place"));
    }

    #[test]
    fn test_parse_types_mixed_case_and_spaces() {
        let s = " Locality , Country , place ".to_string();
        let parsed = parse_types_param(Some(&s));
        assert!(parsed.contains("locality"));
        assert!(parsed.contains("country"));
        assert!(parsed.contains("place"));
        assert_eq!(parsed.len(), 3);
    }

    #[test]
    fn test_parse_types_neighbourhood_synonym() {
        let s = "neighbourhood,locality".to_string();
        let parsed = parse_types_param(Some(&s));
        assert!(parsed.contains("neighborhood"));
        assert!(!parsed.contains("neighbourhood"));
        assert!(parsed.contains("locality"));
    }

    #[test]
    fn test_parse_types_all_division_types_plus_place() {
        let s =
            "country,region,county,localadmin,locality,neighborhood,macrohood,place".to_string();
        let parsed = parse_types_param(Some(&s));
        assert!(parsed.contains("place"));
        assert!(parsed.contains("country"));
        assert_eq!(parsed.len(), 8);
    }

    #[test]
    fn test_invalid_id_index_metadata_maps_to_unavailable() {
        let message = format!(
            "RustError(\"{} invalid id-index metadata for latest version\")",
            NOT_FOUND_SENTINEL
        );
        assert!(is_id_index_unavailable(&message));
        assert!(!is_id_index_unavailable("invalid id-index metadata"));
    }

    #[test]
    fn test_reverse_not_found_json_preserves_routing_contract() {
        let body = reverse_not_found_json("2026-06-17.0", &reverse_routing_fixture());

        assert_eq!(body["data_version"], "2026-06-17.0");
        assert_eq!(body["debug"]["country_routing"]["bbox_candidate_count"], 2);
        assert_eq!(
            body["debug"]["country_routing"]["outcome"],
            "global_fallback"
        );
    }

    #[test]
    fn test_reverse_json_debug_is_opt_in() {
        let result = ReverseResult {
            gers_id: "place-1".to_string(),
            primary_name: "Example".to_string(),
            subtype: "locality".to_string(),
            lat: 42.0,
            lon: -71.0,
            bbox: [-71.1, 41.9, -70.9, 42.1],
            distance_km: 0.1,
            confidence: "high".to_string(),
            hierarchy: vec![HierarchyEntry {
                gers_id: "country-1".to_string(),
                subtype: "country".to_string(),
                name: "Exampleland".to_string(),
            }],
        };

        let plain = reverse_json_body(&result, "2026-06-17.0", &reverse_routing_fixture(), false);
        let debug = reverse_json_body(&result, "2026-06-17.0", &reverse_routing_fixture(), true);

        assert_eq!(plain["gers_id"], "place-1");
        assert_eq!(plain["data_version"], "2026-06-17.0");
        assert!(plain.get("debug").is_none());
        assert_eq!(
            debug["debug"]["country_routing"]["country_decision"],
            "ambiguous_bbox"
        );
    }
}
