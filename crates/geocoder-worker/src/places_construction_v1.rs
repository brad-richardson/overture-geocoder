//! Decoder/query boundary for Places construction-v1 serving artifacts.
//!
//! The `/v2/forward` Places lane routes here when a release's Places family
//! declares the promoted construction format (`PLRV0002+PLHD0002`): the
//! promoted `slice-YYYY-MM-DD.N/families/places/` tree holds `routing.json`
//! (`overture-promoted-places-routing-v1`), the copied head routing manifest
//! (`overture-places-global-head-sharded-v2`), and content-addressed
//! `objects/<sha256>.plrv` / `.plhd` serving artifacts. Everything below
//! `impl ShardLoader` is pure and natively tested; the loader glue only
//! fetches bounded bytes and composes those pure calls.

#![allow(dead_code)]

use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;

use geocoder_core::pages::{format_uuid, ByteRange};
use serde::Deserialize;
use sha2::{Digest, Sha256};

use crate::places_pages::{point_quadkey, PlaceProjection};
use crate::range_reader::RangeReader;
use crate::stac::cache::IMMUTABLE_CACHE_TTL;
use crate::stac::{not_found, ShardLoader};

type Result<T> = std::result::Result<T, String>;
const MAXIMUM_INDEX_PROBES: usize = 32;

/// The promoted construction-v1 Places family format identity
/// (`scripts/promote_construction_slice.py` `DEFAULT_VERSIONS["places"]`):
/// routed `.plrv` artifacts plus the 4,096-way sharded `.plhd` head.
/// The format this worker's encoder produces. `0003` adds a `prominence_rank`
/// u8 immediately after `confidence_rank`.
pub(crate) const PLACES_CONSTRUCTION_FORMAT: &str = "PLRV0003+PLHD0003";

/// Every promoted format this worker can SERVE, newest first.
///
/// `0002` is not legacy cruft -- it is what is live right now. The deploy that
/// carries this change reaches production long before any `0003` build is
/// promoted, so a worker that accepted only `0003` would stop serving Places
/// the moment it shipped. `0002` shards simply decode with a zero prominence
/// prior, which is exactly the ranking they were built with.
pub(crate) const SUPPORTED_PLACES_CONSTRUCTION_FORMATS: [&str; 2] =
    ["PLRV0003+PLHD0003", "PLRV0002+PLHD0002"];

/// Whether a promoted family's declared format is one this worker can decode.
pub(crate) fn supports_places_construction_format(format: &str) -> bool {
    SUPPORTED_PLACES_CONSTRUCTION_FORMATS.contains(&format)
}

/// Which entry layout a shard's magic declares.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum PlacesV1Version {
    /// `field_mask, confidence_rank, ...`
    V2,
    /// `field_mask, confidence_rank, prominence_rank, ...`
    V3,
}
pub(crate) const PLACES_ROUTING_SCHEMA: &str = "overture-promoted-places-routing-v1";
pub(crate) const PLACES_HEAD_MANIFEST_SCHEMA: &str = "overture-places-global-head-sharded-v2";
const PLACES_ROUTING_CELL_SCHEME: &str = "level-4-quadkey-yx-hex";
const PLACES_ROUTING_SUBPARTITION_SCHEME: &str = "token-sha256-nibble-prefix-v1";

/// routing.json cap. The planet slice carries 16,601 routed entries at roughly
/// 80 bytes each (~1.3 MiB); 8 MiB leaves 6x headroom and stays far under the
/// isolate budget.
pub(crate) const MAX_PLACES_ROUTING_BYTES: usize = 8 * 1024 * 1024;
/// Head routing manifest cap: 4,096 shard rows plus digests is about 1.5 MiB.
pub(crate) const MAX_PLACES_HEAD_ROUTING_BYTES: usize = 8 * 1024 * 1024;
/// Whole-object cap retained for fixture/local decode. Live routed serving uses
/// the range reader below, because planet `.plrv` objects reach ~209 MiB and
/// cannot be materialized inside the 128 MiB isolate.
pub(crate) const MAX_ROUTED_OBJECT_BYTES: usize = 64 * 1024 * 1024;
/// The construction publisher enforces a 5 GiB Places object ceiling. Range
/// serving accepts that producer envelope without ever materializing the
/// object, and rejects any larger size before trusting offsets from its header.
const MAX_ROUTED_ARTIFACT_BYTES: u64 = 5 * 1000 * 1000 * 1000;
/// Exact mirrors of the serving encoder's directory caps. Only the fixed
/// 40-byte rows and collision keys are fetched; the full key table is never
/// retained by a request.
const MAX_ROUTED_INDEX_ENTRIES: usize = 250_000;
const MAX_ROUTED_INDEX_KEY_BYTES: u64 = 256 * 1024 * 1024;
const MAX_ROUTED_INDEX_KEY_ENTRY_BYTES: u64 = 5 + u16::MAX as u64;
const ROUTED_HEADER_BYTES: u64 = 32;
const ROUTED_FIXED_INDEX_ROW_BYTES: u64 = 40;
/// Whole-object cap for one `.plhd` head shard (~2.7 MB at planet scale).
pub(crate) const MAX_HEAD_SHARD_BYTES: usize = 16 * 1024 * 1024;
const MAX_ARTIFACT_RECORDS: usize = 5_000_000;
const MAX_ARTIFACT_ENTRY_BYTES: usize = 64 * 1024;
/// Producer cap: at most `maximum_serving_candidates` (256) records survive per
/// `(partition_cell, token)` group (`scripts/places_construction_v1.py`).
const ROUTED_CANDIDATE_CAP: usize = 256;
/// Producer cap: at most `head_result_cap` (10) records per head token.
const HEAD_RESULT_CAP: usize = 10;
pub(crate) const ENTITY_PHRASE_ADMISSION: &str = "prominence-primary-name-v1";
/// Maximum global-head query width. Three admits common landmark names such as
/// `Statue of Liberty`. The no-proximity lane performs at most three ordinary
/// token reads plus two exact phrase reads (the full phrase and, for a
/// three-token query, its two-token prefix). Routed lookup retains its
/// independent four-token cap.
pub(crate) const HEAD_QUERY_TOKEN_CAP: usize = 3;
/// Widest query the additive prefix-head fallback attempts. A tail longer than
/// three dropped tokens is not a POI name with a category or locality suffix,
/// which is the class this fallback exists for.
pub(crate) const PREFIX_HEAD_FALLBACK_TOKEN_CAP: usize = 6;
/// Verified survivors returned by the prefix-head fallback. The prefix probe is
/// already bounded by the head producer caps (at most three ordinary postings of
/// ten plus two phrase postings of ten = fifty pre-verification candidates);
/// this caps what a single fallback contributes to assembly.
const PREFIX_HEAD_FALLBACK_RESULT_CAP: usize = HEAD_RESULT_CAP;
const ENTITY_PHRASE_GROUP_CAP: usize = 2;
const HEAD_CANDIDATE_CAP: usize = 256;
const MAX_ROUTING_CELLS: usize = 65_536;
const MAX_CELL_SUBPARTITIONS: usize = 4_096;
/// `_prefix_sql` in scripts/places_construction_v1.py rejects depth > 8.
const MAX_SUBPARTITION_DEPTH: usize = 8;
const MAX_HEAD_SHARD_BITS: u32 = 24;

thread_local! {
    /// Parsed promoted routing tables, LRU-last, one live generation each.
    static PLACES_ROUTING_CACHE: RefCell<Vec<(String, Rc<PlacesRouting>)>> =
        const { RefCell::new(Vec::new()) };
    static PLACES_HEAD_ROUTING_CACHE: RefCell<Vec<(String, Rc<HeadRoutingManifest>)>> =
        const { RefCell::new(Vec::new()) };
}

struct PlacesV1Index {
    hash: u64,
    key: Vec<u8>,
    payload_offset: usize,
    payload_bytes: usize,
    records: usize,
}

#[derive(Debug, PartialEq, Eq)]
struct PlacesV1RangeHeader {
    expected_records: usize,
    index_offset: u64,
    index_count: usize,
    /// Entry layout declared by this artifact's magic, carried to the payload
    /// decode so a ranged read uses the same decoder as a whole-shard read.
    version: PlacesV1Version,
}

impl PlacesV1RangeHeader {
    fn fixed_index_bytes(&self) -> Result<u64> {
        (self.index_count as u64)
            .checked_mul(ROUTED_FIXED_INDEX_ROW_BYTES)
            .and_then(|bytes| bytes.checked_add(4))
            .ok_or_else(|| "Places v1 fixed index extent overflows".to_string())
    }

    fn key_table_offset(&self) -> Result<u64> {
        self.index_offset
            .checked_add(self.fixed_index_bytes()?)
            .ok_or_else(|| "Places v1 key table offset overflows".to_string())
    }
}

#[derive(Debug, PartialEq, Eq)]
struct PlacesV1RangeIndex {
    hash: u64,
    key_position: u64,
    key_length: u64,
    records: usize,
    payload_offset: u64,
    payload_bytes: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum PlacesV1Mode {
    Routed,
    Head,
}

impl PlacesV1Mode {
    /// Accepted magics for this mode, newest first. Returned with the layout
    /// version each one implies so `parse` can pick the entry decoder.
    fn magics(self) -> [(&'static [u8; 8], PlacesV1Version); 2] {
        match self {
            Self::Routed => [
                (b"PLRV0003", PlacesV1Version::V3),
                (b"PLRV0002", PlacesV1Version::V2),
            ],
            Self::Head => [
                (b"PLHD0003", PlacesV1Version::V3),
                (b"PLHD0002", PlacesV1Version::V2),
            ],
        }
    }

    fn version_of(self, bytes: &[u8]) -> Option<PlacesV1Version> {
        self.magics()
            .into_iter()
            .find(|(magic, _)| bytes.len() >= 8 && &bytes[..8] == magic.as_slice())
            .map(|(_, version)| version)
    }
}

#[derive(Debug, PartialEq)]
pub(crate) struct PlacesV1Record {
    pub token: String,
    pub partition_cell: Option<String>,
    pub field_mask: u8,
    pub confidence_rank: u8,
    /// Static category prominence prior. Always 0 on a `0002` shard, which
    /// carried no such byte -- that is the ranking those shards were built with.
    pub prominence_rank: u8,
    pub id: String,
    pub longitude: f64,
    pub latitude: f64,
    pub source_object_index: u32,
    pub source_row_group: u32,
    pub source_row_index: u64,
    pub primary_name: String,
    pub brand_name: String,
    pub category: String,
    pub locality: String,
    pub region: String,
    pub country: String,
}

pub(crate) struct PlacesV1Artifact<'a> {
    bytes: &'a [u8],
    mode: PlacesV1Mode,
    version: PlacesV1Version,
    index: Vec<PlacesV1Index>,
    maximum_entry_bytes: usize,
}

impl<'a> PlacesV1Artifact<'a> {
    pub(crate) fn parse(
        bytes: &'a [u8],
        mode: PlacesV1Mode,
        maximum_bytes: usize,
        maximum_records: usize,
        maximum_entry_bytes: usize,
    ) -> Result<Self> {
        if bytes.len() > maximum_bytes || bytes.len() < 36 {
            return Err("invalid or over-cap Places v1 artifact".to_string());
        }
        let Some(version) = mode.version_of(bytes) else {
            return Err("invalid or over-cap Places v1 artifact".to_string());
        };
        let expected = usize::try_from(read_u64(bytes, 8)?)
            .map_err(|_| "Places v1 count overflows".to_string())?;
        if expected > maximum_records {
            return Err("Places v1 record cap exceeded".to_string());
        }
        let index_offset = usize::try_from(read_u64(bytes, 16)?)
            .map_err(|_| "Places v1 index offset overflows".to_string())?;
        let index_count = read_u32(bytes, 24)? as usize;
        if read_u32(bytes, 28)? != 0 || index_offset < 32 || index_offset > bytes.len() {
            return Err("Places v1 header does not reconcile".to_string());
        }
        let mut position = index_offset;
        if read_u32_at(bytes, &mut position)? as usize != index_count {
            return Err("Places v1 index count differs".to_string());
        }
        let fixed_start = position;
        let key_start = fixed_start
            .checked_add(
                index_count
                    .checked_mul(40)
                    .ok_or_else(|| "Places v1 index overflows".to_string())?,
            )
            .ok_or_else(|| "Places v1 index overflows".to_string())?;
        if key_start > bytes.len() {
            return Err("Places v1 index is truncated".to_string());
        }
        let mut index = Vec::with_capacity(index_count);
        let mut key_position_expected = 0_usize;
        let mut previous_key: Option<(u64, Vec<u8>)> = None;
        let mut records = 0_usize;
        for _ in 0..index_count {
            let hash = read_u64_at(bytes, &mut position)?;
            let key_position = usize::try_from(read_u64_at(bytes, &mut position)?)
                .map_err(|_| "Places v1 key offset overflows".to_string())?;
            let key_length = read_u32_at(bytes, &mut position)? as usize;
            let entry_records = read_u32_at(bytes, &mut position)? as usize;
            let payload_offset = usize::try_from(read_u64_at(bytes, &mut position)?)
                .map_err(|_| "Places v1 payload offset overflows".to_string())?;
            let payload_bytes = usize::try_from(read_u64_at(bytes, &mut position)?)
                .map_err(|_| "Places v1 payload bytes overflow".to_string())?;
            let key_at = key_start
                .checked_add(key_position)
                .ok_or_else(|| "Places v1 key offset overflows".to_string())?;
            let key = bytes
                .get(
                    key_at
                        ..key_at
                            .checked_add(key_length)
                            .ok_or_else(|| "Places v1 key overflows".to_string())?,
                )
                .ok_or_else(|| "Places v1 key is truncated".to_string())?
                .to_vec();
            if key_position != key_position_expected
                || hash != index_hash(&key)
                || entry_records == 0
                || payload_offset < 32
                || payload_offset
                    .checked_add(payload_bytes)
                    .is_none_or(|end| end > index_offset)
                || previous_key
                    .as_ref()
                    .is_some_and(|value| value >= &(hash, key.clone()))
            {
                return Err("Places v1 index entry is invalid".to_string());
            }
            key_position_expected += key_length;
            records = records
                .checked_add(entry_records)
                .ok_or_else(|| "Places v1 record count overflows".to_string())?;
            previous_key = Some((hash, key.clone()));
            index.push(PlacesV1Index {
                hash,
                key,
                payload_offset,
                payload_bytes,
                records: entry_records,
            });
        }
        if position != key_start
            || key_start + key_position_expected != bytes.len()
            || records != expected
        {
            return Err("Places v1 index length/count differs".to_string());
        }
        let mut ranges: Vec<_> = index
            .iter()
            .map(|item| (item.payload_offset, item.payload_bytes))
            .collect();
        ranges.sort_unstable();
        let mut payload_position = 32;
        for (offset, length) in ranges {
            if offset != payload_position {
                return Err("Places v1 payload ranges are not contiguous".to_string());
            }
            payload_position = payload_position
                .checked_add(length)
                .ok_or_else(|| "Places v1 payload range overflows".to_string())?;
        }
        if payload_position != index_offset {
            return Err("Places v1 payload length differs".to_string());
        }
        Ok(Self {
            bytes,
            mode,
            version,
            index,
            maximum_entry_bytes,
        })
    }

