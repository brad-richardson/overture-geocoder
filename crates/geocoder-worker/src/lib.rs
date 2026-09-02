//! Cloudflare Worker for Overture geocoding.
//!
//! Serves geocoding requests using R2-stored SQLite shards with edge caching.

use worker::*;

mod address;
mod address_construction_v1;
mod address_pages;
mod handlers;
mod places_construction_v1;
mod places_pages;
mod range_reader;
mod reverse_construction_v1;
mod stac;
mod v2;

#[event(fetch)]
async fn fetch(req: Request, env: Env, ctx: Context) -> Result<Response> {
    let started_at = Date::now().as_millis();
    console_error_panic_hook::set_once();

    // The explicit log message contains only a fixed endpoint class. Production
    // Workers Logs persistence is disabled because Cloudflare can enrich even
    // a fixed custom message with raw request URL metadata.
    let endpoint = request_endpoint(req.url()?.path());
    let preview_isolated = is_preview_environment(&env);
    let serve_v2 = v2_serving_enabled(
        preview_isolated,
        env.var("ENABLE_V2_SERVING")
            .ok()
            .as_ref()
            .map(|value| value.to_string())
            .as_deref(),
    );
    let preview_catalog_override = env
        .var("CATALOG_KEY_OVERRIDE")
        .ok()
        .map(|value| value.to_string());

    let is_head = req.method() == Method::Head;

    if !serve_v2 && is_v2_path(req.url()?.path()) {
        // Production v2 is deliberately paused. Fail before preflight, rate
        // limiting, routing, catalog resolution, or any family read from R2.
        // The implementation stays routable in isolated preview/smoke workers.
        return finalize_response(
            Response::error("Not found", 404)?,
            endpoint,
            started_at,
            is_head,
        );
    }

    // Handle CORS preflight requests
    if req.method() == Method::Options {
        return finalize_response(preflight_response()?, endpoint, started_at, false);
    }

    // Detect HEAD requests: convert to GET for routing, strip body later
    let req = if is_head {
        Request::new_with_init(
            req.url()?.as_str(),
            RequestInit::new().with_method(Method::Get),
        )?
    } else {
        req
    };

    // Rate limiting: 60 requests per minute per IP
    let ip = req
        .headers()
        .get("CF-Connecting-IP")
        .ok()
        .flatten()
        .unwrap_or_else(|| "unknown".to_string());

    if let Ok(rate_limiter) = env.rate_limiter("RATE_LIMITER") {
        if let Ok(outcome) = rate_limiter.limit(ip).await {
            if !outcome.success {
                let mut resp = Response::error("Rate limit exceeded", 429)?;
                resp.headers_mut().set("Retry-After", "60")?;
                return finalize_response(resp, endpoint, started_at, is_head);
            }
        }
    }

    if preview_isolated
        && !preview_path_allowed(req.url()?.path(), preview_catalog_override.as_deref())
    {
        return finalize_response(
            Response::error("Not found", 404)?,
            endpoint,
            started_at,
            is_head,
        );
    }

    // Context rides along as router data so handlers can schedule
    // background cache writes via waitUntil.
    let router = Router::with_data(std::rc::Rc::new(ctx))
        .get_async("/search", handlers::handle_search)
        .get_async("/reverse", handlers::handle_reverse)
        .get_async("/id/:gers_id", handlers::handle_id_lookup)
        .get_async("/v2/forward", v2::handle_forward)
        .get_async("/v2/reverse", v2::handle_reverse)
        .get_async("/v2/ids/:id", v2::handle_id);

    let result = if preview_isolated {
        router
            .get_async("/health", v2::handle_preview_health)
            .run(req, env)
            .await
    } else {
        router
            .get_async("/health", handlers::handle_health)
            .get(
                "/",
                if serve_v2 {
                    handle_root_with_v2
                } else {
                    handle_root
                },
            )
            .run(req, env)
            .await
    };

    // Handler errors become 500s here (rather than via `?`) so browser
    // clients still get the CORS header instead of an opaque CORS failure.
    let response = match result {
        Ok(response) => response,
        Err(e) => {
            console_error!("Unhandled error: {:?}", e);
            Response::error("Internal error", 500)?
        }
    };

    finalize_response(response, endpoint, started_at, is_head)
}

