//! Strict range reader for compact Places spatial shards.

use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::rc::Rc;

use geocoder_core::pages::{format_uuid, ByteRange, ByteReader, PageError};
use serde::{Deserialize, Serialize};
use unicode_normalization::{char::is_combining_mark, UnicodeNormalization};
use worker::*;

use crate::range_reader::{RangeReadMetrics, RangeReader};
use crate::stac::{not_found, ShardLoader};

const MAGIC: &[u8; 8] = b"PCSH0001";
const HEAD_MAGIC: &[u8; 8] = b"PHRP0001";
const CATALOG_MAGIC: &[u8; 8] = b"PCAT0001";
const PREAMBLE_BYTES: usize = 12;
const RECORD_INDEX_BYTES: u64 = 8;
const MAX_DIRECTORY_BYTES: usize = 512 * 1024;
const MAX_LEXICON_BLOCK_BYTES: u64 = 512 * 1024;
const MAX_LEXICON_BLOCKS: usize = 32;
const MAX_LEXICON_TOTAL_BYTES: u64 = 2 * 1024 * 1024;
const MAX_LEXICON_MATCHES: usize = 4096;
const MAX_LEXICON_ENTRIES_PER_BLOCK: usize = 4096;
const MAX_TOKEN_BYTES: usize = 4096;
const MIN_PREFIX_CHARS: usize = 2;
const MAX_POSTING_BYTES: u64 = 8 * 1024 * 1024;
const MAX_POSTING_CANDIDATES: usize = 200_000;
const MAX_RESULT_RECORD_BYTES: usize = 64 * 1024;
const MAX_RESULT_RANGE_BYTES: u64 = 2 * 1024 * 1024;
// Coalesce-gap thresholds for the record_index and records stages. The records
// component is laid out in serving-rank order by the producer, so a query's
// served window is rank-local and coalesces into a single physical read at
// these gaps; the gap is the guardrail that keeps the scattered window one read
// (a two-clause query's other stages already consume 7 of the 8-read cold
// budget). These MUST equal the producer/reader-model defaults
// RECORD_INDEX_COALESCE_GAP / RECORDS_COALESCE_GAP in
// scripts/experiment_places_compact_shard.py; tests/test_places_coalesce_gap_parity.py
// fails if the two sides drift.
const RECORD_INDEX_COALESCE_GAP: u64 = 64 * 1024;
const RECORDS_COALESCE_GAP: u64 = 64 * 1024;
// Per-physical-read size cap for the record_index stage (the records stage uses
// MAX_RESULT_RANGE_BYTES). Mirrored by RECORD_INDEX_MAX_RANGE_BYTES /
// RECORDS_MAX_RANGE_BYTES in scripts/experiment_places_compact_shard.py and
// pinned by the same parity test, so the offline model plans the same physical
// reads this Worker issues.
const RECORD_INDEX_MAX_RANGE_BYTES: u64 = 256 * 1024;
// The served window: the routed handler returns at most this many results
// (handlers.rs `results.truncate(10)`) and does not re-rank the shard's
// confidence/doc-id ordering, so fetching more record_index/records than this
// only pulls bytes the handler discards. Kept equal to the served count so the
// dominant cold-read stage (records) fetches exactly what is returned.
const RESULT_LIMIT: usize = 10;
const HEAD_RESULT_LIMIT: usize = 10;
const MAX_QUERY_CLAUSES: usize = 4;
const MAX_QUERY_POSTING_BYTES: u64 = 16 * 1024 * 1024;
const MAX_HEAD_INDEX_BYTES: u64 = 1024 * 1024;
const MAX_HEAD_KEYS: usize = 100_000;
const MAX_HEAD_ENTRY_BYTES: u64 = 128 * 1024;
const MAX_CATALOG_BYTES: usize = 2 * 1024 * 1024;
pub(crate) const MAX_CATALOG_OBJECT_BYTES: usize = PREAMBLE_BYTES + MAX_CATALOG_BYTES;
const MAX_CATALOG_SHARDS: usize = 32_768;
const PLACES_CATALOG_CACHE_MAX_ENTRIES: usize = 1;
const MAX_QUADKEY_LEVEL: usize = 15;
const PARTITION_SCHEME: &str = "world-quadkey-v1";
pub(crate) const TOKENIZER_VERSION: &str = "nfkd-lower-stripmark-cjk-bigram-v4";
const LEGACY_TOKENIZER_VERSION: &str = "nfkd-latin-fold-cjk-bigram-v2";

thread_local! {
    /// Prepared immutable catalog objects, LRU-last. Routing indexes can be
    /// large at planet scale, so keep only the live roll-forward generation;
    /// an in-flight request keeps its own `Rc` alive during replacement.
    static PLACES_CATALOG_CACHE: RefCell<Vec<(String, Rc<PreparedPlacesCatalog>)>> =
        const { RefCell::new(Vec::new()) };
}

fn supported_tokenizer(value: &str) -> bool {
    matches!(value, TOKENIZER_VERSION | LEGACY_TOKENIZER_VERSION)
}

#[cfg(test)]
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

/// Fold a Greek final sigma (U+03C2) to a plain lowercase sigma (U+03C3) so a
/// lowercase Greek query matches the context-free `σ` held in the index. This
/// mirrors the index-side fold in the `places-transform-v1` tokenizer
/// (`tokenizer_version` `nfkd-lower-stripmark-cjk-bigram-v4`).
fn fold_final_sigma(character: char) -> char {
    if character == '\u{03c2}' {
        '\u{03c3}'
    } else {
        character
    }
}

fn normalized_words(value: &str) -> Vec<String> {
    // Trim Unicode `White_Space` only, NFKD-decompose, then lowercase per-char
    // so compatibility-decomposed styled capitals fold to lowercase. Kept
    // byte-for-byte identical to the authoritative `places-transform-v1`
    // tokenizer so query terms match indexed document terms.
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

/// Full document tokenizer shared with the global v3 producer contract.
#[cfg(test)]
pub(crate) fn tokenize_query(value: &str) -> Vec<String> {
    let words = normalized_words(value);
    let mut result = Vec::new();
    let mut seen = HashSet::new();
    for word in words {
        if seen.insert(word.clone()) {
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
                if seen.insert(token.clone()) {
                    result.push(token);
                }
            } else {
                for index in start..end - 1 {
                    let token: String = characters[index..=index + 1].iter().collect();
                    if seen.insert(token.clone()) {
                        result.push(token);
                    }
                }
            }
            start = end;
        }
    }
    result
}

/// Exact query clauses use the full normalized word tokens. Those tokens are
/// always present in the document index; adding every CJK bigram as an AND
/// clause would make ordinary long CJK names exceed the four-clause read cap.
pub(crate) fn query_terms(value: &str) -> Vec<String> {
    normalized_words(value)
}

const FIELD_NAME: u8 = 1;
const FIELD_BRAND: u8 = 2;
const FIELD_CATEGORY: u8 = 4;
const FIELD_CONTEXT: u8 = 8;
const FIELD_ALL: u8 = FIELD_NAME | FIELD_BRAND | FIELD_CATEGORY | FIELD_CONTEXT;

type PageResult<T> = std::result::Result<T, PageError>;

#[derive(Debug, Deserialize)]
struct Component {
    offset: u64,
    length: u64,
}

#[derive(Debug, Deserialize)]
struct LexiconBlock {
    first: String,
    last: String,
    offset: u64,
    length: u64,
    entries: usize,
}

#[derive(Debug, Deserialize)]
struct Directory {
    schema_version: u32,
    tokenizer_version: String,
    record_count: usize,
    token_count: usize,
    lexicon_blocks: Vec<LexiconBlock>,
    field_bits: HashMap<String, u8>,
    components: HashMap<String, Component>,
}

#[derive(Debug, Deserialize)]
struct HeadDirectory {
    schema_version: u32,
    key_count: usize,
    /// Additive famous-unique provenance (schema_version stays 1): the number
    /// of `e2:` pair keys and the admission-rule marker. Objects built before
    /// famous admission omit both; an unknown marker fails closed.
    #[serde(default)]
    e2_key_count: usize,
    #[serde(default)]
    admission: Option<String>,
    components: HashMap<String, Component>,
}

const HEAD_ADMISSION_MARKER: &str = "famous-unique-v1";

fn head_directory_supported(directory: &HeadDirectory) -> bool {
    directory.schema_version == 1
        && directory.key_count > 0
        && directory.key_count <= MAX_HEAD_KEYS
        && directory.e2_key_count <= directory.key_count
        && matches!(
            directory.admission.as_deref(),
            None | Some(HEAD_ADMISSION_MARKER)
        )
        // Pair keys without a declared admission rule are undeclared
        // provenance: fail closed rather than serve them silently.
        && (directory.e2_key_count == 0 || directory.admission.is_some())
}

