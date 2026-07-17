//! Payload-agnostic primitives for the shared range-readable binary family.
//!
//! Addresses (lookup-safe gzip pages + self-describing extensions) and, later,
//! Places (compact spatial shards) share one Worker range-reader core:
//! bounded caps validation, page/extension framing, a generic side index, and
//! a range-coalescing planner. This module owns the *pure* half of that core so
//! the same framing/index/coalescing logic is used by the production Worker,
//! the CLI, and any offline evaluation harness without a Cloudflare dependency.
//! Format-specific record payloads (the address record decoder, the Places
//! record decoder) live behind these primitives.
//!
//! The wire contracts mirror the Python producers exactly so a cross-language
//! fixture can pin them (`scripts/experiment_address_compression.py` for the
//! page/index framing, `scripts/experiment_address_format_convergence.py` for
//! the self-describing extended page and its division extension). See
//! `tests/fixtures/pages/` and the tests at the bottom of this module.

use serde::Serialize;

/// Little-endian `u32` length prefix that frames every stored page inside a
/// data object (`[len: u32 LE][payload]`). Shared by both the address gzip
/// pages and the Places compact-shard pages.
pub const STORED_LEN_PREFIX: usize = 4;

/// A parse/validation failure. Deliberately opaque prose (no payload data) so
/// error strings never leak record content into logs or responses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PageError(String);

impl PageError {
    /// Build an error from a fixed message. Callers pass string literals only.
    pub fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

impl std::fmt::Display for PageError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for PageError {}

/// Result specialized to [`PageError`].
pub type PageResult<T> = std::result::Result<T, PageError>;

/// Hard decode budgets for one binary family. Every reader validates against a
/// preset before allocating, so a malformed object cannot exhaust the Worker
/// heap. [`PageCaps::ADDRESS`] captures the measured address-spike values; a
/// future Places preset lives beside it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PageCaps {
    /// Largest side-index object accepted whole.
    pub max_index_bytes: usize,
    /// Largest number of index entries.
    pub max_index_entries: usize,
    /// Largest encoded first-key (whole 8-field key blob for addresses).
    pub max_key_bytes: usize,
    /// Largest stored (compressed) page payload, excluding the length prefix.
    pub max_stored_page_bytes: usize,
    /// Largest decoded (decompressed) page payload.
    pub max_decoded_page_bytes: usize,
    /// Largest row count in a single page or index entry.
    pub max_page_rows: usize,
    /// Heap-amplification budget: the largest materialized response a single
    /// page may inflate to after dictionary references are cloned.
    pub max_materialized_bytes: usize,
    /// Largest page dictionary (distinct strings).
    pub max_dictionary_strings: usize,
    /// Largest single dictionary string / key field.
    pub max_dictionary_string_bytes: usize,
}

impl PageCaps {
    /// The measured address-spike preset (see `address_pages.rs` history).
    pub const ADDRESS: PageCaps = PageCaps {
        max_index_bytes: 4 * 1024 * 1024,
        max_index_entries: 65_536,
        max_key_bytes: 64 * 1024,
        max_stored_page_bytes: 256 * 1024,
        max_decoded_page_bytes: 1024 * 1024,
        max_page_rows: 10_000,
        max_materialized_bytes: 8 * 1024 * 1024,
        max_dictionary_strings: 100_000,
        max_dictionary_string_bytes: 64 * 1024,
    };
}

/// A forward-only cursor over a byte slice with the varint/text/front-coding
/// primitives every payload in the family shares. All reads are bounds-checked
/// and fail closed; the cursor never panics on hostile input.
pub struct ByteReader<'a> {
    bytes: &'a [u8],
    position: usize,
}

impl<'a> ByteReader<'a> {
    /// Start a reader at `position` within `bytes`.
    pub fn new(bytes: &'a [u8], position: usize) -> Self {
        Self { bytes, position }
    }

    /// Whether the cursor has consumed every byte.
    pub fn is_empty(&self) -> bool {
        self.position >= self.bytes.len()
    }

    /// The current absolute offset into the backing slice.
    pub fn position(&self) -> usize {
        self.position
    }

