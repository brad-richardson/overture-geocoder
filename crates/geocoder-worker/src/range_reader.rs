//! Thin, payload-agnostic bounded range fetch component.
//!
//! It wraps the `ShardLoader` edge-cached R2 helpers (`cached_bounded_prefix_read`
//! and `cached_range_read`) with the shared [`geocoder_core::pages`] coalescing
//! planner, so a caller expresses *what byte spans of one object it needs* and
//! this component decides *how many physical range reads to issue* and slices
//! each want back out.
//!
//! It has no payload-specific logic: the unified v2 address and compact Places
//! readers are both consumers.

use bytes::{Bytes, BytesMut};
use futures::stream::{self, StreamExt, TryStreamExt};
use geocoder_core::pages::{coalesce_ranges, ByteRange};
use serde::Serialize;
use worker::*;

use crate::stac::{not_found, ShardLoader};

/// Maximum physical range reads issued concurrently within one `coalesced`
/// call. The plan is unchanged and deterministic; only the await order (and so
/// wall time) differs. Metrics are accumulated in plan order after all reads
/// resolve, so logical/physical read and byte counts are identical to a
/// sequential fetch.
const MAX_INFLIGHT_READS: usize = 4;

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
pub(crate) struct RangeReadMetrics {
    pub logical_ranges: usize,
    pub planned_physical_ranges: usize,
    pub cache_hits: usize,
    pub r2_reads: usize,
    pub bytes_fetched: usize,
    pub cache_bytes: usize,
    pub r2_bytes: usize,
}

impl RangeReadMetrics {
    fn observe(&mut self, bytes: usize, cache_hit: bool) {
        self.bytes_fetched = self.bytes_fetched.saturating_add(bytes);
        if cache_hit {
            self.cache_hits = self.cache_hits.saturating_add(1);
            self.cache_bytes = self.cache_bytes.saturating_add(bytes);
        } else {
            self.r2_reads = self.r2_reads.saturating_add(1);
            self.r2_bytes = self.r2_bytes.saturating_add(bytes);
        }
    }

    /// Difference between two metric snapshots. Only the Places multi-stage
    /// reader attributes per-stage byte/read deltas, so this is compiled with
    /// that feature.
    pub(crate) fn since(self, earlier: Self) -> Self {
        Self {
            logical_ranges: self.logical_ranges.saturating_sub(earlier.logical_ranges),
            planned_physical_ranges: self
                .planned_physical_ranges
                .saturating_sub(earlier.planned_physical_ranges),
            cache_hits: self.cache_hits.saturating_sub(earlier.cache_hits),
            r2_reads: self.r2_reads.saturating_sub(earlier.r2_reads),
            bytes_fetched: self.bytes_fetched.saturating_sub(earlier.bytes_fetched),
            cache_bytes: self.cache_bytes.saturating_sub(earlier.cache_bytes),
            r2_bytes: self.r2_bytes.saturating_sub(earlier.r2_bytes),
        }
    }

    pub(crate) fn accumulate(&mut self, other: Self) {
        self.logical_ranges = self.logical_ranges.saturating_add(other.logical_ranges);
        self.planned_physical_ranges = self
            .planned_physical_ranges
            .saturating_add(other.planned_physical_ranges);
        self.cache_hits = self.cache_hits.saturating_add(other.cache_hits);
        self.r2_reads = self.r2_reads.saturating_add(other.r2_reads);
        self.bytes_fetched = self.bytes_fetched.saturating_add(other.bytes_fetched);
        self.cache_bytes = self.cache_bytes.saturating_add(other.cache_bytes);
        self.r2_bytes = self.r2_bytes.saturating_add(other.r2_bytes);
    }
}

/// Bounded range reader bound to one immutable R2 object key.
pub(crate) struct RangeReader<'a> {
    loader: &'a ShardLoader,
    key: String,
    metrics: RangeReadMetrics,
}

