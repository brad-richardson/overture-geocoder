//! Thin, payload-agnostic bounded range fetch component.
//!
//! It wraps the `ShardLoader` edge-cached R2 helpers (`cached_bounded_prefix_read`
//! and `cached_range_read`) with the shared [`geocoder_core::pages`] coalescing
//! planner, so a caller expresses *what byte spans of one object it needs* and
//! this component decides *how many physical range reads to issue* and slices
//! each want back out.
//!
//! Only the experimental address spike consumes it today, so it is gated behind
//! `address-spike`. It has no address-specific logic: when the Places compact
//! shard prototype lands it becomes the second consumer and the gate drops.

use bytes::Bytes;
use geocoder_core::pages::{coalesce_ranges, ByteRange};
use serde::Serialize;
use worker::*;

use crate::stac::{not_found, ShardLoader};

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

    pub(crate) fn add(self, other: Self) -> Self {
        Self {
            logical_ranges: self.logical_ranges.saturating_add(other.logical_ranges),
            planned_physical_ranges: self
                .planned_physical_ranges
                .saturating_add(other.planned_physical_ranges),
            cache_hits: self.cache_hits.saturating_add(other.cache_hits),
            r2_reads: self.r2_reads.saturating_add(other.r2_reads),
            bytes_fetched: self.bytes_fetched.saturating_add(other.bytes_fetched),
            cache_bytes: self.cache_bytes.saturating_add(other.cache_bytes),
            r2_bytes: self.r2_bytes.saturating_add(other.r2_bytes),
        }
    }

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
    #[cfg(feature = "address-spike")]
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

    /// Read one exact byte range, edge-cached.
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
        let mut slices: Vec<Option<Bytes>> = vec![None; wants.len()];
        for read in &plan.reads {
            let fetched = self
                .loader
                .cached_range_read_measured(&self.key, read.offset, read.length)
                .await?
                .ok_or_else(|| not_found(&self.key))?;
            self.metrics.observe(fetched.bytes.len(), fetched.cache_hit);
            if fetched.bytes.len() as u64 != read.length {
                return Err(Error::RustError(
                    "Coalesced range returned fewer bytes than requested".into(),
                ));
            }
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