    /// Consume `length` bytes, failing on truncation or overflow.
    pub fn take(&mut self, length: usize) -> PageResult<&'a [u8]> {
        let end = self
            .position
            .checked_add(length)
            .ok_or_else(|| PageError::new("page payload extent overflows"))?;
        if end > self.bytes.len() {
            return Err(PageError::new("truncated page payload"));
        }
        let result = &self.bytes[self.position..end];
        self.position = end;
        Ok(result)
    }

    /// Read an unsigned LEB128 varint (matches the Python `decode_uvarint`).
    pub fn uvarint(&mut self) -> PageResult<u64> {
        let mut value = 0_u64;
        for shift in (0..=63).step_by(7) {
            let byte = *self.take(1)?.first().expect("one-byte slice");
            if shift == 63 && byte > 1 {
                return Err(PageError::new("page varint overflows"));
            }
            value |= u64::from(byte & 0x7f) << shift;
            if byte & 0x80 == 0 {
                return Ok(value);
            }
        }
        Err(PageError::new("invalid page varint"))
    }

    /// Read a `uvarint(len) + bytes` UTF-8 string bounded by `max_bytes`.
    pub fn text(&mut self, max_bytes: usize) -> PageResult<String> {
        let length = usize::try_from(self.uvarint()?)
            .map_err(|_| PageError::new("page text is too large"))?;
        if length > max_bytes {
            return Err(PageError::new("page text exceeds hard byte cap"));
        }
        String::from_utf8(self.take(length)?.to_vec())
            .map_err(|_| PageError::new("page text is not UTF-8"))
    }

    /// Read a little-endian `i32`.
    pub fn i32_le(&mut self) -> PageResult<i32> {
        Ok(i32::from_le_bytes(
            self.take(4)?.try_into().expect("four-byte slice"),
        ))
    }

    /// Apply one front-coded field: `uvarint(shared_prefix) uvarint(suffix_len)
    /// suffix`. `previous` holds the prior field value and is updated in place
    /// to the decoded bytes, so a caller decoding an N-field key keeps one
    /// `Vec<u8>` per field position.
    pub fn apply_front_coding(
        &mut self,
        previous: &mut Vec<u8>,
        max_suffix: usize,
    ) -> PageResult<()> {
        let prefix = usize::try_from(self.uvarint()?)
            .map_err(|_| PageError::new("front-code prefix is too large"))?;
        let suffix_len = usize::try_from(self.uvarint()?)
            .map_err(|_| PageError::new("front-code suffix is too large"))?;
        if prefix > previous.len() || suffix_len > max_suffix {
            return Err(PageError::new("front-coded field is outside hard bounds"));
        }
        let suffix = self.take(suffix_len)?;
        previous.truncate(prefix);
        previous.extend_from_slice(suffix);
        Ok(())
    }
}

/// Format the canonical lowercase, hyphenated UUID string for 16 raw bytes,
/// byte-for-byte identical to Python's `str(uuid.UUID(bytes=...))`.
pub fn format_uuid(bytes: [u8; 16]) -> String {
    format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
        bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]
    )
}

/// Validate the `[len: u32 LE][payload]` frame of a stored page against `caps`
/// and return the inner payload (still compressed for gzip families). The
/// declared length must match the range exactly, so a short or padded range
/// read fails closed before any decompression is attempted.
pub fn strip_stored_page_frame<'a>(bytes: &'a [u8], caps: &PageCaps) -> PageResult<&'a [u8]> {
    if bytes.len() < STORED_LEN_PREFIX
        || bytes.len() > caps.max_stored_page_bytes + STORED_LEN_PREFIX
    {
        return Err(PageError::new("stored page is outside hard bounds"));
    }
    let declared = u32::from_le_bytes(
        bytes[..STORED_LEN_PREFIX]
            .try_into()
            .expect("four-byte slice"),
    ) as usize;
    if declared != bytes.len() - STORED_LEN_PREFIX {
        return Err(PageError::new("stored page length differs from range"));
    }
    Ok(&bytes[STORED_LEN_PREFIX..])
}

/// Byte extent and row count of one page in a data object.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PageExtent {
    /// Absolute offset of the framed page in the data object.
    pub offset: u64,
    /// Framed page length, including the [`STORED_LEN_PREFIX`].
    pub length: u64,
    /// Exact row count the page must decode to.
    pub rows: usize,
}

/// A parsed side index: the sorted first-key -> [`PageExtent`] directory that
/// selects exactly one page for a lookup key. Generic over the key type `K`,
/// with the ordering/monotonicity invariants enforced at parse time regardless
/// of how a payload encodes its keys.
#[derive(Debug, Clone)]
pub struct PageIndex<K> {
    entries: Vec<(K, PageExtent)>,
}

impl<K: Ord + Clone> PageIndex<K> {
    /// Parse an index object: `MAGIC` then a sequence of
    /// `uvarint(offset) uvarint(length) uvarint(rows) uvarint(key_len) key`
    /// entries. `parse_key` turns the raw first-key bytes into the comparable
    /// key `K`. Enforces, generically, every invariant the address reader
    /// required: whole-object and per-field caps, `rows` and `length` within
    /// bounds, page extents non-overlapping and offset-monotonic, and
    /// strictly increasing first-keys.
    pub fn parse<F>(
        bytes: &[u8],
        caps: &PageCaps,
        magic: &[u8],
        mut parse_key: F,
    ) -> PageResult<Self>
    where
        F: FnMut(&[u8]) -> PageResult<K>,
    {
        if bytes.len() > caps.max_index_bytes {
            return Err(PageError::new("page index exceeds hard byte cap"));
        }
        if !bytes.starts_with(magic) {
            return Err(PageError::new("invalid page index magic"));
        }
        let mut reader = ByteReader::new(bytes, magic.len());
        let mut entries: Vec<(K, PageExtent)> = Vec::new();
        let mut previous_key: Option<K> = None;
        let mut previous_end = 0_u64;
        while !reader.is_empty() {
            if entries.len() >= caps.max_index_entries {
                return Err(PageError::new("page index entry cap exceeded"));
            }
            let offset = reader.uvarint()?;
            let length = reader.uvarint()?;
            let rows = usize::try_from(reader.uvarint()?)
                .map_err(|_| PageError::new("page row count is too large"))?;
            let key_len = usize::try_from(reader.uvarint()?)
                .map_err(|_| PageError::new("page index key is too large"))?;
            if key_len > caps.max_key_bytes {
                return Err(PageError::new("page index key exceeds hard byte cap"));
            }
            let key = parse_key(reader.take(key_len)?)?;
            if rows == 0
                || rows > caps.max_page_rows
                || length <= STORED_LEN_PREFIX as u64
                || length > (caps.max_stored_page_bytes + STORED_LEN_PREFIX) as u64
            {
                return Err(PageError::new("page index extent is outside hard bounds"));
            }
            let end = offset
                .checked_add(length)
                .ok_or_else(|| PageError::new("page index extent overflows"))?;
            if offset < previous_end {
                return Err(PageError::new("page index extents overlap"));
            }
            if previous_key.as_ref().is_some_and(|old| key <= *old) {
                return Err(PageError::new(
                    "page index keys are not strictly increasing",
                ));
            }
            previous_key = Some(key.clone());
            previous_end = end;
            entries.push((
                key,
                PageExtent {
                    offset,
                    length,
                    rows,
                },
            ));
        }
        if entries.is_empty() {
            return Err(PageError::new("page index is empty"));
        }
        Ok(Self { entries })
    }