#[derive(Debug)]
struct LexiconEntry {
    token: String,
    posting_offset: u64,
    posting_length: u64,
    posting_count: usize,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct PlaceProjection {
    pub id: String,
    pub latitude: f32,
    pub longitude: f32,
    pub confidence: f32,
    pub name: String,
    pub category: String,
    pub locality: String,
    pub region: String,
    pub country: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub distance_km: Option<f64>,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct PlacesClause {
    pub token: String,
    pub prefix: bool,
    pub field: Option<String>,
    #[serde(skip)]
    field_mask: u8,
}

impl PlacesClause {
    pub(crate) fn new(token: String, prefix: bool, field: Option<String>) -> Result<Self> {
        if token.is_empty()
            || token.len() > MAX_TOKEN_BYTES
            || (prefix && token.chars().count() < MIN_PREFIX_CHARS)
        {
            return Err(Error::RustError(
                "Places lookup token is outside hard bounds".into(),
            ));
        }
        let field_mask = match field.as_deref() {
            None => FIELD_ALL,
            Some("name") => FIELD_NAME,
            Some("brand") => FIELD_BRAND,
            Some("category") => FIELD_CATEGORY,
            Some("context") => FIELD_CONTEXT,
            Some(_) => {
                return Err(Error::RustError(
                    "Places lookup field is unsupported".into(),
                ))
            }
        };
        Ok(Self {
            token,
            prefix,
            field,
            field_mask,
        })
    }

    pub(crate) fn head_eligible(&self) -> bool {
        self.field.is_none() && !self.prefix
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub(crate) struct PlacesCatalogShard {
    pub id: String,
    pub object: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cell: Option<String>,
    pub bbox: [f64; 4],
    pub center: [f64; 2],
}

#[derive(Debug, Deserialize, Serialize)]
struct PlacesPartition {
    scheme: String,
    minimum_level: usize,
    maximum_level: usize,
    split_row_cap: usize,
    split_cells: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize)]
pub(crate) struct PlacesCatalog {
    schema_version: u32,
    tokenizer_version: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    coverage: Option<[f64; 4]>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    partition: Option<PlacesPartition>,
    pub shards: Vec<PlacesCatalogShard>,
}

impl PlacesCatalog {
    pub(crate) fn route_point(&self, longitude: f64, latitude: f64) -> Option<&PlacesCatalogShard> {
        if self.schema_version == 2 {
            if !longitude.is_finite() || !latitude.is_finite() {
                return None;
            }
            let coverage = self.coverage.as_ref()?;
            if longitude < coverage[0]
                || longitude > coverage[2]
                || latitude < coverage[1]
                || latitude > coverage[3]
            {
                return None;
            }
            let maximum_level = self.partition.as_ref()?.maximum_level;
            let point_cell = point_quadkey(longitude, latitude, maximum_level)?;
            return self
                .shards
                .iter()
                .filter(|shard| {
                    shard
                        .cell
                        .as_deref()
                        .is_some_and(|cell| point_cell.starts_with(cell))
                })
                .max_by_key(|shard| shard.cell.as_ref().map_or(0, String::len));
        }
        self.shards
            .iter()
            .filter(|shard| {
                shard.bbox[0] <= longitude
                    && longitude <= shard.bbox[2]
                    && shard.bbox[1] <= latitude
                    && latitude <= shard.bbox[3]
            })
            .min_by(|left, right| {
                let left_area = (left.bbox[2] - left.bbox[0]) * (left.bbox[3] - left.bbox[1]);
                let right_area = (right.bbox[2] - right.bbox[0]) * (right.bbox[3] - right.bbox[1]);
                left_area
                    .total_cmp(&right_area)
                    .then(left.id.cmp(&right.id))
            })
    }

    fn supported(&self) -> bool {
        if !supported_tokenizer(&self.tokenizer_version)
            || self.shards.is_empty()
            || self.shards.len() > MAX_CATALOG_SHARDS
        {
            return false;
        }
        let mut ids = HashSet::new();
        for shard in &self.shards {
            let valid_id = !shard.id.is_empty()
                && shard.id.len() <= 64
                && shard
                    .id
                    .chars()
                    .all(|character| character.is_ascii_alphanumeric() || character == '-');
            let valid_object = shard.object.ends_with(".pcsh")
                && shard.object.len() <= 128
                && shard.object.chars().all(|character| {
                    character.is_ascii_alphanumeric() || matches!(character, '-' | '_' | '.')
                });
            if !valid_id
                || !valid_object
                || !valid_geometry(shard)
                || !ids.insert(shard.id.as_str())
            {
                return false;
            }
        }
        match self.schema_version {
            1 => {
                self.coverage.is_none()
                    && self.partition.is_none()
                    && self.shards.iter().all(|shard| shard.cell.is_none())
            }
            2 => self.supported_spatial_partition(),
            _ => false,
        }
    }

    fn supported_spatial_partition(&self) -> bool {
        let (Some(coverage), Some(partition)) = (&self.coverage, &self.partition) else {
            return false;
        };
        if !valid_bbox(coverage)
            || coverage[0] >= coverage[2]
            || coverage[1] >= coverage[3]
            || partition.scheme != PARTITION_SCHEME
            || partition.minimum_level == 0
            || partition.minimum_level > partition.maximum_level
            || partition.maximum_level > MAX_QUADKEY_LEVEL
            || partition.split_row_cap == 0
        {
            return false;
        }
        let split_cells: HashSet<&str> = partition.split_cells.iter().map(String::as_str).collect();
        if split_cells.len() != partition.split_cells.len()
            || partition.split_cells.iter().any(|cell| {
                !valid_quadkey(
                    cell,
                    partition.minimum_level,
                    partition.maximum_level.saturating_sub(1),
                ) || (cell.len() > partition.minimum_level
                    && !split_cells.contains(&cell[..cell.len() - 1]))
            })
        {
            return false;
        }
        let mut leaf_cells = Vec::with_capacity(self.shards.len());
        for shard in &self.shards {
            let Some(cell) = shard.cell.as_deref() else {
                return false;
            };
            if !valid_quadkey(cell, partition.minimum_level, partition.maximum_level)
                || shard.id != format!("q-{cell}")
                || shard.object != format!("q-{cell}.pcsh")
                || split_cells.contains(cell)
                || (cell.len() > partition.minimum_level
                    && !split_cells.contains(&cell[..cell.len() - 1]))
            {
                return false;
            }
            let Some(bbox) = quadkey_bbox(cell) else {
                return false;
            };
            let center = [(bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0];
            if shard.bbox != bbox || shard.center != center {
                return false;
            }
            leaf_cells.push(cell);
        }
        leaf_cells.sort_unstable();
        !leaf_cells
            .windows(2)
            .any(|pair| pair[1].starts_with(pair[0]))
    }
}

struct PreparedPlacesCatalog {
    catalog: PlacesCatalog,
    spatial_routes: HashMap<String, usize>,
}

impl PreparedPlacesCatalog {
    fn new(catalog: PlacesCatalog) -> Self {
        let spatial_routes = if catalog.schema_version == 2 {
            catalog
                .shards
                .iter()
                .enumerate()
                .map(|(index, shard)| {
                    (
                        shard.cell.clone().expect("validated spatial shard cell"),
                        index,
                    )
                })
                .collect()
        } else {
            HashMap::new()
        };
        Self {
            catalog,
            spatial_routes,
        }
    }

    fn route_point(&self, longitude: f64, latitude: f64) -> Option<&PlacesCatalogShard> {
        if self.catalog.schema_version != 2 {
            return self.catalog.route_point(longitude, latitude);
        }
        if !longitude.is_finite() || !latitude.is_finite() {
            return None;
        }
        let coverage = self.catalog.coverage.as_ref()?;
        if longitude < coverage[0]
            || longitude > coverage[2]
            || latitude < coverage[1]
            || latitude > coverage[3]
        {
            return None;
        }
        let partition = self.catalog.partition.as_ref()?;
        let point_cell = point_quadkey(longitude, latitude, partition.maximum_level)?;
        (partition.minimum_level..=partition.maximum_level)
            .rev()
            .find_map(|length| {
                self.spatial_routes
                    .get(&point_cell[..length])
                    .map(|index| &self.catalog.shards[*index])
            })
    }
}

fn valid_bbox(bbox: &[f64; 4]) -> bool {
    let [xmin, ymin, xmax, ymax] = *bbox;
    bbox.iter().all(|value| value.is_finite())
        && (-180.0..=180.0).contains(&xmin)
        && (-180.0..=180.0).contains(&xmax)
        && (-90.0..=90.0).contains(&ymin)
        && (-90.0..=90.0).contains(&ymax)
        && xmin <= xmax
        && ymin <= ymax
}

fn valid_geometry(shard: &PlacesCatalogShard) -> bool {
    valid_bbox(&shard.bbox)
        && shard.center.iter().all(|value| value.is_finite())
        && (shard.bbox[0]..=shard.bbox[2]).contains(&shard.center[0])
        && (shard.bbox[1]..=shard.bbox[3]).contains(&shard.center[1])
}

fn valid_quadkey(cell: &str, minimum_level: usize, maximum_level: usize) -> bool {
    minimum_level <= cell.len()
        && cell.len() <= maximum_level
        && cell.bytes().all(|digit| (b'0'..=b'3').contains(&digit))
}

pub(crate) fn point_quadkey(longitude: f64, latitude: f64, level: usize) -> Option<String> {
    if level == 0
        || level > MAX_QUADKEY_LEVEL
        || !(-180.0..=180.0).contains(&longitude)
        || !(-90.0..=90.0).contains(&latitude)
    {
        return None;
    }
    let size = 1_u32 << level;
    let x = (((longitude + 180.0) / 360.0 * f64::from(size)).floor() as i64)
        .clamp(0, i64::from(size - 1)) as u32;
    let y = (((latitude + 90.0) / 180.0 * f64::from(size)).floor() as i64)
        .clamp(0, i64::from(size - 1)) as u32;
    let mut result = String::with_capacity(level);
    for bit in (0..level).rev() {
        let digit = (((y >> bit) & 1) << 1) | ((x >> bit) & 1);
        result.push(char::from(b'0' + digit as u8));
    }
    Some(result)
}

fn quadkey_bbox(cell: &str) -> Option<[f64; 4]> {
    if !valid_quadkey(cell, 1, MAX_QUADKEY_LEVEL) {
        return None;
    }
    let mut x = 0_u32;
    let mut y = 0_u32;
    for digit in cell.bytes().map(|value| value - b'0') {
        x = (x << 1) | u32::from(digit & 1);
        y = (y << 1) | u32::from((digit >> 1) & 1);
    }
    let size = 1_u32 << cell.len();
    let width = 360.0 / f64::from(size);
    let height = 180.0 / f64::from(size);
    Some([
        -180.0 + f64::from(x) * width,
        -90.0 + f64::from(y) * height,
        -180.0 + f64::from(x + 1) * width,
        -90.0 + f64::from(y + 1) * height,
    ])
}

pub(crate) struct PlacesCatalogLookup {
    catalog: Rc<PreparedPlacesCatalog>,
}

impl PlacesCatalogLookup {
    pub(crate) fn route_point(&self, longitude: f64, latitude: f64) -> Option<&PlacesCatalogShard> {
        self.catalog.route_point(longitude, latitude)
    }
}

#[derive(Debug, Serialize)]
pub(crate) struct PlacesReadStages {
    pub directory: RangeReadMetrics,
    pub lexicon: RangeReadMetrics,
    pub postings: RangeReadMetrics,
    pub record_index: RangeReadMetrics,
    pub records: RangeReadMetrics,
}

#[derive(Debug, Serialize)]
pub(crate) struct PlacesShardLookup {
    pub candidate_count: usize,
    /// DIAGNOSTIC, not candidate recall: entry `i` is the number of candidate
    /// docs actually decoded for clause `i`, or `None` (JSON `null`) for a
    /// clause whose postings were never read because the lookup exited early
    /// (any clause without a lexicon match skips all posting reads; an emptied
    /// running intersection stops the clause loop). The Python oracle
    /// (`experiment_places_compact_shard.CompactShard.query`) mirrors these
    /// rules exactly and must produce identical values for every case.
    pub clause_candidate_counts: Vec<Option<usize>>,
    pub results: Vec<PlaceProjection>,
    pub read_metrics: RangeReadMetrics,
    pub stages: PlacesReadStages,
    pub tokenizer_version: String,
}

#[derive(Debug, Serialize)]
pub(crate) struct PlacesHeadLookup {
    pub hit: bool,
    pub results: Vec<PlaceProjection>,
    pub read_metrics: RangeReadMetrics,
    pub directory_metrics: RangeReadMetrics,
    pub index_metrics: RangeReadMetrics,
    pub entry_metrics: RangeReadMetrics,
}

fn component<'a>(directory: &'a Directory, name: &str) -> Result<&'a Component> {
    directory
        .components
        .get(name)
        .ok_or_else(|| Error::RustError(format!("Places directory omits {name}")))
}

fn head_component<'a>(directory: &'a HeadDirectory, name: &str) -> Result<&'a Component> {
    directory
        .components
        .get(name)
        .ok_or_else(|| Error::RustError(format!("Places head directory omits {name}")))
}

