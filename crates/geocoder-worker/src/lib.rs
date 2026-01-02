//! Cloudflare Worker for Overture geocoding.
//!
//! Serves geocoding requests using R2-stored SQLite shards with edge caching.

use worker::*;

mod handlers;
mod stac;

#[event(fetch)]
async fn fetch(req: Request, env: Env, _ctx: Context) -> Result<Response> {
    console_error_panic_hook::set_once();

    let router = Router::new();

    router
        .get_async("/search", handlers::handle_search)
        .get_async("/reverse", handlers::handle_reverse)
        .get("/health", |_, _| Response::ok("ok"))
        .get("/", |_, _| {
            Response::ok(
                r#"{"name":"overture-geocoder","version":"0.2.0","endpoints":["/search","/reverse"]}"#,
            )
            .map(|r| r.with_headers(json_headers()))
        })
        .run(req, env)
        .await
}

fn json_headers() -> Headers {
    let mut headers = Headers::new();
    headers.set("Content-Type", "application/json").unwrap();
    headers.set("Access-Control-Allow-Origin", "*").unwrap();
    headers
}