    pub(crate) fn lookup(
        &self,
        token: &str,
        partition_cell: Option<&str>,
        maximum_candidates: usize,
        result_cap: usize,
    ) -> Result<Vec<PlacesV1Record>> {
        if token.is_empty() || result_cap > maximum_candidates {
            return Err("invalid Places v1 query caps".to_string());
        }
        if self.mode == PlacesV1Mode::Routed && partition_cell.is_none_or(|cell| cell.len() != 4) {
            return Err("routed Places v1 query requires a cell".to_string());
        }
        if self.mode == PlacesV1Mode::Head && partition_cell.is_some() {
            return Err("head Places v1 query must not include a cell".to_string());
        }
        let key = query_key(self.mode, partition_cell, token);
        let hash = index_hash(&key);
        let start = self.index.partition_point(|item| item.hash < hash);
        let mut selected = None;
        let mut probes = 0;
        for item in &self.index[start..] {
            if item.hash != hash {
                break;
            }
            probes += 1;
            if probes > MAXIMUM_INDEX_PROBES {
                return Err("Places v1 index probe cap exceeded".to_string());
            }
            if item.key == key {
                selected = Some(item);
                break;
            }
        }
        let Some(selected) = selected else {
            return Ok(Vec::new());
        };
        if selected.records > maximum_candidates {
            return Err("Places v1 candidate cap exceeded".to_string());
        }
        let mut position = selected.payload_offset;
        let end = position + selected.payload_bytes;
        let mut output = Vec::new();
        for _ in 0..selected.records {
            let length = read_u32_at(self.bytes, &mut position)? as usize;
            if length > self.maximum_entry_bytes {
                return Err("Places v1 entry cap exceeded".to_string());
            }
            let entry = take(self.bytes, &mut position, length)?;
            let (record, _) = decode_entry(entry, self.mode, self.version)?;
            if record.token != token || record.partition_cell.as_deref() != partition_cell {
                return Err("Places v1 indexed payload key differs".to_string());
            }
            if output.len() < result_cap {
                output.push(record);
            }
        }
        if position != end {
            return Err("Places v1 indexed payload length differs".to_string());
        }
        Ok(output)
    }
}

fn query_key(mode: PlacesV1Mode, cell: Option<&str>, token: &str) -> Vec<u8> {
    if mode == PlacesV1Mode::Routed {
        let mut output = cell.unwrap().as_bytes().to_vec();
        output.push(0);
        output.extend_from_slice(token.as_bytes());
        output
    } else {
        token.as_bytes().to_vec()
    }
}

fn index_hash(key: &[u8]) -> u64 {
    let mut digest = Sha256::new();
    digest.update(b"overture-places-serving-index-v1\0");
    digest.update(key);
    u64::from_be_bytes(digest.finalize()[..8].try_into().unwrap())
}

fn parse_routed_range_header(bytes: &[u8], file_size: u64) -> Result<PlacesV1RangeHeader> {
    if bytes.len() != ROUTED_HEADER_BYTES as usize
        || PlacesV1Mode::Routed.version_of(bytes).is_none()
        || !(36..=MAX_ROUTED_ARTIFACT_BYTES).contains(&file_size)
    {
        return Err("invalid or over-cap Places v1 ranged artifact".to_string());
    }
    let expected_records = usize::try_from(read_u64(bytes, 8)?)
        .map_err(|_| "Places v1 count overflows".to_string())?;
    let index_offset = read_u64(bytes, 16)?;
    let index_count = read_u32(bytes, 24)? as usize;
    if expected_records > MAX_ARTIFACT_RECORDS
        || index_count > MAX_ROUTED_INDEX_ENTRIES
        || read_u32(bytes, 28)? != 0
        || index_offset < ROUTED_HEADER_BYTES
        || index_offset >= file_size
    {
        return Err("Places v1 ranged header does not reconcile".to_string());
    }
    let version = PlacesV1Mode::Routed
        .version_of(bytes)
        .ok_or_else(|| "Places v1 ranged magic is unknown".to_string())?;
    let header = PlacesV1RangeHeader {
        expected_records,
        index_offset,
        index_count,
        version,
    };
    if header.key_table_offset()? > file_size {
        return Err("Places v1 ranged index is truncated".to_string());
    }
    Ok(header)
}

fn parse_routed_range_index(
    bytes: &[u8],
    header: &PlacesV1RangeHeader,
    file_size: u64,
) -> Result<Vec<PlacesV1RangeIndex>> {
    let expected_fixed_bytes = usize::try_from(header.fixed_index_bytes()?)
        .map_err(|_| "Places v1 fixed index exceeds platform bounds".to_string())?;
    if bytes.len() != expected_fixed_bytes {
        return Err("Places v1 fixed index is truncated".to_string());
    }
    let mut position = 0_usize;
    if read_u32_at(bytes, &mut position)? as usize != header.index_count {
        return Err("Places v1 ranged index count differs".to_string());
    }
    let mut index = Vec::with_capacity(header.index_count);
    let mut key_position_expected = 0_u64;
    let mut previous_hash = None;
    let mut records = 0_usize;
    let mut payload_ranges = Vec::with_capacity(header.index_count);
    for _ in 0..header.index_count {
        let hash = read_u64_at(bytes, &mut position)?;
        let key_position = read_u64_at(bytes, &mut position)?;
        let key_length = u64::from(read_u32_at(bytes, &mut position)?);
        let entry_records = read_u32_at(bytes, &mut position)? as usize;
        let payload_offset = read_u64_at(bytes, &mut position)?;
        let payload_bytes = read_u64_at(bytes, &mut position)?;
        let payload_end = payload_offset
            .checked_add(payload_bytes)
            .ok_or_else(|| "Places v1 payload extent overflows".to_string())?;
        if key_position != key_position_expected
            || key_length == 0
            || key_length > MAX_ROUTED_INDEX_KEY_ENTRY_BYTES
            || entry_records == 0
            || payload_bytes == 0
            || payload_offset < ROUTED_HEADER_BYTES
            || payload_end > header.index_offset
            || previous_hash.is_some_and(|previous| previous > hash)
        {
            return Err("Places v1 ranged index entry is invalid".to_string());
        }
        key_position_expected = key_position
            .checked_add(key_length)
            .ok_or_else(|| "Places v1 key extent overflows".to_string())?;
        if key_position_expected > MAX_ROUTED_INDEX_KEY_BYTES {
            return Err("Places v1 ranged index key byte cap exceeded".to_string());
        }
        records = records
            .checked_add(entry_records)
            .ok_or_else(|| "Places v1 record count overflows".to_string())?;
        previous_hash = Some(hash);
        payload_ranges.push((payload_offset, payload_bytes));
        index.push(PlacesV1RangeIndex {
            hash,
            key_position,
            key_length,
            records: entry_records,
            payload_offset,
            payload_bytes,
        });
    }
    if position != bytes.len() || records != header.expected_records {
        return Err("Places v1 ranged index length/count differs".to_string());
    }
    let canonical_size = header
        .key_table_offset()?
        .checked_add(key_position_expected)
        .ok_or_else(|| "Places v1 ranged artifact size overflows".to_string())?;
    if canonical_size != file_size {
        return Err("Places v1 ranged artifact size differs".to_string());
    }
    payload_ranges.sort_unstable();
    let mut payload_position = ROUTED_HEADER_BYTES;
    for (offset, length) in payload_ranges {
        if offset != payload_position {
            return Err("Places v1 ranged payloads are not contiguous".to_string());
        }
        payload_position = payload_position
            .checked_add(length)
            .ok_or_else(|| "Places v1 ranged payload extent overflows".to_string())?;
    }
    if payload_position != header.index_offset {
        return Err("Places v1 ranged payload length differs".to_string());
    }
    Ok(index)
}

fn ranged_index_candidates<'a>(
    index: &'a [PlacesV1RangeIndex],
    key: &[u8],
) -> Result<Vec<&'a PlacesV1RangeIndex>> {
    let hash = index_hash(key);
    let start = index.partition_point(|item| item.hash < hash);
    let candidates: Vec<_> = index[start..]
        .iter()
        .take_while(|item| item.hash == hash)
        .collect();
    if candidates.len() > MAXIMUM_INDEX_PROBES {
        return Err("Places v1 ranged index probe cap exceeded".to_string());
    }
    Ok(candidates)
}

fn validate_routed_range_payload_extent(selected: &PlacesV1RangeIndex) -> Result<()> {
    if selected.records > ROUTED_CANDIDATE_CAP {
        return Err("Places v1 ranged candidate cap exceeded".to_string());
    }
    let minimum_payload = (selected.records as u64)
        .checked_mul(4)
        .ok_or_else(|| "Places v1 ranged payload minimum overflows".to_string())?;
    let maximum_payload = (selected.records as u64)
        .checked_mul(4 + MAX_ARTIFACT_ENTRY_BYTES as u64)
        .ok_or_else(|| "Places v1 ranged payload cap overflows".to_string())?;
    if !(minimum_payload..=maximum_payload).contains(&selected.payload_bytes) {
        return Err("Places v1 ranged payload exceeds its hard bound".to_string());
    }
    Ok(())
}

fn decode_routed_range_payload(
    bytes: &[u8],
    selected: &PlacesV1RangeIndex,
    cell: &str,
    token: &str,
    version: PlacesV1Version,
) -> Result<Vec<PlacesV1Record>> {
    validate_routed_range_payload_extent(selected)?;
    if bytes.len() as u64 != selected.payload_bytes {
        return Err("Places v1 ranged payload length differs from its index".to_string());
    }
    let mut position = 0_usize;
    let mut output = Vec::with_capacity(selected.records);
    for _ in 0..selected.records {
        let length = read_u32_at(bytes, &mut position)? as usize;
        if length > MAX_ARTIFACT_ENTRY_BYTES {
            return Err("Places v1 ranged entry cap exceeded".to_string());
        }
        let entry = take(bytes, &mut position, length)?;
        let (record, _) = decode_entry(entry, PlacesV1Mode::Routed, version)?;
        if record.token != token || record.partition_cell.as_deref() != Some(cell) {
            return Err("Places v1 ranged payload key differs".to_string());
        }
        output.push(record);
    }
    if position != bytes.len() {
        return Err("Places v1 ranged payload length differs".to_string());
    }
    Ok(output)
}

/// Shard id owning a head `token` under a `shard_bits`-wide top-hash prefix.
///
/// The global head is split into `1 << shard_bits` hash shards (a per-build
/// manifest value; 4096 / 12 bits at planet scale) keyed by the top bits of the
/// token's existing index hash — the first three hex nibbles when
/// `shard_bits == 12`, mirroring the production 4096-shard UUID-prefix ID index.
/// A live serving path resolves a token to this id, then range-reads (or
/// fetch-and-caches under the 1-hour shard TTL) exactly that one shard object
/// before decoding it as an ordinary head artifact.
pub(crate) fn head_shard_id(token: &str, shard_bits: u32) -> u32 {
    debug_assert!((1..=24).contains(&shard_bits));
    (index_hash(token.as_bytes()) >> (64 - shard_bits)) as u32
}

/// Decode one already-fetched head shard and answer a token query against it.
///
/// `shard_bytes` is the whole shard object (the ~2.7 MB planet-sized head shard
/// the caller obtained for `head_shard_id(token, shard_bits)`). Beyond the
/// ordinary head decode + lookup this fails closed if the shard's own bytes
/// disagree with the manifest assignment, so a mis-routed fetch can never serve
/// another shard's answer.
#[allow(clippy::too_many_arguments)]
pub(crate) fn lookup_head_shard(
    shard_bytes: &[u8],
    shard_id: u32,
    shard_bits: u32,
    token: &str,
    maximum_bytes: usize,
    maximum_records: usize,
    maximum_entry_bytes: usize,
    maximum_candidates: usize,
    result_cap: usize,
) -> Result<Vec<PlacesV1Record>> {
    if head_shard_id(token, shard_bits) != shard_id {
        return Err("Places v1 head token does not belong to this shard".to_string());
    }
    let artifact = PlacesV1Artifact::parse(
        shard_bytes,
        PlacesV1Mode::Head,
        maximum_bytes,
        maximum_records,
        maximum_entry_bytes,
    )?;
    artifact.lookup(token, None, maximum_candidates, result_cap)
}

fn decode_entry(
    data: &[u8],
    mode: PlacesV1Mode,
    version: PlacesV1Version,
) -> Result<(PlacesV1Record, [u8; 16])> {
    let mut position = 0;
    let token = read_text(data, &mut position)?;
    let partition_cell = match mode {
        PlacesV1Mode::Routed => Some(read_text(data, &mut position)?),
        PlacesV1Mode::Head => None,
    };
    let field_mask = take(data, &mut position, 1)?[0];
    let confidence_rank = take(data, &mut position, 1)?[0];
    // `0002` shards predate this byte. They decode with a zero prior, which is
    // the ranking they were actually built with -- not a silent downgrade.
    let prominence_rank = match version {
        PlacesV1Version::V3 => take(data, &mut position, 1)?[0],
        PlacesV1Version::V2 => 0,
    };
    let raw_id: [u8; 16] = take(data, &mut position, 16)?.try_into().unwrap();
    let longitude = f64::from_le_bytes(take(data, &mut position, 8)?.try_into().unwrap());
    let latitude = f64::from_le_bytes(take(data, &mut position, 8)?.try_into().unwrap());
    let source_object_index = read_u32_at(data, &mut position)?;
    let source_row_group = read_u32_at(data, &mut position)?;
    let source_row_index = read_u64_at(data, &mut position)?;
    let display = (0..6)
        .map(|_| read_text(data, &mut position))
        .collect::<Result<Vec<_>>>()?;
    if position != data.len()
        || token.is_empty()
        || field_mask == 0
        || !longitude.is_finite()
        || !latitude.is_finite()
        || partition_cell.as_ref().is_some_and(|cell| cell.len() != 4)
    {
        return Err("invalid Places v1 entry".to_string());
    }
    Ok((
        PlacesV1Record {
            token,
            partition_cell,
            field_mask,
            confidence_rank,
            prominence_rank,
            id: format_uuid(raw_id),
            longitude,
            latitude,
            source_object_index,
            source_row_group,
            source_row_index,
            primary_name: display[0].clone(),
            brand_name: display[1].clone(),
            category: display[2].clone(),
            locality: display[3].clone(),
            region: display[4].clone(),
            country: display[5].clone(),
        },
        raw_id,
    ))
}

fn read_text(bytes: &[u8], position: &mut usize) -> Result<String> {
    let length = read_u16_at(bytes, position)? as usize;
    std::str::from_utf8(take(bytes, position, length)?)
        .map(str::to_owned)
        .map_err(|_| "Places v1 text is not UTF-8".to_string())
}

fn take<'a>(bytes: &'a [u8], position: &mut usize, length: usize) -> Result<&'a [u8]> {
    let end = position
        .checked_add(length)
        .ok_or_else(|| "Places v1 offset overflows".to_string())?;
    let value = bytes
        .get(*position..end)
        .ok_or_else(|| "Places v1 entry is truncated".to_string())?;
    *position = end;
    Ok(value)
}

fn read_u16_at(bytes: &[u8], position: &mut usize) -> Result<u16> {
    Ok(u16::from_le_bytes(
        take(bytes, position, 2)?.try_into().unwrap(),
    ))
}
fn read_u32_at(bytes: &[u8], position: &mut usize) -> Result<u32> {
    Ok(u32::from_le_bytes(
        take(bytes, position, 4)?.try_into().unwrap(),
    ))
}
fn read_u64_at(bytes: &[u8], position: &mut usize) -> Result<u64> {
    Ok(u64::from_le_bytes(
        take(bytes, position, 8)?.try_into().unwrap(),
    ))
}
fn read_u64(bytes: &[u8], offset: usize) -> Result<u64> {
    Ok(u64::from_le_bytes(
        bytes
            .get(offset..offset + 8)
            .ok_or_else(|| "Places v1 header is truncated".to_string())?
            .try_into()
            .unwrap(),
    ))
}
fn read_u32(bytes: &[u8], offset: usize) -> Result<u32> {
    Ok(u32::from_le_bytes(
        bytes
            .get(offset..offset + 4)
            .ok_or_else(|| "Places v1 header is truncated".to_string())?
            .try_into()
            .unwrap(),
    ))
}

// ---------------------------------------------------------------------------
// Promoted-slice routing (routing.json + head routing manifest).

/// The construction partition hash of one token: the top eight big-endian
/// bytes of SHA-256 over the `overture-places-token-partition-v1\0` domain.
/// Byte-identical to `token_hash` in
/// `crates/geocoder-construction/src/bin/places_transform_v1.rs`; the
/// `token-sha256-nibble-prefix-v1` ownership prefixes are its top nibbles.
pub(crate) fn routed_token_hash(token: &str) -> u64 {
    let mut digest = Sha256::new();
    digest.update(b"overture-places-token-partition-v1\0");
    digest.update(token.as_bytes());
    u64::from_be_bytes(digest.finalize()[..8].try_into().unwrap())
}

/// The level-4 `{y:02x}{x:02x}` construction partition cell containing a
/// point: the level-8 quadkey re-encoded onto the 256x256 `(y<<8)|x` grid.
/// The correspondence is pinned by the shared
/// `tests/fixtures/reverse/cell-identifier-vectors-v1.json` vectors.
pub(crate) fn construction_cell(longitude: f64, latitude: f64) -> Option<String> {
    let quadkey = point_quadkey(longitude, latitude, 8)?;
    let mut x = 0_u32;
    let mut y = 0_u32;
    for digit in quadkey.bytes().map(|value| value - b'0') {
        x = (x << 1) | u32::from(digit & 1);
        y = (y << 1) | u32::from((digit >> 1) & 1);
    }
    Some(format!("{y:02x}{x:02x}"))
}

