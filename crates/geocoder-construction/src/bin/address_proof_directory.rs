//! Exact associative proof directories for sorted Address packs.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufReader, BufWriter};
use std::path::PathBuf;

use anyhow::{bail, Context, Result};
use arrow_array::{
    Array, BinaryArray, FixedSizeBinaryArray, RecordBatch, StringArray, UInt32Array, UInt64Array,
};
use arrow_ipc::reader::StreamReader;
use serde::{Deserialize, Serialize};

#[derive(Deserialize)]
struct Layout {
    row_groups: Vec<RowGroupLayout>,
}

#[derive(Deserialize)]
struct RowGroupLayout {
    index: u32,
    records: u64,
}

#[derive(Clone, Default, Serialize)]
struct Binding {
    records: u64,
    semantic_sum_a: String,
    semantic_sum_b: String,
}

#[derive(Clone, Default)]
struct Accumulator {
    records: u64,
    sum_a: [u8; 32],
    sum_b: [u8; 32],
}

impl Accumulator {
    fn add(&mut self, digest_a: &[u8], digest_b: &[u8]) -> Result<()> {
        let digest_a: &[u8; 32] = digest_a.try_into().context("digest A is not 32 bytes")?;
        let digest_b: &[u8; 32] = digest_b.try_into().context("digest B is not 32 bytes")?;
        add_256(&mut self.sum_a, digest_a);
        add_256(&mut self.sum_b, digest_b);
        self.records += 1;
        Ok(())
    }

    fn binding(&self) -> Binding {
        Binding {
            records: self.records,
            semantic_sum_a: hex(&self.sum_a),
            semantic_sum_b: hex(&self.sum_b),
        }
    }
}

#[derive(Clone, Default)]
struct RoutingAccumulator {
    binding: Accumulator,
    minimum_route_hash: u64,
    maximum_route_hash: u64,
    initialized: bool,
}

impl RoutingAccumulator {
    fn add(&mut self, route_hash: u64, digest_a: &[u8], digest_b: &[u8]) -> Result<()> {
        self.binding.add(digest_a, digest_b)?;
        if !self.initialized {
            self.minimum_route_hash = route_hash;
            self.maximum_route_hash = route_hash;
            self.initialized = true;
        } else {
            self.minimum_route_hash = self.minimum_route_hash.min(route_hash);
            self.maximum_route_hash = self.maximum_route_hash.max(route_hash);
        }
        Ok(())
    }
}

#[derive(Serialize)]
struct RoutingGroup {
    country: String,
    maximum_bucket: u32,
    minimum_route_hash: u64,
    maximum_route_hash: u64,
    binding: Binding,
}

#[derive(Serialize)]
struct RowGroupDirectory {
    index: u32,
    binding: Binding,
    routing_groups: Vec<RoutingGroup>,
}

#[derive(Serialize)]
struct Directory {
    schema: &'static str,
    binding_schema: &'static str,
    binding: Binding,
    row_groups: Vec<RowGroupDirectory>,
    bucket_summaries: Vec<RoutingGroup>,
}

struct Args {
    input: PathBuf,
    layout: PathBuf,
    output: PathBuf,
}

