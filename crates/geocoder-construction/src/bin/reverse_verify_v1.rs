//! Independent structural/logical verifier for `.plrx` reverse serving bytes.
//!
//! Shares no code with `reverse_encode_v1.rs`, exactly as
//! `places_serving_verify_v1.rs` shares none with its encoder: every constant,
//! the `L_lon` table, the leaf-key arithmetic and the digest mechanism are
//! re-implemented here so a defect must land identically in two independent
//! implementations to survive. The mirrors are pinned by unit tests against
//! the same committed fixtures the encoder and the Python module are pinned to
//! (`tests/fixtures/reverse/l-lon-by-row-v1.json`,
//! `tests/fixtures/reverse/cell-identifier-vectors-v1.json`).
//!
//! What is verified, from the bytes alone plus the caller's declared
//! `--family/--cell/--records`:
//! * header magic/level/family/flags, and the sub-cell level re-derived from
//!   the three-ceilings rule over (--records, --cell, family);
//! * every leaf key re-derived from each record's E7 coordinates clamped into
//!   the authoritative cell;
//! * row-major leaf order and within-leaf `(feature_id, source locator)` order;
//! * index/payload reconciliation (hashes, key blob, offsets, byte and record
//!   counts per leaf, (hash, key) sort);
//! * record count == header == --records;
//! * the dual-lane additive record digest, reconciled against the encoder's
//!   `--digest-out` sidecar when `--digest` is given.

use std::fs::File;
use std::io::{BufReader, Read};
use std::path::PathBuf;

use anyhow::{bail, Context, Result};
use serde::Deserialize;
use sha2::{Digest, Sha256};

const MAGIC: &[u8; 8] = b"PLRX0001";
const CELL_LEVEL: u8 = 8;
const INDEX_DOMAIN: &[u8] = b"overture-reverse-index-v1\0";
const DIGEST_DOMAIN_A: &[u8] = b"overture-reverse-shard-v1\0";
const DIGEST_DOMAIN_B: &[u8] = b"overture-reverse-shard-v1\x01";
const ADDRESS_DICTIONARY_MAGIC: &[u8; 8] = b"ARDX0002";
const ADDRESS_DICTIONARY_FLAG: u8 = 1;
const ADDRESS_DICTIONARY_FIELDS: usize = 7;
const MAX_ADDRESS_DICTIONARY_BYTES: usize = 8 * 1024 * 1024;

const LONGITUDE_E7_ORIGIN: i64 = 1_800_000_000;
const LATITUDE_E7_ORIGIN: i64 = 900_000_000;
const LONGITUDE_E7_PER_CELL: i64 = 14_062_500;
const LATITUDE_E7_PER_CELL: i64 = 7_031_250;
const LEAF_TARGET_RECORDS: u64 = 2048;

type OrderKey = (u32, u32, [u8; 16], u32, u32, u64);

#[derive(Clone, Copy, PartialEq, Eq)]
enum Family {
    Places,
    Addresses,
}

impl Family {
    fn parse(value: &str) -> Result<Self> {
        match value {
            "places" => Ok(Family::Places),
            "addresses" => Ok(Family::Addresses),
            _ => bail!("family must be places or addresses"),
        }
    }
    fn code(self) -> u8 {
        match self {
            Family::Places => 0,
            Family::Addresses => 1,
        }
    }
    fn l_lat(self) -> i64 {
        match self {
            Family::Places => 5,
            Family::Addresses => 7,
        }
    }
    fn l_lon(self, row: usize) -> i64 {
        let runs: &[(i64, usize)] = match self {
            Family::Places => &[
                (6, 49),
                (5, 44),
                (4, 18),
                (3, 8),
                (2, 4),
                (1, 2),
                (0, 1),
                (-1, 2),
            ],
            Family::Addresses => &[
                (8, 49),
                (7, 44),
                (6, 18),
                (5, 8),
                (4, 4),
                (3, 2),
                (2, 1),
                (1, 1),
                (-1, 1),
            ],
        };
        let mut remaining = row;
        for &(value, length) in runs {
            if remaining < length {
                return value;
            }
            remaining -= length;
        }
        unreachable!("cell row index exceeds 128 rows")
    }
}

