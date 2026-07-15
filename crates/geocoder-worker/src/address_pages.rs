//! Strict reader for the experimental range-readable address page format.
//!
//! This module deliberately has no route. `ShardLoader` uses it to prove the
//! exact R2 index -> range -> bounded gzip -> useful-record path without
//! exposing an unfinished address API.

use std::fmt;
use std::io::{Cursor, Read};

use flate2::bufread::GzDecoder;
use serde::{Deserialize, Serialize};

pub(crate) const INDEX_MAGIC: &[u8; 8] = b"OACIX01\0";
pub(crate) const DATA_MAGIC: &[u8; 8] = b"OACMP01\0";
pub(crate) const MAX_INDEX_BYTES: usize = 4 * 1024 * 1024;
pub(crate) const MAX_INDEX_ENTRIES: usize = 65_536;
pub(crate) const MAX_KEY_BYTES: usize = 64 * 1024;
pub(crate) const MAX_STORED_PAGE_BYTES: usize = 256 * 1024;
pub(crate) const MAX_DECODED_PAGE_BYTES: usize = 1024 * 1024;
pub(crate) const MAX_PAGE_ROWS: usize = 10_000;
pub(crate) const MAX_MATERIALIZED_RESULT_BYTES: usize = 8 * 1024 * 1024;
const MAX_DICTIONARY_STRINGS: usize = 100_000;
const MAX_DICTIONARY_STRING_BYTES: usize = 64 * 1024;
const MAX_ADDRESS_LEVELS: usize = 64;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct AddressPageError(String);