fn args() -> Result<Args> {
    let mut input = None;
    let mut layout = None;
    let mut output = None;
    let mut values = std::env::args_os().skip(1);
    while let Some(flag) = values.next() {
        let value = values.next().context("missing command-line value")?;
        match flag.to_str() {
            Some("--input") => input = Some(value.into()),
            Some("--layout") => layout = Some(value.into()),
            Some("--output") => output = Some(value.into()),
            _ => bail!("unknown command-line argument {}", flag.to_string_lossy()),
        }
    }
    Ok(Args {
        input: input.context("--input is required")?,
        layout: layout.context("--layout is required")?,
        output: output.context("--output is required")?,
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

fn binary_value<'a>(batch: &'a RecordBatch, name: &str, row: usize) -> Result<&'a [u8]> {
    let array = batch
        .column_by_name(name)
        .with_context(|| format!("input is missing {name}"))?;
    if array.is_null(row) {
        bail!("input {name} contains a null");
    }
    if let Some(value) = array.as_any().downcast_ref::<FixedSizeBinaryArray>() {
        return Ok(value.value(row));
    }
    if let Some(value) = array.as_any().downcast_ref::<BinaryArray>() {
        return Ok(value.value(row));
    }
    bail!("input {name} has the wrong Arrow type")
}

fn routing_groups(value: BTreeMap<(String, u32), RoutingAccumulator>) -> Vec<RoutingGroup> {
    value
        .into_iter()
        .map(|((country, maximum_bucket), item)| RoutingGroup {
            country,
            maximum_bucket,
            minimum_route_hash: item.minimum_route_hash,
            maximum_route_hash: item.maximum_route_hash,
            binding: item.binding.binding(),
        })
        .collect()
}

fn main() -> Result<()> {
    let args = args()?;
    let layout: Layout = serde_json::from_reader(BufReader::new(File::open(args.layout)?))?;
    if layout
        .row_groups
        .iter()
        .enumerate()
        .any(|(index, group)| group.index as usize != index || group.records == 0)
    {
        bail!("row-group layout must be contiguous and positive");
    }
    let mut reader = StreamReader::try_new(BufReader::new(File::open(args.input)?), None)?;
    let mut global = Accumulator::default();
    let mut buckets = BTreeMap::<(String, u32), RoutingAccumulator>::new();
    let mut directories = Vec::with_capacity(layout.row_groups.len());
    let mut group_index = 0_usize;
    let mut group_rows = 0_u64;
    let mut group_binding = Accumulator::default();
    let mut group_routes = BTreeMap::<(String, u32), RoutingAccumulator>::new();
    for batch in &mut reader {
        let batch = batch?;
        let countries = required::<StringArray>(&batch, "country")?;
        let maximum_buckets = required::<UInt32Array>(&batch, "maximum_bucket")?;
        let route_hashes = required::<UInt64Array>(&batch, "route_hash")?;
        for row in 0..batch.num_rows() {
            if [
                countries.is_null(row),
                maximum_buckets.is_null(row),
                route_hashes.is_null(row),
            ]
            .into_iter()
            .any(|value| value)
            {
                bail!("proof input contains null routing or digest values");
            }
            let country = countries.value(row).to_owned();
            let bucket = maximum_buckets.value(row);
            let route_hash = route_hashes.value(row);
            if bucket != (route_hash >> 48) as u32 {
                bail!("maximum bucket differs from the high 16 route bits");
            }
            let digest_a = binary_value(&batch, "semantic_digest_a", row)?;
            let digest_b = binary_value(&batch, "semantic_digest_b", row)?;
            global.add(digest_a, digest_b)?;
            group_binding.add(digest_a, digest_b)?;
            buckets
                .entry((country.clone(), bucket))
                .or_default()
                .add(route_hash, digest_a, digest_b)?;
            group_routes
                .entry((country, bucket))
                .or_default()
                .add(route_hash, digest_a, digest_b)?;
            group_rows += 1;
            let expected = layout
                .row_groups
                .get(group_index)
                .context("proof input has more rows than its layout")?;
            if group_rows == expected.records {
                directories.push(RowGroupDirectory {
                    index: expected.index,
                    binding: group_binding.binding(),
                    routing_groups: routing_groups(std::mem::take(&mut group_routes)),
                });
                group_index += 1;
                group_rows = 0;
                group_binding = Accumulator::default();
            } else if group_rows > expected.records {
                bail!("Arrow batch crossed a declared row-group boundary incorrectly");
            }
        }
    }
    if group_rows != 0 || group_index != layout.row_groups.len() {
        bail!("proof input row count differs from its row-group layout");
    }
    let output = Directory {
        schema: "overture-address-pack-proof-directory-v1",
        binding_schema: "sha256-add-mod-2^256-two-domain-v1",
        binding: global.binding(),
        row_groups: directories,
        bucket_summaries: routing_groups(buckets),
    };
    serde_json::to_writer_pretty(BufWriter::new(File::create(args.output)?), &output)?;
    Ok(())
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
