//! Construction-v1 Address Arrow semantic transform.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufReader, BufWriter};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

use anyhow::{bail, Context, Result};
use arrow_array::builder::{
    FixedSizeBinaryBuilder, Int32Builder, ListBuilder, StringBuilder, UInt32Builder, UInt64Builder,
};
use arrow_array::{
    Array, ArrayRef, BinaryArray, Int32Array, ListArray, RecordBatch, StringArray, StructArray,
};
use arrow_ipc::reader::StreamReader;
use arrow_ipc::writer::StreamWriter;
use arrow_schema::{DataType, Field, Schema};
use serde::Serialize;
use sha2::{Digest, Sha256};
use unicode_normalization::UnicodeNormalization;
use uuid::Uuid;

const MAX_RECORD_BYTES: usize = 1_048_576;
const MAXIMUM_HASH_BITS: u32 = 16;
const NORMALIZATION_VERSION: &str = "nfc-uniws-collapse-ascii-lower-v1";
const TRANSFORM_VERSION: &str = "address-rust-arrow-transform-v1";
const DIGEST_VERSION: &str = "sha256-add-mod-2^256-two-domain-v1";
const DOMAIN_A: &[u8] = b"overture-address-construction-v1\0";
const DOMAIN_B: &[u8] = b"overture-address-construction-v1\x01";
const REJECTION_PRECEDENCE: [&str; 9] = [
    "missing_street_or_number",
    "invalid_geometry",
    "blank_country",
    "invalid_country",
    "missing_uuid",
    "invalid_uuid",
    "invalid_source_locator",
    "record_too_large",
    "invalid_record",
];

#[derive(Debug)]
struct Args {
    input: PathBuf,
    output: PathBuf,
    report: PathBuf,
    source_limits: Option<PathBuf>,
}

#[derive(Serialize)]
struct Report {
    schema: &'static str,
    transform_version: &'static str,
    normalization_version: &'static str,
    digest_version: &'static str,
    maximum_hash_bits: u32,
    input_rows: u64,
    admitted_rows: u64,
    rejected_rows: u64,
    rejections_by_precedence: BTreeMap<&'static str, u64>,
    semantic_sum_a: String,
    semantic_sum_b: String,
    elapsed_seconds: f64,
}

#[derive(Clone)]
struct LogicalRow {
    country: String,
    maximum_bucket: u32,
    route_hash: u64,
    normalized: [String; 8],
    feature_id: [u8; 16],
    longitude_e7: i32,
    latitude_e7: i32,
    source_object_index: u32,
    source_row_group: u32,
    source_row_index: u64,
    display_country: String,
    postal_city: String,
    postcode: String,
    street: String,
    number: String,
    unit: String,
    address_levels: Vec<String>,
}

fn parse_args() -> Result<Args> {
    let mut values = std::env::args_os().skip(1);
    let mut input = None;
    let mut output = None;
    let mut report = None;
    let mut source_limits = None;
    while let Some(flag) = values.next() {
        let target = values
            .next()
            .context("missing value after command-line flag")?;
        match flag.to_str() {
            Some("--input") => input = Some(target.into()),
            Some("--output") => output = Some(target.into()),
            Some("--report") => report = Some(target.into()),
            Some("--source-limits") => source_limits = Some(target.into()),
            _ => bail!("unknown command-line flag: {}", flag.to_string_lossy()),
        }
    }
    Ok(Args {
        input: input.context("--input is required")?,
        output: output.context("--output is required")?,
        report: report.context("--report is required")?,
        source_limits,
    })
}

fn output_schema() -> Arc<Schema> {
    let mut fields = vec![
        Field::new("country", DataType::Utf8, false),
        Field::new("maximum_bucket", DataType::UInt32, false),
        Field::new("route_hash", DataType::UInt64, false),
    ];
    fields.extend(
        (0..8).map(|index| Field::new(format!("normalized_key_{index}"), DataType::Utf8, false)),
    );
    fields.extend([
        Field::new("feature_id", DataType::FixedSizeBinary(16), false),
        Field::new("longitude_e7", DataType::Int32, false),
        Field::new("latitude_e7", DataType::Int32, false),
        Field::new("source_object_index", DataType::UInt32, false),
        Field::new("source_row_group", DataType::UInt32, false),
        Field::new("source_row_index", DataType::UInt64, false),
        Field::new("display_country", DataType::Utf8, false),
        Field::new("postal_city", DataType::Utf8, false),
        Field::new("postcode", DataType::Utf8, false),
        Field::new("street", DataType::Utf8, false),
        Field::new("number", DataType::Utf8, false),
        Field::new("unit", DataType::Utf8, false),
        Field::new(
            "address_levels",
            DataType::List(Arc::new(Field::new("item", DataType::Utf8, true))),
            false,
        ),
        Field::new("semantic_digest_a", DataType::FixedSizeBinary(32), false),
        Field::new("semantic_digest_b", DataType::FixedSizeBinary(32), false),
    ]);
    Arc::new(Schema::new(fields))
}

