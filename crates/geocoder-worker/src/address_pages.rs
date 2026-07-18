//! Strict reader for the experimental range-readable address page format.
//!
//! This module deliberately has no route. `ShardLoader` uses it to prove the
//! exact R2 index -> range -> bounded gzip -> useful-record path without
//! exposing an unfinished address API.
//!
//! The framing, side-index, caps, and cursor primitives now live in the shared
//! [`geocoder_core::pages`] range-reader core; this module keeps only the
//! address-specific record payload (front-coded 8-field key, display
//! dictionary, raw address levels, heap-amplification budget) on top of them.

use std::io::{Cursor, Read};

use flate2::bufread::GzDecoder;
use geocoder_core::pages::{strip_stored_page_frame, ByteReader, PageCaps, PageError, PageIndex};
use serde::{Deserialize, Serialize};

/// Address side-index / data-object magics. Format-specific, so they stay here
/// rather than in the payload-agnostic core.
pub(crate) const INDEX_MAGIC: &[u8; 8] = b"OACIX01\0";
pub(crate) const DATA_MAGIC: &[u8; 8] = b"OACMP01\0";

/// Address decode budgets: the single shared preset.
const CAPS: PageCaps = PageCaps::ADDRESS;
/// Re-exported for the R2 bounded-prefix read in `stac::cache`.
pub(crate) const MAX_INDEX_BYTES: usize = CAPS.max_index_bytes;
/// Largest framed candidate-page range (stored payload + length prefix). The
/// coalescing planner budget for a single address page span.
pub(crate) const MAX_STORED_PAGE_RANGE: u64 =
    (CAPS.max_stored_page_bytes + geocoder_core::pages::STORED_LEN_PREFIX) as u64;
const MAX_DICTIONARY_STRING_BYTES: usize = CAPS.max_dictionary_string_bytes;
/// Largest raw `address_levels` sequence length (address-specific).
const MAX_ADDRESS_LEVELS: usize = 64;

type Result<T> = std::result::Result<T, PageError>;

/// The parsed address side index: a first-key -> page directory keyed by the
/// normalized eight-field address key.
pub(crate) type AddressPageIndex = PageIndex<[String; 8]>;

