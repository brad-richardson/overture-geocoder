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

use bytes::Bytes;
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
}

/// Bounded range reader bound to one immutable R2 object key.
pub(crate) struct RangeReader<'a> {
    loader: &'a ShardLoader,
    key: String,
    metrics: RangeReadMetrics,
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
        self.metrics.logical_ranges = self.metrics.logical_ranges.saturating_add(1);
        self.metrics.planned_physical_ranges =
            self.metrics.planned_physical_ranges.saturating_add(1);
        let read = self
            .loader
            .cached_range_read_measured(&self.key, offset, length)
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
        let plan = coalesce_ranges(wants, gap_threshold, max_range_len)
            .map_err(|error| Error::RustError(format!("Range coalescing failed: {error}")))?;
        self.metrics.logical_ranges = self.metrics.logical_ranges.saturating_add(wants.len());
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
                    .cached_range_read_measured(key, offset, length)
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
