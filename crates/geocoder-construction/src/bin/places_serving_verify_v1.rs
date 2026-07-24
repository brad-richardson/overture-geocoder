//! Independent structural/logical verifier for Places construction-v1 serving bytes.
//!
//! Two verification surfaces share one decoder:
//!   * `--mode routed|head`: a single serving artifact is structurally and
//!     logically self-consistent (unchanged contract).
//!   * `--mode head-sharded --manifest <path>`: a *set* of head shards binds
//!     into one global head. Every shard is individually valid, every entry's
//!     index hash maps to the shard it lives in, and the cross-shard totals
//!     (records, distinct index entries, and the dual-lane additive head
//!     digest) reconcile against the manifest's declared binding sums.

use anyhow::{bail, Context, Result};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::fs::File;
use std::io::{BufReader, Read};
use std::path::{Path, PathBuf};

type OrderKey = (String, String, u8, [u8; 16], u32, u32, u64);

const HEAD_DIGEST_DOMAIN_A: &[u8] = b"overture-places-head-shard-v1\0";
const HEAD_DIGEST_DOMAIN_B: &[u8] = b"overture-places-head-shard-v1\x01";

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

/// Shard id owning `hash` under a `shard_bits`-wide top-bit prefix. With
/// `shard_bits = 12` this is the first three hex nibbles of the index hash,
/// mirroring the production UUID-prefix ID index.
fn shard_of(hash: u64, shard_bits: u32) -> u64 {
    hash >> (64 - shard_bits)
}

