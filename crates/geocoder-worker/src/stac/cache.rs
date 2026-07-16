//! R2, edge (Cache API), and isolate-level caching primitives.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use bytes::Bytes;
use geocoder_core::Database;
use worker::*;

use crate::address_pages::{
    decode_useful_gzip_range, parse_useful_gzip_header, AddressPageIndex, AddressPageRecord,
    MAX_INDEX_BYTES,
};

use super::{not_found, ShardLoader};

// Cache TTLs for different resource types
pub(crate) const CATALOG_CACHE_TTL: u64 = 300; // 5 minutes - need fresh version pointers
                                               // SQLite shards and collection JSON under a {version}/ prefix are never
                                               // rewritten (versioned paths = natural invalidation), so cache them at
                                               // the edge for a week.
pub(crate) const IMMUTABLE_CACHE_TTL: u64 = 7 * 24 * 3600;
// The id-index is NOT immutable: patch-id-stage rebuilds
// {version}/id-index/*.parquet and re-uploads id-meta.json in place with no
// cache purge. A stale cached footer combined with a rewritten file yields
// garbage range reads (non-retriable 500s), so bound the exposure to an
// hour instead of a week.
pub(crate) const ID_INDEX_CACHE_TTL: u64 = 3600;

// Cache key prefix (uses custom domain for Cache API to work)
const CACHE_PREFIX: &str = "https://geocoder.bradr.dev/__cache/";

const NEGATIVE_CACHE_TTL: u64 = 30; // 30 seconds - avoids hammering R2 for missing objects

// Isolate-level (in-memory) cache limits. Workers isolates persist across
// requests; keeping deserialized shard databases in memory lets warm
// requests skip the Cache API round trip and the SQLite deserialize copy.
const DB_CACHE_MAX_BYTES: usize = 64 * 1024 * 1024;
const DB_CACHE_MAX_ENTRIES: usize = 4;
// Catalog/collection JSON memo TTL. Short: this bounds how stale the
// version pointer can be within one isolate.
pub(crate) const TEXT_MEMO_TTL_MS: u64 = 60_000;

thread_local! {
    /// Deserialized shard databases keyed by versioned R2 key (immutable
    /// content). Vec ordered LRU-last; evicted by byte budget + entry count.
    static DB_CACHE: RefCell<Vec<(String, Rc<Database>, usize)>> =
        const { RefCell::new(Vec::new()) };
    /// Small JSON texts (catalog/collections/id-meta) with expiry timestamps.
    static TEXT_MEMO: RefCell<HashMap<String, (Option<String>, u64)>> =
        RefCell::new(HashMap::new());
}

impl ShardLoader {
    /// Write bytes to the edge cache, off the critical path via `waitUntil`
    /// when an execution context is available (best effort either way;
    /// inline await otherwise). Takes `Bytes` so the caller only pays a
    /// refcount bump: the full body copy a `Response` requires happens
    /// inside the deferred future, not before returning to the user.
    async fn cache_put_bytes_background(&self, cache_key: String, bytes: Bytes, ttl: u64) {
        let put = async move {
            let result: Result<()> = async {
                let headers = Headers::new();
                headers.set("Cache-Control", &format!("s-maxage={}", ttl))?;
                headers.set("Content-Type", "application/octet-stream")?;
                let response = Response::from_bytes(bytes.to_vec())?.with_headers(headers);
                let request = Request::new(&cache_key, Method::Get)?;
                Cache::default().put(&request, response).await
            }
            .await;
            if let Err(e) = result {
                console_log!("Cache PUT failed for {}: {:?}", cache_key, e);
            }
        };
        match &self.ctx {
            Some(ctx) => ctx.wait_until(put),
            None => put.await,
        }
    }