fn chunk_ranges(
    wants: &[ByteRange],
    max_range_len: u64,
) -> std::result::Result<(Vec<ByteRange>, Vec<usize>), String> {
    if max_range_len == 0 {
        return Err("Coalesced range maximum must be positive".into());
    }
    let mut chunks = Vec::new();
    let mut chunks_per_want = Vec::with_capacity(wants.len());
    for want in wants {
        if want.length == 0 {
            return Err("Coalesced range length must be positive".into());
        }
        let mut offset = want.offset;
        let mut remaining = want.length;
        let mut count = 0_usize;
        while remaining > 0 {
            let length = remaining.min(max_range_len);
            chunks.push(ByteRange { offset, length });
            offset = offset
                .checked_add(length)
                .ok_or_else(|| "Coalesced range extent overflows".to_string())?;
            remaining -= length;
            count = count
                .checked_add(1)
                .ok_or_else(|| "Coalesced range chunk count overflows".to_string())?;
        }
        chunks_per_want.push(count);
    }
    Ok((chunks, chunks_per_want))
}

fn reassemble_chunks(
    chunks: Vec<Bytes>,
    chunks_per_want: &[usize],
    wants: &[ByteRange],
) -> std::result::Result<Vec<Bytes>, String> {
    if chunks_per_want.len() != wants.len() {
        return Err("Coalesced range chunk map differs from wants".into());
    }
    let mut chunks = chunks.into_iter();
    let mut assembled = Vec::with_capacity(wants.len());
    for (want, count) in wants.iter().zip(chunks_per_want) {
        if *count == 1 {
            let chunk = chunks
                .next()
                .ok_or_else(|| "Coalesced plan missed a chunk".to_string())?;
            if chunk.len() as u64 != want.length {
                return Err("Coalesced chunk length differs from requested extent".into());
            }
            assembled.push(chunk);
            continue;
        }
        let capacity = usize::try_from(want.length)
            .map_err(|_| "Coalesced range length exceeds platform bounds".to_string())?;
        let mut value = BytesMut::with_capacity(capacity);
        for _ in 0..*count {
            value.extend_from_slice(
                &chunks
                    .next()
                    .ok_or_else(|| "Coalesced plan missed a chunk".to_string())?,
            );
        }
        if value.len() != capacity {
            return Err("Coalesced chunks differ from requested extent".into());
        }
        assembled.push(value.freeze());
    }
    if chunks.next().is_some() {
        return Err("Coalesced plan returned an unclaimed chunk".into());
    }
    Ok(assembled)
}

impl<'a> RangeReader<'a> {
    /// Bind a reader to `key`.
    pub(crate) fn new(loader: &'a ShardLoader, key: impl Into<String>) -> Self {
        Self {
            loader,
            key: key.into(),
            metrics: RangeReadMetrics::default(),
        }
    }

    pub(crate) fn metrics(&self) -> RangeReadMetrics {
        self.metrics
    }

    /// Read at most `max_bytes` from the start of the object, failing closed if
    /// the object overflows the cap (see `cached_bounded_prefix_read`).
    pub(crate) async fn bounded_prefix(
        &mut self,
        max_bytes: usize,
        ttl: u64,
    ) -> Result<Option<Bytes>> {
        self.metrics.logical_ranges = self.metrics.logical_ranges.saturating_add(1);
        self.metrics.planned_physical_ranges =
            self.metrics.planned_physical_ranges.saturating_add(1);
        let read = self
            .loader
            .cached_bounded_prefix_read_measured(&self.key, max_bytes, ttl)
            .await?;
        if let Some(read) = read {
            self.metrics.observe(read.bytes.len(), read.cache_hit);
            Ok(Some(read.bytes))
        } else {
            Ok(None)
        }
    }

    /// Read at most `max_bytes` from the object start. Short-at-EOF is valid;
    /// this is for envelope prefixes, not exact component extents.
    pub(crate) async fn at_most_prefix(
        &mut self,
        max_bytes: usize,
        ttl: u64,
    ) -> Result<Option<Bytes>> {
        self.metrics.logical_ranges = self.metrics.logical_ranges.saturating_add(1);
        self.metrics.planned_physical_ranges =
            self.metrics.planned_physical_ranges.saturating_add(1);
        let read = self
            .loader
            .cached_at_most_prefix_read_measured(&self.key, max_bytes, ttl)
            .await?;
        if let Some(read) = read {
            self.metrics.observe(read.bytes.len(), read.cache_hit);
            Ok(Some(read.bytes))
        } else {
            Ok(None)
        }
    }