impl AddressPageError {
    fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

impl fmt::Display for AddressPageError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for AddressPageError {}

type Result<T> = std::result::Result<T, AddressPageError>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct AddressPageExtent {
    pub offset: u64,
    pub length: u64,
    pub rows: usize,
}

#[derive(Debug, Clone)]
struct IndexEntry {
    key: [String; 8],
    extent: AddressPageExtent,
}

#[derive(Debug, Clone)]
pub(crate) struct AddressPageIndex {
    entries: Vec<IndexEntry>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub(crate) struct AddressPageRecord {
    pub key: [String; 8],
    pub id: String,
    pub longitude: f64,
    pub latitude: f64,
    pub source_row_group: u32,
    pub source_row_index: u32,
    pub country: String,
    pub postal_city: String,
    pub postcode: String,
    pub street: String,
    pub number: String,
    pub unit: String,
    pub address_levels: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct DataHeader {
    format: u32,
    variant: String,
    page_rows: usize,
}

pub(crate) fn parse_useful_gzip_header(bytes: &[u8]) -> Result<usize> {
    if bytes.len() < DATA_MAGIC.len() + 4 || &bytes[..DATA_MAGIC.len()] != DATA_MAGIC {
        return Err(AddressPageError::new("invalid address data magic"));
    }
    let header_len = u32::from_le_bytes(
        bytes[DATA_MAGIC.len()..DATA_MAGIC.len() + 4]
            .try_into()
            .expect("four-byte slice"),
    ) as usize;
    if header_len > MAX_KEY_BYTES || DATA_MAGIC.len() + 4 + header_len > bytes.len() {
        return Err(AddressPageError::new(
            "address data header is outside hard bounds",
        ));
    }
    let header: DataHeader =
        serde_json::from_slice(&bytes[DATA_MAGIC.len() + 4..DATA_MAGIC.len() + 4 + header_len])
            .map_err(|_| AddressPageError::new("invalid address data header JSON"))?;
    if header.format != 1 || header.variant != "useful_gzip" {
        return Err(AddressPageError::new("unsupported address data format"));
    }
    if header.page_rows == 0 || header.page_rows > 4096 {
        return Err(AddressPageError::new("invalid address page-row target"));
    }
    Ok(header.page_rows)
}

impl AddressPageIndex {
    pub(crate) fn parse(bytes: &[u8]) -> Result<Self> {
        if bytes.len() > MAX_INDEX_BYTES {
            return Err(AddressPageError::new("address index exceeds hard byte cap"));
        }
        if !bytes.starts_with(INDEX_MAGIC) {
            return Err(AddressPageError::new("invalid address index magic"));
        }
        let mut cursor = SliceCursor::new(bytes, INDEX_MAGIC.len());
        let mut entries = Vec::new();
        let mut previous_key: Option<[String; 8]> = None;
        let mut previous_end = 0_u64;
        while !cursor.is_empty() {
            if entries.len() >= MAX_INDEX_ENTRIES {
                return Err(AddressPageError::new("address index entry cap exceeded"));
            }
            let offset = cursor.uvarint()?;
            let length = cursor.uvarint()?;
            let rows = usize::try_from(cursor.uvarint()?)
                .map_err(|_| AddressPageError::new("address page row count is too large"))?;
            let key_len = usize::try_from(cursor.uvarint()?)
                .map_err(|_| AddressPageError::new("address index key is too large"))?;
            if key_len > MAX_KEY_BYTES {
                return Err(AddressPageError::new(
                    "address index key exceeds hard byte cap",
                ));
            }
            let key_bytes = cursor.take(key_len)?;
            let mut key_cursor = SliceCursor::new(key_bytes, 0);
            let key_values = (0..8)
                .map(|_| key_cursor.text(MAX_DICTIONARY_STRING_BYTES))
                .collect::<Result<Vec<_>>>()?;
            let key: [String; 8] = key_values
                .try_into()
                .map_err(|_| AddressPageError::new("address index key field count differs"))?;
            if !key_cursor.is_empty() {
                return Err(AddressPageError::new(
                    "address index key has trailing bytes",
                ));
            }
            if rows == 0
                || rows > MAX_PAGE_ROWS
                || length <= 4
                || length > (MAX_STORED_PAGE_BYTES + 4) as u64
            {
                return Err(AddressPageError::new(
                    "address index page extent is outside hard bounds",
                ));
            }
            let end = offset
                .checked_add(length)
                .ok_or_else(|| AddressPageError::new("address index page extent overflows"))?;
            if offset < previous_end {
                return Err(AddressPageError::new("address index page extents overlap"));
            }
            if previous_key.as_ref().is_some_and(|old| key <= *old) {
                return Err(AddressPageError::new(
                    "address index keys are not strictly increasing",
                ));
            }
            previous_key = Some(key.clone());
            previous_end = end;
            entries.push(IndexEntry {
                key,
                extent: AddressPageExtent {
                    offset,
                    length,
                    rows,
                },
            });
        }
        if entries.is_empty() {
            return Err(AddressPageError::new("address index is empty"));
        }
        Ok(Self { entries })
    }

    pub(crate) fn find(&self, key: &[String; 8]) -> Option<&AddressPageExtent> {
        let position = self.entries.partition_point(|entry| entry.key <= *key);
        position
            .checked_sub(1)
            .map(|index| &self.entries[index].extent)
    }
}

pub(crate) fn decode_useful_gzip_range(
    bytes: &[u8],
    expected_rows: usize,
    lookup_key: &[String; 8],
) -> Result<Vec<AddressPageRecord>> {
    if bytes.len() < 4 || bytes.len() > MAX_STORED_PAGE_BYTES + 4 {
        return Err(AddressPageError::new(
            "stored address page is outside hard bounds",
        ));
    }
    let stored_len = u32::from_le_bytes(bytes[..4].try_into().expect("four-byte slice")) as usize;
    if stored_len != bytes.len() - 4 {
        return Err(AddressPageError::new(
            "address page length differs from range",
        ));
    }
    let mut decoder = GzDecoder::new(Cursor::new(&bytes[4..]));
    let mut decoded = Vec::new();
    decoder
        .by_ref()
        .take((MAX_DECODED_PAGE_BYTES + 1) as u64)
        .read_to_end(&mut decoded)
        .map_err(|_| AddressPageError::new("invalid gzip address page"))?;
    if decoded.len() > MAX_DECODED_PAGE_BYTES {
        return Err(AddressPageError::new(
            "decoded address page exceeds hard byte cap",
        ));
    }
    if decoder.get_ref().position() != stored_len as u64 {
        return Err(AddressPageError::new(
            "gzip address page has trailing bytes",
        ));
    }
    let records = decode_useful_page(&decoded)?;
    if records.len() != expected_rows {
        return Err(AddressPageError::new(
            "decoded address row count differs from index",
        ));
    }
    let matches: Vec<_> = records
        .into_iter()
        .filter(|record| &record.key == lookup_key)
        .collect();
    if matches.len() > MAX_PAGE_ROWS {
        return Err(AddressPageError::new("address candidate cap exceeded"));
    }
    Ok(matches)
}

fn decode_useful_page(bytes: &[u8]) -> Result<Vec<AddressPageRecord>> {
    let mut cursor = SliceCursor::new(bytes, 0);
    let rows = usize::try_from(cursor.uvarint()?)
        .map_err(|_| AddressPageError::new("address page row count is too large"))?;
    if rows == 0 || rows > MAX_PAGE_ROWS {
        return Err(AddressPageError::new(
            "address page row count is outside hard bounds",
        ));
    }
    let string_count = usize::try_from(cursor.uvarint()?)
        .map_err(|_| AddressPageError::new("address dictionary count is too large"))?;
    if string_count > MAX_DICTIONARY_STRINGS {
        return Err(AddressPageError::new(
            "address dictionary entry cap exceeded",
        ));
    }
    let mut strings = Vec::with_capacity(string_count);
    for _ in 0..string_count {
        strings.push(cursor.text(MAX_DICTIONARY_STRING_BYTES)?);
    }
    let sequence_count = usize::try_from(cursor.uvarint()?)
        .map_err(|_| AddressPageError::new("address sequence count is too large"))?;
    if sequence_count > rows {
        return Err(AddressPageError::new(
            "address sequence count exceeds row count",
        ));
    }
    let mut sequences = Vec::with_capacity(sequence_count);
    for _ in 0..sequence_count {
        let count = usize::try_from(cursor.uvarint()?)
            .map_err(|_| AddressPageError::new("address level count is too large"))?;
        if count > MAX_ADDRESS_LEVELS {
            return Err(AddressPageError::new(
                "address level count exceeds hard cap",
            ));
        }
        let mut sequence = Vec::with_capacity(count);
        for _ in 0..count {
            sequence.push(dictionary_id(&mut cursor, strings.len())?);
        }
        sequences.push(sequence);
    }

    let mut previous: [Vec<u8>; 8] = Default::default();
    let mut records = Vec::with_capacity(rows);
    let mut materialized_bytes = 0_usize;
    let mut previous_full_key: Option<([String; 8], [u8; 16])> = None;
    for _ in 0..rows {
        let key_values = (0..8)
            .map(|field| -> Result<String> {
                let prefix = usize::try_from(cursor.uvarint()?)
                    .map_err(|_| AddressPageError::new("front-code prefix is too large"))?;
                let suffix_len = usize::try_from(cursor.uvarint()?)
                    .map_err(|_| AddressPageError::new("front-code suffix is too large"))?;
                if prefix > previous[field].len() || suffix_len > MAX_DICTIONARY_STRING_BYTES {
                    return Err(AddressPageError::new(
                        "front-coded key is outside hard bounds",
                    ));
                }
                let suffix = cursor.take(suffix_len)?;
                previous[field].truncate(prefix);
                previous[field].extend_from_slice(suffix);
                String::from_utf8(previous[field].clone())
                    .map_err(|_| AddressPageError::new("front-coded key is not UTF-8"))
            })
            .collect::<Result<Vec<_>>>()?;
        let key: [String; 8] = key_values
            .try_into()
            .map_err(|_| AddressPageError::new("address key field count differs"))?;
        let id_bytes: [u8; 16] = cursor.take(16)?.try_into().expect("sixteen-byte slice");
        if previous_full_key
            .as_ref()
            .is_some_and(|(old_key, old_id)| (&key, &id_bytes) < (old_key, old_id))
        {
            return Err(AddressPageError::new("address page records are not sorted"));
        }
        previous_full_key = Some((key.clone(), id_bytes));
        let longitude = cursor.i32_le()? as f64 / 10_000_000.0;
        let latitude = cursor.i32_le()? as f64 / 10_000_000.0;
        if !(-180.0..=180.0).contains(&longitude) || !(-90.0..=90.0).contains(&latitude) {
            return Err(AddressPageError::new(
                "address coordinates are outside valid bounds",
            ));
        }
        let source_row_group = u32::try_from(cursor.uvarint()?)
            .map_err(|_| AddressPageError::new("source row group is too large"))?;
        let source_row_index = u32::try_from(cursor.uvarint()?)
            .map_err(|_| AddressPageError::new("source row index is too large"))?;
        let display_values = (0..6)
            .map(|_| dictionary_id(&mut cursor, strings.len()))
            .collect::<Result<Vec<_>>>()?;
        let display: [usize; 6] = display_values
            .try_into()
            .map_err(|_| AddressPageError::new("address display field count differs"))?;
        let sequence_id = dictionary_id(&mut cursor, sequences.len())?;
        // Dictionary references are compact on disk but cloning them into the
        // response can amplify memory dramatically. Charge conservative String,
        // Vec, key, display, and context storage before allocating any clones.
        let string_bytes = key.iter().map(String::len).sum::<usize>()
            + display
                .iter()
                .map(|index| strings[*index].len())
                .sum::<usize>()
            + sequences[sequence_id]
                .iter()
                .map(|index| strings[*index].len())
                .sum::<usize>();
        let allocation_overhead = 256_usize
            .checked_add(sequences[sequence_id].len().saturating_mul(32))
            .ok_or_else(|| AddressPageError::new("materialized address size overflows"))?;
        materialized_bytes = materialized_bytes
            .checked_add(string_bytes)
            .and_then(|value| value.checked_add(allocation_overhead))
            .ok_or_else(|| AddressPageError::new("materialized address size overflows"))?;
        if materialized_bytes > MAX_MATERIALIZED_RESULT_BYTES {
            return Err(AddressPageError::new(
                "materialized address page exceeds Worker heap budget",
            ));
        }
        records.push(AddressPageRecord {
            key,
            id: format_uuid(id_bytes),
            longitude,
            latitude,
            source_row_group,
            source_row_index,
            country: strings[display[0]].clone(),
            postal_city: strings[display[1]].clone(),
            postcode: strings[display[2]].clone(),
            street: strings[display[3]].clone(),
            number: strings[display[4]].clone(),
            unit: strings[display[5]].clone(),
            address_levels: sequences[sequence_id]
                .iter()
                .map(|index| strings[*index].clone())
                .collect(),
        });
    }
    if !cursor.is_empty() {
        return Err(AddressPageError::new(
            "address page has trailing decoded bytes",
        ));
    }
    Ok(records)
}

fn dictionary_id(cursor: &mut SliceCursor<'_>, count: usize) -> Result<usize> {
    let id = usize::try_from(cursor.uvarint()?)
        .map_err(|_| AddressPageError::new("address dictionary ID is too large"))?;
    if id >= count {
        return Err(AddressPageError::new(
            "address dictionary ID is out of range",
        ));
    }
    Ok(id)
}

fn format_uuid(bytes: [u8; 16]) -> String {
    format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
        bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]
    )
}

struct SliceCursor<'a> {
    bytes: &'a [u8],
    position: usize,
}

