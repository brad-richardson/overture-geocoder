//! STAC catalog and collection loading, version fallback, and shared STAC types.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use serde::Deserialize;
use worker::*;

use super::cache::{CATALOG_CACHE_TTL, ID_INDEX_CACHE_TTL, IMMUTABLE_CACHE_TTL, TEXT_MEMO_TTL_MS};
use super::{not_found, ShardLoader, NOT_FOUND_SENTINEL};

const MAX_VERSION_ATTEMPTS: usize = 4; // Max versions to try (latest + fallbacks); 4 keeps
                                       // the newest complete id-index reachable while fresher versions still
                                       // build. Retention (rebuild-r2-shards.yml KEEP_VERSIONS and the
                                       // r2-cleanup floor) keeps the newest 4 versions to match this
                                       // fallback window, so every probe can have a live candidate even
                                       // when a stale cached catalog still offers a just-pruned version.

thread_local! {
    /// Parsed collections keyed by versioned R2 key (immutable content within
    /// a version). Avoids re-parsing multi-KB collection JSON on every
    /// forward/reverse request; the memo TTL matches the text memo so version
    /// pointers cannot go stale within an isolate any longer than before.
    static COLLECTION_MEMO: RefCell<HashMap<String, (Rc<StacCollection>, u64)>> =
        RefCell::new(HashMap::new());
}

#[derive(Debug, Deserialize)]
pub(crate) struct StacCatalog {
    links: Vec<StacLink>,
}

#[derive(Debug, Deserialize)]
struct StacLink {
    rel: String,
    href: String,
    #[serde(default)]
    latest: bool,
}

/// Embedded item metadata in collection.json
#[derive(Debug, Deserialize)]
pub(crate) struct EmbeddedItem {
    pub(crate) record_count: u64,
    #[allow(dead_code)]
    pub(crate) size_bytes: u64,
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) sha256: Option<String>,
    pub(crate) href: String,
    /// Bounding box [min_lon, min_lat, max_lon, max_lat] for proximity queries
    #[serde(default)]
    pub(crate) bbox: Option<[f64; 4]>,
    /// Parent country code for region shards (e.g., "CN" for "CN-GD")
    #[serde(default)]
    #[allow(dead_code)]
    pub(crate) parent_country: Option<String>,
}

#[derive(Debug, Deserialize)]
pub(crate) struct StacCollection {
    #[allow(dead_code)]
    pub(crate) id: String,
    /// Embedded items (new format) - keyed by shard ID (e.g., "US", "HEAD")
    #[serde(default)]
    pub(crate) items: std::collections::HashMap<String, EmbeddedItem>,
    /// Legacy links to individual item files
    links: Vec<StacLink>,
    /// Countries that have been split into region shards
    /// e.g., {"CN": ["CN-GD", "CN-BJ", ...], "IN": [...]}
    #[serde(default)]
    pub(crate) region_sharded: std::collections::HashMap<String, Vec<String>>,
}

/// Legacy STAC item format (for backward compatibility with old catalogs)
#[derive(Debug, Deserialize)]
pub(crate) struct StacItem {
    #[allow(dead_code)]
    pub(crate) id: String,
    pub(crate) properties: StacItemProperties,
    pub(crate) assets: StacAssets,
}

#[derive(Debug, Deserialize)]
pub(crate) struct StacItemProperties {
    pub(crate) record_count: u64,
    #[allow(dead_code)]
    pub(crate) size_bytes: u64,
    #[allow(dead_code)]
    #[serde(default)]
    pub(crate) sha256: Option<String>,
}

#[derive(Debug, Deserialize)]
pub(crate) struct StacAssets {
    pub(crate) data: StacAsset,
}

#[derive(Debug, Deserialize)]
pub(crate) struct StacAsset {
    pub(crate) href: String,
}

/// Check if an error indicates a missing resource that should trigger version fallback.
///
/// Only missing-resource errors (built via [`not_found`]) are retriable — these
/// indicate a version whose data hasn't been fully deployed yet. Operational
/// errors (database corruption, query failures, parse errors) are surfaced
/// immediately to avoid silently serving stale data.
pub(crate) fn is_retriable_error(e: &Error) -> bool {
    format!("{:?}", e).contains(NOT_FOUND_SENTINEL)
}