    /// The page whose first-key is the greatest key `<=` the lookup key, or
    /// `None` when the lookup key sorts before the first page.
    pub fn find(&self, key: &K) -> Option<&PageExtent> {
        let position = self
            .entries
            .partition_point(|(entry_key, _)| entry_key <= key);
        position.checked_sub(1).map(|index| &self.entries[index].1)
    }

    /// Number of index entries.
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    /// Whether the index has no entries (never true for a parsed index).
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// Iterate the `(first_key, extent)` entries in stored (sorted) order.
    pub fn entries(&self) -> impl Iterator<Item = (&K, &PageExtent)> {
        self.entries.iter().map(|(key, extent)| (key, extent))
    }
}

/// One decoded row of the self-describing division extension: the containing
/// region/county/locality GERS IDs plus the match method/confidence nibbles.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DivisionExtensionRow {
    /// Containing-division GERS UUIDs (canonical lowercase string form),
    /// resolved from the page-local dictionary in stored order.
    pub division_gers_ids: Vec<String>,
    /// Match-method code (high nibble of the provenance byte).
    pub match_method: u8,
    /// Coarse confidence bucket (low nibble of the provenance byte).
    pub match_confidence: u8,
}

/// Hard limits for a page-local reference extension. The shared decoder uses
/// these before allocating or cloning identifiers; payload families choose a
/// preset that reflects their measured row and heap envelope.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReferenceExtensionCaps {
    pub max_rows: usize,
    pub max_dictionary_entries: usize,
    pub max_references_per_row: usize,
    pub max_total_references: usize,
}

/// Generic decoded row for a dictionary-backed extension. The trailing tag is
/// deliberately uninterpreted here (division uses it for two nibbles; another
/// payload can assign different semantics without forking the framing logic).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReferenceExtensionRow<I> {
    pub references: Vec<I>,
    pub tag: u8,
}

/// Measured address-division extension limits. A normal row has only a small
/// region/county/locality chain; the looser caps tolerate unusual hierarchies
/// while keeping hostile dictionaries and reference amplification bounded.
pub const DIVISION_EXTENSION_CAPS: ReferenceExtensionCaps = ReferenceExtensionCaps {
    max_rows: PageCaps::ADDRESS.max_page_rows,
    max_dictionary_entries: 30_000,
    max_references_per_row: 64,
    max_total_references: 100_000,
};

/// Split a decompressed extended page into its `(core, extension)` byte halves
/// using only the bytes: `uvarint(core_length)` locates the boundary between
/// the reused lookup-safe core page and the appended extension. Rejects a core
/// length that runs past the buffer. Mirrors the first half of the Python
/// `decode_extended_page`.
pub fn split_extended_page(payload: &[u8]) -> PageResult<(&[u8], &[u8])> {
    let mut reader = ByteReader::new(payload, 0);
    let core_length = usize::try_from(reader.uvarint()?)
        .map_err(|_| PageError::new("extended-page core length is too large"))?;
    let start = reader.position();
    let end = start
        .checked_add(core_length)
        .ok_or_else(|| PageError::new("extended-page core length overflows"))?;
    if end > payload.len() {
        return Err(PageError::new("truncated extended-page core"));
    }
    Ok((&payload[start..end], &payload[end..]))
}