impl<'a> SliceCursor<'a> {
    fn new(bytes: &'a [u8], position: usize) -> Self {
        Self { bytes, position }
    }

    fn is_empty(&self) -> bool {
        self.position == self.bytes.len()
    }

    fn take(&mut self, length: usize) -> Result<&'a [u8]> {
        let end = self
            .position
            .checked_add(length)
            .ok_or_else(|| AddressPageError::new("address payload extent overflows"))?;
        if end > self.bytes.len() {
            return Err(AddressPageError::new("truncated address payload"));
        }
        let result = &self.bytes[self.position..end];
        self.position = end;
        Ok(result)
    }

    fn uvarint(&mut self) -> Result<u64> {
        let mut value = 0_u64;
        for shift in (0..=63).step_by(7) {
            let byte = *self.take(1)?.first().expect("one-byte slice");
            if shift == 63 && byte > 1 {
                return Err(AddressPageError::new("address varint overflows"));
            }
            value |= u64::from(byte & 0x7f) << shift;
            if byte & 0x80 == 0 {
                return Ok(value);
            }
        }
        Err(AddressPageError::new("invalid address varint"))
    }

    fn text(&mut self, max_bytes: usize) -> Result<String> {
        let length = usize::try_from(self.uvarint()?)
            .map_err(|_| AddressPageError::new("address text is too large"))?;
        if length > max_bytes {
            return Err(AddressPageError::new("address text exceeds hard byte cap"));
        }
        String::from_utf8(self.take(length)?.to_vec())
            .map_err(|_| AddressPageError::new("address text is not UTF-8"))
    }

    fn i32_le(&mut self) -> Result<i32> {
        Ok(i32::from_le_bytes(
            self.take(4)?.try_into().expect("four-byte slice"),
        ))
    }
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use flate2::write::GzEncoder;
    use flate2::Compression;

    use super::*;

    fn uvarint(mut value: u64) -> Vec<u8> {
        let mut bytes = Vec::new();
        while value >= 0x80 {
            bytes.push((value as u8 & 0x7f) | 0x80);
            value >>= 7;
        }
        bytes.push(value as u8);
        bytes
    }

    fn text(value: &str) -> Vec<u8> {
        let mut bytes = uvarint(value.len() as u64);
        bytes.extend_from_slice(value.as_bytes());
        bytes
    }

    fn key(number: &str) -> [String; 8] {
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

    fn index_fixture() -> Vec<u8> {
        let mut bytes = INDEX_MAGIC.to_vec();
        for (offset, number) in [(100_u64, "10"), (200, "11")] {
            let key_bytes: Vec<u8> = key(number).iter().flat_map(|value| text(value)).collect();
            bytes.extend(uvarint(offset));
            bytes.extend(uvarint(50));
            bytes.extend(uvarint(1));
            bytes.extend(uvarint(key_bytes.len() as u64));
            bytes.extend(key_bytes);
        }
        bytes
    }

    fn useful_page_fixture(candidate_count: usize) -> Vec<u8> {
        let strings = [
            "",
            "02180",
            "10",
            "Main Street",
            "MA",
            "Middlesex",
            "Stoneham",
            "US",
        ];
        let mut raw = uvarint(candidate_count as u64);
        raw.extend(uvarint(strings.len() as u64));
        for value in strings {
            raw.extend(text(value));
        }
        raw.extend(uvarint(1));
        raw.extend(uvarint(3));
        raw.extend([uvarint(4), uvarint(5), uvarint(6)].concat());
        let lookup = key("10");
        let mut previous = [
            Vec::<u8>::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
        ];
        for id in 1..=candidate_count {
            for (field, value) in lookup.iter().enumerate() {
                let value = value.as_bytes();
                let prefix = previous[field]
                    .iter()
                    .zip(value)
                    .take_while(|(a, b)| a == b)
                    .count();
                raw.extend(uvarint(prefix as u64));
                raw.extend(uvarint((value.len() - prefix) as u64));
                raw.extend_from_slice(&value[prefix..]);
                previous[field] = value.to_vec();
            }
            let mut uuid = [0_u8; 16];
            uuid[15] = id as u8;
            raw.extend(uuid);
            raw.extend((-710_999_000_i32).to_le_bytes());
            raw.extend(424_801_000_i32.to_le_bytes());
            raw.extend(uvarint(12));
            raw.extend(uvarint(id as u64));
            // country, city, postcode, street, number, unit, level sequence
            for dictionary_id in [7_u64, 6, 1, 3, 2, 0, 0] {
                raw.extend(uvarint(dictionary_id));
            }
        }
        let mut encoder = GzEncoder::new(Vec::new(), Compression::new(6));
        encoder.write_all(&raw).unwrap();
        let stored = encoder.finish().unwrap();
        let mut framed = (stored.len() as u32).to_le_bytes().to_vec();
        framed.extend(stored);
        framed
    }

    fn dictionary_amplification_fixture() -> Vec<u8> {
        let huge = "x".repeat(MAX_DICTIONARY_STRING_BYTES);
        let mut raw = uvarint(32);
        raw.extend(uvarint(2));
        raw.extend(text(""));
        raw.extend(text(&huge));
        raw.extend(uvarint(1));
        raw.extend(uvarint(1));
        raw.extend(uvarint(1));
        let mut previous = [
            Vec::<u8>::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
        ];
        for id in 1..=32_u8 {
            for field in &mut previous {
                raw.extend(uvarint(field.len() as u64));
                raw.extend(uvarint(0));
            }
            let mut uuid = [0_u8; 16];
            uuid[15] = id;
            raw.extend(uuid);
            raw.extend(0_i32.to_le_bytes());
            raw.extend(0_i32.to_le_bytes());
            raw.extend(uvarint(0));
            raw.extend(uvarint(u64::from(id)));
            // Six display fields all reference the huge string, as does context.
            for dictionary_id in [1_u64, 1, 1, 1, 1, 1, 0] {
                raw.extend(uvarint(dictionary_id));
            }
        }
        let mut encoder = GzEncoder::new(Vec::new(), Compression::new(6));
        encoder.write_all(&raw).unwrap();
        let stored = encoder.finish().unwrap();
        let mut framed = (stored.len() as u32).to_le_bytes().to_vec();
        framed.extend(stored);
        framed
    }

    #[test]
    fn parses_index_and_selects_predecessor_page() {
        let index = AddressPageIndex::parse(&index_fixture()).unwrap();
        assert!(index.find(&key("09")).is_none());
        assert_eq!(index.find(&key("10")).unwrap().offset, 100);
        assert_eq!(index.find(&key("10a")).unwrap().offset, 100);
        assert_eq!(index.find(&key("11")).unwrap().offset, 200);
    }

    #[test]
    fn decodes_all_candidates_from_one_bounded_gzip_range() {
        let lookup = key("10");
        let records = decode_useful_gzip_range(&useful_page_fixture(5), 5, &lookup).unwrap();
        assert_eq!(records.len(), 5);
        assert_eq!(records[0].street, "Main Street");
        assert_eq!(records[0].address_levels, ["MA", "Middlesex", "Stoneham"]);
        assert_eq!(records[4].source_row_index, 5);
    }

    #[test]
    fn rejects_index_overlap_row_mismatch_and_trailing_gzip_bytes() {
        let mut index = index_fixture();
        // The second offset's single-byte varint starts after the first entry.
        let second = index
            .iter()
            .rposition(|byte| *byte == 200_u8 | 0x80)
            .unwrap();
        index[second] = 120;
        index[second + 1] = 0;
        assert!(AddressPageIndex::parse(&index).is_err());

        let page = useful_page_fixture(2);
        assert!(decode_useful_gzip_range(&page, 3, &key("10")).is_err());
        let mut trailing = page;
        trailing.push(0);
        let len = (trailing.len() - 4) as u32;
        trailing[..4].copy_from_slice(&len.to_le_bytes());
        assert!(decode_useful_gzip_range(&trailing, 2, &key("10")).is_err());
    }

    #[test]
    fn validates_data_header_before_page_reads() {
        let header = br#"{"format":1,"variant":"useful_gzip","page_rows":256}"#;
        let mut bytes = DATA_MAGIC.to_vec();
        bytes.extend((header.len() as u32).to_le_bytes());
        bytes.extend(header);
        assert_eq!(parse_useful_gzip_header(&bytes).unwrap(), 256);
    }

    #[test]
    fn rejects_small_dictionary_page_with_extreme_heap_amplification() {
        let page = dictionary_amplification_fixture();
        assert!(page.len() < 4096);
        let error = decode_useful_gzip_range(&page, 32, &key("10")).unwrap_err();
        assert!(error.to_string().contains("heap budget"));
    }
}