fn cell_yx(cell: &str) -> Result<(u32, u32)> {
    if cell.len() != 4
        || !cell
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
    {
        bail!("reverse partition cell is malformed: {cell:?}")
    }
    Ok((
        u32::from_str_radix(&cell[..2], 16).unwrap(),
        u32::from_str_radix(&cell[2..], 16).unwrap(),
    ))
}

fn cell_row_index(y: u32) -> usize {
    if y >= 128 {
        (y - 128) as usize
    } else {
        (127 - y) as usize
    }
}

fn sub_cell_level(records: u64, cell: &str, family: Family) -> Result<u8> {
    let (y, _) = cell_yx(cell)?;
    let mut l_records: i64 = 0;
    while l_records < family.l_lat() && records > LEAF_TARGET_RECORDS << (2 * l_records) {
        l_records += 1;
    }
    let l_lon = family.l_lon(cell_row_index(y));
    let level = l_records
        .min(family.l_lat())
        .min(l_lon)
        .clamp(0, family.l_lat());
    Ok(level as u8)
}

fn leaf_sub(longitude_e7: i64, latitude_e7: i64, x: u32, y: u32, level: u8) -> (u32, u32) {
    let dx = (longitude_e7 + LONGITUDE_E7_ORIGIN - i64::from(x) * LONGITUDE_E7_PER_CELL)
        .clamp(0, LONGITUDE_E7_PER_CELL - 1);
    let dy = (latitude_e7 + LATITUDE_E7_ORIGIN - i64::from(y) * LATITUDE_E7_PER_CELL)
        .clamp(0, LATITUDE_E7_PER_CELL - 1);
    let sub_x = ((dx << level) / LONGITUDE_E7_PER_CELL) as u32;
    let sub_y = ((dy << level) / LATITUDE_E7_PER_CELL) as u32;
    (sub_y, sub_x)
}

fn leaf_digits(sub_y: u32, sub_x: u32, level: u8) -> String {
    (0..level)
        .rev()
        .map(|bit| char::from(b'0' + ((((sub_y >> bit) & 1) << 1) | ((sub_x >> bit) & 1)) as u8))
        .collect()
}

fn index_hash(key: &[u8]) -> u64 {
    let mut digest = Sha256::new();
    digest.update(INDEX_DOMAIN);
    digest.update(key);
    u64::from_be_bytes(digest.finalize()[..8].try_into().unwrap())
}

fn record_digest(domain: &[u8], payload: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(domain);
    hasher.update((payload.len() as u64).to_be_bytes());
    hasher.update(payload);
    hasher.finalize().into()
}

fn add_256(accumulator: &mut [u8; 32], value: &[u8; 32]) {
    let mut carry = 0_u16;
    for index in (0..32).rev() {
        let sum = u16::from(accumulator[index]) + u16::from(value[index]) + carry;
        accumulator[index] = sum as u8;
        carry = sum >> 8;
    }
}

fn take<'a>(data: &'a [u8], position: &mut usize, length: usize) -> Result<&'a [u8]> {
    let end = position.checked_add(length).context("offset overflow")?;
    if end > data.len() {
        bail!("truncated entry")
    }
    let value = &data[*position..end];
    *position = end;
    Ok(value)
}
fn u16_at(data: &[u8], position: &mut usize) -> Result<u16> {
    Ok(u16::from_le_bytes(
        take(data, position, 2)?.try_into().unwrap(),
    ))
}
fn u32_at(data: &[u8], position: &mut usize) -> Result<u32> {
    Ok(u32::from_le_bytes(
        take(data, position, 4)?.try_into().unwrap(),
    ))
}
fn u64_at(data: &[u8], position: &mut usize) -> Result<u64> {
    Ok(u64::from_le_bytes(
        take(data, position, 8)?.try_into().unwrap(),
    ))
}
fn i32_at(data: &[u8], position: &mut usize) -> Result<i32> {
    Ok(i32::from_le_bytes(
        take(data, position, 4)?.try_into().unwrap(),
    ))
}
fn text_at<'a>(data: &'a [u8], position: &mut usize) -> Result<&'a str> {
    let length = u16_at(data, position)? as usize;
    Ok(std::str::from_utf8(take(data, position, length)?)?)
}