/// Decode a page-local dictionary followed by per-row references and one raw
/// tag byte. `decode_identifier` owns only the identifier representation; all
/// counts, references, and amplification limits stay in this shared reader.
pub fn decode_reference_extension<I, F>(
    payload: &[u8],
    count: usize,
    caps: &ReferenceExtensionCaps,
    mut decode_identifier: F,
) -> PageResult<(Vec<ReferenceExtensionRow<I>>, usize)>
where
    I: Clone,
    F: FnMut(&mut ByteReader<'_>) -> PageResult<I>,
{
    if count > caps.max_rows {
        return Err(PageError::new("extension row count exceeds hard cap"));
    }
    let mut reader = ByteReader::new(payload, 0);
    let dictionary_count = usize::try_from(reader.uvarint()?)
        .map_err(|_| PageError::new("extension dictionary count is too large"))?;
    if dictionary_count > caps.max_dictionary_entries {
        return Err(PageError::new(
            "extension dictionary entry count exceeds hard cap",
        ));
    }
    let mut identifiers: Vec<I> = Vec::with_capacity(dictionary_count);
    for _ in 0..dictionary_count {
        identifiers.push(decode_identifier(&mut reader)?);
    }
    let mut rows: Vec<ReferenceExtensionRow<I>> = Vec::with_capacity(count);
    let mut total_references = 0_usize;
    for _ in 0..count {
        let reference_count = usize::try_from(reader.uvarint()?)
            .map_err(|_| PageError::new("extension reference count is too large"))?;
        if reference_count > caps.max_references_per_row {
            return Err(PageError::new(
                "extension row reference count exceeds hard cap",
            ));
        }
        total_references = total_references
            .checked_add(reference_count)
            .ok_or_else(|| PageError::new("extension reference count overflows"))?;
        if total_references > caps.max_total_references {
            return Err(PageError::new(
                "extension total reference count exceeds hard cap",
            ));
        }
        let mut references: Vec<I> = Vec::with_capacity(reference_count);
        for _ in 0..reference_count {
            let reference = usize::try_from(reader.uvarint()?)
                .map_err(|_| PageError::new("extension dictionary index is too large"))?;
            if reference >= identifiers.len() {
                return Err(PageError::new("extension dictionary index is out of range"));
            }
            references.push(identifiers[reference].clone());
        }
        let tag = *reader.take(1)?.first().expect("one-byte slice");
        rows.push(ReferenceExtensionRow { references, tag });
    }
    Ok((rows, reader.position()))
}

/// Address-specific wrapper around [`decode_reference_extension`].
pub fn decode_division_extension(
    payload: &[u8],
    count: usize,
) -> PageResult<(Vec<DivisionExtensionRow>, usize)> {
    let (rows, consumed) =
        decode_reference_extension(payload, count, &DIVISION_EXTENSION_CAPS, |reader| {
            let raw: [u8; 16] = reader.take(16)?.try_into().expect("sixteen-byte slice");
            Ok(format_uuid(raw))
        })?;
    Ok((
        rows.into_iter()
            .map(|row| DivisionExtensionRow {
                division_gers_ids: row.references,
                match_method: row.tag >> 4,
                match_confidence: row.tag & 0x0f,
            })
            .collect(),
        consumed,
    ))
}

/// Decode an extended page with payload-specific core and extension closures.
/// This is the reusable composition point for address, Places, and future
/// payload families.
pub fn decode_extended_page_with<T, E, F, G>(
    payload: &[u8],
    decode_core: F,
    decode_extension: G,
) -> PageResult<(T, E)>
where
    F: FnOnce(&[u8]) -> PageResult<(T, usize)>,
    G: FnOnce(&[u8], usize) -> PageResult<(E, usize)>,
{
    let (core, extension) = split_extended_page(payload)?;
    let (value, rows) = decode_core(core)?;
    let (extension_value, consumed) = decode_extension(extension, rows)?;
    if consumed != extension.len() {
        return Err(PageError::new("trailing extended-page bytes"));
    }
    Ok((value, extension_value))
}

/// Decode a self-describing extended page end to end: split the framing, let
/// `decode_core` decode the reused core page (returning its decoded value and
/// exact row count), then decode the division extension for that many rows and
/// reject any trailing bytes. Mirrors the Python `decode_extended_page`, but
/// keeps the format-specific core decode behind the `decode_core` closure so
/// this stays payload-agnostic.
pub fn decode_extended_page<T, F>(
    payload: &[u8],
    decode_core: F,
) -> PageResult<(T, Vec<DivisionExtensionRow>)>
where
    F: FnOnce(&[u8]) -> PageResult<(T, usize)>,
{
    decode_extended_page_with(payload, decode_core, decode_division_extension)
}

// ---------------------------------------------------------------------------
// Bounded range-coalescing planner.
// ---------------------------------------------------------------------------

/// A requested byte span within a single object.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ByteRange {
    /// Absolute offset in the object.
    pub offset: u64,
    /// Span length in bytes (must be non-zero).
    pub length: u64,
}

/// Where an original want lands inside its coalesced physical read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WantSlice {
    /// Index of the want in the caller's original `wants` slice.
    pub want_index: usize,
    /// Offset of the want within the coalesced read's fetched bytes.
    pub relative_offset: u64,
    /// Length of the want (equal to the original want length).
    pub length: u64,
}

/// One physical range read that satisfies one or more coalesced wants.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CoalescedRead {
    /// Absolute offset of the merged read.
    pub offset: u64,
    /// Length of the merged read.
    pub length: u64,
    /// The wants served by this read and how to slice each one out.
    pub wants: Vec<WantSlice>,
}

/// The plan produced by [`coalesce_ranges`]: the physical reads to issue and,
/// through each read's [`WantSlice`]s, the mapping back to every original want.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CoalescePlan {
    /// Physical reads in ascending offset order.
    pub reads: Vec<CoalescedRead>,
}

