use geocoder_core::{GeocoderQuery, GeocoderResult, LocationBias, ReverseResult};
use serde::Serialize;
use worker::*;

use crate::stac::{SearchDebugInfo, ShardLoader, UserLocation, NOT_FOUND_SENTINEL};

const MAX_QUERY_LENGTH: usize = 200;
const MIN_AUTOCOMPLETE_QUERY_CHARS: usize = 2;
const MAX_TOKEN_COUNT: usize = 10;

fn empty_json_response() -> Result<Response> {
    let body = serde_json::json!({ "results": [] });
    let mut resp = Response::from_json(&body)?;
    resp.headers_mut()
        .set("Content-Type", "application/json; charset=utf-8")?;
    Ok(resp)
}

fn empty_geojson_response() -> Result<Response> {
    let body = serde_json::json!({
        "type": "FeatureCollection",
        "features": []
    });
    let mut resp = Response::from_json(&body)?;
    resp.headers_mut()
        .set("Content-Type", "application/geo+json; charset=utf-8")?;
    Ok(resp)
}

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
        return Response::error(
            format!("Too many tokens: max {}", MAX_TOKEN_COUNT),
            400,
        );
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
        return match format {
            "geojson" => empty_geojson_response(),
            _ => empty_json_response(),
        };
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

    let query = GeocoderQuery::new(&q)
        .with_limit(limit)
        .with_autocomplete(autocomplete)
        .with_bias(bias);

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

    let cf_country = req.headers().get("CF-IPCountry").ok().flatten();

    let loader = ShardLoader::with_context(&ctx.env, ctx.data.clone())?;
    let search = loader
        .reverse_geocode(lat, lon, cf_country.as_deref())
        .await?;

    match search.result {
        Some(r) => match format {
            "geojson" => reverse_to_geojson_response(&r, &search.version),
            _ => {
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
                    .set("X-Data-Version", &search.version)?;
                Ok(resp)
            }
        },
        None => Response::error("No results found for coordinates", 404),
    }
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
                resp.headers_mut()
                    .set("X-Data-Version", &search.version)?;
                Ok(resp)
            }
            None => Response::error("GERS ID not found", 404),
        },
        Err(e) => {
            let err_msg = format!("{:?}", e);
            if err_msg.contains(NOT_FOUND_SENTINEL) && err_msg.contains("id-index") {
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
