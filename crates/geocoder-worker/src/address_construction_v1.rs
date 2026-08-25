//! Decoder and range-read serving lane for the `global-v2-construction-v1`
//! Address artifact (`OAV1ART`).
//!
//! The `/v2/forward` structured lane routes here when a release's addresses
//! family declares the promoted construction format (`OAV1ART`): the promoted
//! `slice-YYYY-MM-DD.N/families/addresses/` tree holds `routing.json`
//! (`overture-promoted-addresses-routing-v1`, a per-country inclusive
//! route-hash envelope table) and content-addressed `objects/<sha256>.av1`
//! serving artifacts.
//!
//! Planet `OAV1ART` objects approach the 2 GiB per-object publication cap —
//! far beyond the 128 MiB isolate — so the serving lane NEVER hydrates a whole
//! artifact. [`RangedAddressLookup`] is a synchronous read planner that the
//! loader glue drives over [`crate::range_reader::RangeReader`]: it reads the
//! fixed 44-byte header, binary-searches the 24-byte index entries via ranged
//! reads, reads one bounded candidate window, and finally ranged-reads only the
//! target payload run. Every phase is capped, so one lookup transfers at most
//! `44 + 24*MAX_ADDRESS_INDEX_PROBES + 24*(cap+1) + MAX_ADDRESS_LOOKUP_PAYLOAD_BYTES`
//! bytes (~8.4 MiB worst case) and fails closed on any breach.
//!
//! [`AddressV1Artifact`] (whole-buffer parse + lookup) is retained as the
//! reference implementation; native tests cross-check the range planner
//! against it on identical bytes.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;

use geocoder_core::pages::format_uuid;
use serde::Deserialize;

use crate::address::AddressOutcome;
use crate::address_pages::AddressPageRecord;
use crate::places_construction_v1::content_addressed_name;
use crate::range_reader::RangeReader;
use crate::stac::{not_found, ShardLoader};

const MAGIC: &[u8; 8] = b"OAV1ART\0";
const HEADER_BYTES: usize = 44;
const INDEX_BYTES: usize = 24;

type Result<T> = std::result::Result<T, String>;

/// The promoted construction-v1 addresses family format identity
/// (`scripts/promote_construction_slice.py` `DEFAULT_VERSIONS["addresses"]`):
/// per-partition `OAV1ART` serving artifacts routed by country + route hash.
pub(crate) const ADDRESS_CONSTRUCTION_FORMAT: &str = "OAV1ART";
/// The producer key normalization the promoted family manifest declares: the
/// `address-transform-v1` Rust transform in `geocoder-construction`.
pub(crate) const ADDRESS_CONSTRUCTION_NORMALIZATION_VERSION: &str = "address-transform-v1";
pub(crate) const ADDRESS_ROUTING_SCHEMA: &str = "overture-promoted-addresses-routing-v1";
const ADDRESS_ROUTING_KEY_SCHEME: &str = "country-route-hash-range-v1";

/// routing.json cap. The planet slice carries 581 partitions at roughly 130
/// bytes each (~75 KiB); reusing the shared 8 MiB routing cap leaves two
/// orders of magnitude of headroom and stays far under the isolate budget.
pub(crate) const MAX_ADDRESS_ROUTING_BYTES: usize = 8 * 1024 * 1024;
const MAX_ADDRESS_ROUTING_PARTITIONS: usize = 65_536;

/// Publication enforces a 2 GiB per-object cap on `OAV1ART` serving artifacts
/// (see `docs/plans/construction-v1-state.md`); a header claiming more fails
/// closed before any index read.
const MAX_ADDRESS_OBJECT_BYTES: u64 = 2 * 1024 * 1024 * 1024;
/// Per-record payload cap for the serving lane, aligned with the Places
/// per-entry cap. Real address records are a few hundred bytes.
const MAX_ADDRESS_RECORD_BYTES: usize = 64 * 1024;
/// One lookup's payload-run cap: 512 candidates x a few hundred bytes in
/// practice; 8 MiB fails closed long before the isolate budget.
const MAX_ADDRESS_LOOKUP_PAYLOAD_BYTES: u64 = 8 * 1024 * 1024;
/// Binary-search probe cap. The 2 GiB object cap bounds the index to ~89.5M
/// entries (27 probes); 64 is generous and still trivially cheap.
const MAX_ADDRESS_INDEX_PROBES: usize = 64;
/// Exact-key candidate cap for one serving lookup, matching the v2 handler's
/// structured candidate cap. The candidate window reads `cap + 1` entries so
/// an over-cap equal-hash run is detected and fails closed rather than being
/// silently truncated.
pub(crate) const ADDRESS_SERVING_CANDIDATE_CAP: usize = 512;

thread_local! {
    /// Parsed promoted address routing tables, LRU-last, one live generation.
    static ADDRESS_ROUTING_CACHE: RefCell<Vec<(String, Rc<AddressRouting>)>> =
        const { RefCell::new(Vec::new()) };
}

#[derive(Debug, PartialEq)]
pub(crate) struct AddressV1Record {
    pub key: [String; 8],
    pub id: String,
    pub longitude_e7: i32,
    pub latitude_e7: i32,
    pub source_object_index: u32,
    pub source_row_group: u32,
    pub source_row_index: u64,
    pub country: String,
    pub postal_city: String,
    pub postcode: String,
    pub street: String,
    pub number: String,
    pub unit: String,
    pub address_levels: Vec<String>,
}

/// Whole-buffer reference implementation. Production serving uses
/// [`RangedAddressLookup`]; native tests cross-check the two on identical
/// bytes, so this parser stays the format's executable specification.
#[allow(dead_code)]
pub(crate) struct AddressV1Artifact<'a> {
    bytes: &'a [u8],
    records: usize,
    maximum_record_bytes: usize,
}

#[allow(dead_code)]
impl<'a> AddressV1Artifact<'a> {
    pub(crate) fn parse(
        bytes: &'a [u8],
        maximum_bytes: usize,
        maximum_records: usize,
        maximum_record_bytes: usize,
    ) -> Result<Self> {
        if bytes.len() > maximum_bytes || bytes.len() < HEADER_BYTES || &bytes[..8] != MAGIC {
            return Err("invalid or over-cap Address v1 artifact".to_string());
        }
        if read_u32(bytes, 8)? != 1 {
            return Err("unsupported Address v1 artifact version".to_string());
        }
        let records = usize::try_from(read_u64(bytes, 12)?)
            .map_err(|_| "Address v1 record count overflows".to_string())?;
        if records > maximum_records
            || read_u64(bytes, 20)? != HEADER_BYTES as u64
            || read_u64(bytes, 28)?
                != (HEADER_BYTES as u64)
                    .checked_add(
                        (records as u64)
                            .checked_mul(INDEX_BYTES as u64)
                            .ok_or_else(|| "Address v1 index overflows".to_string())?,
                    )
                    .ok_or_else(|| "Address v1 index overflows".to_string())?
            || read_u64(bytes, 28)?
                .checked_add(read_u64(bytes, 36)?)
                .ok_or_else(|| "Address v1 payload overflows".to_string())?
                != bytes.len() as u64
        {
            return Err("Address v1 header does not reconcile".to_string());
        }
        let mut expected_offset = read_u64(bytes, 28)?;
        let mut previous_hash = None;
        for index in 0..records {
            let entry = HEADER_BYTES + index * INDEX_BYTES;
            let route_hash = read_u64(bytes, entry)?;
            let offset = read_u64(bytes, entry + 8)?;
            let length = read_u32(bytes, entry + 16)? as usize;
            if read_u32(bytes, entry + 20)? != 0
                || offset != expected_offset
                || length > maximum_record_bytes
                || offset
                    .checked_add(length as u64)
                    .is_none_or(|end| end > bytes.len() as u64)
                || previous_hash.is_some_and(|value| value > route_hash)
            {
                return Err("Address v1 index is invalid".to_string());
            }
            expected_offset += length as u64;
            previous_hash = Some(route_hash);
        }
        if expected_offset != bytes.len() as u64 {
            return Err("Address v1 payload length differs".to_string());
        }
        Ok(Self {
            bytes,
            records,
            maximum_record_bytes,
        })
    }