/// Merge sorted-by-offset byte wants into a minimal set of physical range reads.
///
/// Two wants coalesce into one read when the gap between them is at most
/// `gap_threshold` bytes *and* the resulting merged span stays within
/// `max_range_len`. Overlapping and duplicate wants always coalesce (gap 0).
/// Each returned [`CoalescedRead`] carries [`WantSlice`]s that map every
/// original want back to a `(relative_offset, length)` inside the fetched
/// bytes, so a caller reads once and slices many. This is the primitive the
/// Places multi-span reads (lexicon + postings + records) will build on.
///
/// Wants may be supplied in any order; the plan is computed over a stable
/// offset sort and `want_index` always refers to the original position.
/// Fails closed on a zero-length want, an offset+length overflow, a
/// `max_range_len` of zero, or any single want larger than `max_range_len`.
pub fn coalesce_ranges(
    wants: &[ByteRange],
    gap_threshold: u64,
    max_range_len: u64,
) -> PageResult<CoalescePlan> {
    if max_range_len == 0 {
        return Err(PageError::new("coalesce max range must be positive"));
    }
    // Validate and index the wants, then sort by (offset, end) keeping the
    // original index so the mapping survives an out-of-order caller.
    let mut ordered: Vec<(usize, u64, u64)> = Vec::with_capacity(wants.len());
    for (index, want) in wants.iter().enumerate() {
        if want.length == 0 {
            return Err(PageError::new("coalesce want has zero length"));
        }
        let end = want
            .offset
            .checked_add(want.length)
            .ok_or_else(|| PageError::new("coalesce want extent overflows"))?;
        if want.length > max_range_len {
            return Err(PageError::new("coalesce want exceeds max range budget"));
        }
        ordered.push((index, want.offset, end));
    }
    ordered.sort_by(|a, b| a.1.cmp(&b.1).then(a.2.cmp(&b.2)).then(a.0.cmp(&b.0)));

    let mut reads: Vec<CoalescedRead> = Vec::new();
    let mut current_start = 0_u64;
    let mut current_end = 0_u64;
    let mut current_wants: Vec<WantSlice> = Vec::new();
    for (index, offset, end) in ordered {
        if current_wants.is_empty() {
            current_start = offset;
            current_end = end;
            current_wants.push(WantSlice {
                want_index: index,
                relative_offset: offset - current_start,
                length: end - offset,
            });
            continue;
        }
        let gap = offset.saturating_sub(current_end);
        let merged_end = current_end.max(end);
        if gap <= gap_threshold && merged_end - current_start <= max_range_len {
            current_end = merged_end;
            current_wants.push(WantSlice {
                want_index: index,
                relative_offset: offset - current_start,
                length: end - offset,
            });
        } else {
            reads.push(CoalescedRead {
                offset: current_start,
                length: current_end - current_start,
                wants: std::mem::take(&mut current_wants),
            });
            current_start = offset;
            current_end = end;
            current_wants.push(WantSlice {
                want_index: index,
                relative_offset: offset - current_start,
                length: end - offset,
            });
        }
    }
    if !current_wants.is_empty() {
        reads.push(CoalescedRead {
            offset: current_start,
            length: current_end - current_start,
            wants: current_wants,
        });
    }
    Ok(CoalescePlan { reads })
}

#[cfg(test)]
mod tests {
    use super::*;

    // ---- ByteReader --------------------------------------------------------

    fn uvarint_bytes(mut value: u64) -> Vec<u8> {
        let mut bytes = Vec::new();
        while value >= 0x80 {
            bytes.push((value as u8 & 0x7f) | 0x80);
            value >>= 7;
        }
        bytes.push(value as u8);
        bytes
    }

    #[test]
    fn byte_reader_reads_varints_text_and_i32() {
        let mut buffer = uvarint_bytes(300);
        buffer.extend(uvarint_bytes(3));
        buffer.extend(b"abc");
        buffer.extend((-5_i32).to_le_bytes());
        let mut reader = ByteReader::new(&buffer, 0);
        assert_eq!(reader.uvarint().unwrap(), 300);
        assert_eq!(reader.text(16).unwrap(), "abc");
        assert_eq!(reader.i32_le().unwrap(), -5);
        assert!(reader.is_empty());
    }

    #[test]
    fn byte_reader_fails_closed_on_truncation_and_caps() {
        assert!(ByteReader::new(&[0x80], 0).uvarint().is_err());
        let mut buffer = uvarint_bytes(10);
        buffer.extend(b"short");
        assert!(ByteReader::new(&buffer, 0).text(64).is_err());
        buffer.truncate(1);
        // A one-byte length of 10 with no payload is truncated.
        assert!(ByteReader::new(&buffer, 0).text(64).is_err());
        let mut too_long = uvarint_bytes(5);
        too_long.extend(b"hello");
        assert!(ByteReader::new(&too_long, 0).text(4).is_err());
    }

    #[test]
    fn front_coding_round_trips_a_shared_prefix() {
        // "main" then "maine" (prefix 4, suffix "e") then "mall" (prefix 2).
        let mut buffer = uvarint_bytes(0);
        buffer.extend(uvarint_bytes(4));
        buffer.extend(b"main");
        buffer.extend(uvarint_bytes(4));
        buffer.extend(uvarint_bytes(1));
        buffer.extend(b"e");
        buffer.extend(uvarint_bytes(2));
        buffer.extend(uvarint_bytes(2));
        buffer.extend(b"ll");
        let mut reader = ByteReader::new(&buffer, 0);
        let mut previous = Vec::new();
        reader.apply_front_coding(&mut previous, 64).unwrap();
        assert_eq!(previous, b"main");
        reader.apply_front_coding(&mut previous, 64).unwrap();
        assert_eq!(previous, b"maine");
        reader.apply_front_coding(&mut previous, 64).unwrap();
        assert_eq!(previous, b"mall");
    }

