//! R2, edge (Cache API), and isolate-level caching primitives.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use bytes::Bytes;
use futures::StreamExt;
use geocoder_core::Database;
use sha2::{Digest, Sha256};
use worker::*;

use crate::address_pages::{
    decode_useful_gzip_range_measured, parse_address_index, parse_useful_gzip_header,
    AddressPageRecord, MAX_INDEX_BYTES, MAX_STORED_PAGE_RANGE,
};
use crate::range_reader::RangeReader;

use super::{not_found, ShardLoader};

pub(crate) struct CacheRangeRead {
    pub bytes: Bytes,
    pub cache_hit: bool,
}

fn validate_at_most_prefix_length(actual: usize, max_bytes: usize) -> Result<()> {
    if max_bytes == 0 || actual > max_bytes {
        return Err(Error::RustError(
            "Prefix response exceeds requested hard cap".into(),
        ));
    }
    Ok(())
}

pub(crate) struct AddressPageLookup {
    pub records: Vec<AddressPageRecord>,
}

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
const DB_CACHE_MAX_BYTES: usize = 48 * 1024 * 1024;
const DB_CACHE_MAX_ENTRIES: usize = 4;
// Catalog/collection JSON memo TTL. Short: this bounds how stale the
// version pointer can be within one isolate.
pub(crate) const TEXT_MEMO_TTL_MS: u64 = 60_000;
const TEXT_MEMO_MAX_ENTRIES: usize = 64;
// Isolate-retained memory budget (Cloudflare's standard isolate limit is 128
// MiB): byte-backed long-lived caches admit at most 48 MiB of SQLite plus 4
// MiB of text. V2 parsed caches retain one generation, with address bounded to
// an 8 MiB wire document / 4,096 canonical routes and Places to a 2 MiB wire
// catalog. Even budgeting 2x their wire caps for parsed structure overhead,
// that is 72 MiB retained, leaving 56 MiB for the largest 16 MiB Places
// posting plan, response shaping, and runtime overhead. Oversize DBs and parsed
// address raw text are explicitly not retained.
const TEXT_MEMO_MAX_BYTES: usize = 4 * 1024 * 1024;

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
    /// Stream an immutable object through SHA-256 without retaining its body.
    /// Used only during cold v2 release admission for identities explicitly
    /// pinned by the release manifest.
    pub(crate) async fn immutable_object_identity(
        &self,
        key: &str,
        max_bytes: u64,
    ) -> Result<(u64, String)> {
        let object = self
            .bucket
            .get(key)
            .execute()
            .await?
            .ok_or_else(|| not_found(key))?;
        let declared_size = object.size();
        if declared_size == 0 || declared_size > max_bytes {
            return Err(Error::RustError(format!(
                "Immutable object {key} is outside its identity-stream size cap"
            )));
        }
        let body = object
            .body()
            .ok_or_else(|| Error::RustError(format!("Immutable object {key} has no body")))?;
        let mut stream = body.stream()?;
        let mut actual_size = 0_u64;
        let mut hasher = Sha256::new();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk?;
            actual_size = actual_size
                .checked_add(chunk.len() as u64)
                .ok_or_else(|| Error::RustError("Immutable object size overflow".into()))?;
            if actual_size > max_bytes {
                return Err(Error::RustError(format!(
                    "Immutable object {key} streamed beyond its identity cap"
                )));
            }
            hasher.update(&chunk);
        }
        if actual_size != declared_size {
            return Err(Error::RustError(format!(
                "Immutable object {key} streamed size differs from R2 metadata"
            )));
        }
        Ok((actual_size, format!("{:x}", hasher.finalize())))
    }

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
        Ok(self
            .cached_range_read_measured(key, offset, length)
            .await?
            .map(|read| read.bytes))
    }

    pub(crate) async fn cached_range_read_measured(
        &self,
        key: &str,
        offset: u64,
        length: u64,
    ) -> Result<Option<CacheRangeRead>> {
        self.cached_range_read_measured_with_ttl(key, offset, length, ID_INDEX_CACHE_TTL)
            .await
    }

    /// Range-read one immutable or mutable object with the caller's cache TTL.
    /// The legacy wrapper above keeps the ID-index TTL; construction artifacts
    /// use the seven-day immutable TTL without cloning this range/cache path.
    pub(crate) async fn cached_range_read_measured_with_ttl(
        &self,
        key: &str,
        offset: u64,
        length: u64,
        ttl: u64,
    ) -> Result<Option<CacheRangeRead>> {
        let cache_key = format!("{}{}__r{}-{}", CACHE_PREFIX, key, offset, length);

        let request = Request::new(&cache_key, Method::Get)?;
        if let Some(mut response) = self.cache.get(&request, false).await? {
            let bytes = response.bytes().await?;
            if bytes.is_empty() {
                return Ok(None);
            }
            if bytes.len() as u64 != length {
                return Err(Error::RustError(
                    "Cached range length differs from requested extent".into(),
                ));
            }
            console_log!("Cache HIT range: {} ({}..{})", key, offset, offset + length);
            return Ok(Some(CacheRangeRead {
                bytes: Bytes::from(bytes),
                cache_hit: true,
            }));
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
        if bytes.len() as u64 != length {
            return Err(Error::RustError(
                "R2 range length differs from requested extent".into(),
            ));
        }
        self.cache_put_bytes_background(cache_key, bytes.clone(), ttl)
            .await;

        Ok(Some(CacheRangeRead {
            bytes,
            cache_hit: false,
        }))
    }

    /// Read at most `max_bytes` from the start of an object and only cache the
    /// result after proving the object did not fill a `max + 1` sentinel range.
    /// This prevents a corrupt index from being fully materialized by
    /// `cached_get` before its size cap can be checked.
    pub(crate) async fn cached_bounded_prefix_read_measured(
        &self,
        key: &str,
        max_bytes: usize,
        ttl: u64,
    ) -> Result<Option<CacheRangeRead>> {
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
            return Ok(Some(CacheRangeRead {
                bytes: Bytes::from(bytes),
                cache_hit: true,
            }));
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
        Ok(Some(CacheRangeRead {
            bytes,
            cache_hit: false,
        }))
    }

    /// Read up to `max_bytes` from the start of an object. Unlike the bounded
    /// whole-object helper above, a short response at EOF is valid: callers use
    /// this only for a fixed-size envelope/header prefix of a possibly larger
    /// object. Both cache hits and R2 responses are checked before acceptance.
    pub(crate) async fn cached_at_most_prefix_read_measured(
        &self,
        key: &str,
        max_bytes: usize,
        ttl: u64,
    ) -> Result<Option<CacheRangeRead>> {
        validate_at_most_prefix_length(0, max_bytes)?;
        let cache_key = format!("{}{}__at-most-prefix-{}", CACHE_PREFIX, key, max_bytes);
        let request = Request::new(&cache_key, Method::Get)?;
        if let Some(mut response) = self.cache.get(&request, false).await? {
            let bytes = response.bytes().await?;
            if bytes.is_empty() {
                return Ok(None);
            }
            validate_at_most_prefix_length(bytes.len(), max_bytes)?;
            return Ok(Some(CacheRangeRead {
                bytes: Bytes::from(bytes),
                cache_hit: true,
            }));
        }
        let obj = self
            .bucket
            .get(key)
            .range(worker::Range::OffsetWithLength {
                offset: 0,
                length: max_bytes as u64,
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
            .ok_or_else(|| Error::RustError("Empty prefix body".into()))?;
        let bytes = Bytes::from(body.bytes().await?);
        if bytes.is_empty() {
            return Err(Error::RustError("R2 prefix body is empty".into()));
        }
        validate_at_most_prefix_length(bytes.len(), max_bytes)?;
        self.cache_put_bytes_background(cache_key, bytes.clone(), ttl)
            .await;
        Ok(Some(CacheRangeRead {
            bytes,
            cache_hit: false,
        }))
    }

    /// Exact-address storage read path used by unified `/v2/forward`.
    ///
    /// The caller supplies immutable versioned object keys and an already
    /// normalized eight-field address key. The small side index is edge-cached,
    /// then exactly one group-aligned gzip page is range-read and decoded under
    /// the hard limits in `address_pages`. Every candidate that carries the
    /// exact key is returned in producer order; the caller applies the response
    /// candidate cap.
    pub(crate) async fn lookup_address_page(
        &self,
        index_key: &str,
        data_key: &str,
        lookup_key: &[String; 8],
    ) -> Result<AddressPageLookup> {
        use geocoder_core::pages::ByteRange;

        let mut index_reader = RangeReader::new(self, index_key);
        let index_bytes = index_reader
            .bounded_prefix(MAX_INDEX_BYTES, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(index_key))?;
        let index = parse_address_index(&index_bytes)
            .map_err(|error| Error::RustError(format!("Invalid address page index: {error}")))?;
        let Some(extent) = index.find(lookup_key).copied() else {
            return Ok(AddressPageLookup {
                records: Vec::new(),
            });
        };

        // Validate the object envelope independently from the index. A 4 KiB
        // immutable range is enough for the producer's capped JSON header and
        // is edge-cached separately from candidate pages.
        let mut data_reader = RangeReader::new(self, data_key);
        let header = data_reader
            .at_most_prefix(4096, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(data_key))?;
        parse_useful_gzip_header(&header)
            .map_err(|error| Error::RustError(format!("Invalid address page data: {error}")))?;

        // Route the candidate page through the shared coalescing planner. Today
        // this is one want -> one range read; the same primitive serves the
        // Places compact shard's future multi-span lexicon/postings reads.
        let want = ByteRange {
            offset: extent.offset,
            length: extent.length,
        };
        let page = data_reader
            .coalesced(&[want], 0, MAX_STORED_PAGE_RANGE)
            .await?
            .into_iter()
            .next()
            .ok_or_else(|| Error::RustError("Address range plan returned no page".into()))?;
        let decode = decode_useful_gzip_range_measured(&page, extent.rows, lookup_key)
            .map_err(|error| Error::RustError(format!("Invalid address page: {error}")))?;
        Ok(AddressPageLookup {
            records: decode.records,
        })
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
            let incoming_bytes = text.as_ref().map_or(0, String::len);
            if incoming_bytes <= TEXT_MEMO_MAX_BYTES {
                bound_text_memo(&mut memo, key, now, incoming_bytes);
                memo.insert(key.to_string(), (text.clone(), now + TEXT_MEMO_TTL_MS));
            }
        });
        Ok(text)
    }

    /// Fetch a UTF-8 control document without ever materializing an object
    /// larger than `max_bytes`. This is the safe path for mutable discovery
    /// roots and large family routing manifests: the `max + 1` R2 range proves
    /// EOF before the body is admitted to the edge or isolate cache.
    pub(crate) async fn memoized_get_bounded_text(
        &self,
        key: &str,
        max_bytes: usize,
        ttl: u64,
    ) -> Result<Option<String>> {
        let now = Date::now().as_millis();
        let memoized = TEXT_MEMO.with(|memo| {
            memo.borrow()
                .get(key)
                .filter(|(_, expires)| *expires > now)
                .map(|(text, _)| text.clone())
        });
        if let Some(text) = memoized {
            if text.as_ref().is_some_and(|value| value.len() > max_bytes) {
                return Err(Error::RustError(format!(
                    "Control document {key} exceeds its hard cap"
                )));
            }
            return Ok(text);
        }

        let text = match self
            .cached_bounded_prefix_read_measured(key, max_bytes, ttl)
            .await?
        {
            Some(read) => Some(
                std::str::from_utf8(&read.bytes)
                    .map_err(|error| Error::RustError(format!("Invalid UTF-8: {error}")))?
                    .to_owned(),
            ),
            None => None,
        };
        TEXT_MEMO.with(|memo| {
            let mut memo = memo.borrow_mut();
            let incoming_bytes = text.as_ref().map_or(0, String::len);
            if incoming_bytes <= TEXT_MEMO_MAX_BYTES {
                bound_text_memo(&mut memo, key, now, incoming_bytes);
                memo.insert(key.to_string(), (text.clone(), now + TEXT_MEMO_TTL_MS));
            }
        });
        Ok(text)
    }

    /// Drop one isolate-level text immediately after a caller has converted it
    /// into a bounded parsed representation. Large address routing documents
    /// must not remain retained alongside that representation.
    pub(crate) fn forget_memoized_text(&self, key: &str) {
        TEXT_MEMO.with(|memo| {
            forget_text_memo(&mut memo.borrow_mut(), key);
        });
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
            if db_cacheable(size) && !cache.iter().any(|(k, _, _)| k == shard_key) {
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

fn db_cacheable(size: usize) -> bool {
    size <= DB_CACHE_MAX_BYTES
}

fn bound_text_memo(
    memo: &mut HashMap<String, (Option<String>, u64)>,
    incoming_key: &str,
    now: u64,
    incoming_bytes: usize,
) {
    memo.retain(|_, (_, expires)| *expires > now);
    // The caller inserts the incoming value immediately afterward. Remove a
    // previous value for that key so replacement with a larger body is also
    // charged against the byte budget.
    memo.remove(incoming_key);
    while memo.len() >= TEXT_MEMO_MAX_ENTRIES
        || memo
            .values()
            .map(|(text, _)| text.as_ref().map_or(0, String::len))
            .sum::<usize>()
            .saturating_add(incoming_bytes)
            > TEXT_MEMO_MAX_BYTES
    {
        if let Some(oldest) = memo
            .iter()
            .min_by_key(|(_, (_, expires))| *expires)
            .map(|(key, _)| key.clone())
        {
            memo.remove(&oldest);
        } else {
            break;
        }
    }
}

fn forget_text_memo(memo: &mut HashMap<String, (Option<String>, u64)>, key: &str) {
    memo.remove(key);
}

#[cfg(test)]
mod address_prefix_tests {
    use std::collections::HashMap;

    use super::{
        bound_text_memo, db_cacheable, forget_text_memo, validate_at_most_prefix_length,
        DB_CACHE_MAX_BYTES, TEXT_MEMO_MAX_BYTES, TEXT_MEMO_MAX_ENTRIES,
    };

    #[test]
    fn at_most_prefix_accepts_short_eof_and_exact_cap() {
        assert!(validate_at_most_prefix_length(985, 4096).is_ok());
        assert!(validate_at_most_prefix_length(4096, 4096).is_ok());
    }

    #[test]
    fn at_most_prefix_rejects_overflow_and_zero_cap() {
        assert!(validate_at_most_prefix_length(4097, 4096).is_err());
        assert!(validate_at_most_prefix_length(0, 0).is_err());
    }

    #[test]
    fn text_memo_is_strictly_bounded_after_expiry_cleanup() {
        let mut memo = (0..TEXT_MEMO_MAX_ENTRIES)
            .map(|index| {
                (
                    format!("key-{index}"),
                    (Some(String::new()), index as u64 + 10),
                )
            })
            .collect::<HashMap<_, _>>();
        bound_text_memo(&mut memo, "incoming", 0, 0);
        assert_eq!(memo.len(), TEXT_MEMO_MAX_ENTRIES - 1);
        assert!(!memo.contains_key("key-0"));

        memo.insert("incoming".into(), (None, 100));
        bound_text_memo(&mut memo, "incoming", 50, 0);
        memo.insert("incoming".into(), (None, 100));
        assert!(memo.len() <= TEXT_MEMO_MAX_ENTRIES);
    }

    #[test]
    fn maximum_text_replacement_evicts_every_other_retained_body() {
        let mut memo = HashMap::from([
            ("incoming".into(), (Some("old".into()), 30)),
            ("oldest".into(), (Some("a".repeat(1024)), 10)),
            ("newer".into(), (Some("b".repeat(1024)), 20)),
        ]);
        bound_text_memo(&mut memo, "incoming", 0, TEXT_MEMO_MAX_BYTES);
        memo.insert(
            "incoming".into(),
            (Some("x".repeat(TEXT_MEMO_MAX_BYTES)), 100),
        );
        assert_eq!(memo.len(), 1);
        assert_eq!(
            memo.values()
                .map(|(text, _)| text.as_ref().map_or(0, String::len))
                .sum::<usize>(),
            TEXT_MEMO_MAX_BYTES
        );
    }

    #[test]
    fn parsed_document_can_evict_its_raw_text_immediately() {
        let mut memo = HashMap::from([("address.json".into(), (Some("raw".into()), 100))]);
        forget_text_memo(&mut memo, "address.json");
        assert!(memo.is_empty());
    }

    #[test]
    fn oversize_database_is_not_admitted_to_the_isolate_cache() {
        assert!(db_cacheable(DB_CACHE_MAX_BYTES));
        assert!(!db_cacheable(DB_CACHE_MAX_BYTES + 1));
    }
}
