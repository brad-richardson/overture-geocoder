//! Encoder for the construction-v1 Address serving artifact.

use std::fs::{File, OpenOptions};
use std::io::{BufReader, BufWriter, Write};
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use arrow_array::{
    Array, BinaryArray, FixedSizeBinaryArray, Int32Array, ListArray, RecordBatch, StringArray,
    UInt32Array, UInt64Array,
};
use arrow_ipc::reader::StreamReader;
use sha2::{Digest, Sha256};

const MAGIC: &[u8; 8] = b"OAV1ART\0";
const HEADER_BYTES: u64 = 44;
const INDEX_BYTES: u64 = 24;
const DOMAIN_A: &[u8] = b"overture-address-construction-v1\0";
const DOMAIN_B: &[u8] = b"overture-address-construction-v1\x01";

type TotalKey = (u64, Vec<String>, [u8; 16], u32, u32, u64);

struct Args {
    input: PathBuf,
    output: PathBuf,
    max_output_bytes: u64,
}

fn args() -> Result<Args> {
    let mut input = None;
    let mut output = None;
    let mut maximum = None;
    let mut values = std::env::args_os().skip(1);
    while let Some(flag) = values.next() {
        let value = values.next().context("missing command-line value")?;
        match flag.to_str() {
            Some("--input") => input = Some(value.into()),
            Some("--output") => output = Some(value.into()),
            Some("--max-output-bytes") => {
                maximum = Some(
                    value
                        .to_str()
                        .context("max output bytes is not UTF-8")?
                        .parse::<u64>()?,
                )
            }
            _ => bail!("unknown command-line argument {}", flag.to_string_lossy()),
        }
    }
    let max_output_bytes = maximum.context("--max-output-bytes is required")?;
    if max_output_bytes < HEADER_BYTES {
        bail!("max output bytes is too small");
    }
    Ok(Args {
        input: input.context("--input is required")?,
        output: output.context("--output is required")?,
        max_output_bytes,
    })
}

fn required<'a, T: Array + 'static>(batch: &'a RecordBatch, name: &str) -> Result<&'a T> {
    batch
        .column_by_name(name)
        .with_context(|| format!("input is missing {name}"))?
        .as_any()
        .downcast_ref::<T>()
        .with_context(|| format!("input {name} has the wrong Arrow type"))
}

fn text(array: &StringArray, index: usize) -> Result<&str> {
    if array.is_null(index) {
        bail!("serving input contains a null string")
    }
    Ok(array.value(index))
}

fn binary_value<'a>(batch: &'a RecordBatch, name: &str, row: usize) -> Result<&'a [u8]> {
    let array = batch
        .column_by_name(name)
        .with_context(|| format!("input is missing {name}"))?;
    if array.is_null(row) {
        bail!("serving input {name} is null");
    }
    if let Some(value) = array.as_any().downcast_ref::<FixedSizeBinaryArray>() {
        return Ok(value.value(row));
    }
    if let Some(value) = array.as_any().downcast_ref::<BinaryArray>() {
        return Ok(value.value(row));
    }
    bail!("serving input {name} has the wrong Arrow type")
}

fn list(array: &ListArray, index: usize) -> Result<Vec<String>> {
    if array.is_null(index) {
        bail!("serving input contains a null address-level list");
    }
    let values = array.value(index);
    let strings = values
        .as_any()
        .downcast_ref::<StringArray>()
        .context("serving address levels are not UTF-8")?;
    (0..strings.len())
        .map(|item| {
            if strings.is_null(item) {
                bail!("serving address level is null")
            }
            Ok(strings.value(item).to_owned())
        })
        .collect()
}

fn push_text(output: &mut Vec<u8>, value: &str) {
    output.extend_from_slice(&(value.len() as u64).to_be_bytes());
    output.extend_from_slice(value.as_bytes());
}

fn digest(domain: &[u8], payload: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(domain);
    hasher.update((payload.len() as u64).to_be_bytes());
    hasher.update(payload);
    hasher.finalize().into()
}

fn main() -> Result<()> {
    let args = args()?;
    let payload_path = temporary_payload_path(&args.output);
    let payload_file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&payload_path)
        .context("create serving payload temporary")?;
    let result = encode(&args, &payload_path, BufWriter::new(payload_file));
    if result.is_err() {
        let _ = std::fs::remove_file(&payload_path);
    }
    result
}