#[derive(Debug, Clone, PartialEq, Serialize)]
pub(crate) struct AddressPageRecord {
    pub key: [String; 8],
    pub id: String,
    pub longitude: f64,
    pub latitude: f64,
    pub source_object_index: u32,
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

#[derive(Debug, Serialize)]
pub(crate) struct AddressPageDecode {
    pub records: Vec<AddressPageRecord>,
    pub stored_bytes: usize,
    pub decoded_bytes: usize,
    pub materialized_bytes: usize,
}

#[derive(Debug, Deserialize)]
struct DataHeader {
    format: u32,
    variant: String,
    page_rows: usize,
}

/// Parse and validate the capped JSON header at the front of a data object.
pub(crate) fn parse_useful_gzip_header(bytes: &[u8]) -> Result<usize> {
    if bytes.len() < DATA_MAGIC.len() + 4 || &bytes[..DATA_MAGIC.len()] != DATA_MAGIC {
        return Err(PageError::new("invalid address data magic"));
    }
    let header_len = u32::from_le_bytes(
        bytes[DATA_MAGIC.len()..DATA_MAGIC.len() + 4]
            .try_into()
            .expect("four-byte slice"),
    ) as usize;
    if header_len > CAPS.max_key_bytes || DATA_MAGIC.len() + 4 + header_len > bytes.len() {
        return Err(PageError::new("address data header is outside hard bounds"));
    }
    let header: DataHeader =
        serde_json::from_slice(&bytes[DATA_MAGIC.len() + 4..DATA_MAGIC.len() + 4 + header_len])
            .map_err(|_| PageError::new("invalid address data header JSON"))?;
    if header.format != 2 || header.variant != "useful_gzip" {
        return Err(PageError::new("unsupported address data format"));
    }
    if header.page_rows == 0 || header.page_rows > 4096 {
        return Err(PageError::new("invalid address page-row target"));
    }
    Ok(header.page_rows)
}

/// Parse the address side index using the shared generic index reader, with the
/// address eight-field key decode/comparison supplied here.
pub(crate) fn parse_address_index(bytes: &[u8]) -> Result<AddressPageIndex> {
    PageIndex::parse(bytes, &CAPS, INDEX_MAGIC, parse_address_index_key)
}

fn parse_address_index_key(bytes: &[u8]) -> Result<[String; 8]> {
    let mut reader = ByteReader::new(bytes, 0);
    let mut values: Vec<String> = Vec::with_capacity(8);
    for _ in 0..8 {
        values.push(reader.text(MAX_DICTIONARY_STRING_BYTES)?);
    }
    if !reader.is_empty() {
        return Err(PageError::new("address index key has trailing bytes"));
    }
    values
        .try_into()
        .map_err(|_| PageError::new("address index key field count differs"))
}

/// Decode one framed + gzipped page and return only the records matching
/// `lookup_key`. The stored frame, decode budgets, and cursor primitives come
/// from the shared core; the record shape is address-specific.
#[cfg(test)]
pub(crate) fn decode_useful_gzip_range(
    bytes: &[u8],
    expected_rows: usize,
    lookup_key: &[String; 8],
) -> Result<Vec<AddressPageRecord>> {
    Ok(decode_useful_gzip_range_measured(bytes, expected_rows, lookup_key)?.records)
}

pub(crate) fn decode_useful_gzip_range_measured(
    bytes: &[u8],
    expected_rows: usize,
    lookup_key: &[String; 8],
) -> Result<AddressPageDecode> {
    let inner = strip_stored_page_frame(bytes, &CAPS)?;
    let mut decoder = GzDecoder::new(Cursor::new(inner));
    let mut decoded = Vec::new();
    decoder
        .by_ref()
        .take((CAPS.max_decoded_page_bytes + 1) as u64)
        .read_to_end(&mut decoded)
        .map_err(|_| PageError::new("invalid gzip address page"))?;
    if decoded.len() > CAPS.max_decoded_page_bytes {
        return Err(PageError::new("decoded address page exceeds hard byte cap"));
    }
    if decoder.get_ref().position() != inner.len() as u64 {
        return Err(PageError::new("gzip address page has trailing bytes"));
    }
    let (records, materialized_bytes) = decode_useful_page_measured(&decoded)?;
    if records.len() != expected_rows {
        return Err(PageError::new(
            "decoded address row count differs from index",
        ));
    }
    let matches: Vec<_> = records
        .into_iter()
        .filter(|record| &record.key == lookup_key)
        .collect();
    if matches.len() > CAPS.max_page_rows {
        return Err(PageError::new("address candidate cap exceeded"));
    }
    Ok(AddressPageDecode {
        records: matches,
        stored_bytes: bytes.len(),
        decoded_bytes: decoded.len(),
        materialized_bytes,
    })
}

#[cfg(test)]
pub(crate) fn decode_useful_page(bytes: &[u8]) -> Result<Vec<AddressPageRecord>> {
    Ok(decode_useful_page_measured(bytes)?.0)
}

fn decode_useful_page_measured(bytes: &[u8]) -> Result<(Vec<AddressPageRecord>, usize)> {
    let mut reader = ByteReader::new(bytes, 0);
    let rows = usize::try_from(reader.uvarint()?)
        .map_err(|_| PageError::new("address page row count is too large"))?;
    if rows == 0 || rows > CAPS.max_page_rows {
        return Err(PageError::new(
            "address page row count is outside hard bounds",
        ));
    }
    let string_count = usize::try_from(reader.uvarint()?)
        .map_err(|_| PageError::new("address dictionary count is too large"))?;
    if string_count > CAPS.max_dictionary_strings {
        return Err(PageError::new("address dictionary entry cap exceeded"));
    }
    let mut strings = Vec::with_capacity(string_count);
    for _ in 0..string_count {
        strings.push(reader.text(MAX_DICTIONARY_STRING_BYTES)?);
    }
    let sequence_count = usize::try_from(reader.uvarint()?)
        .map_err(|_| PageError::new("address sequence count is too large"))?;
    if sequence_count > rows {
        return Err(PageError::new("address sequence count exceeds row count"));
    }
    let mut sequences = Vec::with_capacity(sequence_count);
    for _ in 0..sequence_count {
        let count = usize::try_from(reader.uvarint()?)
            .map_err(|_| PageError::new("address level count is too large"))?;
        if count > MAX_ADDRESS_LEVELS {
            return Err(PageError::new("address level count exceeds hard cap"));
        }
        let mut sequence = Vec::with_capacity(count);
        for _ in 0..count {
            sequence.push(dictionary_id(&mut reader, strings.len())?);
        }
        sequences.push(sequence);
    }

    let mut previous: [Vec<u8>; 8] = Default::default();
    let mut records = Vec::with_capacity(rows);
    let mut materialized_bytes = 0_usize;
    let mut previous_full_key: Option<([String; 8], [u8; 16])> = None;
    for _ in 0..rows {
        let mut key_values: Vec<String> = Vec::with_capacity(8);
        for field in previous.iter_mut() {
            reader.apply_front_coding(field, MAX_DICTIONARY_STRING_BYTES)?;
            key_values.push(
                String::from_utf8(field.clone())
                    .map_err(|_| PageError::new("front-coded key is not UTF-8"))?,
            );
        }
        let key: [String; 8] = key_values
            .try_into()
            .map_err(|_| PageError::new("address key field count differs"))?;
        let id_bytes: [u8; 16] = reader.take(16)?.try_into().expect("sixteen-byte slice");
        if previous_full_key
            .as_ref()
            .is_some_and(|(old_key, old_id)| (&key, &id_bytes) < (old_key, old_id))
        {
            return Err(PageError::new("address page records are not sorted"));
        }
        previous_full_key = Some((key.clone(), id_bytes));
        let longitude = reader.i32_le()? as f64 / 10_000_000.0;
        let latitude = reader.i32_le()? as f64 / 10_000_000.0;
        if !(-180.0..=180.0).contains(&longitude) || !(-90.0..=90.0).contains(&latitude) {
            return Err(PageError::new(
                "address coordinates are outside valid bounds",
            ));
        }
        let source_object_index = u32::try_from(reader.uvarint()?)
            .map_err(|_| PageError::new("source object index is too large"))?;
        let source_row_group = u32::try_from(reader.uvarint()?)
            .map_err(|_| PageError::new("source row group is too large"))?;
        let source_row_index = u32::try_from(reader.uvarint()?)
            .map_err(|_| PageError::new("source row index is too large"))?;
        let mut display = [0_usize; 6];
        for slot in display.iter_mut() {
            *slot = dictionary_id(&mut reader, strings.len())?;
        }
        let sequence_id = dictionary_id(&mut reader, sequences.len())?;
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
            .ok_or_else(|| PageError::new("materialized address size overflows"))?;
        materialized_bytes = materialized_bytes
            .checked_add(string_bytes)
            .and_then(|value| value.checked_add(allocation_overhead))
            .ok_or_else(|| PageError::new("materialized address size overflows"))?;
        if materialized_bytes > CAPS.max_materialized_bytes {
            return Err(PageError::new(
                "materialized address page exceeds Worker heap budget",
            ));
        }
        records.push(AddressPageRecord {
            key,
            id: geocoder_core::pages::format_uuid(id_bytes),
            longitude,
            latitude,
            source_object_index,
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
    if !reader.is_empty() {
        return Err(PageError::new("address page has trailing decoded bytes"));
    }
    Ok((records, materialized_bytes))
}

fn dictionary_id(reader: &mut ByteReader<'_>, count: usize) -> Result<usize> {
    let id = usize::try_from(reader.uvarint()?)
        .map_err(|_| PageError::new("address dictionary ID is too large"))?;
    if id >= count {
        return Err(PageError::new("address dictionary ID is out of range"));
    }
    Ok(id)
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
            raw.extend(uvarint(0));
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
        let index = parse_address_index(&index_fixture()).unwrap();
        assert!(index.find(&key("09")).is_none());
        assert_eq!(index.find(&key("10")).unwrap().offset, 100);
        assert_eq!(index.find(&key("10a")).unwrap().offset, 100);
        assert_eq!(index.find(&key("11")).unwrap().offset, 200);
        assert_eq!(index.find(&key("11")).unwrap().rows, 1);
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
        assert!(parse_address_index(&index).is_err());

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
        let header = br#"{"format":2,"variant":"useful_gzip","page_rows":256}"#;
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

    /// Cross-language fixture: the committed `plain_page.bin` (produced by the
    /// Python `encode_page(useful=True)`) must decode to the same address
    /// records here. This pins the address record payload contract the same way
    /// the core fixtures pin the framing/extension contract.
    #[test]
    fn decodes_committed_plain_page_fixture() {
        let plain = include_bytes!("../../../tests/fixtures/pages/plain_page.bin");
        let records = decode_useful_page(plain).unwrap();
        assert_eq!(records.len(), fixture::RECORD_COUNT);
        assert_eq!(records[0].street, "Main Street");
        assert_eq!(records[0].country, "US");
        assert_eq!(records[0].address_levels, ["MA", "Cambridge"]);
        // Records are sorted by (key, id); the number field increments.
        assert_eq!(records[0].number, "10");
        assert_eq!(records[1].number, "11");
    }

    mod fixture {
        /// Record count baked into `tests/fixtures/pages/*` by the generator
        /// `tests/generate_page_fixtures.py`; kept in sync via the Python
        /// byte-for-byte regeneration test.
        pub(super) const RECORD_COUNT: usize = 3;
    }
}
