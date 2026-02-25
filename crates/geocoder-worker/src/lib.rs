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

    let response = router
        .get_async("/search", handlers::handle_search)
        .get_async("/reverse", handlers::handle_reverse)
        .get_async("/id/:gers_id", handlers::handle_id_lookup)
        .get("/health", |_, _| Response::ok("ok"))
        .get("/", |_, _| {
            Response::ok(
                r#"{"name":"overture-geocoder","version":"0.3.0","endpoints":["/search","/reverse","/id/:id"]}"#,
            )
        })
        .run(req, env)
        .await?;

    // Add CORS header to existing response headers (don't replace)
    let mut response = response;
    response
        .headers_mut()
        .set("Access-Control-Allow-Origin", "*")?;
    Ok(response)
}

/// Response for CORS preflight (OPTIONS) requests.
fn preflight_response() -> Result<Response> {
    let headers = Headers::new();
    headers.set("Access-Control-Allow-Origin", "*").unwrap();
    headers
        .set("Access-Control-Allow-Methods", "GET, OPTIONS")
        .unwrap();
    headers
        .set("Access-Control-Allow-Headers", "Content-Type")
        .unwrap();
    headers.set("Access-Control-Max-Age", "86400").unwrap();
    Ok(Response::empty()?.with_status(204).with_headers(headers))
}
