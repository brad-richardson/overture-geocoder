//! Deterministic `.plrx` reverse serving shard encoder, family-generic.
//!
//! One shard per populated level-8 cell (docs/plans/2026-07-25-reverse-v2-design.md
//! section 2): payload records grouped by leaf key (4-hex cell + L base-4
//! sub-digits), leaves written in ROW-MAJOR order (ascending leaf y, then leaf
//! x), the forward serving encoder's hash index over the leaf keys, and the
//! same dual-lane additive digest mechanism as `places_serving_encode_v1.rs`.
//!
//! Pinned decisions (reverse R1-b):
//! * Binary names are family-generic — `reverse-encode-v1` / `reverse-verify-v1`
//!   with `--family places|addresses`. The header family byte is 0 for places
//!   and 1 for addresses; the depth ceilings map onto the design's family names
//!   (places -> "poi", addresses -> "address"), as mirrored from
//!   `scripts/reverse_shard_v1.py`.
//! * Places `f64` coordinates become E7 via `round_ties_even`, matching the
//!   address transform (`crates/geocoder-construction/src/main.rs`), NOT
//!   truncation.
//! * `L_lon` is the pinned per-row integer table mirrored from
//!   `scripts/reverse_shard_v1.py`; a unit test pins all 128 values per family
//!   against the shared committed fixture
//!   `tests/fixtures/reverse/l-lon-by-row-v1.json`, which the Python suite pins
//!   against the Python module, so the mirrors cannot drift.
//! * The address payload carries `display_country` plus a count-prefixed
//!   `address_levels` list (structure mirroring `address_serving_encode_v1.rs`,
//!   widths in this format's u16 framing); normalized `country` is routing
//!   metadata and stays OUT of the payload.
//! * Record payloads carry the source-locator triple. The duplicate-GERS
//!   contract and within-leaf order are keyed on those 16 bytes, and the
//!   design's measured size evidence includes them.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io::{BufReader, BufWriter, Seek, SeekFrom, Write};
use std::path::PathBuf;

use anyhow::{bail, Context, Result};
use arrow_array::{
    Array, BinaryArray, FixedSizeBinaryArray, Float64Array, Int32Array, LargeBinaryArray,
    ListArray, RecordBatch, StringArray, UInt32Array, UInt64Array, UInt8Array,
};
use arrow_ipc::reader::StreamReader;
use sha2::{Digest, Sha256};

const MAGIC: &[u8; 8] = b"PLRX0001";
const HEADER_BYTES: u64 = 32;
const CELL_LEVEL: u8 = 8;
const INDEX_DOMAIN: &[u8] = b"overture-reverse-index-v1\0";
// Dual-lane additive record digest domains, summed mod 2^256 exactly like the
// forward head digest so the sum is partition-independent.
const DIGEST_DOMAIN_A: &[u8] = b"overture-reverse-shard-v1\0";
const DIGEST_DOMAIN_B: &[u8] = b"overture-reverse-shard-v1\x01";
const ADDRESS_DICTIONARY_MAGIC: &[u8; 8] = b"ARDX0001";
const ADDRESS_DICTIONARY_FLAG: u8 = 1;
const ADDRESS_DICTIONARY_FIELDS: usize = 7;
const MAX_ADDRESS_DICTIONARY_BYTES: usize = 8 * 1024 * 1024;

const LONGITUDE_E7_ORIGIN: i64 = 1_800_000_000;
const LATITUDE_E7_ORIGIN: i64 = 900_000_000;
const LONGITUDE_E7_PER_CELL: i64 = 14_062_500;
const LATITUDE_E7_PER_CELL: i64 = 7_031_250;
const LEAF_TARGET_RECORDS: u64 = 2048;