fn checked_extent(base: u64, offset: u64, length: u64, component_len: u64) -> Result<ByteRange> {
    let relative_end = offset
        .checked_add(length)
        .ok_or_else(|| Error::RustError("Places component extent overflows".into()))?;
    if relative_end > component_len || length == 0 {
        return Err(Error::RustError(
            "Places component extent is outside hard bounds".into(),
        ));
    }
    Ok(ByteRange {
        offset: base
            .checked_add(offset)
            .ok_or_else(|| Error::RustError("Places absolute extent overflows".into()))?,
        length,
    })
}

fn decode_lexicon_block(bytes: &[u8], expected_entries: usize) -> PageResult<Vec<LexiconEntry>> {
    let mut reader = ByteReader::new(bytes, 0);
    let count = usize::try_from(reader.uvarint()?)
        .map_err(|_| PageError::new("Places lexicon entry count is too large"))?;
    if count != expected_entries || count > MAX_LEXICON_ENTRIES_PER_BLOCK {
        return Err(PageError::new(
            "Places lexicon entry count is outside hard bounds",
        ));
    }
    let mut previous = Vec::new();
    let mut previous_token: Option<String> = None;
    let mut entries = Vec::with_capacity(count);
    for _ in 0..count {
        reader.apply_front_coding(&mut previous, MAX_TOKEN_BYTES)?;
        let token = String::from_utf8(previous.clone())
            .map_err(|_| PageError::new("Places lexicon token is not UTF-8"))?;
        if previous_token.as_ref().is_some_and(|old| token <= *old) {
            return Err(PageError::new("Places lexicon tokens are not sorted"));
        }
        let posting_offset = reader.uvarint()?;
        let posting_length = reader.uvarint()?;
        let posting_count = usize::try_from(reader.uvarint()?)
            .map_err(|_| PageError::new("Places posting count is too large"))?;
        if posting_length == 0 || posting_count == 0 {
            return Err(PageError::new("Places posting extent is empty"));
        }
        entries.push(LexiconEntry {
            token: token.clone(),
            posting_offset,
            posting_length,
            posting_count,
        });
        previous_token = Some(token);
    }
    if !reader.is_empty() {
        return Err(PageError::new("Places lexicon block has trailing bytes"));
    }
    Ok(entries)
}

fn select_lexicon_blocks<'a>(
    directory: &'a Directory,
    token: &str,
    prefix: bool,
) -> Result<Vec<&'a LexiconBlock>> {
    if token.is_empty()
        || token.len() > MAX_TOKEN_BYTES
        || (prefix && token.chars().count() < MIN_PREFIX_CHARS)
    {
        return Err(Error::RustError(
            "Places lookup token is outside hard bounds".into(),
        ));
    }
    let upper = format!("{token}\u{10ffff}");
    let mut selected = Vec::new();
    let mut total_bytes = 0_u64;
    for block in &directory.lexicon_blocks {
        let overlaps = if prefix {
            block.last.as_str() >= token && block.first <= upper
        } else {
            block.first.as_str() <= token && token <= block.last.as_str()
        };
        if !overlaps {
            continue;
        }
        if block.length == 0
            || block.length > MAX_LEXICON_BLOCK_BYTES
            || block.entries == 0
            || block.entries > MAX_LEXICON_ENTRIES_PER_BLOCK
        {
            return Err(Error::RustError(
                "Places lexicon block exceeds hard cap".into(),
            ));
        }
        if selected.len() >= MAX_LEXICON_BLOCKS {
            return Err(Error::RustError(
                "Places lexicon block selection exceeds hard cap".into(),
            ));
        }
        total_bytes = total_bytes
            .checked_add(block.length)
            .ok_or_else(|| Error::RustError("Places lexicon byte total overflows".into()))?;
        if total_bytes > MAX_LEXICON_TOTAL_BYTES {
            return Err(Error::RustError(
                "Places lexicon byte total exceeds hard cap".into(),
            ));
        }
        selected.push(block);
    }
    Ok(selected)
}

fn decode_postings(bytes: &[u8], count: usize) -> PageResult<Vec<(u64, u8, u8)>> {
    if count > MAX_POSTING_CANDIDATES {
        return Err(PageError::new("Places posting count exceeds hard cap"));
    }
    let mut reader = ByteReader::new(bytes, 0);
    let mut previous = 0_u64;
    let mut result = Vec::with_capacity(count);
    for index in 0..count {
        let delta = reader.uvarint()?;
        let doc_id = if index == 0 {
            delta
        } else {
            previous
                .checked_add(delta)
                .ok_or_else(|| PageError::new("Places document ID overflows"))?
        };
        if index > 0 && doc_id <= previous {
            return Err(PageError::new("Places posting IDs are not increasing"));
        }
        let pair = reader.take(2)?;
        if pair[0] == 0 || pair[0] & !FIELD_ALL != 0 {
            return Err(PageError::new("Places posting field mask is invalid"));
        }
        result.push((doc_id, pair[0], pair[1]));
        previous = doc_id;
    }
    if !reader.is_empty() {
        return Err(PageError::new("Places posting list has trailing bytes"));
    }
    Ok(result)
}

fn decode_projection(bytes: &[u8]) -> PageResult<PlaceProjection> {
    if bytes.len() > MAX_RESULT_RECORD_BYTES || bytes.len() < 10 {
        return Err(PageError::new("Places projection is outside hard bounds"));
    }
    let mut reader = ByteReader::new(bytes, 0);
    let latitude = f32::from_le_bytes(reader.take(4)?.try_into().expect("four-byte slice"));
    let longitude = f32::from_le_bytes(reader.take(4)?.try_into().expect("four-byte slice"));
    if !latitude.is_finite()
        || !longitude.is_finite()
        || !(-90.0..=90.0).contains(&latitude)
        || !(-180.0..=180.0).contains(&longitude)
    {
        return Err(PageError::new(
            "Places coordinates are outside valid bounds",
        ));
    }
    let confidence = f32::from(*reader.take(1)?.first().expect("one-byte slice")) / 255.0;
    let identity_kind = *reader.take(1)?.first().expect("one-byte slice");
    let id = match identity_kind {
        0 => reader.text(MAX_TOKEN_BYTES)?,
        1 => format_uuid(reader.take(16)?.try_into().expect("sixteen-byte slice")),
        _ => return Err(PageError::new("Places identity encoding is unsupported")),
    };
    let name = reader.text(MAX_TOKEN_BYTES)?;
    let category = reader.text(MAX_TOKEN_BYTES)?;
    let locality = reader.text(MAX_TOKEN_BYTES)?;
    let region = reader.text(MAX_TOKEN_BYTES)?;
    let country = reader.text(MAX_TOKEN_BYTES)?;
    if !reader.is_empty() {
        return Err(PageError::new("Places projection has trailing bytes"));
    }
    Ok(PlaceProjection {
        id,
        latitude,
        longitude,
        confidence,
        name,
        category,
        locality,
        region,
        country,
        distance_km: None,
    })
}

fn decode_head_projection(bytes: &[u8]) -> PageResult<PlaceProjection> {
    if bytes.len() > MAX_RESULT_RECORD_BYTES || bytes.len() < 13 {
        return Err(PageError::new(
            "Places head projection is outside hard bounds",
        ));
    }
    let mut reader = ByteReader::new(bytes, 0);
    let latitude = f32::from_le_bytes(reader.take(4)?.try_into().expect("four-byte slice"));
    let longitude = f32::from_le_bytes(reader.take(4)?.try_into().expect("four-byte slice"));
    let confidence = f32::from_le_bytes(reader.take(4)?.try_into().expect("four-byte slice"));
    if !latitude.is_finite()
        || !longitude.is_finite()
        || !confidence.is_finite()
        || !(-90.0..=90.0).contains(&latitude)
        || !(-180.0..=180.0).contains(&longitude)
    {
        return Err(PageError::new(
            "Places head values are outside valid bounds",
        ));
    }
    let identity_kind = *reader.take(1)?.first().expect("one-byte slice");
    let id = match identity_kind {
        0 => reader.text(MAX_TOKEN_BYTES)?,
        1 => format_uuid(reader.take(16)?.try_into().expect("sixteen-byte slice")),
        _ => {
            return Err(PageError::new(
                "Places head identity encoding is unsupported",
            ))
        }
    };
    let name = reader.text(MAX_TOKEN_BYTES)?;
    let _brand = reader.text(MAX_TOKEN_BYTES)?;
    let category = reader.text(MAX_TOKEN_BYTES)?;
    let locality = reader.text(MAX_TOKEN_BYTES)?;
    let region = reader.text(MAX_TOKEN_BYTES)?;
    let country = reader.text(MAX_TOKEN_BYTES)?;
    if !reader.is_empty() {
        return Err(PageError::new("Places head projection has trailing bytes"));
    }
    Ok(PlaceProjection {
        id,
        latitude,
        longitude,
        confidence,
        name,
        category,
        locality,
        region,
        country,
        distance_km: None,
    })
}