#[derive(Deserialize)]
struct Sidecar {
    records: u64,
    index_entries: u64,
    dictionary_bytes: u64,
    sum_a: String,
    sum_b: String,
}

fn parse_hex_256(value: &str) -> Result<[u8; 32]> {
    if value.len() != 64 {
        bail!("digest hex is not 32 bytes")
    }
    let mut output = [0_u8; 32];
    for (index, byte) in output.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&value[index * 2..index * 2 + 2], 16)
            .context("digest hex is malformed")?;
    }
    Ok(output)
}

struct Args {
    input: PathBuf,
    family: Family,
    cell: String,
    records: u64,
    digest: Option<PathBuf>,
}

fn args() -> Result<Args> {
    let mut input = None;
    let mut family = None;
    let mut cell = None;
    let mut records = None;
    let mut digest = None;
    let mut values = std::env::args_os().skip(1);
    while let Some(flag) = values.next() {
        let value = values.next().context("missing command-line value")?;
        match flag.to_str() {
            Some("--input") => input = Some(PathBuf::from(value)),
            Some("--family") => {
                family = Some(Family::parse(
                    value.to_str().context("family is not UTF-8")?,
                )?)
            }
            Some("--cell") => cell = value.to_str().map(str::to_owned),
            Some("--records") => {
                records = Some(
                    value
                        .to_str()
                        .context("record count is not UTF-8")?
                        .parse::<u64>()
                        .context("record count is not an unsigned integer")?,
                )
            }
            // Optional: the encoder's --digest-out sidecar to reconcile.
            Some("--digest") => digest = Some(PathBuf::from(value)),
            _ => bail!("unknown argument {}", flag.to_string_lossy()),
        }
    }
    Ok(Args {
        input: input.context("--input is required")?,
        family: family.context("--family is required")?,
        cell: cell.context("--cell is required")?,
        records: records.context("--records is required")?,
        digest,
    })
}

/// Mirror of the encoder's per-field dictionary code width.
fn code_width(count: usize) -> u8 {
    match count {
        0..=0x100 => 1,
        0x101..=0x1_0000 => 2,
        _ => 4,
    }
}

/// Read one little-endian dictionary code of the given 1/2/4-byte width.
fn code_at(data: &[u8], position: &mut usize, width: usize) -> Result<usize> {
    let bytes = take(data, position, width)?;
    let mut value = [0_u8; 4];
    value[..width].copy_from_slice(bytes);
    Ok(u32::from_le_bytes(value) as usize)
}

fn parse_address_dictionary(data: &[u8], position: &mut usize) -> Result<Vec<(usize, usize)>> {
    if take(data, position, 8)? != ADDRESS_DICTIONARY_MAGIC
        || take(data, position, 1)? != [ADDRESS_DICTIONARY_FIELDS as u8]
        || take(data, position, 1)? != [0]
        || u16_at(data, position)? != 0
    {
        bail!("reverse address dictionary header is malformed")
    }
    let mut fields = Vec::with_capacity(ADDRESS_DICTIONARY_FIELDS);
    for _ in 0..ADDRESS_DICTIONARY_FIELDS {
        let count = u32_at(data, position)? as usize;
        let width = usize::from(*take(data, position, 1)?.first().expect("one-byte slice"));
        // The width is derived from the count, so accepting any other value
        // would admit two encodings of one dictionary and break the digest.
        if width != usize::from(code_width(count)) {
            bail!("reverse address dictionary code width is not canonical")
        }
        let mut previous = None;
        for _ in 0..count {
            let value = text_at(data, position)?;
            if previous.is_some_and(|old| old >= value) {
                bail!("reverse address dictionary is not unique and sorted")
            }
            previous = Some(value);
        }
        fields.push((count, width));
    }
    Ok(fields)
}

