# Build Upload Retry & Parquet Footer Caching — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add retry logic to ID index build uploads and cache parquet footers at the edge to cut repeat ID lookups from 2 R2 reads to 1.

**Architecture:** Two independent changes — (1) wrap the DuckDB `COPY TO s3://` in the build worker with the existing `_retry_transient` helper, and fix the broken wrangler upload retry in `_upload_to_r2`; (2) cache the 32KB parquet suffix read in the Cloudflare Cache API so subsequent lookups to the same shard prefix skip the footer R2 read.

**Tech Stack:** Python (build_id_index.py), Rust/wasm32 (geocoder-worker), Cloudflare Workers Cache API

---

### Task 1: Add retry to build phase COPY TO S3

**Files:**
- Modify: `scripts/build_id_index.py:592-599`

- [ ] **Step 1: Wrap the COPY TO statement with `_retry_transient`**

In `_worker_build_r2_batch`, replace the bare `con.execute(COPY TO ...)` at line 592-599 with a `_retry_transient` wrapped call:

```python
            # Sort and write to R2
            r2_dest = f"s3://{bucket}/{version}/id-index/{prefix}.parquet"
            def _do_copy():
                con.execute(f"""
                    COPY (
                        SELECT * FROM ({union_query}) ORDER BY id
                    ) TO '{r2_dest}'
                    (FORMAT PARQUET, COMPRESSION UNCOMPRESSED, ROW_GROUP_SIZE 100000);
                """)
            _retry_transient(_do_copy)()
```

This matches the pattern used in `_partition_release_type` (line 422) and registry staging.

- [ ] **Step 2: Commit**

```bash
git add scripts/build_id_index.py
git commit -m "Add retry to build phase COPY TO S3 uploads"
```

---

### Task 2: Fix `_upload_to_r2` retry backoff

**Files:**
- Modify: `scripts/build_id_index.py:486-496`

- [ ] **Step 1: Add sleep between retry attempts**

Replace `_upload_to_r2` with a version that sleeps between retries:

```python
def _upload_to_r2(local_path, r2_key, retries=3):
    """Upload a file to R2 via wrangler with retries."""
    for attempt in range(retries):
        result = subprocess.run(
            ["wrangler", "r2", "object", "put", r2_key,
             "--file", str(local_path), "--remote"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return None
        if attempt < retries - 1:
            wait = 5 * (2 ** attempt)  # 5s, 10s, 20s
            print(f"    Upload retry {attempt + 1}/{retries} for {r2_key}, waiting {wait}s...")
            time.sleep(wait)
    return result.stderr[:200]
```

- [ ] **Step 2: Commit**

```bash
git add scripts/build_id_index.py
git commit -m "Fix upload retry with exponential backoff in _upload_to_r2"
```

---

### Task 3: Cache parquet footer suffix in the worker

**Files:**
- Modify: `crates/geocoder-worker/src/stac.rs:616-654`

- [ ] **Step 1: Add a `cached_suffix_read` method**

Add a new method on `ShardLoader` after `cached_get_text` (around line 330). This caches the suffix bytes + file size together, keyed by shard path:

```rust
    /// Fetch parquet suffix (footer) from R2 with edge caching.
    ///
    /// Returns (file_size, tail_bytes) on success, None if the object doesn't exist.
    /// The cache value is: 8 bytes (file_size as u64 LE) + raw suffix bytes.
    async fn cached_suffix_read(
        &self,
        key: &str,
        suffix_size: u64,
    ) -> Result<Option<(u64, Bytes)>> {
        let cache_key = format!("{}{}__suffix", CACHE_PREFIX, key);

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
                let neg_headers = Headers::new();
                neg_headers.set("Cache-Control", &format!("s-maxage={}", NEGATIVE_CACHE_TTL))?;
                neg_headers.set("Content-Type", "application/octet-stream")?;
                let neg_response = Response::from_bytes(vec![])?.with_headers(neg_headers);
                let neg_request = Request::new(&cache_key, Method::Get)?;
                let _ = self.cache.put(&neg_request, neg_response).await;
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

        let headers = Headers::new();
        headers.set("Cache-Control", &format!("s-maxage={}", SHARD_CACHE_TTL))?;
        headers.set("Content-Type", "application/octet-stream")?;
        let cache_response = Response::from_bytes(cache_bytes)?.with_headers(headers);
        let cache_request = Request::new(&cache_key, Method::Get)?;
        if let Err(e) = self.cache.put(&cache_request, cache_response).await {
            console_log!("Cache PUT failed for suffix {}: {:?}", key, e);
        }

        Ok(Some((file_size, tail_bytes)))
    }
```

- [ ] **Step 2: Replace the suffix read in `try_lookup_id` with `cached_suffix_read`**

In `try_lookup_id`, replace lines 633-654 (the raw R2 suffix read) with:

```rust
        // Step 1: Suffix read to get footer + file size (cached at edge for 1hr).
        const FOOTER_SUFFIX_SIZE: u64 = 32768;
        let (file_size, tail_bytes) = match self
            .cached_suffix_read(&shard_key, FOOTER_SUFFIX_SIZE)
            .await?
        {
            Some(result) => result,
            None => return Ok(None),
        };
        let tail_len = tail_bytes.len() as u64;
        let tail_offset = file_size - tail_len;
```

Everything after this (footer parsing, row group selection, range read) stays unchanged.

- [ ] **Step 3: Run tests and clippy**

```bash
cd crates && cargo test
cd crates && cargo clippy --all-targets
```

- [ ] **Step 4: Commit**

```bash
git add crates/geocoder-worker/src/stac.rs
git commit -m "Cache parquet footer suffix at the edge to reduce R2 reads"
```

---

### Task 4: Verify

- [ ] **Step 1: Run full workspace checks**

```bash
cd crates && cargo fmt --all --check
cd crates && cargo clippy --all-targets
cd crates && cargo test
```

- [ ] **Step 2: Verify grep for retry pattern in build worker**

```bash
grep -n "_retry_transient" scripts/build_id_index.py
```

Expected: the new call in `_worker_build_r2_batch` alongside existing ones.

- [ ] **Step 3: Verify grep for cached_suffix_read**

```bash
grep -n "cached_suffix_read" crates/geocoder-worker/src/stac.rs
```

Expected: method definition + one call site in `try_lookup_id`.