    pub(crate) fn lookup(
        &self,
        key: &[String; 8],
        maximum_candidates: usize,
    ) -> Result<Vec<AddressV1Record>> {
        let target = route_hash(key);
        let start = self.lower_bound(target)?;
        let mut output = Vec::new();
        for (scanned, index) in (start..self.records).enumerate() {
            let route = self.route_at(index)?;
            if route != target {
                break;
            }
            if scanned >= maximum_candidates {
                return Err("Address v1 candidate cap exceeded".to_string());
            }
            let record = self.decode(index, route)?;
            if &record.key == key {
                output.push(record);
            }
        }
        Ok(output)
    }

    fn lower_bound(&self, target: u64) -> Result<usize> {
        let mut low = 0;
        let mut high = self.records;
        while low < high {
            let middle = low + (high - low) / 2;
            if self.route_at(middle)? < target {
                low = middle + 1;
            } else {
                high = middle;
            }
        }
        Ok(low)
    }

    fn route_at(&self, index: usize) -> Result<u64> {
        read_u64(self.bytes, HEADER_BYTES + index * INDEX_BYTES)
    }

    fn decode(&self, index: usize, expected_route: u64) -> Result<AddressV1Record> {
        let entry = HEADER_BYTES + index * INDEX_BYTES;
        let offset = usize::try_from(read_u64(self.bytes, entry + 8)?)
            .map_err(|_| "Address v1 offset overflows".to_string())?;
        let length = read_u32(self.bytes, entry + 16)? as usize;
        if length > self.maximum_record_bytes {
            return Err("Address v1 record exceeds cap".to_string());
        }
        let payload = self
            .bytes
            .get(offset..offset + length)
            .ok_or_else(|| "Address v1 record is truncated".to_string())?;
        decode_record(payload, expected_route)
    }
}

/// Decode one complete record payload, reconciling its embedded normalized key
/// against `expected_route`. Shared verbatim by the whole-buffer reference
/// parser and the range-read serving lane so the two cannot drift.
fn decode_record(payload: &[u8], expected_route: u64) -> Result<AddressV1Record> {
    let mut position = 0;
    let key: [String; 8] = (0..8)
        .map(|_| read_text(payload, &mut position))
        .collect::<Result<Vec<_>>>()?
        .try_into()
        .map_err(|_| "Address v1 key has wrong arity".to_string())?;
    let raw_id: [u8; 16] = take(payload, &mut position, 16)?.try_into().unwrap();
    let longitude_e7 = read_i32_at(payload, &mut position)?;
    let latitude_e7 = read_i32_at(payload, &mut position)?;
    let source_object_index = read_u32_at(payload, &mut position)?;
    let source_row_group = read_u32_at(payload, &mut position)?;
    let source_row_index = read_u64_at(payload, &mut position)?;
    let display = (0..6)
        .map(|_| read_text(payload, &mut position))
        .collect::<Result<Vec<_>>>()?;
    let level_count = usize::try_from(read_u64_at(payload, &mut position)?)
        .map_err(|_| "Address v1 level count overflows".to_string())?;
    if level_count > 64 {
        return Err("Address v1 level count exceeds cap".to_string());
    }
    let address_levels = (0..level_count)
        .map(|_| read_text(payload, &mut position))
        .collect::<Result<Vec<_>>>()?;
    if position != payload.len() || route_hash(&key) != expected_route {
        return Err("Address v1 record does not reconcile".to_string());
    }
    Ok(AddressV1Record {
        key,
        id: format_uuid(raw_id),
        longitude_e7,
        latitude_e7,
        source_object_index,
        source_row_group,
        source_row_index,
        country: display[0].clone(),
        postal_city: display[1].clone(),
        postcode: display[2].clone(),
        street: display[3].clone(),
        number: display[4].clone(),
        unit: display[5].clone(),
        address_levels,
    })
}

fn read_text(bytes: &[u8], position: &mut usize) -> Result<String> {
    let length = usize::try_from(read_u64_at(bytes, position)?)
        .map_err(|_| "Address v1 text length overflows".to_string())?;
    let value = std::str::from_utf8(take(bytes, position, length)?)
        .map_err(|_| "Address v1 text is not UTF-8".to_string())?;
    Ok(value.to_string())
}

fn take<'a>(bytes: &'a [u8], position: &mut usize, length: usize) -> Result<&'a [u8]> {
    let end = position
        .checked_add(length)
        .ok_or_else(|| "Address v1 field overflows".to_string())?;
    let value = bytes
        .get(*position..end)
        .ok_or_else(|| "Address v1 field is truncated".to_string())?;
    *position = end;
    Ok(value)
}

fn read_i32_at(bytes: &[u8], position: &mut usize) -> Result<i32> {
    Ok(i32::from_be_bytes(
        take(bytes, position, 4)?.try_into().unwrap(),
    ))
}

fn read_u32_at(bytes: &[u8], position: &mut usize) -> Result<u32> {
    Ok(u32::from_be_bytes(
        take(bytes, position, 4)?.try_into().unwrap(),
    ))
}

fn read_u64_at(bytes: &[u8], position: &mut usize) -> Result<u64> {
    Ok(u64::from_be_bytes(
        take(bytes, position, 8)?.try_into().unwrap(),
    ))
}

fn read_u32(bytes: &[u8], offset: usize) -> Result<u32> {
    Ok(u32::from_be_bytes(
        bytes
            .get(offset..offset + 4)
            .ok_or_else(|| "Address v1 u32 is truncated".to_string())?
            .try_into()
            .unwrap(),
    ))
}

fn read_u64(bytes: &[u8], offset: usize) -> Result<u64> {
    Ok(u64::from_be_bytes(
        bytes
            .get(offset..offset + 8)
            .ok_or_else(|| "Address v1 u64 is truncated".to_string())?
            .try_into()
            .unwrap(),
    ))
}

/// FNV-1a over the eight normalized fields joined by `0x1f`. Byte-identical to
/// the producer's `route_hash` in `crates/geocoder-construction/src/main.rs`
/// and to `address_key_hash` in `crate::address` (a parity test pins all
/// three-way agreement via shared vectors).
pub(crate) fn route_hash(fields: &[String; 8]) -> u64 {
    let mut value = 0xcbf29ce484222325_u64;
    for (index, field) in fields.iter().enumerate() {
        if index > 0 {
            value ^= 0x1f;
            value = value.wrapping_mul(0x100000001b3);
        }
        for byte in field.as_bytes() {
            value ^= u64::from(*byte);
            value = value.wrapping_mul(0x100000001b3);
        }
    }
    value
}

// ---------------------------------------------------------------------------
// Promoted-slice routing (`overture-promoted-addresses-routing-v1`).