type ParsedEntry = (i64, i64, [u8; 16], u32, u32, u64, usize);

/// Parse one record entry, returning its E7 position, order identity, and the
/// number of entry bytes consumed. Places entries are length-framed by the
/// caller. Address entries are self-delimiting through their dictionary-code
/// count, so they need no per-record u32 framing.
fn parse_entry(
    entry: &[u8],
    family: Family,
    dictionary_counts: Option<&[(usize, usize)]>,
) -> Result<ParsedEntry> {
    let mut at = 0;
    let id: [u8; 16] = take(entry, &mut at, 16)?.try_into().unwrap();
    let longitude_e7 = i64::from(i32_at(entry, &mut at)?);
    let latitude_e7 = i64::from(i32_at(entry, &mut at)?);
    if family == Family::Places {
        take(entry, &mut at, 1)?; // confidence_rank
    }
    let object = u32_at(entry, &mut at)?;
    let row_group = u32_at(entry, &mut at)?;
    let row = u64_at(entry, &mut at)?;
    if family == Family::Places {
        for _ in 0..6 {
            text_at(entry, &mut at)?;
        }
        if at != entry.len() {
            bail!("reverse entry has trailing bytes")
        }
    } else {
        let counts = dictionary_counts.context("reverse address dictionary is absent")?;
        if counts.len() != ADDRESS_DICTIONARY_FIELDS {
            bail!("reverse address dictionary field count differs")
        }
        for (count, width) in counts.iter().take(6) {
            if code_at(entry, &mut at, *width)? >= *count {
                bail!("reverse address dictionary code is out of range")
            }
        }
        let levels = u16_at(entry, &mut at)?;
        let (level_count, level_width) = counts[6];
        for _ in 0..levels {
            if code_at(entry, &mut at, level_width)? >= level_count {
                bail!("reverse address-level dictionary code is out of range")
            }
        }
    }
    if longitude_e7.abs() > LONGITUDE_E7_ORIGIN || latitude_e7.abs() > LATITUDE_E7_ORIGIN {
        bail!("reverse entry coordinate is out of world range")
    }
    Ok((longitude_e7, latitude_e7, id, object, row_group, row, at))
}