fn valid_sha256_hex(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

/// A published object name must be content-addressed: `<sha256><extension>`.
pub(crate) fn content_addressed_name(name: &str, extension: &str) -> bool {
    name.len() == 64 + extension.len() && name.ends_with(extension) && valid_sha256_hex(&name[..64])
}

#[derive(Debug, Deserialize)]
struct RawRoutingHead {
    schema: String,
    shard_bits: u32,
    shard_count: u32,
    populated_shards: usize,
    manifest_object: String,
}

#[derive(Debug, Deserialize)]
struct RawRouting {
    schema: String,
    family: String,
    cell_scheme: String,
    subpartition_scheme: String,
    cells: std::collections::BTreeMap<String, Vec<(String, String)>>,
    head: RawRoutingHead,
}

#[derive(Debug)]
struct CellSubpartition {
    depth: usize,
    prefix: u64,
    object: String,
}

/// Head geometry as promoted routing.json records it; must agree exactly with
/// the head routing manifest it points at.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct PlacesRoutingHead {
    pub shard_bits: u32,
    pub shard_count: u32,
    pub populated_shards: usize,
    pub manifest_object: String,
}

/// Validated `overture-promoted-places-routing-v1` table.
#[derive(Debug)]
pub(crate) struct PlacesRouting {
    cells: HashMap<String, Vec<CellSubpartition>>,
    pub head: PlacesRoutingHead,
}

impl PlacesRouting {
    pub(crate) fn parse(text: &str) -> Result<Self> {
        let raw: RawRouting = serde_json::from_str(text)
            .map_err(|error| format!("invalid Places routing JSON: {error}"))?;
        if raw.schema != PLACES_ROUTING_SCHEMA
            || raw.family != "places"
            || raw.cell_scheme != PLACES_ROUTING_CELL_SCHEME
            || raw.subpartition_scheme != PLACES_ROUTING_SUBPARTITION_SCHEME
        {
            return Err("unsupported Places routing contract".into());
        }
        if raw.cells.is_empty() || raw.cells.len() > MAX_ROUTING_CELLS {
            return Err("Places routing cell count is outside hard bounds".into());
        }
        let mut cells = HashMap::with_capacity(raw.cells.len());
        for (cell, entries) in raw.cells {
            if cell.len() != 4
                || !cell
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            {
                return Err(format!("Places routing cell is malformed: {cell}"));
            }
            if entries.is_empty() || entries.len() > MAX_CELL_SUBPARTITIONS {
                return Err(format!(
                    "Places routing cell {cell} subpartition count is outside hard bounds"
                ));
            }
            let mut parsed = Vec::with_capacity(entries.len());
            for (prefix, object) in entries {
                let depth = prefix.len();
                if depth > MAX_SUBPARTITION_DEPTH
                    || !prefix
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
                {
                    return Err(format!(
                        "Places routing cell {cell} has an invalid token prefix"
                    ));
                }
                if !content_addressed_name(&object, ".plrv") {
                    return Err(format!(
                        "Places routing cell {cell} names a non-content-addressed object"
                    ));
                }
                let prefix = if depth == 0 {
                    0
                } else {
                    u64::from_str_radix(&prefix, 16)
                        .map_err(|_| format!("Places routing cell {cell} prefix overflows"))?
                };
                parsed.push(CellSubpartition {
                    depth,
                    prefix,
                    object,
                });
            }
            // Exact tiling of the nibble space, mirroring the promotion tool's
            // fail-closed check: a gap would silently drop every token whose
            // hash lands in it, an overlap would make routing ambiguous.
            let depth_max = parsed.iter().map(|entry| entry.depth).max().unwrap_or(0);
            let mut spans: Vec<(u64, u64)> = parsed
                .iter()
                .map(|entry| {
                    let width = 16_u64.pow((depth_max - entry.depth) as u32);
                    (entry.prefix * width, width)
                })
                .collect();
            spans.sort_unstable();
            let mut expected_start = 0_u64;
            for (start, width) in &spans {
                if *start != expected_start {
                    return Err(format!(
                        "Places routing cell {cell} subpartitions do not tile the nibble space"
                    ));
                }
                expected_start = start
                    .checked_add(*width)
                    .ok_or_else(|| format!("Places routing cell {cell} span overflows"))?;
            }
            if expected_start != 16_u64.pow(depth_max as u32) {
                return Err(format!(
                    "Places routing cell {cell} subpartitions do not cover the nibble space"
                ));
            }
            cells.insert(cell, parsed);
        }
        let head = raw.head;
        if head.schema != PLACES_HEAD_MANIFEST_SCHEMA
            || head.shard_bits == 0
            || head.shard_bits > MAX_HEAD_SHARD_BITS
            || head.shard_count != 1_u32 << head.shard_bits
            || head.populated_shards == 0
            || head.populated_shards > head.shard_count as usize
            || !content_addressed_name(&head.manifest_object, ".json")
        {
            return Err("unsupported Places routing head block".into());
        }
        Ok(Self {
            cells,
            head: PlacesRoutingHead {
                shard_bits: head.shard_bits,
                shard_count: head.shard_count,
                populated_shards: head.populated_shards,
                manifest_object: head.manifest_object,
            },
        })
    }

    /// Names of every routed `.plrv` object reachable from the cell table.
    pub(crate) fn routed_object_names(&self) -> impl Iterator<Item = &str> {
        self.cells
            .values()
            .flatten()
            .map(|entry| entry.object.as_str())
    }

    /// The routed object owning `token` inside `cell`. `Ok(None)` means the
    /// cell holds no Places at all; an unowned token inside a populated cell
    /// is a broken tiling invariant and fails closed.
    pub(crate) fn route(&self, cell: &str, token: &str) -> Result<Option<&str>> {
        let Some(entries) = self.cells.get(cell) else {
            return Ok(None);
        };
        let hash = routed_token_hash(token);
        for entry in entries {
            let matched =
                entry.depth == 0 || (hash >> (64 - 4 * entry.depth as u32)) == entry.prefix;
            if matched {
                return Ok(Some(&entry.object));
            }
        }
        Err(format!(
            "Places routing cell {cell} owns no subpartition for the token hash"
        ))
    }
}

#[derive(Debug, Deserialize)]
struct RawHeadShard {
    shard_id: u32,
    path: String,
    sha256: String,
    bytes: u64,
}

#[derive(Debug, Deserialize)]
struct RawHeadManifest {
    schema: String,
    #[serde(default)]
    entity_phrase_admission: Option<String>,
    shard_bits: u32,
    shard_count: u32,
    populated_shards: usize,
    shards: Vec<RawHeadShard>,
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) struct HeadShardIdentity {
    pub shard_id: u32,
    pub path: String,
    pub sha256: String,
    pub bytes: u64,
}

/// Validated `overture-places-global-head-sharded-v2` routing manifest.
#[derive(Debug)]
pub(crate) struct HeadRoutingManifest {
    pub shard_bits: u32,
    pub shard_count: u32,
    entity_phrase_admission: bool,
    shards: HashMap<u32, HeadShardIdentity>,
}

impl HeadRoutingManifest {
    pub(crate) fn parse(text: &str) -> Result<Self> {
        let raw: RawHeadManifest = serde_json::from_str(text)
            .map_err(|error| format!("invalid Places head routing manifest: {error}"))?;
        if raw.schema != PLACES_HEAD_MANIFEST_SCHEMA
            || raw.shard_bits == 0
            || raw.shard_bits > MAX_HEAD_SHARD_BITS
            || raw.shard_count != 1_u32 << raw.shard_bits
            || raw.shards.is_empty()
            || raw.populated_shards != raw.shards.len()
            || raw.shards.len() > raw.shard_count as usize
        {
            return Err("unsupported Places head routing manifest contract".into());
        }
        if raw
            .entity_phrase_admission
            .as_deref()
            .is_some_and(|value| value != ENTITY_PHRASE_ADMISSION)
        {
            return Err("unsupported Places head entity-phrase admission".into());
        }
        let entity_phrase_admission = raw.entity_phrase_admission.is_some();
        let mut shards = HashMap::with_capacity(raw.shards.len());
        for shard in raw.shards {
            if shard.shard_id >= raw.shard_count
                || !content_addressed_name(&shard.path, ".plhd")
                || !valid_sha256_hex(&shard.sha256)
                || shard.bytes == 0
                || shard.bytes > MAX_HEAD_SHARD_BYTES as u64
            {
                return Err("invalid Places head routing shard entry".into());
            }
            let identity = HeadShardIdentity {
                shard_id: shard.shard_id,
                path: shard.path,
                sha256: shard.sha256,
                bytes: shard.bytes,
            };
            if shards.insert(identity.shard_id, identity).is_some() {
                return Err("Places head routing manifest repeats a shard id".into());
            }
        }
        Ok(Self {
            shard_bits: raw.shard_bits,
            shard_count: raw.shard_count,
            entity_phrase_admission,
            shards,
        })
    }

    pub(crate) fn shard(&self, shard_id: u32) -> Option<&HeadShardIdentity> {
        self.shards.get(&shard_id)
    }

    pub(crate) fn shards(&self) -> impl Iterator<Item = &HeadShardIdentity> {
        self.shards.values()
    }

    pub(crate) fn admits_entity_phrases(&self) -> bool {
        self.entity_phrase_admission
    }

    /// Geometry agreement with the routing.json head block, checked at both
    /// admission and serving time.
    pub(crate) fn agrees_with(&self, head: &PlacesRoutingHead) -> bool {
        self.shard_bits == head.shard_bits
            && self.shard_count == head.shard_count
            && self.shards.len() == head.populated_shards
    }

    /// Deterministic admission spot-check sample: the first, middle, and last
    /// populated shards in shard-id order (deduplicated). Full verification of
    /// all 4,096 shards (5.14 GB) is deliberately NOT done at admission; the
    /// per-shard identities remain pinned by this manifest, whose own bytes are
    /// pinned by the release-attested family manifest.
    pub(crate) fn admission_sample(&self) -> Vec<&HeadShardIdentity> {
        let mut ids: Vec<u32> = self.shards.keys().copied().collect();
        ids.sort_unstable();
        let mut picks = vec![ids[0], ids[ids.len() / 2], ids[ids.len() - 1]];
        picks.dedup();
        picks.into_iter().map(|id| &self.shards[&id]).collect()
    }
}

// ---------------------------------------------------------------------------
// Pure query pipeline over fetched artifact bytes.

/// Decode one fetched routed `.plrv` object and answer `(cell, token)`.
pub(crate) fn routed_lookup(bytes: &[u8], cell: &str, token: &str) -> Result<Vec<PlacesV1Record>> {
    let artifact = PlacesV1Artifact::parse(
        bytes,
        PlacesV1Mode::Routed,
        MAX_ROUTED_OBJECT_BYTES,
        MAX_ARTIFACT_RECORDS,
        MAX_ARTIFACT_ENTRY_BYTES,
    )?;
    artifact.lookup(
        token,
        Some(cell),
        ROUTED_CANDIDATE_CAP,
        ROUTED_CANDIDATE_CAP,
    )
}

/// Decode one fetched `.plhd` head shard and answer `token`, keeping the
/// misroute fail-closed check in [`lookup_head_shard`].
pub(crate) fn head_shard_lookup(
    bytes: &[u8],
    shard_id: u32,
    shard_bits: u32,
    token: &str,
) -> Result<Vec<PlacesV1Record>> {
    lookup_head_shard(
        bytes,
        shard_id,
        shard_bits,
        token,
        MAX_HEAD_SHARD_BYTES,
        MAX_ARTIFACT_RECORDS,
        MAX_ARTIFACT_ENTRY_BYTES,
        HEAD_CANDIDATE_CAP,
        HEAD_RESULT_CAP,
    )
}

/// Plan the routed fetches for one proximity query: token indexes grouped by
/// owning object in first-use order. The serving loop walks this plan one
/// object at a time; each object is range-read through its fixed index and
/// selected <=256-record payloads rather than materialized in full. Each
/// object's directory is fetched exactly once per query. `Ok(None)` means the
/// cell holds no Places at all.
/// One fetch per DISTINCT routed object: `(object name, owned token indexes)`
/// in first-use order.
pub(crate) type RoutedFetchPlan<'routing> = Vec<(&'routing str, Vec<usize>)>;

pub(crate) fn routed_fetch_plan<'routing>(
    routing: &'routing PlacesRouting,
    cell: &str,
    tokens: &[String],
) -> Result<Option<RoutedFetchPlan<'routing>>> {
    let mut plan: Vec<(&str, Vec<usize>)> = Vec::new();
    for (index, token) in tokens.iter().enumerate() {
        let Some(object) = routing.route(cell, token)? else {
            return Ok(None);
        };
        match plan.iter_mut().find(|(name, _)| *name == object) {
            Some((_, indexes)) => indexes.push(index),
            None => plan.push((object, vec![index])),
        }
    }
    Ok(Some(plan))
}

/// Field classes a token matched, as emitted by `places_transform_v1`:
/// primary and common names are 1, brand 2, category 4, and
/// locality/region/country context 8.
pub(crate) const FIELD_NAME: u8 = 1;
pub(crate) const FIELD_BRAND: u8 = 2;

/// Whether the matched fields IDENTIFY the record rather than merely describe
/// it.
///
/// A token matching a name or a brand says *this is the thing you asked for*. A
/// token matching only the category or the locality/region/country context says
/// *this is a thing of that sort, or a thing that happens to be there* — which
/// is why `q=paris` returned Dessirier, Rexel and Midas, all POIs whose sole
/// relation to the query is being located in Paris. The mask was already stored
/// and decoded on every record; it was simply never consulted when ranking.
pub(crate) fn identifying(field_mask: u8) -> bool {
    field_mask & (FIELD_NAME | FIELD_BRAND) != 0
}

/// Merge independently bounded postings without making a full producer posting
/// a false-negative gate.
///
/// Absence from a shorter posting is authoritative, while absence from a full
/// posting may only mean that the producer ordering evicted the record. Seed
/// from the bounded union, retain direct posting membership (including
/// common-name evidence that is not present in the serving projection), and
/// admit a missing full-posting token only when the record's stored display
/// fields prove it.
///
/// Identity includes the source locator because construction-v1 deliberately
/// preserves duplicate UUID rows at different source positions.
fn merge_bounded_candidates(
    tokens: &[String],
    per_token: Vec<Vec<PlacesV1Record>>,
    posting_cap: usize,
    token_cap: usize,
    lane: &str,
) -> Result<Vec<PlacesV1Record>> {
    if tokens.len() != per_token.len() {
        return Err(format!("Places v1 {lane} token/posting count differs"));
    }
    if tokens.is_empty() {
        return Ok(Vec::new());
    }
    if tokens.len() > token_cap {
        return Err(format!("Places v1 {lane} token cap exceeded"));
    }
    if per_token.iter().any(|records| records.len() > posting_cap) {
        return Err(format!("Places v1 {lane} posting exceeds producer cap"));
    }
    if per_token.iter().any(Vec::is_empty) {
        return Ok(Vec::new());
    }

    struct Candidate {
        record: PlacesV1Record,
        posting_membership: u8,
        /// OR of the field masks of every query token this record matched.
        field_mask_union: u8,
    }

    let saturated: Vec<bool> = per_token
        .iter()
        .map(|records| records.len() == posting_cap)
        .collect();
    let mut candidates: HashMap<(String, u32, u32, u64), Candidate> = HashMap::new();
    for (token_index, records) in per_token.into_iter().enumerate() {
        for record in records {
            let identity = (
                record.id.clone(),
                record.source_object_index,
                record.source_row_group,
                record.source_row_index,
            );
            let field_mask = record.field_mask;
            let candidate = candidates.entry(identity).or_insert_with(|| Candidate {
                record,
                posting_membership: 0,
                field_mask_union: 0,
            });
            candidate.posting_membership |= 1 << token_index;
            candidate.field_mask_union |= field_mask;
        }
    }

    let mut results = Vec::new();
    for candidate in candidates.into_values() {
        let mut display_tokens = None;
        let matches = tokens.iter().enumerate().all(|(token_index, token)| {
            if candidate.posting_membership & (1 << token_index) != 0 {
                return true;
            }
            if !saturated[token_index] {
                return false;
            }
            let tokens =
                display_tokens.get_or_insert_with(|| record_display_tokens(&candidate.record));
            tokens.contains(token)
        });
        if matches {
            results.push((candidate.record, candidate.field_mask_union));
        }
    }
    // Identifying matches first, then producer order within each group. A
    // record admitted only by the saturated-posting display-token fallback
    // above contributes no mask for that token, which is correct: it was not
    // in the posting, so it supplies no evidence of how it matched.
    results.sort_by(|(left, left_mask), (right, right_mask)| {
        identifying(*right_mask)
            .cmp(&identifying(*left_mask))
            .then_with(|| right.prominence_rank.cmp(&left.prominence_rank))
            .then_with(|| right.confidence_rank.cmp(&left.confidence_rank))
            .then_with(|| left.id.cmp(&right.id))
            .then_with(|| left.source_object_index.cmp(&right.source_object_index))
            .then_with(|| left.source_row_group.cmp(&right.source_row_group))
            .then_with(|| left.source_row_index.cmp(&right.source_row_index))
    });
    Ok(results.into_iter().map(|(record, _)| record).collect())
}