fn normalize(value: &str) -> String {
    let collapsed = value.nfc().collect::<String>();
    collapsed
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .map(|character| {
            if character.is_ascii_uppercase() {
                character.to_ascii_lowercase()
            } else {
                character
            }
        })
        .collect()
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

fn parse_point(value: &[u8]) -> Option<(f64, f64)> {
    if value.len() != 21 || !matches!(value[0], 0 | 1) {
        return None;
    }
    let little = value[0] == 1;
    let type_bytes: [u8; 4] = value[1..5].try_into().ok()?;
    let geometry_type = if little {
        u32::from_le_bytes(type_bytes)
    } else {
        u32::from_be_bytes(type_bytes)
    };
    if geometry_type != 1 {
        return None;
    }
    let lon_bytes: [u8; 8] = value[5..13].try_into().ok()?;
    let lat_bytes: [u8; 8] = value[13..21].try_into().ok()?;
    let longitude = if little {
        f64::from_le_bytes(lon_bytes)
    } else {
        f64::from_be_bytes(lon_bytes)
    };
    let latitude = if little {
        f64::from_le_bytes(lat_bytes)
    } else {
        f64::from_be_bytes(lat_bytes)
    };
    (longitude.is_finite()
        && latitude.is_finite()
        && (-180.0..=180.0).contains(&longitude)
        && (-90.0..=90.0).contains(&latitude))
    .then_some((longitude, latitude))
}

fn required<'a, T: Array + 'static>(batch: &'a RecordBatch, name: &str) -> Result<&'a T> {
    batch
        .column_by_name(name)
        .with_context(|| format!("input is missing column {name}"))?
        .as_any()
        .downcast_ref::<T>()
        .with_context(|| format!("input column {name} has the wrong Arrow type"))
}

fn text(array: &StringArray, index: usize) -> &str {
    if array.is_null(index) {
        ""
    } else {
        array.value(index)
    }
}

fn levels(array: &ListArray, index: usize) -> Result<Vec<String>> {
    if array.is_null(index) {
        return Ok(Vec::new());
    }
    let values = array.value(index);
    if let Some(strings) = values.as_any().downcast_ref::<StringArray>() {
        return Ok((0..strings.len())
            .filter(|item| !strings.is_null(*item))
            .map(|item| strings.value(item).to_owned())
            .collect());
    }
    if let Some(structs) = values.as_any().downcast_ref::<StructArray>() {
        let strings = structs
            .column_by_name("value")
            .context("address_levels struct has no value field")?
            .as_any()
            .downcast_ref::<StringArray>()
            .context("address_levels.value is not Utf8")?;
        return Ok((0..strings.len())
            .filter(|item| !strings.is_null(*item))
            .map(|item| strings.value(item).to_owned())
            .collect());
    }
    bail!("address_levels must be list<string> or list<struct<value:string>>")
}

fn canonical_payload(row: &LogicalRow) -> Vec<u8> {
    fn push_text(output: &mut Vec<u8>, value: &str) {
        output.extend_from_slice(&(value.len() as u64).to_be_bytes());
        output.extend_from_slice(value.as_bytes());
    }
    let mut output = Vec::new();
    for field in &row.normalized {
        push_text(&mut output, field);
    }
    output.extend_from_slice(&row.feature_id);
    output.extend_from_slice(&row.longitude_e7.to_be_bytes());
    output.extend_from_slice(&row.latitude_e7.to_be_bytes());
    output.extend_from_slice(&row.source_object_index.to_be_bytes());
    output.extend_from_slice(&row.source_row_group.to_be_bytes());
    output.extend_from_slice(&row.source_row_index.to_be_bytes());
    for field in [
        &row.display_country,
        &row.postal_city,
        &row.postcode,
        &row.street,
        &row.number,
        &row.unit,
    ] {
        push_text(&mut output, field);
    }
    output.extend_from_slice(&(row.address_levels.len() as u64).to_be_bytes());
    for level in &row.address_levels {
        push_text(&mut output, level);
    }
    output
}

fn digest(domain: &[u8], payload: &[u8]) -> [u8; 32] {
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

fn transform_batch(
    batch: &RecordBatch,
    rejected: &mut BTreeMap<&'static str, u64>,
    sum_a: &mut [u8; 32],
    sum_b: &mut [u8; 32],
    source_limits: Option<&[(u64, u32)]>,
) -> Result<(RecordBatch, u64)> {
    let ids = required::<StringArray>(batch, "id")?;
    let streets = required::<StringArray>(batch, "street")?;
    let numbers = required::<StringArray>(batch, "number")?;
    let units = required::<StringArray>(batch, "unit")?;
    let postcodes = required::<StringArray>(batch, "postcode")?;
    let cities = required::<StringArray>(batch, "postal_city")?;
    let countries = required::<StringArray>(batch, "country")?;
    let geometries = required::<BinaryArray>(batch, "geometry")?;
    let address_levels = required::<ListArray>(batch, "address_levels")?;
    let object_indexes = required::<Int32Array>(batch, "source_object_index")?;
    let row_groups = required::<Int32Array>(batch, "source_row_group")?;
    let row_indexes = required::<Int32Array>(batch, "source_row_index")?;

    let mut rows = Vec::with_capacity(batch.num_rows());
    for index in 0..batch.num_rows() {
        let street = text(streets, index);
        let number = text(numbers, index);
        let normalized_street = normalize(street);
        let normalized_number = normalize(number);
        if normalized_street.is_empty() || normalized_number.is_empty() {
            *rejected.get_mut("missing_street_or_number").unwrap() += 1;
            continue;
        }
        let Some(point) = (!geometries.is_null(index))
            .then(|| parse_point(geometries.value(index)))
            .flatten()
        else {
            *rejected.get_mut("invalid_geometry").unwrap() += 1;
            continue;
        };
        let display_country = text(countries, index);
        let country = normalize(display_country);
        if country.is_empty() {
            *rejected.get_mut("blank_country").unwrap() += 1;
            continue;
        }
        if !(2..=3).contains(&country.len())
            || !country
                .bytes()
                .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
        {
            *rejected.get_mut("invalid_country").unwrap() += 1;
            continue;
        }
        let raw_id = text(ids, index).trim();
        if raw_id.is_empty() {
            *rejected.get_mut("missing_uuid").unwrap() += 1;
            continue;
        }
        let valid_uuid_shape = (raw_id.len() == 32
            && raw_id.bytes().all(|byte| byte.is_ascii_hexdigit()))
            || (raw_id.len() == 36
                && raw_id.bytes().enumerate().all(|(offset, byte)| {
                    if matches!(offset, 8 | 13 | 18 | 23) {
                        byte == b'-'
                    } else {
                        byte.is_ascii_hexdigit()
                    }
                }));
        let Ok(feature_id) = Uuid::parse_str(raw_id) else {
            *rejected.get_mut("invalid_uuid").unwrap() += 1;
            continue;
        };
        if !valid_uuid_shape {
            *rejected.get_mut("invalid_uuid").unwrap() += 1;
            continue;
        }
        if object_indexes.is_null(index)
            || row_groups.is_null(index)
            || row_indexes.is_null(index)
            || object_indexes.value(index) < 0
            || row_groups.value(index) < 0
            || row_indexes.value(index) < 0
        {
            *rejected.get_mut("invalid_source_locator").unwrap() += 1;
            continue;
        }
        let object_index = object_indexes.value(index) as u32;
        let row_group = row_groups.value(index) as u32;
        let row_index = row_indexes.value(index) as u64;
        if let Some(limits) = source_limits {
            let invalid = match limits.get(object_index as usize) {
                None => true,
                Some((records, groups)) => row_group >= *groups || row_index >= *records,
            };
            if invalid {
                *rejected.get_mut("invalid_source_locator").unwrap() += 1;
                continue;
            }
        }
        let levels = match levels(address_levels, index) {
            Ok(value) => value,
            Err(_) => {
                *rejected.get_mut("invalid_record").unwrap() += 1;
                continue;
            }
        };
        let normalized = [
            country.clone(),
            normalize(levels.first().map(String::as_str).unwrap_or("")),
            normalize(levels.last().map(String::as_str).unwrap_or("")),
            normalize(text(cities, index)),
            normalize(text(postcodes, index)),
            normalized_street,
            normalized_number,
            normalize(text(units, index)),
        ];
        let hash = route_hash(&normalized);
        let row = LogicalRow {
            country,
            maximum_bucket: (hash >> (64 - MAXIMUM_HASH_BITS)) as u32,
            route_hash: hash,
            normalized,
            feature_id: *feature_id.as_bytes(),
            longitude_e7: (point.0 * 10_000_000.0).round_ties_even() as i32,
            latitude_e7: (point.1 * 10_000_000.0).round_ties_even() as i32,
            source_object_index: object_index,
            source_row_group: row_group,
            source_row_index: row_index,
            display_country: display_country.to_owned(),
            postal_city: text(cities, index).to_owned(),
            postcode: text(postcodes, index).to_owned(),
            street: street.to_owned(),
            number: number.to_owned(),
            unit: text(units, index).to_owned(),
            address_levels: levels,
        };
        let payload = canonical_payload(&row);
        if payload.len() > MAX_RECORD_BYTES {
            *rejected.get_mut("record_too_large").unwrap() += 1;
            continue;
        }
        rows.push((row, digest(DOMAIN_A, &payload), digest(DOMAIN_B, &payload)));
    }

    let mut strings: Vec<StringBuilder> = (0..15).map(|_| StringBuilder::new()).collect();
    let mut buckets = UInt32Builder::new();
    let mut hashes = UInt64Builder::new();
    let mut ids = FixedSizeBinaryBuilder::new(16);
    let mut lon = Int32Builder::new();
    let mut lat = Int32Builder::new();
    let mut objects = UInt32Builder::new();
    let mut groups = UInt32Builder::new();
    let mut indexes = UInt64Builder::new();
    let mut level_builder = ListBuilder::new(StringBuilder::new());
    let mut digests_a = FixedSizeBinaryBuilder::new(32);
    let mut digests_b = FixedSizeBinaryBuilder::new(32);
    for (row, digest_a, digest_b) in &rows {
        strings[0].append_value(&row.country);
        buckets.append_value(row.maximum_bucket);
        hashes.append_value(row.route_hash);
        for (offset, value) in row.normalized.iter().enumerate() {
            strings[offset + 1].append_value(value);
        }
        ids.append_value(row.feature_id)?;
        lon.append_value(row.longitude_e7);
        lat.append_value(row.latitude_e7);
        objects.append_value(row.source_object_index);
        groups.append_value(row.source_row_group);
        indexes.append_value(row.source_row_index);
        for (offset, value) in [
            &row.display_country,
            &row.postal_city,
            &row.postcode,
            &row.street,
            &row.number,
            &row.unit,
        ]
        .into_iter()
        .enumerate()
        {
            strings[offset + 9].append_value(value);
        }
        for level in &row.address_levels {
            level_builder.values().append_value(level);
        }
        level_builder.append(true);
        digests_a.append_value(digest_a)?;
        digests_b.append_value(digest_b)?;
        add_256(sum_a, digest_a);
        add_256(sum_b, digest_b);
    }
    let mut columns: Vec<ArrayRef> = vec![
        Arc::new(strings.remove(0).finish()),
        Arc::new(buckets.finish()),
        Arc::new(hashes.finish()),
    ];
    for _ in 0..8 {
        columns.push(Arc::new(strings.remove(0).finish()));
    }
    columns.extend([
        Arc::new(ids.finish()) as ArrayRef,
        Arc::new(lon.finish()),
        Arc::new(lat.finish()),
        Arc::new(objects.finish()),
        Arc::new(groups.finish()),
        Arc::new(indexes.finish()),
    ]);
    for builder in &mut strings {
        columns.push(Arc::new(builder.finish()));
    }
    columns.extend([
        Arc::new(level_builder.finish()) as ArrayRef,
        Arc::new(digests_a.finish()),
        Arc::new(digests_b.finish()),
    ]);
    Ok((
        RecordBatch::try_new(output_schema(), columns)?,
        rows.len() as u64,
    ))
}

fn main() -> Result<()> {
    let args = parse_args()?;
    let started = Instant::now();
    let source = BufReader::new(File::open(&args.input).context("open input Arrow IPC")?);
    let reader = StreamReader::try_new(source, None).context("read input Arrow IPC schema")?;
    let output = BufWriter::new(File::create(&args.output).context("create output Arrow IPC")?);
    let mut writer =
        StreamWriter::try_new(output, &output_schema()).context("write output schema")?;
    let mut rejected = REJECTION_PRECEDENCE
        .into_iter()
        .map(|reason| (reason, 0))
        .collect::<BTreeMap<_, _>>();
    let mut input_rows = 0_u64;
    let mut admitted_rows = 0_u64;
    let mut sum_a = [0_u8; 32];
    let mut sum_b = [0_u8; 32];
    let source_limits = args
        .source_limits
        .as_ref()
        .map(|path| -> Result<Vec<(u64, u32)>> {
            #[derive(serde::Deserialize)]
            struct Limits {
                objects: Vec<Limit>,
            }
            #[derive(serde::Deserialize)]
            struct Limit {
                records: u64,
                row_groups: u32,
            }
            let parsed: Limits = serde_json::from_reader(BufReader::new(File::open(path)?))?;
            if parsed.objects.is_empty()
                || parsed
                    .objects
                    .iter()
                    .any(|item| item.records == 0 || item.row_groups == 0)
            {
                bail!("source limits must contain positive object bounds");
            }
            Ok(parsed
                .objects
                .into_iter()
                .map(|item| (item.records, item.row_groups))
                .collect())
        })
        .transpose()?;
    for batch in reader {
        let batch = batch.context("read input Arrow IPC batch")?;
        input_rows += batch.num_rows() as u64;
        let (output, admitted) = transform_batch(
            &batch,
            &mut rejected,
            &mut sum_a,
            &mut sum_b,
            source_limits.as_deref(),
        )?;
        if output.num_rows() > 0 {
            writer
                .write(&output)
                .context("write transformed Arrow IPC batch")?;
        }
        admitted_rows += admitted;
    }
    writer.finish().context("finish output Arrow IPC")?;
    let rejected_rows = rejected.values().sum();
    if input_rows != admitted_rows + rejected_rows {
        bail!("address accounting does not reconcile");
    }
    let report = Report {
        schema: "overture-address-rust-transform-report-v1",
        transform_version: TRANSFORM_VERSION,
        normalization_version: NORMALIZATION_VERSION,
        digest_version: DIGEST_VERSION,
        maximum_hash_bits: MAXIMUM_HASH_BITS,
        input_rows,
        admitted_rows,
        rejected_rows,
        rejections_by_precedence: rejected,
        semantic_sum_a: hex(&sum_a),
        semantic_sum_b: hex(&sum_b),
        elapsed_seconds: started.elapsed().as_secs_f64(),
    };
    serde_json::to_writer_pretty(BufWriter::new(File::create(&args.report)?), &report)?;
    Ok(())
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalization_is_nfc_unicode_whitespace_and_ascii_lower_only() {
        assert_eq!(normalize("  CAF\u{0045}\u{0301}\tİ  "), "cafÉ İ");
    }

    #[test]
    fn strict_point_rejects_extra_dimensions_and_bounds() {
        let mut point = vec![1];
        point.extend_from_slice(&1_u32.to_le_bytes());
        point.extend_from_slice(&(-71_f64).to_le_bytes());
        point.extend_from_slice(&(42_f64).to_le_bytes());
        assert_eq!(parse_point(&point), Some((-71.0, 42.0)));
        let mut big_endian = vec![0];
        big_endian.extend_from_slice(&1_u32.to_be_bytes());
        big_endian.extend_from_slice(&(-71_f64).to_be_bytes());
        big_endian.extend_from_slice(&(42_f64).to_be_bytes());
        assert_eq!(parse_point(&big_endian), Some((-71.0, 42.0)));
        point.extend_from_slice(&0_f64.to_le_bytes());
        assert_eq!(parse_point(&point), None);
    }

    #[test]
    fn digest_add_preserves_duplicate_multiplicity() {
        let value = [0x80_u8; 32];
        let mut sum = [0_u8; 32];
        add_256(&mut sum, &value);
        assert_eq!(sum, value);
        add_256(&mut sum, &value);
        assert_eq!(
            sum,
            [
                1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
                1, 1, 1, 0
            ]
        );
    }
}
