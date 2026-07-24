//! Decoder for the genesis `global-v2-construction-v1` Address artifact.
//!
//! This module deliberately has no route or R2 loader in checkpoint 2. It
//! freezes and tests the Worker-side byte decoder without activating an
//! unshipped serving contract.

#![allow(dead_code)]

use geocoder_core::pages::format_uuid;

const MAGIC: &[u8; 8] = b"OAV1ART\0";
const HEADER_BYTES: usize = 44;
const INDEX_BYTES: usize = 24;

type Result<T> = std::result::Result<T, String>;

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

pub(crate) struct AddressV1Artifact<'a> {
    bytes: &'a [u8],
    records: usize,
    maximum_record_bytes: usize,
}

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

fn route_hash(fields: &[String; 8]) -> u64 {
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
            .chunks_exact(2)
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

    #[test]
    fn exact_lookup_preserves_duplicate_key_multiplicity() {
        let key = [
            "us",
            "ma",
            "stoneham",
            "stoneham",
            "02180",
            "main street",
            "10",
            "",
        ]
        .map(str::to_string);
        let other = [
            "us",
            "ma",
            "stoneham",
            "stoneham",
            "02180",
            "main street",
            "99",
            "",
        ]
        .map(str::to_string);
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
        let key = [
            "us",
            "ma",
            "stoneham",
            "stoneham",
            "02180",
            "main street",
            "10",
            "",
        ]
        .map(str::to_string);
        let parsed = AddressV1Artifact::parse(&bytes, 1_000_000, 10, 10_000).unwrap();
        let matches = parsed.lookup(&key, 10).unwrap();
        assert_eq!(matches.len(), 2);
        assert_eq!(matches[0].source_row_index, 0);
        assert_eq!(matches[1].source_row_index, 1);
    }
}
