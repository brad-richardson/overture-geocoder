//! Independent structural/logical verifier for Places construction-v1 serving bytes.

use anyhow::{bail, Context, Result};
use sha2::{Digest, Sha256};
use std::fs::File;
use std::io::{BufReader, Read};
use std::path::PathBuf;

type OrderKey = (String, String, u8, [u8; 16], u32, u32, u64);
type IndexEntry = (u64, Vec<u8>, u64, u64, u32);

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

fn take<'a>(data: &'a [u8], position: &mut usize, length: usize) -> Result<&'a [u8]> {
    let end = position.checked_add(length).context("offset overflow")?;
    if end > data.len() {
        bail!("truncated entry")
    };
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
fn text<'a>(data: &'a [u8], position: &mut usize) -> Result<&'a str> {
    let length = u16_at(data, position)? as usize;
    Ok(std::str::from_utf8(take(data, position, length)?)?)
}

fn main() -> Result<()> {
    let mut values = std::env::args_os().skip(1);
    let mut input = None;
    let mut mode = None;
    while let Some(flag) = values.next() {
        let value = values.next().context("missing command-line value")?;
        match flag.to_str() {
            Some("--input") => input = Some(PathBuf::from(value)),
            Some("--mode") => mode = value.to_str().map(str::to_owned),
            _ => bail!("unknown argument"),
        }
    }
    let mode = mode.context("--mode is required")?;
    let magic = if mode == "routed" {
        b"PLRV0002"
    } else if mode == "head" {
        b"PLHD0002"
    } else {
        bail!("invalid mode")
    };
    let mut data = Vec::new();
    BufReader::new(File::open(input.context("--input is required")?)?).read_to_end(&mut data)?;
    if data.len() < 36 || &data[..8] != magic {
        bail!("bad serving magic")
    }
    let count = u64::from_le_bytes(data[8..16].try_into().unwrap());
    let index_offset = usize::try_from(u64::from_le_bytes(data[16..24].try_into().unwrap()))
        .context("index offset overflows")?;
    let index_count = u32::from_le_bytes(data[24..28].try_into().unwrap()) as usize;
    if u32::from_le_bytes(data[28..32].try_into().unwrap()) != 0
        || index_offset < 32
        || index_offset > data.len()
    {
        bail!("bad serving header")
    }
    let mut position = 32;
    let mut observed = 0;
    let mut previous: Option<OrderKey> = None;
    let mut expected_index = Vec::<IndexEntry>::new();
    let mut active_key: Option<Vec<u8>> = None;
    while position < index_offset {
        let payload_offset = position as u64;
        let length = u32_at(&data, &mut position)? as usize;
        let entry = take(&data, &mut position, length)?;
        let mut at = 0;
        let token = text(entry, &mut at)?.to_owned();
        let cell = if mode == "routed" {
            text(entry, &mut at)?.to_owned()
        } else {
            String::new()
        };
        let mask = take(entry, &mut at, 1)?[0];
        let rank = take(entry, &mut at, 1)?[0];
        let id: [u8; 16] = take(entry, &mut at, 16)?.try_into().unwrap();
        let lon = f64::from_le_bytes(take(entry, &mut at, 8)?.try_into().unwrap());
        let lat = f64::from_le_bytes(take(entry, &mut at, 8)?.try_into().unwrap());
        let object = u32_at(entry, &mut at)?;
        let group = u32_at(entry, &mut at)?;
        let row = u64_at(entry, &mut at)?;
        for _ in 0..6 {
            text(entry, &mut at)?;
        }
        if at != entry.len()
            || token.is_empty()
            || mask == 0
            || !lon.is_finite()
            || !lat.is_finite()
            || (mode == "routed" && cell.len() != 4)
        {
            bail!("invalid serving entry")
        };
        let key = (cell, token, 255 - rank, id, object, group, row);
        if previous.as_ref().is_some_and(|value| value > &key) {
            bail!("serving order regressed")
        };
        previous = Some(key);
        let serving_key = index_key(
            &mode,
            &previous.as_ref().unwrap().0,
            &previous.as_ref().unwrap().1,
        );
        let encoded_bytes = 4_u64 + length as u64;
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
        bail!("serving count differs")
    };
    let stored_count = u32_at(&data, &mut position)? as usize;
    if stored_count != index_count {
        bail!("serving index count differs")
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
        bail!("serving index is truncated")
    }
    expected_index.sort_by(|left, right| (left.0, &left.1).cmp(&(right.0, &right.1)));
    let mut expected_key_offset = 0_u64;
    for expected in expected_index {
        let hash = u64_at(&data, &mut position)?;
        let key_offset = u64_at(&data, &mut position)?;
        let key_length = u32_at(&data, &mut position)? as usize;
        let records = u32_at(&data, &mut position)?;
        let payload_offset = u64_at(&data, &mut position)?;
        let payload_bytes = u64_at(&data, &mut position)?;
        let key_position = key_start
            .checked_add(usize::try_from(key_offset).context("key offset overflows")?)
            .context("key offset overflows")?;
        let stored_key = data
            .get(key_position..key_position + key_length)
            .context("serving index key is truncated")?;
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
            bail!("serving index does not reconcile")
        }
        expected_key_offset += key_length as u64;
    }
    if position != key_start || key_start + expected_key_offset as usize != data.len() {
        bail!("serving index length differs")
    }
    Ok(())
}