/// The `e2:` famous-pair key for a two-clause head-eligible query.
///
/// The two tokens are joined in ascending byte order, matching the builder's
/// `a < b` pair emission. Any other clause shape — or two identical tokens,
/// which the builder never emits — has no pair key. The Python smoke oracle
/// (`prepare_places_worker_smoke.py::query_head` via `famous_pair_key`)
/// constructs the identical key; producer-oracle equality enforces lockstep.
fn famous_pair_key(clauses: &[PlacesClause]) -> Option<String> {
    let [first, second] = clauses else {
        return None;
    };
    let (low, high) = if first.token <= second.token {
        (&first.token, &second.token)
    } else {
        (&second.token, &first.token)
    };
    if low == high {
        return None;
    }
    Some(format!("e2:{low} {high}"))
}

fn find_head_entry(bytes: &[u8], key: &str) -> PageResult<Option<(u64, u64)>> {
    let mut reader = ByteReader::new(bytes, 0);
    let mut previous = String::new();
    let mut count = 0_usize;
    while !reader.is_empty() {
        count += 1;
        if count > MAX_HEAD_KEYS {
            return Err(PageError::new("Places head key count exceeds hard cap"));
        }
        let candidate = reader.text(MAX_TOKEN_BYTES)?;
        if candidate < previous {
            return Err(PageError::new("Places head keys are not sorted"));
        }
        let offset = reader.uvarint()?;
        let length = reader.uvarint()?;
        if length == 0 || length > MAX_HEAD_ENTRY_BYTES {
            return Err(PageError::new("Places head entry is outside hard bounds"));
        }
        if candidate == key {
            return Ok(Some((offset, length)));
        }
        if candidate.as_str() > key {
            return Ok(None);
        }
        previous = candidate;
    }
    Ok(None)
}

/// Union the per-entry posting chunks of one clause into its candidate map.
///
/// `chunks[i]` must hold exactly the posting bytes of `matches[i]` — the
/// per-want slices returned by `RangeReader::coalesced` for one want per
/// matched lexicon entry — regardless of how the physical reads were merged or
/// split. Occurrences whose field mask misses `field_mask` are skipped; ranks
/// keep the per-doc maximum. Fails closed on a `matches`/`chunks` length
/// mismatch (a mis-zip would silently drop entries) and on the candidate cap.
fn union_clause_postings<B: AsRef<[u8]>>(
    matches: &[LexiconEntry],
    chunks: &[B],
    field_mask: u8,
) -> Result<BTreeMap<u64, u8>> {
    if matches.len() != chunks.len() {
        return Err(Error::RustError(
            "Places posting chunks do not align with lexicon matches".into(),
        ));
    }
    let mut clause_docs: BTreeMap<u64, u8> = BTreeMap::new();
    for (entry, encoded) in matches.iter().zip(chunks) {
        for (doc_id, occurrence_mask, rank) in
            decode_postings(encoded.as_ref(), entry.posting_count)
                .map_err(|error| Error::RustError(format!("Invalid Places posting: {error}")))?
        {
            if occurrence_mask & field_mask == 0 {
                continue;
            }
            clause_docs
                .entry(doc_id)
                .and_modify(|old| *old = (*old).max(rank))
                .or_insert(rank);
            if clause_docs.len() > MAX_POSTING_CANDIDATES {
                return Err(Error::RustError(
                    "Places union candidate count exceeds hard cap".into(),
                ));
            }
        }
    }
    Ok(clause_docs)
}

fn decode_head_entry(bytes: &[u8]) -> PageResult<Vec<PlaceProjection>> {
    let mut reader = ByteReader::new(bytes, 0);
    let mut results = Vec::new();
    while !reader.is_empty() {
        if results.len() >= HEAD_RESULT_LIMIT {
            return Err(PageError::new("Places head result count exceeds hard cap"));
        }
        let length = usize::try_from(reader.uvarint()?)
            .map_err(|_| PageError::new("Places head result length is too large"))?;
        results.push(decode_head_projection(reader.take(length)?)?);
    }
    Ok(results)
}

impl ShardLoader {
    pub(crate) async fn lookup_places_catalog(
        &self,
        object_key: &str,
    ) -> Result<PlacesCatalogLookup> {
        let cached = PLACES_CATALOG_CACHE.with(|cache| {
            let mut cache = cache.borrow_mut();
            cache
                .iter()
                .position(|(cached_key, _)| cached_key == object_key)
                .map(|position| {
                    let entry = cache.remove(position);
                    let catalog = Rc::clone(&entry.1);
                    cache.push(entry);
                    catalog
                })
        });
        if let Some(catalog) = cached {
            return Ok(PlacesCatalogLookup { catalog });
        }

        let mut reader = RangeReader::new(self, object_key);
        let preamble = reader
            .range(0, PREAMBLE_BYTES as u64)
            .await?
            .ok_or_else(|| not_found(object_key))?;
        if preamble.len() != PREAMBLE_BYTES || &preamble[..8] != CATALOG_MAGIC {
            return Err(Error::RustError("Invalid Places catalog preamble".into()));
        }
        let payload_length =
            u32::from_le_bytes(preamble[8..12].try_into().expect("four-byte slice")) as usize;
        if payload_length == 0 || payload_length > MAX_CATALOG_BYTES {
            return Err(Error::RustError(
                "Places catalog payload is outside hard bounds".into(),
            ));
        }
        let payload = reader
            .range(PREAMBLE_BYTES as u64, payload_length as u64)
            .await?
            .ok_or_else(|| not_found(object_key))?;
        let catalog: PlacesCatalog = serde_json::from_slice(&payload)
            .map_err(|_| Error::RustError("Invalid Places catalog JSON".into()))?;
        if !catalog.supported() {
            return Err(Error::RustError(
                "Unsupported Places catalog contract".into(),
            ));
        }
        let catalog = Rc::new(PreparedPlacesCatalog::new(catalog));
        PLACES_CATALOG_CACHE.with(|cache| {
            let mut cache = cache.borrow_mut();
            if !cache.iter().any(|(cached_key, _)| cached_key == object_key) {
                cache.push((object_key.to_string(), Rc::clone(&catalog)));
                while cache.len() > PLACES_CATALOG_CACHE_MAX_ENTRIES {
                    cache.remove(0);
                }
            }
        });
        Ok(PlacesCatalogLookup { catalog })
    }

    pub(crate) async fn lookup_places_head(
        &self,
        object_key: &str,
        clauses: &[PlacesClause],
    ) -> Result<PlacesHeadLookup> {
        if clauses.is_empty()
            || clauses.len() > MAX_QUERY_CLAUSES
            || clauses.iter().any(|clause| !clause.head_eligible())
        {
            return Err(Error::RustError(
                "Places head query is outside its eligibility contract".into(),
            ));
        }
        let mut reader = RangeReader::new(self, object_key);
        let preamble = reader
            .range(0, PREAMBLE_BYTES as u64)
            .await?
            .ok_or_else(|| not_found(object_key))?;
        if preamble.len() != PREAMBLE_BYTES || &preamble[..8] != HEAD_MAGIC {
            return Err(Error::RustError("Invalid Places head preamble".into()));
        }
        let directory_length =
            u32::from_le_bytes(preamble[8..12].try_into().expect("four-byte slice")) as usize;
        if directory_length == 0 || directory_length > MAX_DIRECTORY_BYTES {
            return Err(Error::RustError(
                "Places head directory is outside hard bounds".into(),
            ));
        }
        let directory_bytes = reader
            .range(PREAMBLE_BYTES as u64, directory_length as u64)
            .await?
            .ok_or_else(|| not_found(object_key))?;
        let directory: HeadDirectory = serde_json::from_slice(&directory_bytes)
            .map_err(|_| Error::RustError("Invalid Places head directory JSON".into()))?;
        if !head_directory_supported(&directory) {
            return Err(Error::RustError(
                "Unsupported Places head directory contract".into(),
            ));
        }
        let after_directory = reader.metrics();
        let key_index = head_component(&directory, "key_index")?;
        if key_index.length > MAX_HEAD_INDEX_BYTES {
            return Err(Error::RustError(
                "Places head key index exceeds hard cap".into(),
            ));
        }
        let index_bytes = reader
            .range(key_index.offset, key_index.length)
            .await?
            .ok_or_else(|| not_found(object_key))?;
        // Probe order matches the Python smoke oracle: a two-clause query
        // first probes the famous `e2:` pair entry, which is by construction
        // the bounded top-k of the exact posting AND; a pair hit is served as
        // the single decoded entry. A pair miss falls back to the per-token
        // entries and their stable ID intersection, unchanged.
        let mut located = Vec::with_capacity(clauses.len());
        if let Some(pair_key) = famous_pair_key(clauses) {
            if let Some(extent) = find_head_entry(&index_bytes, &pair_key)
                .map_err(|error| Error::RustError(format!("Invalid Places head index: {error}")))?
            {
                located.push(extent);
            }
        }
        if located.is_empty() {
            for clause in clauses {
                let head_key = format!("e:{}", clause.token);
                let Some(extent) = find_head_entry(&index_bytes, &head_key).map_err(|error| {
                    Error::RustError(format!("Invalid Places head index: {error}"))
                })?
                else {
                    let after_index = reader.metrics();
                    return Ok(PlacesHeadLookup {
                        hit: false,
                        results: Vec::new(),
                        read_metrics: after_index,
                        directory_metrics: after_directory,
                        index_metrics: after_index.since(after_directory),
                        entry_metrics: RangeReadMetrics::default(),
                    });
                };
                located.push(extent);
            }
        }
        let after_index = reader.metrics();
        let entries = head_component(&directory, "entries")?;
        let wants = located
            .into_iter()
            .map(|(offset, length)| checked_extent(entries.offset, offset, length, entries.length))
            .collect::<Result<Vec<_>>>()?;
        let chunks = reader.coalesced(&wants, 64 * 1024, 512 * 1024).await?;
        let mut per_clause = chunks
            .iter()
            .map(|bytes| {
                decode_head_entry(bytes).map_err(|error| {
                    Error::RustError(format!("Invalid Places head entry: {error}"))
                })
            })
            .collect::<Result<Vec<_>>>()?;
        let mut results = per_clause.remove(0);
        for clause_results in per_clause {
            let ids: HashSet<_> = clause_results
                .iter()
                .map(|place| place.id.as_str())
                .collect();
            results.retain(|place| ids.contains(place.id.as_str()));
        }
        results.truncate(HEAD_RESULT_LIMIT);
        let after_entry = reader.metrics();
        Ok(PlacesHeadLookup {
            hit: !results.is_empty(),
            results,
            read_metrics: after_entry,
            directory_metrics: after_directory,
            index_metrics: after_index.since(after_directory),
            entry_metrics: after_entry.since(after_index),
        })
    }

