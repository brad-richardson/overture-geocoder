//! Export the Rust standard-library Unicode property tables that the Places
//! `tokenizer_version` `nfkd-lower-stripmark-cjk-bigram-v4` depends on, so the
//! frozen Python semantic baseline can emulate Rust string semantics exactly
//! instead of relying on CPython's independent (and version-skewed) Unicode
//! opinion.
//!
//! Two properties are exported, both read straight from the same
//! `std::char` methods the authoritative `places-transform-v1` tokenizer uses:
//!
//! * `word_char_ranges` — inclusive codepoint ranges where
//!   `char::is_alphanumeric()` is true. The tokenizer's word class is exactly
//!   this set plus `_`.
//! * `lowercase_map` — for every scalar value whose `char::to_lowercase()`
//!   differs from itself, the mapped sequence of codepoints. `to_lowercase`
//!   is context-free (no Greek `Final_Sigma`), so applying this map per-char
//!   in Python reproduces Rust lowercasing without CPython's `str.lower`.
//!
//! The output is deterministic; regenerate the checked-in
//! `scripts/places_unicode_tables_v1.json` with:
//!
//! ```text
//! cargo run -p geocoder-construction --bin places-unicode-tables-v1 \
//!   -- --output scripts/places_unicode_tables_v1.json
//! ```

use std::fs::File;
use std::io::BufWriter;
use std::path::PathBuf;

use anyhow::{bail, Context, Result};
use serde::Serialize;

/// Unicode version the tokenizer contract pins. The exported tables are the
/// source of truth for the word class and lowercase map, but NFKD and combining
/// marks come from `unicode-normalization`, so both sides must agree on one
/// version. `assert_pinned_unicode_version` fails closed if the crate moves.
const UNICODE_VERSION: &str = "17.0.0";

/// Fail the build if `unicode-normalization` moves off the pinned version, so
/// the label this exporter stamps into the tables cannot silently go stale.
const _: () = assert!(
    unicode_normalization::UNICODE_VERSION.0 == 17 && unicode_normalization::UNICODE_VERSION.1 == 0
);
const TOKENIZER_VERSION: &str = "nfkd-lower-stripmark-cjk-bigram-v4";
const SCHEMA: &str = "overture-places-unicode-tables-v1";

#[derive(Serialize)]
struct Tables {
    schema: &'static str,
    unicode_version: &'static str,
    tokenizer_version: &'static str,
    /// U+03C2 (final sigma) -> U+03C3 (plain sigma), applied after lowercasing.
    sigma_fold: [u32; 2],
    /// Codepoints where `char::is_whitespace()` (Unicode `White_Space`) is
    /// true. `str::trim` strips exactly these — notably *excluding* the C0
    /// separators U+001C..U+001F that CPython `str.strip()` also strips.
    whitespace: Vec<u32>,
    /// Inclusive `[start, end]` codepoint ranges where `is_alphanumeric()`.
    word_char_ranges: Vec<[u32; 2]>,
    /// Codepoint -> lowercase codepoint sequence, for chars `to_lowercase`
    /// changes. Keys are decimal strings for portable JSON object keys.
    lowercase_map: std::collections::BTreeMap<String, Vec<u32>>,
}

fn scalar_values() -> impl Iterator<Item = char> {
    (0u32..=0x10FFFF).filter_map(char::from_u32)
}

fn word_char_ranges() -> Vec<[u32; 2]> {
    let mut ranges: Vec<[u32; 2]> = Vec::new();
    for character in scalar_values() {
        if !character.is_alphanumeric() {
            continue;
        }
        let point = character as u32;
        match ranges.last_mut() {
            Some(range) if range[1] + 1 == point => range[1] = point,
            _ => ranges.push([point, point]),
        }
    }
    ranges
}

fn lowercase_map() -> std::collections::BTreeMap<String, Vec<u32>> {
    let mut map = std::collections::BTreeMap::new();
    for character in scalar_values() {
        let lowered: Vec<u32> = character.to_lowercase().map(|lower| lower as u32).collect();
        if lowered.len() != 1 || lowered[0] != character as u32 {
            map.insert(format!("{}", character as u32), lowered);
        }
    }
    map
}

fn main() -> Result<()> {
    let mut output: Option<PathBuf> = None;
    let mut values = std::env::args_os().skip(1);
    while let Some(flag) = values.next() {
        match flag.to_str() {
            Some("--output") => {
                output = Some(values.next().context("--output needs a value")?.into())
            }
            other => bail!("unknown argument {}", other.unwrap_or("<non-utf8>")),
        }
    }
    let output = output.context("--output is required")?;
    let mut writer = BufWriter::new(File::create(&output)?);
    serde_json::to_writer_pretty(&mut writer, &tables())?;
    use std::io::Write;
    writer.write_all(b"\n")?;
    Ok(())
}

fn tables() -> Tables {
    Tables {
        schema: SCHEMA,
        unicode_version: UNICODE_VERSION,
        tokenizer_version: TOKENIZER_VERSION,
        sigma_fold: [0x03c2, 0x03c3],
        whitespace: scalar_values()
            .filter(|character| character.is_whitespace())
            .map(|character| character as u32)
            .collect(),
        word_char_ranges: word_char_ranges(),
        lowercase_map: lowercase_map(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The checked-in table the Python baseline loads must equal what this
    /// toolchain emits. If a Rust upgrade shifts a Unicode property, this fails
    /// until `scripts/places_unicode_tables_v1.json` is regenerated — which is
    /// exactly the point: the baseline must never load stale tables.
    #[test]
    fn checked_in_tables_match_toolchain() {
        let path = concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../scripts/places_unicode_tables_v1.json"
        );
        let committed: serde_json::Value =
            serde_json::from_reader(File::open(path).unwrap()).unwrap();
        let generated: serde_json::Value = serde_json::to_value(tables()).unwrap();
        assert_eq!(
            committed, generated,
            "regenerate scripts/places_unicode_tables_v1.json with places-unicode-tables-v1"
        );
    }
}
