//! Strict range reader for the experimental compact Places spatial shard.

use std::collections::{BTreeMap, HashMap, HashSet};

use geocoder_core::pages::{format_uuid, ByteRange, ByteReader, PageError};
use serde::{Deserialize, Serialize};
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
const MAX_CATALOG_BYTES: usize = 256 * 1024;
const MAX_CATALOG_SHARDS: usize = 4096;

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
    components: HashMap<String, Component>,
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
    pub bbox: [f64; 4],
    pub center: [f64; 2],
}

#[derive(Debug, Deserialize, Serialize)]
pub(crate) struct PlacesCatalog {
    schema_version: u32,
    tokenizer_version: String,
    pub shards: Vec<PlacesCatalogShard>,
}

impl PlacesCatalog {
    pub(crate) fn route_context(&self, context: &str) -> Option<&PlacesCatalogShard> {
        self.shards.iter().find(|shard| shard.id == context)
    }

    pub(crate) fn route_point(&self, longitude: f64, latitude: f64) -> Option<&PlacesCatalogShard> {
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
}

#[derive(Debug, Serialize)]
pub(crate) struct PlacesCatalogLookup {
    pub catalog: PlacesCatalog,
    pub read_metrics: RangeReadMetrics,
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
    pub clause_candidate_counts: Vec<usize>,
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
    pub(crate) async fn lookup_places_catalog_spike(
        &self,
        object_key: &str,
    ) -> Result<PlacesCatalogLookup> {
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
        if catalog.schema_version != 1
            || catalog.tokenizer_version != "nfkd-latin-fold-cjk-bigram-v2"
            || catalog.shards.is_empty()
            || catalog.shards.len() > MAX_CATALOG_SHARDS
        {
            return Err(Error::RustError(
                "Unsupported Places catalog contract".into(),
            ));
        }
        let mut ids = HashSet::new();
        for shard in &catalog.shards {
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
            let [xmin, ymin, xmax, ymax] = shard.bbox;
            let valid_geometry = shard.bbox.iter().all(|value| value.is_finite())
                && shard.center.iter().all(|value| value.is_finite())
                && (-180.0..=180.0).contains(&xmin)
                && (-180.0..=180.0).contains(&xmax)
                && (-90.0..=90.0).contains(&ymin)
                && (-90.0..=90.0).contains(&ymax)
                && xmin <= xmax
                && ymin <= ymax
                && (xmin..=xmax).contains(&shard.center[0])
                && (ymin..=ymax).contains(&shard.center[1]);
            if !valid_id || !valid_object || !valid_geometry || !ids.insert(shard.id.as_str()) {
                return Err(Error::RustError(
                    "Places catalog shard is outside hard bounds".into(),
                ));
            }
        }
        Ok(PlacesCatalogLookup {
            catalog,
            read_metrics: reader.metrics(),
        })
    }

    pub(crate) async fn lookup_places_head_spike(
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
        if directory.schema_version != 1
            || directory.key_count == 0
            || directory.key_count > MAX_HEAD_KEYS
        {
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
        let mut located = Vec::with_capacity(clauses.len());
        for clause in clauses {
            let head_key = format!("e:{}", clause.token);
            let Some(extent) = find_head_entry(&index_bytes, &head_key)
                .map_err(|error| Error::RustError(format!("Invalid Places head index: {error}")))?
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

    pub(crate) async fn lookup_places_shard_spike(
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
            || directory.tokenizer_version != "nfkd-latin-fold-cjk-bigram-v2"
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
        // Length is fixed to clauses.len(); positions for clauses skipped by the
        // early-exit below stay 0, so the diagnostic keeps a stable shape.
        let mut clause_candidate_counts = vec![0_usize; clauses.len()];
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
                let mut clause_docs: BTreeMap<u64, u8> = BTreeMap::new();
                for (entry, encoded) in matches.iter().zip(&posting_chunks) {
                    for (doc_id, field_mask, rank) in decode_postings(encoded, entry.posting_count)
                        .map_err(|error| {
                            Error::RustError(format!("Invalid Places posting: {error}"))
                        })?
                    {
                        if field_mask & clause.field_mask == 0 {
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
                clause_candidate_counts[position] = clause_docs.len();
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
            .coalesced(&index_wants, 64 * 1024, 256 * 1024)
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
            .coalesced(&record_wants, 256 * 1024, MAX_RESULT_RANGE_BYTES)
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
            shards: vec![
                PlacesCatalogShard {
                    id: "large".into(),
                    object: "large.pcsh".into(),
                    bbox: [-72.0, 41.0, -70.0, 43.0],
                    center: [-71.0, 42.0],
                },
                PlacesCatalogShard {
                    id: "small".into(),
                    object: "small.pcsh".into(),
                    bbox: [-71.5, 41.5, -70.5, 42.5],
                    center: [-71.0, 42.0],
                },
            ],
        };
        assert_eq!(catalog.route_point(-71.0, 42.0).unwrap().id, "small");
        assert!(catalog.route_point(0.0, 0.0).is_none());
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
}