/// Every normalized word a record's stored display/projection fields carry.
///
/// This is the evidence the saturated-posting relaxation in
/// `merge_bounded_candidates` uses to prove a query token from a record that was
/// evicted from a full producer posting. It is the same proof the prefix-head
/// fallback needs for the tokens it dropped, so the two share one definition.
fn record_display_tokens(record: &PlacesV1Record) -> HashSet<String> {
    [
        &record.primary_name,
        &record.brand_name,
        &record.category,
        &record.locality,
        &record.region,
        &record.country,
    ]
    .into_iter()
    .flat_map(|value| crate::places_pages::query_terms(value))
    // A record's own evidence includes the COMPONENTS of a compound token, not
    // just the compound. `normalized_words` treats `_` as a word character, so
    // Overture's category vocabulary tokenizes to single tokens like
    // `train_station` that never yield `station`.
    //
    // Measured consequence on Overture 2026-07-22.0: Singapore's stations are
    // stored as `Geylang Bahru MRT` -- WITHOUT "Station" -- under category
    // `train_station`. The query `GEYLANG BAHRU MRT STATION` drives the
    // prefix-head fallback, which composes `e3:geylang bahru mrt`, finds it,
    // and retrieves the CORRECT record -- and this set then failed to prove the
    // dropped `station`, so the right answer was discarded and the response was
    // empty. Six everyday-POI cases returned nothing for exactly that reason
    // (docs/plans/2026-08-04-head-miss-interrogation.md).
    //
    // Splitting on `_` only, never on substrings: `station` is proof because it
    // is a whole component of `train_station`, while `stat` is not proof of
    // anything. Underscores are effectively a category-vocabulary separator and
    // do not occur in real display names, so this widens the evidence without
    // widening what counts as a match.
    .flat_map(|token| {
        let components: Vec<String> = if token.contains('_') {
            token
                .split('_')
                .filter(|part| !part.is_empty())
                .map(str::to_string)
                .collect()
        } else {
            Vec::new()
        };
        std::iter::once(token).chain(components)
    })
    .collect()
}

/// Split a no-proximity query too wide for the global head into the three-token
/// prefix the head can actually probe and the tail that probe drops.
///
/// `None` for anything the head lane already serves (<= 3 tokens) and for
/// anything wider than `PREFIX_HEAD_FALLBACK_TOKEN_CAP`.
pub(crate) fn prefix_head_fallback_split(tokens: &[String]) -> Option<(&[String], &[String])> {
    if !(HEAD_QUERY_TOKEN_CAP + 1..=PREFIX_HEAD_FALLBACK_TOKEN_CAP).contains(&tokens.len()) {
        return None;
    }
    Some(tokens.split_at(HEAD_QUERY_TOKEN_CAP))
}

/// Fail-closed verification for the prefix-head fallback.
///
/// A three-token prefix probe answers a question nobody asked: it proves only
/// that the record matches `X Y Z`, not that it matches `X Y Z W`. Keep a
/// candidate only when EVERY dropped token is proven by the record's own stored
/// display fields — the same proof the saturated-posting relaxation accepts, and
/// the same tokenizer on both sides. Nothing else may admit a record here; an
/// empty tail is a caller error and yields nothing rather than an unverified
/// prefix search.
pub(crate) fn retain_records_proving_dropped_tokens(
    records: Vec<PlacesV1Record>,
    dropped: &[String],
) -> Vec<PlacesV1Record> {
    if dropped.is_empty() {
        return Vec::new();
    }
    let mut verified: Vec<PlacesV1Record> = records
        .into_iter()
        .filter(|record| {
            let display = record_display_tokens(record);
            dropped.iter().all(|token| display.contains(token))
        })
        .collect();
    verified.truncate(PREFIX_HEAD_FALLBACK_RESULT_CAP);
    verified
}

/// Merge routed `(cell, token)` postings, each capped at 256 records.
pub(crate) fn merge_routed_candidates(
    tokens: &[String],
    per_token: Vec<Vec<PlacesV1Record>>,
) -> Result<Vec<PlacesV1Record>> {
    merge_bounded_candidates(tokens, per_token, ROUTED_CANDIDATE_CAP, 4, "routed")
}

/// Merge global-head token postings, each capped at 10 records.
///
/// A full head posting is lossy just like a full routed posting. Treating a
/// missing member as authoritative made common second tokens such as `tower`
/// erase records already retrieved by a selective first token such as
/// `eiffel`, even when the stored primary name proved the complete query.
pub(crate) fn merge_head_candidates(
    tokens: &[String],
    per_token: Vec<Vec<PlacesV1Record>>,
) -> Result<Vec<PlacesV1Record>> {
    merge_bounded_candidates(
        tokens,
        per_token,
        HEAD_RESULT_CAP,
        HEAD_QUERY_TOKEN_CAP,
        "head",
    )
}

/// Compose exact-primary-name phrase evidence with the ordinary global head.
///
/// Phrase postings are additive rather than terminal: the full phrase can be
/// saturated with replicas while a selective ordinary token still retrieves
/// the canonical feature, and a two-token prefix can preserve locality-suffix
/// routing for queries such as `Union Station Toronto`. If the full phrase is
/// saturated and a distinct ordinary candidate has strictly greater producer
/// prominence than its weakest row, reserve one recovery slot by dropping only
/// that weakest phrase row. This prevents ten low-value replicas from erasing a
/// canonical, prominent longer official name while retaining nine exact phrase
/// results. Preserve fetch order (full phrase, optional prefix, ordinary
/// producer rank) and deduplicate only identical source rows; construction-v1
/// deliberately keeps duplicate UUIDs at distinct source positions.
pub(crate) fn compose_entity_phrase_candidates(
    mut phrase_groups: Vec<Vec<PlacesV1Record>>,
    ordinary: Vec<PlacesV1Record>,
) -> Result<Vec<PlacesV1Record>> {
    if phrase_groups.len() > ENTITY_PHRASE_GROUP_CAP {
        return Err("Places v1 entity phrase group cap exceeded".into());
    }
    if phrase_groups
        .iter()
        .any(|records| records.len() > HEAD_RESULT_CAP)
    {
        return Err("Places v1 entity phrase posting exceeds producer cap".into());
    }
    if ordinary.len() > HEAD_QUERY_TOKEN_CAP * HEAD_RESULT_CAP {
        return Err("Places v1 composed ordinary head cap exceeded".into());
    }

    let reserve_recovery_slot = {
        let phrase_identities: HashSet<(&str, u32, u32, u64)> = phrase_groups
            .iter()
            .flatten()
            .map(|record| {
                (
                    record.id.as_str(),
                    record.source_object_index,
                    record.source_row_group,
                    record.source_row_index,
                )
            })
            .collect();
        let weakest_full_phrase_prominence = phrase_groups
            .first()
            .filter(|records| records.len() == HEAD_RESULT_CAP)
            .and_then(|records| records.last())
            .map(|record| record.prominence_rank);
        weakest_full_phrase_prominence.is_some_and(|weakest| {
            ordinary.iter().any(|record| {
                !phrase_identities.contains(&(
                    record.id.as_str(),
                    record.source_object_index,
                    record.source_row_group,
                    record.source_row_index,
                )) && record.prominence_rank > weakest
            })
        })
    };
    if reserve_recovery_slot {
        if let Some(full_phrase) = phrase_groups.first_mut() {
            full_phrase.pop();
        }
    }

    let mut seen = HashSet::new();
    let mut results =
        Vec::with_capacity(phrase_groups.iter().map(Vec::len).sum::<usize>() + ordinary.len());
    for record in phrase_groups.into_iter().flatten().chain(ordinary) {
        let identity = (
            record.id.clone(),
            record.source_object_index,
            record.source_row_group,
            record.source_row_index,
        );
        if seen.insert(identity) {
            results.push(record);
        }
    }
    Ok(results)
}

/// Synthetic exact-primary-name key shared with `places-transform-v1`.
/// `query_terms` already supplies the frozen normalized word sequence.
pub(crate) fn entity_phrase_key(tokens: &[String]) -> Option<String> {
    if !(2..=3).contains(&tokens.len()) {
        return None;
    }
    Some(format!("e{}:{}", tokens.len(), tokens.join(" ")))
}

/// Ordered exact-phrase lookups for the global head. The full phrase retains
/// direct exact-name evidence; a three-token query also probes its two-token
/// prefix so locality-suffix inference can distinguish `Union Station Toronto`
/// from a three-token landmark name.
pub(crate) fn entity_phrase_token_groups(tokens: &[String]) -> Vec<&[String]> {
    if entity_phrase_key(tokens).is_none() {
        return Vec::new();
    }
    let mut groups = vec![tokens];
    if tokens.len() == 3 {
        groups.push(&tokens[..2]);
    }
    groups
}

/// Fail closed if a malformed phrase posting does not prove its own key.
pub(crate) fn validate_entity_phrase_records(
    tokens: &[String],
    records: Vec<PlacesV1Record>,
) -> Vec<PlacesV1Record> {
    records
        .into_iter()
        .filter(|record| {
            record.prominence_rank > 0
                && record.field_mask == FIELD_NAME
                && crate::places_pages::query_terms(&record.primary_name) == tokens
        })
        .collect()
}

/// Project one construction record into the shared serving projection.
pub(crate) fn record_projection(record: &PlacesV1Record) -> PlaceProjection {
    PlaceProjection {
        id: record.id.clone(),
        latitude: record.latitude as f32,
        longitude: record.longitude as f32,
        confidence: f32::from(record.confidence_rank) / 255.0,
        prominence: f32::from(record.prominence_rank) / 255.0,
        name: record.primary_name.clone(),
        category: record.category.clone(),
        locality: record.locality.clone(),
        region: record.region.clone(),
        country: record.country.clone(),
        distance_km: None,
    }
}

// ---------------------------------------------------------------------------
// Bounded R2 loader glue. Identity trust model matches the PCSH lane: object
// identities are release-pinned and spot-verified at admission; serving-time
// integrity rests on the artifacts' structural self-checks (exact index/key
// reconciliation, misroute fail-closed) rather than per-request re-hashing.

impl ShardLoader {
    pub(crate) async fn lookup_places_construction_routing(
        &self,
        object_key: &str,
    ) -> worker::Result<Rc<PlacesRouting>> {
        if let Some(routing) = cache_get(&PLACES_ROUTING_CACHE, object_key) {
            return Ok(routing);
        }
        let read = self
            .cached_bounded_prefix_read_measured(
                object_key,
                MAX_PLACES_ROUTING_BYTES,
                IMMUTABLE_CACHE_TTL,
            )
            .await?
            .ok_or_else(|| not_found(object_key))?;
        let text = std::str::from_utf8(&read.bytes)
            .map_err(|_| worker::Error::RustError("Places routing is not UTF-8".into()))?;
        let routing = Rc::new(PlacesRouting::parse(text).map_err(worker::Error::RustError)?);
        cache_put(&PLACES_ROUTING_CACHE, object_key, Rc::clone(&routing));
        Ok(routing)
    }

    pub(crate) async fn lookup_places_construction_head_routing(
        &self,
        object_key: &str,
        head: &PlacesRoutingHead,
    ) -> worker::Result<Rc<HeadRoutingManifest>> {
        let manifest = match cache_get(&PLACES_HEAD_ROUTING_CACHE, object_key) {
            Some(manifest) => manifest,
            None => {
                let read = self
                    .cached_bounded_prefix_read_measured(
                        object_key,
                        MAX_PLACES_HEAD_ROUTING_BYTES,
                        IMMUTABLE_CACHE_TTL,
                    )
                    .await?
                    .ok_or_else(|| not_found(object_key))?;
                let text = std::str::from_utf8(&read.bytes).map_err(|_| {
                    worker::Error::RustError("Places head routing manifest is not UTF-8".into())
                })?;
                let manifest =
                    Rc::new(HeadRoutingManifest::parse(text).map_err(worker::Error::RustError)?);
                cache_put(&PLACES_HEAD_ROUTING_CACHE, object_key, Rc::clone(&manifest));
                manifest
            }
        };
        if !manifest.agrees_with(head) {
            return Err(worker::Error::RustError(
                "Places head routing manifest geometry differs from routing.json".into(),
            ));
        }
        Ok(manifest)
    }

    /// Resolve one or more exact tokens from a routed construction artifact
    /// without loading the whole `.plrv` object. The immutable artifact's
    /// canonical size comes from a one-byte suffix read; then the request reads
    /// the 32-byte header, fixed index rows, only hash-collision keys, and one
    /// bounded payload at a time. A 209 MiB planet object therefore has
    /// request residency proportional to its ~10 MiB fixed index plus one
    /// <=256-record payload, not its stored size.
    pub(crate) async fn lookup_places_construction_routed(
        &self,
        object_key: &str,
        cell: &str,
        tokens: &[String],
    ) -> worker::Result<Vec<Vec<PlacesV1Record>>> {
        if cell.len() != 4 || tokens.is_empty() || tokens.len() > 4 {
            return Err(worker::Error::RustError(
                "invalid Places ranged query shape".into(),
            ));
        }
        let (file_size, tail) = self
            .cached_suffix_read(object_key, 1)
            .await?
            .ok_or_else(|| not_found(object_key))?;
        if tail.len() != 1 {
            return Err(worker::Error::RustError(
                "Places ranged suffix length differs".into(),
            ));
        }
        let mut reader = RangeReader::new(self, object_key);
        let header_bytes = reader
            .range(0, ROUTED_HEADER_BYTES)
            .await?
            .ok_or_else(|| not_found(object_key))?;
        let header = parse_routed_range_header(&header_bytes, file_size)
            .map_err(worker::Error::RustError)?;
        let fixed_bytes = reader
            .range(
                header.index_offset,
                header
                    .fixed_index_bytes()
                    .map_err(worker::Error::RustError)?,
            )
            .await?
            .ok_or_else(|| not_found(object_key))?;
        let index = parse_routed_range_index(&fixed_bytes, &header, file_size)
            .map_err(worker::Error::RustError)?;
        drop(fixed_bytes);

        let query_keys: Vec<Vec<u8>> = tokens
            .iter()
            .map(|token| query_key(PlacesV1Mode::Routed, Some(cell), token))
            .collect();
        let candidates = query_keys
            .iter()
            .map(|key| ranged_index_candidates(&index, key))
            .collect::<Result<Vec<_>>>()
            .map_err(worker::Error::RustError)?;
        let key_table_offset = header
            .key_table_offset()
            .map_err(worker::Error::RustError)?;
        let mut wants = Vec::new();
        let mut owners = Vec::new();
        for (token_index, token_candidates) in candidates.iter().enumerate() {
            for (candidate_index, candidate) in token_candidates.iter().enumerate() {
                wants.push(ByteRange {
                    offset: key_table_offset
                        .checked_add(candidate.key_position)
                        .ok_or_else(|| {
                            worker::Error::RustError("Places ranged key offset overflows".into())
                        })?,
                    length: candidate.key_length,
                });
                owners.push((token_index, candidate_index));
            }
        }
        let key_bytes = if wants.is_empty() {
            Vec::new()
        } else {
            reader
                .coalesced(&wants, 0, MAX_ROUTED_INDEX_KEY_ENTRY_BYTES)
                .await?
        };
        let mut selected: Vec<Option<&PlacesV1RangeIndex>> =
            (0..tokens.len()).map(|_| None).collect();
        for ((token_index, candidate_index), bytes) in owners.into_iter().zip(key_bytes) {
            let candidate = candidates[token_index][candidate_index];
            if index_hash(&bytes) != candidate.hash {
                return Err(worker::Error::RustError(
                    "Places ranged index key hash differs".into(),
                ));
            }
            if bytes.as_ref() == query_keys[token_index].as_slice()
                && selected[token_index].replace(candidate).is_some()
            {
                return Err(worker::Error::RustError(
                    "Places ranged index repeats a query key".into(),
                ));
            }
        }

        let mut output = Vec::with_capacity(tokens.len());
        for (token_index, match_entry) in selected.into_iter().enumerate() {
            let Some(selected) = match_entry else {
                output.push(Vec::new());
                continue;
            };
            // Validate the producer-derived count/byte envelope BEFORE asking
            // R2 or the edge cache to materialize the selected range. A corrupt
            // index can therefore never turn this bounded lookup into a
            // multi-gigabyte allocation inside the 128 MiB isolate.
            validate_routed_range_payload_extent(selected).map_err(worker::Error::RustError)?;
            let payload = reader
                .range(selected.payload_offset, selected.payload_bytes)
                .await?
                .ok_or_else(|| not_found(object_key))?;
            output.push(
                decode_routed_range_payload(
                    &payload,
                    selected,
                    cell,
                    &tokens[token_index],
                    header.version,
                )
                .map_err(worker::Error::RustError)?,
            );
        }
        Ok(output)
    }

