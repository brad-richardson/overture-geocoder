//! Authoritative Places construction-v1 Arrow semantic transform.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufReader, BufWriter};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

use anyhow::{bail, Context, Result};
use arrow_array::builder::{
    FixedSizeBinaryBuilder, Float64Builder, StringBuilder, UInt32Builder, UInt64Builder,
    UInt8Builder,
};
use arrow_array::{
    Array, ArrayRef, BinaryArray, Float64Array, Int32Array, ListArray, RecordBatch, StringArray,
    UInt8Array,
};
use arrow_ipc::reader::StreamReader;
use arrow_ipc::writer::StreamWriter;
use arrow_schema::{DataType, Field, Schema};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use unicode_normalization::{char::is_combining_mark, UnicodeNormalization};
use uuid::Uuid;

const MAX_RECORD_BYTES: usize = 1_048_576;
const TOKENIZER_VERSION: &str = "nfkd-lower-stripmark-cjk-bigram-v4";
const ENTITY_PHRASE_MIN_WORDS: usize = 2;
const ENTITY_PHRASE_MAX_WORDS: usize = 3;

/// The tokenizer contract pins one Unicode version across both implementations.
/// NFKD and combining-mark classification come from `unicode-normalization`
/// while the word class and lowercase map come from `std`, so a crate bump can
/// move tokenization independently of the toolchain. Fail the build instead.
const _: () = assert!(
    unicode_normalization::UNICODE_VERSION.0 == 17 && unicode_normalization::UNICODE_VERSION.1 == 0
);
const TRANSFORM_VERSION: &str = "places-rust-arrow-transform-v1";
const DIGEST_VERSION: &str = "sha256-add-mod-2^256-two-domain-v1";
const DOMAIN_A: &[u8] = b"overture-places-construction-v1\0";
const DOMAIN_B: &[u8] = b"overture-places-construction-v1\x01";
const REJECTIONS: [&str; 8] = [
    "missing_primary_name",
    "invalid_uuid",
    "permanently_closed",
    "invalid_geometry",
    "invalid_confidence",
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

#[derive(Deserialize)]
struct SourceLimits {
    objects: Vec<SourceLimit>,
}

#[derive(Deserialize)]
struct SourceLimit {
    records: u64,
    row_groups: u32,
}

#[derive(Serialize)]
struct Report {
    schema: &'static str,
    transform_version: &'static str,
    tokenizer_version: &'static str,
    digest_version: &'static str,
    input_features: u64,
    admitted_features: u64,
    multilingual_features: u64,
    cjk_features: u64,
    emitted_term_rows: u64,
    rejected_features: u64,
    rejections_by_precedence: BTreeMap<&'static str, u64>,
    semantic_sum_a: String,
    semantic_sum_b: String,
    elapsed_seconds: f64,
}

#[derive(Clone)]
struct TermRow {
    execution_group: String,
    partition_cell: String,
    partition_key: u32,
    token: String,
    field_mask: u8,
    confidence_rank: u8,
    prominence_rank: u8,
    feature_id: [u8; 16],
    longitude: f64,
    latitude: f64,
    primary_name: String,
    brand_name: String,
    category: String,
    locality: String,
    region: String,
    country: String,
    source_object_index: u32,
    source_row_group: u32,
    source_row_index: u64,
}

#[derive(Default)]
struct BatchMetrics {
    admitted: u64,
    multilingual: u64,
    cjk: u64,
}

fn args() -> Result<Args> {
    let mut values = std::env::args_os().skip(1);
    let mut input = None;
    let mut output = None;
    let mut report = None;
    let mut source_limits = None;
    while let Some(flag) = values.next() {
        let value = values.next().context("missing command-line value")?;
        match flag.to_str() {
            Some("--input") => input = Some(value.into()),
            Some("--output") => output = Some(value.into()),
            Some("--report") => report = Some(value.into()),
            Some("--source-limits") => source_limits = Some(value.into()),
            _ => bail!("unknown argument {}", flag.to_string_lossy()),
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
    Arc::new(Schema::new(vec![
        Field::new("execution_group", DataType::Utf8, false),
        Field::new("partition_cell", DataType::Utf8, false),
        Field::new("partition_key", DataType::UInt32, false),
        Field::new("token", DataType::Utf8, false),
        Field::new("token_hash", DataType::UInt64, false),
        Field::new("field_mask", DataType::UInt8, false),
        Field::new("confidence_rank", DataType::UInt8, false),
        Field::new("prominence_rank", DataType::UInt8, false),
        Field::new("feature_id", DataType::FixedSizeBinary(16), false),
        Field::new("longitude", DataType::Float64, false),
        Field::new("latitude", DataType::Float64, false),
        Field::new("primary_name", DataType::Utf8, false),
        Field::new("brand_name", DataType::Utf8, false),
        Field::new("category", DataType::Utf8, false),
        Field::new("locality", DataType::Utf8, false),
        Field::new("region", DataType::Utf8, false),
        Field::new("country", DataType::Utf8, false),
        Field::new("source_object_index", DataType::UInt32, false),
        Field::new("source_row_group", DataType::UInt32, false),
        Field::new("source_row_index", DataType::UInt64, false),
        Field::new("semantic_digest_a", DataType::FixedSizeBinary(32), false),
        Field::new("semantic_digest_b", DataType::FixedSizeBinary(32), false),
    ]))
}

fn required<'a, T: Array + 'static>(batch: &'a RecordBatch, name: &str) -> Result<&'a T> {
    batch
        .column_by_name(name)
        .with_context(|| format!("input is missing {name}"))?
        .as_any()
        .downcast_ref::<T>()
        .with_context(|| format!("input column {name} has the wrong Arrow type"))
}

/// Like `required`, but returns None when the column is absent.
///
/// Optional columns are additive physical-projection generations. Legacy
/// evidence remains transformable with the exact old schema and semantics.
fn optional<'a, T: Array + 'static>(batch: &'a RecordBatch, name: &str) -> Option<&'a T> {
    batch.column_by_name(name)?.as_any().downcast_ref::<T>()
}

fn text(array: &StringArray, index: usize) -> &str {
    if array.is_null(index) {
        ""
    } else {
        array.value(index)
    }
}

fn is_cjk(character: char) -> bool {
    matches!(
        character as u32,
        0x3400..=0x4DBF
            | 0x4E00..=0x9FFF
            | 0x3040..=0x30FF
            | 0x31F0..=0x31FF
            | 0xAC00..=0xD7AF
    )
}

/// Fold a Greek final sigma (U+03C2) to a plain lowercase sigma (U+03C3).
///
/// Rust `char::to_lowercase` is context-free and always lowers `Σ` to `σ`,
/// but a lowercase Greek query naturally carries the final form `ς`, so the
/// index and the Worker query tokenizer both apply this fold to keep the two
/// forms searchable against one another. This is what Unicode case folding
/// does; see `tokenizer_version` `nfkd-lower-stripmark-cjk-bigram-v4`.
fn fold_final_sigma(character: char) -> char {
    if character == '\u{03c2}' {
        '\u{03c3}'
    } else {
        character
    }
}

fn normalized_words(value: &str) -> Vec<String> {
    // Trim Unicode `White_Space` only (Rust `str::trim`), decompose with NFKD,
    // then lowercase per-char. Lowercasing runs *after* NFKD so that
    // compatibility-decomposed styled capitals (e.g. `𝓓` -> `D`) become
    // lowercase-searchable (`d`) rather than surviving as bare capitals.
    let folded: String = value
        .trim()
        .nfkd()
        .flat_map(char::to_lowercase)
        .map(fold_final_sigma)
        .filter(|character| !is_combining_mark(*character))
        .collect();
    let mut words = Vec::new();
    let mut current = String::new();
    for character in folded.chars() {
        if character.is_alphanumeric() || character == '_' {
            current.push(character);
        } else if !current.is_empty() {
            words.push(std::mem::take(&mut current));
        }
    }
    if !current.is_empty() {
        words.push(current);
    }
    words
}

fn tokens(value: &str) -> Vec<String> {
    let mut result = Vec::new();
    for word in normalized_words(value) {
        if !result.contains(&word) {
            result.push(word.clone());
        }
        let characters: Vec<char> = word.chars().collect();
        let mut start = 0;
        while start < characters.len() {
            if !is_cjk(characters[start]) {
                start += 1;
                continue;
            }
            let mut end = start + 1;
            while end < characters.len() && is_cjk(characters[end]) {
                end += 1;
            }
            if end - start == 1 {
                let token = characters[start].to_string();
                if !result.contains(&token) {
                    result.push(token);
                }
            } else {
                for offset in start..end - 1 {
                    let token: String = characters[offset..=offset + 1].iter().collect();
                    if !result.contains(&token) {
                        result.push(token);
                    }
                }
            }
            start = end;
        }
    }
    result
}

/// One head-only exact-primary-name key for a category-prominent entity.
///
/// Exact phrase equality is the identity evidence; `prominence_rank > 0` is
/// only the bounded admission budget.  The `eN:` namespace cannot collide with
/// ordinary tokenizer output because `:` is a word separator.  CJK runs are a
/// single normalized word here and remain on the existing token/bigram path.
fn entity_phrase_key(primary_name: &str, prominence_rank: u8) -> Option<String> {
    if prominence_rank == 0 {
        return None;
    }
    let words = normalized_words(primary_name);
    if !(ENTITY_PHRASE_MIN_WORDS..=ENTITY_PHRASE_MAX_WORDS).contains(&words.len()) {
        return None;
    }
    Some(format!("e{}:{}", words.len(), words.join(" ")))
}

fn point(value: &[u8]) -> Option<(f64, f64)> {
    if value.len() != 21 || !matches!(value[0], 0 | 1) {
        return None;
    }
    let little = value[0] == 1;
    let u32_at = |offset| {
        let bytes: [u8; 4] = value[offset..offset + 4].try_into().unwrap();
        if little {
            u32::from_le_bytes(bytes)
        } else {
            u32::from_be_bytes(bytes)
        }
    };
    let f64_at = |offset| {
        let bytes: [u8; 8] = value[offset..offset + 8].try_into().unwrap();
        f64::from_bits(if little {
            u64::from_le_bytes(bytes)
        } else {
            u64::from_be_bytes(bytes)
        })
    };
    let longitude = f64_at(5);
    let latitude = f64_at(13);
    (u32_at(1) == 1
        && longitude.is_finite()
        && latitude.is_finite()
        && (-180.0..=180.0).contains(&longitude)
        && (-90.0..=90.0).contains(&latitude))
    .then_some((longitude, latitude))
}

fn route(longitude: f64, latitude: f64) -> (u32, String, String) {
    let x = (((longitude + 180.0) / 360.0 * 256.0).floor() as i64).clamp(0, 255) as u32;
    let y = (((latitude + 90.0) / 180.0 * 256.0).floor() as i64).clamp(0, 255) as u32;
    let key = (y << 8) | x;
    let cell = format!("{y:02x}{x:02x}");
    (key, cell[..2].to_owned(), cell)
}

fn payload(row: &TermRow) -> Vec<u8> {
    let mut output = Vec::new();
    for value in [&row.execution_group, &row.partition_cell, &row.token] {
        output.extend_from_slice(&(value.len() as u32).to_be_bytes());
        output.extend_from_slice(value.as_bytes());
    }
    output.extend_from_slice(&row.partition_key.to_be_bytes());
    output.push(row.field_mask);
    output.push(row.confidence_rank);
    output.push(row.prominence_rank);
    output.extend_from_slice(&row.feature_id);
    output.extend_from_slice(&row.longitude.to_bits().to_be_bytes());
    output.extend_from_slice(&row.latitude.to_bits().to_be_bytes());
    for value in [
        &row.primary_name,
        &row.brand_name,
        &row.category,
        &row.locality,
        &row.region,
        &row.country,
    ] {
        output.extend_from_slice(&(value.len() as u32).to_be_bytes());
        output.extend_from_slice(value.as_bytes());
    }
    output.extend_from_slice(&row.source_object_index.to_be_bytes());
    output.extend_from_slice(&row.source_row_group.to_be_bytes());
    output.extend_from_slice(&row.source_row_index.to_be_bytes());
    output
}

fn digest(domain: &[u8], value: &[u8]) -> [u8; 32] {
    let mut digest = Sha256::new();
    digest.update(domain);
    digest.update(value);
    digest.finalize().into()
}

fn token_hash(value: &str) -> u64 {
    let digest = digest(b"overture-places-token-partition-v1\0", value.as_bytes());
    u64::from_be_bytes(digest[..8].try_into().unwrap())
}

fn add_256(target: &mut [u8; 32], value: &[u8; 32]) {
    let mut carry = 0_u16;
    for index in (0..32).rev() {
        let sum = u16::from(target[index]) + u16::from(value[index]) + carry;
        target[index] = sum as u8;
        carry = sum >> 8;
    }
}

fn transform_batch(
    batch: &RecordBatch,
    rejected: &mut BTreeMap<&'static str, u64>,
    limits: Option<&SourceLimits>,
    sum_a: &mut [u8; 32],
    sum_b: &mut [u8; 32],
) -> Result<(RecordBatch, BatchMetrics)> {
    let ids = required::<StringArray>(batch, "id")?;
    let primary_names = required::<StringArray>(batch, "primary_name")?;
    let common_names = required::<ListArray>(batch, "common_names")?;
    let common_values = common_names
        .values()
        .as_any()
        .downcast_ref::<StringArray>()
        .context("common_names values must be strings")?;
    let brands = required::<StringArray>(batch, "brand_name")?;
    let categories = required::<StringArray>(batch, "category")?;
    let category_terms = optional::<StringArray>(batch, "category_terms");
    let localities = required::<StringArray>(batch, "locality")?;
    let regions = required::<StringArray>(batch, "region")?;
    let countries = required::<StringArray>(batch, "country")?;
    let confidences = required::<Float64Array>(batch, "confidence")?;
    let prominences = optional::<UInt8Array>(batch, "prominence_rank");
    let statuses = required::<StringArray>(batch, "operating_status")?;
    let geometries = required::<BinaryArray>(batch, "geometry")?;
    let objects = required::<Int32Array>(batch, "source_object_index")?;
    let groups = required::<Int32Array>(batch, "source_row_group")?;
    let indexes = required::<Int32Array>(batch, "source_row_index")?;
    let mut rows = Vec::new();
    let mut metrics = BatchMetrics::default();
    for index in 0..batch.num_rows() {
        let primary_name = text(primary_names, index).trim();
        if primary_name.is_empty() {
            *rejected.get_mut("missing_primary_name").unwrap() += 1;
            continue;
        }
        let raw_id = text(ids, index).trim();
        let Ok(identifier) = Uuid::parse_str(raw_id) else {
            *rejected.get_mut("invalid_uuid").unwrap() += 1;
            continue;
        };
        if identifier.to_string() != raw_id.to_ascii_lowercase() {
            *rejected.get_mut("invalid_uuid").unwrap() += 1;
            continue;
        }
        if text(statuses, index)
            .trim()
            .eq_ignore_ascii_case("permanently_closed")
        {
            *rejected.get_mut("permanently_closed").unwrap() += 1;
            continue;
        }
        let Some((longitude, latitude)) = (!geometries.is_null(index))
            .then(|| point(geometries.value(index)))
            .flatten()
        else {
            *rejected.get_mut("invalid_geometry").unwrap() += 1;
            continue;
        };
        if confidences.is_null(index)
            || !confidences.value(index).is_finite()
            || !(0.0..=1.0).contains(&confidences.value(index))
        {
            *rejected.get_mut("invalid_confidence").unwrap() += 1;
            continue;
        }
        if objects.is_null(index)
            || groups.is_null(index)
            || indexes.is_null(index)
            || objects.value(index) < 0
            || groups.value(index) < 0
            || indexes.value(index) < 0
        {
            *rejected.get_mut("invalid_source_locator").unwrap() += 1;
            continue;
        }
        let object = objects.value(index) as u32;
        let group = groups.value(index) as u32;
        let row_index = indexes.value(index) as u64;
        if let Some(limits) = limits {
            let invalid = limits
                .objects
                .get(object as usize)
                .is_none_or(|limit| group >= limit.row_groups || row_index >= limit.records);
            if invalid {
                *rejected.get_mut("invalid_source_locator").unwrap() += 1;
                continue;
            }
        }
        let mut terms = BTreeMap::<String, u8>::new();
        let mut multilingual = false;
        let mut add = |value: &str, mask: u8| {
            for token in tokens(value) {
                *terms.entry(token).or_default() |= mask;
            }
        };
        add(primary_name, 1);
        if !common_names.is_null(index) {
            let offsets = common_names.value_offsets();
            for position in offsets[index] as usize..offsets[index + 1] as usize {
                let common = text(common_values, position).trim();
                if !common.is_empty() && common != primary_name {
                    multilingual = true;
                }
                add(common, 1);
            }
        }
        add(text(brands, index), 2);
        add(
            category_terms.map_or_else(|| text(categories, index), |terms| text(terms, index)),
            4,
        );
        for value in [
            text(localities, index),
            text(regions, index),
            text(countries, index),
        ] {
            add(value, 8);
        }
        if terms.is_empty() {
            *rejected.get_mut("invalid_record").unwrap() += 1;
            continue;
        }
        let (partition_key, execution_group, partition_cell) = route(longitude, latitude);
        let rank = (confidences.value(index) * 255.0).round() as u8;
        let prominence = prominences
            .filter(|array| !array.is_null(index))
            .map_or(0, |array| array.value(index));
        if let Some(phrase) = entity_phrase_key(primary_name, prominence) {
            *terms.entry(phrase).or_default() |= 1;
        }
        let template = TermRow {
            execution_group,
            partition_cell,
            partition_key,
            token: String::new(),
            field_mask: 0,
            confidence_rank: rank,
            prominence_rank: prominence,
            feature_id: *identifier.as_bytes(),
            longitude,
            latitude,
            primary_name: primary_name.to_owned(),
            brand_name: text(brands, index).to_owned(),
            category: text(categories, index).to_owned(),
            locality: text(localities, index).to_owned(),
            region: text(regions, index).to_owned(),
            country: text(countries, index).to_owned(),
            source_object_index: object,
            source_row_group: group,
            source_row_index: row_index,
        };
        if terms.iter().any(|(term, _)| {
            let mut row = template.clone();
            row.token.clone_from(term);
            payload(&row).len() > MAX_RECORD_BYTES
        }) {
            *rejected.get_mut("record_too_large").unwrap() += 1;
            continue;
        }
        metrics.admitted += 1;
        metrics.multilingual += u64::from(multilingual);
        metrics.cjk += u64::from(terms.keys().any(|term| term.chars().any(is_cjk)));
        rows.extend(terms.into_iter().map(|(token, mask)| {
            let mut row = template.clone();
            row.token = token;
            row.field_mask = mask;
            row
        }));
    }
    let mut strings: Vec<StringBuilder> = (0..9).map(|_| StringBuilder::new()).collect();
    let mut partition_keys = UInt32Builder::new();
    let mut token_hashes = UInt64Builder::new();
    let mut masks = UInt8Builder::new();
    let mut ranks = UInt8Builder::new();
    let mut prominences = UInt8Builder::new();
    let mut ids = FixedSizeBinaryBuilder::new(16);
    let mut longitudes = Float64Builder::new();
    let mut latitudes = Float64Builder::new();
    let mut object_builder = UInt32Builder::new();
    let mut group_builder = UInt32Builder::new();
    let mut index_builder = UInt64Builder::new();
    let mut digests_a = FixedSizeBinaryBuilder::new(32);
    let mut digests_b = FixedSizeBinaryBuilder::new(32);
    for row in &rows {
        let values = [
            &row.execution_group,
            &row.partition_cell,
            &row.token,
            &row.primary_name,
            &row.brand_name,
            &row.category,
            &row.locality,
            &row.region,
            &row.country,
        ];
        for (builder, value) in strings.iter_mut().zip(values) {
            builder.append_value(value);
        }
        partition_keys.append_value(row.partition_key);
        token_hashes.append_value(token_hash(&row.token));
        masks.append_value(row.field_mask);
        ranks.append_value(row.confidence_rank);
        prominences.append_value(row.prominence_rank);
        ids.append_value(row.feature_id)?;
        longitudes.append_value(row.longitude);
        latitudes.append_value(row.latitude);
        object_builder.append_value(row.source_object_index);
        group_builder.append_value(row.source_row_group);
        index_builder.append_value(row.source_row_index);
        let encoded = payload(row);
        let digest_a = digest(DOMAIN_A, &encoded);
        let digest_b = digest(DOMAIN_B, &encoded);
        add_256(sum_a, &digest_a);
        add_256(sum_b, &digest_b);
        digests_a.append_value(digest_a)?;
        digests_b.append_value(digest_b)?;
    }
    let mut columns: Vec<ArrayRef> = vec![
        Arc::new(strings.remove(0).finish()),
        Arc::new(strings.remove(0).finish()),
        Arc::new(partition_keys.finish()),
        Arc::new(strings.remove(0).finish()),
        Arc::new(token_hashes.finish()),
        Arc::new(masks.finish()),
        Arc::new(ranks.finish()),
        Arc::new(prominences.finish()),
        Arc::new(ids.finish()),
        Arc::new(longitudes.finish()),
        Arc::new(latitudes.finish()),
    ];
    for mut builder in strings {
        columns.push(Arc::new(builder.finish()));
    }
    columns.push(Arc::new(object_builder.finish()));
    columns.push(Arc::new(group_builder.finish()));
    columns.push(Arc::new(index_builder.finish()));
    columns.push(Arc::new(digests_a.finish()));
    columns.push(Arc::new(digests_b.finish()));
    Ok((RecordBatch::try_new(output_schema(), columns)?, metrics))
}

fn hex(value: &[u8]) -> String {
    value.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn main() -> Result<()> {
    let args = args()?;
    let limits = args
        .source_limits
        .as_ref()
        .map(|path| {
            serde_json::from_reader(BufReader::new(File::open(path)?)).map_err(anyhow::Error::from)
        })
        .transpose()?;
    let started = Instant::now();
    let mut rejected = REJECTIONS.into_iter().map(|name| (name, 0)).collect();
    let mut input_features = 0_u64;
    let mut admitted_features = 0_u64;
    let mut multilingual_features = 0_u64;
    let mut cjk_features = 0_u64;
    let mut emitted_term_rows = 0_u64;
    let mut sum_a = [0_u8; 32];
    let mut sum_b = [0_u8; 32];
    let reader = StreamReader::try_new(BufReader::new(File::open(&args.input)?), None)?;
    let mut writer = StreamWriter::try_new(
        BufWriter::new(File::create(&args.output)?),
        &output_schema(),
    )?;
    for batch in reader {
        let batch = batch?;
        input_features += batch.num_rows() as u64;
        let (output, metrics) = transform_batch(
            &batch,
            &mut rejected,
            limits.as_ref(),
            &mut sum_a,
            &mut sum_b,
        )?;
        admitted_features += metrics.admitted;
        multilingual_features += metrics.multilingual;
        cjk_features += metrics.cjk;
        emitted_term_rows += output.num_rows() as u64;
        writer.write(&output)?;
    }
    writer.finish()?;
    let report = Report {
        schema: "overture-places-rust-transform-report-v1",
        transform_version: TRANSFORM_VERSION,
        tokenizer_version: TOKENIZER_VERSION,
        digest_version: DIGEST_VERSION,
        input_features,
        admitted_features,
        multilingual_features,
        cjk_features,
        emitted_term_rows,
        rejected_features: input_features - admitted_features,
        rejections_by_precedence: rejected,
        semantic_sum_a: hex(&sum_a),
        semantic_sum_b: hex(&sum_b),
        elapsed_seconds: started.elapsed().as_secs_f64(),
    };
    serde_json::to_writer_pretty(BufWriter::new(File::create(args.report)?), &report)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tokenizer_matches_hand_vectors() {
        assert_eq!(tokens(" Café / 東京 "), vec!["cafe", "東京"]);
        assert_eq!(tokens("カフェ"), vec!["カフェ", "カフ", "フェ"]);
    }

    #[test]
    fn final_sigma_folds_to_plain_sigma() {
        // Context-free lowercase of Σ is σ; final sigma ς folds to σ too, so a
        // lowercase Greek query matches the index. No terminal ς ever survives.
        assert_eq!(tokens("ΝΕΟΣ ΚΟΣΜΟΣ"), vec!["νεοσ", "κοσμοσ"]);
        assert_eq!(tokens("Ιρις"), vec!["ιρισ"]);
        assert!(!tokens("ΚΟΣΜΟΣ")[0].contains('\u{03c2}'));
    }

    #[test]
    fn lowercases_after_nfkd_for_styled_capitals() {
        // Math-styled and enclosed capitals compat-decompose to bare capitals;
        // lowercasing after NFKD makes them lowercase-searchable.
        assert_eq!(
            tokens("\u{1D4D3}\u{1D4EE}\u{1D4F9}\u{1D4D4}\u{1D4ED}"),
            vec!["deped"]
        );
    }

    #[test]
    fn trims_white_space_only_keeping_c0_controls() {
        // Rust str::trim keeps the C0 separators U+001C..U+001F that CPython
        // str.strip() would remove; they are token separators either way.
        assert_eq!(tokens("\u{1d}NUNU Shop"), vec!["nunu", "shop"]);
    }

    #[test]
    fn golden_token_vectors_match_fixture() {
        let path = concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tests/fixtures/places_tokenizer_v4_golden.json"
        );
        let fixture: serde_json::Value =
            serde_json::from_reader(File::open(path).unwrap()).unwrap();
        assert_eq!(
            fixture["tokenizer_version"].as_str().unwrap(),
            TOKENIZER_VERSION
        );
        for vector in fixture["token_vectors"].as_array().unwrap() {
            let input = vector["input"].as_str().unwrap();
            let expected: BTreeMap<String, ()> = vector["expected"]
                .as_array()
                .unwrap()
                .iter()
                .map(|token| (token.as_str().unwrap().to_owned(), ()))
                .collect();
            let actual: BTreeMap<String, ()> =
                tokens(input).into_iter().map(|token| (token, ())).collect();
            assert_eq!(
                actual,
                expected,
                "token vector {} mismatch",
                vector["name"].as_str().unwrap()
            );
        }
    }

    #[test]
    fn zero_point_routes_to_center_cell() {
        assert_eq!(route(0.0, 0.0), (32_896, "80".into(), "8080".into()));
    }
}