fn verify(data: &[u8], args: &Args) -> Result<()> {
    if data.len() < 36 || &data[..8] != MAGIC {
        bail!("bad reverse magic")
    }
    let count = u64::from_le_bytes(data[8..16].try_into().unwrap());
    let index_offset = usize::try_from(u64::from_le_bytes(data[16..24].try_into().unwrap()))
        .context("index offset overflows")?;
    let index_count = u32::from_le_bytes(data[24..28].try_into().unwrap()) as usize;
    let (family_code, cell_level, level, flags) = (data[28], data[29], data[30], data[31]);
    let expected_level = sub_cell_level(args.records, &args.cell, args.family)?;
    let expected_flags = if args.family == Family::Addresses {
        ADDRESS_DICTIONARY_FLAG
    } else {
        0
    };
    if family_code != args.family.code()
        || cell_level != CELL_LEVEL
        || level != expected_level
        || flags != expected_flags
        || index_offset < 32
        || index_offset > data.len()
    {
        bail!("bad reverse header")
    }
    if count != args.records {
        bail!("reverse header record count differs from --records")
    }
    let (cell_y, cell_x) = cell_yx(&args.cell)?;
    let mut position = 32;
    let dictionary_counts = if args.family == Family::Addresses {
        Some(parse_address_dictionary(
            &data[..index_offset],
            &mut position,
        )?)
    } else {
        None
    };
    let dictionary_bytes = position - 32;
    if dictionary_bytes > MAX_ADDRESS_DICTIONARY_BYTES {
        bail!("reverse address dictionary exceeds serving read cap")
    }
    let mut observed = 0_u64;
    let mut previous: Option<OrderKey> = None;
    let mut expected_index = Vec::<(u64, Vec<u8>, u64, u64, u32)>::new();
    let mut active_key: Option<Vec<u8>> = None;
    let mut sum_a = [0_u8; 32];
    let mut sum_b = [0_u8; 32];
    while position < index_offset {
        let payload_offset = position as u64;
        let (entry, encoded_bytes) = if args.family == Family::Places {
            let length = u32_at(data, &mut position)? as usize;
            (take(data, &mut position, length)?, 4_u64 + length as u64)
        } else {
            let start = position;
            let parsed = parse_entry(
                &data[start..index_offset],
                args.family,
                dictionary_counts.as_deref(),
            )?;
            position = position
                .checked_add(parsed.6)
                .context("reverse address entry offset overflows")?;
            (&data[start..position], parsed.6 as u64)
        };
        add_256(&mut sum_a, &record_digest(DIGEST_DOMAIN_A, entry));
        add_256(&mut sum_b, &record_digest(DIGEST_DOMAIN_B, entry));
        let (longitude_e7, latitude_e7, id, object, row_group, row, consumed) =
            parse_entry(entry, args.family, dictionary_counts.as_deref())?;
        if consumed != entry.len() {
            bail!("reverse entry has trailing bytes")
        }
        // Re-derive the leaf key from the record's own E7 coordinates clamped
        // into the authoritative cell -- never trust the stored index keys.
        let (sub_y, sub_x) = leaf_sub(longitude_e7, latitude_e7, cell_x, cell_y, level);
        let key = (sub_y, sub_x, id, object, row_group, row);
        if previous.as_ref().is_some_and(|value| value > &key) {
            bail!("reverse row-major order regressed")
        }
        previous = Some(key);
        let mut serving_key = args.cell.clone().into_bytes();
        serving_key.extend_from_slice(leaf_digits(sub_y, sub_x, level).as_bytes());
        if active_key.as_ref() != Some(&serving_key) {
            expected_index.push((
                index_hash(&serving_key),
                serving_key.clone(),
                payload_offset,
                0,
                0,
            ));
            active_key = Some(serving_key);
        }
        let active = expected_index.last_mut().unwrap();
        active.3 += encoded_bytes;
        active.4 += 1;
        observed += 1;
    }
    if observed != count || position != index_offset || expected_index.len() != index_count {
        bail!("reverse record count differs")
    }
    let stored_count = u32_at(data, &mut position)? as usize;
    if stored_count != index_count {
        bail!("reverse index count differs")
    }
    let fixed_start = position;
    let key_start = fixed_start
        .checked_add(
            index_count
                .checked_mul(40)
                .context("index size overflows")?,
        )
        .context("index size overflows")?;
    if key_start > data.len() {
        bail!("reverse index is truncated")
    }
    expected_index.sort_by(|left, right| (left.0, &left.1).cmp(&(right.0, &right.1)));
    let mut expected_key_offset = 0_u64;
    for expected in expected_index {
        let hash = u64_at(data, &mut position)?;
        let key_offset = u64_at(data, &mut position)?;
        let key_length = u32_at(data, &mut position)? as usize;
        let records = u32_at(data, &mut position)?;
        let payload_offset = u64_at(data, &mut position)?;
        let payload_bytes = u64_at(data, &mut position)?;
        let key_position = key_start
            .checked_add(usize::try_from(key_offset).context("key offset overflows")?)
            .context("key offset overflows")?;
        let stored_key = data
            .get(key_position..key_position + key_length)
            .context("reverse index key is truncated")?;
        if (hash, stored_key, payload_offset, payload_bytes, records)
            != (
                expected.0,
                expected.1.as_slice(),
                expected.2,
                expected.3,
                expected.4,
            )
            || key_offset != expected_key_offset
        {
            bail!("reverse index does not reconcile")
        }
        expected_key_offset += key_length as u64;
    }
    if position != key_start || key_start + expected_key_offset as usize != data.len() {
        bail!("reverse index length differs")
    }
    if let Some(path) = &args.digest {
        let sidecar: Sidecar = serde_json::from_reader(BufReader::new(
            File::open(path).context("open reverse digest sidecar")?,
        ))
        .context("parse reverse digest sidecar")?;
        if sidecar.records != count
            || sidecar.index_entries != index_count as u64
            || sidecar.dictionary_bytes != dictionary_bytes as u64
            || parse_hex_256(&sidecar.sum_a)? != sum_a
            || parse_hex_256(&sidecar.sum_b)? != sum_b
        {
            bail!("reverse digest sidecar does not reconcile")
        }
    }
    Ok(())
}