    /// Fetch from R2 with edge caching via Cache API.
    ///
    /// Caches both positive results (with the caller's TTL) and negative results
    /// (object not found, with a short TTL) to avoid hammering R2 during deployments.
    pub(crate) async fn cached_get(&self, key: &str, ttl: u64) -> Result<Option<Bytes>> {
        let cache_key = format!("{}{}", CACHE_PREFIX, key);

        // Try cache first
        let request = Request::new(&cache_key, Method::Get)?;
        if let Some(mut response) = self.cache.get(&request, false).await? {
            let bytes = response.bytes().await?;
            // Empty body is our negative-cache sentinel (real R2 objects are never empty)
            if bytes.is_empty() {
                console_log!("Cache HIT (negative): {}", key);
                return Ok(None);
            }
            console_log!("Cache HIT: {}", key);
            return Ok(Some(Bytes::from(bytes)));
        }

        console_log!("Cache MISS: {}", key);

        // Fetch from R2
        let obj = self.bucket.get(key).execute().await?;
        if let Some(obj) = obj {
            let body = obj
                .body()
                .ok_or_else(|| Error::RustError("Empty object".into()))?;
            // Bytes: the cache write below only bumps a refcount here; the
            // multi-MB body copy happens inside the deferred future.
            let bytes = Bytes::from(body.bytes().await?);
            self.cache_put_bytes_background(cache_key, bytes.clone(), ttl)
                .await;
            return Ok(Some(bytes));
        }

        // Cache the negative result (empty body sentinel) with a short TTL to avoid
        // repeated R2 GETs for objects that don't exist yet during deployments
        self.cache_put_bytes_background(cache_key, Bytes::new(), NEGATIVE_CACHE_TTL)
            .await;

        Ok(None)
    }

    /// Range-read part of an R2 object with edge caching (id-index TTL:
    /// patch runs can rewrite these files in place).
    /// Used for parquet row groups, which are re-read often by ID lookups.
    pub(crate) async fn cached_range_read(
        &self,
        key: &str,
        offset: u64,
        length: u64,
    ) -> Result<Option<Bytes>> {
        let cache_key = format!("{}{}__r{}-{}", CACHE_PREFIX, key, offset, length);

        let request = Request::new(&cache_key, Method::Get)?;
        if let Some(mut response) = self.cache.get(&request, false).await? {
            let bytes = response.bytes().await?;
            if bytes.is_empty() {
                return Ok(None);
            }
            console_log!("Cache HIT range: {} ({}..{})", key, offset, offset + length);
            return Ok(Some(Bytes::from(bytes)));
        }

        let obj = self
            .bucket
            .get(key)
            .range(worker::Range::OffsetWithLength { offset, length })
            .execute()
            .await?;
        let Some(obj) = obj else {
            self.cache_put_bytes_background(cache_key, Bytes::new(), NEGATIVE_CACHE_TTL)
                .await;
            return Ok(None);
        };
        let body = obj
            .body()
            .ok_or_else(|| Error::RustError("Empty range body".into()))?;
        let bytes = Bytes::from(body.bytes().await?);
        self.cache_put_bytes_background(cache_key, bytes.clone(), ID_INDEX_CACHE_TTL)
            .await;

        Ok(Some(bytes))
    }

    /// Read at most `max_bytes` from the start of an object and only cache the
    /// result after proving the object did not fill a `max + 1` sentinel range.
    /// This prevents a corrupt index from being fully materialized by
    /// `cached_get` before its size cap can be checked.
    async fn cached_bounded_prefix_read(
        &self,
        key: &str,
        max_bytes: usize,
        ttl: u64,
    ) -> Result<Option<Bytes>> {
        let sentinel_length = max_bytes
            .checked_add(1)
            .ok_or_else(|| Error::RustError("Bounded prefix length overflow".into()))?;
        let cache_key = format!("{}{}__bounded-prefix-{}", CACHE_PREFIX, key, max_bytes);
        let request = Request::new(&cache_key, Method::Get)?;
        if let Some(mut response) = self.cache.get(&request, false).await? {
            let bytes = response.bytes().await?;
            if bytes.is_empty() {
                return Ok(None);
            }
            if bytes.len() > max_bytes {
                return Err(Error::RustError("Cached bounded prefix exceeds cap".into()));
            }
            return Ok(Some(Bytes::from(bytes)));
        }
        let obj = self
            .bucket
            .get(key)
            .range(worker::Range::OffsetWithLength {
                offset: 0,
                length: sentinel_length as u64,
            })
            .execute()
            .await?;
        let Some(obj) = obj else {
            self.cache_put_bytes_background(cache_key, Bytes::new(), NEGATIVE_CACHE_TTL)
                .await;
            return Ok(None);
        };
        let body = obj
            .body()
            .ok_or_else(|| Error::RustError("Empty bounded-prefix body".into()))?;
        let bytes = Bytes::from(body.bytes().await?);
        if bytes.len() > max_bytes {
            return Err(Error::RustError(format!(
                "R2 object {} exceeds bounded prefix cap",
                key
            )));
        }
        self.cache_put_bytes_background(cache_key, bytes.clone(), ttl)
            .await;
        Ok(Some(bytes))
    }