    pub(crate) async fn lookup_places_shard(
        &self,
        key: &str,
        clauses: &[PlacesClause],
    ) -> Result<PlacesShardLookup> {
        if clauses.is_empty() || clauses.len() > MAX_QUERY_CLAUSES {
            return Err(Error::RustError(
                "Places clause count is outside hard bounds".into(),
            ));
        }
        let mut reader = RangeReader::new(self, key);
        let preamble = reader
            .range(0, PREAMBLE_BYTES as u64)
            .await?
            .ok_or_else(|| not_found(key))?;
        if preamble.len() != PREAMBLE_BYTES || &preamble[..8] != MAGIC {
            return Err(Error::RustError("Invalid Places shard preamble".into()));
        }
        let directory_length =
            u32::from_le_bytes(preamble[8..12].try_into().expect("four-byte slice")) as usize;
        if directory_length == 0 || directory_length > MAX_DIRECTORY_BYTES {
            return Err(Error::RustError(
                "Places directory length is outside hard bounds".into(),
            ));
        }
        let directory_bytes = reader
            .range(PREAMBLE_BYTES as u64, directory_length as u64)
            .await?
            .ok_or_else(|| not_found(key))?;
        let directory: Directory = serde_json::from_slice(&directory_bytes)
            .map_err(|_| Error::RustError("Invalid Places directory JSON".into()))?;
        if directory.schema_version != 1
            || !supported_tokenizer(&directory.tokenizer_version)
            || directory.record_count == 0
            || directory.token_count == 0
            || directory.lexicon_blocks.is_empty()
            || directory.field_bits.get("name") != Some(&FIELD_NAME)
            || directory.field_bits.get("brand") != Some(&FIELD_BRAND)
            || directory.field_bits.get("category") != Some(&FIELD_CATEGORY)
            || directory.field_bits.get("context") != Some(&FIELD_CONTEXT)
        {
            return Err(Error::RustError(
                "Unsupported Places directory contract".into(),
            ));
        }
        let after_directory = reader.metrics();

        let lexicon = component(&directory, "lexicon")?;
        let mut clause_matches = Vec::with_capacity(clauses.len());
        for clause in clauses {
            let blocks = select_lexicon_blocks(&directory, &clause.token, clause.prefix)?;
            let wants: Vec<_> = blocks
                .iter()
                .map(|block| {
                    checked_extent(lexicon.offset, block.offset, block.length, lexicon.length)
                })
                .collect::<Result<_>>()?;
            let chunks = reader.coalesced(&wants, 0, MAX_LEXICON_BLOCK_BYTES).await?;
            let mut matches = Vec::new();
            for (block, bytes) in blocks.iter().zip(chunks) {
                for entry in decode_lexicon_block(&bytes, block.entries)
                    .map_err(|error| Error::RustError(format!("Invalid Places lexicon: {error}")))?
                {
                    if entry.token == clause.token
                        || (clause.prefix && entry.token.starts_with(&clause.token))
                    {
                        if matches.len() >= MAX_LEXICON_MATCHES {
                            return Err(Error::RustError(
                                "Places lexicon matches exceed hard cap".into(),
                            ));
                        }
                        matches.push(entry);
                    }
                }
            }
            matches.sort_by(|left, right| left.token.cmp(&right.token));
            clause_matches.push(matches);
        }
        let after_lexicon = reader.metrics();
        if after_lexicon.since(after_directory).planned_physical_ranges
            > MAX_LEXICON_BLOCKS.saturating_mul(clauses.len())
        {
            return Err(Error::RustError(
                "Places lexicon physical reads exceed hard cap".into(),
            ));
        }
        let postings = component(&directory, "postings")?;
        let mut total_posting_bytes = 0_u64;
        // DIAGNOSTIC, not candidate recall (see PlacesShardLookup): the length
        // is fixed to clauses.len(); a position holds Some(decoded candidate
        // count) once that clause's postings are read, and stays None for
        // clauses skipped by the early exits below. The Python oracle mirrors
        // the same skip/break rules and must report identical values.
        let mut clause_candidate_counts: Vec<Option<usize>> = vec![None; clauses.len()];
        let mut candidates: Option<BTreeMap<u64, u8>> = None;
        // A clause with no lexicon match makes the AND-intersection provably
        // empty, so no posting read can change the (empty) result: skip them all.
        let any_clause_unmatched = clause_matches.iter().any(|matches| matches.is_empty());
        if !any_clause_unmatched {
            for (position, (clause, matches)) in clauses.iter().zip(&clause_matches).enumerate() {
                // Read each matched entry's own posting range rather than the
                // single [first, last] span. Postings are stored contiguously in
                // token order, so adjacent matches coalesce into one physical
                // read at gap 0 while distant matches split — the dead bytes
                // between them are never fetched (the old span read fetched and
                // then discarded them). Slicing is per-want, so the old unchecked
                // `entry.posting_offset - posting_start` subtraction is gone too.
                let posting_wants: Vec<_> = matches
                    .iter()
                    .map(|entry| {
                        checked_extent(
                            postings.offset,
                            entry.posting_offset,
                            entry.posting_length,
                            postings.length,
                        )
                    })
                    .collect::<Result<_>>()?;
                let clause_posting_bytes = matches
                    .iter()
                    .try_fold(0_u64, |sum, entry| sum.checked_add(entry.posting_length))
                    .ok_or_else(|| Error::RustError("Places posting span overflows".into()))?;
                if clause_posting_bytes > MAX_POSTING_BYTES {
                    return Err(Error::RustError(
                        "Places posting span exceeds hard cap".into(),
                    ));
                }
                total_posting_bytes = total_posting_bytes
                    .checked_add(clause_posting_bytes)
                    .ok_or_else(|| {
                        Error::RustError("Places posting byte total overflows".into())
                    })?;
                if total_posting_bytes > MAX_QUERY_POSTING_BYTES {
                    return Err(Error::RustError(
                        "Places query posting bytes exceed hard cap".into(),
                    ));
                }
                let posting_chunks = reader
                    .coalesced(&posting_wants, 0, MAX_POSTING_BYTES)
                    .await?;
                let clause_docs =
                    union_clause_postings(matches, &posting_chunks, clause.field_mask)?;
                clause_candidate_counts[position] = Some(clause_docs.len());
                candidates = Some(match candidates {
                    None => clause_docs,
                    Some(mut prior) => {
                        prior.retain(|doc_id, rank| {
                            if let Some(clause_rank) = clause_docs.get(doc_id) {
                                *rank = (*rank).max(*clause_rank);
                                true
                            } else {
                                false
                            }
                        });
                        prior
                    }
                });
                // The intersection only shrinks; once empty no later clause can
                // revive it, so stop before issuing their posting reads.
                if matches!(&candidates, Some(docs) if docs.is_empty()) {
                    break;
                }
            }
        }
        let candidates = candidates.unwrap_or_default();
        let after_postings = reader.metrics();
        if candidates.is_empty() {
            return Ok(PlacesShardLookup {
                candidate_count: 0,
                clause_candidate_counts,
                results: Vec::new(),
                read_metrics: after_postings,
                stages: PlacesReadStages {
                    directory: after_directory,
                    lexicon: after_lexicon.since(after_directory),
                    postings: after_postings.since(after_lexicon),
                    record_index: RangeReadMetrics::default(),
                    records: RangeReadMetrics::default(),
                },
                tokenizer_version: directory.tokenizer_version,
            });
        }
        let mut best: Vec<_> = candidates
            .iter()
            .map(|(doc_id, rank)| (*doc_id, *rank))
            .collect();
        best.sort_by(|left, right| right.1.cmp(&left.1).then(left.0.cmp(&right.0)));
        best.truncate(RESULT_LIMIT);

        let record_index = component(&directory, "record_index")?;
        let index_wants: Vec<_> = best
            .iter()
            .map(|(doc_id, _)| {
                if *doc_id >= directory.record_count as u64 {
                    return Err(Error::RustError(
                        "Places document ID exceeds record inventory".into(),
                    ));
                }
                checked_extent(
                    record_index.offset,
                    doc_id.saturating_mul(RECORD_INDEX_BYTES),
                    RECORD_INDEX_BYTES,
                    record_index.length,
                )
            })
            .collect::<Result<_>>()?;
        let index_bytes = reader
            .coalesced(
                &index_wants,
                RECORD_INDEX_COALESCE_GAP,
                RECORD_INDEX_MAX_RANGE_BYTES,
            )
            .await?;
        let mut positions = Vec::with_capacity(index_bytes.len());
        for bytes in index_bytes {
            let offset = u32::from_le_bytes(bytes[..4].try_into().expect("four-byte slice"));
            let length = u32::from_le_bytes(bytes[4..8].try_into().expect("four-byte slice"));
            if length == 0 || length as usize > MAX_RESULT_RECORD_BYTES {
                return Err(Error::RustError(
                    "Places projection extent exceeds hard cap".into(),
                ));
            }
            positions.push((u64::from(offset), u64::from(length)));
        }
        let after_record_index = reader.metrics();

        let records = component(&directory, "records")?;
        let record_wants: Vec<_> = positions
            .iter()
            .map(|(offset, length)| {
                checked_extent(records.offset, *offset, *length, records.length)
            })
            .collect::<Result<_>>()?;
        let records_bytes = reader
            .coalesced(&record_wants, RECORDS_COALESCE_GAP, MAX_RESULT_RANGE_BYTES)
            .await?;
        let results = records_bytes
            .iter()
            .map(|bytes| {
                decode_projection(bytes).map_err(|error| {
                    Error::RustError(format!("Invalid Places projection: {error}"))
                })
            })
            .collect::<Result<Vec<_>>>()?;
        let after_records = reader.metrics();
        Ok(PlacesShardLookup {
            candidate_count: candidates.len(),
            clause_candidate_counts,
            results,
            read_metrics: after_records,
            stages: PlacesReadStages {
                directory: after_directory,
                lexicon: after_lexicon.since(after_directory),
                postings: after_postings.since(after_lexicon),
                record_index: after_record_index.since(after_postings),
                records: after_records.since(after_record_index),
            },
            tokenizer_version: directory.tokenizer_version,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn v3_query_tokenizer_matches_global_producer_vectors() {
        assert_eq!(
            tokenize_query("  Caf\u{e9} / GOLDEN_gate  "),
            ["cafe", "golden_gate"]
        );
        assert_eq!(
            tokenize_query("\u{30b9}\u{30bf}\u{30fc}\u{30d0}\u{30c3}\u{30af}\u{30b9}"),
            [
                "\u{30b9}\u{30bf}\u{30fc}\u{30cf}\u{30c3}\u{30af}\u{30b9}",
                "\u{30b9}\u{30bf}",
                "\u{30bf}\u{30fc}",
                "\u{30fc}\u{30cf}",
                "\u{30cf}\u{30c3}",
                "\u{30c3}\u{30af}",
                "\u{30af}\u{30b9}",
            ]
        );
        assert_eq!(
            query_terms("\u{30b9}\u{30bf}\u{30fc}\u{30d0}\u{30c3}\u{30af}\u{30b9}"),
            ["\u{30b9}\u{30bf}\u{30fc}\u{30cf}\u{30c3}\u{30af}\u{30b9}"]
        );
    }

    fn varint(mut value: u64) -> Vec<u8> {
        let mut bytes = Vec::new();
        while value >= 0x80 {
            bytes.push((value as u8 & 0x7f) | 0x80);
            value >>= 7;
        }
        bytes.push(value as u8);
        bytes
    }

    #[test]
    fn lexicon_block_decodes_front_coding_and_posting_extents() {
        let mut bytes = varint(2);
        bytes.extend(varint(0));
        bytes.extend(varint(5));
        bytes.extend(b"alpha");
        bytes.extend(varint(0));
        bytes.extend(varint(3));
        bytes.extend(varint(1));
        bytes.extend(varint(0));
        bytes.extend(varint(4));
        bytes.extend(b"beta");
        bytes.extend(varint(3));
        bytes.extend(varint(3));
        bytes.extend(varint(1));
        let entries = decode_lexicon_block(&bytes, 2).unwrap();
        assert_eq!(entries[0].token, "alpha");
        assert_eq!(entries[1].token, "beta");
        assert_eq!(entries[1].posting_offset, 3);
    }

    #[test]
    fn posting_decoder_rejects_trailing_bytes() {
        let mut bytes = varint(7);
        bytes.extend([1, 255, 0]);
        assert!(decode_postings(&bytes, 1).is_err());
    }

    #[test]
    fn posting_decoder_preserves_and_validates_field_masks() {
        let mut bytes = varint(7);
        bytes.extend([FIELD_BRAND | FIELD_CATEGORY, 200]);
        assert_eq!(
            decode_postings(&bytes, 1).unwrap(),
            vec![(7, FIELD_BRAND | FIELD_CATEGORY, 200)]
        );

        let mut invalid = varint(7);
        invalid.extend([0, 200]);
        assert!(decode_postings(&invalid, 1).is_err());
    }

    #[test]
    fn clause_field_masks_and_head_eligibility_are_explicit() {
        let exact = PlacesClause::new("starbucks".into(), false, None).unwrap();
        let brand = PlacesClause::new("starbucks".into(), false, Some("brand".into())).unwrap();
        assert_eq!(exact.field_mask, FIELD_ALL);
        assert_eq!(brand.field_mask, FIELD_BRAND);
        assert!(exact.head_eligible());
        assert!(!brand.head_eligible());
        assert!(PlacesClause::new("s".into(), true, None).is_err());
    }

    #[test]
    fn catalog_point_route_prefers_the_smallest_covering_shard() {
        let catalog = PlacesCatalog {
            schema_version: 1,
            tokenizer_version: "nfkd-latin-fold-cjk-bigram-v2".into(),
            coverage: None,
            partition: None,
            shards: vec![
                PlacesCatalogShard {
                    id: "large".into(),
                    object: "large.pcsh".into(),
                    cell: None,
                    bbox: [-72.0, 41.0, -70.0, 43.0],
                    center: [-71.0, 42.0],
                },
                PlacesCatalogShard {
                    id: "small".into(),
                    object: "small.pcsh".into(),
                    cell: None,
                    bbox: [-71.5, 41.5, -70.5, 42.5],
                    center: [-71.0, 42.0],
                },
            ],
        };
        assert_eq!(catalog.route_point(-71.0, 42.0).unwrap().id, "small");
        assert!(catalog.route_point(0.0, 0.0).is_none());
    }

    fn spatial_shard(cell: &str) -> PlacesCatalogShard {
        let bbox = quadkey_bbox(cell).unwrap();
        PlacesCatalogShard {
            id: format!("q-{cell}"),
            object: format!("q-{cell}.pcsh"),
            cell: Some(cell.into()),
            bbox,
            center: [(bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0],
        }
    }

    #[test]
    fn spatial_catalog_routes_by_exact_quadkey_ownership() {
        let point_cell = point_quadkey(-71.0, 42.0, 4).unwrap();
        let sibling = if point_cell.ends_with('0') {
            format!("{}1", &point_cell[..3])
        } else {
            format!("{}0", &point_cell[..3])
        };
        let catalog = PlacesCatalog {
            schema_version: 2,
            tokenizer_version: "nfkd-latin-fold-cjk-bigram-v2".into(),
            coverage: Some([-180.0, -90.0, 180.0, 90.0]),
            partition: Some(PlacesPartition {
                scheme: PARTITION_SCHEME.into(),
                minimum_level: 4,
                maximum_level: 8,
                split_row_cap: 1_500_000,
                split_cells: vec![],
            }),
            shards: vec![spatial_shard(&point_cell), spatial_shard(&sibling)],
        };
        assert!(catalog.supported());
        assert_eq!(
            catalog.route_point(-71.0, 42.0).unwrap().cell.as_deref(),
            Some(point_cell.as_str())
        );
        assert!(catalog.route_point(0.0, -80.0).is_none());
    }

    #[test]
    fn prepared_spatial_catalog_routes_by_prefix_index() {
        let point_cell = point_quadkey(-71.0, 42.0, 8).unwrap();
        let leaf = point_cell[..6].to_string();
        let catalog = PlacesCatalog {
            schema_version: 2,
            tokenizer_version: TOKENIZER_VERSION.into(),
            coverage: Some([-180.0, -90.0, 180.0, 90.0]),
            partition: Some(PlacesPartition {
                scheme: PARTITION_SCHEME.into(),
                minimum_level: 4,
                maximum_level: 8,
                split_row_cap: 1_500_000,
                split_cells: vec![point_cell[..4].into(), point_cell[..5].into()],
            }),
            shards: vec![spatial_shard(&leaf)],
        };
        assert!(catalog.supported());
        let prepared = PreparedPlacesCatalog::new(catalog);
        assert_eq!(
            prepared.route_point(-71.0, 42.0).unwrap().cell.as_deref(),
            Some(leaf.as_str())
        );
        assert!(prepared.route_point(10.0, -80.0).is_none());
    }

    #[test]
    fn world_quadkey_matches_the_python_partition_contract() {
        assert_eq!(point_quadkey(-180.0, -90.0, 3).as_deref(), Some("000"));
        assert_eq!(point_quadkey(180.0, 90.0, 3).as_deref(), Some("333"));
        assert_eq!(point_quadkey(-71.0, 42.0, 6).as_deref(), Some("212231"));
        assert_eq!(
            quadkey_bbox("212231"),
            Some([-73.125, 39.375, -67.5, 42.1875])
        );
        // The shared cell-identifier vectors, generated by
        // tests/generate_reverse_cell_identifier_vectors.py and asserted against
        // `route()`, the Python mirrors and DuckDB by
        // tests/test_reverse_cell_identifier_vectors.py. This is the Rust half
        // of that gate: `point_quadkey` at level 8 must re-encode the same
        // `(y<<8)|x` grid, corners and boundary clamps included.
        let fixture = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/fixtures/reverse/cell-identifier-vectors-v1.json");
        let payload: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&fixture).unwrap()).unwrap();
        assert_eq!(
            payload["schema"].as_str(),
            Some("overture-reverse-cell-identifier-vectors-v1")
        );
        let vectors = payload["vectors"].as_array().unwrap();
        assert!(vectors.len() >= 300);
        for vector in vectors {
            let longitude_e7 = vector["longitude_e7"].as_i64().unwrap();
            let latitude_e7 = vector["latitude_e7"].as_i64().unwrap();
            let cell = vector["partition_cell"].as_str().unwrap();
            let key = u32::try_from(vector["partition_key"].as_u64().unwrap()).unwrap();
            let quadkey = vector["quadkey8"].as_str().unwrap();
            assert_eq!(
                point_quadkey(longitude_e7 as f64 / 1e7, latitude_e7 as f64 / 1e7, 8).as_deref(),
                Some(quadkey),
                "quadkey mismatch at E7 ({longitude_e7}, {latitude_e7})"
            );
            let mut x = 0_u32;
            let mut y = 0_u32;
            for digit in quadkey.bytes().map(|value| value - b'0') {
                x = (x << 1) | u32::from(digit & 1);
                y = (y << 1) | u32::from(digit >> 1);
            }
            assert_eq!(format!("{y:02x}{x:02x}"), cell);
            assert_eq!((y << 8) | x, key);
            // Every leaf key extends its hex cell key by base-4 sub-digits.
            for (level, leaf) in vector["leaf_keys"].as_array().unwrap().iter().enumerate() {
                let leaf = leaf.as_str().unwrap();
                assert_eq!(leaf.len(), 4 + level);
                assert!(leaf.starts_with(cell));
                assert!(leaf[4..]
                    .bytes()
                    .all(|digit| (b'0'..=b'3').contains(&digit)));
            }
        }
    }

    #[test]
    fn spatial_catalog_rejects_leaf_ancestor_overlap() {
        let parent = point_quadkey(-71.0, 42.0, 4).unwrap();
        let child = format!("{parent}0");
        let catalog = PlacesCatalog {
            schema_version: 2,
            tokenizer_version: "nfkd-latin-fold-cjk-bigram-v2".into(),
            coverage: Some([-180.0, -90.0, 180.0, 90.0]),
            partition: Some(PlacesPartition {
                scheme: PARTITION_SCHEME.into(),
                minimum_level: 4,
                maximum_level: 8,
                split_row_cap: 1_500_000,
                split_cells: vec![],
            }),
            shards: vec![spatial_shard(&parent), spatial_shard(&child)],
        };
        assert!(!catalog.supported());
    }

    #[test]
    fn projection_decodes_python_wire_shape() {
        let mut bytes = 42.0_f32.to_le_bytes().to_vec();
        bytes.extend((-71.0_f32).to_le_bytes());
        bytes.push(255);
        bytes.push(0);
        for value in ["id", "Name", "cafe", "Boston", "MA", "US"] {
            bytes.extend(varint(value.len() as u64));
            bytes.extend(value.as_bytes());
        }
        let place = decode_projection(&bytes).unwrap();
        assert_eq!(place.id, "id");
        assert_eq!(place.name, "Name");
        assert_eq!(place.country, "US");
    }

    fn directory_with_blocks(blocks: Vec<LexiconBlock>) -> Directory {
        Directory {
            schema_version: 1,
            tokenizer_version: "nfkd-latin-fold-cjk-bigram-v2".into(),
            record_count: 1,
            token_count: blocks.iter().map(|block| block.entries).sum(),
            lexicon_blocks: blocks,
            field_bits: HashMap::from([
                ("name".into(), FIELD_NAME),
                ("brand".into(), FIELD_BRAND),
                ("category".into(), FIELD_CATEGORY),
                ("context".into(), FIELD_CONTEXT),
            ]),
            components: HashMap::new(),
        }
    }

    fn matching_block(index: usize, length: u64) -> LexiconBlock {
        LexiconBlock {
            first: format!("ab{index:04}"),
            last: format!("ab{index:04}z"),
            offset: index as u64 * length,
            length,
            entries: 1,
        }
    }

    #[test]
    fn broad_prefix_rejects_too_many_lexicon_blocks_before_reading() {
        let blocks = (0..=MAX_LEXICON_BLOCKS)
            .map(|index| matching_block(index, 1))
            .collect();
        let directory = directory_with_blocks(blocks);
        let error = select_lexicon_blocks(&directory, "ab", true).unwrap_err();
        assert!(format!("{error:?}").contains("block selection exceeds hard cap"));
    }

    #[test]
    fn broad_prefix_rejects_aggregate_lexicon_bytes_before_reading() {
        let blocks = (0..5)
            .map(|index| matching_block(index, MAX_LEXICON_BLOCK_BYTES))
            .collect();
        let directory = directory_with_blocks(blocks);
        let error = select_lexicon_blocks(&directory, "ab", true).unwrap_err();
        assert!(format!("{error:?}").contains("byte total exceeds hard cap"));
    }

    #[test]
    fn single_character_prefix_is_unsupported() {
        let directory = directory_with_blocks(vec![matching_block(0, 1)]);
        assert!(select_lexicon_blocks(&directory, "a", true).is_err());
    }

    fn union_masked(
        matches: &[LexiconEntry],
        chunks: &[&[u8]],
    ) -> std::result::Result<BTreeMap<u64, u8>, Error> {
        union_clause_postings(matches, chunks, FIELD_ALL)
    }

    #[test]
    fn clause_posting_union_fails_closed_on_chunk_entry_misalignment() {
        let matches = vec![
            LexiconEntry {
                token: "alpha".into(),
                posting_offset: 0,
                posting_length: 3,
                posting_count: 1,
            },
            LexiconEntry {
                token: "beta".into(),
                posting_offset: 3,
                posting_length: 3,
                posting_count: 1,
            },
        ];
        let mut posting = varint(7);
        posting.extend([FIELD_NAME, 200]);
        let chunks: Vec<&[u8]> = vec![&posting];
        let error = union_masked(&matches, &chunks).unwrap_err();
        assert!(format!("{error:?}").contains("do not align"));
    }

    /// Slice every want of a gap-0 coalesce plan back out of the raw object,
    /// mirroring what `RangeReader::coalesced` returns per want.
    fn plan_slices<'a>(
        object: &'a [u8],
        wants: &[ByteRange],
        plan: &geocoder_core::pages::CoalescePlan,
    ) -> Vec<&'a [u8]> {
        let mut slices: Vec<Option<&[u8]>> = vec![None; wants.len()];
        for read in &plan.reads {
            for want in &read.wants {
                let start = usize::try_from(read.offset + want.relative_offset).unwrap();
                let length = usize::try_from(want.length).unwrap();
                slices[want.want_index] = Some(&object[start..start + length]);
            }
        }
        slices.into_iter().map(Option::unwrap).collect()
    }