fn encode(args: &Args, payload_path: &Path, mut payload_output: BufWriter<File>) -> Result<()> {
    let mut reader = StreamReader::try_new(BufReader::new(File::open(&args.input)?), None)?;
    let mut index = Vec::<(u64, u64, u32)>::new();
    let mut payload_bytes = 0_u64;
    let mut previous: Option<TotalKey> = None;
    for batch in &mut reader {
        let batch = batch?;
        let route_hashes = required::<UInt64Array>(&batch, "route_hash")?;
        let normalized = (0..8)
            .map(|field| required::<StringArray>(&batch, &format!("normalized_key_{field}")))
            .collect::<Result<Vec<_>>>()?;
        let longitude = required::<Int32Array>(&batch, "longitude_e7")?;
        let latitude = required::<Int32Array>(&batch, "latitude_e7")?;
        let objects = required::<UInt32Array>(&batch, "source_object_index")?;
        let groups = required::<UInt32Array>(&batch, "source_row_group")?;
        let rows = required::<UInt64Array>(&batch, "source_row_index")?;
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
            let normalized = normalized
                .iter()
                .map(|array| text(array, row).map(str::to_owned))
                .collect::<Result<Vec<_>>>()?;
            let id: [u8; 16] = binary_value(&batch, "feature_id", row)?
                .try_into()
                .context("feature ID is not 16 bytes")?;
            let key = (
                route_hashes.value(row),
                normalized.clone(),
                id,
                objects.value(row),
                groups.value(row),
                rows.value(row),
            );
            if previous.as_ref().is_some_and(|value| value >= &key) {
                bail!("serving input is not in unique route/key/source order");
            }
            previous = Some(key);
            let mut payload = Vec::new();
            for value in &normalized {
                push_text(&mut payload, value);
            }
            payload.extend_from_slice(&id);
            payload.extend_from_slice(&longitude.value(row).to_be_bytes());
            payload.extend_from_slice(&latitude.value(row).to_be_bytes());
            payload.extend_from_slice(&objects.value(row).to_be_bytes());
            payload.extend_from_slice(&groups.value(row).to_be_bytes());
            payload.extend_from_slice(&rows.value(row).to_be_bytes());
            for value in &display {
                push_text(&mut payload, text(value, row)?);
            }
            let levels = list(levels, row)?;
            payload.extend_from_slice(&(levels.len() as u64).to_be_bytes());
            for level in levels {
                push_text(&mut payload, &level);
            }
            if digest(DOMAIN_A, &payload) != binary_value(&batch, "semantic_digest_a", row)?
                || digest(DOMAIN_B, &payload) != binary_value(&batch, "semantic_digest_b", row)?
            {
                bail!("serving row differs from its semantic digest columns");
            }
            let payload_length =
                u32::try_from(payload.len()).context("serving row is too large")?;
            index.push((route_hashes.value(row), payload_bytes, payload_length));
            payload_output.write_all(&payload)?;
            payload_bytes += u64::from(payload_length);
            let projected = HEADER_BYTES + INDEX_BYTES * index.len() as u64 + payload_bytes;
            if projected > args.max_output_bytes {
                bail!("serving artifact exceeded its hard output cap");
            }
        }
    }
    payload_output.flush()?;
    drop(payload_output);
    let payload_offset = HEADER_BYTES + INDEX_BYTES * index.len() as u64;
    let mut output = BufWriter::new(File::create(&args.output)?);
    output.write_all(MAGIC)?;
    output.write_all(&1_u32.to_be_bytes())?;
    output.write_all(&(index.len() as u64).to_be_bytes())?;
    output.write_all(&HEADER_BYTES.to_be_bytes())?;
    output.write_all(&payload_offset.to_be_bytes())?;
    output.write_all(&payload_bytes.to_be_bytes())?;
    for (route_hash, offset, length) in index {
        output.write_all(&route_hash.to_be_bytes())?;
        output.write_all(&(payload_offset + offset).to_be_bytes())?;
        output.write_all(&length.to_be_bytes())?;
        output.write_all(&0_u32.to_be_bytes())?;
    }
    let mut payload_input = BufReader::new(File::open(payload_path)?);
    std::io::copy(&mut payload_input, &mut output)?;
    output.flush()?;
    drop(output);
    std::fs::remove_file(payload_path)?;
    if args.output.metadata()?.len() > args.max_output_bytes {
        bail!("closed serving artifact exceeded its hard output cap");
    }
    Ok(())
}

fn temporary_payload_path(output: &Path) -> PathBuf {
    let mut name = output.as_os_str().to_owned();
    name.push(".payload.tmp");
    PathBuf::from(name)
}