    /// Read one exact byte range, edge-cached. Only the Places multi-stage
    /// reader issues single exact ranges directly; the address path plans its
    /// page reads through [`RangeReader::coalesced`].
    pub(crate) async fn range(&mut self, offset: u64, length: u64) -> Result<Option<Bytes>> {
        self.range_with_ttl(offset, length, crate::stac::cache::ID_INDEX_CACHE_TTL)
            .await
    }

    pub(crate) async fn range_with_ttl(
        &mut self,
        offset: u64,
        length: u64,
        ttl: u64,
    ) -> Result<Option<Bytes>> {
        self.metrics.logical_ranges = self.metrics.logical_ranges.saturating_add(1);
        self.metrics.planned_physical_ranges =
            self.metrics.planned_physical_ranges.saturating_add(1);
        let read = self
            .loader
            .cached_range_read_measured_with_ttl(&self.key, offset, length, ttl)
            .await?;
        if let Some(read) = read {
            self.metrics.observe(read.bytes.len(), read.cache_hit);
            if read.bytes.len() as u64 != length {
                return Err(Error::RustError(
                    "Exact range length differs from requested extent".into(),
                ));
            }
            Ok(Some(read.bytes))
        } else {
            Ok(None)
        }
    }

    /// Fetch every want in `wants`, coalescing adjacent/overlapping spans into
    /// as few physical reads as the `gap_threshold` / `max_range_len` budget
    /// allows, and return one [`Bytes`] view per want (in the caller's original
    /// order). A missing object fails with the STAC not-found sentinel so the
    /// version-fallback path still triggers; a short physical read fails closed.
    pub(crate) async fn coalesced(
        &mut self,
        wants: &[ByteRange],
        gap_threshold: u64,
        max_range_len: u64,
    ) -> Result<Vec<Bytes>> {
        self.coalesced_with_ttl(
            wants,
            gap_threshold,
            max_range_len,
            crate::stac::cache::ID_INDEX_CACHE_TTL,
        )
        .await
    }

    pub(crate) async fn coalesced_with_ttl(
        &mut self,
        wants: &[ByteRange],
        gap_threshold: u64,
        max_range_len: u64,
        ttl: u64,
    ) -> Result<Vec<Bytes>> {
        self.fetch_coalesced(wants, gap_threshold, max_range_len, ttl, wants.len())
            .await
    }

    /// Like [`RangeReader::coalesced_with_ttl`], but explicitly permits a
    /// logical want to exceed the physical range-read cap. Oversized wants are
    /// split for transport and reassembled before returning. Callers must
    /// enforce a separate whole-payload budget before using this method.
    pub(crate) async fn coalesced_chunked_with_ttl(
        &mut self,
        wants: &[ByteRange],
        gap_threshold: u64,
        max_range_len: u64,
        ttl: u64,
    ) -> Result<Vec<Bytes>> {
        // A logical payload extent can be larger than the physical range-read
        // cap (for example, a highly skewed spatial leaf). Split such wants
        // before planning, then reassemble them under the caller's existing
        // whole-payload budget. This preserves the hard physical-read bound
        // without making the on-disk format reject otherwise valid shards.
        let (chunked_wants, chunks_per_want) =
            chunk_ranges(wants, max_range_len).map_err(Error::RustError)?;
        let chunks = self
            .fetch_coalesced(
                &chunked_wants,
                gap_threshold,
                max_range_len,
                ttl,
                wants.len(),
            )
            .await?;
        reassemble_chunks(chunks, &chunks_per_want, wants).map_err(Error::RustError)
    }

