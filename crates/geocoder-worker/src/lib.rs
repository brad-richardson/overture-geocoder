//! Cloudflare Worker for Overture geocoding.
//!
//! Serves geocoding requests using R2-stored SQLite shards with edge caching.

use worker::*;

mod handlers;
mod stac;

#[event(fetch)]
async fn fetch(req: Request, env: Env, _ctx: Context) -> Result<Response> {
    console_error_panic_hook::set_once();

    // Handle CORS preflight requests
    if req.method() == Method::Options {
        return preflight_response();
    }

    // Detect HEAD requests: convert to GET for routing, strip body later
    let is_head = req.method() == Method::Head;
    let req = if is_head {
        Request::new_with_init(req.url()?.as_str(), RequestInit::new().with_method(Method::Get))?
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
                return Ok(resp);
            }
        }
    }

    let router = Router::new();

    let mut response = router
        .get_async("/search", handlers::handle_search)
        .get_async("/reverse", handlers::handle_reverse)
        .get_async("/id/:gers_id", handlers::handle_id_lookup)
        .get_async("/health", handlers::handle_health)
        .get("/", |_, _| {
            Response::ok(
                r#"{"name":"overture-geocoder","version":"0.3.0","endpoints":["/search","/reverse","/id/:id"]}"#,
            )
        })
        .run(req, env)
        .await?;

    // Add CORS header
    response
        .headers_mut()
        .set("Access-Control-Allow-Origin", "*")?;

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