    fn split_fixture_matches(object: &[u8], prefix: &str) -> (Directory, Vec<LexiconEntry>) {
        assert_eq!(&object[..8], MAGIC);
        let directory_length = u32::from_le_bytes(object[8..12].try_into().unwrap()) as usize;
        let directory: Directory =
            serde_json::from_slice(&object[12..12 + directory_length]).unwrap();
        let lexicon = fixture_component(object, component(&directory, "lexicon").unwrap());
        let blocks = select_lexicon_blocks(&directory, prefix, true).unwrap();
        let mut matches: Vec<LexiconEntry> = blocks
            .iter()
            .flat_map(|block| {
                let start = usize::try_from(block.offset).unwrap();
                let length = usize::try_from(block.length).unwrap();
                decode_lexicon_block(&lexicon[start..start + length], block.entries).unwrap()
            })
            .filter(|entry| entry.token.starts_with(prefix))
            .collect();
        matches.sort_by(|left, right| left.token.cmp(&right.token));
        (directory, matches)
    }

    fn split_fixture_ids(
        object: &[u8],
        directory: &Directory,
        docs: &BTreeMap<u64, u8>,
    ) -> Vec<String> {
        let record_index = fixture_component(object, component(directory, "record_index").unwrap());
        let records = fixture_component(object, component(directory, "records").unwrap());
        docs.keys()
            .map(|doc_id| {
                let index_start = usize::try_from(*doc_id).unwrap() * RECORD_INDEX_BYTES as usize;
                let offset = u32::from_le_bytes(
                    record_index[index_start..index_start + 4]
                        .try_into()
                        .unwrap(),
                ) as usize;
                let length = u32::from_le_bytes(
                    record_index[index_start + 4..index_start + 8]
                        .try_into()
                        .unwrap(),
                ) as usize;
                decode_projection(&records[offset..offset + length])
                    .unwrap()
                    .id
            })
            .collect()
    }