/// Run an async operation with version fallback.
///
/// Tries each version in order. Errors matching `is_retriable_error` (missing resources)
/// trigger fallback to the next version. Non-retriable errors (corruption, query failures)
/// are returned immediately.
macro_rules! with_version_fallback {
    ($self:expr, $endpoint:expr, $version:ident, $body:expr) => {{
        let catalog = $self.load_catalog().await?;
        let versions = $crate::stac::catalog::get_ordered_versions(&catalog, &$self.catalog_key);
        if versions.is_empty() {
            return Err(Error::RustError("No versions found in catalog".into()));
        }
        let mut last_error = None;
        for $version in &versions {
            let $version = $version.as_str();
            match $body {
                Ok(result) => {
                    if last_error.is_some() {
                        console_log!(
                            "Fallback to version {} succeeded for {}",
                            $version,
                            $endpoint
                        );
                    }
                    return Ok(result);
                }
                Err(e) if $crate::stac::catalog::is_retriable_error(&e) => {
                    console_log!(
                        "Version {} not available for {}: {:?}, trying fallback",
                        $version,
                        $endpoint,
                        e
                    );
                    last_error = Some(e);
                }
                Err(e) => return Err(e),
            }
        }
        Err(last_error.unwrap_or_else(|| {
            Error::RustError(format!("No working version found for {}", $endpoint))
        }))
    }};
}
pub(crate) use with_version_fallback;

/// Resolve the catalog object without allowing a deployed production Worker
/// to be redirected. The override is deliberately narrower than a general R2
/// key: only the fixed smoke-family prefixes used by merge-only workflows are
/// accepted, and only when the Worker declares a smoke/preview environment.
pub(crate) fn resolve_catalog_key(
    environment: Option<&str>,
    override_key: Option<&str>,
) -> std::result::Result<String, String> {
    let Some(key) = override_key else {
        return Ok("catalog.json".to_string());
    };
    if !matches!(environment, Some("smoke" | "preview")) {
        return Err(
            "CATALOG_KEY_OVERRIDE is allowed only in smoke or preview environments".to_string(),
        );
    }
    let valid_family = key == "smoketest-id/catalog.json" || key == "smoketest-shards/catalog.json";
    if !valid_family {
        return Err("CATALOG_KEY_OVERRIDE must name a fixed smoketest family catalog".to_string());
    }
    Ok(key.to_string())
}

impl ShardLoader {
    /// Health check: verify catalog, latest version, and that required
    /// versioned assets exist. Response shape stays {"status":"ok","version":...}.
    pub async fn check_health(&self) -> Result<String> {
        let catalog = self.load_catalog().await?;
        let versions = get_ordered_versions(&catalog, &self.catalog_key);
        if versions.is_empty() {
            return Err(Error::RustError("No versions found in catalog".into()));
        }
        let latest = versions[0].clone();

        let collection_key = format!("{}/collection.json", latest);
        let collection_text = self
            .memoized_get_text(&collection_key, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(&collection_key))?;
        serde_json::from_str::<StacCollection>(&collection_text)
            .map_err(|e| Error::RustError(format!("Invalid {}: {}", collection_key, e)))?;

        let reverse_key = format!("{}/reverse-collection.json", latest);
        let reverse_text = self
            .memoized_get_text(&reverse_key, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(&reverse_key))?;
        serde_json::from_str::<StacCollection>(&reverse_text)
            .map_err(|e| Error::RustError(format!("Invalid {}: {}", reverse_key, e)))?;

        let id_meta_key = format!("{}/id-meta.json", latest);
        let id_collection_key = format!("{}/id-collection.json", latest);
        let id_meta_text = self
            .memoized_get_text(&id_meta_key, ID_INDEX_CACHE_TTL)
            .await?;
        let has_id_meta = if let Some(ref text) = id_meta_text {
            serde_json::from_str::<serde_json::Value>(text)
                .map_err(|e| Error::RustError(format!("Invalid {}: {}", id_meta_key, e)))?;
            true
        } else {
            false
        };
        let has_id_collection = if has_id_meta {
            true
        } else {
            match self
                .memoized_get_text(&id_collection_key, ID_INDEX_CACHE_TTL)
                .await?
            {
                Some(text) => {
                    if text.find("\"prefix_len\"").is_none() {
                        serde_json::from_str::<serde_json::Value>(&text).map_err(|e| {
                            Error::RustError(format!("Invalid {}: {}", id_collection_key, e))
                        })?;
                    }
                    true
                }
                None => false,
            }
        };
        if !has_id_collection {
            return Err(not_found(format!(
                "id-index metadata for version {} (checked {} and {})",
                latest, id_meta_key, id_collection_key
            )));
        }

        Ok(latest)
    }

