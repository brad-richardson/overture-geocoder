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
use worker::*;

use crate::stac::{not_found, ShardLoader};

/// Bounded range reader bound to one immutable R2 object key.
pub(crate) struct RangeReader<'a> {
    loader: &'a ShardLoader,
    key: String,
}

impl<'a> RangeReader<'a> {
    /// Bind a reader to `key`.
    pub(crate) fn new(loader: &'a ShardLoader, key: impl Into<String>) -> Self {
        Self {
            loader,
            key: key.into(),
        }
    }

    /// Read at most `max_bytes` from the start of the object, failing closed if
    /// the object overflows the cap (see `cached_bounded_prefix_read`).
    pub(crate) async fn bounded_prefix(&self, max_bytes: usize, ttl: u64) -> Result<Option<Bytes>> {
        self.loader
            .cached_bounded_prefix_read(&self.key, max_bytes, ttl)
            .await
    }

    /// Read one exact byte range, edge-cached.
    pub(crate) async fn range(&self, offset: u64, length: u64) -> Result<Option<Bytes>> {
        self.loader
            .cached_range_read(&self.key, offset, length)
            .await
    }

    /// Fetch every want in `wants`, coalescing adjacent/overlapping spans into
    /// as few physical reads as the `gap_threshold` / `max_range_len` budget
    /// allows, and return one [`Bytes`] view per want (in the caller's original
    /// order). A missing object fails with the STAC not-found sentinel so the
    /// version-fallback path still triggers; a short physical read fails closed.
    pub(crate) async fn coalesced(
        &self,
        wants: &[ByteRange],
        gap_threshold: u64,
        max_range_len: u64,
    ) -> Result<Vec<Bytes>> {
        let plan = coalesce_ranges(wants, gap_threshold, max_range_len)
            .map_err(|error| Error::RustError(format!("Range coalescing failed: {error}")))?;
        let mut slices: Vec<Option<Bytes>> = vec![None; wants.len()];
        for read in &plan.reads {
            let fetched = self
                .loader
                .cached_range_read(&self.key, read.offset, read.length)
                .await?
                .ok_or_else(|| not_found(&self.key))?;
            if fetched.len() as u64 != read.length {
                return Err(Error::RustError(
                    "Coalesced range returned fewer bytes than requested".into(),
                ));
            }
            for want in &read.wants {
                let start = want.relative_offset as usize;
                let end = start + want.length as usize;
                slices[want.want_index] = Some(fetched.slice(start..end));
            }
        }
        slices
            .into_iter()
            .map(|slot| slot.ok_or_else(|| Error::RustError("Coalesced plan missed a want".into())))
            .collect()
    }
}