/// Apply headers and timing consistently to normal, rejected, and HEAD
/// responses. This keeps the early production-v2 gate behavior identical to a
/// routed response without allowing the request to reach a v2 handler.
fn finalize_response(
    mut response: Response,
    endpoint: &str,
    started_at_ms: u64,
    is_head: bool,
) -> Result<Response> {
    response
        .headers_mut()
        .set("Access-Control-Allow-Origin", "*")?;
    response
        .headers_mut()
        .set("Access-Control-Expose-Headers", "X-Data-Version")?;

    add_timing(&mut response, endpoint, started_at_ms)?;

    // For HEAD requests, return empty body with same status and headers
    if is_head {
        let status = response.status_code();
        let headers = response.headers().clone();
        let mut head_resp = Response::empty()?.with_status(status);
        for (key, value) in headers.entries() {
            head_resp.headers_mut().set(&key, &value)?;
        }
        return Ok(head_resp);
    }

    Ok(response)
}

fn is_preview_environment(env: &Env) -> bool {
    env.var("ENVIRONMENT")
        .ok()
        .is_some_and(|value| matches!(value.to_string().as_str(), "preview" | "smoke"))
}

/// V2 serving is fail-closed in production and always available to an
/// isolated preview/smoke Worker. Only the exact string `true` opts a
/// non-preview deployment back in.
fn v2_serving_enabled(preview_isolated: bool, configured: Option<&str>) -> bool {
    preview_isolated || configured == Some("true")
}

fn is_v2_path(path: &str) -> bool {
    path == "/v2" || path.starts_with("/v2/")
}

fn root_document(v2_serving_enabled: bool) -> &'static str {
    if v2_serving_enabled {
        concat!(
            r#"{"name":"overture-geocoder","version":""#,
            env!("CARGO_PKG_VERSION"),
            r#"","endpoints":["/search","/reverse","/id/:id","/v2/forward","/v2/reverse","/v2/ids/:id"]}"#,
        )
    } else {
        concat!(
            r#"{"name":"overture-geocoder","version":""#,
            env!("CARGO_PKG_VERSION"),
            r#"","endpoints":["/search","/reverse","/id/:id"]}"#,
        )
    }
}

fn handle_root(_: Request, _: RouteContext<std::rc::Rc<Context>>) -> Result<Response> {
    Response::ok(root_document(false))
}

fn handle_root_with_v2(_: Request, _: RouteContext<std::rc::Rc<Context>>) -> Result<Response> {
    Response::ok(root_document(true))
}

fn preview_path_allowed(path: &str, catalog_override: Option<&str>) -> bool {
    if catalog_override == Some("smoketest-id/catalog.json") {
        return path.strip_prefix("/id/").is_some_and(uuid_path_segment);
    }
    matches!(path, "/health" | "/v2/forward" | "/v2/reverse") || path.starts_with("/v2/ids/")
}

fn uuid_path_segment(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(index, byte)| {
            if matches!(index, 8 | 13 | 18 | 23) {
                byte == b'-'
            } else {
                byte.is_ascii_hexdigit()
            }
        })
}

/// Return a fixed, privacy-safe endpoint label for request timing logs.
fn request_endpoint(path: &str) -> &'static str {
    match path {
        "/search" => "search",
        "/reverse" => "reverse",
        "/v2/forward" => "v2_forward",
        "/v2/reverse" => "v2_reverse",
        "/health" => "health",
        "/" => "root",
        path if path.starts_with("/id/") => "id",
        path if path.starts_with("/v2/ids/") => "v2_id",
        _ => "other",
    }
}

/// Add a standard request-duration metric without modifying response bodies.
///
/// `Server-Timing` lets clients distinguish application time from transport
/// time. The custom message intentionally contains no request data. Production
/// log persistence is disabled as well because platform-added metadata can
/// contain request URLs even when the custom message does not.
fn add_timing(response: &mut Response, endpoint: &str, started_at_ms: u64) -> Result<()> {
    let total_ms = Date::now().as_millis().saturating_sub(started_at_ms) as f64;
    response
        .headers_mut()
        .set("Server-Timing", &format_server_timing(total_ms))?;
    console_log!(
        "request endpoint={} status={} total_ms={:.1}",
        endpoint,
        response.status_code(),
        total_ms
    );
    Ok(())
}

fn format_server_timing(total_ms: f64) -> String {
    // A single decimal is adequate for edge-request monitoring and prevents
    // needlessly variable header values from fragmenting downstream caches.
    format!("total;dur={total_ms:.1}")
}

