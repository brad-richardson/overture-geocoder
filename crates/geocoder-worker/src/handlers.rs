//! Request handlers for geocoding endpoints.

use std::collections::HashSet;

use geocoder_core::{GeocoderQuery, GeocoderResult, LocationBias, ReverseResult};
use serde::Serialize;
use worker::*;

#[cfg(feature = "places-spike")]
use crate::places_pages::PlacesClause;
use crate::stac::{
    ReverseRoutingDebug, SearchDebugInfo, ShardLoader, UserLocation, NOT_FOUND_SENTINEL,
};

const MAX_QUERY_LENGTH: usize = 200;
const MIN_AUTOCOMPLETE_QUERY_CHARS: usize = 2;
const MAX_TOKEN_COUNT: usize = 10;

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
#[cfg(feature = "address-spike")]
pub async fn handle_address_page_spike(
    req: Request,
    ctx: RouteContext<std::rc::Rc<Context>>,
) -> Result<Response> {
    let environment_ok = ctx
        .env
        .var("ENVIRONMENT")
        .ok()
        .is_some_and(|value| value.to_string() == "address-smoke");
    if !environment_ok {
        return Response::error("Not found", 404);
    }
    let prefix = ctx
        .env
        .var("ADDRESS_SPIKE_PREFIX")
        .ok()
        .map(|value| value.to_string())
        .filter(|value| valid_address_smoke_prefix(value))
        .ok_or_else(|| Error::RustError("Invalid address smoke prefix".into()))?;
    let index_key = format!("{prefix}/useful_gzip.idx");
    let data_key = format!("{prefix}/useful_gzip.bin");
    let values: Vec<String> = req
        .url()?
        .query_pairs()
        .filter(|(name, _)| name == "k")
        .map(|(_, value)| value.into_owned())
        .collect();
    if values.len() != 8 || values.iter().any(|value| value.len() > 512) {
        return Response::error("Expected exactly eight bounded k parameters", 400);
    }
    let key: [String; 8] = values
        .try_into()
        .map_err(|_| Error::RustError("Address smoke key field count differs".into()))?;
    let loader = ShardLoader::with_context(&ctx.env, ctx.data.clone())?;
    let lookup = loader
        .lookup_address_page_spike(&index_key, &data_key, &key)
        .await?;
    let body = serde_json::json!({
        "schema": "overture-address-worker-spike-v2",
        "candidate_count": lookup.records.len(),
        "first": lookup.records.first(),
        "last_id": lookup.records.last().map(|record| record.id.as_str()),
        "ids": lookup.records.iter().map(|record| record.id.as_str()).collect::<Vec<_>>(),
        "read_metrics": lookup.read_metrics,
        "index_bytes": lookup.index_bytes,
        "stored_page_bytes": lookup.stored_page_bytes,
        "decoded_page_bytes": lookup.decoded_page_bytes,
        "materialized_page_bytes": lookup.materialized_page_bytes,
    });
    let mut response = Response::from_json(&body)?;
    response.headers_mut().set("Cache-Control", "no-store")?;
    Ok(response)
}

#[cfg(feature = "address-spike")]
fn valid_address_smoke_prefix(value: &str) -> bool {
    value
        .strip_prefix("smoketest-address-")
        .is_some_and(|suffix| {
            !suffix.is_empty()
                && suffix.len() <= 64
                && suffix
                    .chars()
                    .all(|character| character.is_ascii_digit() || character == '-')
        })
}