    /// Latest discoverable data version, or `None` when the catalog lists none.
    ///
    /// Optional families (e.g. address) key their objects to this single
    /// version rather than walking the fallback window: an absent family object
    /// is then a clean family-unavailable signal instead of silently serving an
    /// older release's family data.
    pub(crate) async fn latest_version(&self) -> Result<Option<String>> {
        let catalog = self.load_catalog().await?;
        Ok(get_ordered_versions(&catalog, &self.catalog_key)
            .into_iter()
            .next())
    }

    pub(crate) async fn load_catalog(&self) -> Result<StacCatalog> {
        let text = self
            .memoized_get_text(&self.catalog_key, CATALOG_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(&self.catalog_key))?;

        serde_json::from_str(&text).map_err(|e| {
            Error::RustError(format!(
                "Failed to parse catalog {}: {}",
                self.catalog_key, e
            ))
        })
    }

    /// Fetch and parse a collection, memoizing the parsed value per isolate so
    /// repeated requests skip both the text-memo lookup and the JSON parse.
    async fn memoized_collection(
        &self,
        key: &str,
        parse_context: &str,
    ) -> Result<Rc<StacCollection>> {
        let now = Date::now().as_millis();
        let memoized = COLLECTION_MEMO.with(|memo| {
            memo.borrow()
                .get(key)
                .filter(|(_, expires)| *expires > now)
                .map(|(collection, _)| Rc::clone(collection))
        });
        if let Some(collection) = memoized {
            return Ok(collection);
        }

        let text = self
            .memoized_get_text(key, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(key))?;
        let collection: StacCollection = serde_json::from_str(&text)
            .map_err(|e| Error::RustError(format!("{}: {}", parse_context, e)))?;
        let collection = Rc::new(collection);
        COLLECTION_MEMO.with(|memo| {
            let mut memo = memo.borrow_mut();
            // Bound the memo: drop expired entries when it grows.
            if memo.len() > 64 {
                memo.retain(|_, (_, expires)| *expires > now);
            }
            memo.insert(
                key.to_string(),
                (Rc::clone(&collection), now + TEXT_MEMO_TTL_MS),
            );
        });
        Ok(collection)
    }

    /// Load a forward collection for a specific version.
    pub(crate) async fn load_collection(&self, version: &str) -> Result<Rc<StacCollection>> {
        let key = format!("{}/collection.json", version);
        self.memoized_collection(&key, "Failed to parse collection")
            .await
    }

    /// Load the reverse collection for a given version.
    pub(crate) async fn load_reverse_collection(
        &self,
        version: &str,
    ) -> Result<Rc<StacCollection>> {
        let key = format!("{}/reverse-collection.json", version);
        self.memoized_collection(&key, "Failed to parse reverse collection")
            .await
    }

    pub(crate) fn collection_has_shard(collection: &StacCollection, shard_id: &str) -> bool {
        // Check embedded items first (new format)
        if collection.items.contains_key(shard_id) {
            return true;
        }
        // Fall back to legacy links check
        collection
            .links
            .iter()
            .any(|l| l.rel == "item" && l.href.contains(&format!("/{}.json", shard_id)))
    }

    /// Get embedded item metadata from collection, or return None if not found.
    pub(crate) fn get_embedded_item<'b>(
        &self,
        collection: &'b StacCollection,
        shard_id: &str,
    ) -> Option<&'b EmbeddedItem> {
        collection.items.get(shard_id)
    }
}

/// Extract ordered versions from catalog (latest first, then descending by version string).
///
/// Returns up to `MAX_VERSION_ATTEMPTS` versions so the caller can try each
/// in order until one succeeds.
fn child_version(catalog_key: &str, href: &str) -> Option<String> {
    let relative = href.trim_start_matches("./");
    if relative.is_empty() {
        return None;
    }
    if let Some((version, _)) = relative.split_once('/') {
        return (!version.is_empty()).then(|| version.to_string());
    }
    let catalog_parent = catalog_key
        .rsplit_once('/')
        .map(|(parent, _)| parent)
        .unwrap_or("");
    if !catalog_parent.is_empty() {
        return catalog_parent
            .rsplit('/')
            .next()
            .filter(|version| !version.is_empty())
            .map(str::to_string);
    }

    // Preserve root-catalog behavior exactly. The nested preview catalog is
    // the only catalog whose child href intentionally omits a version.
    Some(relative.to_string())
}