/// Response for CORS preflight (OPTIONS) requests.
fn preflight_response() -> Result<Response> {
    let headers = Headers::new();
    headers.set("Access-Control-Allow-Origin", "*").unwrap();
    headers
        .set("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        .unwrap();
    headers
        .set("Access-Control-Allow-Headers", "Content-Type")
        .unwrap();
    headers.set("Access-Control-Max-Age", "86400").unwrap();
    Ok(Response::empty()?.with_status(204).with_headers(headers))
}

#[cfg(test)]
mod tests {
    use super::{
        format_server_timing, is_v2_path, preview_path_allowed, request_endpoint, root_document,
        v2_serving_enabled,
    };

    #[test]
    fn classifies_known_endpoints_without_retaining_path_parameters() {
        assert_eq!(request_endpoint("/search"), "search");
        assert_eq!(request_endpoint("/reverse"), "reverse");
        assert_eq!(request_endpoint("/id/abc-123"), "id");
        assert_eq!(request_endpoint("/v2/forward"), "v2_forward");
        assert_eq!(request_endpoint("/v2/reverse"), "v2_reverse");
        assert_eq!(request_endpoint("/v2/ids/abc-123"), "v2_id");
        assert_eq!(request_endpoint("/v2/features/abc-123"), "other");
        assert_eq!(request_endpoint("/unexpected"), "other");
    }

    #[test]
    fn formats_standard_total_duration_metric() {
        assert_eq!(format_server_timing(12.34), "total;dur=12.3");
    }

    #[test]
    fn production_v2_serving_is_explicit_and_fail_closed() {
        assert!(!v2_serving_enabled(false, None));
        assert!(!v2_serving_enabled(false, Some("false")));
        assert!(!v2_serving_enabled(false, Some("TRUE")));
        assert!(v2_serving_enabled(false, Some("true")));
        assert!(v2_serving_enabled(true, None));
        assert!(v2_serving_enabled(true, Some("false")));
    }

    #[test]
    fn v2_pause_covers_the_namespace_without_matching_similar_paths() {
        for path in [
            "/v2",
            "/v2/",
            "/v2/forward",
            "/v2/reverse",
            "/v2/ids/id",
            "/v2/anything/else",
        ] {
            assert!(is_v2_path(path), "expected v2 path: {path}");
        }
        for path in ["/", "/v2ish", "/V2/forward", "/search"] {
            assert!(!is_v2_path(path), "unexpected v2 path: {path}");
        }
    }

    #[test]
    fn production_discovery_omits_paused_v2_routes() {
        let production = root_document(false);
        assert!(production.contains(r#""endpoints":["/search","/reverse","/id/:id"]"#));
        assert!(!production.contains("/v2/"));

        let enabled = root_document(true);
        assert!(enabled.contains("/v2/forward"));
        assert!(enabled.contains("/v2/reverse"));
        assert!(enabled.contains("/v2/ids/:id"));
    }

    #[test]
    fn v2_preview_exposes_only_candidate_health_and_required_v2_routes() {
        for path in ["/health", "/v2/forward", "/v2/reverse", "/v2/ids/id"] {
            assert!(preview_path_allowed(path, None));
        }
        for path in [
            "/",
            "/search",
            "/reverse",
            "/id/id",
            "/id",
            "/v2/features/id",
            "/unexpected",
        ] {
            assert!(!preview_path_allowed(path, None));
        }
    }

    #[test]
    fn id_smoke_preview_exposes_only_nonempty_legacy_id_route() {
        for path in [
            "/id/00000000-0000-4000-8000-000000000000",
            "/id/ABCDEF00-0000-4000-8000-000000000000",
        ] {
            assert!(preview_path_allowed(
                path,
                Some("smoketest-id/catalog.json")
            ));
        }
        for path in [
            "/",
            "/health",
            "/search",
            "/reverse",
            "/id",
            "/id/",
            "/id/id",
            "/id/foo/bar",
            "/id//",
            "/id/00000000-0000-4000-8000-000000000000/extra",
            "/id/00000000-0000-4000-8000-00000000000%2F",
            "/v2/forward",
            "/v2/reverse",
            "/v2/ids/id",
            "/v2/features/id",
            "/unexpected",
        ] {
            assert!(!preview_path_allowed(
                path,
                Some("smoketest-id/catalog.json")
            ));
        }
        for catalog_override in [
            "smoketest-shards/catalog.json",
            "smoketest-id/catalog.json/extra",
            "prefix/smoketest-id/catalog.json",
        ] {
            assert!(!preview_path_allowed(
                "/id/00000000-0000-4000-8000-000000000000",
                Some(catalog_override)
            ));
        }
    }
}
