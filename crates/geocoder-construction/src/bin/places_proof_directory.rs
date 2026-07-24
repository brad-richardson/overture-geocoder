//! Exact associative proof directories for sorted Places construction packs.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufReader, BufWriter};
use std::path::PathBuf;

use anyhow::{bail, Context, Result};
use arrow_array::{
    Array, BinaryArray, FixedSizeBinaryArray, LargeBinaryArray, RecordBatch, StringArray,
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

#[derive(Clone, Default, Serialize, PartialEq, Eq)]
struct Binding {
    records: u64,
    semantic_sum_a: String,
    semantic_sum_b: String,
}

#[derive(Clone, Default)]
struct Accumulator {
    records: u64,
    a: [u8; 32],
    b: [u8; 32],
}

impl Accumulator {
    fn add(&mut self, a: &[u8], b: &[u8]) -> Result<()> {
        let a: &[u8; 32] = a.try_into().context("digest A is not 32 bytes")?;
        let b: &[u8; 32] = b.try_into().context("digest B is not 32 bytes")?;
        add_256(&mut self.a, a);
        add_256(&mut self.b, b);
        self.records += 1;
        Ok(())
    }
    fn binding(&self) -> Binding {
        Binding {
            records: self.records,
            semantic_sum_a: hex(&self.a),
            semantic_sum_b: hex(&self.b),
        }
    }
}

#[derive(Serialize)]
struct RoutingGroup {
    execution_group: String,
    partition_cell: String,
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
    routing_summaries: Vec<RoutingGroup>,
}

fn required<'a, T: Array + 'static>(batch: &'a RecordBatch, name: &str) -> Result<&'a T> {
    batch
        .column_by_name(name)
        .with_context(|| format!("missing {name}"))?
        .as_any()
        .downcast_ref::<T>()
        .with_context(|| format!("wrong type for {name}"))
}

fn binary<'a>(batch: &'a RecordBatch, name: &str, row: usize) -> Result<&'a [u8]> {
    let value = batch
        .column_by_name(name)
        .with_context(|| format!("missing {name}"))?;
    if value.is_null(row) {
        bail!("null digest")
    }
    if let Some(array) = value.as_any().downcast_ref::<FixedSizeBinaryArray>() {
        Ok(array.value(row))
    } else if let Some(array) = value.as_any().downcast_ref::<BinaryArray>() {
        Ok(array.value(row))
    } else if let Some(array) = value.as_any().downcast_ref::<LargeBinaryArray>() {
        Ok(array.value(row))
    } else {
        bail!("wrong digest type")
    }
}

fn groups(value: BTreeMap<(String, String), Accumulator>) -> Vec<RoutingGroup> {
    value
        .into_iter()
        .map(|((execution_group, partition_cell), item)| RoutingGroup {
            execution_group,
            partition_cell,
            binding: item.binding(),
        })
        .collect()
}

fn add_256(target: &mut [u8; 32], value: &[u8; 32]) {
    let mut carry = 0_u16;
    for index in (0..32).rev() {
        let sum = u16::from(target[index]) + u16::from(value[index]) + carry;
        target[index] = sum as u8;
        carry = sum >> 8;
    }
}
fn hex(value: &[u8]) -> String {
    value.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn main() -> Result<()> {
    let mut values = std::env::args_os().skip(1);
    let mut input = None;
    let mut layout = None;
    let mut output = None;
    while let Some(flag) = values.next() {
        let value = values.next().context("missing command-line value")?;
        match flag.to_str() {
            Some("--input") => input = Some(PathBuf::from(value)),
            Some("--layout") => layout = Some(PathBuf::from(value)),
            Some("--output") => output = Some(PathBuf::from(value)),
            _ => bail!("unknown argument {}", flag.to_string_lossy()),
        }
    }
    let input = input.context("--input is required")?;
    let layout: Layout = serde_json::from_reader(BufReader::new(File::open(
        layout.context("--layout is required")?,
    )?))?;
    if layout
        .row_groups
        .iter()
        .enumerate()
        .any(|(i, g)| g.index as usize != i || g.records == 0)
    {
        bail!("invalid row-group layout")
    }
    let mut total = Accumulator::default();
    let mut routing = BTreeMap::new();
    let mut directories = Vec::new();
    let mut layout_position = 0_usize;
    let mut within = 0_u64;
    let mut row_acc = Accumulator::default();
    let mut row_routing = BTreeMap::new();
    let reader = StreamReader::try_new(BufReader::new(File::open(input)?), None)?;
    for batch in reader {
        let batch = batch?;
        let execution = required::<StringArray>(&batch, "execution_group")?;
        let cells = required::<StringArray>(&batch, "partition_cell")?;
        for row in 0..batch.num_rows() {
            if layout_position >= layout.row_groups.len()
                || execution.is_null(row)
                || cells.is_null(row)
            {
                bail!("proof rows exceed layout or contain null routing")
            }
            let a = binary(&batch, "semantic_digest_a", row)?;
            let b = binary(&batch, "semantic_digest_b", row)?;
            total.add(a, b)?;
            row_acc.add(a, b)?;
            let key = (execution.value(row).to_owned(), cells.value(row).to_owned());
            routing
                .entry(key.clone())
                .or_insert_with(Accumulator::default)
                .add(a, b)?;
            row_routing
                .entry(key)
                .or_insert_with(Accumulator::default)
                .add(a, b)?;
            within += 1;
            if within == layout.row_groups[layout_position].records {
                directories.push(RowGroupDirectory {
                    index: layout_position as u32,
                    binding: row_acc.binding(),
                    routing_groups: groups(std::mem::take(&mut row_routing)),
                });
                row_acc = Accumulator::default();
                within = 0;
                layout_position += 1;
            }
        }
    }
    if layout_position != layout.row_groups.len() || within != 0 {
        bail!("proof rows do not reconcile to layout")
    }
    let directory = Directory {
        schema: "overture-places-pack-proof-directory-v1",
        binding_schema: "sha256-add-mod-2^256-two-domain-v1",
        binding: total.binding(),
        row_groups: directories,
        routing_summaries: groups(routing),
    };
    serde_json::to_writer_pretty(
        BufWriter::new(File::create(output.context("--output is required")?)?),
        &directory,
    )?;
    Ok(())
}