#[cfg(feature = "places-spike")]
pub async fn handle_places_page_spike(
    req: Request,
    ctx: RouteContext<std::rc::Rc<Context>>,
) -> Result<Response> {
    let environment_ok = ctx
        .env
        .var("ENVIRONMENT")
        .ok()
        .is_some_and(|value| value.to_string() == "places-smoke");
    if !environment_ok {
        return Response::error("Not found", 404);
    }
    let prefix = ctx
        .env
        .var("PLACES_SPIKE_PREFIX")
        .ok()
        .map(|value| value.to_string())
        .filter(|value| valid_places_smoke_prefix(value))
        .ok_or_else(|| Error::RustError("Invalid Places smoke prefix".into()))?;
    let url = req.url()?;
    let tokens: Vec<String> = url
        .query_pairs()
        .filter(|(name, _)| name == "token")
        .map(|(_, value)| value.into_owned())
        .collect();
    if tokens.is_empty() || tokens.len() > 4 {
        return Response::error("Expected one to four bounded normalized tokens", 400);
    }
    let prefix_values: Vec<String> = url
        .query_pairs()
        .filter(|(name, _)| name == "prefix")
        .map(|(_, value)| value.into_owned())
        .collect();
    let field_values: Vec<String> = url
        .query_pairs()
        .filter(|(name, _)| name == "field")
        .map(|(_, value)| value.into_owned())
        .collect();
    if (!prefix_values.is_empty()
        && prefix_values.len() != 1
        && prefix_values.len() != tokens.len())
        || (!field_values.is_empty()
            && field_values.len() != 1
            && field_values.len() != tokens.len())
    {
        return Response::error("Places clause parameter counts differ", 400);
    }
    if prefix_values
        .iter()
        .any(|value| value != "0" && value != "1")
        || field_values.iter().any(|value| {
            !matches!(
                value.as_str(),
                "" | "any" | "name" | "brand" | "category" | "context"
            )
        })
    {
        return Response::error("Places clause parameter is unsupported", 400);
    }
    let clauses = tokens
        .into_iter()
        .enumerate()
        .map(|(index, token)| {
            let prefix_query = prefix_values
                .get(if prefix_values.len() == 1 { 0 } else { index })
                .is_some_and(|value| value == "1");
            let field = field_values
                .get(if field_values.len() == 1 { 0 } else { index })
                .filter(|value| !value.is_empty() && value.as_str() != "any")
                .cloned();
            PlacesClause::new(token, prefix_query, field)
        })
        .collect::<Result<Vec<_>>>();
    let Ok(clauses) = clauses else {
        return Response::error("Places clause is outside hard bounds", 400);
    };
    let scope = url
        .query_pairs()
        .find(|(name, _)| name == "scope")
        .map(|(_, value)| value.into_owned());
    if scope
        .as_deref()
        .is_some_and(|value| !valid_places_smoke_scope(value))
    {
        return Response::error("Invalid Places smoke scope", 400);
    }
    let object_root = scope
        .as_ref()
        .map_or_else(|| prefix.clone(), |scope| format!("{prefix}/case-{scope}"));
    let context = url
        .query_pairs()
        .find(|(name, _)| name == "context")
        .map(|(_, value)| value.into_owned())
        .filter(|value| !value.is_empty());
    let Ok(longitude) = places_query_coordinate(&url, "lon", -180.0, 180.0) else {
        return Response::error("Invalid Places longitude", 400);
    };
    let Ok(latitude) = places_query_coordinate(&url, "lat", -90.0, 90.0) else {
        return Response::error("Invalid Places latitude", 400);
    };
    if longitude.is_some() != latitude.is_some() {
        return Response::error("Places point routing requires both lat and lon", 400);
    }
    if context.is_some() && longitude.is_some() {
        return Response::error("Choose Places context or point routing, not both", 400);
    }

    let loader = ShardLoader::with_context(&ctx.env, ctx.data.clone())?;
    let head_eligible = context.is_none()
        && longitude.is_none()
        && clauses.len() <= 2
        && clauses.iter().all(PlacesClause::head_eligible);
    if head_eligible {
        let head = loader
            .lookup_places_head_spike(&format!("{object_root}/head.phrp"), &clauses)
            .await?;
        let body = serde_json::json!({
            "schema": "overture-places-worker-spike-v2",
            "candidate_count": head.results.len(),
            "results": head.results,
            "clauses": clauses,
            "read_metrics": head.read_metrics,
            "route": if head.hit { "packed_head" } else { "head_only_miss" },
            "shards": [],
            "head_consulted": true,
            "head_hit": head.hit,
            "head_stages": {
                "directory": head.directory_metrics,
                "index": head.index_metrics,
                "entry": head.entry_metrics,
            },
            "fame_evidence": {
                "contract": "context-free, one-or-two exact unfielded tokens; two-token queries probe the famous e2: pair entry first, then fall back to the per-token top-10 ID intersection; a miss on any per-token entry is a head miss",
                "eligible": true,
                "intersection_hit": head.hit,
            },
        });
        let mut response = Response::from_json(&body)?;
        response.headers_mut().set("Cache-Control", "no-store")?;
        return Ok(response);
    }
    if context.is_none() && longitude.is_none() {
        return Response::error(
            "Fielded, prefix, and longer Places queries require explicit context or coordinates",
            400,
        );
    }

    let catalog_lookup = loader
        .lookup_places_catalog_spike(&format!("{object_root}/catalog.pcat"))
        .await?;
    let routed = if let Some(context) = context.as_deref() {
        catalog_lookup.catalog.route_context(context)
    } else {
        catalog_lookup.catalog.route_point(
            longitude.expect("paired coordinate"),
            latitude.expect("paired coordinate"),
        )
    };
    let Some(routed) = routed.cloned() else {
        let body = serde_json::json!({
            "schema": "overture-places-worker-spike-v2",
            "error": "No compact Places shard covers the explicit route",
            "route": "catalog_miss",
            "read_metrics": catalog_lookup.read_metrics,
        });
        let mut response = Response::from_json(&body)?.with_status(400);
        response.headers_mut().set("Cache-Control", "no-store")?;
        return Ok(response);
    };
    let key = format!("{object_root}/{}", routed.object);
    let lookup = loader.lookup_places_shard_spike(&key, &clauses).await?;
    let bias = longitude
        .zip(latitude)
        .unwrap_or((routed.center[0], routed.center[1]));
    let mut results = lookup.results.clone();
    for place in &mut results {
        place.distance_km = Some(haversine_km(
            bias.1,
            bias.0,
            f64::from(place.latitude),
            f64::from(place.longitude),
        ));
    }
    // Distance is diagnostic only in this slice. The shard reader has already
    // selected a bounded confidence/doc-ID top window; reordering that window
    // by distance would falsely imply globally complete nearest ranking.
    results.truncate(10);
    let metrics = catalog_lookup.read_metrics.add(lookup.read_metrics);
    let body = serde_json::json!({
        "schema": "overture-places-worker-spike-v2",
        "candidate_count": lookup.candidate_count,
        "clause_candidate_counts": lookup.clause_candidate_counts,
        "results": results,
        "clauses": clauses,
        "read_metrics": metrics,
        "route": if context.is_some() { "catalog_context" } else { "catalog_point" },
        "route_context": context,
        "route_shard": routed,
        "route_bias": {"longitude": bias.0, "latitude": bias.1},
        "catalog_read_metrics": catalog_lookup.read_metrics,
        "shards": [{
            "shard": routed.id,
            "candidate_count": lookup.candidate_count,
            "read_metrics": lookup.read_metrics,
            "stages": lookup.stages,
            "tokenizer_version": lookup.tokenizer_version,
        }],
        "head_consulted": false,
        "head_hit": false,
        "head_stages": null,
    });
    let mut response = Response::from_json(&body)?;
    response.headers_mut().set("Cache-Control", "no-store")?;
    Ok(response)
}