fn head_digest(domain: &[u8], payload: &[u8]) -> [u8; 32] {
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

/// Result of structurally verifying one serving artifact.
struct Verified {
    /// Total entry (record) count.
    records: u64,
    /// Distinct index entries (one per serving key, i.e. per token in head mode).
    index_entries: u32,
    /// Distinct index keys in payload order (each `index_key(mode, cell, token)`).
    keys: Vec<Vec<u8>>,
    /// Dual-lane additive head-entry digest, matching the encoder's sidecar.
    head_sum_a: [u8; 32],
    head_sum_b: [u8; 32],
}

/// Full structural + logical verification of one serving artifact, identical to
/// the original single-artifact contract, additionally returning the distinct
/// index keys and dual-lane head digest for cross-shard reconciliation.
fn verify_artifact(data: &[u8], mode: &str) -> Result<Verified> {
    let magic: &[u8; 8] = if mode == "routed" {
        b"PLRV0002"
    } else if mode == "head" {
        b"PLHD0002"
    } else {
        bail!("invalid mode")
    };
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
    let mut expected_index = Vec::<(u64, Vec<u8>, u64, u64, u32)>::new();
    let mut active_key: Option<Vec<u8>> = None;
    let mut keys = Vec::<Vec<u8>>::new();
    let mut head_sum_a = [0_u8; 32];
    let mut head_sum_b = [0_u8; 32];
    while position < index_offset {
        let payload_offset = position as u64;
        let length = u32_at(data, &mut position)? as usize;
        let entry = take(data, &mut position, length)?;
        add_256(&mut head_sum_a, &head_digest(HEAD_DIGEST_DOMAIN_A, entry));
        add_256(&mut head_sum_b, &head_digest(HEAD_DIGEST_DOMAIN_B, entry));
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
            mode,
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
            keys.push(serving_key.clone());
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
    let stored_count = u32_at(data, &mut position)? as usize;
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
    Ok(Verified {
        records: count,
        index_entries: index_count as u32,
        keys,
        head_sum_a,
        head_sum_b,
    })
}

#[derive(Deserialize)]
struct Manifest {
    schema: String,
    shard_count: u64,
    shard_bits: u32,
    total_records: u64,
    total_index_entries: u64,
    head_sum_a: String,
    head_sum_b: String,
    shards: Vec<ShardEntry>,
}

#[derive(Deserialize)]
struct ShardEntry {
    shard_id: u64,
    path: String,
    bytes: u64,
    records: u64,
    index_entries: u64,
    head_sum_a: String,
    head_sum_b: String,
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

fn verify_sharded_head(manifest_path: &Path) -> Result<()> {
    let manifest: Manifest = serde_json::from_reader(BufReader::new(
        File::open(manifest_path).context("open head manifest")?,
    ))
    .context("parse head manifest")?;
    if manifest.schema != "overture-places-global-head-sharded-v1" {
        bail!("unexpected head manifest schema")
    }
    // Shard count is a per-build value, but must be a power of two so the top
    // `shard_bits` of the index hash address it exactly.
    if manifest.shard_bits == 0
        || manifest.shard_bits > 24
        || manifest.shard_count != (1_u64 << manifest.shard_bits)
    {
        bail!("head manifest shard count is not a power of two matching shard_bits")
    }
    let declared_sum_a = parse_hex_256(&manifest.head_sum_a)?;
    let declared_sum_b = parse_hex_256(&manifest.head_sum_b)?;

    let base = manifest_path.parent().unwrap_or_else(|| Path::new("."));
    let mut seen = std::collections::BTreeSet::<u64>::new();
    let mut total_records = 0_u64;
    let mut total_index_entries = 0_u64;
    let mut total_sum_a = [0_u8; 32];
    let mut total_sum_b = [0_u8; 32];
    for shard in &manifest.shards {
        if shard.shard_id >= manifest.shard_count {
            bail!("head shard id is out of range")
        }
        if !seen.insert(shard.shard_id) {
            bail!("head manifest lists a shard id twice")
        }
        let path = {
            let candidate = PathBuf::from(&shard.path);
            if candidate.is_absolute() {
                candidate
            } else {
                base.join(candidate)
            }
        };
        let mut bytes = Vec::new();
        BufReader::new(
            File::open(&path).with_context(|| format!("open shard {}", shard.shard_id))?,
        )
        .read_to_end(&mut bytes)?;
        if bytes.len() as u64 != shard.bytes {
            bail!(
                "head shard {} byte length differs from manifest",
                shard.shard_id
            )
        }
        let verified = verify_artifact(&bytes, "head")
            .with_context(|| format!("verify head shard {}", shard.shard_id))?;
        // Every entry's index hash must map to exactly this shard.
        for key in &verified.keys {
            if shard_of(index_hash(key), manifest.shard_bits) != shard.shard_id {
                bail!(
                    "head shard {} contains a mis-assigned token",
                    shard.shard_id
                )
            }
        }
        // Per-shard declared values must match the independently decoded bytes.
        if verified.records != shard.records
            || u64::from(verified.index_entries) != shard.index_entries
            || verified.head_sum_a != parse_hex_256(&shard.head_sum_a)?
            || verified.head_sum_b != parse_hex_256(&shard.head_sum_b)?
        {
            bail!(
                "head shard {} does not reconcile with its manifest entry",
                shard.shard_id
            )
        }
        total_records += verified.records;
        total_index_entries += u64::from(verified.index_entries);
        add_256(&mut total_sum_a, &verified.head_sum_a);
        add_256(&mut total_sum_b, &verified.head_sum_b);
    }
    // Cross-shard reconciliation against the declared binding sums. Assignment
    // correctness plus each shard's own per-token index uniqueness guarantees a
    // token can appear in at most one shard, so equal totals prove the sharding
    // is a lossless partition of the un-sharded head.
    if total_records != manifest.total_records
        || total_index_entries != manifest.total_index_entries
        || total_sum_a != declared_sum_a
        || total_sum_b != declared_sum_b
    {
        bail!("sharded head cross-shard totals do not reconcile with the manifest binding")
    }
    Ok(())
}

fn main() -> Result<()> {
    let mut values = std::env::args_os().skip(1);
    let mut input = None;
    let mut manifest = None;
    let mut mode = None;
    while let Some(flag) = values.next() {
        let value = values.next().context("missing command-line value")?;
        match flag.to_str() {
            Some("--input") => input = Some(PathBuf::from(value)),
            Some("--manifest") => manifest = Some(PathBuf::from(value)),
            Some("--mode") => mode = value.to_str().map(str::to_owned),
            _ => bail!("unknown argument"),
        }
    }
    let mode = mode.context("--mode is required")?;
    if mode == "head-sharded" {
        return verify_sharded_head(&manifest.context("--manifest is required")?);
    }
    let mut data = Vec::new();
    BufReader::new(File::open(input.context("--input is required")?)?).read_to_end(&mut data)?;
    verify_artifact(&data, &mode)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{add_256, index_hash, shard_of};

    /// Reference top-`n`-per-token merge — the associative/idempotent oracle the
    /// DuckDB control-plane tree-merge implements. Candidates are
    /// `(token, sort_key)`; a lower `sort_key` ranks higher.
    fn top_n_merge(groups: &[&[(u32, u64)]], n: usize) -> Vec<(u32, u64)> {
        use std::collections::BTreeMap;
        let mut by_token: BTreeMap<u32, Vec<u64>> = BTreeMap::new();
        for group in groups {
            for &(token, key) in *group {
                by_token.entry(token).or_default().push(key);
            }
        }
        let mut output = Vec::new();
        for (token, mut keys) in by_token {
            keys.sort_unstable();
            keys.dedup();
            keys.truncate(n);
            for key in keys {
                output.push((token, key));
            }
        }
        output.sort_unstable();
        output
    }

    #[test]
    fn shard_assignment_is_the_top_index_hash_bits() {
        use sha2::Digest;
        // 12 bits => 4096 shards => first three hex nibbles of the index hash.
        for token in ["cafe", "tokyo tower", "東京", "a", "zzzz"] {
            let hash = index_hash(token.as_bytes());
            let shard = shard_of(hash, 12);
            assert!(shard < 4096);
            assert_eq!(shard, hash >> 52);
            // Hex-prefix equivalence with the ID-index sharding convention.
            let mut full = sha2::Sha256::new();
            full.update(b"overture-places-serving-index-v1\0");
            full.update(token.as_bytes());
            let digest = full.finalize();
            let prefix = format!("{shard:03x}");
            assert_eq!(prefix, format!("{:02x}{:x}", digest[0], digest[1] >> 4));
        }
    }

    #[test]
    fn merge_is_associative_over_partition_order() {
        let a: &[(u32, u64)] = &[(1, 10), (1, 30), (2, 5), (7, 9)];
        let b: &[(u32, u64)] = &[(1, 20), (1, 5), (2, 8), (9, 1)];
        let c: &[(u32, u64)] = &[(1, 25), (7, 3), (7, 100), (2, 2)];
        // top-2 per token, merged in two different fold orders plus flat.
        let left = top_n_merge(&[a, b], 2);
        let left = top_n_merge(&[&left, c], 2);
        let right = top_n_merge(&[b, c], 2);
        let right = top_n_merge(&[a, &right], 2);
        let flat = top_n_merge(&[a, b, c], 2);
        assert_eq!(left, right);
        assert_eq!(left, flat);
    }

    #[test]
    fn additive_digest_is_partition_independent() {
        // Splitting entries across shards and summing per shard must equal the
        // whole-set sum, regardless of which shard an entry lands in.
        let entries: Vec<[u8; 32]> = (0..7u8).map(|i| [i.wrapping_mul(37); 32]).collect();
        let mut whole = [0_u8; 32];
        for entry in &entries {
            add_256(&mut whole, entry);
        }
        let mut shard_a = [0_u8; 32];
        let mut shard_b = [0_u8; 32];
        for (index, entry) in entries.iter().enumerate() {
            if index % 2 == 0 {
                add_256(&mut shard_a, entry);
            } else {
                add_256(&mut shard_b, entry);
            }
        }
        let mut recombined = shard_a;
        add_256(&mut recombined, &shard_b);
        assert_eq!(whole, recombined);
    }
}
