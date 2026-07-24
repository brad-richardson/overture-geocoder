//! Dormant decoder/query boundary for Places construction-v1 serving artifacts.
//!
//! No Worker route or R2 loader references this module. Checkpoint 4 proves
//! only that independently verified construction bytes have a bounded consumer.

#![allow(dead_code)]

use geocoder_core::pages::format_uuid;
use sha2::{Digest, Sha256};

type Result<T> = std::result::Result<T, String>;
const MAXIMUM_INDEX_PROBES: usize = 32;

struct PlacesV1Index {
    hash: u64,
    key: Vec<u8>,
    payload_offset: usize,
    payload_bytes: usize,
    records: usize,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum PlacesV1Mode {
    Routed,
    Head,
}

impl PlacesV1Mode {
    fn magic(self) -> &'static [u8; 8] {
        match self {
            Self::Routed => b"PLRV0002",
            Self::Head => b"PLHD0002",
        }
    }
}

#[derive(Debug, PartialEq)]
pub(crate) struct PlacesV1Record {
    pub token: String,
    pub partition_cell: Option<String>,
    pub field_mask: u8,
    pub confidence_rank: u8,
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
        if bytes.len() > maximum_bytes || bytes.len() < 36 || &bytes[..8] != mode.magic() {
            return Err("invalid or over-cap Places v1 artifact".to_string());
        }
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
            let (record, _) = decode_entry(entry, self.mode)?;
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

fn decode_entry(data: &[u8], mode: PlacesV1Mode) -> Result<(PlacesV1Record, [u8; 16])> {
    let mut position = 0;
    let token = read_text(data, &mut position)?;
    let partition_cell = match mode {
        PlacesV1Mode::Routed => Some(read_text(data, &mut position)?),
        PlacesV1Mode::Head => None,
    };
    let field_mask = take(data, &mut position, 1)?[0];
    let confidence_rank = take(data, &mut position, 1)?[0];
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

#[cfg(test)]
mod tests {
    use super::{head_shard_id, index_hash, lookup_head_shard, PlacesV1Artifact, PlacesV1Mode};

    fn text(output: &mut Vec<u8>, value: &str) {
        output.extend_from_slice(&(value.len() as u16).to_le_bytes());
        output.extend_from_slice(value.as_bytes());
    }

    fn entry(token: &str, cell: Option<&str>, rank: u8, id: u128, row: u64) -> Vec<u8> {
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
        output.extend_from_slice(&row.to_le_bytes());
        for value in ["Cafe", "", "restaurant", "Town", "Region", "XX"] {
            text(&mut output, value);
        }
        output
    }

    fn artifact(mode: PlacesV1Mode, entries: &[Vec<u8>]) -> Vec<u8> {
        let mut output = mode.magic().to_vec();
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
}