    /// Experimental exact-address storage path.
    ///
    /// The caller supplies immutable versioned object keys and an already
    /// normalized eight-field address key. The small side index is edge-cached,
    /// then exactly one group-aligned gzip page is range-read and decoded under
    /// the hard limits in `address_pages`. This is deliberately not routed yet:
    /// the spike must measure real Worker/R2 latency before becoming an API.
    pub(crate) async fn lookup_address_page_spike(
        &self,
        index_key: &str,
        data_key: &str,
        lookup_key: &[String; 8],
    ) -> Result<Vec<AddressPageRecord>> {
        let index_bytes = self
            .cached_bounded_prefix_read(index_key, MAX_INDEX_BYTES, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(index_key))?;
        let index = AddressPageIndex::parse(&index_bytes)
            .map_err(|error| Error::RustError(format!("Invalid address page index: {error}")))?;
        let Some(extent) = index.find(lookup_key).cloned() else {
            return Ok(Vec::new());
        };

        // Validate the object envelope independently from the index. A 4 KiB
        // immutable range is enough for the producer's capped JSON header and
        // is edge-cached separately from candidate pages.
        let header = self
            .cached_range_read(data_key, 0, 4096)
            .await?
            .ok_or_else(|| not_found(data_key))?;
        parse_useful_gzip_header(&header)
            .map_err(|error| Error::RustError(format!("Invalid address page data: {error}")))?;

        let page = self
            .cached_range_read(data_key, extent.offset, extent.length)
            .await?
            .ok_or_else(|| not_found(format!("{} range", data_key)))?;
        if page.len() as u64 != extent.length {
            return Err(Error::RustError(
                "Address page range length differs from index".into(),
            ));
        }
        decode_useful_gzip_range(&page, extent.rows, lookup_key)
            .map_err(|error| Error::RustError(format!("Invalid address page: {error}")))
    }

    /// Fetch small JSON text with an isolate-level memo in front of the edge
    /// cache. Saves a Cache API round trip per request for catalog/collection
    /// metadata; the memo TTL bounds staleness within an isolate.
    pub(crate) async fn memoized_get_text(&self, key: &str, ttl: u64) -> Result<Option<String>> {
        let now = Date::now().as_millis();
        let memoized = TEXT_MEMO.with(|memo| {
            memo.borrow()
                .get(key)
                .filter(|(_, expires)| *expires > now)
                .map(|(text, _)| text.clone())
        });
        if let Some(text) = memoized {
            return Ok(text);
        }

        let text = self.cached_get_text(key, ttl).await?;
        TEXT_MEMO.with(|memo| {
            let mut memo = memo.borrow_mut();
            // Bound the memo: drop expired entries when it grows.
            if memo.len() > 64 {
                memo.retain(|_, (_, expires)| *expires > now);
            }
            memo.insert(key.to_string(), (text.clone(), now + TEXT_MEMO_TTL_MS));
        });
        Ok(text)
    }

    /// Fetch text from R2 with caching.
    pub(crate) async fn cached_get_text(&self, key: &str, ttl: u64) -> Result<Option<String>> {
        match self.cached_get(key, ttl).await? {
            Some(bytes) => {
                let text = std::str::from_utf8(&bytes)
                    .map_err(|e| Error::RustError(format!("Invalid UTF-8: {}", e)))?;
                Ok(Some(text.to_owned()))
            }
            None => Ok(None),
        }
    }