    #[test]
    fn front_coding_rejects_prefix_longer_than_previous() {
        let mut buffer = uvarint_bytes(3); // prefix 3 with empty previous
        buffer.extend(uvarint_bytes(0));
        let mut reader = ByteReader::new(&buffer, 0);
        let mut previous = Vec::new();
        assert!(reader.apply_front_coding(&mut previous, 64).is_err());
    }

    // ---- stored frame ------------------------------------------------------

    #[test]
    fn stored_frame_validates_declared_length() {
        let mut framed = (3_u32).to_le_bytes().to_vec();
        framed.extend(b"abc");
        assert_eq!(
            strip_stored_page_frame(&framed, &PageCaps::ADDRESS).unwrap(),
            b"abc"
        );
        let mut wrong = (4_u32).to_le_bytes().to_vec();
        wrong.extend(b"abc");
        assert!(strip_stored_page_frame(&wrong, &PageCaps::ADDRESS).is_err());
        assert!(strip_stored_page_frame(&[0, 0], &PageCaps::ADDRESS).is_err());
    }

    // ---- generic index -----------------------------------------------------

    const TEST_MAGIC: &[u8] = b"IDX01\0";

    fn parse_string_key(bytes: &[u8]) -> PageResult<String> {
        let mut reader = ByteReader::new(bytes, 0);
        let value = reader.text(PageCaps::ADDRESS.max_dictionary_string_bytes)?;
        if !reader.is_empty() {
            return Err(PageError::new("index key has trailing bytes"));
        }
        Ok(value)
    }

    fn index_entry(offset: u64, length: u64, rows: u64, key: &str) -> Vec<u8> {
        let mut key_bytes = uvarint_bytes(key.len() as u64);
        key_bytes.extend(key.as_bytes());
        let mut bytes = uvarint_bytes(offset);
        bytes.extend(uvarint_bytes(length));
        bytes.extend(uvarint_bytes(rows));
        bytes.extend(uvarint_bytes(key_bytes.len() as u64));
        bytes.extend(key_bytes);
        bytes
    }

    fn index_fixture() -> Vec<u8> {
        let mut bytes = TEST_MAGIC.to_vec();
        bytes.extend(index_entry(100, 50, 1, "alpha"));
        bytes.extend(index_entry(200, 50, 1, "mike"));
        bytes.extend(index_entry(300, 50, 1, "zulu"));
        bytes
    }

    #[test]
    fn index_parses_and_selects_predecessor_page() {
        let index = PageIndex::parse(
            &index_fixture(),
            &PageCaps::ADDRESS,
            TEST_MAGIC,
            parse_string_key,
        )
        .unwrap();
        assert_eq!(index.len(), 3);
        assert!(index.find(&"aa".to_string()).is_none());
        assert_eq!(index.find(&"alpha".to_string()).unwrap().offset, 100);
        assert_eq!(index.find(&"mm".to_string()).unwrap().offset, 200);
        assert_eq!(index.find(&"zzz".to_string()).unwrap().offset, 300);
    }

    #[test]
    fn index_rejects_overlap_and_non_increasing_keys() {
        // Overlapping extents: second offset lands inside the first page.
        let mut overlap = TEST_MAGIC.to_vec();
        overlap.extend(index_entry(100, 50, 1, "alpha"));
        overlap.extend(index_entry(120, 50, 1, "mike"));
        assert!(
            PageIndex::parse(&overlap, &PageCaps::ADDRESS, TEST_MAGIC, parse_string_key).is_err()
        );

        // Non-increasing keys.
        let mut unsorted = TEST_MAGIC.to_vec();
        unsorted.extend(index_entry(100, 50, 1, "mike"));
        unsorted.extend(index_entry(200, 50, 1, "alpha"));
        assert!(
            PageIndex::parse(&unsorted, &PageCaps::ADDRESS, TEST_MAGIC, parse_string_key).is_err()
        );

        // Bad magic and empty index.
        assert!(
            PageIndex::parse(b"nope", &PageCaps::ADDRESS, TEST_MAGIC, parse_string_key).is_err()
        );
        assert!(
            PageIndex::parse(TEST_MAGIC, &PageCaps::ADDRESS, TEST_MAGIC, parse_string_key).is_err()
        );
    }

    #[test]
    fn index_rejects_extent_outside_bounds() {
        // rows == 0 is out of bounds.
        let mut zero_rows = TEST_MAGIC.to_vec();
        zero_rows.extend(index_entry(0, 50, 0, "alpha"));
        assert!(
            PageIndex::parse(&zero_rows, &PageCaps::ADDRESS, TEST_MAGIC, parse_string_key).is_err()
        );
        // length <= STORED_LEN_PREFIX is out of bounds.
        let mut tiny = TEST_MAGIC.to_vec();
        tiny.extend(index_entry(0, 4, 1, "alpha"));
        assert!(PageIndex::parse(&tiny, &PageCaps::ADDRESS, TEST_MAGIC, parse_string_key).is_err());
    }

    // ---- division extension ------------------------------------------------

    fn uuid_bytes(n: u128) -> [u8; 16] {
        n.to_be_bytes()
    }