    /// Fetch one whole serving artifact under a hard byte cap, edge-cached
    /// with the immutable TTL like every other content-addressed object.
    pub(crate) async fn places_construction_object(
        &self,
        object_key: &str,
        max_bytes: usize,
    ) -> worker::Result<bytes::Bytes> {
        let read = self
            .cached_bounded_prefix_read_measured(object_key, max_bytes, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(object_key))?;
        Ok(read.bytes)
    }
}

/// One thread-local generation of parsed immutable control documents.
pub(crate) type ParsedDocumentCache<T> = std::thread::LocalKey<RefCell<Vec<(String, Rc<T>)>>>;

pub(crate) fn cache_get<T>(cache: &'static ParsedDocumentCache<T>, key: &str) -> Option<Rc<T>> {
    cache.with(|cache| {
        let mut cache = cache.borrow_mut();
        cache
            .iter()
            .position(|(cached_key, _)| cached_key == key)
            .map(|position| {
                let entry = cache.remove(position);
                let value = Rc::clone(&entry.1);
                cache.push(entry);
                value
            })
    })
}

pub(crate) fn cache_put<T>(cache: &'static ParsedDocumentCache<T>, key: &str, value: Rc<T>) {
    const MAX_ENTRIES: usize = 1;
    cache.with(|cache| {
        let mut cache = cache.borrow_mut();
        if !cache.iter().any(|(cached_key, _)| cached_key == key) {
            cache.push((key.to_string(), value));
            while cache.len() > MAX_ENTRIES {
                cache.remove(0);
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::{
        compose_entity_phrase_candidates, construction_cell, decode_routed_range_payload,
        entity_phrase_key, entity_phrase_token_groups, head_shard_id, head_shard_lookup,
        index_hash, lookup_head_shard, merge_head_candidates, merge_routed_candidates,
        parse_routed_range_header, parse_routed_range_index, prefix_head_fallback_split, query_key,
        ranged_index_candidates, record_projection, retain_records_proving_dropped_tokens,
        routed_fetch_plan, routed_lookup, routed_token_hash, validate_entity_phrase_records,
        validate_routed_range_payload_extent, HeadRoutingManifest, PlacesRouting, PlacesV1Artifact,
        PlacesV1Mode, PlacesV1RangeIndex, PlacesV1Record, PlacesV1Version, ENTITY_PHRASE_ADMISSION,
        MAX_ARTIFACT_ENTRY_BYTES, PLACES_HEAD_MANIFEST_SCHEMA, PLACES_ROUTING_SCHEMA,
    };
    use serde_json::{json, Value};
    use sha2::{Digest, Sha256};
    use std::collections::HashMap;

    fn text(output: &mut Vec<u8>, value: &str) {
        output.extend_from_slice(&(value.len() as u16).to_le_bytes());
        output.extend_from_slice(value.as_bytes());
    }

    fn entry(token: &str, cell: Option<&str>, rank: u8, id: u128, row: u64) -> Vec<u8> {
        entry_masked(token, cell, super::FIELD_NAME, rank, id, row)
    }

    /// `entry` with an explicit field mask: 1 name, 2 brand, 4 category,
    /// 8 locality/region/country context.
    fn entry_masked(
        token: &str,
        cell: Option<&str>,
        field_mask: u8,
        rank: u8,
        id: u128,
        row: u64,
    ) -> Vec<u8> {
        let mut output = Vec::new();
        text(&mut output, token);
        if let Some(cell) = cell {
            text(&mut output, cell);
        }
        output.extend_from_slice(&[field_mask, rank, 0]);
        output.extend_from_slice(&id.to_be_bytes());
        output.extend_from_slice(&1.25_f64.to_le_bytes());
        output.extend_from_slice(&2.5_f64.to_le_bytes());
        output.extend_from_slice(&3_u32.to_le_bytes());
        output.extend_from_slice(&4_u32.to_le_bytes());
        output.extend_from_slice(&row.to_le_bytes());
        for value in ["Cafe", "", "restaurant", "Town", "Region", "XX"] {
            text(&mut output, value);
        }
        output
    }

    fn artifact(mode: PlacesV1Mode, entries: &[Vec<u8>]) -> Vec<u8> {
        artifact_versioned(mode, entries, PlacesV1Version::V3)
    }

    fn artifact_versioned(
        mode: PlacesV1Mode,
        entries: &[Vec<u8>],
        version: PlacesV1Version,
    ) -> Vec<u8> {
        let magic = mode
            .magics()
            .into_iter()
            .find(|(_, candidate)| *candidate == version)
            .map(|(magic, _)| magic)
            .expect("mode has this version");
        let mut output = magic.to_vec();
        output.extend_from_slice(&(entries.len() as u64).to_le_bytes());
        output.extend_from_slice(&0_u64.to_le_bytes());
        output.extend_from_slice(&0_u32.to_le_bytes());
        output.extend_from_slice(&0_u32.to_le_bytes());
        let mut index: Vec<(u64, Vec<u8>, u64, u64, u32)> = Vec::new();
        for entry in entries {
            let mut at = 0;
            let token_length = u16::from_le_bytes(entry[at..at + 2].try_into().unwrap()) as usize;
            at += 2;
            let token = &entry[at..at + token_length];
            at += token_length;
            let mut key = Vec::new();
            if mode == PlacesV1Mode::Routed {
                let cell_length =
                    u16::from_le_bytes(entry[at..at + 2].try_into().unwrap()) as usize;
                at += 2;
                key.extend_from_slice(&entry[at..at + cell_length]);
                key.push(0);
            }
            key.extend_from_slice(token);
            let payload_offset = output.len() as u64;
            output.extend_from_slice(&(entry.len() as u32).to_le_bytes());
            output.extend_from_slice(entry);
            let encoded = 4 + entry.len() as u64;
            if index.last().is_some_and(|item| item.1 == key) {
                let active = index.last_mut().unwrap();
                active.3 += encoded;
                active.4 += 1;
            } else {
                index.push((index_hash(&key), key, payload_offset, encoded, 1));
            }
        }
        let index_offset = output.len() as u64;
        index.sort_by(|left, right| (left.0, &left.1).cmp(&(right.0, &right.1)));
        output.extend_from_slice(&(index.len() as u32).to_le_bytes());
        let mut key_offset = 0_u64;
        for item in &index {
            output.extend_from_slice(&item.0.to_le_bytes());
            output.extend_from_slice(&key_offset.to_le_bytes());
            output.extend_from_slice(&(item.1.len() as u32).to_le_bytes());
            output.extend_from_slice(&item.4.to_le_bytes());
            output.extend_from_slice(&item.2.to_le_bytes());
            output.extend_from_slice(&item.3.to_le_bytes());
            key_offset += item.1.len() as u64;
        }
        for item in &index {
            output.extend_from_slice(&item.1);
        }
        output[16..24].copy_from_slice(&index_offset.to_le_bytes());
        output[24..28].copy_from_slice(&(index.len() as u32).to_le_bytes());
        output
    }

    fn ranged_lookup_from_bytes(
        bytes: &[u8],
        cell: &str,
        token: &str,
    ) -> Result<Vec<PlacesV1Record>, String> {
        let header = parse_routed_range_header(&bytes[..32], bytes.len() as u64)?;
        let fixed_start = header.index_offset as usize;
        let fixed_end = fixed_start + header.fixed_index_bytes()? as usize;
        let index =
            parse_routed_range_index(&bytes[fixed_start..fixed_end], &header, bytes.len() as u64)?;
        let key = query_key(PlacesV1Mode::Routed, Some(cell), token);
        let key_table = header.key_table_offset()? as usize;
        let mut selected = None;
        for candidate in ranged_index_candidates(&index, &key)? {
            let start = key_table + candidate.key_position as usize;
            let end = start + candidate.key_length as usize;
            if bytes[start..end] == key && selected.replace(candidate).is_some() {
                return Err("fixture range index repeats a query key".into());
            }
        }
        let Some(selected) = selected else {
            return Ok(Vec::new());
        };
        let start = selected.payload_offset as usize;
        let end = start + selected.payload_bytes as usize;
        decode_routed_range_payload(&bytes[start..end], selected, cell, token, header.version)
    }

    #[test]
    fn decodes_and_queries_routed_and_head_bytes_with_caps() {
        let routed = artifact(
            PlacesV1Mode::Routed,
            &[
                entry("cafe", Some("8080"), 255, 1, 0),
                entry("cafe", Some("8080"), 200, 2, 1),
                entry("town", Some("8080"), 255, 1, 0),
            ],
        );
        let parsed =
            PlacesV1Artifact::parse(&routed, PlacesV1Mode::Routed, 64 * 1024, 10, 1024).unwrap();
        let hits = parsed.lookup("cafe", Some("8080"), 2, 2).unwrap();
        assert_eq!(hits.len(), 2);
        assert_eq!(hits[0].confidence_rank, 255);
        assert_eq!(hits[0].primary_name, "Cafe");
        assert_eq!(hits[0].source_object_index, 3);
        assert!(parsed.lookup("cafe", Some("8080"), 1, 1).is_err());

        let head = artifact(PlacesV1Mode::Head, &[entry("cafe", None, 255, 1, 0)]);
        let parsed = PlacesV1Artifact::parse(&head, PlacesV1Mode::Head, 4096, 2, 1024).unwrap();
        assert_eq!(parsed.lookup("cafe", None, 1, 1).unwrap().len(), 1);
        assert!(parsed.lookup("cafe", Some("8080"), 1, 1).is_err());
    }

    #[test]
    fn ranged_routed_lookup_matches_whole_object_decode_without_a_whole_object_cap() {
        let routed = artifact(
            PlacesV1Mode::Routed,
            &[
                entry("cafe", Some("8080"), 255, 1, 0),
                entry("cafe", Some("8080"), 200, 2, 1),
                entry("town", Some("8080"), 255, 1, 0),
            ],
        );
        let whole = routed_lookup(&routed, "8080", "cafe").unwrap();
        let ranged = ranged_lookup_from_bytes(&routed, "8080", "cafe").unwrap();
        assert_eq!(ranged, whole);
        assert!(ranged_lookup_from_bytes(&routed, "8080", "missing")
            .unwrap()
            .is_empty());

        // The old serving shape fails as soon as the artifact is one byte over
        // its materialization cap. The ranged shape uses the producer/file
        // envelope and fetches only directory rows plus the selected payload.
        assert!(
            PlacesV1Artifact::parse(&routed, PlacesV1Mode::Routed, routed.len() - 1, 10, 1024,)
                .is_err()
        );
        assert_eq!(ranged.len(), 2);
    }

    #[test]
    fn ranged_routed_lookup_rejects_size_index_and_payload_lies() {
        let routed = artifact(
            PlacesV1Mode::Routed,
            &[entry("cafe", Some("8080"), 255, 1, 0)],
        );
        assert!(
            parse_routed_range_header(&routed[..32], routed.len() as u64 + 1)
                .and_then(|header| {
                    let start = header.index_offset as usize;
                    let end = start + header.fixed_index_bytes()? as usize;
                    parse_routed_range_index(&routed[start..end], &header, routed.len() as u64 + 1)
                        .map(|_| ())
                })
                .is_err()
        );

        let index_offset = u64::from_le_bytes(routed[16..24].try_into().unwrap()) as usize;
        let mut wrong_count = routed.clone();
        wrong_count[index_offset..index_offset + 4].copy_from_slice(&2_u32.to_le_bytes());
        assert!(ranged_lookup_from_bytes(&wrong_count, "8080", "cafe").is_err());

        let mut trailing = routed.clone();
        trailing.push(0);
        assert!(ranged_lookup_from_bytes(&trailing, "8080", "cafe").is_err());

        let mut wrong_key = routed.clone();
        let key_at = index_offset + 4 + 40;
        wrong_key[key_at] ^= 1;
        assert!(ranged_lookup_from_bytes(&wrong_key, "8080", "cafe")
            .unwrap()
            .is_empty());
    }

    #[test]
    fn ranged_payload_extent_is_bounded_before_any_body_read() {
        let entry = PlacesV1RangeIndex {
            hash: 0,
            key_position: 0,
            key_length: 1,
            records: 1,
            payload_offset: 32,
            payload_bytes: 5 + MAX_ARTIFACT_ENTRY_BYTES as u64,
        };
        assert!(validate_routed_range_payload_extent(&entry).is_err());

        let too_many = PlacesV1RangeIndex {
            records: 257,
            payload_bytes: 4,
            ..entry
        };
        assert!(validate_routed_range_payload_extent(&too_many).is_err());
    }

    #[test]
    fn head_shard_id_matches_top_index_hash_bits() {
        for token in ["cafe", "tokyo tower", "restaurant", "東京", "a"] {
            let hash = index_hash(token.as_bytes());
            assert_eq!(head_shard_id(token, 12), (hash >> 52) as u32);
            assert!(head_shard_id(token, 12) < 4096);
            assert_eq!(head_shard_id(token, 4), (hash >> 60) as u32);
        }
    }

    #[test]
    fn resolves_token_through_its_head_shard() {
        // Build a distinct single-entry head artifact per shard, then serve each
        // token only from the shard its index hash addresses.
        let shard_bits = 4;
        let tokens = ["cafe", "town", "restaurant", "museum", "park", "bakery"];
        let mut by_shard: std::collections::BTreeMap<u32, Vec<(&str, Vec<u8>)>> =
            std::collections::BTreeMap::new();
        for (rank, token) in tokens.iter().enumerate() {
            let shard = head_shard_id(token, shard_bits);
            by_shard
                .entry(shard)
                .or_default()
                .push((token, entry(token, None, 255 - rank as u8, 1, 0)));
        }
        for (shard_id, mut entries) in by_shard {
            // Head payload order is by (token, …); sort so parse accepts it.
            entries.sort_by(|left, right| left.0.cmp(right.0));
            let bytes = artifact(
                PlacesV1Mode::Head,
                &entries.iter().map(|(_, e)| e.clone()).collect::<Vec<_>>(),
            );
            for (token, _) in &entries {
                let hits = lookup_head_shard(
                    &bytes,
                    shard_id,
                    shard_bits,
                    token,
                    64 * 1024,
                    100,
                    1024,
                    10,
                    5,
                )
                .unwrap();
                assert_eq!(hits.len(), 1);
                assert_eq!(&hits[0].token, token);
                // A token routed to the wrong shard fails closed rather than
                // silently missing.
                let wrong = (shard_id + 1) % (1 << shard_bits);
                if head_shard_id(token, shard_bits) != wrong {
                    assert!(lookup_head_shard(
                        &bytes,
                        wrong,
                        shard_bits,
                        token,
                        64 * 1024,
                        100,
                        1024,
                        10,
                        5
                    )
                    .is_err());
                }
            }
        }
    }

    /// Local-decoder evidence over real, locally-built head shards.
    ///
    /// This is the honest, no-network `worker_local_decoder_evidence` class the
    /// readiness validator accepts in place of a deployed Worker: it exercises
    /// the *actual* Worker head-shard decode + lookup path
    /// (`lookup_head_shard`, including the mis-route fail-closed check) against
    /// the `.plhd` bytes produced by the census sharded-head build. It is
    /// `#[ignore]`d so normal `cargo test` skips it; the rehearsal runs it with
    /// `--ignored` after setting the sample environment.
    ///
    /// `PLACES_HEAD_SHARD_BITS` — head manifest `shard_bits`.
    /// `PLACES_HEAD_SAMPLES` — `token\tshard_id\tpath` rows joined by `\n`.
    /// `PLACES_HEAD_EVIDENCE_OUT` — path to write a small JSON evidence summary.
    #[test]
    #[ignore = "requires locally-built head shards; driven by the rehearsal"]
    fn local_decoder_resolves_real_head_shards() {
        use super::{
            entity_phrase_key, head_shard_id, lookup_head_shard, validate_entity_phrase_records,
        };
        let shard_bits: u32 = std::env::var("PLACES_HEAD_SHARD_BITS")
            .expect("PLACES_HEAD_SHARD_BITS is required")
            .parse()
            .expect("shard bits must parse");
        let samples =
            std::env::var("PLACES_HEAD_SAMPLES").expect("PLACES_HEAD_SAMPLES is required");
        let rows: Vec<&str> = samples.lines().filter(|line| !line.is_empty()).collect();
        assert!(!rows.is_empty(), "no head-shard samples supplied");
        let mut resolved = 0usize;
        let mut misroute_rejected = 0usize;
        let mut entity_phrase_records_validated = 0usize;
        for row in &rows {
            let mut parts = row.split('\t');
            let token = parts.next().expect("token");
            let shard_id: u32 = parts
                .next()
                .expect("shard id")
                .parse()
                .expect("shard id parses");
            let path = parts.next().expect("path");
            // The decoder itself must agree the token addresses this shard.
            assert_eq!(
                head_shard_id(token, shard_bits),
                shard_id,
                "sample shard id disagrees with the decoder"
            );
            let bytes = std::fs::read(path).expect("read head shard bytes");
            let hits = lookup_head_shard(
                &bytes,
                shard_id,
                shard_bits,
                token,
                64 * 1024 * 1024,
                5_000_000,
                64 * 1024,
                256,
                10,
            )
            .expect("real head shard must decode and resolve the token");
            assert!(!hits.is_empty(), "token {token} resolved zero head records");
            assert_eq!(&hits[0].token, token, "decoded head record token differs");
            if token.starts_with("e2:") || token.starts_with("e3:") {
                let words: Vec<String> = token[3..].split(' ').map(ToString::to_string).collect();
                assert_eq!(entity_phrase_key(&words).as_deref(), Some(token));
                let valid = validate_entity_phrase_records(&words, hits);
                assert!(
                    !valid.is_empty(),
                    "entity phrase {token} has no contract-valid records"
                );
                entity_phrase_records_validated += valid.len();
            }
            resolved += 1;
            // Mis-route fail-closed: the same bytes served under any other shard
            // id must be rejected rather than silently answered.
            let wrong = (shard_id + 1) % (1u32 << shard_bits);
            if wrong != shard_id {
                assert!(
                    lookup_head_shard(
                        &bytes,
                        wrong,
                        shard_bits,
                        token,
                        64 * 1024 * 1024,
                        5_000_000,
                        64 * 1024,
                        256,
                        10,
                    )
                    .is_err(),
                    "mis-routed head-shard fetch for {token} was not rejected"
                );
                misroute_rejected += 1;
            }
        }
        if let Ok(out) = std::env::var("PLACES_HEAD_EVIDENCE_OUT") {
            let json = format!(
                "{{\"class\":\"worker_local_decoder_evidence\",\"decoder\":\"geocoder-worker::places_construction_v1::lookup_head_shard\",\"shard_bits\":{shard_bits},\"tokens_resolved\":{resolved},\"misroute_rejections\":{misroute_rejected},\"entity_phrase_records_validated\":{entity_phrase_records_validated}}}"
            );
            std::fs::write(out, json).expect("write head decoder evidence");
        }
    }

    #[test]
    fn rejects_corruption_order_regression_and_header_lies() {
        let mut corrupt_index = artifact(
            PlacesV1Mode::Routed,
            &[
                entry("town", Some("8080"), 255, 1, 0),
                entry("cafe", Some("8080"), 255, 1, 0),
            ],
        );
        corrupt_index[28] = 1;
        assert!(
            PlacesV1Artifact::parse(&corrupt_index, PlacesV1Mode::Routed, 4096, 10, 1024).is_err()
        );

        let mut truncated = artifact(PlacesV1Mode::Head, &[entry("cafe", None, 255, 1, 0)]);
        truncated.pop();
        assert!(PlacesV1Artifact::parse(&truncated, PlacesV1Mode::Head, 4096, 10, 1024).is_err());
    }

    // -----------------------------------------------------------------------
    // Promoted-slice routing and serving pipeline.

    fn sha_hex(bytes: &[u8]) -> String {
        format!("{:x}", Sha256::digest(bytes))
    }

    /// Cross-implementation pin: byte-identical to `token_hash` in
    /// `places_transform_v1.rs` (values computed independently with Python
    /// `hashlib` over the `overture-places-token-partition-v1\0` domain).
    #[test]
    fn routed_token_hash_matches_the_construction_transform() {
        assert_eq!(routed_token_hash("cafe"), 0x1440_127e_7afa_2247);
        assert_eq!(routed_token_hash("tower"), 0x28ee_0390_2490_ca39);
        assert_eq!(routed_token_hash("museum"), 0x9724_4ca3_f765_613e);
        assert_eq!(routed_token_hash("東京"), 0x0635_792b_4bf5_adb4);
    }

    /// The bias-point cell derivation must re-encode the exact `(y<<8)|x` grid
    /// the construction partitioner uses, pinned by the shared PR #187 cell
    /// identifier vectors (quadkey8 <-> 4-hex `{y:02x}{x:02x}` cell).
    #[test]
    fn construction_cell_matches_the_shared_identifier_vectors() {
        let fixture = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/fixtures/reverse/cell-identifier-vectors-v1.json");
        let payload: Value =
            serde_json::from_str(&std::fs::read_to_string(&fixture).unwrap()).unwrap();
        assert_eq!(
            payload["schema"].as_str(),
            Some("overture-reverse-cell-identifier-vectors-v1")
        );
        let vectors = payload["vectors"].as_array().unwrap();
        assert!(vectors.len() >= 300);
        for vector in vectors {
            let longitude = vector["longitude_e7"].as_i64().unwrap() as f64 / 1e7;
            let latitude = vector["latitude_e7"].as_i64().unwrap() as f64 / 1e7;
            assert_eq!(
                construction_cell(longitude, latitude).as_deref(),
                vector["partition_cell"].as_str(),
                "cell mismatch at ({longitude}, {latitude})"
            );
        }
    }

    fn head_block(populated_shards: usize, manifest_object: &str) -> Value {
        json!({
            "schema": PLACES_HEAD_MANIFEST_SCHEMA,
            "shard_bits": 4,
            "shard_count": 16,
            "populated_shards": populated_shards,
            "manifest_object": manifest_object,
        })
    }

    fn routing_json(cells: Value, head: Value) -> String {
        json!({
            "schema": PLACES_ROUTING_SCHEMA,
            "family": "places",
            "cell_scheme": "level-4-quadkey-yx-hex",
            "subpartition_scheme": "token-sha256-nibble-prefix-v1",
            "cells": cells,
            "head": head,
        })
        .to_string()
    }

    fn plrv_name(bytes: &[u8]) -> String {
        format!("{}.plrv", sha_hex(bytes))
    }

    /// A minimal promoted slice: one unsplit cell, one depth-1 split cell whose
    /// sixteen subpartitions tile the nibble space, an unpopulated rest of the
    /// world, and a 16-way sharded head with its routing manifest.
    struct PromotedSlice {
        store: HashMap<String, Vec<u8>>,
        routing: PlacesRouting,
        head: HeadRoutingManifest,
        unsplit_cell: String,
        split_cell: String,
    }

    fn promoted_slice() -> PromotedSlice {
        // Boston-ish (unsplit) and Paris-ish (split) bias points.
        let unsplit_cell = construction_cell(-71.06, 42.36).unwrap();
        let split_cell = construction_cell(2.35, 48.86).unwrap();
        assert_ne!(unsplit_cell, split_cell);
        let mut store = HashMap::new();

        // Unsplit cell: "cafe" -> {1, 2}, "town" -> {2, 3}; intersection is 2.
        let unsplit_bytes = artifact(
            PlacesV1Mode::Routed,
            &[
                entry("cafe", Some(&unsplit_cell), 255, 1, 0),
                entry("cafe", Some(&unsplit_cell), 200, 2, 1),
                entry("town", Some(&unsplit_cell), 250, 2, 1),
                entry("town", Some(&unsplit_cell), 240, 3, 2),
            ],
        );
        let unsplit_name = plrv_name(&unsplit_bytes);
        store.insert(unsplit_name.clone(), unsplit_bytes);

        // Split cell at depth 1: "cafe" (nibble 1) and "tower" (nibble 2) live
        // in their owning subpartitions and share feature id 7; the other
        // fourteen subpartitions are valid empty artifacts.
        let mut split_entries = Vec::new();
        for nibble in 0..16_u64 {
            let members: Vec<Vec<u8>> = match nibble {
                1 => vec![
                    entry("cafe", Some(&split_cell), 255, 7, 0),
                    entry("cafe", Some(&split_cell), 254, 8, 1),
                ],
                2 => vec![entry("tower", Some(&split_cell), 255, 7, 0)],
                _ => Vec::new(),
            };
            let bytes = artifact(PlacesV1Mode::Routed, &members);
            let name = plrv_name(&bytes);
            store.insert(name.clone(), bytes);
            split_entries.push(json!([format!("{nibble:x}"), name]));
        }
        assert_eq!(routed_token_hash("cafe") >> 60, 1);
        assert_eq!(routed_token_hash("tower") >> 60, 2);

        // Sharded head over "cafe" and "town" at shard_bits 4.
        let mut by_shard: std::collections::BTreeMap<u32, Vec<Vec<u8>>> =
            std::collections::BTreeMap::new();
        for (token, id) in [("cafe", 21_u128), ("town", 22)] {
            by_shard
                .entry(head_shard_id(token, 4))
                .or_default()
                .push(entry(token, None, 255, id, 0));
        }
        let mut shard_rows = Vec::new();
        for (shard_id, entries) in by_shard {
            let bytes = artifact(PlacesV1Mode::Head, &entries);
            let name = format!("{}.plhd", sha_hex(&bytes));
            shard_rows.push(json!({
                "shard_id": shard_id,
                "path": name,
                "sha256": sha_hex(&bytes),
                "bytes": bytes.len(),
            }));
            store.insert(name, bytes);
        }
        let head_manifest_text = json!({
            "schema": PLACES_HEAD_MANIFEST_SCHEMA,
            "shard_bits": 4,
            "shard_count": 16,
            "populated_shards": shard_rows.len(),
            "result_cap": 10,
            "shards": shard_rows,
        })
        .to_string();
        let head_manifest_name = format!("{}.json", sha_hex(head_manifest_text.as_bytes()));
        let populated = store.keys().filter(|key| key.ends_with(".plhd")).count();
        store.insert(
            head_manifest_name.clone(),
            head_manifest_text.clone().into_bytes(),
        );

        let mut cells = serde_json::Map::new();
        cells.insert(unsplit_cell.clone(), json!([["", unsplit_name]]));
        cells.insert(split_cell.clone(), Value::Array(split_entries));
        let routing_text = routing_json(
            Value::Object(cells),
            head_block(populated, &head_manifest_name),
        );
        let routing = PlacesRouting::parse(&routing_text).unwrap();
        let head = HeadRoutingManifest::parse(&head_manifest_text).unwrap();
        assert!(head.agrees_with(&routing.head));
        PromotedSlice {
            store,
            routing,
            head,
            unsplit_cell,
            split_cell,
        }
    }

    /// The pure serving pipeline for the proximity lane, mirroring
    /// `search_places_construction` step for step over a mock store: one plan,
    /// one live object per plan entry, bytes dropped before the next fetch.
    fn routed_query(
        slice: &PromotedSlice,
        longitude: f64,
        latitude: f64,
        tokens: &[&str],
        limit: usize,
    ) -> Result<Vec<PlacesV1Record>, String> {
        let cell = construction_cell(longitude, latitude).unwrap();
        let tokens: Vec<String> = tokens.iter().map(|token| token.to_string()).collect();
        let Some(plan) = routed_fetch_plan(&slice.routing, &cell, &tokens)? else {
            return Ok(Vec::new());
        };
        let mut per_token: Vec<Option<Vec<PlacesV1Record>>> =
            (0..tokens.len()).map(|_| None).collect();
        for (object, token_indexes) in plan {
            let bytes = slice.store.get(object).expect("routed object is published");
            for index in token_indexes {
                let records = routed_lookup(bytes, &cell, &tokens[index])?;
                if records.is_empty() {
                    return Ok(Vec::new());
                }
                per_token[index] = Some(records);
            }
        }
        let per_token = per_token
            .into_iter()
            .map(|records| records.expect("plan covers every token"))
            .collect();
        let mut records = merge_routed_candidates(&tokens, per_token)?;
        records.truncate(limit);
        Ok(records)
    }

    /// Pins the residency-bound fetch policy: the plan holds one entry per
    /// DISTINCT routed object in first-use order, every token index exactly
    /// once, so the serving loop fetches each object once and never keeps more
    /// than one routed artifact's bytes alive at a time.
    #[test]
    fn routed_fetch_plan_groups_tokens_one_object_at_a_time() {
        let slice = promoted_slice();

        // Split cell: "cafe" and "tower" live in different subpartitions, so a
        // two-token query is two sequential single-object fetches.
        let tokens: Vec<String> = vec!["cafe".into(), "tower".into()];
        let plan = routed_fetch_plan(&slice.routing, &slice.split_cell, &tokens)
            .unwrap()
            .unwrap();
        assert_eq!(plan.len(), 2);
        assert_ne!(plan[0].0, plan[1].0);
        assert_eq!(plan[0].1, vec![0]);
        assert_eq!(plan[1].1, vec![1]);

        // Unsplit cell: both tokens share the one object, which therefore
        // appears once and is fetched once.
        let tokens: Vec<String> = vec!["cafe".into(), "town".into()];
        let plan = routed_fetch_plan(&slice.routing, &slice.unsplit_cell, &tokens)
            .unwrap()
            .unwrap();
        assert_eq!(plan.len(), 1);
        assert_eq!(plan[0].1, vec![0, 1]);

        // Four tokens over the split cell still plan at most one entry per
        // distinct object, with every token index covered exactly once.
        let tokens: Vec<String> = vec!["cafe".into(), "tower".into(), "cafe".into(), "東京".into()];
        let plan = routed_fetch_plan(&slice.routing, &slice.split_cell, &tokens)
            .unwrap()
            .unwrap();
        let mut objects: Vec<&str> = plan.iter().map(|(object, _)| *object).collect();
        objects.sort_unstable();
        objects.dedup();
        assert_eq!(objects.len(), plan.len(), "plan repeats an object");
        let mut covered: Vec<usize> = plan.iter().flat_map(|(_, ids)| ids.clone()).collect();
        covered.sort_unstable();
        assert_eq!(covered, vec![0, 1, 2, 3]);

        // An unpopulated cell yields no plan at all.
        let unpopulated = construction_cell(151.2, -33.87).unwrap();
        assert!(routed_fetch_plan(&slice.routing, &unpopulated, &tokens)
            .unwrap()
            .is_none());
    }

    /// The pure serving pipeline for the head lane over a mock store.
    fn head_query(
        slice: &PromotedSlice,
        tokens: &[&str],
        limit: usize,
    ) -> Result<Vec<PlacesV1Record>, String> {
        let tokens: Vec<String> = tokens.iter().map(|token| token.to_string()).collect();
        let mut per_token = Vec::new();
        for token in &tokens {
            let shard_id = head_shard_id(token, slice.head.shard_bits);
            let Some(shard) = slice.head.shard(shard_id) else {
                return Ok(Vec::new());
            };
            let bytes = slice
                .store
                .get(&shard.path)
                .expect("head shard is published");
            let records = head_shard_lookup(bytes, shard_id, slice.head.shard_bits, token)?;
            if records.is_empty() {
                return Ok(Vec::new());
            }
            per_token.push(records);
        }
        let mut records = merge_head_candidates(&tokens, per_token)?;
        records.truncate(limit);
        Ok(records)
    }

    #[test]
    fn routed_lane_serves_split_unsplit_and_unpopulated_cells() {
        let slice = promoted_slice();

        // Unsplit cell: single token is served in producer rank order.
        let hits = routed_query(&slice, -71.06, 42.36, &["cafe"], 10).unwrap();
        assert_eq!(hits.len(), 2);
        assert_eq!(hits[0].confidence_rank, 255);
        assert_eq!(
            hits[0].partition_cell.as_deref(),
            Some(slice.unsplit_cell.as_str())
        );

        // Unsplit cell: two-token AND keeps only the shared feature.
        let hits = routed_query(&slice, -71.06, 42.36, &["cafe", "town"], 10).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].id, format_uuid_of(2));

        // Split cell: each token resolves through its own nibble subpartition,
        // and the cross-object AND keeps the shared feature.
        let hits = routed_query(&slice, 2.35, 48.86, &["cafe"], 10).unwrap();
        assert_eq!(hits.len(), 2);
        let hits = routed_query(&slice, 2.35, 48.86, &["cafe", "tower"], 10).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].id, format_uuid_of(7));

        // A token whose owning subpartition is empty resolves to no results.
        assert!(routed_query(&slice, 2.35, 48.86, &["museum"], 10)
            .unwrap()
            .is_empty());

        // A bias point in an unpopulated cell is empty, not an error.
        assert!(routed_query(&slice, 151.2, -33.87, &["cafe"], 10)
            .unwrap()
            .is_empty());

        // The projection carries the construction confidence and identity.
        let hits = routed_query(&slice, -71.06, 42.36, &["cafe"], 10).unwrap();
        let projection = record_projection(&hits[0]);
        assert_eq!(projection.id, hits[0].id);
        assert!((projection.confidence - 1.0).abs() < f32::EPSILON);
        assert_eq!(projection.name, "Cafe");
    }

    fn format_uuid_of(id: u128) -> String {
        geocoder_core::pages::format_uuid(id.to_be_bytes())
    }

    #[test]
    fn head_lane_resolves_tokens_and_preserves_misroute_fail_closed() {
        let slice = promoted_slice();
        let hits = head_query(&slice, &["cafe"], 10).unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].token, "cafe");

        // Distinct-feature two-token AND is empty (no shared id in the head).
        assert!(head_query(&slice, &["cafe", "town"], 10)
            .unwrap()
            .is_empty());

        // A token whose shard is unpopulated resolves empty.
        let absent = ["bakery", "museum", "park", "harbor"]
            .into_iter()
            .find(|token| slice.head.shard(head_shard_id(token, 4)).is_none());
        if let Some(token) = absent {
            assert!(head_query(&slice, &[token], 10).unwrap().is_empty());
        }

        // The decoder still rejects the same bytes under any other shard id.
        let shard_id = head_shard_id("cafe", 4);
        let shard = slice.head.shard(shard_id).unwrap();
        let bytes = slice.store.get(&shard.path).unwrap();
        let wrong = (shard_id + 1) % 16;
        assert!(head_shard_lookup(bytes, wrong, 4, "cafe").is_err());
    }

    #[test]
    fn head_merge_keeps_producer_rank_order() {
        let first = vec![
            entry_record("cafe", 255, 1),
            entry_record("cafe", 250, 2),
            entry_record("cafe", 240, 3),
        ];
        let second = vec![entry_record("town", 255, 3), entry_record("town", 200, 1)];
        let tokens = vec!["cafe".to_string(), "town".to_string()];
        let merged = merge_head_candidates(&tokens, vec![first, second]).unwrap();
        assert_eq!(merged.len(), 2);
        assert_eq!(merged[0].id, format_uuid_of(1));
        assert_eq!(merged[1].id, format_uuid_of(3));
        assert!(merge_head_candidates(&[], Vec::new()).unwrap().is_empty());
    }

    #[test]
    fn head_merge_ranks_identifying_matches_above_context_only_ones() {
        // The live failure: `q=paris` returned Dessirier, Rexel and Midas --
        // maximum-confidence POIs whose only relation to the query is being
        // located in Paris -- ahead of anything actually named Paris. Context
        // must lose to name or brand even at a saturated confidence of 255.
        let context_only = masked_record("paris", 8, 255, 1);
        let named = masked_record("paris", super::FIELD_NAME, 10, 2);
        let branded = masked_record("paris", super::FIELD_BRAND, 5, 3);
        let ranked = merge_head_candidates(
            &["paris".to_string()],
            vec![vec![context_only, named, branded]],
        )
        .unwrap();
        assert_eq!(
            ranked
                .iter()
                .map(|record| record.id.clone())
                .collect::<Vec<_>>(),
            vec![format_uuid_of(2), format_uuid_of(3), format_uuid_of(1)],
            "name (255-ranked context loses), then brand, then context-only"
        );
    }

    #[test]
    fn head_merge_unions_masks_across_tokens() {
        // `IKEA Berlin`: the record's "ikea" posting carries the brand bit and
        // its "berlin" posting carries only context. The union identifies it,
        // so it must outrank a record that is context-only for BOTH tokens
        // despite the latter's higher confidence.
        let store_brand = masked_record("ikea", super::FIELD_BRAND, 10, 1);
        let store_context = masked_record("berlin", 8, 10, 1);
        let bystander_a = masked_record("ikea", 8, 255, 2);
        let bystander_b = masked_record("berlin", 8, 255, 2);
        let tokens = vec!["ikea".to_string(), "berlin".to_string()];
        let ranked = merge_head_candidates(
            &tokens,
            vec![
                vec![store_brand, bystander_a],
                vec![store_context, bystander_b],
            ],
        )
        .unwrap();
        assert_eq!(ranked.len(), 2);
        assert_eq!(
            ranked[0].id,
            format_uuid_of(1),
            "brand-identified store wins"
        );
        assert_eq!(ranked[1].id, format_uuid_of(2));
    }

    #[test]
    fn routed_merge_ranks_identifying_matches_first() {
        let tokens = vec!["paris".to_string()];
        let records = vec![
            masked_record("paris", 8, 255, 1),
            masked_record("paris", super::FIELD_NAME, 1, 2),
        ];
        let merged = merge_routed_candidates(&tokens, vec![records]).unwrap();
        assert_eq!(merged.len(), 2);
        assert_eq!(
            merged[0].id,
            format_uuid_of(2),
            "a name match at confidence 1 outranks context-only at 255"
        );
    }

    #[test]
    fn identifying_covers_name_and_brand_only() {
        assert!(super::identifying(super::FIELD_NAME));
        assert!(super::identifying(super::FIELD_BRAND));
        assert!(super::identifying(super::FIELD_NAME | 8));
        assert!(!super::identifying(8), "context alone does not identify");
        assert!(!super::identifying(4), "category alone does not identify");
        assert!(!super::identifying(4 | 8));
        assert!(!super::identifying(0));
    }

    #[test]
    fn routed_merge_treats_only_saturated_posting_absence_as_recoverable() {
        let target = entry_record("cafe", 200, 999);
        let selective = vec![target];
        let saturated: Vec<_> = (1..=256).map(|id| entry_record("town", 255, id)).collect();
        let tokens = vec!["cafe".to_string(), "town".to_string()];

        let merged = merge_routed_candidates(&tokens, vec![selective, saturated]).unwrap();
        assert_eq!(merged.len(), 1);
        assert_eq!(merged[0].id, format_uuid_of(999));

        // A shorter posting is complete, so local display text cannot excuse
        // absence from it.
        let target = entry_record("cafe", 200, 999);
        assert!(merge_routed_candidates(
            &tokens,
            vec![vec![target], vec![entry_record("town", 255, 1)]],
        )
        .unwrap()
        .is_empty());
    }

    #[test]
    fn head_merge_recovers_only_absence_from_a_saturated_posting() {
        let target = || {
            let mut record = entry_record("eiffel", 200, 999);
            record.primary_name = "Eiffel Tower".to_string();
            record
        };
        let saturated: Vec<_> = (1..=10).map(|id| entry_record("tower", 255, id)).collect();
        let tokens = vec!["eiffel".to_string(), "tower".to_string()];

        let merged = merge_head_candidates(&tokens, vec![vec![target()], saturated]).unwrap();
        assert_eq!(merged.len(), 1);
        assert_eq!(merged[0].id, format_uuid_of(999));

        // Nine rows prove that the producer posting is complete. Display text
        // must not turn authoritative absence into a false positive.
        let unsaturated: Vec<_> = (1..=9).map(|id| entry_record("tower", 255, id)).collect();
        assert!(
            merge_head_candidates(&tokens, vec![vec![target()], unsaturated])
                .unwrap()
                .is_empty()
        );
    }

    #[test]
    fn head_merge_recovers_a_three_token_landmark_from_one_selective_posting() {
        let mut target = entry_record("liberty", 200, 999);
        target.primary_name = "Statue of Liberty National Monument".to_string();
        let saturated_statue: Vec<_> = (1..=10).map(|id| entry_record("statue", 255, id)).collect();
        let saturated_of: Vec<_> = (11..=20).map(|id| entry_record("of", 255, id)).collect();
        let tokens = vec![
            "statue".to_string(),
            "of".to_string(),
            "liberty".to_string(),
        ];

        let merged =
            merge_head_candidates(&tokens, vec![saturated_statue, saturated_of, vec![target]])
                .unwrap();
        assert_eq!(merged.len(), 1);
        assert_eq!(merged[0].id, format_uuid_of(999));
    }

    #[test]
    fn prefix_head_fallback_split_covers_exactly_four_to_six_tokens() {
        let tokens = |value: &str| value.split(' ').map(str::to_string).collect::<Vec<_>>();
        // Widths the ordinary head lane already serves stay untouched, so the
        // fallback can never contribute to a query that produced a result.
        assert!(prefix_head_fallback_split(&[]).is_none());
        assert!(prefix_head_fallback_split(&tokens("yishun mrt station")).is_none());
        assert!(prefix_head_fallback_split(&tokens("a b c d e f g")).is_none());

        let station = tokens("geylang bahru mrt station");
        let (head, dropped) = prefix_head_fallback_split(&station).expect("four tokens split");
        assert_eq!(head, ["geylang", "bahru", "mrt"]);
        assert_eq!(dropped, ["station"]);
        assert_eq!(head.len(), super::HEAD_QUERY_TOKEN_CAP);

        let six = tokens("a b c d e f");
        let (head, dropped) = prefix_head_fallback_split(&six).expect("six tokens split");
        assert_eq!(head.len(), 3);
        assert_eq!(dropped, ["d", "e", "f"]);
    }

    #[test]
    fn prefix_head_fallback_admits_only_records_proving_every_dropped_token() {
        let named = |name: &str, id: u128| {
            let mut record = entry_record("mrt", 255, id);
            record.primary_name = name.to_string();
            record
        };
        let dropped = vec!["station".to_string()];

        // The prefix probe retrieved the station; its stored primary name
        // proves the token the probe dropped.
        let kept = retain_records_proving_dropped_tokens(
            vec![named("Geylang Bahru MRT Station", 1)],
            &dropped,
        );
        assert_eq!(kept.len(), 1);
        assert_eq!(kept[0].id, format_uuid_of(1));

        // Same prefix, no proof of the tail: a three-token prefix match is not
        // an answer to the four-token query.
        assert!(retain_records_proving_dropped_tokens(
            vec![named("Geylang Bahru MRT Exit A", 2)],
            &dropped,
        )
        .is_empty());

        // Every dropped token must be proven, not merely one of them. The
        // record's context fields carry "Town", so "station" alone decides it.
        let two_dropped = vec!["town".to_string(), "station".to_string()];
        assert!(retain_records_proving_dropped_tokens(
            vec![named("Geylang Bahru MRT Exit A", 3)],
            &two_dropped,
        )
        .is_empty());
        assert_eq!(
            retain_records_proving_dropped_tokens(
                vec![named("Geylang Bahru MRT Station", 4)],
                &two_dropped,
            )
            .len(),
            1
        );

        // A dropped token may be proven by any stored display field, not just
        // the primary name: category "restaurant", locality "Town", country
        // "XX" are all part of the record's own evidence.
        assert_eq!(
            retain_records_proving_dropped_tokens(
                vec![named("Geylang Bahru MRT", 5)],
                &["restaurant".to_string()],
            )
            .len(),
            1
        );

        // Fail closed: with no dropped tail this would be an unverified prefix
        // search, so it returns nothing at all.
        assert!(
            retain_records_proving_dropped_tokens(vec![named("Geylang Bahru MRT", 6)], &[])
                .is_empty()
        );
    }

    #[test]
    fn dropped_token_is_proven_by_a_component_of_a_compound_category() {
        // The real shape measured on Overture 2026-07-22.0: Singapore's
        // stations are stored as "Geylang Bahru MRT" -- WITHOUT "Station" --
        // under category `train_station`.
        //
        // The four-token query `GEYLANG BAHRU MRT STATION` drives the
        // prefix-head fallback, which composes `e3:geylang bahru mrt`, finds
        // it in the head, and retrieves the correct record. This proof step
        // then decided whether that record survives.
        //
        // `normalized_words` treats `_` as a word character, so the category
        // tokenizes to the single token `train_station` and never yields
        // `station`. The right answer was retrieved and thrown away. Six
        // everyday-POI cases returned EMPTY for exactly this reason; see
        // docs/plans/2026-08-04-head-miss-interrogation.md.
        let mut record = entry_record("mrt", 255, 1);
        record.primary_name = "Geylang Bahru MRT".to_string();
        record.category = "train_station".to_string();

        let kept = retain_records_proving_dropped_tokens(vec![record], &["station".to_string()]);
        assert_eq!(kept.len(), 1, "compound category must prove its components");
        assert_eq!(kept[0].id, format_uuid_of(1));
    }

    #[test]
    fn compound_component_proof_does_not_admit_unrelated_tails() {
        // The relaxation is scoped to components of a compound token, not to
        // substring matching. PlacesV1Record is not Clone, so each assertion
        // builds its own station record.
        let station = |id: u128| {
            let mut record = entry_record("mrt", 255, id);
            record.primary_name = "Geylang Bahru MRT".to_string();
            record.category = "train_station".to_string();
            record
        };

        assert!(
            retain_records_proving_dropped_tokens(vec![station(2)], &["museum".to_string()])
                .is_empty(),
            "an unrelated dropped token must still fail closed"
        );
        // A component that is genuinely part of the compound is proven...
        assert_eq!(
            retain_records_proving_dropped_tokens(vec![station(3)], &["train".to_string()]).len(),
            1
        );
        // ...but a partial substring of a component is not.
        assert!(
            retain_records_proving_dropped_tokens(vec![station(4)], &["stat".to_string()])
                .is_empty(),
            "substring of a component is not proof"
        );
        // And the whole compound still proves itself.
        assert_eq!(
            retain_records_proving_dropped_tokens(
                vec![station(5)],
                &["train_station".to_string()],
            )
            .len(),
            1
        );
    }

    #[test]
    fn prefix_head_fallback_output_is_bounded_by_the_head_result_cap() {
        let verified: Vec<_> = (1..=50)
            .map(|id| {
                let mut record = entry_record("mrt", 255, id);
                record.primary_name = "Geylang Bahru MRT Station".to_string();
                record
            })
            .collect();
        let kept = retain_records_proving_dropped_tokens(verified, &["station".to_string()]);
        assert_eq!(kept.len(), super::PREFIX_HEAD_FALLBACK_RESULT_CAP);
    }

    #[test]
    fn entity_phrase_composition_keeps_phrase_prefix_and_ordinary_evidence() {
        let full = entry_record_at("e3:union station toronto", 255, 1, 10);
        let prefix = entry_record_at("e2:union station", 254, 2, 20);
        let duplicate_prefix = entry_record_at("union", 254, 2, 20);
        let canonical = entry_record_at("liberty", 253, 3, 30);

        let composed = compose_entity_phrase_candidates(
            vec![vec![full], vec![prefix]],
            vec![duplicate_prefix, canonical],
        )
        .unwrap();
        assert_eq!(
            composed
                .iter()
                .map(|record| record.id.clone())
                .collect::<Vec<_>>(),
            vec![format_uuid_of(1), format_uuid_of(2), format_uuid_of(3)]
        );
        assert_eq!(composed[1].token, "e2:union station");
    }

    #[test]
    fn saturated_phrase_reserves_one_slot_for_stronger_distinct_ordinary_evidence() {
        let mut full = (1..=10)
            .map(|id| entry_record("e3:statue of liberty", 255, id))
            .collect::<Vec<_>>();
        for record in &mut full {
            record.prominence_rank = 128;
        }
        full.last_mut().unwrap().prominence_rank = 89;
        let mut canonical = entry_record("liberty", 248, 99);
        canonical.prominence_rank = 255;

        let composed = compose_entity_phrase_candidates(vec![full], vec![canonical]).unwrap();
        assert_eq!(composed.len(), 10);
        assert!(composed
            .iter()
            .any(|record| record.id == format_uuid_of(99)));
        assert!(!composed
            .iter()
            .any(|record| record.id == format_uuid_of(10)));

        let mut full = (1..=10)
            .map(|id| entry_record("e3:statue of liberty", 255, id))
            .collect::<Vec<_>>();
        for record in &mut full {
            record.prominence_rank = 128;
        }
        let mut weaker = entry_record("liberty", 255, 99);
        weaker.prominence_rank = 89;
        let composed = compose_entity_phrase_candidates(vec![full], vec![weaker]).unwrap();
        assert_eq!(composed.len(), 11);
        assert!(composed
            .iter()
            .any(|record| record.id == format_uuid_of(10)));

        let mut full = (1..=10)
            .map(|id| entry_record("e3:statue of liberty", 255, id))
            .collect::<Vec<_>>();
        for record in &mut full {
            record.prominence_rank = 128;
        }
        full.last_mut().unwrap().prominence_rank = 89;
        let mut duplicate = entry_record("liberty", 255, 1);
        duplicate.prominence_rank = 255;
        let composed = compose_entity_phrase_candidates(vec![full], vec![duplicate]).unwrap();
        assert_eq!(composed.len(), 10);
        assert!(composed
            .iter()
            .any(|record| record.id == format_uuid_of(10)));
    }

    #[test]
    fn entity_phrase_composition_fails_closed_on_oversized_inputs() {
        let groups = vec![
            vec![entry_record("e2:a b", 1, 1)],
            vec![entry_record("e2:a b", 1, 2)],
            vec![entry_record("e2:a b", 1, 3)],
        ];
        assert!(compose_entity_phrase_candidates(groups, Vec::new()).is_err());

        let oversized_phrase = (1..=11).map(|id| entry_record("e2:a b", 1, id)).collect();
        assert!(compose_entity_phrase_candidates(vec![oversized_phrase], Vec::new()).is_err());

        let oversized_ordinary = (1..=31).map(|id| entry_record("ordinary", 1, id)).collect();
        assert!(compose_entity_phrase_candidates(Vec::new(), oversized_ordinary).is_err());
    }

    #[test]
    fn head_merge_fails_closed_on_contract_mismatch_or_oversized_posting() {
        let tokens = vec!["eiffel".to_string(), "tower".to_string()];
        assert!(merge_head_candidates(&tokens, vec![vec![entry_record("eiffel", 1, 1)]]).is_err());

        let oversized: Vec<_> = (1..=11).map(|id| entry_record("tower", 255, id)).collect();
        assert!(merge_head_candidates(&["tower".to_string()], vec![oversized]).is_err());

        let too_many_tokens = vec!["one", "two", "three", "four"]
            .into_iter()
            .map(str::to_string)
            .collect::<Vec<_>>();
        let postings = too_many_tokens
            .iter()
            .map(|token| vec![entry_record(token, 1, 1)])
            .collect();
        assert!(merge_head_candidates(&too_many_tokens, postings).is_err());
    }

    #[test]
    fn routed_merge_preserves_duplicate_uuid_rows_by_source_locator() {
        let tokens = vec!["cafe".to_string(), "town".to_string()];
        let merged = merge_routed_candidates(
            &tokens,
            vec![
                vec![
                    entry_record_at("cafe", 200, 999, 10),
                    entry_record_at("cafe", 200, 999, 11),
                ],
                vec![
                    entry_record_at("town", 200, 999, 10),
                    entry_record_at("town", 200, 999, 11),
                ],
            ],
        )
        .unwrap();
        assert_eq!(merged.len(), 2);
        assert_eq!(
            merged
                .iter()
                .map(|record| record.source_row_index)
                .collect::<Vec<_>>(),
            vec![10, 11]
        );
    }

    /// A `0002` entry: the byte layout that is LIVE right now, with no
    /// prominence byte between the confidence rank and the id.
    fn entry_v2(token: &str, cell: Option<&str>, rank: u8, id: u128) -> Vec<u8> {
        let mut output = Vec::new();
        text(&mut output, token);
        if let Some(cell) = cell {
            text(&mut output, cell);
        }
        output.extend_from_slice(&[1, rank]);
        output.extend_from_slice(&id.to_be_bytes());
        output.extend_from_slice(&1.25_f64.to_le_bytes());
        output.extend_from_slice(&2.5_f64.to_le_bytes());
        output.extend_from_slice(&3_u32.to_le_bytes());
        output.extend_from_slice(&4_u32.to_le_bytes());
        output.extend_from_slice(&0_u64.to_le_bytes());
        for value in ["Cafe", "", "restaurant", "Town", "Region", "XX"] {
            text(&mut output, value);
        }
        output
    }

    /// The deploy that carries the prominence byte reaches production long
    /// before any 0003 build is promoted. If this fails, shipping it takes
    /// Places search down until a full planet rebuild completes.
    #[test]
    fn still_decodes_the_live_0002_head_layout() {
        let bytes = entry_v2("cafe", None, 200, 7);
        let (record, _) =
            super::decode_entry(&bytes, PlacesV1Mode::Head, PlacesV1Version::V2).unwrap();
        assert_eq!(record.token, "cafe");
        assert_eq!(record.confidence_rank, 200);
        assert_eq!(
            record.prominence_rank, 0,
            "a 0002 shard carries no prominence byte and must read as zero, \
             which is the ranking it was actually built with"
        );
        assert_eq!(record.primary_name, "Cafe");
        assert_eq!(record.country, "XX");
    }

    #[test]
    fn parse_accepts_both_shard_generations_and_rejects_others() {
        for (version, expected) in [(PlacesV1Version::V2, 0_u8), (PlacesV1Version::V3, 9)] {
            let entry = match version {
                PlacesV1Version::V2 => entry_v2("cafe", None, 200, 7),
                PlacesV1Version::V3 => entry_masked_with_prominence("cafe", 1, 200, 7, expected),
            };
            let shard = artifact_versioned(PlacesV1Mode::Head, &[entry], version);
            let parsed = PlacesV1Artifact::parse(
                &shard,
                PlacesV1Mode::Head,
                1 << 20,
                16,
                MAX_ARTIFACT_ENTRY_BYTES,
            )
            .unwrap_or_else(|error| panic!("{version:?} must parse: {error}"));
            let found = parsed.lookup("cafe", None, 16, 8).unwrap();
            assert_eq!(found.len(), 1);
            assert_eq!(found[0].prominence_rank, expected, "{version:?}");
        }

        // A magic from neither generation still fails closed.
        let mut bogus = artifact(
            PlacesV1Mode::Head,
            &[entry_masked("cafe", None, 1, 200, 7, 0)],
        );
        bogus[..8].copy_from_slice(b"PLHD9999");
        assert!(PlacesV1Artifact::parse(
            &bogus,
            PlacesV1Mode::Head,
            1 << 20,
            16,
            MAX_ARTIFACT_ENTRY_BYTES
        )
        .is_err());
    }

    fn entry_masked_with_prominence(
        token: &str,
        field_mask: u8,
        rank: u8,
        id: u128,
        prominence: u8,
    ) -> Vec<u8> {
        let mut output = Vec::new();
        text(&mut output, token);
        output.extend_from_slice(&[field_mask, rank, prominence]);
        output.extend_from_slice(&id.to_be_bytes());
        output.extend_from_slice(&1.25_f64.to_le_bytes());
        output.extend_from_slice(&2.5_f64.to_le_bytes());
        output.extend_from_slice(&3_u32.to_le_bytes());
        output.extend_from_slice(&4_u32.to_le_bytes());
        output.extend_from_slice(&0_u64.to_le_bytes());
        for value in ["Cafe", "", "restaurant", "Town", "Region", "XX"] {
            text(&mut output, value);
        }
        output
    }

    /// The measured failure this whole change exists for: on one token, a
    /// landmark with LOWER confidence than the commodity records around it.
    #[test]
    fn prominence_outranks_confidence_within_a_token() {
        let landmark = entry_masked_with_prominence("sagrada", 1, 252, 1, 153);
        let starbucks = entry_masked_with_prominence("sagrada", 1, 255, 2, 0);
        let shard = artifact(PlacesV1Mode::Head, &[landmark, starbucks]);
        let parsed = PlacesV1Artifact::parse(
            &shard,
            PlacesV1Mode::Head,
            1 << 20,
            16,
            MAX_ARTIFACT_ENTRY_BYTES,
        )
        .unwrap();
        let found = parsed.lookup("sagrada", None, 16, 8).unwrap();
        assert_eq!(found.len(), 2);
        let ranked = merge_head_candidates(&["sagrada".to_string()], vec![found]).unwrap();
        assert_eq!(
            ranked[0].id,
            format_uuid_of(1),
            "the prominent record must lead despite its lower confidence"
        );
    }

    fn entry_record(token: &str, rank: u8, id: u128) -> PlacesV1Record {
        entry_record_at(token, rank, id, 0)
    }

    fn masked_record(token: &str, field_mask: u8, rank: u8, id: u128) -> PlacesV1Record {
        let bytes = entry_masked(token, None, field_mask, rank, id, 0);
        let (record, _) =
            super::decode_entry(&bytes, PlacesV1Mode::Head, PlacesV1Version::V3).unwrap();
        record
    }

    fn entry_record_at(token: &str, rank: u8, id: u128, row: u64) -> PlacesV1Record {
        let bytes = entry(token, None, rank, id, row);
        let (record, _) =
            super::decode_entry(&bytes, PlacesV1Mode::Head, PlacesV1Version::V3).unwrap();
        record
    }

    #[test]
    fn routing_rejects_broken_schemas_tilings_and_names() {
        let slice = promoted_slice();
        let name = slice
            .routing
            .routed_object_names()
            .next()
            .unwrap()
            .to_string();
        let head = head_block(2, &format!("{}.json", "a".repeat(64)));

        // Wrong schema strings fail closed.
        for (field, value) in [
            ("schema", "overture-promoted-places-routing-v2"),
            ("family", "addresses"),
            ("cell_scheme", "level-8-quadkey"),
            ("subpartition_scheme", "token-md5-prefix"),
        ] {
            let mut value_json: Value =
                serde_json::from_str(&routing_json(json!({"8080": [["", name]]}), head.clone()))
                    .unwrap();
            value_json[field] = json!(value);
            assert!(PlacesRouting::parse(&value_json.to_string()).is_err());
        }

        // A malformed cell, a non-content-addressed object, and an over-deep
        // prefix all fail closed.
        assert!(
            PlacesRouting::parse(&routing_json(json!({"80800": [["", name]]}), head.clone()))
                .is_err()
        );
        assert!(PlacesRouting::parse(&routing_json(
            json!({"8080": [["", "objects.plrv"]]}),
            head.clone()
        ))
        .is_err());
        assert!(PlacesRouting::parse(&routing_json(
            json!({"8080": [["012345678", name]]}),
            head.clone()
        ))
        .is_err());

        // A gap (missing nibble) and an overlap both break the exact tiling.
        let partial: Vec<Value> = (0..15_u64)
            .map(|nibble| json!([format!("{nibble:x}"), name]))
            .collect();
        assert!(
            PlacesRouting::parse(&routing_json(json!({"8080": partial}), head.clone())).is_err()
        );
        assert!(PlacesRouting::parse(&routing_json(
            json!({"8080": [["", name], ["0", name]]}),
            head.clone()
        ))
        .is_err());

        // Head geometry lies fail closed.
        let mut bad_head = head.clone();
        bad_head["shard_count"] = json!(8);
        assert!(
            PlacesRouting::parse(&routing_json(json!({"8080": [["", name]]}), bad_head)).is_err()
        );
    }

    #[test]
    fn head_manifest_rejects_geometry_and_identity_lies() {
        let slice = promoted_slice();
        let shard = slice.head.shards().next().unwrap();
        let row = json!({
            "shard_id": shard.shard_id,
            "path": shard.path,
            "sha256": shard.sha256,
            "bytes": shard.bytes,
        });
        let manifest = |mutate: &dyn Fn(&mut Value)| {
            let mut value = json!({
                "schema": PLACES_HEAD_MANIFEST_SCHEMA,
                "shard_bits": 4,
                "shard_count": 16,
                "populated_shards": 1,
                "shards": [row.clone()],
            });
            mutate(&mut value);
            HeadRoutingManifest::parse(&value.to_string())
        };
        assert!(manifest(&|_| {}).is_ok());
        let admitted =
            manifest(&|value| value["entity_phrase_admission"] = json!(ENTITY_PHRASE_ADMISSION))
                .unwrap();
        assert!(admitted.admits_entity_phrases());
        assert!(
            manifest(&|value| { value["entity_phrase_admission"] = json!("unknown") }).is_err()
        );
        assert!(manifest(&|value| value["schema"] = json!("other")).is_err());
        assert!(manifest(&|value| value["shard_count"] = json!(8)).is_err());
        assert!(manifest(&|value| value["populated_shards"] = json!(2)).is_err());
        assert!(manifest(&|value| value["shards"][0]["shard_id"] = json!(16)).is_err());
        assert!(manifest(&|value| value["shards"][0]["bytes"] = json!(0)).is_err());
        assert!(manifest(&|value| value["shards"][0]["path"] = json!("head.plhd")).is_err());

        // The deterministic admission sample is first, middle, last by id.
        let ids: Vec<u32> = {
            let mut ids: Vec<u32> = slice.head.shards().map(|shard| shard.shard_id).collect();
            ids.sort_unstable();
            ids
        };
        let sample: Vec<u32> = slice
            .head
            .admission_sample()
            .iter()
            .map(|shard| shard.shard_id)
            .collect();
        let mut expected = vec![ids[0], ids[ids.len() / 2], ids[ids.len() - 1]];
        expected.dedup();
        assert_eq!(sample, expected);
    }

    #[test]
    fn entity_phrase_key_and_record_validation_are_exact_and_bounded() {
        let words = vec!["big".to_string(), "ben".to_string()];
        assert_eq!(entity_phrase_key(&words).as_deref(), Some("e2:big ben"));
        assert_eq!(
            entity_phrase_key(&[
                "empire".to_string(),
                "state".to_string(),
                "building".to_string(),
            ])
            .as_deref(),
            Some("e3:empire state building")
        );
        assert!(entity_phrase_key(&["ben".to_string()]).is_none());
        assert!(entity_phrase_key(&[
            "one".to_string(),
            "two".to_string(),
            "three".to_string(),
            "four".to_string(),
        ])
        .is_none());
        let three = vec!["union".into(), "station".into(), "toronto".into()];
        assert_eq!(
            entity_phrase_token_groups(&three),
            vec![three.as_slice(), &three[..2]]
        );
        let two = vec!["big".into(), "ben".into()];
        assert_eq!(entity_phrase_token_groups(&two), vec![two.as_slice()]);
        assert!(entity_phrase_token_groups(&["liberty".into()]).is_empty());

        let mut exact = entry_record("e2:big ben", 255, 1);
        exact.primary_name = "Big Ben".to_string();
        exact.prominence_rank = 1;
        let mut wrong = entry_record("e2:big ben", 255, 2);
        wrong.primary_name = "Big Ben Memorial".to_string();
        wrong.prominence_rank = 1;
        let mut zero_prominence = entry_record("e2:big ben", 255, 3);
        zero_prominence.primary_name = "Big Ben".to_string();
        zero_prominence.prominence_rank = 0;
        let mut wrong_mask = entry_record("e2:big ben", 255, 4);
        wrong_mask.primary_name = "Big Ben".to_string();
        wrong_mask.prominence_rank = 1;
        wrong_mask.field_mask = super::FIELD_BRAND;
        let records =
            validate_entity_phrase_records(&words, vec![wrong, zero_prominence, wrong_mask, exact]);
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].primary_name, "Big Ben");
    }

    /// Local evidence over a real promoted slice (for example the Monaco
    /// harness output of `scripts/promote_construction_slice.py`). `#[ignore]`d
    /// like the head-shard evidence harness; drive it with:
    ///
    /// `PLACES_PROMOTED_FAMILY_DIR` — the promoted `.../families/places` dir.
    /// `PLACES_PROMOTED_QUERIES` — `longitude\tlatitude\ttoken` rows.
    #[test]
    #[ignore = "requires a locally promoted slice; driven manually"]
    fn local_promoted_slice_serves_routed_queries() {
        let root = std::path::PathBuf::from(
            std::env::var("PLACES_PROMOTED_FAMILY_DIR")
                .expect("PLACES_PROMOTED_FAMILY_DIR is required"),
        );
        let routing_text =
            std::fs::read_to_string(root.join("routing.json")).expect("read routing.json");
        let routing = PlacesRouting::parse(&routing_text).expect("routing parses");
        let head_text =
            std::fs::read_to_string(root.join("objects").join(&routing.head.manifest_object))
                .expect("read head routing manifest");
        let head = HeadRoutingManifest::parse(&head_text).expect("head manifest parses");
        assert!(head.agrees_with(&routing.head));
        let queries =
            std::env::var("PLACES_PROMOTED_QUERIES").expect("PLACES_PROMOTED_QUERIES is required");
        for row in queries.lines().filter(|line| !line.is_empty()) {
            let mut parts = row.split('\t');
            let longitude: f64 = parts.next().expect("longitude").parse().unwrap();
            let latitude: f64 = parts.next().expect("latitude").parse().unwrap();
            let token = parts.next().expect("token");
            let cell = construction_cell(longitude, latitude).expect("cell");
            let object = routing
                .route(&cell, token)
                .expect("tiling holds")
                .expect("cell is populated");
            let bytes = std::fs::read(root.join("objects").join(object)).expect("routed object");
            let hits = routed_lookup(&bytes, &cell, token).expect("routed artifact decodes");
            assert!(!hits.is_empty(), "token {token} resolved zero records");
            assert_eq!(hits[0].token, token);
        }
    }
}
