//! Range-readable construction-v1 reverse catalogs and `.plrx` shards.
//!
//! The build emits one immutable shard per populated level-8 cell. Querying is
//! deliberately four-stage and bounded: root catalog, one or more catalog
//! shards, coalesced shard header+tail index, then only intersecting populated
//! leaf payloads. No whole `.plrx` object is ever materialized by the Worker.

use std::collections::{BTreeMap, BTreeSet};

use futures::stream::{self, StreamExt, TryStreamExt};
use geocoder_core::pages::{format_uuid, ByteRange};
use serde::Serialize;
use sha2::{Digest, Sha256};
use worker::*;

use crate::range_reader::{RangeReadMetrics, RangeReader};
use crate::stac::cache::IMMUTABLE_CACHE_TTL;
use crate::stac::{not_found, ShardLoader};

const CATALOG_ROOT_MAGIC: &[u8; 8] = b"RCAT0001";
const CATALOG_SHARD_MAGIC: &[u8; 8] = b"RCAS0001";
const SHARD_MAGIC: &[u8; 8] = b"PLRX0001";
const INDEX_DOMAIN: &[u8] = b"overture-reverse-index-v1\0";
const CATALOG_ROOT_BYTES: usize = 688;
const CATALOG_ROOT_HEADER_BYTES: usize = 48;
const CATALOG_ROOT_SHARDS: usize = 16;
const CATALOG_ROOT_SHARD_BYTES: usize = 40;
const CATALOG_SHARD_HEADER_BYTES: usize = 16;
const CATALOG_CELL_ENTRY_BYTES: usize = 52;
const SHARD_HEADER_BYTES: usize = 32;
const SHARD_INDEX_ENTRY_BYTES: usize = 40;
const CELL_LEVEL: u8 = 8;
const MAX_CATALOG_SHARD_BYTES: u64 = 1024 * 1024;
// A maximally populated Address shard has 4^7 leaves. Its index is
// 4 + 16,384 * (40-byte entry + 11-byte key) = 835,588 bytes.
const MAX_SHARD_INDEX_BYTES: u64 = 1024 * 1024;
const MAX_SHARD_INDEX_ENTRIES: usize = 16_384;
const MAX_LEAVES_PER_SHARD: usize = 32;
const MAX_CELLS: usize = 4;
const MAX_PAYLOAD_RANGE_BYTES: u64 = 2 * 1024 * 1024;
const MAX_PAYLOAD_BYTES_PER_SHARD: u64 = 8 * 1024 * 1024;
const MAX_RECORD_BYTES: usize = 1024 * 1024;
const MAX_RESULTS: usize = 10;
const EARTH_RADIUS_M: f64 = 6_371_008.8;
const LONGITUDE_E7_ORIGIN: i64 = 1_800_000_000;
const LATITUDE_E7_ORIGIN: i64 = 900_000_000;
const LONGITUDE_E7_PER_CELL: i64 = 14_062_500;
const LATITUDE_E7_PER_CELL: i64 = 7_031_250;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ReverseFamily {
    Places,
    Addresses,
}