#[cfg(feature = "places-spike")]
fn places_query_coordinate(
    url: &Url,
    name: &str,
    minimum: f64,
    maximum: f64,
) -> Result<Option<f64>> {
    let Some(raw) = url
        .query_pairs()
        .find(|(candidate, _)| candidate == name)
        .map(|(_, value)| value.into_owned())
    else {
        return Ok(None);
    };
    let value = raw
        .parse::<f64>()
        .map_err(|_| Error::RustError(format!("Invalid Places {name} coordinate")))?;
    if !value.is_finite() || !(minimum..=maximum).contains(&value) {
        return Err(Error::RustError(format!(
            "Places {name} coordinate is outside bounds"
        )));
    }
    Ok(Some(value))
}

#[cfg(feature = "places-spike")]
fn haversine_km(latitude_a: f64, longitude_a: f64, latitude_b: f64, longitude_b: f64) -> f64 {
    let radius_km = 6371.0088_f64;
    let delta_latitude = (latitude_b - latitude_a).to_radians();
    let delta_longitude = (longitude_b - longitude_a).to_radians();
    let latitude_a = latitude_a.to_radians();
    let latitude_b = latitude_b.to_radians();
    let haversine = (delta_latitude / 2.0).sin().powi(2)
        + latitude_a.cos() * latitude_b.cos() * (delta_longitude / 2.0).sin().powi(2);
    2.0 * radius_km * haversine.sqrt().asin()
}

#[cfg(feature = "places-spike")]
fn valid_places_smoke_scope(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || character == '-')
}

#[cfg(feature = "places-spike")]
fn valid_places_smoke_prefix(value: &str) -> bool {
    value
        .strip_prefix("smoketest-places-")
        .is_some_and(|suffix| {
            !suffix.is_empty()
                && suffix.len() <= 64
                && suffix
                    .chars()
                    .all(|character| character.is_ascii_digit() || character == '-')
        })
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

    let loader = ShardLoader::with_context(&ctx.env, ctx.data.clone())?;
    let search = loader.reverse_geocode(lat, lon).await?;

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
    use geocoder_core::routing::{ReverseCountryDecision, ReverseRoutingOutcome};
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

    #[cfg(feature = "address-spike")]
    #[test]
    fn address_smoke_prefix_is_unique_and_strictly_bounded() {
        assert!(valid_address_smoke_prefix("smoketest-address-12345-2"));
        assert!(!valid_address_smoke_prefix("smoketest-address"));
        assert!(!valid_address_smoke_prefix("smoketest-address-production"));
        assert!(!valid_address_smoke_prefix(
            "smoketest-address-12/../catalog"
        ));
    }

    #[cfg(feature = "places-spike")]
    #[test]
    fn places_smoke_scope_is_unique_and_strictly_bounded() {
        assert!(valid_places_smoke_scope("relevance-famous-unique"));
        assert!(!valid_places_smoke_scope(""));
        assert!(!valid_places_smoke_scope("../shared"));
        assert!(!valid_places_smoke_scope("case_with_underscore"));
    }

    #[cfg(feature = "places-spike")]
    #[test]
    fn places_distance_is_zero_for_the_same_point() {
        assert_eq!(haversine_km(42.0, -71.0, 42.0, -71.0), 0.0);
        assert!((haversine_km(42.0, -71.0, 43.0, -71.0) - 111.2).abs() < 0.1);
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
