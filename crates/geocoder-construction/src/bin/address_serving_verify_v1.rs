//! Independent structural and logical verifier for Address v1 serving bytes.

use std::fs::File;
use std::io::{BufReader, BufWriter, Read};
use std::path::PathBuf;

use anyhow::{bail, Context, Result};
use serde::Serialize;
use sha2::{Digest, Sha256};

const MAGIC: &[u8; 8] = b"OAV1ART\0";
const HEADER_BYTES: usize = 44;
const INDEX_BYTES: usize = 24;
const DOMAIN_A: &[u8] = b"overture-address-construction-v1\0";
const DOMAIN_B: &[u8] = b"overture-address-construction-v1\x01";

struct Args {
    input: PathBuf,
    output: PathBuf,
    max_input_bytes: u64,
    query: Option<[String; 8]>,
}

#[derive(Serialize)]
struct Binding {
    records: u64,
    semantic_sum_a: String,
    semantic_sum_b: String,
}

#[derive(Serialize)]
struct Report {
    schema: &'static str,
    records: u64,
    bytes: u64,
    minimum_route_hash: Option<u64>,
    maximum_route_hash: Option<u64>,
    binding: Binding,
    query_feature_ids: Option<Vec<String>>,
}

#[derive(Debug, PartialEq, Eq, PartialOrd, Ord)]
struct TotalKey {
    route_hash: u64,
    normalized: [String; 8],
    feature_id: [u8; 16],
    source_object_index: u32,
    source_row_group: u32,
    source_row_index: u64,
}

struct Decoded {
    key: TotalKey,
}

fn args() -> Result<Args> {
    let mut input = None;
    let mut output = None;
    let mut maximum = None;
    let mut query = None;
    let mut values = std::env::args_os().skip(1);
    while let Some(flag) = values.next() {
        let value = values.next().context("missing command-line value")?;
        match flag.to_str() {
            Some("--input") => input = Some(value.into()),
            Some("--output") => output = Some(value.into()),
            Some("--max-input-bytes") => {
                maximum = Some(
                    value
                        .to_str()
                        .context("max input bytes is not UTF-8")?
                        .parse::<u64>()?,
                )
            }
            Some("--query-json") => {
                let fields: Vec<String> =
                    serde_json::from_reader(BufReader::new(File::open(PathBuf::from(value))?))?;
                query = Some(
                    fields
                        .try_into()
                        .map_err(|_| anyhow::anyhow!("query must contain exactly eight fields"))?,
                );
            }
            _ => bail!("unknown command-line argument {}", flag.to_string_lossy()),
        }
    }
    Ok(Args {
        input: input.context("--input is required")?,
        output: output.context("--output is required")?,
        max_input_bytes: maximum.context("--max-input-bytes is required")?,
        query,
    })
}

fn main() -> Result<()> {
    let args = args()?;
    let size = args.input.metadata()?.len();
    if size > args.max_input_bytes {
        bail!("serving artifact exceeds verifier input cap");
    }
    let mut bytes = Vec::with_capacity(size as usize);
    BufReader::new(File::open(&args.input)?).read_to_end(&mut bytes)?;
    let report = verify(&bytes, args.query.as_ref())?;
    serde_json::to_writer_pretty(BufWriter::new(File::create(args.output)?), &report)?;
    Ok(())
}