fn valid_routing_country(country: &str) -> bool {
    (2..=3).contains(&country.len())
        && country
            .bytes()
            .all(|value| value.is_ascii_lowercase() || value.is_ascii_digit())
}

#[derive(Debug, Deserialize)]
struct RawAddressPartition {
    country: String,
    hash_start: u64,
    hash_end: u64,
    object: String,
}

#[derive(Debug, Deserialize)]
struct RawAddressRouting {
    schema: String,
    family: String,
    key_scheme: String,
    partitions: Vec<RawAddressPartition>,
}

#[derive(Debug)]
struct AddressPartition {
    hash_start: u64,
    hash_end: u64,
    object: String,
}

/// Where a `(country, route_hash)` pair lands in the promoted envelope table.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum AddressEnvelope<'routing> {
    /// The family serves no partition for the query's country.
    OutOfCoverage,
    /// The country is served, but the hash falls in an inter-envelope gap: the
    /// producer emits partitions only for populated hash buckets, so a gap
    /// proves no retained row hashes there. An empty result, never an error.
    ProvablyEmpty,
    /// The single serving object whose inclusive envelope contains the hash.
    Object(&'routing str),
}

/// Validated `overture-promoted-addresses-routing-v1` table.
#[derive(Debug)]
pub(crate) struct AddressRouting {
    countries: HashMap<String, Vec<AddressPartition>>,
}

impl AddressRouting {
    pub(crate) fn parse(text: &str) -> Result<Self> {
        let raw: RawAddressRouting = serde_json::from_str(text)
            .map_err(|error| format!("invalid address routing JSON: {error}"))?;
        if raw.schema != ADDRESS_ROUTING_SCHEMA
            || raw.family != "addresses"
            || raw.key_scheme != ADDRESS_ROUTING_KEY_SCHEME
        {
            return Err("unsupported address routing contract".into());
        }
        if raw.partitions.is_empty() || raw.partitions.len() > MAX_ADDRESS_ROUTING_PARTITIONS {
            return Err("address routing partition count is outside hard bounds".into());
        }
        let mut countries: HashMap<String, Vec<AddressPartition>> = HashMap::new();
        let mut previous: Option<(String, u64, u64)> = None;
        for partition in raw.partitions {
            if !valid_routing_country(&partition.country) {
                return Err(format!(
                    "address routing country is malformed: {}",
                    partition.country
                ));
            }
            if partition.hash_start > partition.hash_end {
                return Err(format!(
                    "address routing envelope is inverted for country {}",
                    partition.country
                ));
            }
            if !content_addressed_name(&partition.object, ".av1") {
                return Err(format!(
                    "address routing names a non-content-addressed object for country {}",
                    partition.country
                ));
            }
            // The promotion tool emits rows sorted by (country, hash_start)
            // and rejects same-country overlaps; require exactly that here so
            // per-country envelope resolution can binary-search, and so a
            // reordered or overlapping table fails closed instead of routing
            // ambiguously.
            if let Some((country, _, end)) = &previous {
                let ordered = match country.as_str().cmp(partition.country.as_str()) {
                    std::cmp::Ordering::Less => true,
                    std::cmp::Ordering::Equal => partition.hash_start > *end,
                    std::cmp::Ordering::Greater => false,
                };
                if !ordered {
                    return Err(format!(
                        "address routing envelopes are unsorted or overlap for country {}",
                        partition.country
                    ));
                }
            }
            previous = Some((
                partition.country.clone(),
                partition.hash_start,
                partition.hash_end,
            ));
            countries
                .entry(partition.country)
                .or_default()
                .push(AddressPartition {
                    hash_start: partition.hash_start,
                    hash_end: partition.hash_end,
                    object: partition.object,
                });
        }
        Ok(Self { countries })
    }

    /// Names of every routed `.av1` object reachable from the envelope table.
    pub(crate) fn routed_object_names(&self) -> impl Iterator<Item = &str> {
        self.countries
            .values()
            .flatten()
            .map(|partition| partition.object.as_str())
    }

    /// Resolve `(country, route_hash)` to its serving envelope. Bounds are
    /// inclusive on both ends.
    pub(crate) fn route(&self, country: &str, hash: u64) -> AddressEnvelope<'_> {
        let Some(partitions) = self.countries.get(country) else {
            return AddressEnvelope::OutOfCoverage;
        };
        let Some(index) = partitions
            .partition_point(|partition| partition.hash_start <= hash)
            .checked_sub(1)
        else {
            return AddressEnvelope::ProvablyEmpty;
        };
        let partition = &partitions[index];
        if hash <= partition.hash_end {
            AddressEnvelope::Object(&partition.object)
        } else {
            AddressEnvelope::ProvablyEmpty
        }
    }
}

// ---------------------------------------------------------------------------
// Range-read lookup planner over one OAV1ART object.

/// Header fields the range lane needs, reconciled and capped at parse.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct AddressV1Header {
    records: u64,
    payload_offset: u64,
    total_bytes: u64,
}

