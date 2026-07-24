//! Deterministic routed/global-head Places construction-v1 serving encoder.

use std::fs::File;
use std::io::{BufReader, BufWriter, Seek, SeekFrom, Write};
use std::path::PathBuf;

use anyhow::{bail, Context, Result};
use arrow_array::{
    Array, BinaryArray, FixedSizeBinaryArray, Float64Array, LargeBinaryArray, RecordBatch,
    StringArray, UInt32Array, UInt64Array, UInt8Array,
};
use arrow_ipc::reader::StreamReader;
use sha2::{Digest, Sha256};

const ROUTED_MAGIC: &[u8; 8] = b"PLRV0002";
const HEAD_MAGIC: &[u8; 8] = b"PLHD0002";
const HEADER_BYTES: u64 = 32;
const MAX_INDEX_ENTRIES: usize = 250_000;
const MAX_INDEX_KEY_BYTES: usize = 268_435_456;
type OrderKey = (String, String, String, u8, [u8; 16], u32, u32, u64);

struct IndexEntry {
    hash: u64,
    key: Vec<u8>,
    payload_offset: u64,
    payload_bytes: u64,
    records: u32,
}

fn index_key(mode: &str, cell: &str, token: &str) -> Vec<u8> {
    if mode == "routed" {
        let mut output = cell.as_bytes().to_vec();
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
        bail!("null serving text")
    } else {
        Ok(array.value(row))
    }
}
fn put_text(output: &mut Vec<u8>, value: &str) -> Result<()> {
    let length: u16 = value.len().try_into().context("serving text exceeds u16")?;
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

fn main() -> Result<()> {
    let mut values = std::env::args_os().skip(1);
    let mut input = None;
    let mut output = None;
    let mut mode = None;
    while let Some(flag) = values.next() {
        let value = values.next().context("missing command-line value")?;
        match flag.to_str() {
            Some("--input") => input = Some(PathBuf::from(value)),
            Some("--output") => output = Some(PathBuf::from(value)),
            Some("--mode") => mode = value.to_str().map(str::to_owned),
            _ => bail!("unknown argument {}", flag.to_string_lossy()),
        }
    }
    let mode = mode.context("--mode is required")?;
    let magic = match mode.as_str() {
        "routed" => ROUTED_MAGIC,
        "head" => HEAD_MAGIC,
        _ => bail!("mode must be routed or head"),
    };
    let mut destination = BufWriter::new(File::create(output.context("--output is required")?)?);
    destination.write_all(magic)?;
    destination.write_all(&0_u64.to_le_bytes())?;
    destination.write_all(&0_u64.to_le_bytes())?;
    destination.write_all(&0_u32.to_le_bytes())?;
    destination.write_all(&0_u32.to_le_bytes())?;
    let reader = StreamReader::try_new(
        BufReader::new(File::open(input.context("--input is required")?)?),
        None,
    )?;
    let mut count = 0_u64;
    let mut offset = HEADER_BYTES;
    let mut index = Vec::<IndexEntry>::new();
    let mut active_key: Option<Vec<u8>> = None;
    let mut previous: Option<OrderKey> = None;
    for batch in reader {
        let batch = batch?;
        let groups = required::<StringArray>(&batch, "execution_group")?;
        let cells = required::<StringArray>(&batch, "partition_cell")?;
        let tokens = required::<StringArray>(&batch, "token")?;
        let masks = required::<UInt8Array>(&batch, "field_mask")?;
        let ranks = required::<UInt8Array>(&batch, "confidence_rank")?;
        let lons = required::<Float64Array>(&batch, "longitude")?;
        let lats = required::<Float64Array>(&batch, "latitude")?;
        let objects = required::<UInt32Array>(&batch, "source_object_index")?;
        let row_groups = required::<UInt32Array>(&batch, "source_row_group")?;
        let rows = required::<UInt64Array>(&batch, "source_row_index")?;
        let names = required::<StringArray>(&batch, "primary_name")?;
        let brands = required::<StringArray>(&batch, "brand_name")?;
        let categories = required::<StringArray>(&batch, "category")?;
        let localities = required::<StringArray>(&batch, "locality")?;
        let regions = required::<StringArray>(&batch, "region")?;
        let countries = required::<StringArray>(&batch, "country")?;
        for row in 0..batch.num_rows() {
            if [
                masks.is_null(row),
                ranks.is_null(row),
                lons.is_null(row),
                lats.is_null(row),
                objects.is_null(row),
                row_groups.is_null(row),
                rows.is_null(row),
            ]
            .into_iter()
            .any(|v| v)
            {
                bail!("null serving scalar")
            }
            let id = id(&batch, row)?;
            let group = text(groups, row)?;
            let cell = text(cells, row)?;
            let token = text(tokens, row)?;
            if cell.len() != 4
                || group.len() != 2
                || !cell.starts_with(group)
                || token.is_empty()
                || masks.value(row) == 0
                || !lons.value(row).is_finite()
                || !lats.value(row).is_finite()
            {
                bail!("invalid serving row")
            }
            let key = if mode == "routed" {
                (
                    group.to_owned(),
                    cell.to_owned(),
                    token.to_owned(),
                    255 - ranks.value(row),
                    id,
                    objects.value(row),
                    row_groups.value(row),
                    rows.value(row),
                )
            } else {
                (
                    String::new(),
                    String::new(),
                    token.to_owned(),
                    255 - ranks.value(row),
                    id,
                    objects.value(row),
                    row_groups.value(row),
                    rows.value(row),
                )
            };
            if previous.as_ref().is_some_and(|value| value > &key) {
                bail!("serving input is not in unique total order")
            }
            previous = Some(key);
            let mut entry = Vec::new();
            put_text(&mut entry, token)?;
            if mode == "routed" {
                put_text(&mut entry, cell)?;
            }
            entry.push(masks.value(row));
            entry.push(ranks.value(row));
            entry.extend_from_slice(&id);
            entry.extend_from_slice(&lons.value(row).to_le_bytes());
            entry.extend_from_slice(&lats.value(row).to_le_bytes());
            entry.extend_from_slice(&objects.value(row).to_le_bytes());
            entry.extend_from_slice(&row_groups.value(row).to_le_bytes());
            entry.extend_from_slice(&rows.value(row).to_le_bytes());
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
            let length: u32 = entry
                .len()
                .try_into()
                .context("serving entry exceeds u32")?;
            let serving_key = index_key(&mode, cell, token);
            if active_key.as_ref() != Some(&serving_key) {
                if index.len() >= MAX_INDEX_ENTRIES {
                    bail!("serving index entry cap exceeded")
                }
                index.push(IndexEntry {
                    hash: index_hash(&serving_key),
                    key: serving_key.clone(),
                    payload_offset: offset,
                    payload_bytes: 0,
                    records: 0,
                });
                active_key = Some(serving_key);
            }
            destination.write_all(&length.to_le_bytes())?;
            destination.write_all(&entry)?;
            let encoded_bytes = 4_u64 + u64::from(length);
            let active = index.last_mut().unwrap();
            active.payload_bytes += encoded_bytes;
            active.records = active
                .records
                .checked_add(1)
                .context("serving index count overflows")?;
            offset += encoded_bytes;
            count += 1;
        }
    }
    let key_bytes: usize = index.iter().map(|item| item.key.len()).sum();
    if key_bytes > MAX_INDEX_KEY_BYTES {
        bail!("serving index key byte cap exceeded")
    }
    let index_offset = offset;
    index.sort_by(|left, right| (left.hash, &left.key).cmp(&(right.hash, &right.key)));
    let index_count: u32 = index
        .len()
        .try_into()
        .context("serving index count overflows")?;
    destination.write_all(&index_count.to_le_bytes())?;
    let mut key_offset = 0_u64;
    for item in &index {
        let key_length: u32 = item
            .key
            .len()
            .try_into()
            .context("serving index key too long")?;
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
    destination.write_all(&count.to_le_bytes())?;
    destination.write_all(&index_offset.to_le_bytes())?;
    destination.write_all(&index_count.to_le_bytes())?;
    destination.write_all(&0_u32.to_le_bytes())?;
    destination.flush()?;
    Ok(())
}