    fn division_extension_fixture() -> (Vec<u8>, usize) {
        // Dictionary of three UUIDs (1, 2, 3); three rows referencing them.
        let mut bytes = uvarint_bytes(3);
        for n in 1..=3u128 {
            bytes.extend(uuid_bytes(n));
        }
        // Row 0: ids [0, 1], method 1, confidence 3.
        bytes.extend(uvarint_bytes(2));
        bytes.extend(uvarint_bytes(0));
        bytes.extend(uvarint_bytes(1));
        bytes.push((1 << 4) | 3);
        // Row 1: id [2], method 2, confidence 1.
        bytes.extend(uvarint_bytes(1));
        bytes.extend(uvarint_bytes(2));
        bytes.push((2 << 4) | 1);
        // Row 2: empty, method 0, confidence 0.
        bytes.extend(uvarint_bytes(0));
        bytes.push(0);
        (bytes, 3)
    }

    #[test]
    fn division_extension_decodes_dictionary_rows_and_match_byte() {
        let (bytes, count) = division_extension_fixture();
        let (rows, consumed) = decode_division_extension(&bytes, count).unwrap();
        assert_eq!(consumed, bytes.len());
        assert_eq!(rows.len(), 3);
        assert_eq!(
            rows[0].division_gers_ids,
            vec![format_uuid(uuid_bytes(1)), format_uuid(uuid_bytes(2))]
        );
        assert_eq!(rows[0].match_method, 1);
        assert_eq!(rows[0].match_confidence, 3);
        assert_eq!(rows[1].division_gers_ids, vec![format_uuid(uuid_bytes(3))]);
        assert_eq!(rows[1].match_method, 2);
        assert!(rows[2].division_gers_ids.is_empty());
        assert_eq!(rows[2].match_confidence, 0);
    }

    #[test]
    fn division_extension_rejects_out_of_range_index() {
        let mut bytes = uvarint_bytes(1);
        bytes.extend(uuid_bytes(1));
        bytes.extend(uvarint_bytes(1));
        bytes.extend(uvarint_bytes(5)); // dictionary only has index 0
        bytes.push(0);
        assert!(decode_division_extension(&bytes, 1).is_err());
    }

    #[test]
    fn reference_extension_enforces_dictionary_and_reference_caps() {
        let caps = ReferenceExtensionCaps {
            max_rows: 2,
            max_dictionary_entries: 1,
            max_references_per_row: 1,
            max_total_references: 1,
        };
        let too_many_dictionary_entries = uvarint_bytes(2);
        assert!(
            decode_reference_extension(&too_many_dictionary_entries, 0, &caps, |reader| Ok(
                *reader.take(1)?.first().expect("one-byte slice")
            ),)
            .is_err()
        );

        let mut too_many_references = uvarint_bytes(1);
        too_many_references.push(7);
        too_many_references.extend(uvarint_bytes(2));
        assert!(
            decode_reference_extension(&too_many_references, 1, &caps, |reader| Ok(*reader
                .take(1)?
                .first()
                .expect("one-byte slice")),)
            .is_err()
        );
    }

    #[test]
    fn extended_page_splits_core_and_extension() {
        let (extension, count) = division_extension_fixture();
        let core = b"the-core-bytes";
        let mut payload = uvarint_bytes(core.len() as u64);
        payload.extend(core);
        payload.extend(&extension);
        let (split_core, split_ext) = split_extended_page(&payload).unwrap();
        assert_eq!(split_core, core);
        assert_eq!(split_ext, extension.as_slice());
        // Full decode: the core "decoder" just returns the row count.
        let (value, rows) =
            decode_extended_page(&payload, |bytes| Ok((bytes.to_vec(), count))).unwrap();
        assert_eq!(value, core);
        assert_eq!(rows.len(), count);
    }

    #[test]
    fn extended_page_rejects_truncation_and_trailing_bytes() {
        let (extension, count) = division_extension_fixture();
        let core = b"core";
        let mut payload = uvarint_bytes(core.len() as u64);
        payload.extend(core);
        payload.extend(&extension);

        // Trailing byte after the extension.
        let mut trailing = payload.clone();
        trailing.push(0);
        let error = decode_extended_page(&trailing, |bytes| Ok((bytes.to_vec(), count)))
            .unwrap_err()
            .to_string();
        assert_eq!(error, "trailing extended-page bytes");

        // Core length claims more than the buffer holds.
        assert_eq!(
            split_extended_page(&uvarint_bytes(10_000))
                .unwrap_err()
                .to_string(),
            "truncated extended-page core"
        );
    }

    // ---- coalescing planner ------------------------------------------------

    fn want(offset: u64, length: u64) -> ByteRange {
        ByteRange { offset, length }
    }

    #[test]
    fn coalesce_empty_is_empty_plan() {
        let plan = coalesce_ranges(&[], 16, 1024).unwrap();
        assert!(plan.reads.is_empty());
    }

    #[test]
    fn coalesce_single_want_is_one_read() {
        let plan = coalesce_ranges(&[want(100, 20)], 16, 1024).unwrap();
        assert_eq!(plan.reads.len(), 1);
        assert_eq!(plan.reads[0].offset, 100);
        assert_eq!(plan.reads[0].length, 20);
        assert_eq!(
            plan.reads[0].wants,
            vec![WantSlice {
                want_index: 0,
                relative_offset: 0,
                length: 20
            }]
        );
    }