fn parse_ranged_header(bytes: &[u8]) -> Result<AddressV1Header> {
    if bytes.len() != HEADER_BYTES || &bytes[..8] != MAGIC {
        return Err("invalid Address v1 artifact header".to_string());
    }
    if read_u32(bytes, 8)? != 1 {
        return Err("unsupported Address v1 artifact version".to_string());
    }
    let records = read_u64(bytes, 12)?;
    let expected_payload_offset = (HEADER_BYTES as u64)
        .checked_add(
            records
                .checked_mul(INDEX_BYTES as u64)
                .ok_or_else(|| "Address v1 index overflows".to_string())?,
        )
        .ok_or_else(|| "Address v1 index overflows".to_string())?;
    let payload_offset = read_u64(bytes, 28)?;
    let total_bytes = payload_offset
        .checked_add(read_u64(bytes, 36)?)
        .ok_or_else(|| "Address v1 payload overflows".to_string())?;
    if read_u64(bytes, 20)? != HEADER_BYTES as u64
        || payload_offset != expected_payload_offset
        || total_bytes > MAX_ADDRESS_OBJECT_BYTES
    {
        return Err("Address v1 header does not reconcile".to_string());
    }
    Ok(AddressV1Header {
        records,
        payload_offset,
        total_bytes,
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct RangedIndexEntry {
    route_hash: u64,
    offset: u64,
    length: u32,
}

fn parse_ranged_index_entry(bytes: &[u8], header: &AddressV1Header) -> Result<RangedIndexEntry> {
    if bytes.len() != INDEX_BYTES {
        return Err("Address v1 index entry has the wrong extent".to_string());
    }
    let route_hash = read_u64(bytes, 0)?;
    let offset = read_u64(bytes, 8)?;
    let length = read_u32(bytes, 16)?;
    if read_u32(bytes, 20)? != 0
        || length as usize > MAX_ADDRESS_RECORD_BYTES
        || offset < header.payload_offset
        || offset
            .checked_add(u64::from(length))
            .is_none_or(|end| end > header.total_bytes)
    {
        return Err("Address v1 index entry is invalid".to_string());
    }
    Ok(RangedIndexEntry {
        route_hash,
        offset,
        length,
    })
}

/// One step of the range-read lookup protocol: either the exact next byte
/// range the driver must fetch, or the finished exact-key candidate list.
#[derive(Debug, PartialEq)]
pub(crate) enum RangedStep {
    Read { offset: u64, length: u64 },
    Done(Vec<AddressV1Record>),
}

enum RangedPhase {
    Start,
    AwaitHeader,
    AwaitProbe {
        low: u64,
        high: u64,
        mid: u64,
        /// Largest probed hash known to sit strictly left of the search
        /// window, and smallest known to sit at/right of it. A probe that
        /// violates these bounds proves the index is unsorted; fail closed.
        left: Option<u64>,
        right: Option<u64>,
    },
    AwaitWindow {
        entries: u64,
    },
    AwaitPayload {
        run: Vec<RangedIndexEntry>,
        expected_bytes: u64,
    },
    Finished,
}

/// Synchronous planner for one exact-key lookup against one `OAV1ART` object.
///
/// Protocol: call [`RangedAddressLookup::advance`] with `None` first, then
/// with the exact bytes of each requested read, until it returns
/// [`RangedStep::Done`]. Reads per lookup: one 44-byte header, at most
/// `MAX_ADDRESS_INDEX_PROBES` 24-byte index probes (`ceil(log2(records))` in
/// practice, <= 27 under the 2 GiB object cap), one candidate window of at
/// most `(cap + 1) * 24` bytes, and at most one payload run of at most
/// [`MAX_ADDRESS_LOOKUP_PAYLOAD_BYTES`]. The planner is deterministic and
/// pure, so native tests drive it over in-memory bytes and cross-check it
/// against [`AddressV1Artifact::parse`] + `lookup`.
pub(crate) struct RangedAddressLookup {
    key: [String; 8],
    target: u64,
    maximum_candidates: usize,
    header: Option<AddressV1Header>,
    probes: usize,
    phase: RangedPhase,
}

impl RangedAddressLookup {
    pub(crate) fn new(key: [String; 8], maximum_candidates: usize) -> Result<Self> {
        if maximum_candidates == 0 || maximum_candidates > ADDRESS_SERVING_CANDIDATE_CAP {
            return Err("invalid Address v1 candidate cap".to_string());
        }
        let target = route_hash(&key);
        Ok(Self {
            key,
            target,
            maximum_candidates,
            header: None,
            probes: 0,
            phase: RangedPhase::Start,
        })
    }

    fn emit_probe(
        &mut self,
        low: u64,
        high: u64,
        left: Option<u64>,
        right: Option<u64>,
    ) -> Result<RangedStep> {
        self.probes += 1;
        if self.probes > MAX_ADDRESS_INDEX_PROBES {
            return Err("Address v1 index probe cap exceeded".to_string());
        }
        let mid = low + (high - low) / 2;
        self.phase = RangedPhase::AwaitProbe {
            low,
            high,
            mid,
            left,
            right,
        };
        Ok(RangedStep::Read {
            offset: HEADER_BYTES as u64 + mid * INDEX_BYTES as u64,
            length: INDEX_BYTES as u64,
        })
    }

    fn emit_window(&mut self, lower_bound: u64) -> Result<RangedStep> {
        let header = self.header.expect("window follows header");
        if lower_bound >= header.records {
            self.phase = RangedPhase::Finished;
            return Ok(RangedStep::Done(Vec::new()));
        }
        let entries = (header.records - lower_bound).min(self.maximum_candidates as u64 + 1);
        self.phase = RangedPhase::AwaitWindow { entries };
        Ok(RangedStep::Read {
            offset: HEADER_BYTES as u64 + lower_bound * INDEX_BYTES as u64,
            length: entries * INDEX_BYTES as u64,
        })
    }

    pub(crate) fn advance(&mut self, response: Option<&[u8]>) -> Result<RangedStep> {
        match std::mem::replace(&mut self.phase, RangedPhase::Finished) {
            RangedPhase::Start => {
                if response.is_some() {
                    return Err("Address v1 lookup received an unrequested read".to_string());
                }
                self.phase = RangedPhase::AwaitHeader;
                Ok(RangedStep::Read {
                    offset: 0,
                    length: HEADER_BYTES as u64,
                })
            }
            RangedPhase::AwaitHeader => {
                let response =
                    response.ok_or_else(|| "Address v1 lookup is missing its read".to_string())?;
                let header = parse_ranged_header(response)?;
                self.header = Some(header);
                if header.records == 0 {
                    self.phase = RangedPhase::Finished;
                    return Ok(RangedStep::Done(Vec::new()));
                }
                self.emit_probe(0, header.records, None, None)
            }
            RangedPhase::AwaitProbe {
                low,
                high,
                mid,
                left,
                right,
            } => {
                let response =
                    response.ok_or_else(|| "Address v1 lookup is missing its read".to_string())?;
                let header = self.header.expect("probe follows header");
                let entry = parse_ranged_index_entry(response, &header)?;
                if left.is_some_and(|bound| entry.route_hash < bound)
                    || right.is_some_and(|bound| entry.route_hash > bound)
                {
                    return Err("Address v1 index is not sorted".to_string());
                }
                let (low, high, left, right) = if entry.route_hash < self.target {
                    (mid + 1, high, Some(entry.route_hash), right)
                } else {
                    (low, mid, left, Some(entry.route_hash))
                };
                if low < high {
                    self.emit_probe(low, high, left, right)
                } else {
                    self.emit_window(low)
                }
            }
            RangedPhase::AwaitWindow { entries } => {
                let response =
                    response.ok_or_else(|| "Address v1 lookup is missing its read".to_string())?;
                let header = self.header.expect("window follows header");
                let entry_count = usize::try_from(entries)
                    .map_err(|_| "Address v1 window overflows".to_string())?;
                if response.len() != entry_count * INDEX_BYTES {
                    return Err("Address v1 window has the wrong extent".to_string());
                }
                let mut parsed = Vec::with_capacity(entry_count);
                for index in 0..entry_count {
                    let entry = parse_ranged_index_entry(
                        &response[index * INDEX_BYTES..(index + 1) * INDEX_BYTES],
                        &header,
                    )?;
                    if let Some(previous) = parsed.last() {
                        let previous: &RangedIndexEntry = previous;
                        // Global invariants of the encoder, checked over the
                        // fetched window: index sorted by route hash and
                        // payload contiguous in index order.
                        if entry.route_hash < previous.route_hash
                            || entry.offset != previous.offset + u64::from(previous.length)
                        {
                            return Err("Address v1 window is not sorted/contiguous".to_string());
                        }
                    }
                    parsed.push(entry);
                }
                // The window starts at the binary-search lower bound: on a
                // sorted index its first hash is >= target, and any smaller
                // hash proves the index unsorted.
                if parsed
                    .first()
                    .is_some_and(|entry| entry.route_hash < self.target)
                {
                    return Err("Address v1 index is not sorted".to_string());
                }
                let run: Vec<RangedIndexEntry> = parsed
                    .iter()
                    .take_while(|entry| entry.route_hash == self.target)
                    .copied()
                    .collect();
                if run.is_empty() {
                    self.phase = RangedPhase::Finished;
                    return Ok(RangedStep::Done(Vec::new()));
                }
                // The window holds `cap + 1` entries whenever that many exist
                // past the lower bound (`emit_window`), so an equal-hash run
                // longer than the cap is always visible here and fails closed
                // instead of being truncated.
                if run.len() > self.maximum_candidates {
                    return Err("Address v1 candidate cap exceeded".to_string());
                }
                let first = run[0];
                let last = run[run.len() - 1];
                let expected_bytes = last.offset + u64::from(last.length) - first.offset;
                if expected_bytes > MAX_ADDRESS_LOOKUP_PAYLOAD_BYTES {
                    return Err("Address v1 payload run cap exceeded".to_string());
                }
                if expected_bytes == 0 {
                    // A run of zero-length records decodes to nothing without
                    // a payload read; decode_record rejects an empty payload,
                    // so surface the same failure without issuing a read.
                    return Err("Address v1 record does not reconcile".to_string());
                }
                self.phase = RangedPhase::AwaitPayload {
                    run,
                    expected_bytes,
                };
                Ok(RangedStep::Read {
                    offset: first.offset,
                    length: expected_bytes,
                })
            }
            RangedPhase::AwaitPayload {
                run,
                expected_bytes,
            } => {
                let response =
                    response.ok_or_else(|| "Address v1 lookup is missing its read".to_string())?;
                if response.len() as u64 != expected_bytes {
                    return Err("Address v1 payload run has the wrong extent".to_string());
                }
                let base = run[0].offset;
                let mut output = Vec::new();
                for entry in &run {
                    let start = usize::try_from(entry.offset - base)
                        .map_err(|_| "Address v1 payload offset overflows".to_string())?;
                    let payload = response
                        .get(start..start + entry.length as usize)
                        .ok_or_else(|| "Address v1 record is truncated".to_string())?;
                    let record = decode_record(payload, self.target)?;
                    if record.key == self.key {
                        output.push(record);
                    }
                }
                self.phase = RangedPhase::Finished;
                Ok(RangedStep::Done(output))
            }
            RangedPhase::Finished => Err("Address v1 lookup already finished".to_string()),
        }
    }
}

/// Project one construction record into the shared serving record shape used
/// by the reduce-2 lane, so `/v2/forward` emits byte-identical feature JSON
/// from either lane.
fn page_record(record: AddressV1Record) -> AddressPageRecord {
    AddressPageRecord {
        key: record.key,
        id: record.id,
        longitude: f64::from(record.longitude_e7) / 1e7,
        latitude: f64::from(record.latitude_e7) / 1e7,
        source_object_index: record.source_object_index,
        source_row_group: record.source_row_group,
        source_row_index: record.source_row_index,
        country: record.country,
        postal_city: record.postal_city,
        postcode: record.postcode,
        street: record.street,
        number: record.number,
        unit: record.unit,
        address_levels: record.address_levels,
    }
}

// ---------------------------------------------------------------------------
// Bounded R2 loader glue. Identity trust model matches the Places
// construction lane: object identities are release-pinned and spot-verified
// at admission; serving-time integrity rests on the artifact's structural
// self-checks (header reconciliation, per-entry bounds, window sort and
// contiguity, per-record key/route reconciliation) rather than per-request
// re-hashing of multi-GiB objects.

impl ShardLoader {
    pub(crate) async fn lookup_address_construction_routing(
        &self,
        object_key: &str,
    ) -> worker::Result<Rc<AddressRouting>> {
        if let Some(routing) =
            crate::places_construction_v1::cache_get(&ADDRESS_ROUTING_CACHE, object_key)
        {
            return Ok(routing);
        }
        let read = self
            .cached_bounded_prefix_read_measured(
                object_key,
                MAX_ADDRESS_ROUTING_BYTES,
                crate::stac::cache::IMMUTABLE_CACHE_TTL,
            )
            .await?
            .ok_or_else(|| not_found(object_key))?;
        let text = std::str::from_utf8(&read.bytes)
            .map_err(|_| worker::Error::RustError("address routing is not UTF-8".into()))?;
        let routing = Rc::new(AddressRouting::parse(text).map_err(worker::Error::RustError)?);
        crate::places_construction_v1::cache_put(
            &ADDRESS_ROUTING_CACHE,
            object_key,
            Rc::clone(&routing),
        );
        Ok(routing)
    }

    /// Resolve one exact structured lookup through a promoted construction
    /// slice: envelope resolution over routing.json, then a bounded
    /// range-read lookup against the single owning `OAV1ART` object.
    pub(crate) async fn lookup_address_construction(
        &self,
        key: &[String; 8],
        routing_key: &str,
        geocoder_build: &str,
    ) -> worker::Result<AddressOutcome> {
        const SUFFIX: &str = "/families/addresses/routing.json";
        let data_root = routing_key.strip_suffix(SUFFIX).ok_or_else(|| {
            worker::Error::RustError(
                "v2 address entrypoint is outside its canonical family path".into(),
            )
        })?;
        if data_root.is_empty() || data_root.contains('/') {
            return Err(worker::Error::RustError(
                "v2 address entrypoint has an invalid source version".into(),
            ));
        }
        let routing = self
            .lookup_address_construction_routing(routing_key)
            .await?;
        let object = match routing.route(&key[0], route_hash(key)) {
            AddressEnvelope::OutOfCoverage => {
                return Ok(AddressOutcome::OutOfCoverage {
                    data_version: geocoder_build.to_string(),
                    normalization_version: ADDRESS_CONSTRUCTION_NORMALIZATION_VERSION,
                })
            }
            AddressEnvelope::ProvablyEmpty => {
                return Ok(AddressOutcome::Resolved {
                    data_version: geocoder_build.to_string(),
                    normalization_version: ADDRESS_CONSTRUCTION_NORMALIZATION_VERSION,
                    candidates: Vec::new(),
                })
            }
            AddressEnvelope::Object(object) => object.to_string(),
        };
        let object_key = format!("{data_root}/families/addresses/objects/{object}");
        let records = self.ranged_address_lookup(&object_key, key).await?;
        Ok(AddressOutcome::Resolved {
            data_version: geocoder_build.to_string(),
            normalization_version: ADDRESS_CONSTRUCTION_NORMALIZATION_VERSION,
            candidates: records.into_iter().map(page_record).collect(),
        })
    }

    /// Drive [`RangedAddressLookup`] over the edge-cached exact range reader.
    /// Every read is exact-extent and fails closed on a short return; a
    /// missing object surfaces the STAC not-found sentinel.
    async fn ranged_address_lookup(
        &self,
        object_key: &str,
        key: &[String; 8],
    ) -> worker::Result<Vec<AddressV1Record>> {
        let mut reader = RangeReader::new(self, object_key);
        let mut lookup = RangedAddressLookup::new(key.clone(), ADDRESS_SERVING_CANDIDATE_CAP)
            .map_err(worker::Error::RustError)?;
        let mut response: Option<bytes::Bytes> = None;
        loop {
            match lookup
                .advance(response.as_deref())
                .map_err(worker::Error::RustError)?
            {
                RangedStep::Read { offset, length } => {
                    response = Some(
                        reader
                            .range(offset, length)
                            .await?
                            .ok_or_else(|| not_found(object_key))?,
                    );
                }
                RangedStep::Done(records) => return Ok(records),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn push_text(output: &mut Vec<u8>, value: &str) {
        output.extend_from_slice(&(value.len() as u64).to_be_bytes());
        output.extend_from_slice(value.as_bytes());
    }

    fn payload(key: &[String; 8], id: u128, number: &str) -> Vec<u8> {
        let mut output = Vec::new();
        for value in key {
            push_text(&mut output, value);
        }
        output.extend_from_slice(&id.to_be_bytes());
        output.extend_from_slice(&(-710_000_000_i32).to_be_bytes());
        output.extend_from_slice(&420_000_000_i32.to_be_bytes());
        output.extend_from_slice(&0_u32.to_be_bytes());
        output.extend_from_slice(&0_u32.to_be_bytes());
        output.extend_from_slice(&(id as u64).to_be_bytes());
        for value in ["US", "Stoneham", "02180", "Main Street", number, ""] {
            push_text(&mut output, value);
        }
        output.extend_from_slice(&2_u64.to_be_bytes());
        push_text(&mut output, "MA");
        push_text(&mut output, "Stoneham");
        output
    }

    fn artifact(mut records: Vec<(u64, Vec<u8>)>) -> Vec<u8> {
        records.sort_by_key(|item| item.0);
        artifact_presorted(records)
    }

    /// Assemble an artifact WITHOUT sorting, so tests can synthesize an index
    /// that violates the encoder's sort invariant.
    fn artifact_presorted(records: Vec<(u64, Vec<u8>)>) -> Vec<u8> {
        let payload_offset = HEADER_BYTES + records.len() * INDEX_BYTES;
        let payload_bytes: usize = records.iter().map(|item| item.1.len()).sum();
        let mut output = Vec::new();
        output.extend_from_slice(MAGIC);
        output.extend_from_slice(&1_u32.to_be_bytes());
        output.extend_from_slice(&(records.len() as u64).to_be_bytes());
        output.extend_from_slice(&(HEADER_BYTES as u64).to_be_bytes());
        output.extend_from_slice(&(payload_offset as u64).to_be_bytes());
        output.extend_from_slice(&(payload_bytes as u64).to_be_bytes());
        let mut offset = payload_offset;
        for (hash, payload) in &records {
            output.extend_from_slice(&hash.to_be_bytes());
            output.extend_from_slice(&(offset as u64).to_be_bytes());
            output.extend_from_slice(&(payload.len() as u32).to_be_bytes());
            output.extend_from_slice(&0_u32.to_be_bytes());
            offset += payload.len();
        }
        for (_, payload) in records {
            output.extend_from_slice(&payload);
        }
        output
    }

    fn decode_hex(value: &str) -> Vec<u8> {
        let value = value.trim().as_bytes();
        assert_eq!(value.len() % 2, 0);
        value
            .as_chunks::<2>()
            .0
            .iter()
            .map(|pair| {
                let digit = |byte: u8| match byte {
                    b'0'..=b'9' => byte - b'0',
                    b'a'..=b'f' => byte - b'a' + 10,
                    _ => panic!("non-hex fixture byte"),
                };
                digit(pair[0]) << 4 | digit(pair[1])
            })
            .collect()
    }

    fn key_with_number(number: &str) -> [String; 8] {
        [
            "us",
            "ma",
            "stoneham",
            "stoneham",
            "02180",
            "main street",
            number,
            "",
        ]
        .map(str::to_string)
    }

    /// The `(offset, length)` reads a driven range lookup issued.
    type IssuedReads = Vec<(u64, u64)>;

    /// Drive the range planner over in-memory bytes, returning the records and
    /// the exact reads it issued.
    fn drive(
        bytes: &[u8],
        key: &[String; 8],
        maximum_candidates: usize,
    ) -> Result<(Vec<AddressV1Record>, IssuedReads)> {
        let mut lookup = RangedAddressLookup::new(key.clone(), maximum_candidates)?;
        let mut response: Option<Vec<u8>> = None;
        let mut reads = Vec::new();
        loop {
            match lookup.advance(response.as_deref())? {
                RangedStep::Read { offset, length } => {
                    reads.push((offset, length));
                    let start = usize::try_from(offset).unwrap();
                    let end = start + usize::try_from(length).unwrap();
                    let slice = bytes
                        .get(start..end)
                        .ok_or_else(|| "test read is out of object bounds".to_string())?;
                    response = Some(slice.to_vec());
                }
                RangedStep::Done(records) => return Ok((records, reads)),
            }
        }
    }

    #[test]
    fn exact_lookup_preserves_duplicate_key_multiplicity() {
        let key = key_with_number("10");
        let other = key_with_number("99");
        let bytes = artifact(vec![
            (route_hash(&key), payload(&key, 1, "10")),
            (route_hash(&key), payload(&key, 2, "10")),
            (route_hash(&other), payload(&other, 3, "99")),
        ]);
        let parsed = AddressV1Artifact::parse(&bytes, 1_000_000, 10, 10_000).unwrap();
        let matches = parsed.lookup(&key, 10).unwrap();
        assert_eq!(matches.len(), 2);
        assert_eq!(matches[0].id, "00000000-0000-0000-0000-000000000001");
        assert_eq!(matches[1].id, "00000000-0000-0000-0000-000000000002");
        assert!(parsed.lookup(&other, 10).unwrap()[0].number == "99");
    }

    #[test]
    fn parser_rejects_dirty_reserved_index_field() {
        let key = ["us", "", "", "", "", "main", "1", ""].map(str::to_string);
        let mut bytes = artifact(vec![(route_hash(&key), payload(&key, 1, "1"))]);
        bytes[HEADER_BYTES + 23] = 1;
        assert!(AddressV1Artifact::parse(&bytes, 1_000_000, 10, 10_000).is_err());
    }

    #[test]
    fn worker_decodes_artifact_emitted_by_construction_encoder() {
        let bytes = decode_hex(include_str!(
            "../../../tests/fixtures/address_construction_v1_artifact.hex"
        ));
        let key = key_with_number("10");
        let parsed = AddressV1Artifact::parse(&bytes, 1_000_000, 10, 10_000).unwrap();
        let matches = parsed.lookup(&key, 10).unwrap();
        assert_eq!(matches.len(), 2);
        assert_eq!(matches[0].source_row_index, 0);
        assert_eq!(matches[1].source_row_index, 1);
    }

    #[test]
    fn range_planner_matches_encoder_fixture_bytes() {
        let bytes = decode_hex(include_str!(
            "../../../tests/fixtures/address_construction_v1_artifact.hex"
        ));
        let key = key_with_number("10");
        let reference = AddressV1Artifact::parse(&bytes, 1_000_000, 10, 10_000)
            .unwrap()
            .lookup(&key, 10)
            .unwrap();
        let (ranged, reads) = drive(&bytes, &key, ADDRESS_SERVING_CANDIDATE_CAP).unwrap();
        assert_eq!(ranged, reference);
        assert_eq!(reads[0], (0, HEADER_BYTES as u64));
        assert!(reads.len() <= 2 + MAX_ADDRESS_INDEX_PROBES);
    }

    /// Cross-check the range planner against the whole-buffer reference on the
    /// same bytes, across index sizes that exercise binary-search edges
    /// (1-record, even and odd counts) and hit/miss keys at both extremes.
    #[test]
    fn range_planner_agrees_with_reference_across_index_sizes() {
        for count in [1_usize, 2, 3, 4, 5, 8, 9, 16, 33] {
            let keys: Vec<[String; 8]> = (0..count)
                .map(|n| key_with_number(&n.to_string()))
                .collect();
            let bytes = artifact(
                keys.iter()
                    .enumerate()
                    .map(|(n, key)| (route_hash(key), payload(key, n as u128 + 1, &n.to_string())))
                    .collect(),
            );
            let parsed =
                AddressV1Artifact::parse(&bytes, 10_000_000, count, MAX_ADDRESS_RECORD_BYTES)
                    .unwrap();
            for key in &keys {
                let reference = parsed.lookup(key, ADDRESS_SERVING_CANDIDATE_CAP).unwrap();
                assert_eq!(reference.len(), 1, "count {count}");
                let (ranged, reads) = drive(&bytes, key, ADDRESS_SERVING_CANDIDATE_CAP).unwrap();
                assert_eq!(ranged, reference, "count {count}");
                // header + probes + window + payload, probes logarithmic.
                assert!(
                    reads.len() <= 3 + (usize::BITS - count.leading_zeros()) as usize,
                    "count {count}: {} reads",
                    reads.len()
                );
            }
            // A key whose hash misses: below the minimum, above the maximum,
            // or between occupied hashes; all resolve empty without error and
            // agree with the reference.
            for miss in ["missing", "also missing", "zz"] {
                let key = key_with_number(miss);
                let reference = parsed.lookup(&key, ADDRESS_SERVING_CANDIDATE_CAP).unwrap();
                let (ranged, _) = drive(&bytes, &key, ADDRESS_SERVING_CANDIDATE_CAP).unwrap();
                assert_eq!(ranged, reference, "count {count} miss {miss}");
                assert!(ranged.is_empty());
            }
        }
    }

    #[test]
    fn range_planner_preserves_duplicate_multiplicity_and_read_shape() {
        let key = key_with_number("10");
        let other = key_with_number("99");
        let bytes = artifact(vec![
            (route_hash(&key), payload(&key, 1, "10")),
            (route_hash(&key), payload(&key, 2, "10")),
            (route_hash(&other), payload(&other, 3, "99")),
        ]);
        let (records, reads) = drive(&bytes, &key, ADDRESS_SERVING_CANDIDATE_CAP).unwrap();
        assert_eq!(records.len(), 2);
        assert_eq!(records[0].id, "00000000-0000-0000-0000-000000000001");
        assert_eq!(records[1].id, "00000000-0000-0000-0000-000000000002");
        // The final payload read covers exactly the two-record run.
        let (offset, length) = *reads.last().unwrap();
        let run_bytes = payload(&key, 1, "10").len() + payload(&key, 2, "10").len();
        assert_eq!(length, run_bytes as u64);
        assert!(offset >= (HEADER_BYTES + 3 * INDEX_BYTES) as u64);
    }

    #[test]
    fn range_planner_handles_empty_artifact_with_one_read() {
        let bytes = artifact(Vec::new());
        let key = key_with_number("10");
        let (records, reads) = drive(&bytes, &key, ADDRESS_SERVING_CANDIDATE_CAP).unwrap();
        assert!(records.is_empty());
        assert_eq!(reads, vec![(0, HEADER_BYTES as u64)]);
    }

    #[test]
    fn range_planner_fails_closed_on_candidate_cap() {
        let key = key_with_number("10");
        let bytes = artifact(
            (0..4)
                .map(|n| (route_hash(&key), payload(&key, n + 1, "10")))
                .collect(),
        );
        assert!(drive(&bytes, &key, 3)
            .unwrap_err()
            .contains("candidate cap exceeded"));
        // At exactly the cap the run is served in full.
        let (records, _) = drive(&bytes, &key, 4).unwrap();
        assert_eq!(records.len(), 4);
    }

    #[test]
    fn range_planner_fails_closed_on_tampered_bytes() {
        let key = key_with_number("10");
        let bytes = artifact(vec![(route_hash(&key), payload(&key, 1, "10"))]);

        // Dirty reserved index field.
        let mut dirty = bytes.clone();
        dirty[HEADER_BYTES + 23] = 1;
        assert!(drive(&dirty, &key, 8).unwrap_err().contains("index entry"));

        // Header that does not reconcile (payload offset off by one).
        let mut header = bytes.clone();
        header[35] = header[35].wrapping_add(1);
        assert!(drive(&header, &key, 8)
            .unwrap_err()
            .contains("does not reconcile"));

        // Wrong magic.
        let mut magic = bytes.clone();
        magic[0] = b'X';
        assert!(drive(&magic, &key, 8).unwrap_err().contains("header"));

        // Unsupported version.
        let mut version = bytes.clone();
        version[11] = 2;
        assert!(drive(&version, &key, 8).unwrap_err().contains("version"));

        // Payload tampered so the record's embedded key no longer reproduces
        // the index hash: flip the first key byte ("us" -> "xs").
        let mut swapped = bytes.clone();
        let key_byte = HEADER_BYTES + INDEX_BYTES + 8;
        assert_eq!(swapped[key_byte], b'u');
        swapped[key_byte] = b'x';
        assert!(drive(&swapped, &key, 8)
            .unwrap_err()
            .contains("does not reconcile"));
    }

    #[test]
    fn range_planner_detects_unsorted_window() {
        let key_a = key_with_number("10");
        let key_b = key_with_number("99");
        let (low, high) = if route_hash(&key_a) < route_hash(&key_b) {
            (key_a.clone(), key_b)
        } else {
            (key_b, key_a.clone())
        };
        // Descending index order violates the sort invariant; the target is
        // the smaller hash so the window covers both entries.
        let bytes = artifact_presorted(vec![
            (route_hash(&high), payload(&high, 1, "x")),
            (route_hash(&low), payload(&low, 2, "y")),
        ]);
        let result = drive(&bytes, &low, 8);
        assert!(result.is_err(), "unsorted index must fail closed");
    }

    #[test]
    fn range_planner_rejects_protocol_misuse() {
        let key = key_with_number("10");
        let mut lookup = RangedAddressLookup::new(key.clone(), 8).unwrap();
        assert!(lookup.advance(Some(&[0_u8; 44])).is_err());
        let mut lookup = RangedAddressLookup::new(key.clone(), 8).unwrap();
        assert!(matches!(
            lookup.advance(None).unwrap(),
            RangedStep::Read {
                offset: 0,
                length: 44
            }
        ));
        assert!(lookup.advance(None).is_err());
        assert!(RangedAddressLookup::new(key.clone(), 0).is_err());
        assert!(RangedAddressLookup::new(key, ADDRESS_SERVING_CANDIDATE_CAP + 1).is_err());
    }

    #[test]
    fn page_record_projection_scales_coordinates_and_keeps_source() {
        let key = key_with_number("10");
        let bytes = artifact(vec![(route_hash(&key), payload(&key, 7, "10"))]);
        let (mut records, _) = drive(&bytes, &key, 8).unwrap();
        let record = page_record(records.remove(0));
        assert_eq!(record.longitude, -71.0);
        assert_eq!(record.latitude, 42.0);
        assert_eq!(record.id, "00000000-0000-0000-0000-000000000007");
        assert_eq!(record.source_row_index, 7);
        assert_eq!(record.number, "10");
        assert_eq!(record.address_levels, vec!["MA", "Stoneham"]);
    }

    // -----------------------------------------------------------------------
    // Routing table.

    fn routing_value() -> serde_json::Value {
        serde_json::json!({
            "schema": ADDRESS_ROUTING_SCHEMA,
            "family": "addresses",
            "key_scheme": ADDRESS_ROUTING_KEY_SCHEME,
            "partitions": [
                {"country": "de", "hash_start": 0_u64, "hash_end": u64::MAX, "object": format!("{}.av1", "d".repeat(64))},
                {"country": "us", "hash_start": 0_u64, "hash_end": 999_u64, "object": format!("{}.av1", "a".repeat(64))},
                {"country": "us", "hash_start": 1000_u64, "hash_end": 1999_u64, "object": format!("{}.av1", "b".repeat(64))},
                {"country": "us", "hash_start": 3000_u64, "hash_end": u64::MAX, "object": format!("{}.av1", "c".repeat(64))},
            ],
        })
    }

    #[test]
    fn envelope_resolution_is_inclusive_per_country_with_gap_as_empty() {
        let routing = AddressRouting::parse(&routing_value().to_string()).unwrap();
        // Inclusive at both ends of an envelope.
        assert_eq!(
            routing.route("us", 0),
            AddressEnvelope::Object(&format!("{}.av1", "a".repeat(64)))
        );
        assert_eq!(
            routing.route("us", 999),
            AddressEnvelope::Object(&format!("{}.av1", "a".repeat(64)))
        );
        // Adjacent range boundary belongs to the next envelope.
        assert_eq!(
            routing.route("us", 1000),
            AddressEnvelope::Object(&format!("{}.av1", "b".repeat(64)))
        );
        assert_eq!(
            routing.route("us", 1999),
            AddressEnvelope::Object(&format!("{}.av1", "b".repeat(64)))
        );
        // A gap between envelopes is provably empty, not an error.
        assert_eq!(routing.route("us", 2000), AddressEnvelope::ProvablyEmpty);
        assert_eq!(routing.route("us", 2999), AddressEnvelope::ProvablyEmpty);
        assert_eq!(
            routing.route("us", u64::MAX),
            AddressEnvelope::Object(&format!("{}.av1", "c".repeat(64)))
        );
        // Whole-space envelope.
        assert_eq!(
            routing.route("de", 12345),
            AddressEnvelope::Object(&format!("{}.av1", "d".repeat(64)))
        );
        // A country absent from the table is out of coverage.
        assert_eq!(routing.route("fr", 0), AddressEnvelope::OutOfCoverage);
        let names: std::collections::HashSet<&str> = routing.routed_object_names().collect();
        assert_eq!(names.len(), 4);
    }

    #[test]
    fn routing_parse_fails_closed_on_malformed_tables() {
        let ok = routing_value();

        let mut wrong_schema = ok.clone();
        wrong_schema["schema"] = serde_json::json!("overture-promoted-places-routing-v1");
        assert!(AddressRouting::parse(&wrong_schema.to_string()).is_err());

        let mut wrong_family = ok.clone();
        wrong_family["family"] = serde_json::json!("places");
        assert!(AddressRouting::parse(&wrong_family.to_string()).is_err());

        let mut wrong_scheme = ok.clone();
        wrong_scheme["key_scheme"] = serde_json::json!("country-route-hash-range-v2");
        assert!(AddressRouting::parse(&wrong_scheme.to_string()).is_err());

        let mut empty = ok.clone();
        empty["partitions"] = serde_json::json!([]);
        assert!(AddressRouting::parse(&empty.to_string()).is_err());

        // Same-country overlap (inclusive bounds touching).
        let mut overlap = ok.clone();
        overlap["partitions"][2]["hash_start"] = serde_json::json!(999_u64);
        assert!(AddressRouting::parse(&overlap.to_string())
            .unwrap_err()
            .contains("unsorted or overlap"));

        // Unsorted rows.
        let mut unsorted = ok.clone();
        let partitions = unsorted["partitions"].as_array_mut().unwrap();
        partitions.swap(1, 2);
        assert!(AddressRouting::parse(&unsorted.to_string())
            .unwrap_err()
            .contains("unsorted or overlap"));

        // Inverted envelope.
        let mut inverted = ok.clone();
        inverted["partitions"][1]["hash_end"] = serde_json::json!(0_u64);
        inverted["partitions"][1]["hash_start"] = serde_json::json!(999_u64);
        assert!(AddressRouting::parse(&inverted.to_string())
            .unwrap_err()
            .contains("inverted"));

        // Non-content-addressed object name and wrong extension.
        let mut bad_object = ok.clone();
        bad_object["partitions"][0]["object"] = serde_json::json!("../escape.av1");
        assert!(AddressRouting::parse(&bad_object.to_string()).is_err());
        let mut bad_extension = ok.clone();
        bad_extension["partitions"][0]["object"] =
            serde_json::json!(format!("{}.plrv", "d".repeat(64)));
        assert!(AddressRouting::parse(&bad_extension.to_string()).is_err());

        // Malformed country.
        let mut bad_country = ok.clone();
        bad_country["partitions"][0]["country"] = serde_json::json!("USA!");
        assert!(AddressRouting::parse(&bad_country.to_string()).is_err());
    }

    // -----------------------------------------------------------------------
    // Producer normalization parity.

    /// The worker's request-side key builder must reproduce the
    /// `address-transform-v1` producer normalization
    /// (`crates/geocoder-construction/src/main.rs::normalize`): NFC, Unicode
    /// whitespace collapse to single ASCII spaces, ASCII-only lowercasing.
    /// The vectors mirror the producer's own unit test plus collapse and
    /// case-fold edges. Known benign divergence: the worker additionally
    /// collapses the C0 separators U+001C..U+001F (Python `str.split()`
    /// semantics inherited from the reduce-2 contract) which
    /// `address-transform-v1` preserves; a query containing them can only
    /// produce an exact miss, never a wrong record, because the payload key
    /// is re-checked byte-for-byte after decode.
    #[test]
    fn lookup_key_normalization_matches_address_transform_v1_vectors() {
        let vectors = [
            // The producer's own unit vector (main.rs): NFC composes E+acute
            // to É, whitespace collapses, ASCII-only lowering keeps É and İ.
            ("  CAF\u{0045}\u{0301}\tİ  ", "cafÉ İ"),
            ("Main   Street", "main street"),
            // NBSP and EM SPACE are Unicode whitespace for both sides.
            ("A\u{00a0}B\u{2003}C", "a b c"),
            // NFC composition without ASCII case change: Å stays uppercase.
            ("\u{0041}\u{030a}ngstro\u{0308}m", "\u{00c5}ngstr\u{00f6}m"),
            ("10-B", "10-b"),
            ("", ""),
        ];
        for (raw, expected) in vectors {
            let params: std::collections::HashMap<String, String> = crate::address::FIELD_NAMES
                .iter()
                .map(|name| {
                    let value = match *name {
                        "postal_city" => raw.to_string(),
                        "country" => "US".to_string(),
                        "street" => "Main".to_string(),
                        "number" => "1".to_string(),
                        _ => String::new(),
                    };
                    ((*name).to_string(), value)
                })
                .collect();
            let key = crate::address::build_lookup_key(&params).unwrap();
            assert_eq!(key[3], expected, "normalizing {raw:?}");
        }
    }

    /// The serving route hash, the producer route hash (pinned by the
    /// encoder-emitted fixture above), and `crate::address::address_key_hash`
    /// must agree; the fixture test proves producer agreement and this pins
    /// the in-crate pair plus the frozen constants.
    #[test]
    fn route_hash_matches_address_key_hash_contract() {
        let keys = [
            key_with_number("10"),
            ["us", "", "", "", "", "main", "1", ""].map(str::to_string),
            [
                "de",
                "by",
                "münchen",
                "münchen",
                "80331",
                "marienplatz",
                "8",
                "2a",
            ]
            .map(str::to_string),
        ];
        for key in keys {
            assert_eq!(route_hash(&key), crate::address::address_key_hash(&key));
        }
        let empty: [String; 8] = Default::default();
        // FNV-1a offset basis folded over seven 0x1f separators.
        let mut expected = 0xcbf29ce484222325_u64;
        for _ in 0..7 {
            expected ^= 0x1f;
            expected = expected.wrapping_mul(0x100000001b3);
        }
        assert_eq!(route_hash(&empty), expected);
    }
}