pub(crate) fn get_ordered_versions(catalog: &StacCatalog, catalog_key: &str) -> Vec<String> {
    let mut latest = None;
    let mut others: Vec<String> = Vec::new();

    for link in &catalog.links {
        if link.rel != "child" {
            continue;
        }
        let Some(version) = child_version(catalog_key, &link.href) else {
            continue;
        };
        if link.latest {
            latest = Some(version);
        } else {
            others.push(version);
        }
    }

    // Sort non-latest versions descending. The .N suffix compares
    // numerically: plain string order would rank 2026-02-25.9 above
    // 2026-02-25.10.
    others.sort_unstable_by(|a, b| version_sort_key(b).cmp(&version_sort_key(a)));

    let mut versions = Vec::new();
    if let Some(v) = latest {
        versions.push(v);
    }
    versions.extend(others);
    versions.truncate(MAX_VERSION_ATTEMPTS);
    versions
}

/// Sort key for "{YYYY-MM-DD}.{N}" version strings: ISO date part compares
/// lexicographically, the .N suffix numerically. Version strings without a
/// numeric suffix compare whole-string with suffix 0.
fn version_sort_key(version: &str) -> (&str, u64) {
    match version.rsplit_once('.') {
        Some((date, n)) => match n.parse::<u64>() {
            Ok(n) => (date, n),
            Err(_) => (version, 0),
        },
        None => (version, 0),
    }
}