    #[test]
    fn split_posting_prefix_plan_splits_and_excludes_dead_gap_bytes() {
        use geocoder_core::pages::coalesce_ranges;

        let object = include_bytes!("../../../tests/fixtures/places-pages/split.pcsh");
        let (directory, matches) = split_fixture_matches(object, "shared");
        let tokens: Vec<_> = matches.iter().map(|entry| entry.token.as_str()).collect();
        assert_eq!(tokens, ["shareda", "sharedz"]);
        let postings = component(&directory, "postings").unwrap();
        let wants: Vec<_> = matches
            .iter()
            .map(|entry| {
                checked_extent(
                    postings.offset,
                    entry.posting_offset,
                    entry.posting_length,
                    postings.length,
                )
                .unwrap()
            })
            .collect();
        let plan = coalesce_ranges(&wants, 0, MAX_POSTING_BYTES).unwrap();
        // Non-adjacent matched entries (gapx's postings sit between them) must
        // split the gap-0 plan instead of merging across the dead gap.
        assert!(plan.reads.len() > 1);
        assert_eq!(plan.reads.len(), 2);
        // Byte accounting excludes the dead gap: the plan fetches exactly the
        // matched entries' extents, nothing between them.
        let planned: u64 = plan.reads.iter().map(|read| read.length).sum();
        let matched: u64 = matches.iter().map(|entry| entry.posting_length).sum();
        assert_eq!(planned, matched);
        let span = wants
            .iter()
            .map(|want| want.offset + want.length)
            .max()
            .unwrap()
            - wants.iter().map(|want| want.offset).min().unwrap();
        assert!(
            planned < span,
            "a span read would have fetched the dead gap"
        );
        // Candidate set equality against the fixture's brute-force truth: the
        // union of the split chunks stitched through the product code equals
        // exactly the docs whose names carry a shared* token.
        let chunks = plan_slices(object, &wants, &plan);
        let docs = union_masked(&matches, &chunks).unwrap();
        let ids = split_fixture_ids(object, &directory, &docs);
        assert_eq!(ids, ["split-02", "split-05"]);
    }

    #[test]
    fn adjacent_posting_prefix_plan_merges_into_one_read() {
        use geocoder_core::pages::coalesce_ranges;

        let object = include_bytes!("../../../tests/fixtures/places-pages/split.pcsh");
        let (directory, matches) = split_fixture_matches(object, "adj");
        let tokens: Vec<_> = matches.iter().map(|entry| entry.token.as_str()).collect();
        assert_eq!(tokens, ["adja", "adjb"]);
        let postings = component(&directory, "postings").unwrap();
        let wants: Vec<_> = matches
            .iter()
            .map(|entry| {
                checked_extent(
                    postings.offset,
                    entry.posting_offset,
                    entry.posting_length,
                    postings.length,
                )
                .unwrap()
            })
            .collect();
        let plan = coalesce_ranges(&wants, 0, MAX_POSTING_BYTES).unwrap();
        assert_eq!(plan.reads.len(), 1);
        let planned: u64 = plan.reads.iter().map(|read| read.length).sum();
        let matched: u64 = matches.iter().map(|entry| entry.posting_length).sum();
        assert_eq!(planned, matched);
        let chunks = plan_slices(object, &wants, &plan);
        let docs = union_masked(&matches, &chunks).unwrap();
        let ids = split_fixture_ids(object, &directory, &docs);
        assert_eq!(ids, ["split-00", "split-01", "split-02"]);
    }

