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

    // Log only a fixed endpoint class. Avoiding the raw path keeps query
    // strings, GERS IDs, IP-derived location, and explicit coordinates out of
    // request logs.
    let endpoint = request_endpoint(req.url()?.path());
    let preview_isolated = is_preview_environment(&env);
    let preview_catalog_override = env
        .var("CATALOG_KEY_OVERRIDE")
        .ok()
        .map(|value| value.to_string());

    // Handle CORS preflight requests
    if req.method() == Method::Options {
        let mut response = preflight_response()?;
        add_timing(&mut response, endpoint, started_at)?;
        return Ok(response);
    }

    // Detect HEAD requests: convert to GET for routing, strip body later
    let is_head = req.method() == Method::Head;
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
                resp.headers_mut().set("Access-Control-Allow-Origin", "*")?;
                resp.headers_mut().set("Retry-After", "60")?;
                add_timing(&mut resp, endpoint, started_at)?;
                return Ok(resp);
            }
        }
    }

    if preview_isolated
        && !preview_path_allowed(req.url()?.path(), preview_catalog_override.as_deref())
    {
        let mut response = Response::error("Not found", 404)?;
        response
            .headers_mut()
            .set("Access-Control-Allow-Origin", "*")?;
        add_timing(&mut response, endpoint, started_at)?;
        return Ok(response);
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
            .get("/", |_, _| {
                Response::ok(concat!(
                    r#"{"name":"overture-geocoder","version":""#,
                    env!("CARGO_PKG_VERSION"),
                    r#"","endpoints":["/search","/reverse","/id/:id","/v2/forward","/v2/reverse","/v2/ids/:id"]}"#,
                ))
            })
            .run(req, env)
            .await
    };

    // Handler errors become 500s here (rather than via `?`) so browser
    // clients still get the CORS header instead of an opaque CORS failure.
    let mut response = match result {
        Ok(response) => response,
        Err(e) => {
            console_error!("Unhandled error: {:?}", e);
            Response::error("Internal error", 500)?
        }
    };

    // Add CORS header
    response
        .headers_mut()
        .set("Access-Control-Allow-Origin", "*")?;
    response
        .headers_mut()
        .set("Access-Control-Expose-Headers", "X-Data-Version")?;

    add_timing(&mut response, endpoint, started_at)?;

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
/// time. The accompanying worker log intentionally contains no request data:
/// it is useful for endpoint-level latency monitoring without recording IDs,
/// query strings, client IPs, or coordinates.
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
    use super::{format_server_timing, preview_path_allowed, request_endpoint};

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