/// Shared test helper: build a collection whose embedded items carry the given
/// bounding boxes. Used by forward, reverse, and router shard-selection tests.
#[cfg(test)]
pub(crate) fn collection_with_bboxes(rows: &[(&str, Option<[f64; 4]>)]) -> StacCollection {
    let items = rows
        .iter()
        .map(|(id, bbox)| {
            (
                (*id).to_string(),
                EmbeddedItem {
                    record_count: 0,
                    size_bytes: 0,
                    sha256: None,
                    href: format!("reverse/{}.db", id),
                    bbox: *bbox,
                    parent_country: None,
                },
            )
        })
        .collect();

    StacCollection {
        id: "test".to_string(),
        items,
        links: vec![],
        region_sharded: std::collections::HashMap::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn catalog_key_defaults_to_production_root() {
        assert_eq!(
            resolve_catalog_key(Some("production"), None).unwrap(),
            "catalog.json"
        );
        assert_eq!(resolve_catalog_key(None, None).unwrap(), "catalog.json");
    }

    #[test]
    fn catalog_override_is_fixed_prefix_and_preview_only() {
        assert_eq!(
            resolve_catalog_key(Some("smoke"), Some("smoketest-id/catalog.json")).unwrap(),
            "smoketest-id/catalog.json"
        );
        assert_eq!(
            resolve_catalog_key(Some("preview"), Some("smoketest-shards/catalog.json")).unwrap(),
            "smoketest-shards/catalog.json"
        );
        assert!(
            resolve_catalog_key(Some("production"), Some("smoketest-id/catalog.json")).is_err()
        );
        assert!(resolve_catalog_key(Some("smoke"), Some("catalog.json")).is_err());
        assert!(resolve_catalog_key(Some("smoke"), Some("smoketest-id/../catalog.json")).is_err());
    }

    #[test]
    fn test_is_retriable_error_not_found() {
        // Anything built via not_found() must trigger version fallback —
        // including the id-index case, where a version's parquet hasn't
        // been uploaded yet.
        assert!(is_retriable_error(&not_found(
            "2026-02-25.0/collection.json"
        )));
        assert!(is_retriable_error(&not_found("shard 2026-02-25.0/HEAD.db")));
        assert!(is_retriable_error(&not_found(
            "id-index shard 2026-04-25.0/id-index/abc.parquet"
        )));
    }

    #[test]
    fn test_is_retriable_error_operational() {
        // Operational errors should NOT be retriable — even when their
        // prose happens to contain "not found".
        let e = Error::RustError("Failed to open shard database: corrupt header".into());
        assert!(!is_retriable_error(&e));

        let e = Error::RustError("Search failed: FTS5 error".into());
        assert!(!is_retriable_error(&e));

        let e = Error::RustError("Failed to parse collection: invalid JSON".into());
        assert!(!is_retriable_error(&e));

        let e = Error::RustError("R2 backend error: object not found in cache tier".into());
        assert!(!is_retriable_error(&e));
    }

    #[test]
    fn test_get_ordered_versions_latest_first() {
        let catalog = StacCatalog {
            links: vec![
                StacLink {
                    rel: "self".to_string(),
                    href: "./catalog.json".to_string(),
                    latest: false,
                },
                StacLink {
                    rel: "child".to_string(),
                    href: "./2025-12-25.0/collection.json".to_string(),
                    latest: false,
                },
                StacLink {
                    rel: "child".to_string(),
                    href: "./2026-02-25.0/collection.json".to_string(),
                    latest: true,
                },
                StacLink {
                    rel: "child".to_string(),
                    href: "./2026-01-25.0/collection.json".to_string(),
                    latest: false,
                },
            ],
        };

        let versions = get_ordered_versions(&catalog, "catalog.json");
        assert_eq!(
            versions,
            vec!["2026-02-25.0", "2026-01-25.0", "2025-12-25.0"]
        );
    }

    #[test]
    fn test_get_ordered_versions_truncates_to_max() {
        let catalog = StacCatalog {
            links: vec![
                StacLink {
                    rel: "child".to_string(),
                    href: "./2025-10-25.0/collection.json".to_string(),
                    latest: false,
                },
                StacLink {
                    rel: "child".to_string(),
                    href: "./2025-11-25.0/collection.json".to_string(),
                    latest: false,
                },
                StacLink {
                    rel: "child".to_string(),
                    href: "./2025-12-25.0/collection.json".to_string(),
                    latest: false,
                },
                StacLink {
                    rel: "child".to_string(),
                    href: "./2026-01-25.0/collection.json".to_string(),
                    latest: false,
                },
                StacLink {
                    rel: "child".to_string(),
                    href: "./2026-02-25.0/collection.json".to_string(),
                    latest: true,
                },
            ],
        };

        let versions = get_ordered_versions(&catalog, "catalog.json");
        assert_eq!(versions.len(), MAX_VERSION_ATTEMPTS);
        assert_eq!(versions[0], "2026-02-25.0"); // latest first
        assert_eq!(versions[1], "2026-01-25.0"); // then descending
        assert_eq!(versions[2], "2025-12-25.0");
    }

    #[test]
    fn test_get_ordered_versions_no_latest_flag() {
        let catalog = StacCatalog {
            links: vec![
                StacLink {
                    rel: "child".to_string(),
                    href: "./2026-01-25.0/collection.json".to_string(),
                    latest: false,
                },
                StacLink {
                    rel: "child".to_string(),
                    href: "./2026-02-25.0/collection.json".to_string(),
                    latest: false,
                },
            ],
        };

        let versions = get_ordered_versions(&catalog, "catalog.json");
        // No latest flag, so just sorted descending
        assert_eq!(versions, vec!["2026-02-25.0", "2026-01-25.0"]);
    }

    #[test]
    fn test_get_ordered_versions_single_version() {
        let catalog = StacCatalog {
            links: vec![StacLink {
                rel: "child".to_string(),
                href: "./2026-02-25.0/collection.json".to_string(),
                latest: true,
            }],
        };

        let versions = get_ordered_versions(&catalog, "catalog.json");
        assert_eq!(versions, vec!["2026-02-25.0"]);
    }

    #[test]
    fn test_get_ordered_versions_numeric_suffix_order() {
        // Lexicographic order would rank .9 above .10.
        let catalog = StacCatalog {
            links: ["./2026-02-25.9/x", "./2026-02-25.10/x", "./2026-02-25.2/x"]
                .iter()
                .map(|href| StacLink {
                    rel: "child".to_string(),
                    href: href.to_string(),
                    latest: false,
                })
                .collect(),
        };

        let versions = get_ordered_versions(&catalog, "catalog.json");
        assert_eq!(
            versions,
            vec!["2026-02-25.10", "2026-02-25.9", "2026-02-25.2"]
        );
    }

    #[test]
    fn test_get_ordered_versions_empty() {
        let catalog = StacCatalog {
            links: vec![StacLink {
                rel: "self".to_string(),
                href: "./catalog.json".to_string(),
                latest: false,
            }],
        };

        let versions = get_ordered_versions(&catalog, "catalog.json");
        assert!(versions.is_empty());
    }

    #[test]
    fn test_get_ordered_versions_uses_nested_catalog_parent_for_bare_child() {
        let catalog = StacCatalog {
            links: vec![StacLink {
                rel: "child".to_string(),
                href: "./id-collection.json".to_string(),
                latest: true,
            }],
        };

        let versions = get_ordered_versions(&catalog, "smoketest-id/catalog.json");
        assert_eq!(versions, vec!["smoketest-id"]);
    }
}