    async fn fetch_coalesced(
        &mut self,
        wants: &[ByteRange],
        gap_threshold: u64,
        max_range_len: u64,
        ttl: u64,
        logical_ranges: usize,
    ) -> Result<Vec<Bytes>> {
        let plan = coalesce_ranges(wants, gap_threshold, max_range_len)
            .map_err(|error| Error::RustError(format!("Range coalescing failed: {error}")))?;
        self.metrics.logical_ranges = self.metrics.logical_ranges.saturating_add(logical_ranges);
        self.metrics.planned_physical_ranges = self
            .metrics
            .planned_physical_ranges
            .saturating_add(plan.reads.len());
        // Issue the plan's physical reads with bounded concurrency. `buffered`
        // preserves input (plan) order in its output, so the fetched bytes zip
        // back to `plan.reads` deterministically regardless of completion order.
        // The key/loader are borrowed immutably by every read; metrics (a
        // mutable borrow of self) are folded in afterwards, in plan order.
        let loader = self.loader;
        let key = self.key.as_str();
        let fetched: Vec<_> = stream::iter(plan.reads.iter().map(|read| {
            let (offset, length) = (read.offset, read.length);
            async move {
                let read_bytes = loader
                    .cached_range_read_measured_with_ttl(key, offset, length, ttl)
                    .await?
                    .ok_or_else(|| not_found(key))?;
                if read_bytes.bytes.len() as u64 != length {
                    return Err(Error::RustError(
                        "Coalesced range returned fewer bytes than requested".into(),
                    ));
                }
                Ok::<_, Error>(read_bytes)
            }
        }))
        .buffered(MAX_INFLIGHT_READS)
        .try_collect()
        .await?;

        let mut slices: Vec<Option<Bytes>> = vec![None; wants.len()];
        for (read, fetched) in plan.reads.iter().zip(fetched.iter()) {
            self.metrics.observe(fetched.bytes.len(), fetched.cache_hit);
            for want in &read.wants {
                let start = usize::try_from(want.relative_offset).map_err(|_| {
                    Error::RustError("Coalesced range offset exceeds platform bounds".into())
                })?;
                let length = usize::try_from(want.length).map_err(|_| {
                    Error::RustError("Coalesced range length exceeds platform bounds".into())
                })?;
                let end = start.checked_add(length).ok_or_else(|| {
                    Error::RustError("Coalesced range slice extent overflows".into())
                })?;
                if end > fetched.bytes.len() {
                    return Err(Error::RustError(
                        "Coalesced range slice exceeds fetched bytes".into(),
                    ));
                }
                slices[want.want_index] = Some(fetched.bytes.slice(start..end));
            }
        }
        slices
            .into_iter()
            .map(|slot| slot.ok_or_else(|| Error::RustError("Coalesced plan missed a want".into())))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn oversized_logical_ranges_split_to_the_physical_cap() {
        let wants = [
            ByteRange {
                offset: 100,
                length: 5_000_000,
            },
            ByteRange {
                offset: 8_000_000,
                length: 7,
            },
        ];
        let (chunks, counts) = chunk_ranges(&wants, 2_000_000).unwrap();
        assert_eq!(counts, vec![3, 1]);
        assert_eq!(
            chunks,
            vec![
                ByteRange {
                    offset: 100,
                    length: 2_000_000,
                },
                ByteRange {
                    offset: 2_000_100,
                    length: 2_000_000,
                },
                ByteRange {
                    offset: 4_000_100,
                    length: 1_000_000,
                },
                ByteRange {
                    offset: 8_000_000,
                    length: 7,
                },
            ]
        );
    }

    #[test]
    fn split_ranges_reassemble_to_the_original_logical_wants() {
        let wants = [ByteRange {
            offset: 0,
            length: 6,
        }];
        let chunks = vec![
            Bytes::from_static(b"ab"),
            Bytes::from_static(b"cd"),
            Bytes::from_static(b"ef"),
        ];
        assert_eq!(
            reassemble_chunks(chunks, &[3], &wants).unwrap()[0],
            Bytes::from_static(b"abcdef")
        );
    }
}