    #[test]
    fn coalesce_merges_within_gap_and_splits_beyond_it() {
        // Gap of exactly 8 between [0,10) and [18,28): merges at threshold 8.
        let merged = coalesce_ranges(&[want(0, 10), want(18, 10)], 8, 1024).unwrap();
        assert_eq!(merged.reads.len(), 1);
        assert_eq!(merged.reads[0].offset, 0);
        assert_eq!(merged.reads[0].length, 28);
        assert_eq!(merged.reads[0].wants[1].relative_offset, 18);

        // Gap of 9 with threshold 8: two reads.
        let split = coalesce_ranges(&[want(0, 10), want(19, 10)], 8, 1024).unwrap();
        assert_eq!(split.reads.len(), 2);
        assert_eq!(split.reads[1].offset, 19);
    }

    #[test]
    fn coalesce_respects_max_range_budget() {
        // Adjacent wants that would merge on gap, but the merged span exceeds
        // the max range budget, so they stay separate.
        let plan = coalesce_ranges(&[want(0, 60), want(60, 60)], 1024, 100).unwrap();
        assert_eq!(plan.reads.len(), 2);
    }

    #[test]
    fn coalesce_handles_overlap_containment_and_duplicates() {
        // want 1 is contained in want 0; want 2 duplicates want 0.
        let wants = [want(0, 100), want(20, 10), want(0, 100)];
        let plan = coalesce_ranges(&wants, 0, 1024).unwrap();
        assert_eq!(plan.reads.len(), 1);
        assert_eq!(plan.reads[0].offset, 0);
        assert_eq!(plan.reads[0].length, 100);
        let mut mapped: Vec<_> = plan.reads[0]
            .wants
            .iter()
            .map(|slice| (slice.want_index, slice.relative_offset, slice.length))
            .collect();
        mapped.sort();
        assert_eq!(mapped, vec![(0, 0, 100), (1, 20, 10), (2, 0, 100)]);
    }

    #[test]
    fn coalesce_sorts_out_of_order_input_and_preserves_indices() {
        // Supplied high-offset-first; the plan must sort but keep want_index.
        let wants = [want(1000, 10), want(0, 10)];
        let plan = coalesce_ranges(&wants, 0, 1024).unwrap();
        assert_eq!(plan.reads.len(), 2);
        assert_eq!(plan.reads[0].offset, 0);
        assert_eq!(plan.reads[0].wants[0].want_index, 1);
        assert_eq!(plan.reads[1].offset, 1000);
        assert_eq!(plan.reads[1].wants[0].want_index, 0);
    }

    #[test]
    fn coalesce_rejects_pathological_inputs() {
        assert!(coalesce_ranges(&[want(0, 0)], 16, 1024).is_err());
        assert!(coalesce_ranges(&[want(u64::MAX, 1)], 16, 1024).is_err());
        assert!(coalesce_ranges(&[want(0, 2048)], 16, 1024).is_err());
        assert!(coalesce_ranges(&[want(0, 10)], 16, 0).is_err());
    }

    // ---- cross-language fixtures ------------------------------------------

    /// Record count baked into `tests/fixtures/pages/*` by
    /// `tests/generate_page_fixtures.py` (kept in sync by the Python
    /// byte-for-byte regeneration test).
    const FIXTURE_RECORD_COUNT: usize = 3;

    #[test]
    fn decodes_committed_extended_page_fixture() {
        // The Python `encode_extended_page` output must split into the exact
        // standalone plain page plus the division extension for every record.
        let extended = include_bytes!("../../../tests/fixtures/pages/extended_page.bin");
        let plain = include_bytes!("../../../tests/fixtures/pages/plain_page.bin");
        let (core, extension_bytes) = split_extended_page(extended).unwrap();
        assert_eq!(core, plain);

        let (rows, consumed) =
            decode_division_extension(extension_bytes, FIXTURE_RECORD_COUNT).unwrap();
        assert_eq!(consumed, extension_bytes.len());
        assert_eq!(rows.len(), FIXTURE_RECORD_COUNT);
        // Record 0 has two containing-division GERS IDs (UUID 7 and 8),
        // interior match, confidence 2; record 2 has none.
        assert_eq!(
            rows[0].division_gers_ids,
            vec![
                format_uuid(7u128.to_be_bytes()),
                format_uuid(8u128.to_be_bytes())
            ]
        );
        assert_eq!(rows[0].match_method, 1);
        assert_eq!(rows[0].match_confidence, 2);
        assert!(rows[2].division_gers_ids.is_empty());
        assert_eq!(rows[2].match_method, 0);

        // The whole page also decodes through the composed helper, using the
        // page's leading uvarint row count as the payload-agnostic core count.
        let (core_bytes, ext_rows) = decode_extended_page(extended, |core| {
            let mut reader = ByteReader::new(core, 0);
            let count = usize::try_from(reader.uvarint()?).unwrap();
            Ok((core.to_vec(), count))
        })
        .unwrap();
        assert_eq!(core_bytes, plain);
        assert_eq!(ext_rows.len(), FIXTURE_RECORD_COUNT);
    }

    #[test]
    fn rejects_committed_truncated_extended_page_fixture() {
        let truncated = include_bytes!("../../../tests/fixtures/pages/truncated_extended_page.bin");
        assert_eq!(
            split_extended_page(truncated).unwrap_err().to_string(),
            "truncated extended-page core"
        );
    }
}