/// Row-major total order: leaf position first, then the within-leaf record
/// identity `(feature_id, source_object_index, source_row_group,
/// source_row_index)`.
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
    /// Run-length form of the 128-row `L_LON_BY_ROW` table pinned in
    /// `scripts/reverse_shard_v1.py` (places -> "poi", addresses -> "address").
    fn l_lon_runs(self) -> &'static [(i64, usize)] {
        match self {
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
        }
    }
    fn l_lon(self, row: usize) -> i64 {
        let mut remaining = row;
        for &(value, length) in self.l_lon_runs() {
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

/// Symmetric row index, 0 (equator-adjacent) to 127 (polar), from cell y.
fn cell_row_index(y: u32) -> usize {
    if y >= 128 {
        (y - 128) as usize
    } else {
        (127 - y) as usize
    }
}

/// Shard depth from the three ceilings (design section 2), mirroring
/// `reverse_shard_v1.sub_cell_level`. The density loop is capped at the family
/// ceiling because the final `min` erases any deeper value.
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

/// Leaf position of an E7 point clamped into the authoritative cell, mirroring
/// `reverse_shard_v1.leaf_digits_e7` exactly (integer arithmetic only).
fn leaf_sub(longitude_e7: i64, latitude_e7: i64, x: u32, y: u32, level: u8) -> (u32, u32) {
    let dx = (longitude_e7 + LONGITUDE_E7_ORIGIN - i64::from(x) * LONGITUDE_E7_PER_CELL)
        .clamp(0, LONGITUDE_E7_PER_CELL - 1);
    let dy = (latitude_e7 + LATITUDE_E7_ORIGIN - i64::from(y) * LATITUDE_E7_PER_CELL)
        .clamp(0, LATITUDE_E7_PER_CELL - 1);
    let sub_x = ((dx << level) / LONGITUDE_E7_PER_CELL) as u32;
    let sub_y = ((dy << level) / LATITUDE_E7_PER_CELL) as u32;
    (sub_y, sub_x)
}

/// Base-4 digits from the leaf position, point_quadkey convention
/// `(y_bit << 1) | x_bit`, most significant first.
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

fn hex(value: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(value.len() * 2);
    for byte in value {
        output.push(DIGITS[(byte >> 4) as usize] as char);
        output.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    output
}

fn required<'a, T: Array + 'static>(batch: &'a RecordBatch, name: &str) -> Result<&'a T> {
    batch
        .column_by_name(name)
        .with_context(|| format!("missing {name}"))?
        .as_any()
        .downcast_ref::<T>()
        .with_context(|| format!("wrong type for {name}"))
}

fn text(array: &StringArray, row: usize) -> Result<&str> {
    if array.is_null(row) {
        bail!("null reverse text")
    } else {
        Ok(array.value(row))
    }
}

fn put_text(output: &mut Vec<u8>, value: &str) -> Result<()> {
    let length: u16 = value.len().try_into().context("reverse text exceeds u16")?;
    output.extend_from_slice(&length.to_le_bytes());
    output.extend_from_slice(value.as_bytes());
    Ok(())
}

fn id(batch: &RecordBatch, row: usize) -> Result<[u8; 16]> {
    let value = batch
        .column_by_name("feature_id")
        .context("missing feature_id")?;
    if value.is_null(row) {
        bail!("null feature ID")
    }
    let bytes = if let Some(array) = value.as_any().downcast_ref::<FixedSizeBinaryArray>() {
        array.value(row)
    } else if let Some(array) = value.as_any().downcast_ref::<BinaryArray>() {
        array.value(row)
    } else if let Some(array) = value.as_any().downcast_ref::<LargeBinaryArray>() {
        array.value(row)
    } else {
        bail!("wrong type for feature_id")
    };
    bytes.try_into().context("feature ID is not 16 bytes")
}

fn address_levels(array: &ListArray, row: usize) -> Result<Vec<String>> {
    if array.is_null(row) {
        bail!("null address-level list")
    }
    let values = array.value(row);
    let strings = values
        .as_any()
        .downcast_ref::<StringArray>()
        .context("address levels are not UTF-8")?;
    (0..strings.len())
        .map(|item| {
            if strings.is_null(item) {
                bail!("null address level")
            }
            Ok(strings.value(item).to_owned())
        })
        .collect()
}

struct AddressDictionary {
    codes: Vec<BTreeMap<String, u16>>,
    bytes: Vec<u8>,
}

impl AddressDictionary {
    fn build(input: &PathBuf, cell: &str) -> Result<Self> {
        let mut values = (0..ADDRESS_DICTIONARY_FIELDS)
            .map(|_| BTreeSet::<String>::new())
            .collect::<Vec<_>>();
        let reader = StreamReader::try_new(BufReader::new(File::open(input)?), None)?;
        for batch in reader {
            let batch = batch?;
            let cells = required::<StringArray>(&batch, "partition_cell")?;
            let display = [
                required::<StringArray>(&batch, "display_country")?,
                required::<StringArray>(&batch, "postal_city")?,
                required::<StringArray>(&batch, "postcode")?,
                required::<StringArray>(&batch, "street")?,
                required::<StringArray>(&batch, "number")?,
                required::<StringArray>(&batch, "unit")?,
            ];
            let levels = required::<ListArray>(&batch, "address_levels")?;
            for row in 0..batch.num_rows() {
                if text(cells, row)? != cell {
                    bail!("reverse row cell differs from --cell")
                }
                for (field, array) in display.iter().enumerate() {
                    values[field].insert(text(array, row)?.to_owned());
                }
                for level in address_levels(levels, row)? {
                    values[6].insert(level);
                }
            }
        }

        let mut bytes = Vec::new();
        bytes.extend_from_slice(ADDRESS_DICTIONARY_MAGIC);
        bytes.push(ADDRESS_DICTIONARY_FIELDS as u8);
        bytes.push(0);
        bytes.extend_from_slice(&0_u16.to_le_bytes());
        let mut codes = Vec::with_capacity(ADDRESS_DICTIONARY_FIELDS);
        for field in values {
            if field.len() > usize::from(u16::MAX) + 1 {
                bail!("reverse address dictionary field exceeds u16 codes")
            }
            let count: u32 = field
                .len()
                .try_into()
                .context("reverse address dictionary count exceeds u32")?;
            bytes.extend_from_slice(&count.to_le_bytes());
            let mut mapping = BTreeMap::new();
            for (index, value) in field.into_iter().enumerate() {
                let code: u16 = index
                    .try_into()
                    .context("reverse address dictionary code exceeds u16")?;
                put_text(&mut bytes, &value)?;
                mapping.insert(value, code);
            }
            codes.push(mapping);
        }
        if bytes.len() > MAX_ADDRESS_DICTIONARY_BYTES {
            bail!("reverse address dictionary exceeds serving read cap")
        }
        Ok(Self { codes, bytes })
    }

    fn code(&self, field: usize, value: &str) -> Result<u16> {
        self.codes
            .get(field)
            .and_then(|codes| codes.get(value))
            .copied()
            .context("reverse address value is absent from its dictionary")
    }
}

struct IndexEntry {
    hash: u64,
    key: Vec<u8>,
    payload_offset: u64,
    payload_bytes: u64,
    records: u32,
}

struct Args {
    input: PathBuf,
    output: PathBuf,
    family: Family,
    cell: String,
    records: u64,
    digest_out: Option<PathBuf>,
}

fn args() -> Result<Args> {
    let mut input = None;
    let mut output = None;
    let mut family = None;
    let mut cell = None;
    let mut records = None;
    let mut digest_out = None;
    let mut values = std::env::args_os().skip(1);
    while let Some(flag) = values.next() {
        let value = values.next().context("missing command-line value")?;
        match flag.to_str() {
            Some("--input") => input = Some(PathBuf::from(value)),
            Some("--output") => output = Some(PathBuf::from(value)),
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
            // Optional. When set, write the shard's records + dual-lane
            // additive record digest as JSON, for the reverse control plane and
            // the verifier's independent reconciliation.
            Some("--digest-out") => digest_out = Some(PathBuf::from(value)),
            _ => bail!("unknown argument {}", flag.to_string_lossy()),
        }
    }
    Ok(Args {
        input: input.context("--input is required")?,
        output: output.context("--output is required")?,
        family: family.context("--family is required")?,
        cell: cell.context("--cell is required")?,
        records: records.context("--records is required")?,
        digest_out,
    })
}

struct Encoder {
    destination: BufWriter<File>,
    cell: String,
    cell_y: u32,
    cell_x: u32,
    level: u8,
    offset: u64,
    count: u64,
    index: Vec<IndexEntry>,
    active_key: Option<Vec<u8>>,
    previous: Option<OrderKey>,
    digest: bool,
    sum_a: [u8; 32],
    sum_b: [u8; 32],
}

impl Encoder {
    fn push(
        &mut self,
        longitude_e7: i64,
        latitude_e7: i64,
        order_identity: ([u8; 16], u32, u32, u64),
        entry: &[u8],
        framed: bool,
    ) -> Result<()> {
        let (sub_y, sub_x) = leaf_sub(
            longitude_e7,
            latitude_e7,
            self.cell_x,
            self.cell_y,
            self.level,
        );
        let key = (
            sub_y,
            sub_x,
            order_identity.0,
            order_identity.1,
            order_identity.2,
            order_identity.3,
        );
        if self.previous.as_ref().is_some_and(|value| value > &key) {
            bail!("reverse input is not in row-major leaf/record order")
        }
        self.previous = Some(key);
        let mut serving_key = self.cell.clone().into_bytes();
        serving_key.extend_from_slice(leaf_digits(sub_y, sub_x, self.level).as_bytes());
        if self.active_key.as_ref() != Some(&serving_key) {
            self.index.push(IndexEntry {
                hash: index_hash(&serving_key),
                key: serving_key.clone(),
                payload_offset: self.offset,
                payload_bytes: 0,
                records: 0,
            });
            self.active_key = Some(serving_key);
        }
        let length: u32 = entry
            .len()
            .try_into()
            .context("reverse entry exceeds u32")?;
        if self.digest {
            add_256(&mut self.sum_a, &record_digest(DIGEST_DOMAIN_A, entry));
            add_256(&mut self.sum_b, &record_digest(DIGEST_DOMAIN_B, entry));
        }
        if framed {
            self.destination.write_all(&length.to_le_bytes())?;
        }
        self.destination.write_all(entry)?;
        let encoded_bytes = u64::from(length) + if framed { 4 } else { 0 };
        let active = self.index.last_mut().unwrap();
        active.payload_bytes += encoded_bytes;
        active.records = active
            .records
            .checked_add(1)
            .context("reverse index count overflows")?;
        self.offset += encoded_bytes;
        self.count += 1;
        Ok(())
    }
}

fn locator(batch: &RecordBatch, row: usize) -> Result<(u32, u32, u64)> {
    let objects = required::<UInt32Array>(batch, "source_object_index")?;
    let row_groups = required::<UInt32Array>(batch, "source_row_group")?;
    let rows = required::<UInt64Array>(batch, "source_row_index")?;
    if objects.is_null(row) || row_groups.is_null(row) || rows.is_null(row) {
        bail!("null source locator")
    }
    Ok((objects.value(row), row_groups.value(row), rows.value(row)))
}

fn encode_places_batch(encoder: &mut Encoder, batch: &RecordBatch) -> Result<()> {
    let cells = required::<StringArray>(batch, "partition_cell")?;
    let lons = required::<Float64Array>(batch, "longitude")?;
    let lats = required::<Float64Array>(batch, "latitude")?;
    let ranks = required::<UInt8Array>(batch, "confidence_rank")?;
    let names = required::<StringArray>(batch, "primary_name")?;
    let brands = required::<StringArray>(batch, "brand_name")?;
    let categories = required::<StringArray>(batch, "category")?;
    let localities = required::<StringArray>(batch, "locality")?;
    let regions = required::<StringArray>(batch, "region")?;
    let countries = required::<StringArray>(batch, "country")?;
    for row in 0..batch.num_rows() {
        if text(cells, row)? != encoder.cell {
            bail!("reverse row cell differs from --cell")
        }
        if lons.is_null(row) || lats.is_null(row) || ranks.is_null(row) {
            bail!("null reverse scalar")
        }
        let (longitude, latitude) = (lons.value(row), lats.value(row));
        if !longitude.is_finite()
            || !latitude.is_finite()
            || !(-180.0..=180.0).contains(&longitude)
            || !(-90.0..=90.0).contains(&latitude)
        {
            bail!("reverse coordinate is not a finite world position")
        }
        // E7 via round_ties_even, matching the address transform. NOT floor.
        let longitude_e7 = (longitude * 1e7).round_ties_even() as i64;
        let latitude_e7 = (latitude * 1e7).round_ties_even() as i64;
        let id = id(batch, row)?;
        let locator = locator(batch, row)?;
        let mut entry = Vec::new();
        entry.extend_from_slice(&id);
        entry.extend_from_slice(&(longitude_e7 as i32).to_le_bytes());
        entry.extend_from_slice(&(latitude_e7 as i32).to_le_bytes());
        entry.push(ranks.value(row));
        entry.extend_from_slice(&locator.0.to_le_bytes());
        entry.extend_from_slice(&locator.1.to_le_bytes());
        entry.extend_from_slice(&locator.2.to_le_bytes());
        for value in [
            text(names, row)?,
            text(brands, row)?,
            text(categories, row)?,
            text(localities, row)?,
            text(regions, row)?,
            text(countries, row)?,
        ] {
            put_text(&mut entry, value)?;
        }
        encoder.push(
            longitude_e7,
            latitude_e7,
            (id, locator.0, locator.1, locator.2),
            &entry,
            true,
        )?;
    }
    Ok(())
}

fn encode_addresses_batch(
    encoder: &mut Encoder,
    batch: &RecordBatch,
    dictionary: &AddressDictionary,
) -> Result<()> {
    let cells = required::<StringArray>(batch, "partition_cell")?;
    let lons = required::<Int32Array>(batch, "longitude_e7")?;
    let lats = required::<Int32Array>(batch, "latitude_e7")?;
    let display = [
        required::<StringArray>(batch, "display_country")?,
        required::<StringArray>(batch, "postal_city")?,
        required::<StringArray>(batch, "postcode")?,
        required::<StringArray>(batch, "street")?,
        required::<StringArray>(batch, "number")?,
        required::<StringArray>(batch, "unit")?,
    ];
    let levels = required::<ListArray>(batch, "address_levels")?;
    for row in 0..batch.num_rows() {
        if text(cells, row)? != encoder.cell {
            bail!("reverse row cell differs from --cell")
        }
        if lons.is_null(row) || lats.is_null(row) {
            bail!("null reverse scalar")
        }
        let longitude_e7 = i64::from(lons.value(row));
        let latitude_e7 = i64::from(lats.value(row));
        if longitude_e7.abs() > LONGITUDE_E7_ORIGIN || latitude_e7.abs() > LATITUDE_E7_ORIGIN {
            bail!("reverse coordinate is not a finite world position")
        }
        let id = id(batch, row)?;
        let locator = locator(batch, row)?;
        let mut entry = Vec::new();
        entry.extend_from_slice(&id);
        entry.extend_from_slice(&(longitude_e7 as i32).to_le_bytes());
        entry.extend_from_slice(&(latitude_e7 as i32).to_le_bytes());
        entry.extend_from_slice(&locator.0.to_le_bytes());
        entry.extend_from_slice(&locator.1.to_le_bytes());
        entry.extend_from_slice(&locator.2.to_le_bytes());
        for (field, value) in display.iter().enumerate() {
            entry.extend_from_slice(&dictionary.code(field, text(value, row)?)?.to_le_bytes());
        }
        let levels = address_levels(levels, row)?;
        let count: u16 = levels
            .len()
            .try_into()
            .context("address level count exceeds u16")?;
        entry.extend_from_slice(&count.to_le_bytes());
        for level in &levels {
            entry.extend_from_slice(&dictionary.code(6, level)?.to_le_bytes());
        }
        encoder.push(
            longitude_e7,
            latitude_e7,
            (id, locator.0, locator.1, locator.2),
            &entry,
            false,
        )?;
    }
    Ok(())
}

fn main() -> Result<()> {
    let args = args()?;
    let (cell_y, cell_x) = cell_yx(&args.cell)?;
    let level = sub_cell_level(args.records, &args.cell, args.family)?;
    let address_dictionary = if args.family == Family::Addresses {
        Some(AddressDictionary::build(&args.input, &args.cell)?)
    } else {
        None
    };
    let dictionary_bytes = address_dictionary
        .as_ref()
        .map_or(0_u64, |dictionary| dictionary.bytes.len() as u64);
    let mut destination = BufWriter::new(File::create(&args.output)?);
    destination.write_all(MAGIC)?;
    destination.write_all(&0_u64.to_le_bytes())?;
    destination.write_all(&0_u64.to_le_bytes())?;
    destination.write_all(&0_u32.to_le_bytes())?;
    destination.write_all(&[
        args.family.code(),
        CELL_LEVEL,
        level,
        if address_dictionary.is_some() {
            ADDRESS_DICTIONARY_FLAG
        } else {
            0
        },
    ])?;
    if let Some(dictionary) = &address_dictionary {
        destination.write_all(&dictionary.bytes)?;
    }
    let reader = StreamReader::try_new(BufReader::new(File::open(&args.input)?), None)?;
    let mut encoder = Encoder {
        destination,
        cell: args.cell.clone(),
        cell_y,
        cell_x,
        level,
        offset: HEADER_BYTES + dictionary_bytes,
        count: 0,
        index: Vec::new(),
        active_key: None,
        previous: None,
        digest: args.digest_out.is_some(),
        sum_a: [0_u8; 32],
        sum_b: [0_u8; 32],
    };
    for batch in reader {
        let batch = batch?;
        match args.family {
            Family::Places => encode_places_batch(&mut encoder, &batch)?,
            Family::Addresses => encode_addresses_batch(
                &mut encoder,
                &batch,
                address_dictionary
                    .as_ref()
                    .context("address dictionary was not constructed")?,
            )?,
        }
    }
    // The depth was derived from --records, so a count mismatch would be a
    // shard whose recorded level disagrees with its own contents. Fail closed.
    if encoder.count != args.records {
        bail!(
            "reverse stream carried {} records but --records declared {}",
            encoder.count,
            args.records
        )
    }
    let Encoder {
        mut destination,
        offset,
        mut index,
        sum_a,
        sum_b,
        ..
    } = encoder;
    let index_offset = offset;
    index.sort_by(|left, right| (left.hash, &left.key).cmp(&(right.hash, &right.key)));
    let index_count: u32 = index
        .len()
        .try_into()
        .context("reverse index count overflows")?;
    destination.write_all(&index_count.to_le_bytes())?;
    let mut key_offset = 0_u64;
    for item in &index {
        let key_length: u32 = item
            .key
            .len()
            .try_into()
            .context("reverse index key too long")?;
        destination.write_all(&item.hash.to_le_bytes())?;
        destination.write_all(&key_offset.to_le_bytes())?;
        destination.write_all(&key_length.to_le_bytes())?;
        destination.write_all(&item.records.to_le_bytes())?;
        destination.write_all(&item.payload_offset.to_le_bytes())?;
        destination.write_all(&item.payload_bytes.to_le_bytes())?;
        key_offset += u64::from(key_length);
    }
    for item in &index {
        destination.write_all(&item.key)?;
    }
    destination.flush()?;
    destination.seek(SeekFrom::Start(8))?;
    destination.write_all(&args.records.to_le_bytes())?;
    destination.write_all(&index_offset.to_le_bytes())?;
    destination.write_all(&index_count.to_le_bytes())?;
    destination.flush()?;
    if let Some(path) = args.digest_out {
        let sidecar = format!(
            "{{\"records\":{},\"index_entries\":{index_count},\"dictionary_bytes\":{dictionary_bytes},\"sum_a\":\"{}\",\"sum_b\":\"{}\"}}",
            args.records,
            hex(&sum_a),
            hex(&sum_b)
        );
        std::fs::write(path, sidecar).context("write reverse digest sidecar")?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{cell_yx, leaf_digits, leaf_sub, sub_cell_level, Family};

    fn fixture(name: &str) -> serde_json::Value {
        let payload: serde_json::Value = serde_json::from_str(match name {
            "l-lon" => include_str!("../../../../tests/fixtures/reverse/l-lon-by-row-v1.json"),
            "vectors" => {
                include_str!("../../../../tests/fixtures/reverse/cell-identifier-vectors-v1.json")
            }
            _ => unreachable!(),
        })
        .unwrap();
        payload
    }

    /// Every one of the 128 per-row values, both families, against the shared
    /// committed fixture the Python suite pins to `reverse_shard_v1.py`.
    #[test]
    fn l_lon_table_matches_the_shared_fixture() {
        let payload = fixture("l-lon");
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

    /// The committed cross-implementation vector file: every vector's leaf key
    /// at every level, re-derived from E7 through this encoder's mirror.
    #[test]
    fn leaf_keys_match_the_committed_cell_identifier_vectors() {
        let payload = fixture("vectors");
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

    /// The three-ceilings depth rule, mirroring the pinned Python contract
    /// tests in `tests/test_reverse_cell_identifier_vectors.py`.
    #[test]
    fn sub_cell_level_takes_the_minimum_of_the_three_ceilings() {
        assert_eq!(sub_cell_level(0, "8080", Family::Places).unwrap(), 0);
        assert_eq!(sub_cell_level(2048, "8080", Family::Places).unwrap(), 0);
        assert_eq!(sub_cell_level(2049, "8080", Family::Places).unwrap(), 1);
        assert_eq!(
            sub_cell_level(2048 * 4_u64.pow(4), "8080", Family::Places).unwrap(),
            4
        );
        assert_eq!(
            sub_cell_level(1_384_000, "b2e3", Family::Places).unwrap(),
            5
        );
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
            sub_cell_level(10_u64.pow(9), "f700", Family::Addresses).unwrap(),
            4
        );
        assert_eq!(
            sub_cell_level(10_u64.pow(9), "ff00", Family::Places).unwrap(),
            0
        );
        assert_eq!(
            sub_cell_level(10_u64.pow(9), "0000", Family::Places).unwrap(),
            0
        );
        assert_eq!(
            sub_cell_level(10_u64.pow(9), "7f00", Family::Places).unwrap(),
            sub_cell_level(10_u64.pow(9), "8000", Family::Places).unwrap()
        );
        assert!(sub_cell_level(1, "80800", Family::Places).is_err());
        assert!(sub_cell_level(1, "80G0", Family::Places).is_err());
    }

    /// Places E7 conversion is ties-even rounding, not truncation.
    #[test]
    fn places_e7_conversion_rounds_ties_to_even() {
        assert_eq!((0.000_000_15_f64 * 1e7).round_ties_even() as i64, 2);
        assert_eq!((0.000_000_25_f64 * 1e7).round_ties_even() as i64, 2);
        assert_eq!((-0.000_000_15_f64 * 1e7).round_ties_even() as i64, -2);
    }
}
