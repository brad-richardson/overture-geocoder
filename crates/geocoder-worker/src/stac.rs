//! STAC catalog loading and shard management with edge caching.
//!
//! This module is split into focused submodules:
//! - [`cache`]: R2 / edge (Cache API) / isolate caching primitives.
//! - [`catalog`]: STAC catalog + collection loading, version fallback, shared types.
//! - [`forward`]: forward shard selection (suffix / router / proximity / places).
//! - [`reverse`]: reverse country routing + bbox/geometry helpers.
//! - [`id_index`]: parquet ID-index reader + locator dictionary validation.
//! - [`router_db`]: the forward text router.

use std::rc::Rc;

use worker::*;

pub(crate) mod cache;
pub(crate) mod catalog;
pub(crate) mod forward;
pub(crate) mod id_index;
pub(crate) mod reverse;
pub(crate) mod router_db;

pub use forward::{SearchDebugInfo, UserLocation};
pub use geocoder_core::routing::ReverseRoutingDebug;

/// Sentinel marking missing-resource errors. Version fallback and the
/// handlers' 503 mapping key off this exact marker rather than matching
/// arbitrary error prose (which risked false positives/negatives).
pub const NOT_FOUND_SENTINEL: &str = "[not-found]";

/// Build a missing-resource error that triggers version fallback.
pub(crate) fn not_found(what: impl std::fmt::Display) -> Error {
    Error::RustError(format!("{} {}", NOT_FOUND_SENTINEL, what))
}

/// Loads and caches shards from R2 with edge caching via Cache API.
pub struct ShardLoader {
    bucket: Bucket,
    cache: Cache,
    /// R2 catalog object. Production always uses the root catalog; an
    /// explicitly smoke-scoped override lets preview Workers exercise an
    /// isolated fixed-prefix catalog without making it discoverable live.
    catalog_key: String,
    /// Unified v2 catalog object. Production is fixed at `v2/catalog.json`;
    /// preview Workers may use one guarded, run-scoped smoke catalog.
    v2_catalog_key: String,
    /// Execution context for background cache writes via waitUntil.
    /// When absent, cache writes happen inline (slower, but correct).
    ctx: Option<Rc<Context>>,
}

impl ShardLoader {
    pub fn new(env: &Env) -> Result<Self> {
        let bucket = env.bucket("SHARDS_BUCKET")?;
        let cache = Cache::default();
        let environment = env.var("ENVIRONMENT").ok().map(|value| value.to_string());
        let override_key = env
            .var("CATALOG_KEY_OVERRIDE")
            .ok()
            .map(|value| value.to_string());
        let catalog_key =
            catalog::resolve_catalog_key(environment.as_deref(), override_key.as_deref())
                .map_err(Error::RustError)?;
        let v2_override_key = env
            .var("V2_CATALOG_KEY_OVERRIDE")
            .ok()
            .map(|value| value.to_string());
        let v2_catalog_key =
            catalog::resolve_v2_catalog_key(environment.as_deref(), v2_override_key.as_deref())
                .map_err(Error::RustError)?;
        Ok(Self {
            bucket,
            cache,
            catalog_key,
            v2_catalog_key,
            ctx: None,
        })
    }

    /// Create a loader that performs cache writes in the background via
    /// `waitUntil`, keeping multi-MB cache.put calls off the critical path.
    pub fn with_context(env: &Env, ctx: Rc<Context>) -> Result<Self> {
        let mut loader = Self::new(env)?;
        loader.ctx = Some(ctx);
        Ok(loader)
    }

    pub(crate) fn v2_catalog_key(&self) -> &str {
        &self.v2_catalog_key
    }
}