fn verify(bytes: &[u8], query: Option<&[String; 8]>) -> Result<Report> {
    if bytes.len() < HEADER_BYTES || &bytes[..8] != MAGIC {
        bail!("serving artifact has an invalid header");
    }
    if read_u32(bytes, 8)? != 1 {
        bail!("serving artifact version is unsupported");
    }
    let records = read_u64(bytes, 12)?;
    let index_offset = read_u64(bytes, 20)?;
    let payload_offset = read_u64(bytes, 28)?;
    let payload_bytes = read_u64(bytes, 36)?;
    if index_offset != HEADER_BYTES as u64
        || payload_offset
            != index_offset
                .checked_add(
                    records
                        .checked_mul(INDEX_BYTES as u64)
                        .context("index overflow")?,
                )
                .context("index overflow")?
        || payload_offset
            .checked_add(payload_bytes)
            .context("payload overflow")?
            != bytes.len() as u64
    {
        bail!("serving artifact offsets do not reconcile");
    }
    let mut previous = None;
    let mut sum_a = [0_u8; 32];
    let mut sum_b = [0_u8; 32];
    let query_hash = query.map(route_hash);
    let mut query_feature_ids = query.map(|_| Vec::new());
    let mut minimum_route_hash = None;
    let mut maximum_route_hash = None;
    let mut expected_payload_offset = payload_offset;
    for index in 0..records {
        let entry = HEADER_BYTES + index as usize * INDEX_BYTES;
        let route_hash = read_u64(bytes, entry)?;
        let offset = read_u64(bytes, entry + 8)?;
        let length = read_u32(bytes, entry + 16)? as u64;
        if read_u32(bytes, entry + 20)? != 0 || offset != expected_payload_offset {
            bail!("serving index is reserved-field dirty or payloads are not contiguous");
        }
        let end = offset.checked_add(length).context("record overflow")?;
        if end > bytes.len() as u64 {
            bail!("serving record exceeds the artifact");
        }
        let payload = &bytes[offset as usize..end as usize];
        let decoded = decode(payload, route_hash)?;
        if previous.as_ref().is_some_and(|value| value >= &decoded.key) {
            bail!("serving records are not in unique total order");
        }
        if query_hash == Some(route_hash)
            && query.is_some_and(|fields| fields == &decoded.key.normalized)
        {
            query_feature_ids
                .as_mut()
                .unwrap()
                .push(format_uuid(&decoded.key.feature_id));
        }
        add_256(&mut sum_a, &digest(DOMAIN_A, payload));
        add_256(&mut sum_b, &digest(DOMAIN_B, payload));
        minimum_route_hash.get_or_insert(route_hash);
        maximum_route_hash = Some(route_hash);
        previous = Some(decoded.key);
        expected_payload_offset = end;
    }
    if expected_payload_offset != bytes.len() as u64 {
        bail!("serving payload bytes do not reconcile");
    }
    Ok(Report {
        schema: "overture-address-serving-verification-v1",
        records,
        bytes: bytes.len() as u64,
        minimum_route_hash,
        maximum_route_hash,
        binding: Binding {
            records,
            semantic_sum_a: hex(&sum_a),
            semantic_sum_b: hex(&sum_b),
        },
        query_feature_ids,
    })
}

fn decode(payload: &[u8], expected_hash: u64) -> Result<Decoded> {
    let mut position = 0_usize;
    let normalized: [String; 8] = (0..8)
        .map(|_| read_text(payload, &mut position))
        .collect::<Result<Vec<_>>>()?
        .try_into()
        .map_err(|_| anyhow::anyhow!("normalized key does not contain eight fields"))?;
    if position + 16 + 4 + 4 + 4 + 4 + 8 > payload.len() {
        bail!("serving record fixed fields are truncated");
    }
    let feature_id = payload[position..position + 16].try_into().unwrap();
    position += 16;
    position += 8;
    let source_object_index = read_u32(payload, position)?;
    position += 4;
    let source_row_group = read_u32(payload, position)?;
    position += 4;
    let source_row_index = read_u64(payload, position)?;
    position += 8;
    for _ in 0..6 {
        read_text(payload, &mut position)?;
    }
    let levels = read_u64(payload, position)?;
    position += 8;
    if levels > (payload.len().saturating_sub(position) / 8) as u64 {
        bail!("serving address-level count is implausible");
    }
    for _ in 0..levels {
        read_text(payload, &mut position)?;
    }
    if position != payload.len() || route_hash(&normalized) != expected_hash {
        bail!("serving record is trailing or routed incorrectly");
    }
    Ok(Decoded {
        key: TotalKey {
            route_hash: expected_hash,
            normalized,
            feature_id,
            source_object_index,
            source_row_group,
            source_row_index,
        },
    })
}

fn read_text(payload: &[u8], position: &mut usize) -> Result<String> {
    let length = read_u64(payload, *position)?;
    *position += 8;
    let end = (*position as u64)
        .checked_add(length)
        .context("text overflow")? as usize;
    let value = std::str::from_utf8(payload.get(*position..end).context("text is truncated")?)?;
    *position = end;
    Ok(value.to_owned())
}

fn read_u32(bytes: &[u8], offset: usize) -> Result<u32> {
    Ok(u32::from_be_bytes(
        bytes
            .get(offset..offset + 4)
            .context("u32 is truncated")?
            .try_into()
            .unwrap(),
    ))
}

fn read_u64(bytes: &[u8], offset: usize) -> Result<u64> {
    Ok(u64::from_be_bytes(
        bytes
            .get(offset..offset + 8)
            .context("u64 is truncated")?
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

fn hex(value: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(value.len() * 2);
    for byte in value {
        output.push(DIGITS[(byte >> 4) as usize] as char);
        output.push(DIGITS[(byte & 0x0f) as usize] as char);
    }
    output
}

fn format_uuid(bytes: &[u8; 16]) -> String {
    let value = hex(bytes);
    format!(
        "{}-{}-{}-{}-{}",
        &value[0..8],
        &value[8..12],
        &value[12..16],
        &value[16..20],
        &value[20..32]
    )
}
