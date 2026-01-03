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

    let router = Router::new();

    let response = router
        .get_async("/search", handlers::handle_search)
        .get_async("/reverse", handlers::handle_reverse)
        .get("/health", |_, _| Response::ok("ok"))
        .get("/", |_, _| {
            Response::ok(
                r#"{"name":"overture-geocoder","version":"0.2.0","endpoints":["/search","/reverse"]}"#,
            )
        })
        .run(req, env)
        .await?;

    // Apply CORS headers to all responses
    Ok(response.with_headers(cors_headers()))
}

/// CORS headers applied to all responses.
fn cors_headers() -> Headers {
    let mut headers = Headers::new();
    headers.set("Access-Control-Allow-Origin", "*").unwrap();
    headers
}

/// Response for CORS preflight (OPTIONS) requests.
fn preflight_response() -> Result<Response> {
    let mut headers = Headers::new();
    headers.set("Access-Control-Allow-Origin", "*").unwrap();
    headers.set("Access-Control-Allow-Methods", "GET, OPTIONS").unwrap();
    headers.set("Access-Control-Allow-Headers", "Content-Type").unwrap();
    headers.set("Access-Control-Max-Age", "86400").unwrap();
    Ok(Response::empty()?.with_status(204).with_headers(headers))
}