impl ReverseFamily {
    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Places => "places",
            Self::Addresses => "addresses",
        }
    }

    pub(crate) fn feature_type(self) -> &'static str {
        match self {
            Self::Places => "poi",
            Self::Addresses => "address",
        }
    }

    pub(crate) fn default_radius_m(self) -> u32 {
        match self {
            Self::Places => 250,
            Self::Addresses => 100,
        }
    }

    pub(crate) fn maximum_radius_m(self) -> u32 {
        match self {
            Self::Places => 2_000,
            Self::Addresses => 500,
        }
    }

    fn catalog_code(self) -> u8 {
        match self {
            Self::Places => 1,
            Self::Addresses => 2,
        }
    }

    fn shard_code(self) -> u8 {
        match self {
            Self::Places => 0,
            Self::Addresses => 1,
        }
    }

    fn maximum_sub_cell_level(self) -> u8 {
        match self {
            Self::Places => 5,
            Self::Addresses => 7,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub(crate) struct ReverseRecord {
    pub family: ReverseFamily,
    pub id: String,
    pub longitude: f64,
    pub latitude: f64,
    pub source_object_index: u32,
    pub source_row_group: u32,
    pub source_row_index: u64,
    pub confidence_rank: Option<u8>,
    pub primary_name: String,
    pub brand_name: String,
    pub category: String,
    pub locality: String,
    pub region: String,
    pub country: String,
    pub display_country: String,
    pub postal_city: String,
    pub postcode: String,
    pub street: String,
    pub number: String,
    pub unit: String,
    pub address_levels: Vec<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub(crate) struct ReverseHit {
    pub record: ReverseRecord,
    pub distance_m: f64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub(crate) struct ReverseCellLevel {
    pub cell: String,
    pub sub_cell_level: u8,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct ReverseQueryMetadata {
    pub radius_m: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub effective_radius_m: Option<f64>,
    pub limit: usize,
    pub sub_cell_levels: Vec<ReverseCellLevel>,
    pub cells_read: Vec<String>,
    pub leaves_read: usize,
    pub budget_exhausted: bool,
    pub ranges: RangeReadMetrics,
}

#[derive(Debug, Clone)]
pub(crate) struct ReverseFamilySearch {
    pub hits: Vec<ReverseHit>,
    pub metadata: ReverseQueryMetadata,
}

#[derive(Debug, Clone)]
struct ObjectIdentity {
    bytes: u64,
    sha256: [u8; 32],
}

#[derive(Debug, Clone)]
struct CatalogRoot {
    max_radius_m: u32,
    shards: Vec<ObjectIdentity>,
}

#[derive(Debug, Clone)]
struct CatalogCell {
    cell: u16,
    sub_cell_level: u8,
    records: u32,
    object: ObjectIdentity,
    index_bytes: u32,
}

#[derive(Debug, Clone)]
struct LeafIndex {
    hash: u64,
    key: Vec<u8>,
    records: u32,
    payload_offset: u64,
    payload_bytes: u64,
}

#[derive(Debug, Clone)]
struct ShardIndex {
    entries: Vec<LeafIndex>,
}

#[derive(Debug, Clone, Copy)]
struct GeoBox {
    min_lon: f64,
    min_lat: f64,
    max_lon: f64,
    max_lat: f64,
}

#[derive(Debug, Clone)]
struct SpatialPlan {
    cell: String,
    y: u8,
    x: u8,
    distance_m: f64,
}

#[derive(Debug, Clone)]
struct LeafPlan {
    key: Vec<u8>,
    distance_m: f64,
    y: u16,
    x: u16,
}

#[derive(Debug)]
struct CellSearch {
    hits: Vec<ReverseHit>,
    level: ReverseCellLevel,
    leaves_read: usize,
    budget_exhausted: bool,
    effective_radius_m: Option<f64>,
    metrics: RangeReadMetrics,
}

#[derive(Debug, Clone, Copy)]
struct ReverseQueryPoint {
    longitude: f64,
    latitude: f64,
    radius_m: f64,
}

fn checked_slice(bytes: &[u8], offset: usize, length: usize) -> std::result::Result<&[u8], String> {
    let end = offset
        .checked_add(length)
        .ok_or_else(|| "reverse binary extent overflows".to_string())?;
    bytes
        .get(offset..end)
        .ok_or_else(|| "reverse binary object is truncated".to_string())
}

fn u16_at(bytes: &[u8], offset: usize) -> std::result::Result<u16, String> {
    Ok(u16::from_le_bytes(
        checked_slice(bytes, offset, 2)?
            .try_into()
            .expect("two-byte slice"),
    ))
}

fn u32_at(bytes: &[u8], offset: usize) -> std::result::Result<u32, String> {
    Ok(u32::from_le_bytes(
        checked_slice(bytes, offset, 4)?
            .try_into()
            .expect("four-byte slice"),
    ))
}

fn i32_at(bytes: &[u8], offset: usize) -> std::result::Result<i32, String> {
    Ok(i32::from_le_bytes(
        checked_slice(bytes, offset, 4)?
            .try_into()
            .expect("four-byte slice"),
    ))
}

fn u64_at(bytes: &[u8], offset: usize) -> std::result::Result<u64, String> {
    Ok(u64::from_le_bytes(
        checked_slice(bytes, offset, 8)?
            .try_into()
            .expect("eight-byte slice"),
    ))
}

fn digest_at(bytes: &[u8], offset: usize) -> std::result::Result<[u8; 32], String> {
    Ok(checked_slice(bytes, offset, 32)?
        .try_into()
        .expect("32-byte slice"))
}

fn digest_hex(digest: &[u8; 32]) -> String {
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn bytes_digest(bytes: &[u8]) -> [u8; 32] {
    Sha256::digest(bytes).into()
}

fn index_hash(key: &[u8]) -> u64 {
    let mut digest = Sha256::new();
    digest.update(INDEX_DOMAIN);
    digest.update(key);
    u64::from_be_bytes(
        digest.finalize()[..8]
            .try_into()
            .expect("eight-byte digest"),
    )
}

impl CatalogRoot {
    fn parse(bytes: &[u8], family: ReverseFamily) -> std::result::Result<Self, String> {
        if bytes.len() != CATALOG_ROOT_BYTES
            || checked_slice(bytes, 0, 8)? != CATALOG_ROOT_MAGIC
            || bytes[8] != family.catalog_code()
            || bytes[9] != CELL_LEVEL
            || bytes[10] as usize != CATALOG_ROOT_SHARDS
            || bytes[11] != 0
            || u32_at(bytes, 12)? != family.maximum_radius_m()
            || u64_at(bytes, 32)? == 0
            || u32_at(bytes, 40)? == 0
            || u32_at(bytes, 44)? != 0
        {
            return Err("reverse catalog root header is invalid".into());
        }
        let min_lon = i32_at(bytes, 16)?;
        let min_lat = i32_at(bytes, 20)?;
        let max_lon = i32_at(bytes, 24)?;
        let max_lat = i32_at(bytes, 28)?;
        if min_lon >= max_lon
            || min_lat >= max_lat
            || min_lon < -1_800_000_000
            || max_lon > 1_800_000_000
            || min_lat < -900_000_000
            || max_lat > 900_000_000
        {
            return Err("reverse catalog coverage bbox is invalid".into());
        }
        let mut shards = Vec::with_capacity(CATALOG_ROOT_SHARDS);
        for shard in 0..CATALOG_ROOT_SHARDS {
            let offset = CATALOG_ROOT_HEADER_BYTES + shard * CATALOG_ROOT_SHARD_BYTES;
            let size = u64_at(bytes, offset)?;
            if !(CATALOG_SHARD_HEADER_BYTES as u64..=MAX_CATALOG_SHARD_BYTES).contains(&size) {
                return Err("reverse catalog shard identity is outside hard bounds".into());
            }
            shards.push(ObjectIdentity {
                bytes: size,
                sha256: digest_at(bytes, offset + 8)?,
            });
        }
        Ok(Self {
            max_radius_m: family.maximum_radius_m(),
            shards,
        })
    }
}

fn parse_catalog_shard(
    bytes: &[u8],
    family: ReverseFamily,
    shard_id: u8,
) -> std::result::Result<Vec<CatalogCell>, String> {
    if bytes.len() < CATALOG_SHARD_HEADER_BYTES
        || bytes.len() as u64 > MAX_CATALOG_SHARD_BYTES
        || checked_slice(bytes, 0, 8)? != CATALOG_SHARD_MAGIC
        || bytes[8] != family.catalog_code()
        || bytes[9] != CELL_LEVEL
        || bytes[10] != shard_id
        || bytes[11] != 0
    {
        return Err("reverse catalog shard header is invalid".into());
    }
    let count = u32_at(bytes, 12)? as usize;
    let expected = CATALOG_SHARD_HEADER_BYTES
        .checked_add(
            count
                .checked_mul(CATALOG_CELL_ENTRY_BYTES)
                .ok_or_else(|| "reverse catalog shard count overflows".to_string())?,
        )
        .ok_or_else(|| "reverse catalog shard size overflows".to_string())?;
    if expected != bytes.len() {
        return Err("reverse catalog shard length differs from its count".into());
    }
    let mut cells = Vec::with_capacity(count);
    let mut previous = None;
    for item in 0..count {
        let offset = CATALOG_SHARD_HEADER_BYTES + item * CATALOG_CELL_ENTRY_BYTES;
        let cell = u16_at(bytes, offset)?;
        let level = bytes[offset + 2];
        let flags = bytes[offset + 3];
        let records = u32_at(bytes, offset + 4)?;
        let size = u64_at(bytes, offset + 8)?;
        let index_bytes = u32_at(bytes, offset + 16)?;
        if flags != 0
            || level > family.maximum_sub_cell_level()
            || records == 0
            || size <= SHARD_HEADER_BYTES as u64
            || index_bytes == 0
            || u64::from(index_bytes) > size
            || u64::from(index_bytes) > MAX_SHARD_INDEX_BYTES
            || (cell >> 12) as u8 != shard_id
            || previous.is_some_and(|old| old >= cell)
        {
            return Err("reverse catalog shard entry is invalid".into());
        }
        previous = Some(cell);
        cells.push(CatalogCell {
            cell,
            sub_cell_level: level,
            records,
            object: ObjectIdentity {
                bytes: size,
                sha256: digest_at(bytes, offset + 20)?,
            },
            index_bytes,
        });
    }
    Ok(cells)
}

fn valid_leaf_key(key: &[u8], cell: u16, level: u8) -> bool {
    key.len() == 4 + level as usize
        && key[..4] == format!("{cell:04x}").as_bytes()[..]
        && key[4..].iter().all(|digit| (b'0'..=b'3').contains(digit))
}

impl ShardIndex {
    fn parse(
        header: &[u8],
        index_bytes: &[u8],
        family: ReverseFamily,
        catalog: &CatalogCell,
    ) -> std::result::Result<Self, String> {
        if header.len() != SHARD_HEADER_BYTES
            || checked_slice(header, 0, 8)? != SHARD_MAGIC
            || header[28] != family.shard_code()
            || header[29] != CELL_LEVEL
            || header[30] != catalog.sub_cell_level
            || header[31] != 0
            || u64_at(header, 8)? != u64::from(catalog.records)
        {
            return Err("reverse shard header differs from its catalog".into());
        }
        let index_offset = u64_at(header, 16)?;
        let index_count = u32_at(header, 24)? as usize;
        if index_count == 0
            || index_count > MAX_SHARD_INDEX_ENTRIES
            || index_bytes.len() != catalog.index_bytes as usize
            || index_offset
                .checked_add(index_bytes.len() as u64)
                .is_none_or(|end| end != catalog.object.bytes)
            || u32_at(index_bytes, 0)? as usize != index_count
        {
            return Err("reverse shard index envelope is invalid".into());
        }
        let fixed_bytes = index_count
            .checked_mul(SHARD_INDEX_ENTRY_BYTES)
            .ok_or_else(|| "reverse shard index count overflows".to_string())?;
        let key_start = 4_usize
            .checked_add(fixed_bytes)
            .ok_or_else(|| "reverse shard key table overflows".to_string())?;
        if key_start > index_bytes.len() {
            return Err("reverse shard index is truncated".into());
        }
        let mut entries = Vec::with_capacity(index_count);
        let mut key_position_expected = 0_u64;
        let mut previous_key: Option<(u64, Vec<u8>)> = None;
        let mut records = 0_u64;
        for item in 0..index_count {
            let offset = 4 + item * SHARD_INDEX_ENTRY_BYTES;
            let hash = u64_at(index_bytes, offset)?;
            let key_position = u64_at(index_bytes, offset + 8)?;
            let key_length = u32_at(index_bytes, offset + 16)? as usize;
            let entry_records = u32_at(index_bytes, offset + 20)?;
            let payload_offset = u64_at(index_bytes, offset + 24)?;
            let payload_bytes = u64_at(index_bytes, offset + 32)?;
            let key_at = key_start
                .checked_add(
                    usize::try_from(key_position)
                        .map_err(|_| "reverse shard key position overflows".to_string())?,
                )
                .ok_or_else(|| "reverse shard key position overflows".to_string())?;
            let key = checked_slice(index_bytes, key_at, key_length)?.to_vec();
            if key_position != key_position_expected
                || !valid_leaf_key(&key, catalog.cell, catalog.sub_cell_level)
                || hash != index_hash(&key)
                || entry_records == 0
                || payload_bytes == 0
                || payload_offset < SHARD_HEADER_BYTES as u64
                || payload_offset
                    .checked_add(payload_bytes)
                    .is_none_or(|end| end > index_offset)
                || previous_key
                    .as_ref()
                    .is_some_and(|old| old >= &(hash, key.clone()))
            {
                return Err("reverse shard index entry is invalid".into());
            }
            key_position_expected = key_position_expected
                .checked_add(key_length as u64)
                .ok_or_else(|| "reverse shard key bytes overflow".to_string())?;
            previous_key = Some((hash, key.clone()));
            records = records
                .checked_add(u64::from(entry_records))
                .ok_or_else(|| "reverse shard records overflow".to_string())?;
            entries.push(LeafIndex {
                hash,
                key,
                records: entry_records,
                payload_offset,
                payload_bytes,
            });
        }
        if key_start.checked_add(
            usize::try_from(key_position_expected)
                .map_err(|_| "reverse shard key bytes overflow".to_string())?,
        ) != Some(index_bytes.len())
            || records != u64::from(catalog.records)
        {
            return Err("reverse shard index totals do not reconcile".into());
        }
        let mut payload_order: Vec<_> = entries.iter().collect();
        payload_order.sort_by_key(|entry| entry.payload_offset);
        let mut expected_offset = SHARD_HEADER_BYTES as u64;
        for entry in payload_order {
            if entry.payload_offset != expected_offset {
                return Err("reverse shard payload extents are not contiguous".into());
            }
            expected_offset = expected_offset
                .checked_add(entry.payload_bytes)
                .ok_or_else(|| "reverse shard payload extent overflows".to_string())?;
        }
        if expected_offset != index_offset {
            return Err("reverse shard payload does not meet its index".into());
        }
        Ok(Self { entries })
    }

    fn find(&self, key: &[u8]) -> Option<&LeafIndex> {
        let hash = index_hash(key);
        self.entries
            .binary_search_by(|entry| (entry.hash, entry.key.as_slice()).cmp(&(hash, key)))
            .ok()
            .map(|position| &self.entries[position])
    }
}

fn normalize_longitude_delta(value: f64) -> f64 {
    (value + 180.0).rem_euclid(360.0) - 180.0
}

pub(crate) fn haversine_m(
    longitude_a: f64,
    latitude_a: f64,
    longitude_b: f64,
    latitude_b: f64,
) -> f64 {
    let lat_a = latitude_a.to_radians();
    let lat_b = latitude_b.to_radians();
    let dlat = lat_b - lat_a;
    let dlon = normalize_longitude_delta(longitude_b - longitude_a).to_radians();
    let half_lat = (dlat / 2.0).sin();
    let half_lon = (dlon / 2.0).sin();
    let value = half_lat * half_lat + lat_a.cos() * lat_b.cos() * half_lon * half_lon;
    2.0 * EARTH_RADIUS_M * value.sqrt().asin()
}

fn distance_to_box(longitude: f64, latitude: f64, bounds: GeoBox) -> f64 {
    let mut nearest_longitude = bounds.min_lon;
    let mut best_delta = f64::INFINITY;
    for shifted in [longitude - 360.0, longitude, longitude + 360.0] {
        let candidate = shifted.clamp(bounds.min_lon, bounds.max_lon);
        let delta = (candidate - shifted).abs();
        if delta < best_delta {
            best_delta = delta;
            nearest_longitude = candidate;
        }
    }
    if best_delta == 0.0 && (bounds.min_lat..=bounds.max_lat).contains(&latitude) {
        return 0.0;
    }

    // A lat/lon rectangle is bounded by two small-circle latitude arcs and
    // two great-circle meridian arcs. Evaluate the exact closest point on all
    // four edges; independently clamping latitude and longitude is not the
    // spherical minimum, particularly near the poles.
    let mut best = f64::INFINITY;
    for edge_latitude in [bounds.min_lat, bounds.max_lat] {
        best = best.min(haversine_m(
            longitude,
            latitude,
            nearest_longitude,
            edge_latitude,
        ));
    }
    let query_latitude = latitude.to_radians();
    for edge_longitude in [bounds.min_lon, bounds.max_lon] {
        let longitude_delta = normalize_longitude_delta(edge_longitude - longitude).to_radians();
        let stationary = query_latitude
            .sin()
            .atan2(query_latitude.cos() * longitude_delta.cos());
        let min_latitude = bounds.min_lat.to_radians();
        let max_latitude = bounds.max_lat.to_radians();
        for candidate in [
            min_latitude,
            max_latitude,
            stationary,
            stationary - std::f64::consts::PI,
            stationary + std::f64::consts::PI,
        ] {
            if (min_latitude..=max_latitude).contains(&candidate) {
                best = best.min(haversine_m(
                    longitude,
                    latitude,
                    edge_longitude,
                    candidate.to_degrees(),
                ));
            }
        }
    }
    best
}

fn latitude_span(latitude: f64, radius_m: f64) -> (f64, f64) {
    let angular = radius_m / EARTH_RADIUS_M;
    let min_lat = (latitude.to_radians() - angular)
        .max(-std::f64::consts::FRAC_PI_2)
        .to_degrees();
    let max_lat = (latitude.to_radians() + angular)
        .min(std::f64::consts::FRAC_PI_2)
        .to_degrees();
    (min_lat, max_lat)
}

fn coordinate_index(value: f64, origin: f64, span: f64) -> u8 {
    (((value + origin) / span * 256.0).floor() as i32).clamp(0, 255) as u8
}

fn cell_box(y: u8, x: u8) -> GeoBox {
    GeoBox {
        min_lon: f64::from(x) * 360.0 / 256.0 - 180.0,
        min_lat: f64::from(y) * 180.0 / 256.0 - 90.0,
        max_lon: f64::from(u16::from(x) + 1) * 360.0 / 256.0 - 180.0,
        max_lat: f64::from(u16::from(y) + 1) * 180.0 / 256.0 - 90.0,
    }
}

fn plan_cells(longitude: f64, latitude: f64, radius_m: f64) -> Vec<SpatialPlan> {
    let (min_lat, max_lat) = latitude_span(latitude, radius_m);
    let min_y = coordinate_index(min_lat, 90.0, 180.0);
    let max_y = coordinate_index(max_lat, 90.0, 180.0);
    let mut plans = Vec::new();
    for y in min_y..=max_y {
        for x in 0_u8..=255 {
            let distance = distance_to_box(longitude, latitude, cell_box(y, x));
            if distance <= radius_m + 1e-6 {
                plans.push(SpatialPlan {
                    cell: format!("{y:02x}{x:02x}"),
                    y,
                    x,
                    distance_m: distance,
                });
            }
        }
    }
    plans.sort_by(|left, right| {
        left.distance_m
            .total_cmp(&right.distance_m)
            .then((left.y, left.x).cmp(&(right.y, right.x)))
    });
    plans
}

fn leaf_digits(y: u16, x: u16, level: u8) -> Vec<u8> {
    let mut digits = Vec::with_capacity(level as usize);
    for bit in (0..level).rev() {
        let digit = (((y >> bit) & 1) << 1) | ((x >> bit) & 1);
        digits.push(b'0' + digit as u8);
    }
    digits
}

fn leaf_box(cell_y: u8, cell_x: u8, level: u8, leaf_y: u16, leaf_x: u16) -> GeoBox {
    let divisions = f64::from(1_u32 << level);
    let base = cell_box(cell_y, cell_x);
    let width = (base.max_lon - base.min_lon) / divisions;
    let height = (base.max_lat - base.min_lat) / divisions;
    GeoBox {
        min_lon: base.min_lon + f64::from(leaf_x) * width,
        min_lat: base.min_lat + f64::from(leaf_y) * height,
        max_lon: base.min_lon + f64::from(leaf_x + 1) * width,
        max_lat: base.min_lat + f64::from(leaf_y + 1) * height,
    }
}

fn plan_leaves(
    cell: &SpatialPlan,
    level: u8,
    longitude: f64,
    latitude: f64,
    radius_m: f64,
) -> Vec<LeafPlan> {
    let divisions = 1_u16 << level;
    let mut plans = Vec::new();
    for y in 0..divisions {
        for x in 0..divisions {
            let distance =
                distance_to_box(longitude, latitude, leaf_box(cell.y, cell.x, level, y, x));
            if distance <= radius_m + 1e-6 {
                let mut key = cell.cell.as_bytes().to_vec();
                key.extend_from_slice(&leaf_digits(y, x, level));
                plans.push(LeafPlan {
                    key,
                    distance_m: distance,
                    y,
                    x,
                });
            }
        }
    }
    plans.sort_by(|left, right| {
        left.distance_m
            .total_cmp(&right.distance_m)
            .then((left.y, left.x).cmp(&(right.y, right.x)))
    });
    plans
}

fn leaf_key_e7(longitude_e7: i32, latitude_e7: i32, cell: u16, level: u8) -> Vec<u8> {
    let cell_y = i64::from(cell >> 8);
    let cell_x = i64::from(cell & 0xff);
    let longitude_offset = (i64::from(longitude_e7) + LONGITUDE_E7_ORIGIN
        - cell_x * LONGITUDE_E7_PER_CELL)
        .clamp(0, LONGITUDE_E7_PER_CELL - 1);
    let latitude_offset = (i64::from(latitude_e7) + LATITUDE_E7_ORIGIN
        - cell_y * LATITUDE_E7_PER_CELL)
        .clamp(0, LATITUDE_E7_PER_CELL - 1);
    let divisions = 1_i64 << level;
    let x = (longitude_offset * divisions / LONGITUDE_E7_PER_CELL) as u16;
    let y = (latitude_offset * divisions / LATITUDE_E7_PER_CELL) as u16;
    let mut key = format!("{cell:04x}").into_bytes();
    key.extend_from_slice(&leaf_digits(y, x, level));
    key
}

fn text_at(bytes: &[u8], position: &mut usize) -> std::result::Result<String, String> {
    let length = u16_at(bytes, *position)? as usize;
    *position = position
        .checked_add(2)
        .ok_or_else(|| "reverse text offset overflows".to_string())?;
    let value = checked_slice(bytes, *position, length)?;
    *position = position
        .checked_add(length)
        .ok_or_else(|| "reverse text offset overflows".to_string())?;
    String::from_utf8(value.to_vec()).map_err(|_| "reverse text is not UTF-8".into())
}

fn decode_record(
    bytes: &[u8],
    family: ReverseFamily,
    cell: u16,
    level: u8,
    expected_key: &[u8],
) -> std::result::Result<ReverseRecord, String> {
    if bytes.len() < 40 || bytes.len() > MAX_RECORD_BYTES {
        return Err("reverse record is outside hard bounds".into());
    }
    let id: [u8; 16] = checked_slice(bytes, 0, 16)?
        .try_into()
        .expect("sixteen-byte slice");
    let longitude_e7 = i32_at(bytes, 16)?;
    let latitude_e7 = i32_at(bytes, 20)?;
    if !(-1_800_000_000..=1_800_000_000).contains(&longitude_e7)
        || !(-900_000_000..=900_000_000).contains(&latitude_e7)
        || leaf_key_e7(longitude_e7, latitude_e7, cell, level) != expected_key
    {
        return Err("reverse record is filed under the wrong leaf".into());
    }
    let mut position = 24;
    let confidence_rank = if family == ReverseFamily::Places {
        let value = *checked_slice(bytes, position, 1)?
            .first()
            .expect("one-byte slice");
        position += 1;
        Some(value)
    } else {
        None
    };
    let source_object_index = u32_at(bytes, position)?;
    let source_row_group = u32_at(bytes, position + 4)?;
    let source_row_index = u64_at(bytes, position + 8)?;
    position += 16;
    let mut record = ReverseRecord {
        family,
        id: format_uuid(id),
        longitude: f64::from(longitude_e7) / 1e7,
        latitude: f64::from(latitude_e7) / 1e7,
        source_object_index,
        source_row_group,
        source_row_index,
        confidence_rank,
        primary_name: String::new(),
        brand_name: String::new(),
        category: String::new(),
        locality: String::new(),
        region: String::new(),
        country: String::new(),
        display_country: String::new(),
        postal_city: String::new(),
        postcode: String::new(),
        street: String::new(),
        number: String::new(),
        unit: String::new(),
        address_levels: Vec::new(),
    };
    if family == ReverseFamily::Places {
        record.primary_name = text_at(bytes, &mut position)?;
        record.brand_name = text_at(bytes, &mut position)?;
        record.category = text_at(bytes, &mut position)?;
        record.locality = text_at(bytes, &mut position)?;
        record.region = text_at(bytes, &mut position)?;
        record.country = text_at(bytes, &mut position)?;
    } else {
        record.display_country = text_at(bytes, &mut position)?;
        record.postal_city = text_at(bytes, &mut position)?;
        record.postcode = text_at(bytes, &mut position)?;
        record.street = text_at(bytes, &mut position)?;
        record.number = text_at(bytes, &mut position)?;
        record.unit = text_at(bytes, &mut position)?;
        let count = u16_at(bytes, position)? as usize;
        position += 2;
        if count > 256 {
            return Err("reverse address level count exceeds cap".into());
        }
        for _ in 0..count {
            record.address_levels.push(text_at(bytes, &mut position)?);
        }
    }
    if position != bytes.len() {
        return Err("reverse record carries trailing bytes".into());
    }
    Ok(record)
}

fn decode_leaf(
    bytes: &[u8],
    entry: &LeafIndex,
    family: ReverseFamily,
    cell: u16,
    level: u8,
    query: ReverseQueryPoint,
) -> std::result::Result<Vec<ReverseHit>, String> {
    let mut position = 0_usize;
    let mut decoded = 0_u32;
    let mut hits = Vec::new();
    while position < bytes.len() {
        let length = u32_at(bytes, position)? as usize;
        position = position
            .checked_add(4)
            .ok_or_else(|| "reverse leaf position overflows".to_string())?;
        let record_bytes = checked_slice(bytes, position, length)?;
        position = position
            .checked_add(length)
            .ok_or_else(|| "reverse leaf position overflows".to_string())?;
        let record = decode_record(record_bytes, family, cell, level, &entry.key)?;
        let distance_m = haversine_m(
            query.longitude,
            query.latitude,
            record.longitude,
            record.latitude,
        );
        if distance_m <= query.radius_m + 1e-6 {
            hits.push(ReverseHit { record, distance_m });
        }
        decoded = decoded
            .checked_add(1)
            .ok_or_else(|| "reverse leaf record count overflows".to_string())?;
    }
    if decoded != entry.records {
        return Err("reverse leaf record count differs from its index".into());
    }
    Ok(hits)
}

pub(crate) fn catalog_shard_key(version: &str, family: ReverseFamily, digest: &[u8; 32]) -> String {
    format!(
        "{version}/families/{}/reverse/catalog-shards/sha256/{}.rcas",
        family.name(),
        digest_hex(digest)
    )
}

pub(crate) fn data_shard_key(version: &str, family: ReverseFamily, digest: &[u8; 32]) -> String {
    format!(
        "{version}/families/{}/reverse/shards/sha256/{}.plrx",
        family.name(),
        digest_hex(digest)
    )
}

impl ShardLoader {
    async fn reverse_catalog_cells(
        &self,
        version: &str,
        family: ReverseFamily,
        root: &CatalogRoot,
        plans: &[SpatialPlan],
        metrics: &mut RangeReadMetrics,
    ) -> Result<BTreeMap<u16, CatalogCell>> {
        let shard_ids: BTreeSet<_> = plans.iter().map(|plan| plan.y >> 4).collect();
        let loaded: Vec<_> = stream::iter(shard_ids.into_iter().map(|shard_id| {
            let identity = root.shards[shard_id as usize].clone();
            async move {
                let key = catalog_shard_key(version, family, &identity.sha256);
                let mut reader = RangeReader::new(self, &key);
                let bytes = reader
                    .range_with_ttl(0, identity.bytes, IMMUTABLE_CACHE_TTL)
                    .await?
                    .ok_or_else(|| not_found(&key))?;
                if bytes_digest(&bytes) != identity.sha256 {
                    return Err(Error::RustError(
                        "reverse catalog shard SHA-256 differs from root".into(),
                    ));
                }
                let cells =
                    parse_catalog_shard(&bytes, family, shard_id).map_err(Error::RustError)?;
                Ok::<_, Error>((cells, reader.metrics()))
            }
        }))
        .buffered(MAX_CELLS)
        .try_collect()
        .await?;
        let wanted: BTreeSet<u16> = plans
            .iter()
            .map(|plan| (u16::from(plan.y) << 8) | u16::from(plan.x))
            .collect();
        let mut cells = BTreeMap::new();
        for (catalog_cells, observed) in loaded {
            metrics.accumulate(observed);
            for cell in catalog_cells {
                if wanted.contains(&cell.cell) {
                    cells.insert(cell.cell, cell);
                }
            }
        }
        Ok(cells)
    }

    async fn reverse_cell(
        &self,
        version: &str,
        family: ReverseFamily,
        catalog: CatalogCell,
        plan: SpatialPlan,
        query: ReverseQueryPoint,
    ) -> Result<CellSearch> {
        let key = data_shard_key(version, family, &catalog.object.sha256);
        let index_bytes = u64::from(catalog.index_bytes);
        let index_offset = catalog
            .object
            .bytes
            .checked_sub(index_bytes)
            .ok_or_else(|| Error::RustError("reverse shard index offset underflows".into()))?;
        let mut reader = RangeReader::new(self, &key);
        let envelope = reader
            .coalesced_with_ttl(
                &[
                    ByteRange {
                        offset: 0,
                        length: SHARD_HEADER_BYTES as u64,
                    },
                    ByteRange {
                        offset: index_offset,
                        length: index_bytes,
                    },
                ],
                0,
                MAX_SHARD_INDEX_BYTES,
                IMMUTABLE_CACHE_TTL,
            )
            .await?;
        let index = ShardIndex::parse(&envelope[0], &envelope[1], family, &catalog)
            .map_err(Error::RustError)?;
        let leaves = plan_leaves(
            &plan,
            catalog.sub_cell_level,
            query.longitude,
            query.latitude,
            query.radius_m,
        );
        let mut budget_exhausted = leaves.len() > MAX_LEAVES_PER_SHARD;
        let mut effective_radius_m = leaves.get(MAX_LEAVES_PER_SHARD).map(|leaf| leaf.distance_m);
        let mut wants = Vec::new();
        let mut selected = Vec::new();
        let mut payload_bytes = 0_u64;
        for leaf in leaves.iter().take(MAX_LEAVES_PER_SHARD) {
            let Some(entry) = index.find(&leaf.key) else {
                continue;
            };
            let next = payload_bytes
                .checked_add(entry.payload_bytes)
                .ok_or_else(|| Error::RustError("reverse payload budget overflows".into()))?;
            if next > MAX_PAYLOAD_BYTES_PER_SHARD {
                budget_exhausted = true;
                effective_radius_m = Some(
                    effective_radius_m.map_or(leaf.distance_m, |old| old.min(leaf.distance_m)),
                );
                break;
            }
            payload_bytes = next;
            wants.push(ByteRange {
                offset: entry.payload_offset,
                length: entry.payload_bytes,
            });
            selected.push(entry);
        }
        let payloads = if wants.is_empty() {
            Vec::new()
        } else {
            reader
                .coalesced_chunked_with_ttl(&wants, 0, MAX_PAYLOAD_RANGE_BYTES, IMMUTABLE_CACHE_TTL)
                .await?
        };
        let mut hits = Vec::new();
        for (entry, payload) in selected.iter().zip(payloads.iter()) {
            hits.extend(
                decode_leaf(
                    payload,
                    entry,
                    family,
                    catalog.cell,
                    catalog.sub_cell_level,
                    query,
                )
                .map_err(Error::RustError)?,
            );
        }
        Ok(CellSearch {
            hits,
            level: ReverseCellLevel {
                cell: plan.cell,
                sub_cell_level: catalog.sub_cell_level,
            },
            leaves_read: selected.len(),
            budget_exhausted,
            effective_radius_m,
            metrics: reader.metrics(),
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn reverse_construction_family(
        &self,
        version: &str,
        family: ReverseFamily,
        root_key: &str,
        root_bytes: usize,
        root_sha256: &str,
        longitude: f64,
        latitude: f64,
        radius_m: u32,
        limit: usize,
    ) -> Result<ReverseFamilySearch> {
        if root_bytes != CATALOG_ROOT_BYTES
            || root_sha256.len() != 64
            || limit == 0
            || limit > MAX_RESULTS
            || radius_m == 0
            || radius_m > family.maximum_radius_m()
        {
            return Err(Error::RustError(
                "reverse query or entrypoint is outside hard bounds".into(),
            ));
        }
        let mut metrics = RangeReadMetrics::default();
        let mut root_reader = RangeReader::new(self, root_key);
        let root_bytes = root_reader
            .bounded_prefix(CATALOG_ROOT_BYTES, IMMUTABLE_CACHE_TTL)
            .await?
            .ok_or_else(|| not_found(root_key))?;
        metrics.accumulate(root_reader.metrics());
        if format!("{:x}", Sha256::digest(&root_bytes)) != root_sha256 {
            return Err(Error::RustError(
                "reverse catalog root SHA-256 differs from release".into(),
            ));
        }
        let root = CatalogRoot::parse(&root_bytes, family).map_err(Error::RustError)?;
        if radius_m > root.max_radius_m {
            return Err(Error::RustError(
                "reverse radius exceeds catalog geometry".into(),
            ));
        }
        let all_cells = plan_cells(longitude, latitude, f64::from(radius_m));
        let cell_budget_exhausted = all_cells.len() > MAX_CELLS;
        let mut effective_radius_m = all_cells.get(MAX_CELLS).map(|cell| cell.distance_m);
        let plans: Vec<_> = all_cells.into_iter().take(MAX_CELLS).collect();
        let catalog_cells = self
            .reverse_catalog_cells(version, family, &root, &plans, &mut metrics)
            .await?;
        let searches: Vec<_> = stream::iter(plans.into_iter().filter_map(|plan| {
            let cell = (u16::from(plan.y) << 8) | u16::from(plan.x);
            catalog_cells
                .get(&cell)
                .cloned()
                .map(|catalog| (catalog, plan))
        }))
        .map(|(catalog, plan)| async move {
            self.reverse_cell(
                version,
                family,
                catalog,
                plan,
                ReverseQueryPoint {
                    longitude,
                    latitude,
                    radius_m: f64::from(radius_m),
                },
            )
            .await
        })
        .buffered(MAX_CELLS)
        .try_collect()
        .await?;
        let mut hits = Vec::new();
        let mut levels = Vec::new();
        let mut cells_read = Vec::new();
        let mut leaves_read = 0_usize;
        let mut budget_exhausted = cell_budget_exhausted;
        for search in searches {
            metrics.accumulate(search.metrics);
            hits.extend(search.hits);
            cells_read.push(search.level.cell.clone());
            levels.push(search.level);
            leaves_read = leaves_read.saturating_add(search.leaves_read);
            budget_exhausted |= search.budget_exhausted;
            if let Some(radius) = search.effective_radius_m {
                effective_radius_m = Some(effective_radius_m.map_or(radius, |old| old.min(radius)));
            }
        }
        hits.sort_by(|left, right| {
            left.distance_m
                .total_cmp(&right.distance_m)
                .then(left.record.id.cmp(&right.record.id))
                .then(
                    (
                        left.record.source_object_index,
                        left.record.source_row_group,
                        left.record.source_row_index,
                    )
                        .cmp(&(
                            right.record.source_object_index,
                            right.record.source_row_group,
                            right.record.source_row_index,
                        )),
                )
        });
        hits.truncate(limit);
        Ok(ReverseFamilySearch {
            hits,
            metadata: ReverseQueryMetadata {
                radius_m,
                effective_radius_m: budget_exhausted
                    .then_some(effective_radius_m.unwrap_or(f64::from(radius_m))),
                limit,
                sub_cell_levels: levels,
                cells_read,
                leaves_read,
                budget_exhausted,
                ranges: metrics,
            },
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn put_text(output: &mut Vec<u8>, value: &str) {
        output.extend_from_slice(&(value.len() as u16).to_le_bytes());
        output.extend_from_slice(value.as_bytes());
    }

    fn places_fixture() -> (Vec<u8>, Vec<u8>, Vec<u8>, CatalogCell, LeafIndex) {
        let mut record = vec![1_u8; 16];
        record.extend_from_slice(&0_i32.to_le_bytes());
        record.extend_from_slice(&0_i32.to_le_bytes());
        record.push(7);
        record.extend_from_slice(&1_u32.to_le_bytes());
        record.extend_from_slice(&2_u32.to_le_bytes());
        record.extend_from_slice(&3_u64.to_le_bytes());
        for value in ["Origin", "Brand", "category", "Locality", "Region", "XX"] {
            put_text(&mut record, value);
        }
        let mut payload = Vec::new();
        payload.extend_from_slice(&(record.len() as u32).to_le_bytes());
        payload.extend_from_slice(&record);
        let key = b"8080".to_vec();
        let index_offset = SHARD_HEADER_BYTES as u64 + payload.len() as u64;
        let mut index = Vec::new();
        index.extend_from_slice(&1_u32.to_le_bytes());
        index.extend_from_slice(&index_hash(&key).to_le_bytes());
        index.extend_from_slice(&0_u64.to_le_bytes());
        index.extend_from_slice(&(key.len() as u32).to_le_bytes());
        index.extend_from_slice(&1_u32.to_le_bytes());
        index.extend_from_slice(&(SHARD_HEADER_BYTES as u64).to_le_bytes());
        index.extend_from_slice(&(payload.len() as u64).to_le_bytes());
        index.extend_from_slice(&key);
        let object_bytes = index_offset + index.len() as u64;
        let mut header = Vec::new();
        header.extend_from_slice(SHARD_MAGIC);
        header.extend_from_slice(&1_u64.to_le_bytes());
        header.extend_from_slice(&index_offset.to_le_bytes());
        header.extend_from_slice(&1_u32.to_le_bytes());
        header.extend_from_slice(&[ReverseFamily::Places.shard_code(), CELL_LEVEL, 0, 0]);
        let catalog = CatalogCell {
            cell: 0x8080,
            sub_cell_level: 0,
            records: 1,
            object: ObjectIdentity {
                bytes: object_bytes,
                sha256: [0; 32],
            },
            index_bytes: index.len() as u32,
        };
        let leaf = LeafIndex {
            hash: index_hash(&key),
            key,
            records: 1,
            payload_offset: SHARD_HEADER_BYTES as u64,
            payload_bytes: payload.len() as u64,
        };
        (header, index, payload, catalog, leaf)
    }

    #[test]
    fn index_envelope_covers_every_valid_address_leaf() {
        let level = ReverseFamily::Addresses.maximum_sub_cell_level();
        let maximum_entries = 1_usize << (2 * usize::from(level));
        let maximum_key_bytes = 4 + usize::from(level);
        let maximum_index_bytes =
            4 + maximum_entries * (SHARD_INDEX_ENTRY_BYTES + maximum_key_bytes);

        assert_eq!(maximum_entries, MAX_SHARD_INDEX_ENTRIES);
        assert_eq!(maximum_index_bytes, 835_588);
        assert!(maximum_index_bytes <= MAX_SHARD_INDEX_BYTES as usize);
    }

    #[test]
    fn binary_root_and_catalog_shard_match_r2_wire_contract() {
        let mut root = vec![0_u8; CATALOG_ROOT_BYTES];
        root[..8].copy_from_slice(CATALOG_ROOT_MAGIC);
        root[8..12].copy_from_slice(&[ReverseFamily::Places.catalog_code(), 8, 16, 0]);
        root[12..16].copy_from_slice(&2_000_u32.to_le_bytes());
        root[16..20].copy_from_slice(&(-1_800_000_000_i32).to_le_bytes());
        root[20..24].copy_from_slice(&(-900_000_000_i32).to_le_bytes());
        root[24..28].copy_from_slice(&1_800_000_000_i32.to_le_bytes());
        root[28..32].copy_from_slice(&900_000_000_i32.to_le_bytes());
        root[32..40].copy_from_slice(&1_u64.to_le_bytes());
        root[40..44].copy_from_slice(&1_u32.to_le_bytes());
        for shard in 0..16 {
            let offset = CATALOG_ROOT_HEADER_BYTES + shard * CATALOG_ROOT_SHARD_BYTES;
            root[offset..offset + 8]
                .copy_from_slice(&(CATALOG_SHARD_HEADER_BYTES as u64).to_le_bytes());
            root[offset + 8..offset + 40].fill(shard as u8);
        }
        let parsed = CatalogRoot::parse(&root, ReverseFamily::Places).unwrap();
        assert_eq!(parsed.shards.len(), 16);
        assert_eq!(parsed.shards[12].sha256, [12; 32]);

        let mut shard = Vec::new();
        shard.extend_from_slice(CATALOG_SHARD_MAGIC);
        shard.extend_from_slice(&[ReverseFamily::Places.catalog_code(), 8, 8, 0]);
        shard.extend_from_slice(&1_u32.to_le_bytes());
        shard.extend_from_slice(&0x8080_u16.to_le_bytes());
        shard.extend_from_slice(&[0, 0]);
        shard.extend_from_slice(&1_u32.to_le_bytes());
        shard.extend_from_slice(&100_u64.to_le_bytes());
        shard.extend_from_slice(&48_u32.to_le_bytes());
        shard.extend_from_slice(&[7; 32]);
        let cells = parse_catalog_shard(&shard, ReverseFamily::Places, 8).unwrap();
        assert_eq!(cells.len(), 1);
        assert_eq!(cells[0].cell, 0x8080);
        assert_eq!(cells[0].object.sha256, [7; 32]);
    }

    #[test]
    fn ranged_shard_index_and_payload_decode_self_sufficient_place() {
        let (header, index_bytes, payload, catalog, leaf) = places_fixture();
        let index =
            ShardIndex::parse(&header, &index_bytes, ReverseFamily::Places, &catalog).unwrap();
        assert_eq!(
            index.find(b"8080").unwrap().payload_bytes,
            payload.len() as u64
        );
        let hits = decode_leaf(
            &payload,
            &leaf,
            ReverseFamily::Places,
            0x8080,
            0,
            ReverseQueryPoint {
                longitude: 0.0,
                latitude: 0.0,
                radius_m: 250.0,
            },
        )
        .unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].record.primary_name, "Origin");
        assert_eq!(hits[0].record.confidence_rank, Some(7));
        assert_eq!(hits[0].distance_m, 0.0);

        let mut corrupt = index_bytes;
        corrupt[4] ^= 1;
        assert!(ShardIndex::parse(&header, &corrupt, ReverseFamily::Places, &catalog).is_err());
    }

    #[test]
    fn cell_plan_wraps_antimeridian_and_orders_ties_by_y_x() {
        let cells = plan_cells(180.0, 0.0, 2_000.0);
        assert!(cells.iter().any(|cell| cell.x == 255));
        assert!(cells.iter().any(|cell| cell.x == 0));
        assert!(cells.len() <= 4);
        assert!(cells.windows(2).all(|pair| {
            pair[0].distance_m < pair[1].distance_m
                || (pair[0].distance_m == pair[1].distance_m
                    && (pair[0].y, pair[0].x) <= (pair[1].y, pair[1].x))
        }));
    }

    #[test]
    fn polar_cell_plan_is_budgetable_and_deterministic() {
        let cells = plan_cells(0.0, 89.9, 2_000.0);
        assert!(cells.len() > MAX_CELLS);
        assert!(cells.windows(2).all(|pair| {
            pair[0].distance_m < pair[1].distance_m
                || (pair[0].distance_m == pair[1].distance_m
                    && (pair[0].y, pair[0].x) <= (pair[1].y, pair[1].x))
        }));
    }

    #[test]
    fn cell_and_leaf_plans_cover_independent_in_radius_e7_points() {
        let cases = [
            (179.9999, 0.0, 50.0, -1_799_999_000, 0),
            (-179.9999, 0.0, 50.0, 1_799_999_000, 0),
            (-0.000_000_1, 0.0, 50.0, 1, 1),
            (0.0, 66.0, 50.0, 1_000, 660_001_000),
            (0.0, -66.0, 50.0, -1_000, -660_001_000),
            (45.0, 85.0, 50.0, 450_010_000, 850_001_000),
            (-135.0, -85.0, 50.0, -1_350_010_000, -850_001_000),
            (179.999_999, 89.5, 50.0, -1_799_999_990, 895_001_000),
            (-179.999_999, -89.5, 50.0, 1_799_999_990, -895_001_000),
            (0.0, 89.9999, 20.0, 1_790_000_000, 900_000_000),
            (0.0, -89.9999, 20.0, -1_790_000_000, -900_000_000),
        ];
        for (longitude, latitude, radius_m, record_longitude_e7, record_latitude_e7) in cases {
            let record_longitude = f64::from(record_longitude_e7) / 1e7;
            let record_latitude = f64::from(record_latitude_e7) / 1e7;
            assert!(
                haversine_m(longitude, latitude, record_longitude, record_latitude) <= radius_m,
                "oracle point is outside its stated radius"
            );
            let x = coordinate_index(record_longitude, 180.0, 360.0);
            let y = coordinate_index(record_latitude, 90.0, 180.0);
            let cells = plan_cells(longitude, latitude, radius_m);
            let cell = cells
                .iter()
                .find(|cell| (cell.y, cell.x) == (y, x))
                .unwrap_or_else(|| {
                    panic!("planner omitted E7 point cell {y:02x}{x:02x} at {longitude},{latitude}")
                });
            let expected_leaf = leaf_key_e7(
                record_longitude_e7,
                record_latitude_e7,
                (u16::from(y) << 8) | u16::from(x),
                3,
            );
            assert!(
                plan_leaves(cell, 3, longitude, latitude, radius_m)
                    .iter()
                    .any(|leaf| leaf.key == expected_leaf),
                "planner omitted E7 point leaf in {y:02x}{x:02x}"
            );
        }
    }

    #[test]
    fn polar_spherical_box_regression_includes_cell_0301() {
        let longitude = -176.856_626_028_588_92;
        let latitude = -87.276_792_589_623_34;
        let radius_m = 1_748.0;
        let record_longitude = -177.187_500_1;
        let record_latitude = -87.276_837_9;
        let record_distance = haversine_m(longitude, latitude, record_longitude, record_latitude);
        assert!(record_distance < radius_m);
        assert!(distance_to_box(longitude, latitude, cell_box(0x03, 0x01)) <= record_distance);
        let cells = plan_cells(longitude, latitude, radius_m);
        assert!(cells.iter().any(|cell| (cell.y, cell.x) == (0x03, 0x01)));
        assert!(cells.len() <= MAX_CELLS);
    }

    #[test]
    fn haversine_wraps_at_the_antimeridian() {
        let distance = haversine_m(179.999, 0.0, -179.999, 0.0);
        assert!((distance - 222.39).abs() < 0.2);
    }

    #[test]
    fn e7_leaf_key_clamps_to_the_authoritative_cell() {
        assert_eq!(leaf_key_e7(0, 0, 0x8080, 2), b"808000");
        assert_eq!(
            leaf_key_e7(-1_800_000_000, -900_000_000, 0x0000, 2),
            b"000000"
        );
    }
}