    fn fixture_component<'a>(object: &'a [u8], component: &Component) -> &'a [u8] {
        let start = usize::try_from(component.offset).unwrap();
        let length = usize::try_from(component.length).unwrap();
        &object[start..start + length]
    }

    #[test]
    fn python_generated_full_shard_decodes_through_rust_components() {
        let object = include_bytes!("../../../tests/fixtures/places-pages/shard.pcsh");
        assert_eq!(&object[..8], MAGIC);
        let directory_length = u32::from_le_bytes(object[8..12].try_into().unwrap()) as usize;
        let directory: Directory =
            serde_json::from_slice(&object[12..12 + directory_length]).unwrap();
        let lexicon = fixture_component(object, component(&directory, "lexicon").unwrap());
        let blocks = select_lexicon_blocks(&directory, "shared", false).unwrap();
        let entry = blocks
            .iter()
            .flat_map(|block| {
                let start = usize::try_from(block.offset).unwrap();
                let length = usize::try_from(block.length).unwrap();
                decode_lexicon_block(&lexicon[start..start + length], block.entries).unwrap()
            })
            .find(|entry| entry.token == "shared")
            .unwrap();
        let postings = fixture_component(object, component(&directory, "postings").unwrap());
        let posting_start = usize::try_from(entry.posting_offset).unwrap();
        let posting_length = usize::try_from(entry.posting_length).unwrap();
        let mut docs = decode_postings(
            &postings[posting_start..posting_start + posting_length],
            entry.posting_count,
        )
        .unwrap();
        docs.sort_by(|left, right| right.2.cmp(&left.2).then(left.0.cmp(&right.0)));
        let doc_id = usize::try_from(docs[0].0).unwrap();
        let record_index =
            fixture_component(object, component(&directory, "record_index").unwrap());
        let index_start = doc_id * RECORD_INDEX_BYTES as usize;
        let record_offset = u32::from_le_bytes(
            record_index[index_start..index_start + 4]
                .try_into()
                .unwrap(),
        ) as usize;
        let record_length = u32::from_le_bytes(
            record_index[index_start + 4..index_start + 8]
                .try_into()
                .unwrap(),
        ) as usize;
        let records = fixture_component(object, component(&directory, "records").unwrap());
        let result =
            decode_projection(&records[record_offset..record_offset + record_length]).unwrap();
        assert_eq!(result.id, "fixture-00");
        assert_eq!(result.name, "Shared Cafe");
    }

    #[test]
    fn famous_pair_key_is_sorted_distinct_and_two_clause_only() {
        let tokyo = PlacesClause::new("tokyo".into(), false, None).unwrap();
        let tower = PlacesClause::new("tower".into(), false, None).unwrap();
        assert_eq!(
            famous_pair_key(&[tower.clone(), tokyo.clone()]).as_deref(),
            Some("e2:tokyo tower")
        );
        assert_eq!(
            famous_pair_key(&[tokyo.clone(), tower.clone()]).as_deref(),
            Some("e2:tokyo tower")
        );
        assert!(famous_pair_key(std::slice::from_ref(&tokyo)).is_none());
        assert!(famous_pair_key(&[tokyo.clone(), tokyo.clone()]).is_none());
        assert!(famous_pair_key(&[tokyo, tower.clone(), tower]).is_none());
    }

    #[test]
    fn head_index_sorts_pair_keys_before_exact_keys() {
        let mut bytes = Vec::new();
        for (key, offset, length) in [
            ("e2:alpha beta", 0_u64, 5_u64),
            ("e:alpha", 5, 7),
            ("e:beta", 12, 9),
        ] {
            bytes.extend(varint(key.len() as u64));
            bytes.extend(key.as_bytes());
            bytes.extend(varint(offset));
            bytes.extend(varint(length));
        }
        assert_eq!(
            find_head_entry(&bytes, "e2:alpha beta").unwrap(),
            Some((0, 5))
        );
        assert_eq!(find_head_entry(&bytes, "e:beta").unwrap(), Some((12, 9)));
        assert_eq!(find_head_entry(&bytes, "e2:alpha zeta").unwrap(), None);
        assert_eq!(find_head_entry(&bytes, "e:zeta").unwrap(), None);
    }

    #[test]
    fn head_directory_fails_closed_on_unknown_admission_or_pair_overrun() {
        let supported: HeadDirectory = serde_json::from_str(
            r#"{"schema_version":1,"key_count":4,"e2_key_count":1,"admission":"famous-unique-v1","components":{}}"#,
        )
        .unwrap();
        assert!(head_directory_supported(&supported));
        let legacy: HeadDirectory =
            serde_json::from_str(r#"{"schema_version":1,"key_count":4,"components":{}}"#).unwrap();
        assert!(head_directory_supported(&legacy));
        let unknown_admission: HeadDirectory = serde_json::from_str(
            r#"{"schema_version":1,"key_count":4,"e2_key_count":1,"admission":"famous-unique-v2","components":{}}"#,
        )
        .unwrap();
        assert!(!head_directory_supported(&unknown_admission));
        let pair_overrun: HeadDirectory = serde_json::from_str(
            r#"{"schema_version":1,"key_count":4,"e2_key_count":5,"admission":"famous-unique-v1","components":{}}"#,
        )
        .unwrap();
        assert!(!head_directory_supported(&pair_overrun));
        let undeclared_pairs: HeadDirectory = serde_json::from_str(
            r#"{"schema_version":1,"key_count":4,"e2_key_count":1,"components":{}}"#,
        )
        .unwrap();
        assert!(!head_directory_supported(&undeclared_pairs));
        let wrong_schema: HeadDirectory =
            serde_json::from_str(r#"{"schema_version":2,"key_count":4,"components":{}}"#).unwrap();
        assert!(!head_directory_supported(&wrong_schema));
    }

    #[test]
    fn python_generated_head_serves_famous_pair_and_rare_admitted_entries() {
        let object = include_bytes!("../../../tests/fixtures/places-pages/head.phrp");
        let directory_length = u32::from_le_bytes(object[8..12].try_into().unwrap()) as usize;
        let directory: HeadDirectory =
            serde_json::from_slice(&object[12..12 + directory_length]).unwrap();
        assert!(head_directory_supported(&directory));
        assert!(directory.e2_key_count >= 1);
        assert_eq!(directory.admission.as_deref(), Some(HEAD_ADMISSION_MARKER));
        let key_index = head_component(&directory, "key_index").unwrap();
        let index_start = usize::try_from(key_index.offset).unwrap();
        let index_bytes =
            &object[index_start..index_start + usize::try_from(key_index.length).unwrap()];
        let entries = head_component(&directory, "entries").unwrap();
        let fetch = |key: &str| {
            let (offset, length) = find_head_entry(index_bytes, key).unwrap().unwrap();
            let start = usize::try_from(entries.offset + offset).unwrap();
            decode_head_entry(&object[start..start + usize::try_from(length).unwrap()]).unwrap()
        };
        // The reader's pair probe constructs this key for either clause order.
        let fixture = PlacesClause::new("fixture".into(), false, None).unwrap();
        let tower = PlacesClause::new("tower".into(), false, None).unwrap();
        let pair_key = famous_pair_key(&[tower, fixture]).unwrap();
        assert_eq!(pair_key, "e2:fixture tower");
        let pair = fetch(&pair_key);
        assert_eq!(pair.len(), 1);
        assert_eq!(pair[0].id, "fixture-famous");
        // A single posting is below the density floor; the token is admitted
        // only through the famous set, with dense-entry semantics.
        let rare = fetch("e:tower");
        assert_eq!(rare.len(), 1);
        assert_eq!(rare[0].id, "fixture-famous");
    }

    #[test]
    fn python_generated_full_head_decodes_through_rust_components() {
        let object = include_bytes!("../../../tests/fixtures/places-pages/head.phrp");
        assert_eq!(&object[..8], HEAD_MAGIC);
        let directory_length = u32::from_le_bytes(object[8..12].try_into().unwrap()) as usize;
        let directory: HeadDirectory =
            serde_json::from_slice(&object[12..12 + directory_length]).unwrap();
        let key_index = head_component(&directory, "key_index").unwrap();
        let index_start = usize::try_from(key_index.offset).unwrap();
        let index_length = usize::try_from(key_index.length).unwrap();
        let (offset, length) =
            find_head_entry(&object[index_start..index_start + index_length], "e:shared")
                .unwrap()
                .unwrap();
        let entries = head_component(&directory, "entries").unwrap();
        let start = usize::try_from(entries.offset + offset).unwrap();
        let length = usize::try_from(length).unwrap();
        let results = decode_head_entry(&object[start..start + length]).unwrap();
        assert_eq!(results.len(), HEAD_RESULT_LIMIT);
        assert_eq!(results[0].id, "fixture-00");
        assert_eq!(results[9].id, "fixture-09");
    }

    #[test]
    #[ignore = "readiness workflow supplies freshly generated Python artifacts"]
    fn readiness_queries_dynamic_python_places_fixtures() {
        let shard_path =
            std::env::var("GLOBAL_V2_READINESS_PLACES_SHARD").expect("readiness Places shard path");
        let head_path =
            std::env::var("GLOBAL_V2_READINESS_PLACES_HEAD").expect("readiness Places head path");
        let shard = std::fs::read(shard_path).expect("read readiness Places shard");
        let (directory, matches) = split_fixture_matches(&shard, "shared");
        let postings = fixture_component(&shard, component(&directory, "postings").unwrap());
        let wants: Vec<ByteRange> = matches
            .iter()
            .map(|entry| ByteRange {
                offset: entry.posting_offset,
                length: entry.posting_length,
            })
            .collect();
        let plan = geocoder_core::pages::coalesce_ranges(&wants, 0, MAX_POSTING_BYTES).unwrap();
        let chunks = plan_slices(postings, &wants, &plan);
        let docs = union_masked(&matches, &chunks).unwrap();
        let shard_ids = split_fixture_ids(&shard, &directory, &docs);

        let head = std::fs::read(head_path).expect("read readiness Places head");
        let directory_length = u32::from_le_bytes(head[8..12].try_into().unwrap()) as usize;
        let head_directory: HeadDirectory =
            serde_json::from_slice(&head[12..12 + directory_length]).unwrap();
        let key_index = head_component(&head_directory, "key_index").unwrap();
        let index_start = usize::try_from(key_index.offset).unwrap();
        let index_length = usize::try_from(key_index.length).unwrap();
        let (offset, length) =
            find_head_entry(&head[index_start..index_start + index_length], "e:shared")
                .unwrap()
                .unwrap();
        let entries = head_component(&head_directory, "entries").unwrap();
        let start = usize::try_from(entries.offset + offset).unwrap();
        let results =
            decode_head_entry(&head[start..start + usize::try_from(length).unwrap()]).unwrap();
        let head_ids: Vec<&str> = results.iter().map(|place| place.id.as_str()).collect();
        println!(
            "GLOBAL_V2_READINESS_JSON={}",
            serde_json::json!({
                "head_ids": head_ids,
                "shard_first_id": shard_ids.first().expect("Places shard result"),
            })
        );
    }
}