    /// Fetch parquet suffix (footer) from R2 with edge caching.
    ///
    /// Returns (file_size, tail_bytes) on success, None if the object doesn't exist.
    /// The cache value is: 8 bytes (file_size as u64 LE) + raw suffix bytes.
    /// `suffix_size` defaults to 32KB which covers typical parquet footers;
    /// callers retry with a larger size when the footer overflows it.
    pub(crate) async fn cached_suffix_read(
        &self,
        key: &str,
        suffix_size: u64,
    ) -> Result<Option<(u64, Bytes)>> {
        let cache_key = format!("{}{}__suffix{}", CACHE_PREFIX, key, suffix_size);

        // Try cache first
        let request = Request::new(&cache_key, Method::Get)?;
        if let Some(mut response) = self.cache.get(&request, false).await? {
            let bytes = response.bytes().await?;
            if bytes.is_empty() {
                console_log!("Cache HIT (negative suffix): {}", key);
                return Ok(None);
            }
            if bytes.len() > 8 {
                let file_size = u64::from_le_bytes(bytes[..8].try_into().unwrap());
                let tail_bytes = Bytes::from(bytes[8..].to_vec());
                console_log!("Cache HIT suffix: {} ({}B)", key, tail_bytes.len());
                return Ok(Some((file_size, tail_bytes)));
            }
        }

        console_log!("Cache MISS suffix: {}", key);

        // Fetch suffix from R2
        let obj = self
            .bucket
            .get(key)
            .range(worker::Range::Suffix {
                suffix: suffix_size,
            })
            .execute()
            .await?;
        let obj = match obj {
            Some(o) => o,
            None => {
                // Negative cache
                self.cache_put_bytes_background(cache_key, Bytes::new(), NEGATIVE_CACHE_TTL)
                    .await;
                return Ok(None);
            }
        };
        let file_size = obj.size();
        let body = obj
            .body()
            .ok_or_else(|| Error::RustError("Empty body on suffix read".into()))?;
        let tail_bytes = Bytes::from(body.bytes().await?);

        // Store in cache: 8 bytes file_size + suffix bytes
        let mut cache_bytes = Vec::with_capacity(8 + tail_bytes.len());
        cache_bytes.extend_from_slice(&file_size.to_le_bytes());
        cache_bytes.extend_from_slice(&tail_bytes);
        self.cache_put_bytes_background(cache_key, Bytes::from(cache_bytes), ID_INDEX_CACHE_TTL)
            .await;

        Ok(Some((file_size, tail_bytes)))
    }

    /// Load a shard database, preferring the isolate-level cache, then the
    /// edge cache, then R2. Returns the database and its serialized size.
    pub(crate) async fn load_shard_db(&self, shard_key: &str) -> Result<(Rc<Database>, usize)> {
        let cached = DB_CACHE.with(|c| {
            let mut cache = c.borrow_mut();
            cache
                .iter()
                .position(|(k, _, _)| k == shard_key)
                .map(|pos| {
                    // Move to MRU position (end of the Vec)
                    let entry = cache.remove(pos);
                    let hit = (Rc::clone(&entry.1), entry.2);
                    cache.push(entry);
                    hit
                })
        });
        if let Some((db, size)) = cached {
            console_log!("DB cache HIT: {}", shard_key);
            return Ok((db, size));
        }

        let shard_bytes = self
            .cached_get(shard_key, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(format!("shard {}", shard_key)))?;
        let size = shard_bytes.len();

        let db = Rc::new(Database::from_bytes(&shard_bytes).map_err(|e| {
            Error::RustError(format!(
                "Failed to open shard database {}: {}",
                shard_key, e
            ))
        })?);
        drop(shard_bytes);

        DB_CACHE.with(|c| {
            let mut cache = c.borrow_mut();
            // A concurrent request that missed at the same time may have
            // inserted this key while we awaited the fetch; a duplicate
            // entry would double-count against the byte budget.
            if !cache.iter().any(|(k, _, _)| k == shard_key) {
                cache.push((shard_key.to_string(), Rc::clone(&db), size));
                // Evict from the LRU end; always keep the entry just inserted.
                while cache.len() > 1
                    && (cache.len() > DB_CACHE_MAX_ENTRIES
                        || cache.iter().map(|(_, _, s)| *s).sum::<usize>() > DB_CACHE_MAX_BYTES)
                {
                    let (evicted, _, _) = cache.remove(0);
                    console_log!("DB cache evict: {}", evicted);
                }
            }
        });

        Ok((db, size))
    }
}