fn main() -> Result<()> {
    let args = args()?;
    let mut data = Vec::new();
    BufReader::new(File::open(&args.input)?).read_to_end(&mut data)?;
    verify(&data, &args)
}

#[cfg(test)]
mod tests {
    use super::{cell_yx, leaf_digits, leaf_sub, sub_cell_level, Family};

    /// This verifier's own `L_lon` mirror against the shared committed fixture
    /// -- the same file the encoder's and the Python module's tables are
    /// pinned to, so all three stay byte-exact without sharing code.
    #[test]
    fn l_lon_table_matches_the_shared_fixture() {
        let payload: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/reverse/l-lon-by-row-v1.json"
        ))
        .unwrap();
        for (family, name) in [(Family::Places, "poi"), (Family::Addresses, "address")] {
            let rows = payload["families"][name].as_array().unwrap();
            assert_eq!(rows.len(), 128);
            for (row, value) in rows.iter().enumerate() {
                assert_eq!(
                    family.l_lon(row),
                    value.as_i64().unwrap(),
                    "{name} row {row}"
                );
            }
        }
    }

    #[test]
    fn leaf_keys_match_the_committed_cell_identifier_vectors() {
        let payload: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/reverse/cell-identifier-vectors-v1.json"
        ))
        .unwrap();
        let vectors = payload["vectors"].as_array().unwrap();
        assert!(vectors.len() >= 300);
        for vector in vectors {
            let longitude_e7 = vector["longitude_e7"].as_i64().unwrap();
            let latitude_e7 = vector["latitude_e7"].as_i64().unwrap();
            let cell = vector["partition_cell"].as_str().unwrap();
            let (y, x) = cell_yx(cell).unwrap();
            for (level, expected) in vector["leaf_keys"].as_array().unwrap().iter().enumerate() {
                let (sub_y, sub_x) = leaf_sub(longitude_e7, latitude_e7, x, y, level as u8);
                let key = format!("{cell}{}", leaf_digits(sub_y, sub_x, level as u8));
                assert_eq!(key, expected.as_str().unwrap(), "level {level}");
            }
        }
    }

    #[test]
    fn sub_cell_level_takes_the_minimum_of_the_three_ceilings() {
        assert_eq!(sub_cell_level(2049, "8080", Family::Places).unwrap(), 1);
        assert_eq!(
            sub_cell_level(10_u64.pow(9), "8080", Family::Places).unwrap(),
            5
        );
        assert_eq!(
            sub_cell_level(10_u64.pow(9), "8080", Family::Addresses).unwrap(),
            7
        );
        assert_eq!(
            sub_cell_level(10_u64.pow(9), "f700", Family::Places).unwrap(),
            2
        );
        assert_eq!(
            sub_cell_level(10_u64.pow(9), "ff00", Family::Places).unwrap(),
            0
        );
    }
}
